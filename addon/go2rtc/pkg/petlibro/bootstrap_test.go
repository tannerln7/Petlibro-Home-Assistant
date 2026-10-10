package petlibro

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestBootstrapIOCtrlOrder(t *testing.T) {
	wantIDs := []uint32{
		ioctlPetlibroStreamCtrl, ioctlGetVideoModeReq, ioctlGetStreamCtrlReq,
		ioctlGetAudioOutFormatReq, ioctlInnerSendDataDelay, ioctlStart,
	}
	cmds := (&Client{}).bootstrapIOCtrls(qualityHD)
	if len(cmds) != len(wantIDs) {
		t.Fatalf("got %d commands, want %d", len(cmds), len(wantIDs))
	}
	for i, wantID := range wantIDs {
		if got := binary.LittleEndian.Uint32(cmds[i].payload); got != wantID {
			t.Errorf("command %d ID=0x%04x, want 0x%04x", i, got, wantID)
		}
	}
}

func TestBootstrapAVAPIPayloadShapes(t *testing.T) {
	cmds := (&Client{}).bootstrapIOCtrls(qualityHD)

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

func TestBootstrapUsesCapturedStreamControl(t *testing.T) {
	cmds := (&Client{}).bootstrapIOCtrls(qualityHD)
	if got := cmds[0].channelFamily; got != 0x1000 {
		t.Fatalf("SETSTREAMCTRL channel=0x%04x, want captured 0x1000", got)
	}
	if !bytes.Equal(cmds[0].payload, qualityHD) {
		t.Fatalf("SETSTREAMCTRL=% x, want captured body % x", cmds[0].payload, qualityHD)
	}
}

func TestStreamControlForRequestedQuality(t *testing.T) {
	for _, test := range []struct {
		quality string
		want    []byte
	}{
		{quality: "hd", want: qualityHD},
		{quality: "sd", want: qualitySD},
	} {
		got := streamControlForQuality(test.quality)
		if !bytes.Equal(got, test.want) {
			t.Errorf("quality %q body=% x, want % x", test.quality, got, test.want)
		}
		got[0] ^= 0xff
		if bytes.Equal(got, test.want) {
			t.Errorf("quality %q returned shared template storage", test.quality)
		}
	}
}
