package petlibro

// Camera is the PLAF203 behavior adapter. The Client below it speaks the
// Petlibro/TUTK wire protocol and emits complete access units; Camera turns
// those units into a stable, keyframe-ready, clocked camera stream. Nothing
// above this file needs to know about IOCtrl, FRAMEINFO, transport families,
// fragment indexes, or retransmission windows.

import (
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/aac"
	"github.com/AlexxIT/go2rtc/pkg/h264"
)

const (
	cameraProbeTimeout     = 5 * time.Second
	hdStartupStabilization = 15 * time.Second
)

var errCameraProbeTimeout = errors.New("petlibro: camera readiness timeout")

type cameraTransport interface {
	packets() <-chan *Packet
	doneSignal() <-chan struct{}
	Close() error
	RemoteAddr() net.Addr
	Protocol() string
	healthSnapshot() countersSnapshot
}

// CameraDescription contains standard elementary-stream configuration. Video
// config is the selected SPS-bearing Annex-B IDR; audio config is one ADTS
// frame. The go2rtc integration converts these into its own codec types.
type CameraDescription struct {
	VideoConfig []byte
	AudioConfig []byte
	Width       uint16
	Height      uint16
}

// MediaCodec is deliberately semantic. Vendor codec IDs are translated at
// this boundary and never reach the go2rtc integration.
type MediaCodec uint8

const (
	MediaH264 MediaCodec = iota + 1
	MediaAAC
)

// MediaUnit is the normalized boundary presented by Camera. Its timestamp is
// already in the codec's RTP clock (90 kHz for H.264, sample rate for AAC).
type MediaUnit struct {
	Codec     MediaCodec
	Payload   []byte
	Timestamp uint32
	Keyframe  bool
}

type cameraClock struct {
	lastRaw    uint32
	elapsed    uint64
	rawElapsed uint64
	haveRaw    bool
	haveOutput bool
}

func (c *cameraClock) normalize(rawMS uint32, valid bool) uint32 {
	if !c.haveOutput {
		c.haveOutput = true
		if valid {
			c.lastRaw, c.haveRaw = rawMS, true
		}
		return 0
	}

	if !valid {
		c.elapsed++
		return uint32(c.elapsed)
	}
	if !c.haveRaw {
		c.lastRaw, c.haveRaw = rawMS, true
		c.elapsed++
		c.rawElapsed = c.elapsed
		return uint32(c.elapsed)
	}

	// A delta below half the uint32 range is forward movement, including
	// source-millisecond rollover. A larger delta is a camera clock reset or
	// genuine regression; rebase the source while preserving output continuity.
	delta := rawMS - c.lastRaw
	c.lastRaw = rawMS
	if delta >= 1<<31 {
		c.elapsed++
		c.rawElapsed = c.elapsed
	} else {
		candidate := c.rawElapsed + uint64(delta)*90
		c.rawElapsed = candidate
		if candidate <= c.elapsed {
			c.elapsed++
		} else {
			c.elapsed = candidate
		}
	}
	// elapsed is intentionally wider than RTP. Converting only at the boundary
	// preserves natural uint32 RTP rollover instead of mistaking it for a
	// backwards timestamp.
	return uint32(c.elapsed)
}

type cameraOptions struct {
	quality    string
	audio      bool
	statusFile string
}

func parseCameraOptions(rawURL string) (cameraOptions, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return cameraOptions{}, fmt.Errorf("petlibro: bad url: %w", err)
	}
	quality := u.Query().Get("quality")
	if quality == "" {
		quality = "hd"
	}
	if quality != "hd" && quality != "sd" {
		return cameraOptions{}, fmt.Errorf("petlibro: quality must be hd or sd (got %q)", quality)
	}
	return cameraOptions{
		quality: quality, audio: boolQuery(u.Query(), "audio"),
		statusFile: u.Query().Get("status_file"),
	}, nil
}

// Camera owns one physical viewing session. OpenCamera retries transient
// login/readiness failures here, below the go2rtc producer boundary.
type Camera struct {
	transport  cameraTransport
	quality    string
	audio      bool
	status     *runtimeStatusWriter
	desc       CameraDescription
	ready      *Packet
	videoTime  cameraClock
	audioSeq   uint32
	healthTick *time.Ticker
	closeOnce  sync.Once
	closeErr   error
}

