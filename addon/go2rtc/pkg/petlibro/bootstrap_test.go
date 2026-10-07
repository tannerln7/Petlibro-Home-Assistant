package petlibro

import (
	"bytes"
	"encoding/binary"
	"testing"
	"time"
)

func TestBootstrapIOCtrlOrder(t *testing.T) {
	for _, tt := range []struct {
		name      string
		sendDelay bool
		wantIDs   []uint32
	}{
		{
			name:    "captured default",
			wantIDs: []uint32{ioctlPetlibroStreamCtrl, ioctlGetVideoModeReq, ioctlGetStreamCtrlReq, ioctlGetAudioOutFormatReq, ioctlStart},
		},
		{
			name:      "AVAPI send delay immediately before start",
			sendDelay: true,
			wantIDs:   []uint32{ioctlPetlibroStreamCtrl, ioctlGetVideoModeReq, ioctlGetStreamCtrlReq, ioctlGetAudioOutFormatReq, ioctlInnerSendDataDelay, ioctlStart},
		},
	} {
		t.Run(tt.name, func(t *testing.T) {
			c := &Client{sendDelayCtrl: tt.sendDelay}
			cmds := c.bootstrapIOCtrls(qualityHD)
			if len(cmds) != len(tt.wantIDs) {
				t.Fatalf("got %d commands, want %d", len(cmds), len(tt.wantIDs))
			}
			for i, wantID := range tt.wantIDs {
				if got := binary.LittleEndian.Uint32(cmds[i].payload); got != wantID {
					t.Errorf("command %d ID=0x%04x, want 0x%04x", i, got, wantID)
				}
			}
		})
	}
}

func TestBootstrapAVAPIPayloadShapes(t *testing.T) {
	c := &Client{sendDelayCtrl: true}
	cmds := c.bootstrapIOCtrls(qualityHD)

	delay := cmds[len(cmds)-2].payload
	if len(delay) != 6 || binary.LittleEndian.Uint32(delay) != ioctlInnerSendDataDelay ||
		binary.LittleEndian.Uint16(delay[4:]) != 0 {
		t.Fatalf("send-delay IOCtrl=% x, want ff 00 00 00 00 00", delay)
	}

	start := cmds[len(cmds)-1].payload
	if len(start) != 12 || binary.LittleEndian.Uint32(start) != ioctlStart {
		t.Fatalf("IPCAM_START IOCtrl=% x, want 4-byte type plus 8-byte payload", start)
	}
	if !bytes.Equal(start[4:], make([]byte, 8)) {
		t.Fatalf("IPCAM_START SMsgAVIoctrlAVStream=% x, want channel 0 and zero reserved bytes", start[4:])
	}
}

func TestStreamCtrlVariants(t *testing.T) {
	standard := ioctlBody(ioctlSetStreamCtrlReq, []byte{0, 0, 0, 0, 7, 0, 0, 0})
	if len(standard) != 12 || binary.LittleEndian.Uint32(standard) != 0x0320 || standard[8] != 7 {
		t.Fatalf("standard SETSTREAMCTRL=% x", standard)
	}
	standardClient := &Client{streamCtrlVariant: streamCtrlStandard}
	if got := standardClient.bootstrapIOCtrls(standard)[0].channelFamily; got != 0x7000 {
		t.Fatalf("standard SETSTREAMCTRL channel=0x%04x, want 0x7000", got)
	}
	c := &Client{}
	cmds := c.bootstrapIOCtrls(nil)
	if got := binary.LittleEndian.Uint32(cmds[0].payload); got != ioctlGetVideoModeReq {
		t.Fatalf("none variant first IOCtrl=0x%04x", got)
	}
	if _, err := parseStreamCtrlVariant("bogus"); err == nil {
		t.Fatal("invalid streamctrl variant accepted")
	}
	if got, err := parseStreamCtrlQuality("255", "hd"); err != nil || got != 255 {
		t.Fatalf("quality=%d err=%v", got, err)
	}
}

func TestParseHDProbeWait(t *testing.T) {
	for _, value := range []string{"", "0"} {
		if got, err := parseHDProbeWait(value); err != nil || got != 0 {
			t.Fatalf("parseHDProbeWait(%q)=%s, %v; want zero", value, got, err)
		}
	}
	if got, err := parseHDProbeWait("8000"); err != nil || got != 8*time.Second {
		t.Fatalf("parseHDProbeWait(8000)=%s, %v", got, err)
	}
	for _, value := range []string{"-1", "bogus", "60001"} {
		if _, err := parseHDProbeWait(value); err == nil {
			t.Fatalf("parseHDProbeWait(%q) unexpectedly succeeded", value)
		}
	}
}
