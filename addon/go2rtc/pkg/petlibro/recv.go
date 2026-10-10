package petlibro

import (
	"encoding/binary"
	"errors"
	"io"
	"os"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// petlibro divergence vs pkg/tutk: tutk's worker (pkg/tutk/conn.go:164)
// is a single goroutine that owns the socket: Conn.Read decrypts inline
// and dispatches via handleMsg without any intermediate channel.  Petlibro
// decouples the read syscall from decryption with a reader goroutine
// feeding a 4096-deep channel, so a slow decrypt or GC pause can't
// stall the read syscall and cause kernel UDP drops on high-bitrate
// HD streams.  The cost is one channel hop per packet; the benefit is
// the readerDrops counter making any backlog visible.

// handleEncryptedDatagram decrypts a raw wire packet and dispatches it.
// Tests prefer parseDatagram (plaintext input) directly.
func (c *Client) handleEncryptedDatagram(raw []byte) {
	// petlibro divergence vs pkg/tutk: tutk uses
	// ReverseTransCodePartial in the receive direction (pkg/tutk/conn.go:84)
	// — same primitive, same direction; the only thing different here is
	// that the call lives in a processor goroutine downstream of the
	// read syscall instead of inside the read loop itself.
	pkt := tutk.ReverseTransCodePartial(nil, raw)
	if c.verbose && c.tracePackets {
		log.Trace().Int("wireLen", len(raw)).Hex("plain", pkt).Msg("petlibro D2C packet")
	}
	if c.d2cPlainDump != nil {
		record := make([]byte, 4+len(pkt))
		binary.LittleEndian.PutUint32(record, uint32(len(pkt)))
		copy(record[4:], pkt)

		c.d2cDumpMu.Lock()
		n, err := c.d2cPlainDump.Write(record)
		c.d2cDumpMu.Unlock()
		if err == nil && n != len(record) {
			err = io.ErrShortWrite
		}
		if err != nil {
			log.Warn().Err(err).Msg("petlibro: write plaintext datagram dump")
		}
	}
	c.parseDatagram(pkt)
}

// initACKTracking establishes the last sequence position covered by the
// bootstrap AV-ready ACK. It is separate from avNextExt so assembler skips
// can never acknowledge data that was not actually received.
func (c *Client) initACKTracking(watermark uint64) {
	c.ackMu.Lock()
	c.ackPendingWarn = 32
	c.ackReportedExt = watermark
	c.ackWatermarkExt = watermark
	c.ackHighExt = watermark
	c.ackSeenRanges = nil
	c.ackSeenPending = 0
	c.stats.ackWatermark.Store(watermark)
	c.stats.ackSeenPending.Store(0)
	c.stats.ackSeenRanges.Store(0)
	c.stats.ackHigh.Store(watermark)
	c.stats.ackPrevLow16.Store(uint64(uint16(watermark)))
	c.stats.ackCurrentLow16.Store(uint64(uint16(watermark)))
	c.avNextObserved.Store(c.avNextExt)
	c.ackMu.Unlock()
}

// markACKReceived records one accepted AV/media datagram. Out-of-order
// packets remain pending until every sequence position below them has also
// been observed; forceDrain and avNextExt never participate in this state.
// Consecutive positions are compressed into ranges so a single permanent
// hole cannot grow receive-tracking memory once per packet.
func (c *Client) markACKReceived(subExt uint64) {
	c.ackMu.Lock()
	defer c.ackMu.Unlock()

	if subExt > c.ackHighExt {
		c.ackHighExt = subExt
		c.stats.ackHigh.Store(subExt)
	}
	if subExt <= c.ackWatermarkExt {
		c.stats.ackDuplicateOrOld.Add(1)
		return
	}
	inserted, duplicate := c.insertACKSeenLocked(subExt)
	if duplicate {
		c.stats.ackDuplicateOrOld.Add(1)
		return
	}
	if !inserted {
		c.stats.ackTrackingOverflow.Add(1)
		c.stats.ackSeenRanges.Store(uint64(len(c.ackSeenRanges)))
		return
	}
	c.ackSeenPending++

	var advanced uint64
	for len(c.ackSeenRanges) != 0 && c.ackSeenRanges[0].first == c.ackWatermarkExt+1 {
		r := c.ackSeenRanges[0]
		advanced += r.last - c.ackWatermarkExt
		c.ackWatermarkExt = r.last
		c.ackSeenRanges = c.ackSeenRanges[1:]
	}
	if advanced != 0 {
		c.ackSeenPending -= advanced
		c.stats.ackAdvanced.Add(advanced)
		if c.verbose && !c.traceACK && !c.ackGapStarted.IsZero() && time.Since(c.ackGapStarted) >= time.Second {
			log.Debug().Uint64("advanced", advanced).Uint64("watermark", c.ackWatermarkExt).
				Uint64("pending", c.ackSeenPending).Int("ranges", len(c.ackSeenRanges)).
				Dur("stalledFor", time.Since(c.ackGapStarted)).
				Msg("petlibro ACK gap advanced")
			c.ackGapStarted = time.Now()
		}
	}
	if c.ackSeenPending == 0 {
		c.ackGapStarted = time.Time{}
		c.ackPendingWarn = 32
	} else {
		if c.ackGapStarted.IsZero() {
			c.ackGapStarted = time.Now()
		}
		if c.verbose && !c.traceACK && c.ackSeenPending >= c.ackPendingWarn {
			log.Warn().Uint64("watermark", c.ackWatermarkExt).Uint64("high", c.ackHighExt).
				Uint64("pending", c.ackSeenPending).Int("ranges", len(c.ackSeenRanges)).
				Msg("petlibro ACK receive gap stalled")
			c.ackPendingWarn *= 2
		}
	}
	c.stats.ackWatermark.Store(c.ackWatermarkExt)
	c.stats.ackSeenPending.Store(c.ackSeenPending)
	c.stats.ackSeenRanges.Store(uint64(len(c.ackSeenRanges)))
}

// insertACKSeenLocked inserts subExt into the sorted, non-overlapping receive
// ranges. It returns inserted=false when a new disjoint range would exceed the
// fixed cap. The caller must hold ackMu.
func (c *Client) insertACKSeenLocked(subExt uint64) (inserted, duplicate bool) {
	for i := range c.ackSeenRanges {
		r := &c.ackSeenRanges[i]
		if subExt >= r.first && subExt <= r.last {
			return false, true
		}
		if subExt+1 == r.first {
			r.first = subExt
			return true, false
		}
		if subExt == r.last+1 {
			r.last = subExt
			if i+1 < len(c.ackSeenRanges) && r.last+1 == c.ackSeenRanges[i+1].first {
				r.last = c.ackSeenRanges[i+1].last
				c.ackSeenRanges = append(c.ackSeenRanges[:i+1], c.ackSeenRanges[i+2:]...)
			}
			return true, false
		}
		if subExt < r.first {
			if len(c.ackSeenRanges) >= maxACKSeenRanges {
				return false, false
			}
			c.ackSeenRanges = append(c.ackSeenRanges, ackSeenRange{})
			copy(c.ackSeenRanges[i+1:], c.ackSeenRanges[i:])
			c.ackSeenRanges[i] = ackSeenRange{first: subExt, last: subExt}
			return true, false
		}
	}
	if len(c.ackSeenRanges) >= maxACKSeenRanges {
		return false, false
	}
	c.ackSeenRanges = append(c.ackSeenRanges, ackSeenRange{first: subExt, last: subExt})
	return true, false
}

func (c *Client) contiguousAckExt() uint64 {
	c.ackMu.Lock()
	watermark := c.ackWatermarkExt
	c.ackMu.Unlock()
	return watermark
}

type ackWindow struct {
	baseExt       uint64
	highExt       uint64
	contiguousExt uint64
	missing       []uint16
	seenPending   uint64
}

// ackWindowLocked reports a complete description of (base, high]. Base is the
// contiguous position advanced by the previous successful report, not the
// current receive watermark. AF203 explicitly retires non-NACKed entries in
// this interval, and its native builder likewise uses a prior receive position
// at +8. Keeping the two positions separate ensures newly contiguous packets,
// including a recovered hole, are positively acknowledged once.
//
// If the bounded NACK array fills, high stops immediately before the first
// unrepresentable hole; advancing farther would tell AF203 to discard that
// packet from its resend queue.
func (c *Client) ackWindowLocked() ackWindow {
	w := ackWindow{
		baseExt: c.ackReportedExt, highExt: c.ackHighExt,
		contiguousExt: c.ackWatermarkExt, seenPending: c.ackSeenPending,
	}
	if w.highExt-w.baseExt > 0xffff {
		w.highExt = w.baseExt + 0xffff
	}
	rangeIndex := 0
	for seq := w.baseExt + 1; seq <= w.highExt; seq++ {
		// markACKReceived removes ranges as they join the contiguous prefix,
		// so positions through the current watermark are implicitly seen.
		if seq <= c.ackWatermarkExt {
			continue
		}
		for rangeIndex < len(c.ackSeenRanges) && c.ackSeenRanges[rangeIndex].last < seq {
			rangeIndex++
		}
		seen := rangeIndex < len(c.ackSeenRanges) &&
			c.ackSeenRanges[rangeIndex].first <= seq && seq <= c.ackSeenRanges[rangeIndex].last
		if seen {
			continue
		}
		if len(w.missing) == maxTransportNACKs {
			w.highExt = seq - 1
			break
		}
		w.missing = append(w.missing, uint16(seq-w.baseExt))
	}
	return w
}

// markTransportACKSent commits only the contiguous position represented by a
// successfully written report. It never advances beyond the packet's high
// endpoint (which may have been truncated by the wire-span or NACK limits),
// and a receive hole cannot be skipped because ackWatermarkExt cannot cross it.
func (c *Client) markTransportACKSent(w ackWindow) {
	c.ackMu.Lock()
	reported := w.contiguousExt
	if reported > w.highExt {
		reported = w.highExt
	}
	if reported > c.ackReportedExt {
		c.ackReportedExt = reported
	}
	c.ackMu.Unlock()
}

func (c *Client) nextTransportACK(ordinal, tick uint16) (transportACK, ackWindow) {
	c.ackMu.Lock()
	defer c.ackMu.Unlock()
	w := c.ackWindowLocked()
	a := c.transportACKLocked(ordinal, uint16(w.baseExt), uint16(w.highExt), w.missing, tick)
	return a, w
}

func (c *Client) nextTransportACKForRange(ordinal, base, high uint16, missing []uint16, tick uint16) transportACK {
	c.ackMu.Lock()
	defer c.ackMu.Unlock()
	return c.transportACKLocked(ordinal, base, high, missing, tick)
}

func (c *Client) transportACKLocked(ordinal, base, high uint16, missing []uint16, tick uint16) transportACK {
	a := transportACK{
		Ordinal: ordinal, AVBase: base, AVHigh: high,
		ReliableRecvACK: c.reliableRecvACK, Missing: missing,
		SendCount: c.ackSendCount, Tick: tick,
	}
	c.ackSendCount++
	return a
}

func (c *Client) acceptReliableControl(seq uint16) {
	c.ackMu.Lock()
	defer c.ackMu.Unlock()

	if !c.reliableRecvSet {
		c.reliableRecvACK = seq
		c.reliableRecvSet = true
		c.stats.reliableRecvACK.Store(uint64(seq))
		return
	}

	delta := uint16(seq - c.reliableRecvACK)
	if delta == 0 || delta >= 0x8000 {
		return // duplicate, old, or outside the unambiguous forward half-space
	}
	if delta != 1 {
		if c.reliableSeen == nil {
			c.reliableSeen = make(map[uint16]struct{})
		}
		c.reliableSeen[seq] = struct{}{}
		return
	}

	c.reliableRecvACK = seq
	for {
		next := c.reliableRecvACK + 1
		if _, ok := c.reliableSeen[next]; !ok {
			break
		}
		delete(c.reliableSeen, next)
		c.reliableRecvACK = next
	}
	c.stats.reliableRecvACK.Store(uint64(c.reliableRecvACK))
}

// recvLoop runs two goroutines: a tight reader that does nothing but
// drain the UDP socket into a channel, and a processor that decrypts
// and dispatches.  Decoupling means a slow decrypt or GC pause can't
// stall the read syscall and cause kernel UDP drops.
func (c *Client) recvLoop() {
	defer c.Close()

	rawChan := make(chan []byte, 4096) // ~4 MiB at peak packet sizes
	go c.readerGoroutine(rawChan)

	lastForce := time.Now()
	lastStats := time.Now()
	tick := time.NewTicker(20 * time.Millisecond)
	defer tick.Stop()

	for {
		select {
		case <-c.done:
			return
		case raw, ok := <-rawChan:
			if !ok {
				return
			}
			c.stats.bytesIn.Add(uint64(len(raw)))
			c.stats.pktsIn.Add(1)
			c.handleEncryptedDatagram(raw)
		case <-tick.C:
			// fall through to periodic work below
		}

		// Shorter forceDrain interval — the camera (over LAN) has
		// usually retransmitted lost packets within ~50-100 ms or
		// they're not coming at all.  Holding longer just adds
		// rendering latency without recovering more data.
		if time.Since(lastForce) > 100*time.Millisecond {
			c.forceDrain()
			lastForce = time.Now()
		}
		if c.verbose && time.Since(lastStats) > 5*time.Second {
			c.dumpStats()
			lastStats = time.Now()
		}
	}
}

// readerGoroutine does nothing but pull bytes off the wire as fast as
// the kernel will deliver them and hand them to the processor.  Each
// iteration allocates a fresh buffer because the channel may queue
// many at once.
func (c *Client) readerGoroutine(out chan<- []byte) {
	defer close(out)
	for {
		select {
		case <-c.done:
			return
		default:
		}
		_ = c.conn.SetReadDeadline(time.Now().Add(1 * time.Second))
		buf := make([]byte, 65535)
		n, _, err := c.conn.ReadFromUDP(buf)
		if err != nil {
			if errors.Is(err, os.ErrDeadlineExceeded) {
				c.stats.recvTimeouts.Add(1)
				continue
			}
			return
		}
		if n == 0 {
			continue
		}
		select {
		case <-c.done:
			return
		case out <- buf[:n]:
		default:
			// Processor is behind by 4096 packets. Drop the new
			// one rather than block the reader and let the kernel
			// drop instead — the inner cmd counter / frag_idx gap
			// detection will mark the resulting AU as incomplete.
			c.stats.readerDrops.Add(1)
		}
	}
}

// dumpStats prints a one-line stream-health summary every ~5 s when
// the client is in verbose mode.  Numbers cover the most recent
// interval; the cumulative counters are also visible.
func (c *Client) dumpStats() {
	cur := c.stats.snapshot()
	delta := cur
	if c.havePrevStats {
		delta = countersSnapshot{
			bytesIn:      cur.bytesIn - c.prevStats.bytesIn,
			pktsIn:       cur.pktsIn - c.prevStats.pktsIn,
			keyFrags:     cur.keyFrags - c.prevStats.keyFrags,
			interFrags:   cur.interFrags - c.prevStats.interFrags,
			audioFrags:   cur.audioFrags - c.prevStats.audioFrags,
			otherFrags:   cur.otherFrags - c.prevStats.otherFrags,
			vidFrags:     cur.vidFrags - c.prevStats.vidFrags,
			vidFramesIn:  cur.vidFramesIn - c.prevStats.vidFramesIn,
			vidFramesOut: cur.vidFramesOut - c.prevStats.vidFramesOut,
			vidDropped:   cur.vidDropped - c.prevStats.vidDropped,
			fragSkips:    cur.fragSkips - c.prevStats.fragSkips,
			fragsLost:    cur.fragsLost - c.prevStats.fragsLost,
			forceDrains:  cur.forceDrains - c.prevStats.forceDrains,
			recvTimeouts: cur.recvTimeouts - c.prevStats.recvTimeouts,
			readerDrops:  cur.readerDrops - c.prevStats.readerDrops,
			emitDrops:    cur.emitDrops - c.prevStats.emitDrops,

			fragIdxGap:                 cur.fragIdxGap - c.prevStats.fragIdxGap,
			frameNumJumpKey:            cur.frameNumJumpKey - c.prevStats.frameNumJumpKey,
			frameNumJumpInter:          cur.frameNumJumpInter - c.prevStats.frameNumJumpInter,
			expectedDataShortfall:      cur.expectedDataShortfall - c.prevStats.expectedDataShortfall,
			zeroDataHardDrop:           cur.zeroDataHardDrop - c.prevStats.zeroDataHardDrop,
			strictIDRDrop:              cur.strictIDRDrop - c.prevStats.strictIDRDrop,
			strictPDrop:                cur.strictPDrop - c.prevStats.strictPDrop,
			forceDrainFlush:            cur.forceDrainFlush - c.prevStats.forceDrainFlush,
			forceDrainEntries:          cur.forceDrainEntries - c.prevStats.forceDrainEntries,
			deferredDrop:               cur.deferredDrop - c.prevStats.deferredDrop,
			framesWithLoss:             cur.framesWithLoss - c.prevStats.framesWithLoss,
			idrFramesWithLoss:          cur.idrFramesWithLoss - c.prevStats.idrFramesWithLoss,
			pFramesWithLoss:            cur.pFramesWithLoss - c.prevStats.pFramesWithLoss,
			missingFragmentsTotal:      cur.missingFragmentsTotal - c.prevStats.missingFragmentsTotal,
			maxMissingFragmentsInFrame: cur.maxMissingFragmentsInFrame,
			normalMediaPackets:         cur.normalMediaPackets - c.prevStats.normalMediaPackets,
			extendedMediaCandidates:    cur.extendedMediaCandidates - c.prevStats.extendedMediaCandidates,
			extendedMediaParsed:        cur.extendedMediaParsed - c.prevStats.extendedMediaParsed,
			extendedMediaRejected:      cur.extendedMediaRejected - c.prevStats.extendedMediaRejected,
			extendedMediaDataPackets:   cur.extendedMediaDataPackets - c.prevStats.extendedMediaDataPackets,
			extendedMediaEndPackets:    cur.extendedMediaEndPackets - c.prevStats.extendedMediaEndPackets,
			extendedMediaRarePackets:   cur.extendedMediaRarePackets - c.prevStats.extendedMediaRarePackets,
			unknown0c08Remaining:       cur.unknown0c08Remaining - c.prevStats.unknown0c08Remaining,
			unknown0c0dRemaining:       cur.unknown0c0dRemaining - c.prevStats.unknown0c0dRemaining,
			sequenceSeenButUnhandled:   cur.sequenceSeenButUnhandled - c.prevStats.sequenceSeenButUnhandled,
			sequenceSeenAndAssembled:   cur.sequenceSeenAndAssembled - c.prevStats.sequenceSeenAndAssembled,
			frameInfoChanges:           cur.frameInfoChanges - c.prevStats.frameInfoChanges,
			frameInfoUnexpected:        cur.frameInfoUnexpected - c.prevStats.frameInfoUnexpected,
			frameInfoCodec:             cur.frameInfoCodec,
			frameInfoFlag:              cur.frameInfoFlag,
			frameInfoOnlineNum:         cur.frameInfoOnlineNum,
			transportACKReceived:       cur.transportACKReceived - c.prevStats.transportACKReceived,
			timingProbesReceived:       cur.timingProbesReceived - c.prevStats.timingProbesReceived,
			timingResponsesSent:        cur.timingResponsesSent - c.prevStats.timingResponsesSent,
			timingFeedbackReceived:     cur.timingFeedbackReceived - c.prevStats.timingFeedbackReceived,
			reliableRecvACK:            cur.reliableRecvACK,
			ackNACKCount:               cur.ackNACKCount,
			ackSendCount:               cur.ackSendCount,

			ackWatermark:        cur.ackWatermark,
			ackSeenPending:      cur.ackSeenPending,
			ackSeenRanges:       cur.ackSeenRanges,
			ackTrackingOverflow: cur.ackTrackingOverflow - c.prevStats.ackTrackingOverflow,
			ackDuplicateOrOld:   cur.ackDuplicateOrOld - c.prevStats.ackDuplicateOrOld,
			ackAdvanced:         cur.ackAdvanced - c.prevStats.ackAdvanced,
			ackHigh:             cur.ackHigh,
			ackSent:             cur.ackSent - c.prevStats.ackSent,
			ackPrevLow16:        cur.ackPrevLow16,
			ackCurrentLow16:     cur.ackCurrentLow16,
		}
	}
	if c.havePrevStats && cur.ackCurrentLow16 == c.prevStats.ackCurrentLow16 && cur.ackHigh > c.prevStats.ackHigh && !c.ackCurrStallWarned {
		log.Warn().Uint64("watermark", cur.ackWatermark).
			Uint64("high", cur.ackHigh).Uint16("current", uint16(cur.ackCurrentLow16)).
			Uint64("pending", cur.ackSeenPending).Msg("petlibro ACK current stalled while high advances")
		c.ackCurrStallWarned = true
	} else if !c.havePrevStats || cur.ackCurrentLow16 != c.prevStats.ackCurrentLow16 {
		c.ackCurrStallWarned = false
	}
	stalled := delta.keyFrags == 0 && delta.interFrags == 0 && delta.audioFrags == 0 && delta.vidFramesIn == 0
	sameStallState := c.stallStatsActive && cur.ackWatermark == c.stallStatsWatermark &&
		cur.ackHigh == c.stallStatsHigh && cur.ackCurrentLow16 == c.stallStatsCurrent &&
		cur.ackSeenPending == c.stallStatsPending
	if stalled && sameStallState {
		c.stallStatsRepeat++
		c.stallControlPkts += delta.otherFrags
		log.Debug().Msgf("stats: stalled repeat=%d in=%d controlOnlyPackets=%d watermark=0x%x high=0x%x pending=%d nacks=%d reliable=0x%04x sent=%d highWire=0x%04x",
			c.stallStatsRepeat, delta.pktsIn, c.stallControlPkts, cur.ackWatermark, cur.ackHigh, cur.ackSeenPending,
			cur.ackNACKCount, uint16(cur.reliableRecvACK), delta.ackSent, uint16(cur.ackCurrentLow16))
		c.prevStats = cur
		return
	}
	c.stallStatsActive = stalled
	c.stallStatsRepeat = 0
	c.stallControlPkts = delta.otherFrags
	c.stallStatsWatermark = cur.ackWatermark
	c.stallStatsHigh = cur.ackHigh
	c.stallStatsCurrent = cur.ackCurrentLow16
	c.stallStatsPending = cur.ackSeenPending
	log.Debug().Msgf("stats: in=%d pkts (%d KiB) families: key=%d inter=%d audio=%d other=%d | video: %d frames in -> %d out (drop %d) | loss: frames=%d idr=%d p=%d missing=%d maxFrame=%d | frag skips: %d (%d frags lost) | forceDrain: %d | qDrops reader=%d emit=%d | reasons: fragIdxGap=%d frameNumJumpKey=%d frameNumJumpInter=%d expectedDataShortfall=%d zeroDataHardDrop=%d strictIDRDrop=%d strictPDrop=%d forceDrainFlush=%d forceDrainEntries=%d deferredDrop=%d | mediaHeaders: normal=%d extendedMedia parsed=%d rejected=%d data=%d end=%d rare=%d unknown0c08=%d unknown0c0d=%d candidates=%d seqAssembled=%d seqUnhandled=%d | frameinfo: codec=0x%04x flag=%d onlineNum=%d changes=%d unexpected=%d | transport: ackRx=%d probeRx=%d responseTx=%d feedbackRx=%d watermark=0x%x high=0x%x avNext=0x%x pending=%d ranges=%d nacks=%d overflow=%d advanced=%d duplicate=%d ackTx=%d base=0x%04x highWire=0x%04x reliable=0x%04x sendCount=0x%04x",
		delta.pktsIn, delta.bytesIn/1024,
		delta.keyFrags, delta.interFrags, delta.audioFrags, delta.otherFrags,
		delta.vidFramesIn, delta.vidFramesOut, delta.vidDropped,
		delta.framesWithLoss, delta.idrFramesWithLoss, delta.pFramesWithLoss, delta.missingFragmentsTotal, delta.maxMissingFragmentsInFrame,
		delta.fragSkips, delta.fragsLost, delta.forceDrains,
		delta.readerDrops, delta.emitDrops,
		delta.fragIdxGap, delta.frameNumJumpKey, delta.frameNumJumpInter,
		delta.expectedDataShortfall, delta.zeroDataHardDrop,
		delta.strictIDRDrop, delta.strictPDrop, delta.forceDrainFlush,
		delta.forceDrainEntries, delta.deferredDrop,
		delta.normalMediaPackets, delta.extendedMediaParsed, delta.extendedMediaRejected,
		delta.extendedMediaDataPackets, delta.extendedMediaEndPackets, delta.extendedMediaRarePackets,
		delta.unknown0c08Remaining, delta.unknown0c0dRemaining, delta.extendedMediaCandidates,
		delta.sequenceSeenAndAssembled, delta.sequenceSeenButUnhandled,
		uint16(delta.frameInfoCodec), delta.frameInfoFlag, delta.frameInfoOnlineNum, delta.frameInfoChanges, delta.frameInfoUnexpected,
		delta.transportACKReceived, delta.timingProbesReceived, delta.timingResponsesSent, delta.timingFeedbackReceived,
		delta.ackWatermark, delta.ackHigh, c.avNextObserved.Load(), delta.ackSeenPending,
		delta.ackSeenRanges, delta.ackNACKCount, delta.ackTrackingOverflow, delta.ackAdvanced, delta.ackDuplicateOrOld, delta.ackSent,
		uint16(delta.ackPrevLow16), uint16(delta.ackCurrentLow16), uint16(delta.reliableRecvACK), uint16(delta.ackSendCount))
	c.prevStats = cur
	c.havePrevStats = true
}

// maintenanceLoop sends the outer session keepalive and type-0x09 receive
// reports. Type 0x0a is a timing probe, not a periodic heartbeat; probes from
// the camera are answered reactively by the inner-message dispatcher.
func (c *Client) maintenanceLoop() {
	tick16 := func() uint16 { return uint16(time.Now().UnixMilli() & 0xFFFF) }

	alive := time.NewTicker(1500 * time.Millisecond)
	ack := time.NewTicker(defaultACKInterval)
	defer alive.Stop()
	defer ack.Stop()

	for {
		select {
		case <-c.done:
			return
		case <-alive.C:
			_ = c.send(buildAliveC2D(c.nonce))
		case <-ack.C:
			c.innerMu.Lock()
			select {
			case <-c.done:
				c.innerMu.Unlock()
				return
			default:
			}
			tick := tick16()
			a, window := c.nextTransportACK(c.icounter, tick)
			body := a.marshal()
			if c.verbose && c.traceACK {
				log.Trace().
					Uint16("ordinal", a.Ordinal).
					Uint16("avBase", a.AVBase).
					Uint16("avHigh", a.AVHigh).
					Uint64("ackReportedBaseExt", window.baseExt).
					Uint64("ackWatermarkExt", window.contiguousExt).
					Uint64("avHighExt", window.highExt).
					Uint64("avNextExt", c.avNextObserved.Load()).
					Uint64("ackSeenPending", window.seenPending).
					Interface("missingOffsets", a.Missing).
					Uint16("reliableRecvACK", a.ReliableRecvACK).
					Uint16("sendCount", a.SendCount).
					Uint16("tick16", tick).
					Msg("petlibro transport ACK send")
			}
			err := c.sendInner(body)
			if err == nil {
				c.markTransportACKSent(window)
			}
			c.icounter++
			c.stats.ackPrevLow16.Store(uint64(a.AVBase))
			c.stats.ackCurrentLow16.Store(uint64(a.AVHigh))
			c.stats.ackNACKCount.Store(uint64(len(a.Missing)))
			c.stats.ackSendCount.Store(uint64(a.SendCount))
			if err == nil {
				c.stats.ackSent.Add(1)
			}
			c.innerMu.Unlock()
		}
	}
}