func OpenCamera(rawURL string) (*Camera, error) {
	opts, err := parseCameraOptions(rawURL)
	if err != nil {
		return nil, err
	}
	var lastErr error
	var status *runtimeStatusWriter
	for attempt := 1; attempt <= 3; attempt++ {
		// Each retry is a new physical camera session and therefore a new SPS
		// epoch. Do not carry probe/transition metadata across attempts.
		status = newRuntimeStatusWriter(opts.statusFile, opts.quality)
		client, err := dialTransport(rawURL, opts.quality, opts.audio)
		if err == nil {
			camera := newCamera(client, opts.quality, opts.audio, status)
			status.setStatus("probing")
			err = camera.prepare(cameraProbeTimeout, hdStartupStabilization)
			if err == nil {
				camera.startHealthUpdates()
				status.setStatus("online")
				return camera, nil
			}
			_ = client.Close()
		}
		lastErr = err
		if !retryCameraStartup(err) || attempt == 3 {
			break
		}
		log.Warn().Msgf("petlibro: camera startup attempt %d failed: %v", attempt, err)
		time.Sleep(time.Duration(attempt) * 500 * time.Millisecond)
	}
	status.setStatus("error")
	return nil, lastErr
}

func retryCameraStartup(err error) bool {
	return errors.Is(err, errLoginResponseTimeout) ||
		errors.Is(err, errCameraProbeTimeout) || errors.Is(err, io.EOF)
}

func newCamera(transport cameraTransport, quality string, audio bool, status *runtimeStatusWriter) *Camera {
	return &Camera{transport: transport, quality: quality, audio: audio, status: status}
}

func (c *Camera) startHealthUpdates() {
	if c.status != nil && c.healthTick == nil {
		c.healthTick = time.NewTicker(5 * time.Second)
	}
}

func (c *Camera) healthUpdates() <-chan time.Time {
	if c.healthTick == nil {
		return nil
	}
	return c.healthTick.C
}

func resetTimer(timer *time.Timer, d time.Duration) {
	if !timer.Stop() {
		select {
		case <-timer.C:
		default:
		}
	}
	timer.Reset(d)
}

// prepare establishes the adapter invariant: the first video unit returned by
// Read is an SPS-bearing IDR followed by an unbroken live GOP. HD cameras have
// been observed to emit a lower-resolution epoch briefly, so HD readiness
// waits internally for the higher-resolution SPS instead of exposing a go2rtc
// probe-duration setting.
func (c *Camera) prepare(probeTimeout, hdWait time.Duration) error {
	timer := time.NewTimer(probeTimeout)
	defer timer.Stop()

	var selected *Packet
	var waitingForHD bool
	hdPreferenceComplete := c.quality != "hd"
	var discardedVideoAfterSelected bool
	var stabilizationWidth, stabilizationHeight uint16
	var haveAudio bool

	selectVideo := func(pkt *Packet, width, height uint16) {
		selected = clonePacket(pkt)
		c.desc.VideoConfig = append(c.desc.VideoConfig[:0], pkt.Payload...)
		c.desc.Width, c.desc.Height = width, height
		discardedVideoAfterSelected = false
	}
	finishIfReady := func() bool {
		if selected == nil || waitingForHD || (!haveAudio && c.audio) {
			return false
		}
		if discardedVideoAfterSelected {
			// Replaying selected now would skip reference pictures already
			// consumed during stabilization/audio discovery. Drop to the live
			// edge and bound the wait for a new parameter-set-bearing IDR by the
			// normal readiness timeout.
			selected = nil
			discardedVideoAfterSelected = false
			resetTimer(timer, probeTimeout)
			return false
		}
		c.ready = selected
		return true
	}

	for {
		select {
		case <-timer.C:
			if waitingForHD && selected != nil {
				waitingForHD = false
				hdPreferenceComplete = true
				if finishIfReady() {
					return nil
				}
				// finishIfReady resets the timer when it discards a stale
				// candidate. Audio discovery still needs its own bounded wait.
				if selected != nil {
					resetTimer(timer, probeTimeout)
				}
				continue
			}
			return errCameraProbeTimeout
		case <-c.transport.doneSignal():
			return io.EOF
		case pkt, ok := <-c.transport.packets():
			if !ok {
				return io.EOF
			}
			if pkt == nil {
				continue
			}
			if len(pkt.Payload) < 5 {
				if pkt.Codec == CodecH264 && selected != nil {
					discardedVideoAfterSelected = true
				}
				continue
			}
			switch pkt.Codec {
			case CodecH264:
				width, height, ok := c.observeVideoConfig(pkt.Payload)
				if ok && pkt.IsKeyframe {
					firstCandidate := selected == nil
					if firstCandidate {
						selectVideo(pkt, width, height)
					} else if waitingForHD {
						higherResolution := width > stabilizationWidth || height > stabilizationHeight
						selectVideo(pkt, width, height)
						if higherResolution {
							waitingForHD = false
							hdPreferenceComplete = true
						} else {
							if width > stabilizationWidth {
								stabilizationWidth = width
							}
							if height > stabilizationHeight {
								stabilizationHeight = height
							}
						}
					} else {
						// Audio discovery may outlive video stabilization. Keep its
						// most recent complete GOP head rather than replaying one
						// followed by video that preparation discarded.
						selectVideo(pkt, width, height)
					}

					if firstCandidate && !hdPreferenceComplete {
						stabilizationWidth, stabilizationHeight = width, height
						if c.quality == "hd" && (width < 1920 || height < 1080) {
							waitingForHD = true
							resetTimer(timer, hdWait)
						} else {
							hdPreferenceComplete = true
						}
					}
				} else if selected != nil {
					// This access unit will not be replayed. Any later handoff from
					// selected would therefore contain an unknown reference gap.
					discardedVideoAfterSelected = true
				}
			case CodecAACADTS:
				if !haveAudio && aac.IsADTS(pkt.Payload) {
					haveAudio = true
					c.desc.AudioConfig = append([]byte(nil), pkt.Payload...)
				}
			}
			if finishIfReady() {
				return nil
			}
		}
	}
}

