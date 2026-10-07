package petlibro

import (
	"encoding/binary"
	"errors"
	"io"
	"os"
	"testing"
	"time"
)

// TestDumpAckSummary prints the maintenance/bootstrap ACK bodies from a
// timestamped plaintext C2D dump without requiring a live camera.
// Run from the repository root:
//
//	cd /path/to/go2rtc
//	PETLIBRO_C2D_DUMP=/tmp/petlibro_c2d.dat go test ./pkg/petlibro -run TestDumpAckSummary -v -count=1
func TestDumpAckSummary(t *testing.T) {
	path := os.Getenv("PETLIBRO_C2D_DUMP")
	if path == "" {
		t.Skip("PETLIBRO_C2D_DUMP is unset")
	}

	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open C2D dump: %v", err)
	}
	defer f.Close()

	var firstACK uint64
	var records, acks uint64
	for {
		var header [12]byte
		_, err = io.ReadFull(f, header[:])
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatalf("read record %d header: %v", records+1, err)
		}

		unixNano := binary.LittleEndian.Uint64(header[:8])
		length := binary.LittleEndian.Uint32(header[8:])
		if length == 0 || length > 1<<20 {
			t.Fatalf("record %d has invalid inner-body length %d", records+1, length)
		}
		body := make([]byte, int(length))
		if _, err = io.ReadFull(f, body); err != nil {
			t.Fatalf("read record %d body: %v", records+1, err)
		}
		records++

		if len(body) < 24 || body[0] != 0x09 || body[2] != 0x0c {
			continue
		}
		if firstACK == 0 {
			firstACK = unixNano
		}
		delta := time.Duration(int64(unixNano) - int64(firstACK))
		nackCount := binary.LittleEndian.Uint16(body[14:])
		missing := make([]uint16, 0, nackCount)
		for offset := 22; len(missing) < int(nackCount) && offset+2 <= len(body); offset += 2 {
			missing = append(missing, binary.LittleEndian.Uint16(body[offset:]))
		}
		t.Logf("ack delta=%s ordinal=%d avBase=0x%04x avHigh=0x%04x reliable=0x%04x nacks=%v state=%d sendCount=%d tick=%d raw=%x",
			delta,
			binary.LittleEndian.Uint16(body[4:]),
			binary.LittleEndian.Uint16(body[8:]),
			binary.LittleEndian.Uint16(body[10:]),
			binary.LittleEndian.Uint16(body[12:]),
			missing,
			binary.LittleEndian.Uint16(body[16:]),
			binary.LittleEndian.Uint16(body[18:]),
			binary.LittleEndian.Uint16(body[20:]),
			body)
		acks++
	}
	t.Logf("decoded %d ACK bodies from %d C2D records", acks, records)
}
