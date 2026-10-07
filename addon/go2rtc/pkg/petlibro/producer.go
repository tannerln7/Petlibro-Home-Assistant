package petlibro

import (
	"encoding/binary"
	"fmt"
	"strings"
	"sync/atomic"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/aac"
	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/h264"
	"github.com/AlexxIT/go2rtc/pkg/h264/annexb"
	"github.com/pion/rtp"
)

type Producer struct {
	core.Connection
	client *Client

	// firstAU holds the SPS-bearing IDR access unit that probe()
	// consumed to derive the H.264 codec parameters.  Start() replays
	// it as the first emitted frame so consumers don't have to wait
	// up to a full GOP for the next IDR.  Annex-B encoded.
	firstAU []byte
	firstTS uint32

	// rtpSeq is the producer-side monotonic RTP sequence counter for
	// the video track.  Earlier code copied pkt.FrameNo (the camera's
	// per-channel frame counter, also used for the SPS-replay seq)
	// straight into the RTP header, which collided with the replayed
	// IDR's seq number and caused mpv to drop the second IDR as a
	// "non-monotonic" duplicate.
	rtpSeq atomic.Uint32
}

// NewProducer parses a petlibro:// URL, dials the camera, probes the
// codec (waits for an SPS so we can build a proper SDP), and returns a
// fully-populated Producer.
//
// URL shape (full grammar lives on pkg/petlibro.Dial):
//
//	petlibro://<host>?uid=<UID>[&audio=true][&quality=hd|sd][&send_delay_ctrl=1][&streamctrl_variant=legacy|standard|none][&streamctrl_quality=N][&hd_probe_wait_ms=N][&status_file=<path>][&strict=1][&verbose=1][&trace_ack=1][&trace_frag=1][&trace_frameinfo=1][&trace_packets=1]
//	petlibro://?uid=<UID>[&subnet=192.168.1.0/24][same options as above]
//
// strict=1 — pristine-pixels-over-fluency policy: any IDR with a lost
// fragment is dropped (instead of emitted with localised macroblock
// artefacts) and the rest of the GOP is suppressed until the next
// clean IDR.  Useful when downstream decoder errors are louder than
// the multi-second freezes strict mode causes on lossy networks.
func NewProducer(rawURL string) (*Producer, error) {
	var lastErr error
	for attempt := 1; attempt <= 3; attempt++ {
		producer, err := newProducer(rawURL)
		if err == nil {
			return producer, nil
		}
		lastErr = err
		if !retryStartupError(err) {
			break
		}
		log.Warn().Msgf("petlibro: startup attempt %d failed: %v", attempt, err)
		time.Sleep(time.Duration(attempt) * 500 * time.Millisecond)
	}
	return nil, lastErr
}

func newProducer(rawURL string) (*Producer, error) {
	client, err := Dial(rawURL)
	if err != nil {
		return nil, err
	}

	medias, firstAU, firstTS, err := probe(client)
	if err != nil {
		client.runtimeStatus.setStatus("error")
		_ = client.Close()
		return nil, err
	}
	client.runtimeStatus.setStatus("online")

	return &Producer{
		Connection: core.Connection{
			ID:         core.NewID(),
			FormatName: "petlibro",
			Protocol:   client.Protocol(),
			RemoteAddr: client.RemoteAddr().String(),
			Source:     rawURL,
			Medias:     medias,
			Transport:  client,
		},
		client:  client,
		firstAU: firstAU,
		firstTS: firstTS,
	}, nil
}

func retryStartupError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "LOGIN_RESP timeout") ||
		strings.Contains(msg, "petlibro: probe timeout") ||
		strings.Contains(msg, "petlibro: probe: EOF")
}

func (p *Producer) nextSeq() uint16 {
	return uint16(p.rtpSeq.Add(1))
}

