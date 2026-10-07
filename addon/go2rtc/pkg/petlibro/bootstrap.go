package petlibro

import (
	"encoding/binary"
	"fmt"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// bootstrap runs the post-LOGIN IOCtrl sequence that primes the
// camera to start streaming: captured timing feedback/ACK/probe, then SETSTREAMCTRL +
// standard queries (GET_VIDEOMODE/GETSTREAMCTRL/GETAUDIOOUTFORMAT) +
// optional SND_DATA_DELAY + IPCAM_START + optional AUDIOSTART, with
// per-iteration ack collection, ending with the
// AV-ready ack that switches the camera into AV-streaming mode and
// initialises the wrap-counter state for the assembler.
//
// petlibro divergence vs pkg/tutk: tutk's equivalent post-login work
// is split between WriteCommand (pkg/tutk/conn.go:116, with 1s/5x
// retry on a single command) and Session16.SendIOCtrl
// (pkg/tutk/session16.go:113, builds a 0x00 0x70 control msg).  Petlibro
// instead drives a fixed bootstrap sequence with a 40 ms ack
// window per command (vs tutk's blocking 1s+retry model) and
// terminates with an AV-ready type-0x09 receive report. The channel IDs (0x1000 for SETSTREAMCTRL,
// 0x7000 for standard controls + IPCAM_START) are petlibro-specific.
type bootstrapCmd struct {
	channelFamily uint16
	payload       []byte
}

func (c *Client) debugIOCtrlResponse(inner []byte) {
	if !c.verbose || len(inner) < 40 {
		return
	}
	paylen := int(binary.LittleEndian.Uint32(inner[24:28]))
	if paylen < 4 || 36+paylen > len(inner) {
		return
	}
	body := xorBody(inner[36 : 36+paylen])
	id := binary.LittleEndian.Uint32(body)
	if id == ioctlGetStreamCtrlResp || id == ioctlSetStreamCtrlResp {
		log.Debug().Uint32("ioctl", id).Hex("body", body).Msg("petlibro STREAMCTRL response")
	}
}

func (c *Client) bootstrapIOCtrls(stream []byte) []bootstrapCmd {
	cmds := make([]bootstrapCmd, 0, 7)
	if len(stream) != 0 {
		streamChan := uint16(0x1000)
		if c.streamCtrlVariant == streamCtrlStandard {
			streamChan = 0x7000
		}
		cmds = append(cmds, bootstrapCmd{streamChan, stream})
	}
	cmds = append(cmds, []bootstrapCmd{
		{0x7000, ioctlBody12(ioctlGetVideoModeReq)},
		{0x7000, ioctlBody12(ioctlGetStreamCtrlReq)},
		{0x7000, ioctlBody12(ioctlGetAudioOutFormatReq)},
	}...)
	if c.sendDelayCtrl {
		// Match the public TUTK AVAPI Linux client: send the system
		// pacing IOCTRL with uint16(0) immediately before IPCAM_START.
		cmds = append(cmds, bootstrapCmd{0x7000, ioctlBody(ioctlInnerSendDataDelay, []byte{0, 0})})
	}
	cmds = append(cmds, bootstrapCmd{0x7000, ioctlBody12(ioctlStart)})
	if c.audio {
		// The app enables audio AFTER IPCAM_START via a separate
		// 0x0300 AUDIOSTART; do not also pack it into IPCAM_START.
		audioOn := ioctlBody12(ioctlAudioOn)
		audioOn[4] = 0x01
		cmds = append(cmds, bootstrapCmd{0x7000, audioOn})
	}
	return cmds
}

func (c *Client) bootstrap() error {
	// Captures of the official client start the bootstrap's 32-bit timing
	// values near 0xC000. The reason for that offset is not established, so
	// preserve the observed wire behavior without assigning it SDK semantics.
	// The type-0x09 field is independently observed as wall-clock ms modulo
	// 2^16 and therefore does not use this offset.
	tickBase := time.Now().UnixMilli()
	tick32 := func() uint32 { return uint32((time.Now().UnixMilli() - tickBase + 0xC000) & 0xFFFFFFFF) }
	tick16 := func() uint16 { return uint16(time.Now().UnixMilli() & 0xFFFF) }

	// 1. Preserve the captured startup timing exchange. LOGIN_RESP leaves the
	// reliable receive position at 1 in known-good Petlibro sessions.
	if err := c.sendInner(innerTimingFeedback(0, 0x1F, tick32(), 4)); err != nil {
		return err
	}
	c.acceptReliableControl(1)
	a := c.nextTransportACKForRange(c.icounter, 0xFFFF, 0xFFFF, nil, tick16())
	if err := c.sendInner(a.marshal()); err != nil {
		return err
	}
	c.icounter++
	if err := c.sendInner(innerTimingProbe(c.icounter, tick32())); err != nil {
		return err
	}
	c.icounter++

	// 2. IOCtrl bootstrap commands — match the official Petlibro app's
	// sequence (verified against PCAPdroid_22_May_08_31_19.pcap), with
	// an optional stock-AVAPI pacing command immediately before start:
	//
	//	1. SETSTREAMCTRL HD     (chan=0x1000, chan=1 type=0x3fff)
	//	2. GET_VIDEOMODE_REQ    (chan=0x7000, IOCtrl 0x0372)
	//	3. GETSTREAMCTRL_REQ    (chan=0x7000, IOCtrl 0x0322)
	//	4. GETAUDIOOUTFORMAT_REQ (chan=0x7000, IOCtrl 0x032A)
	//	5. (optional) INNER_SND_DATA_DELAY (chan=0x7000, uint16 value=0)
	//	6. IPCAM_START          (chan=0x7000, 8-byte zero AVStream body)
	//	7. (optional) AUDIOSTART if audio=true
	//
	// Previous bootstrap had two extra 0x0000 channel-init commands
	// and sent SETSTREAMCTRL after the vendor cmds rather than first.
	// On a real HD-capable camera this resulted in the camera dual-
	// streaming HD + SD with the SD IDR sometimes winning probe and
	// breaking decoding.
	var stream []byte
	switch c.streamCtrlVariant {
	case streamCtrlNone:
	case streamCtrlStandard:
		payload := make([]byte, 8) // u32 channel + u8 quality + 3 reserved
		payload[4] = c.streamCtrlQuality
		stream = ioctlBody(ioctlSetStreamCtrlReq, payload)
	default:
		if c.quality == "sd" {
			stream = append([]byte(nil), qualitySD...)
		} else {
			stream = append([]byte(nil), qualityHD...)
		}
		stream[4] = c.streamCtrlQuality
	}
	if len(stream) == 0 {
		log.Debug().Msgf("petlibro: bootstrap SETSTREAMCTRL variant=%s quality=%q configuredQuality=%d skipped", c.streamCtrlVariant, c.quality, c.streamCtrlQuality)
	} else {
		streamChan := uint16(0x1000)
		if c.streamCtrlVariant == streamCtrlStandard {
			streamChan = 0x7000
		}
		log.Debug().Msgf("petlibro: bootstrap SETSTREAMCTRL variant=%s quality=%q configuredQuality=%d chan=0x%04x body=% x", c.streamCtrlVariant, c.quality, c.streamCtrlQuality, streamChan, stream)
	}
	log.Debug().Msgf("petlibro: bootstrap send_delay_ctrl=%t IPCAM_START body=% x", c.sendDelayCtrl, ioctlBody12(ioctlStart))
	cmds := c.bootstrapIOCtrls(stream)

	var bootstrapAVMax uint16 = 0x3FFF
	for i, cm := range cmds {
		if err := c.sendInner(innerData(c.icounter, cm.channelFamily, uint16(i), cm.payload)); err != nil {
			return err
		}
		c.icounter++

		// 40 ms per-IOCtrl ack window.  Calibrated against PLAF203 on
		// 2.4 GHz Wi-Fi where the camera typically responds in
		// 8-25 ms; long-tail responses past 40 ms get caught by the
		// next iteration's pendingAck loop or by the maintenanceLoop
		// ack retransmits, so the trade-off is "occasional ack-drop
		// & retransmit" vs "longer stream-start latency".  Don't
		// raise without re-measuring the camera's response
		// distribution.
		_ = c.conn.SetReadDeadline(time.Now().Add(40 * time.Millisecond))
		for {
			buf := make([]byte, 65535)
			n, _, err := c.conn.ReadFromUDP(buf)
			if err != nil {
				break
			}
			// petlibro divergence vs pkg/tutk: tutk's Conn.Read at
			// pkg/tutk/conn.go:69 also calls ReverseTransCodePartial on
			// every datagram, but petlibro bypasses Conn.Read entirely
			// during bootstrap so that the 40 ms ack-window deadline
			// can apply to a raw UDP read rather than the
			// reader-goroutine path used post-bootstrap.
			pkt := tutk.ReverseTransCodePartial(nil, buf[:n])
			if len(pkt) < 0x1C+20 {
				continue
			}
			if binary.LittleEndian.Uint16(pkt[8:]) != msgSessionD2C {
				continue
			}
			inner := pkt[0x1C:]
			c.debugIOCtrlResponse(inner)
			if inner[0] != 0x0c {
				continue
			}
			if seq, ok := reliableControlSequence(inner); ok {
				c.acceptReliableControl(seq)
			}
			if media, ok := decodeBootstrapMedia(inner); ok && media.subWire > bootstrapAVMax {
				bootstrapAVMax = media.subWire
			}
		}
		a := c.nextTransportACKForRange(c.icounter, 0xFFFF, 0xFFFF, nil, tick16())
		if err := c.sendInner(a.marshal()); err != nil {
			return err
		}
		c.icounter++
	}

	// 3. AV-ready ack. Validate that the camera did not report a
	// sequence position below the 0x3FFF control-channel watermark.
	// The independent receive tracker starts at bootstrapAVMax and
	// advances only when post-bootstrap media packets are observed.
	if bootstrapAVMax < 0x3FFF {
		return fmt.Errorf("petlibro: bootstrap got AVMax=0x%04x, want >=0x3FFF — camera didn't ack any AV channel", bootstrapAVMax)
	}
	a = c.nextTransportACKForRange(c.icounter, 0x3FFF, bootstrapAVMax, nil, tick16())
	if err := c.sendInner(a.marshal()); err != nil {
		return err
	}
	c.icounter++

	if bootstrapAVMax == 0x3FFF {
		c.wrap = wrapSeq{ext: 0x4000}
	} else {
		c.wrap = wrapSeq{ext: uint64(bootstrapAVMax) + 1}
	}
	c.avNextExt = c.wrap.ext
	c.avHighExt = c.wrap.ext - 1
	c.avBuffer = make(map[uint64]*pendingFrag)
	c.initACKTracking(uint64(bootstrapAVMax))
	if c.verbose {
		log.Debug().Msgf("petlibro: bootstrap ready commands=%d AVMax=0x%04x avNext=0x%x reliableRecvACK=0x%04x streamctrlVariant=%s",
			len(cmds), bootstrapAVMax, c.avNextExt, c.reliableRecvACK, c.streamCtrlVariant)
	}
	return nil
}
