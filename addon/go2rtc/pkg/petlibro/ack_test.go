package petlibro

import (
	"encoding/binary"
	"net"
	"reflect"
	"testing"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

func TestTransportACKMarshal(t *testing.T) {
	a := transportACK{
		Ordinal: 0x1234, AVBase: 0xfffe, AVHigh: 2,
		ReliableRecvACK: 0x4567, Missing: []uint16{1, 3},
		State: 1, SendCount: 0x89ab, Tick: 0xcdef,
	}
	want := []byte{
		0x09, 0x00, 0x0c, 0x00, 0x34, 0x12, 0x00, 0x00,
		0xfe, 0xff, 0x02, 0x00, 0x67, 0x45, 0x02, 0x00,
		0x01, 0x00, 0xab, 0x89, 0xef, 0xcd, 0x01, 0x00,
		0x03, 0x00,
	}
	if got := a.marshal(); !reflect.DeepEqual(got, want) {
		t.Fatalf("marshal=% x\nwant   =% x", got, want)
	}
}

func ackFor(t *testing.T, c *Client) (transportACK, ackWindow) {
	t.Helper()
	a, w := c.nextTransportACK(7, 9)
	c.markTransportACKSent(w)
	return a, w
}

// retireAF203Pending models the type 0x09 receive loop in FUN_003b5374:
// every non-NACKed entry in (base, high] is removed from the sender queues.
func retireAF203Pending(pending map[uint64]bool, baseExt uint64, a transportACK) {
	missing := make(map[uint16]bool, len(a.Missing))
	for _, offset := range a.Missing {
		missing[offset] = true
	}
	span := uint16(a.AVHigh - a.AVBase)
	for offset := uint16(1); offset != span+1; offset++ {
		if !missing[offset] {
			delete(pending, baseExt+uint64(offset))
		}
	}
}

func TestTransportACKSequentialProgress(t *testing.T) {
	c := newTestClient(101, "hd") // bootstrap/report position is 100
	pending := map[uint64]bool{101: true, 102: true, 103: true}
	for seq := uint64(101); seq <= 103; seq++ {
		c.markACKReceived(seq)
	}
	a, w := ackFor(t, c)
	if a.AVBase != 100 || a.AVHigh != 103 || len(a.Missing) != 0 || w.seenPending != 0 {
		t.Fatalf("ACK=%+v window=%+v", a, w)
	}
	retireAF203Pending(pending, 100, a)
	if len(pending) != 0 {
		t.Fatalf("sequential ACK did not retire sender entries: %v", pending)
	}
}

func TestTransportACKBaseCommitsOnlyAfterSuccessfulReport(t *testing.T) {
	c := newTestClient(101, "hd") // bootstrap/report position is 100
	c.markACKReceived(101)

	first, firstWindow := c.nextTransportACK(7, 9)
	second, _ := c.nextTransportACK(8, 10)
	if first.AVBase != 100 || first.AVHigh != 101 || second.AVBase != 100 {
		t.Fatalf("unsent ACK advanced base: first=%+v second=%+v", first, second)
	}

	c.markTransportACKSent(firstWindow)
	third, _ := c.nextTransportACK(9, 11)
	if third.AVBase != 101 || third.AVHigh != 101 {
		t.Fatalf("successful ACK did not commit contiguous base: %+v", third)
	}
}

func TestTransportACKRecoveredHoleIsPositivelyAcknowledged(t *testing.T) {
	c := newTestClient(101, "hd") // bootstrap/report position is 100
	pending := map[uint64]bool{101: true, 102: true, 103: true}
	c.markACKReceived(101)
	c.markACKReceived(103)

	first, _ := ackFor(t, c)
	if first.AVBase != 100 || first.AVHigh != 103 || !reflect.DeepEqual(first.Missing, []uint16{2}) {
		t.Fatalf("first ACK=%+v", first)
	}
	retireAF203Pending(pending, 100, first)
	if !reflect.DeepEqual(pending, map[uint64]bool{102: true}) {
		t.Fatalf("first ACK retired wrong sender entries: %v", pending)
	}

	// Re-reporting before recovery must not move base beyond the hole.
	repeated, _ := ackFor(t, c)
	if repeated.AVBase != 101 || repeated.AVHigh != 103 || !reflect.DeepEqual(repeated.Missing, []uint16{1}) {
		t.Fatalf("repeated ACK skipped unresolved hole: %+v", repeated)
	}
	retireAF203Pending(pending, 101, repeated)
	if !pending[102] {
		t.Fatalf("repeated ACK falsely retired unresolved sequence 102")
	}

	c.markACKReceived(102)
	recovered, _ := ackFor(t, c)
	if recovered.AVBase != 101 || recovered.AVHigh != 103 || len(recovered.Missing) != 0 {
		t.Fatalf("recovery ACK=%+v", recovered)
	}
	retireAF203Pending(pending, 101, recovered)
	if len(pending) != 0 {
		t.Fatalf("recovery ACK did not positively retire sequence 102: %v", pending)
	}
}

func TestTransportACKMissingAndRecovery(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	c.markACKReceived(0x4000)
	c.markACKReceived(0x4002)
	c.markACKReceived(0x4004)
	a, _ := ackFor(t, c)
	if a.AVBase != 0x3fff || a.AVHigh != 0x4004 || !reflect.DeepEqual(a.Missing, []uint16{2, 4}) {
		t.Fatalf("ACK with holes=%+v", a)
	}

	c.markACKReceived(0x4001)
	a, _ = ackFor(t, c)
	if a.AVBase != 0x4000 || !reflect.DeepEqual(a.Missing, []uint16{3}) {
		t.Fatalf("ACK after first recovery=%+v", a)
	}
	c.markACKReceived(0x4003)
	a, _ = ackFor(t, c)
	if a.AVBase != 0x4002 || a.AVHigh != 0x4004 || len(a.Missing) != 0 {
		t.Fatalf("ACK after full recovery=%+v", a)
	}
}

func TestTransportACKWrap(t *testing.T) {
	c := newTestClient(0xffff, "hd") // bootstrap/report position is 0xfffe
	pending := map[uint64]bool{0xffff: true, 0x10000: true}
	c.markACKReceived(0x10000)
	a, _ := ackFor(t, c)
	if a.AVBase != 0xfffe || a.AVHigh != 0 || !reflect.DeepEqual(a.Missing, []uint16{1}) {
		t.Fatalf("wrapped ACK=%+v", a)
	}
	retireAF203Pending(pending, 0xfffe, a)
	if !reflect.DeepEqual(pending, map[uint64]bool{0xffff: true}) {
		t.Fatalf("wrapped ACK retired wrong sender entries: %v", pending)
	}
	c.markACKReceived(0xffff)
	a, _ = ackFor(t, c)
	if a.AVBase != 0xfffe || a.AVHigh != 0 || len(a.Missing) != 0 {
		t.Fatalf("recovered wrapped ACK=%+v", a)
	}
	retireAF203Pending(pending, 0xfffe, a)
	if len(pending) != 0 {
		t.Fatalf("wrapped recovery did not retire missing sender entry: %v", pending)
	}
	a, _ = ackFor(t, c)
	if a.AVBase != 0 || a.AVHigh != 0 || len(a.Missing) != 0 {
		t.Fatalf("post-wrap committed ACK=%+v", a)
	}
}

func TestTransportACKCapacityDoesNotAcknowledgeUnlistedHole(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	c.markACKReceived(0x4000)
	for i := uint64(1); i <= 2*maxTransportNACKs+3; i += 2 {
		c.markACKReceived(0x4000 + i)
	}
	a, _ := ackFor(t, c)
	if len(a.Missing) != maxTransportNACKs {
		t.Fatalf("NACK count=%d, want %d", len(a.Missing), maxTransportNACKs)
	}
	if a.AVBase != 0x3fff || a.AVHigh != uint16(0x3fff+2*maxTransportNACKs+2) {
		t.Fatalf("high=0x%04x crossed first unlisted hole", a.AVHigh)
	}
	if got := a.Missing[len(a.Missing)-1]; got != 2*maxTransportNACKs+1 {
		t.Fatalf("last NACK=%d", got)
	}
}

func TestReliableACKAndSendCount(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	c.acceptReliableControl(0x1234)
	a1, _ := ackFor(t, c)
	a2, _ := ackFor(t, c)
	if a1.ReliableRecvACK != 0x1234 || a2.ReliableRecvACK != 0x1234 {
		t.Fatalf("reliable ACKs=%04x/%04x", a1.ReliableRecvACK, a2.ReliableRecvACK)
	}
	if a2.SendCount != a1.SendCount+1 {
		t.Fatalf("send counts=%04x/%04x", a1.SendCount, a2.SendCount)
	}
	c.acceptReliableControl(0x1200)
	a3, _ := ackFor(t, c)
	if a3.ReliableRecvACK != 0x1234 {
		t.Fatalf("reliable ACK regressed to %04x", a3.ReliableRecvACK)
	}
}

func TestReliableACKWaitsForContiguousAcceptedMessages(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	c.acceptReliableControl(0x1234)
	c.acceptReliableControl(0x1236)
	if got := c.reliableRecvACK; got != 0x1234 {
		t.Fatalf("reliable ACK skipped a missing message: got 0x%04x", got)
	}
	c.acceptReliableControl(0x1235)
	if got := c.reliableRecvACK; got != 0x1236 {
		t.Fatalf("reliable ACK did not drain recovered messages: got 0x%04x", got)
	}
}

func TestReliableACKWrap(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	c.acceptReliableControl(0xfffe)
	c.acceptReliableControl(0x0000)
	if got := c.reliableRecvACK; got != 0xfffe {
		t.Fatalf("wrapped reliable ACK skipped 0xffff: got 0x%04x", got)
	}
	c.acceptReliableControl(0xffff)
	if got := c.reliableRecvACK; got != 0x0000 {
		t.Fatalf("wrapped reliable ACK did not drain through zero: got 0x%04x", got)
	}
}

func TestTimingProbeResponse(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	probe := make([]byte, 12)
	probe[0], probe[1] = 0x0a, 0x08
	binary.LittleEndian.PutUint32(probe[8:], 0xaabbccdd)
	response := c.handleTransportControl(probe)
	if len(response) != 20 || response[0] != 0x0b {
		t.Fatalf("response=% x", response)
	}
	if got := binary.LittleEndian.Uint16(response[8:10]); got != 0xccdd {
		t.Fatalf("timing echo=0x%04x", got)
	}
	if got := binary.LittleEndian.Uint16(response[10:12]); got != 0 {
		t.Fatalf("unproven response metric at +10 is nonzero: 0x%04x", got)
	}
	if got := c.stats.timingProbesReceived.Load(); got != 1 {
		t.Fatalf("timing probes=%d", got)
	}
}

func TestParseShortTransportPacket(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	pkt := make([]byte, 0x1c+4)
	pkt[3] = flagsRecv
	binary.LittleEndian.PutUint16(pkt[4:], 0x0c+4)
	binary.LittleEndian.PutUint16(pkt[8:], msgSessionD2C)
	pkt[0x1c] = 0x09
	c.parseDatagram(pkt)
	if got := c.stats.transportACKReceived.Load(); got != 1 {
		t.Fatalf("transport ACK packets=%d", got)
	}
}

func TestParseSessionPacketWithoutInnerBody(t *testing.T) {
	c := newTestClient(0x4000, "hd")
	pkt := make([]byte, 0x1c+1)
	pkt[3] = flagsRecv
	binary.LittleEndian.PutUint16(pkt[4:], 0x0c)
	binary.LittleEndian.PutUint16(pkt[8:], msgSessionD2C)
	c.parseDatagram(pkt) // must reject safely instead of indexing an empty inner body
}

func TestParseTimingProbeSendsFeedback(t *testing.T) {
	receiver, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer receiver.Close()
	sender, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer sender.Close()

	c := newTestClient(0x4000, "hd")
	c.conn = sender
	c.cam = receiver.LocalAddr().(*net.UDPAddr)
	c.nonce = make([]byte, 8)
	probe := make([]byte, 12)
	probe[0], probe[1] = 0x0a, 0x08
	binary.LittleEndian.PutUint16(probe[8:], 0xcdef)
	pkt := make([]byte, 0x1c+len(probe))
	pkt[3] = flagsRecv
	binary.LittleEndian.PutUint16(pkt[4:], uint16(0x0c+len(probe)))
	binary.LittleEndian.PutUint16(pkt[8:], msgSessionD2C)
	copy(pkt[0x1c:], probe)
	c.parseDatagram(pkt)

	if err := receiver.SetReadDeadline(time.Now().Add(time.Second)); err != nil {
		t.Fatal(err)
	}
	wire := make([]byte, 256)
	n, _, err := receiver.ReadFromUDP(wire)
	if err != nil {
		t.Fatal(err)
	}
	plain := tutk.ReverseTransCodePartial(nil, wire[:n])
	if len(plain) < 0x1c+20 {
		t.Fatalf("feedback datagram is only %d bytes", len(plain))
	}
	feedback := plain[0x1c:]
	if feedback[0] != 0x0b || binary.LittleEndian.Uint16(feedback[8:10]) != 0xcdef {
		t.Fatalf("feedback=% x", feedback)
	}
	if got := c.stats.timingResponsesSent.Load(); got != 1 {
		t.Fatalf("timing responses=%d", got)
	}
}

func TestReliableControlClassification(t *testing.T) {
	inner := make([]byte, 36)
	inner[0], inner[16], inner[17] = 0x0c, 0, 0x20
	binary.LittleEndian.PutUint16(inner[18:], 0x2345)
	if seq, ok := reliableControlSequence(inner); !ok || seq != 0x2345 {
		t.Fatalf("reliable classification=%04x,%t", seq, ok)
	}
	inner[16] = innerChAudio
	if _, ok := reliableControlSequence(inner); ok {
		t.Fatal("audio packet classified as reliable control")
	}

	ioctrl := make([]byte, 40)
	ioctrl[0], ioctrl[16], ioctrl[17], ioctrl[20] = 0x0c, 0, 0x70, 1
	binary.LittleEndian.PutUint16(ioctrl[18:], 0x4567)
	binary.LittleEndian.PutUint32(ioctrl[24:], 4)
	if seq, ok := reliableControlSequence(ioctrl); !ok || seq != 0x4567 {
		t.Fatalf("IOCtrl classification=%04x,%t", seq, ok)
	}
	ioctrl[16], ioctrl[17] = innerChInter, 0
	if _, ok := reliableControlSequence(ioctrl); ok {
		t.Fatal("inter-frame packet classified as reliable control")
	}
}

func TestBootstrapACKFieldPlacement(t *testing.T) {
	c := newTestClient(0x3fff, "hd")
	c.acceptReliableControl(0x2345)

	first := c.nextTransportACKForRange(7, 0x3fff, 0x4010, nil, 9).marshal()
	second := c.nextTransportACKForRange(8, 0x4010, 0x4010, nil, 10).marshal()
	if got := binary.LittleEndian.Uint16(first[12:14]); got != 0x2345 {
		t.Fatalf("reliable receive ACK at +12=0x%04x", got)
	}
	if got := binary.LittleEndian.Uint16(first[14:16]); got != 0 {
		t.Fatalf("NACK count at +14=%d", got)
	}
	if got := binary.LittleEndian.Uint16(first[18:20]); got != 0 {
		t.Fatalf("first send counter at +18=%d", got)
	}
	if got := binary.LittleEndian.Uint16(second[18:20]); got != 1 {
		t.Fatalf("second send counter at +18=%d", got)
	}
}