func (p *Producer) Start() error {
	// probe() saved the SPS-bearing IDR.  Replay it as the first frame
	// so the downstream decoder gets a valid GOP head immediately and
	// doesn't have to wait up to a full GOP (~1-2 s) for the next IDR
	// from the camera.  Any P-frames already buffered behind probe's
	// IDR will reference that very IDR — they're now valid too.
	keyframeSeen := false
	if p.firstAU != nil {
		avcc := annexb.EncodeToAVCC(p.firstAU)
		if len(avcc) >= 5 {
			for _, recv := range p.Receivers {
				if recv.Codec.Name == core.CodecH264 {
					// Version=0 (default) is the AVCC-payload
					// sentinel that pkg/h264.RTPPay reads as "fragment
					// me into RTP packets".  Setting Version=2 here
					// would make RTPPay pass our AVCC payload through
					// as if already RTP-packetised — decoders would
					// then see an AVCC length header as a NAL byte and
					// produce only noise.
					recv.WriteRTP(&core.Packet{
						Header: rtp.Header{
							SequenceNumber: p.nextSeq(),
							Timestamp:      p.firstTS,
						},
						Payload: avcc,
					})
					break
				}
			}
		}
		keyframeSeen = true
	}

	for {
		pkt, err := p.client.ReadPacket()
		if err != nil {
			return err
		}
		if pkt == nil {
			continue
		}

		var name string
		var pkt2 *core.Packet

		switch pkt.Codec {
		case CodecH264:
			if !keyframeSeen {
				if !pkt.IsKeyframe {
					continue
				}
				keyframeSeen = true
			}
			name = core.CodecH264
			avcc := annexb.EncodeToAVCC(pkt.Payload)
			if len(avcc) < 5 {
				// AVCC needs at least a 4-byte length + 1 NAL header byte
				// or downstream RepairAVCC panics indexing payload[4].
				continue
			}
			pkt2 = &core.Packet{
				Header:  rtp.Header{SequenceNumber: p.nextSeq(), Timestamp: pkt.Timestamp},
				Payload: avcc,
			}

		case CodecAACADTS:
			name = core.CodecAAC
			payload := pkt.Payload
			if aac.IsADTS(payload) {
				// The camera occasionally appends padding bytes after the
				// real AAC frame; trim to the ADTS-declared length so the
				// RTP packetizer doesn't claim a too-large AU.
				frameLen := int(aac.ReadADTSSize(payload))
				if frameLen > aac.ADTSHeaderLen(payload) && frameLen <= len(payload) {
					payload = payload[:frameLen]
				}
				payload = payload[aac.ADTSHeaderLen(payload):]
			}
			pkt2 = &core.Packet{
				Header:  rtp.Header{Version: aac.RTPPacketVersionAAC, Marker: true, SequenceNumber: uint16(pkt.FrameNo), Timestamp: pkt.Timestamp},
				Payload: payload,
			}

		default:
			continue
		}

		for _, recv := range p.Receivers {
			if recv.Codec.Name == name {
				recv.WriteRTP(pkt2)
				break
			}
		}
	}
}

// avccContainsNALType walks an AVCC-encoded buffer (4-byte length
// prefix + NAL bytes, repeated) and reports whether any NAL unit's
// nal_unit_type matches want.  pkg/h264 doesn't ship an AVCC iterator
// today; rather than copying the same five-line walk into another
// adapter, this is the single place petlibro touches AVCC NALs.  See
// also annexbContainsNALType in client.go for the Annex-B equivalent.
func avccContainsNALType(avcc []byte, want byte) bool {
	for len(avcc) >= 5 {
		size := 4 + int(binary.BigEndian.Uint32(avcc))
		if size > len(avcc) || size < 5 {
			return false
		}
		if avcc[4]&0x1F == want {
			return true
		}
		avcc = avcc[size:]
	}
	return false
}