func clonePacket(pkt *Packet) *Packet {
	clone := *pkt
	clone.Payload = append([]byte(nil), pkt.Payload...)
	return &clone
}

func (c *Camera) observeVideoConfig(payload []byte) (uint16, uint16, bool) {
	sps := annexbNAL(payload, h264.NALUTypeSPS)
	if len(sps) < 4 {
		return 0, 0, false
	}
	decoded := h264.DecodeSPS(sps)
	if decoded == nil {
		return 0, 0, false
	}
	width, height := decoded.Width(), decoded.Height()
	c.status.observeSPS(width, height, sps[1], sps[3])
	if c.transport != nil {
		log.Debug().Uint16("width", width).Uint16("height", height).
			Str("quality", c.quality).Msg("petlibro camera SPS epoch")
	}
	return width, height, true
}

func (c *Camera) Read() (*MediaUnit, error) {
	var pkt *Packet
	if c.ready != nil {
		pkt, c.ready = c.ready, nil
	} else {
		for pkt == nil {
			select {
			case <-c.transport.doneSignal():
				return nil, io.EOF
			case <-c.healthUpdates():
				c.status.updateHealth(c.transport.healthSnapshot())
			case next, ok := <-c.transport.packets():
				if !ok {
					return nil, io.EOF
				}
				pkt = next
			}
		}
	}
	if pkt == nil {
		return nil, nil
	}

	unit := &MediaUnit{Payload: pkt.Payload, Keyframe: pkt.IsKeyframe}
	switch pkt.Codec {
	case CodecH264:
		unit.Codec = MediaH264
		_, _, _ = c.observeVideoConfig(pkt.Payload)
		unit.Timestamp = c.videoTime.normalize(pkt.CameraTimeMS, pkt.HasCameraTime)
	case CodecAACADTS:
		unit.Codec = MediaAAC
		// One AAC-LC access unit advances the native AAC RTP clock by 1024
		// samples, independent of the sample rate advertised by ADTS.
		unit.Timestamp = c.audioSeq * 1024
		c.audioSeq++
	default:
		return nil, nil
	}
	return unit, nil
}

func (c *Camera) Description() CameraDescription {
	desc := c.desc
	desc.VideoConfig = append([]byte(nil), desc.VideoConfig...)
	desc.AudioConfig = append([]byte(nil), desc.AudioConfig...)
	return desc
}
func (c *Camera) RemoteAddr() net.Addr { return c.transport.RemoteAddr() }
func (c *Camera) Protocol() string     { return c.transport.Protocol() }

func (c *Camera) Close() error {
	c.closeOnce.Do(func() {
		if c.healthTick != nil {
			c.healthTick.Stop()
		}
		c.status.updateHealth(c.transport.healthSnapshot())
		c.closeErr = c.transport.Close()
		c.status.markOffline()
	})
	return c.closeErr
}
