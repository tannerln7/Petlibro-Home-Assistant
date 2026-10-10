package petlibro

// This file is intentionally the boring go2rtc edge. Physical-camera
// readiness, keyframe gating, timestamps, retries, and shutdown semantics are
// owned by Camera; wire behavior is owned by Client.

import (
	"fmt"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/aac"
	"github.com/AlexxIT/go2rtc/pkg/core"
	"github.com/AlexxIT/go2rtc/pkg/h264"
	"github.com/AlexxIT/go2rtc/pkg/h264/annexb"
	"github.com/pion/rtp"
)

type Producer struct {
	core.Connection
	camera   *Camera
	videoSeq uint16
	audioSeq uint16
}

func NewProducer(rawURL string) (*Producer, error) {
	producerID := core.NewID()
	startedAt := time.Now()
	log.Debug().Uint32("producer_id", producerID).Msg("petlibro producer construction started")
	camera, err := openCamera(rawURL, producerID)
	if err != nil {
		log.Debug().Uint32("producer_id", producerID).Err(err).
			Dur("elapsed", time.Since(startedAt)).Msg("petlibro producer construction failed")
		return nil, err
	}

	desc := camera.Description()
	video := h264.AVCCToCodec(annexb.EncodeToAVCC(desc.VideoConfig))
	if video == nil {
		_ = camera.Close()
		return nil, fmt.Errorf("petlibro: camera returned invalid H.264 configuration")
	}
	medias := []*core.Media{{
		Kind: core.KindVideo, Direction: core.DirectionRecvonly,
		Codecs: []*core.Codec{video},
	}}
	if len(desc.AudioConfig) != 0 {
		audio := aac.ADTSToCodec(desc.AudioConfig)
		if audio == nil {
			_ = camera.Close()
			return nil, fmt.Errorf("petlibro: camera returned invalid AAC configuration")
		}
		audio.PayloadType = core.PayloadTypeRAW
		medias = append(medias, &core.Media{
			Kind: core.KindAudio, Direction: core.DirectionRecvonly,
			Codecs: []*core.Codec{audio},
		})
	}

	producer := &Producer{
		Connection: core.Connection{
			ID: producerID, FormatName: "petlibro",
			Protocol: camera.Protocol(), RemoteAddr: camera.RemoteAddr().String(),
			Source: rawURL, Medias: medias, Transport: camera,
		},
		camera: camera,
	}
	log.Debug().Uint32("producer_id", producerID).Str("camera_adapter_id", camera.id).
		Str("physical_session_id", camera.transport.SessionID()).Int("media_count", len(medias)).
		Dur("elapsed", time.Since(startedAt)).Msg("petlibro producer media description ready")
	return producer, nil
}

func (p *Producer) Start() error {
	log.Debug().Uint32("producer_id", p.ID).Str("camera_adapter_id", p.camera.id).
		Str("physical_session_id", p.camera.transport.SessionID()).Msg("petlibro producer media forwarding started")
	videoStarted, audioStarted := false, false
	for {
		unit, err := p.camera.Read()
		if err != nil {
			log.Debug().Uint32("producer_id", p.ID).Str("camera_adapter_id", p.camera.id).
				Str("physical_session_id", p.camera.transport.SessionID()).Err(err).
				Msg("petlibro producer media forwarding ended")
			return err
		}
		if unit == nil {
			continue
		}

		var name string
		var packet *core.Packet
		switch unit.Codec {
		case MediaH264:
			payload := annexb.EncodeToAVCC(unit.Payload)
			if len(payload) < 5 {
				continue
			}
			p.videoSeq++
			name = core.CodecH264
			packet = &core.Packet{
				Header:  rtp.Header{SequenceNumber: p.videoSeq, Timestamp: unit.Timestamp},
				Payload: payload,
			}
		case MediaAAC:
			payload := unit.Payload
			if aac.IsADTS(payload) {
				frameLen := int(aac.ReadADTSSize(payload))
				if frameLen > aac.ADTSHeaderLen(payload) && frameLen <= len(payload) {
					payload = payload[:frameLen]
				}
				payload = payload[aac.ADTSHeaderLen(payload):]
			}
			p.audioSeq++
			name = core.CodecAAC
			packet = &core.Packet{
				Header: rtp.Header{
					Version: aac.RTPPacketVersionAAC, Marker: true,
					SequenceNumber: p.audioSeq, Timestamp: unit.Timestamp,
				},
				Payload: payload,
			}
		default:
			continue
		}

		for _, receiver := range p.Receivers {
			if receiver.Codec.Name == name {
				receiver.WriteRTP(packet)
				p.camera.recordForwarded(unit.Codec)
				if unit.Codec == MediaH264 && !videoStarted {
					videoStarted = true
					log.Debug().Uint32("producer_id", p.ID).Str("physical_session_id", p.camera.transport.SessionID()).
						Bool("keyframe", unit.Keyframe).Msg("petlibro producer forwarded first video access unit")
				} else if unit.Codec == MediaAAC && !audioStarted {
					audioStarted = true
					log.Debug().Uint32("producer_id", p.ID).Str("physical_session_id", p.camera.transport.SessionID()).
						Msg("petlibro producer forwarded first audio access unit")
				}
				break
			}
		}
	}
}