// probe reads frames until we have a video codec (with parameter sets)
// and, if audio was requested, an audio codec.  Returns the SPS-bearing
// IDR's full Annex-B AU so the producer can replay it as the first
// emitted frame, saving consumers up to one full GOP of wait time.
func probe(client *Client) ([]*core.Media, []byte, uint32, error) {
	timer := time.NewTimer(core.ProbeTimeout)
	defer timer.Stop()

	var vcodec, acodec *core.Codec
	var firstAU []byte
	var firstTS uint32
	var selectedWidth, selectedHeight uint16
	var selectedFrameNum uint32
	var selectedChannel, selectedOnlineNum byte
	var waitingForHD bool
	probeStarted := time.Now()

	resetTimer := func(d time.Duration) {
		if !timer.Stop() {
			select {
			case <-timer.C:
			default:
			}
		}
		timer.Reset(d)
	}
	logSelection := func(reason string) {
		if vcodec == nil {
			return
		}
		buf := annexb.EncodeToAVCC(firstAU)
		log.Debug().Msgf("petlibro: probe selected resolution=%dx%d reason=%s quality=%q strict=%t firstAU=%d containsSPS=%t containsPPS=%t containsIDR=%t codec=%q fmtp=%q frameNum=%d channel=%d onlineNum=%d",
			selectedWidth, selectedHeight, reason, client.quality, client.strict, len(firstAU),
			avccContainsNALType(buf, h264.NALUTypeSPS),
			avccContainsNALType(buf, h264.NALUTypePPS),
			avccContainsNALType(buf, h264.NALUTypeIFrame),
			vcodec.String(), vcodec.FmtpLine, selectedFrameNum, selectedChannel, selectedOnlineNum)
	}

probeLoop:
	for {
		var pkt *Packet
		select {
		case <-timer.C:
			if waitingForHD && vcodec != nil {
				waitingForHD = false
				logSelection("timeout")
				if acodec != nil || !client.Audio() {
					break probeLoop
				}
				timer.Reset(core.ProbeTimeout)
				continue
			}
			_ = client.Close()
			return nil, nil, 0, fmt.Errorf("petlibro: probe timeout")
		case <-client.done:
			return nil, nil, 0, fmt.Errorf("petlibro: probe: EOF")
		case p, ok := <-client.frames:
			if !ok {
				return nil, nil, 0, fmt.Errorf("petlibro: probe: EOF")
			}
			pkt = p
		}
		if pkt == nil || len(pkt.Payload) < 5 {
			continue
		}

		switch pkt.Codec {
		case CodecH264:
			buf := annexb.EncodeToAVCC(pkt.Payload)
			if !avccContainsNALType(buf, h264.NALUTypeSPS) {
				continue
			}
			codec := h264.AVCCToCodec(buf)
			if codec == nil {
				continue
			}
			sps := annexbNAL(pkt.Payload, h264.NALUTypeSPS)
			if len(sps) < 4 {
				continue
			}
			decoded := h264.DecodeSPS(sps)
			if decoded == nil {
				continue
			}
			width, height := decoded.Width(), decoded.Height()

			if vcodec == nil {
				vcodec = codec
				firstAU = append([]byte(nil), pkt.Payload...)
				firstTS = pkt.Timestamp
				selectedWidth, selectedHeight = width, height
				selectedFrameNum = pkt.CameraFrameNo
				selectedChannel = pkt.Channel
				selectedOnlineNum = pkt.OnlineNum
				if client.quality == "hd" && client.hdProbeWait > 0 && (width < 1920 || height < 1080) {
					waitingForHD = true
					resetTimer(client.hdProbeWait)
				} else {
					logSelection("first_sps")
				}
			} else if waitingForHD && (width != selectedWidth || height != selectedHeight) {
				log.Debug().Msgf("petlibro: spsChange elapsed=%dms old=%dx%d new=%dx%d frameNum=%d channel=%d onlineNum=%d",
					time.Since(probeStarted).Milliseconds(), selectedWidth, selectedHeight, width, height,
					pkt.CameraFrameNo, pkt.Channel, pkt.OnlineNum)
				if width > selectedWidth || height > selectedHeight {
					vcodec = codec
					firstAU = append(firstAU[:0], pkt.Payload...)
					firstTS = pkt.Timestamp
					selectedWidth, selectedHeight = width, height
					selectedFrameNum = pkt.CameraFrameNo
					selectedChannel = pkt.Channel
					selectedOnlineNum = pkt.OnlineNum
					waitingForHD = false
					logSelection("hd_wait_satisfied")
				}
			}
		case CodecAACADTS:
			if acodec == nil {
				// aac.ADTSToCodec validates the header and fills in
				// sample rate / channels / AudioSpecificConfig for us.
				if c := aac.ADTSToCodec(pkt.Payload); c != nil {
					c.PayloadType = core.PayloadTypeRAW
					acodec = c
				}
			}
		}

		if vcodec != nil && !waitingForHD && (acodec != nil || !client.Audio()) {
			break
		}
	}

	_ = client.SetDeadline(time.Time{})

	medias := []*core.Media{
		{
			Kind:      core.KindVideo,
			Direction: core.DirectionRecvonly,
			Codecs:    []*core.Codec{vcodec},
		},
	}
	if acodec != nil {
		medias = append(medias, &core.Media{
			Kind:      core.KindAudio,
			Direction: core.DirectionRecvonly,
			Codecs:    []*core.Codec{acodec},
		})
	}
	return medias, firstAU, firstTS, nil
}
