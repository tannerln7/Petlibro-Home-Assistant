package petlibro

import (
	"bytes"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/rs/zerolog"
)

func capturePetlibroLogs(t *testing.T, level zerolog.Level) *bytes.Buffer {
	t.Helper()
	buffer := new(bytes.Buffer)
	previous := log
	log = zerolog.New(buffer).Level(level)
	t.Cleanup(func() { log = previous })
	return buffer
}

func TestCameraSPSLogsOnlyMeaningfulConfigurationChangesAtDebug(t *testing.T) {
	buffer := capturePetlibroLogs(t, zerolog.DebugLevel)
	transport := newFakeCameraTransport()
	camera := newCamera(transport, "hd", false, nil)
	low := testAccessUnit(t, "Z2QAFqwa0BQF/yzcBAQFAAADAAEAAAMAHo8UIqA=", true, 100)
	high := testAccessUnit(t, "R00AKZmgHgCJ+WEAAAMD6AAATiCE", true, 200)

	if _, _, ok := camera.observeVideoConfig(low); !ok {
		t.Fatal("low-resolution SPS was not parsed")
	}
	if _, _, ok := camera.observeVideoConfig(low); !ok {
		t.Fatal("repeated SPS was not parsed")
	}
	if _, _, ok := camera.observeVideoConfig(high); !ok {
		t.Fatal("changed SPS was not parsed")
	}

	output := buffer.String()
	if got := strings.Count(output, "observed initial H.264 configuration"); got != 1 {
		t.Fatalf("initial configuration logs=%d, want 1\n%s", got, output)
	}
	if strings.Contains(output, "observed repeated H.264 configuration") {
		t.Fatalf("unchanged SPS appeared at DEBUG:\n%s", output)
	}
	if got := strings.Count(output, "H.264 configuration changed"); got != 1 {
		t.Fatalf("configuration-change logs=%d, want 1\n%s", got, output)
	}
}

func TestPacketTracingLogsMetadataNotPlaintext(t *testing.T) {
	buffer := capturePetlibroLogs(t, zerolog.TraceLevel)
	c := &Client{sessionID: "ps-test", verbose: true, tracePackets: true}
	body := []byte{0x0c, 0x0d, 0xde, 0xad, 0xbe, 0xef}
	c.dumpC2DInner(body)

	output := buffer.String()
	if !strings.Contains(output, "packet metadata") || !strings.Contains(output, "plain_length") {
		t.Fatalf("packet metadata missing:\n%s", output)
	}
	if strings.Contains(output, "deadbeef") || strings.Contains(output, "3q2+7w") {
		t.Fatalf("plaintext packet body leaked into trace:\n%s", output)
	}
}

func TestTransportHealthHasCompactDebugAndDetailedTrace(t *testing.T) {
	newClient := func() *Client {
		c := &Client{
			sessionID: "ps-test", connectedAt: time.Now().Add(-time.Minute), verbose: true,
		}
		c.stats.bytesIn.Store(10 * 1024)
		c.stats.pktsIn.Store(20)
		c.stats.vidFramesOut.Store(125)
		c.stats.missingFragmentsTotal.Store(2)
		return c
	}
	t.Run("debug is compact", func(t *testing.T) {
		buffer := capturePetlibroLogs(t, zerolog.DebugLevel)
		newClient().dumpStats()

		output := buffer.String()
		if !strings.Contains(output, "petlibro transport health") ||
			!strings.Contains(output, "video_frames_per_second") {
			t.Fatalf("compact health summary missing:\n%s", output)
		}
		if strings.Contains(output, "petlibro transport diagnostics") {
			t.Fatalf("TRACE transport diagnostics appeared at DEBUG:\n%s", output)
		}
	})
	t.Run("trace retains detailed counters", func(t *testing.T) {
		buffer := capturePetlibroLogs(t, zerolog.TraceLevel)
		newClient().dumpStats()

		output := buffer.String()
		if !strings.Contains(output, "petlibro transport diagnostics") ||
			!strings.Contains(output, "mediaHeaders") {
			t.Fatalf("TRACE transport diagnostics missing:\n%s", output)
		}
	})
}

func TestClientCloseDistinguishesStopWriteFailure(t *testing.T) {
	buffer := capturePetlibroLogs(t, zerolog.DebugLevel)
	udp, err := net.ListenUDP("udp4", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err = udp.Close(); err != nil {
		t.Fatal(err)
	}
	c := &Client{
		sessionID: "ps-test", adapterID: "ca-test", producerID: 42,
		connectedAt: time.Now(), conn: udp,
		cam:   &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1), Port: 32100},
		nonce: make([]byte, 8), done: make(chan struct{}),
	}
	c.mediaStartSent.Store(true)
	if err = c.Close(); err == nil {
		t.Fatal("Close succeeded despite a closed UDP socket")
	}

	output := buffer.String()
	if !strings.Contains(output, "IPCAM_STOP datagram write failed") ||
		!strings.Contains(output, `"stop_attempted":true`) ||
		!strings.Contains(output, `"stop_datagram_written":false`) {
		t.Fatalf("STOP failure lifecycle is ambiguous:\n%s", output)
	}
}

func TestCorrelationIdentifiersAreOpaqueAndDistinct(t *testing.T) {
	adapterA, adapterB := newAdapterID(), newAdapterID()
	sessionA, sessionB := newSessionID(), newSessionID()
	if adapterA == adapterB || sessionA == sessionB {
		t.Fatal("correlation identifiers were reused")
	}
	for _, id := range []string{adapterA, adapterB, sessionA, sessionB} {
		if !strings.HasPrefix(id, "ca-") && !strings.HasPrefix(id, "ps-") {
			t.Fatalf("unexpected correlation identifier %q", id)
		}
		if strings.Contains(id, "PLAF") || strings.Contains(id, ".") {
			t.Fatalf("identifier contains device-like data: %q", id)
		}
	}
}
