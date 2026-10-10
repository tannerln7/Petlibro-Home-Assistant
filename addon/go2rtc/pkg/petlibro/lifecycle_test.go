package petlibro

import (
	"encoding/binary"
	"net"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

func TestClientCloseSendsMediaStopOnce(t *testing.T) {
	server, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	clientConn, err := net.ListenUDP("udp4", nil)
	if err != nil {
		t.Fatal(err)
	}
	c := &Client{
		conn: clientConn, cam: server.LocalAddr().(*net.UDPAddr), nonce: make([]byte, 8),
		frames: make(chan *Packet), done: make(chan struct{}),
	}
	c.mediaStartSent.Store(true)

	if err := c.Close(); err != nil {
		t.Fatal(err)
	}
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}

	if err := server.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 2048)
	n, _, err := server.ReadFromUDP(buf)
	if err != nil {
		t.Fatal(err)
	}
	plain := tutk.ReverseTransCodePartial(nil, buf[:n])
	if len(plain) < 0x1c+48 {
		t.Fatalf("short stop datagram: %d", len(plain))
	}
	inner := plain[0x1c:]
	body := xorBody(inner[36:48])
	if got := binary.LittleEndian.Uint32(body); got != ioctlStop {
		t.Fatalf("shutdown IOCtrl=0x%04x, want IPCAM_STOP 0x%04x", got, ioctlStop)
	}

	if err := server.SetReadDeadline(time.Now().Add(50 * time.Millisecond)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := server.ReadFromUDP(buf); err == nil {
		t.Fatal("second Close sent a duplicate IPCAM_STOP")
	}
}

func TestCloseAfterStartTransmissionBeforeBootstrapCompletion(t *testing.T) {
	server, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer server.Close()
	clientConn, err := net.ListenUDP("udp4", nil)
	if err != nil {
		t.Fatal(err)
	}
	c := &Client{
		conn: clientConn, cam: server.LocalAddr().(*net.UDPAddr), nonce: make([]byte, 8),
		frames: make(chan *Packet), done: make(chan struct{}),
	}
	start := bootstrapCmd{channelFamily: 0x7000, payload: ioctlBody12(ioctlStart), startsMedia: true}
	if err := c.sendBootstrapCommand(start, 5); err != nil {
		t.Fatal(err)
	}
	// Simulate any error after START but before bootstrap declares AV readiness.
	if err := c.Close(); err != nil {
		t.Fatal(err)
	}

	if err := server.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	buf := make([]byte, 2048)
	want := []uint32{ioctlStart, ioctlStop}
	for i, wantID := range want {
		n, _, err := server.ReadFromUDP(buf)
		if err != nil {
			t.Fatalf("datagram %d: %v", i, err)
		}
		plain := tutk.ReverseTransCodePartial(nil, buf[:n])
		if len(plain) < 0x1c+48 {
			t.Fatalf("short datagram %d: %d", i, len(plain))
		}
		body := xorBody(plain[0x1c+36 : 0x1c+48])
		if got := binary.LittleEndian.Uint32(body); got != wantID {
			t.Fatalf("datagram %d IOCtrl=0x%04x, want 0x%04x", i, got, wantID)
		}
	}
}
