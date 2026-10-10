package petlibro

// Camera is the PLAF203 behavior adapter. The Client below it speaks the
// Petlibro/TUTK wire protocol and emits complete access units; Camera turns
// those units into a stable, keyframe-ready, clocked camera stream. Nothing
// above this file needs to know about IOCtrl, FRAMEINFO, transport families,
// fragment indexes, or retransmission windows.

import (
	"bytes"
	"crypto/sha256"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"sync"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/aac"
	"github.com/AlexxIT/go2rtc/pkg/creds"
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
	SessionID() string
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
	id             string
	producerID     uint32
	openedAt       time.Time
	transport      cameraTransport
	quality        string
	audio          bool
	status         *runtimeStatusWriter
	desc           CameraDescription
	ready          *Packet
	videoTime      cameraClock
	audioSeq       uint32
	healthTick     *time.Ticker
	closeOnce      sync.Once
	closeErr       error
	preparing      bool
	lastSPS        []byte
	lastWidth      uint16
	lastHeight     uint16
	lastProfile    byte
	lastLevel      byte
	videoHanded    uint64
	audioHanded    uint64
	videoForwarded uint64
	audioForwarded uint64
}

func OpenCamera(rawURL string) (*Camera, error) {
	return openCamera(rawURL, 0)
}

func openCamera(rawURL string, producerID uint32) (*Camera, error) {
	creds.RegisterEndpointSecrets(rawURL)
	opts, err := parseCameraOptions(rawURL)
	if err != nil {
		return nil, err
	}
	var lastErr error
	var status *runtimeStatusWriter
	adapterID := newAdapterID()
	openedAt := time.Now()
	log.Debug().Str("camera_adapter_id", adapterID).Uint32("producer_id", producerID).
		Str("quality", opts.quality).Bool("audio", opts.audio).
		Msg("petlibro camera adapter opening")
	for attempt := 1; attempt <= 3; attempt++ {
		// Each retry is a new physical camera session and therefore a new SPS
		// epoch. Do not carry probe/transition metadata across attempts.
		status = newRuntimeStatusWriter(opts.statusFile, opts.quality)
		client, err := dialTransport(rawURL, opts.quality, opts.audio, cameraCorrelation{
			adapterID: adapterID, producerID: producerID, startupAttempt: attempt,
		})
		if err == nil {
			camera := newCamera(client, opts.quality, opts.audio, status)
			camera.id, camera.producerID, camera.openedAt = adapterID, producerID, openedAt
			status.setStatus("probing")
			log.Debug().Str("camera_adapter_id", adapterID).
				Str("physical_session_id", client.SessionID()).Int("startup_attempt", attempt).
				Msg("petlibro camera readiness preparation started")
			err = camera.prepare(cameraProbeTimeout, hdStartupStabilization)
			if err == nil {
				camera.startHealthUpdates()
				status.setStatus("online")
				log.Debug().Str("camera_adapter_id", adapterID).
					Str("physical_session_id", client.SessionID()).
					Uint16("width", camera.desc.Width).Uint16("height", camera.desc.Height).
					Bool("audio_ready", len(camera.desc.AudioConfig) != 0).
					Dur("elapsed", time.Since(openedAt)).Msg("petlibro camera media readiness handed off")
				log.Info().Str("camera_adapter_id", adapterID).
					Str("physical_session_id", client.SessionID()).Uint32("producer_id", producerID).
					Uint16("width", camera.desc.Width).Uint16("height", camera.desc.Height).
					Msg("petlibro camera stream available")
				return camera, nil
			}
			_ = client.Close()
		}
		lastErr = err
		if !retryCameraStartup(err) || attempt == 3 {
			break
		}
		log.Debug().Str("camera_adapter_id", adapterID).Int("startup_attempt", attempt).
			Err(err).Msg("petlibro camera startup attempt will retry")
		time.Sleep(time.Duration(attempt) * 500 * time.Millisecond)
	}
	status.setStatus("error")
	log.Error().Str("camera_adapter_id", adapterID).Uint32("producer_id", producerID).
		Err(lastErr).Dur("elapsed", time.Since(openedAt)).Msg("petlibro camera adapter failed to open")
	return nil, lastErr
}

func retryCameraStartup(err error) bool {
	return errors.Is(err, errLoginResponseTimeout) ||
		errors.Is(err, errCameraProbeTimeout) || errors.Is(err, io.EOF)
}

func newCamera(transport cameraTransport, quality string, audio bool, status *runtimeStatusWriter) *Camera {
	return &Camera{
		id: newAdapterID(), openedAt: time.Now(), transport: transport,
		quality: quality, audio: audio, status: status,
	}
}

