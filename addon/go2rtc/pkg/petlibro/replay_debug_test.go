package petlibro

import (
	"encoding/binary"
	"errors"
	"io"
	"os"
	"testing"

	"github.com/rs/zerolog"
)

// TestReplayPlainDump replays post-decryption datagrams through the real,
// unexported parser and assembler without opening a camera connection.
//
// Example:
//
//	PETLIBRO_REPLAY_DUMP=/tmp/petlibro_plain.dat \
//	PETLIBRO_REPLAY_QUALITY=hd PETLIBRO_REPLAY_STRICT=1 \
//	go test ./pkg/petlibro -run TestReplayPlainDump -v -count=1
func TestReplayPlainDump(t *testing.T) {
	path := os.Getenv("PETLIBRO_REPLAY_DUMP")
	if path == "" {
		path = os.Getenv("PETLIBRO_D2C_DUMP")
	}
	if path == "" {
		t.Skip("PETLIBRO_REPLAY_DUMP and PETLIBRO_D2C_DUMP are unset")
	}

	quality := os.Getenv("PETLIBRO_REPLAY_QUALITY")
	if quality == "" {
		quality = "hd"
	}

	f, err := os.Open(path)
	if err != nil {
		t.Fatalf("open replay dump: %v", err)
	}
	defer f.Close()

	previousLog := log
	log = zerolog.New(os.Stderr).Level(zerolog.DebugLevel)
	defer func() { log = previousLog }()

	c := &Client{
		quality:  quality,
		strict:   os.Getenv("PETLIBRO_REPLAY_STRICT") == "1",
		verbose:  true,
		frames:   make(chan *Packet, 1024),
		done:     make(chan struct{}),
		avBuffer: make(map[uint64]*pendingFrag),
		wrap:     wrapSeq{ext: 0x4000},
	}
	c.avNextExt = c.wrap.ext
	c.avHighExt = c.wrap.ext - 1
	c.initACKTracking(c.wrap.ext - 1)

	drainFrames := func() {
		for {
			select {
			case <-c.frames:
			default:
				return
			}
		}
	}

	const forceDrainEvery = 64
	var records uint64
	for {
		var header [4]byte
		_, err = io.ReadFull(f, header[:])
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatalf("read record %d length: %v", records+1, err)
		}

		length := binary.LittleEndian.Uint32(header[:])
		if length == 0 || length > 65535 {
			t.Fatalf("record %d has invalid datagram length %d", records+1, length)
		}
		pkt := make([]byte, int(length))
		if _, err = io.ReadFull(f, pkt); err != nil {
			t.Fatalf("read record %d payload: %v", records+1, err)
		}

		c.stats.bytesIn.Add(uint64(len(pkt)))
		c.stats.pktsIn.Add(1)
		c.parseDatagram(pkt)
		records++
		if records%forceDrainEvery == 0 {
			c.forceDrain()
		}
		drainFrames()
	}

	c.forceDrain()
	drainFrames()
	c.dumpStats()
	s := c.stats.snapshot()
	t.Logf("extended-media summary candidates=%d parsed=%d rejected=%d data=%d end=%d rare=%d unknown0c08=%d unknown0c0d=%d sequenceAssembled=%d sequenceUnhandled=%d",
		s.extendedMediaCandidates, s.extendedMediaParsed, s.extendedMediaRejected,
		s.extendedMediaDataPackets, s.extendedMediaEndPackets, s.extendedMediaRarePackets,
		s.unknown0c08Remaining, s.unknown0c0dRemaining,
		s.sequenceSeenAndAssembled, s.sequenceSeenButUnhandled)
	t.Logf("loss/ACK summary fragIdxGap=%d expectedDataShortfall=%d framesWithLoss=%d missingFragmentsTotal=%d watermark=0x%x high=0x%x pending=%d",
		s.fragIdxGap, s.expectedDataShortfall, s.framesWithLoss, s.missingFragmentsTotal,
		s.ackWatermark, s.ackHigh, s.ackSeenPending)
	t.Logf("replayed %d decrypted datagrams quality=%s strict=%t counters=%+v",
		records, c.quality, c.strict, c.stats.snapshot())
}