func (c *Camera) startHealthUpdates() {
	if c.healthTick == nil {
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
	c.preparing = true
	defer func() { c.preparing = false }()
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
		log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Uint16("width", width).Uint16("height", height).
			Msg("petlibro camera selected startup IDR")
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
			log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
				Msg("petlibro camera discarded stale startup IDR and is awaiting a continuous GOP")
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
				log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
					Uint16("width", c.desc.Width).Uint16("height", c.desc.Height).
					Dur("stabilization_wait", hdWait).
					Msg("petlibro camera HD stabilization ended at current resolution")
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
							log.Debug().Str("camera_adapter_id", c.id).
								Str("physical_session_id", c.transport.SessionID()).
								Uint16("width", width).Uint16("height", height).
								Msg("petlibro camera HD stabilization selected higher resolution")
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
					log.Debug().Str("camera_adapter_id", c.id).
						Str("physical_session_id", c.transport.SessionID()).
						Msg("petlibro camera audio configuration ready")
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
	digest := sha256.Sum256(sps)
	configID := fmt.Sprintf("%x", digest[:4])
	profile, level := sps[1], sps[3]
	if len(c.lastSPS) == 0 {
		log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Str("codec_config_id", configID).Uint16("width", width).Uint16("height", height).
			Uint8("profile_idc", profile).Uint8("level_idc", level).
			Msg("petlibro camera observed initial H.264 configuration")
	} else if bytes.Equal(c.lastSPS, sps) {
		log.Trace().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Str("codec_config_id", configID).Msg("petlibro camera observed repeated H.264 configuration")
	} else {
		change := "sps"
		if width != c.lastWidth || height != c.lastHeight {
			change = "resolution"
		} else if profile != c.lastProfile || level != c.lastLevel {
			change = "profile_level"
		}
		event := log.Info()
		if c.preparing {
			event = log.Debug()
		}
		event.Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Str("change", change).Str("codec_config_id", configID).
			Uint16("width", width).Uint16("height", height).
			Uint8("profile_idc", profile).Uint8("level_idc", level).
			Msg("petlibro camera H.264 configuration changed")
	}
	c.lastSPS = append(c.lastSPS[:0], sps...)
	c.lastWidth, c.lastHeight = width, height
	c.lastProfile, c.lastLevel = profile, level
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
				snapshot := c.transport.healthSnapshot()
				c.status.updateHealth(snapshot)
				log.Debug().Str("camera_adapter_id", c.id).
					Str("physical_session_id", c.transport.SessionID()).
					Dur("uptime", time.Since(c.openedAt)).
					Uint64("adapter_video_units", c.videoHanded).Uint64("adapter_audio_units", c.audioHanded).
					Uint64("producer_video_units", c.videoForwarded).Uint64("producer_audio_units", c.audioForwarded).
					Uint16("width", c.lastWidth).Uint16("height", c.lastHeight).
					Uint64("dropped_frames", snapshot.vidDropped).
					Uint64("missing_fragments", snapshot.missingFragmentsTotal).
					Uint64("reader_queue_drops", snapshot.readerDrops).
					Uint64("output_queue_drops", snapshot.emitDrops).
					Uint64("ack_pending", snapshot.ackSeenPending).
					Msg("petlibro camera media health")
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
		c.videoHanded++
		_, _, _ = c.observeVideoConfig(pkt.Payload)
		unit.Timestamp = c.videoTime.normalize(pkt.CameraTimeMS, pkt.HasCameraTime)
	case CodecAACADTS:
		unit.Codec = MediaAAC
		c.audioHanded++
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

func (c *Camera) recordForwarded(codec MediaCodec) {
	if codec == MediaH264 {
		c.videoForwarded++
	} else if codec == MediaAAC {
		c.audioForwarded++
	}
}

func (c *Camera) Close() error {
	c.closeOnce.Do(func() {
		log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Uint32("producer_id", c.producerID).Msg("petlibro camera adapter closing")
		if c.healthTick != nil {
			c.healthTick.Stop()
		}
		c.status.updateHealth(c.transport.healthSnapshot())
		c.closeErr = c.transport.Close()
		c.status.markOffline()
		log.Debug().Str("camera_adapter_id", c.id).Str("physical_session_id", c.transport.SessionID()).
			Uint32("producer_id", c.producerID).Err(c.closeErr).
			Dur("uptime", time.Since(c.openedAt)).Msg("petlibro camera adapter closed")
	})
	return c.closeErr
}
