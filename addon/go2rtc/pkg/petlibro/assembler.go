package petlibro

import (
	"encoding/binary"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/h264"
)

// petlibro divergence vs pkg/tutk: tutk's session16 reassembler
// (pkg/tutk/session16.go:181 — Session16.SessionRead 0x01 0x03 branch)
// expects the camera to send a single header (chunkSeq==0) carrying the
// total payloadSize, then subsequent chunks concatenated in fixed
// chunkSeq order; gap = drop the whole frame (msgMediaLost).  Petlibro
// firmware instead labels each fragment with totalFrags (inner[20]),
// fragIdx (inner[22..23]), and an end-marker fragment whose tail
// matches the 16-byte stripFragmentMetadataTrailer signature, with two
// independent channels (innerChKey 0x05 for IDR / innerChInter 0x07 for
// P-frames) time-multiplexed in wire order.  The two-channel + trailer-
// based reassembler in this file has no tutk analog.

const forceDrainStallTicks = 5 // ~500 ms with the 100 ms forceDrain cadence

type pendingFrag struct {
	channel byte
	// isAudio: true for either AAC variant — ch=0x03 sub17=0x01 ADTS
	// at offset 36, or ch=0x07 sub17=0x00 b1=0x0d with an 8-byte
	// inner header before ADTS sync.
	isAudio    bool
	subExt     uint64
	frameNum   uint32 // camera's per-channel frame counter (inner[28..31])
	fragIdx    uint16 // index of fragment within frame (inner[22..23])
	totalFrags uint16 // total fragments comprising this frame (inner[20]); widened from byte so >255-fragment IDRs at high bitrates don't silently truncate
	payload    []byte
}

// channelAsm holds the assembly state for a single video channel.  The
// camera interleaves IDR fragments on ch=0x05 with P-frame fragments
// on ch=0x07 in wire order, so they MUST be assembled independently —
// a single shared buffer would constantly drop partial frames whenever
// the channels switch.
//
// framePending is an explicit "an AU is in progress on this channel"
// flag.  Earlier versions overloaded `curFrameNum == 0` as the
// sentinel, which silently treated a legitimate frameNum=0 (camera
// reboot or wrap-around to zero) as "no AU in progress" and merged it
// into whatever the next AU was — a frankenframe.  Splitting the
// concerns avoids that.
type channelAsm struct {
	buf            []byte
	curFrameNum    uint32
	framePending   bool
	curAUTotal     uint16 // inner[20]: total fragments advertised for current frame
	curAUDataCount uint16
	curAUGapped    bool
	expectFragIdx  uint16
	receivedData   map[uint16]struct{}
}

func (a *channelAsm) reset() {
	a.buf = a.buf[:0]
	a.curAUTotal = 0
	a.curAUDataCount = 0
	a.curAUGapped = false
	a.expectFragIdx = 0
	a.receivedData = nil
	a.framePending = false
}

// debugFrameEvent records only consequential assembler decisions. It is
// deliberately gated by verbose so normal streams don't log per-frame noise.
func (c *Client) debugFrameEvent(reason string, e *pendingFrag, asm *channelAsm, onlineNum byte, hasTrailer bool, isKeyframe *bool) {
	if !c.verbose || !c.traceFrag {
		return
	}
	event := log.Trace().
		Str("reason", reason).
		Str("quality", c.quality).
		Bool("strict", c.strict).
		Uint8("channel", e.channel).
		Uint32("frameNum", e.frameNum).
		Uint16("fragIdx", e.fragIdx).
		Uint16("expectedFragIdx", asm.expectFragIdx).
		Uint16("totalFrags", e.totalFrags).
		Uint16("curAUDataCount", asm.curAUDataCount).
		Uint8("onlineNum", onlineNum).
		Bool("hasTrailer", hasTrailer).
		Uint16("subWire", uint16(e.subExt)).
		Uint64("subExt", e.subExt)
	if isKeyframe != nil {
		event = event.Bool("isKeyframe", *isKeyframe)
	}
	event.Msg("petlibro frame decision")
}

func (c *Client) debugFlushPendingIDR(reason string, asm *channelAsm) {
	if !c.verbose || !c.traceFrag {
		return
	}
	fragIdx := uint16(0)
	if asm.expectFragIdx > 0 {
		fragIdx = asm.expectFragIdx - 1
	}
	log.Trace().
		Str("reason", reason).
		Str("quality", c.quality).
		Bool("strict", c.strict).
		Uint8("channel", innerChKey).
		Uint32("frameNum", asm.curFrameNum).
		Uint16("fragIdx", fragIdx).
		Uint16("expectedFragIdx", asm.expectFragIdx).
		Uint16("totalFrags", asm.curAUTotal).
		Uint16("curAUDataCount", asm.curAUDataCount).
		Uint8("onlineNum", 0).
		Bool("hasTrailer", false).
		Bool("isKeyframe", true).
		Msg("petlibro frame decision")
}

// debugAVFrameInfo decodes the 16-byte trailer only after the strict signature
// check accepts it. AF203 identifies byte 4 as its online-client count/state;
// it is not a resolution selector. Normal verbose mode logs only the first
// trailer and changes.
func (c *Client) debugAVFrameInfo(e *pendingFrag, p []byte, hasTrailer bool) {
	if !c.verbose || !hasTrailer || len(p) < 16 {
		return
	}
	t := p[len(p)-16:]
	codecID := binary.LittleEndian.Uint16(t[0:2])
	frameFlag := t[2]
	byte4 := t[4]
	unexpected := frameFlag > 1 ||
		(e.channel == innerChKey && frameFlag != 1) ||
		(e.channel == innerChInter && frameFlag != 0)
	changed := c.frameInfoSeen &&
		(codecID != c.frameInfoCodec || byte4 != c.frameInfoOnlineNum)
	shouldLog := c.traceFrameInfo || !c.frameInfoSeen || changed || unexpected
	if changed {
		c.stats.frameInfoChanges.Add(1)
	}
	if unexpected {
		c.stats.frameInfoUnexpected.Add(1)
	}
	c.frameInfoSeen = true
	c.frameInfoCodec = codecID
	c.frameInfoFlag = frameFlag
	c.frameInfoOnlineNum = byte4
	c.stats.frameInfoCodec.Store(uint64(codecID))
	c.stats.frameInfoFlag.Store(uint64(frameFlag))
	c.stats.frameInfoOnlineNum.Store(uint64(byte4))
	if !shouldLog {
		return
	}
	log.Trace().
		Hex("raw", t).
		Uint16("codecID", codecID).
		Uint8("frameFlag", frameFlag).
		Uint8("camIndex", t[3]).
		Uint8("onlineNum", byte4).
		Uint32("timestamp", binary.LittleEndian.Uint32(t[12:16])).
		Uint32("frameNum", e.frameNum).
		Uint8("channel", e.channel).
		Bool("unexpected", unexpected).
		Uint16("subWire", uint16(e.subExt)).
		Uint64("subExt", e.subExt).
		Msg("petlibro frameinfo")
}

func missingFragmentRanges(asm *channelAsm, hasTrailer bool) ([]uint16, uint16) {
	if asm.curAUTotal == 0 {
		return nil, uint16(len(asm.receivedData))
	}
	expectedData := asm.curAUTotal - 1
	missing := make([]uint16, 0)
	for i := uint16(0); i < expectedData; i++ {
		if _, ok := asm.receivedData[i]; !ok {
			missing = append(missing, i)
		}
	}
	received := uint16(len(asm.receivedData))
	if hasTrailer {
		received++
	} else {
		missing = append(missing, asm.curAUTotal-1)
	}
	return missing, received
}

func compressFragmentRanges(missing []uint16) string {
	if len(missing) == 0 {
		return "none"
	}
	parts := make([]string, 0, len(missing))
	for i := 0; i < len(missing); {
		start, end := missing[i], missing[i]
		for i+1 < len(missing) && missing[i+1] == end+1 {
			i++
			end = missing[i]
		}
		if start == end {
			parts = append(parts, strconv.Itoa(int(start)))
		} else {
			parts = append(parts, strconv.Itoa(int(start))+"-"+strconv.Itoa(int(end)))
		}
		i++
	}
	return strings.Join(parts, ",")
}

func (c *Client) logAVLoss(asm *channelAsm, channel byte, hasTrailer bool, trailer, frameData []byte) {
	missing, received := missingFragmentRanges(asm, hasTrailer)
	if len(missing) == 0 && !asm.curAUGapped {
		return
	}
	missingCount := uint64(len(missing))
	if missingCount == 0 {
		// A backward/duplicate frag index can poison a frame without a
		// uniquely reconstructable missing position.
		missingCount = 1
	}
	c.stats.framesWithLoss.Add(1)
	if channel == innerChKey {
		c.stats.idrFramesWithLoss.Add(1)
	} else {
		c.stats.pFramesWithLoss.Add(1)
	}
	c.stats.missingFragmentsTotal.Add(missingCount)
	for {
		old := c.stats.maxMissingFragmentsInFrame.Load()
		if missingCount <= old || c.stats.maxMissingFragmentsInFrame.CompareAndSwap(old, missingCount) {
			break
		}
	}
	if !c.verbose {
		return
	}
	event := log.Trace().
		Uint32("frameNum", asm.curFrameNum).
		Uint8("channel", channel).
		Str("quality", c.quality).
		Uint16("totalFrags", asm.curAUTotal).
		Uint16("receivedFrags", received).
		Str("missing", compressFragmentRanges(missing)).
		Bool("isKeyframe", channel == innerChKey).
		Bool("hasTrailer", hasTrailer).
		Int("actualBytes", len(frameData))
	if len(trailer) != 0 {
		event = event.Hex("trailerBytes", trailer)
	}
	event.Msg("petlibro avloss")
}

type decodedMedia struct {
	b1            byte
	channel       byte
	subFlag       byte
	subWire       uint16
	totalFrags    uint16
	fragIdx       uint16
	payloadLen    uint16
	frameNum      uint32
	nextFrameLike uint32
	payload       []byte
	extraLen      int
	isAudio       bool
	isEnd         bool
	extended      bool
}

func isExtendedMediaFamily(b1 byte) bool {
	switch b1 {
	case 0x08, 0x09, 0x0c, 0x0d:
		return true
	default:
		return false
	}
}

// decodeExtendedMedia validates the alternate media layout seen in PLAF203
// plaintext captures. It is eight bytes longer than the original layout and
// carries 16-bit total/fragment fields. nextFrameLike is deliberately kept
// unnamed beyond the observed invariant: every retained capture stores
// frameNum+1 there, including uint32 wrap semantics.
func decodeExtendedMedia(inner []byte) (m decodedMedia, reason string, ok bool) {
	if len(inner) >= 2 {
		m.b1 = inner[1]
	}
	m.extended = true
	if !isExtendedMediaFamily(m.b1) {
		return m, "not_extended_family", false
	}
	if len(inner) >= 28 {
		m.subWire = binary.LittleEndian.Uint16(inner[26:28])
	}
	if len(inner) < 44 {
		return m, "short_header", false
	}

	m.channel = inner[24]
	m.subFlag = inner[25]
	m.totalFrags = binary.LittleEndian.Uint16(inner[28:30])
	m.fragIdx = binary.LittleEndian.Uint16(inner[30:32])
	m.payloadLen = binary.LittleEndian.Uint16(inner[32:34])
	m.frameNum = binary.LittleEndian.Uint32(inner[36:40])
	m.nextFrameLike = binary.LittleEndian.Uint32(inner[40:44])

	if m.channel != innerChKey && m.channel != innerChInter {
		return m, "invalid_channel", false
	}
	if m.totalFrags == 0 {
		return m, "zero_total_frags", false
	}
	if m.payloadLen == 0 {
		return m, "zero_payload", false
	}
	payloadEnd := 44 + int(m.payloadLen)
	if payloadEnd > len(inner) {
		return m, "payload_overflow", false
	}
	if m.nextFrameLike != m.frameNum+1 {
		return m, "next_frame_incoherent", false
	}

	m.payload = inner[44:payloadEnd]
	m.extraLen = len(inner) - payloadEnd
	_, _, _, hasTrailer := stripFragmentMetadataTrailer(m.payload)
	switch m.b1 {
	case 0x08, 0x0c:
		if m.subFlag != 0 {
			return m, "data_sub_flag", false
		}
		if m.fragIdx >= m.totalFrags {
			return m, "data_frag_out_of_range", false
		}
		if hasTrailer {
			return m, "data_has_frameinfo", false
		}
	case 0x09, 0x0d:
		m.isEnd = true
		if m.subFlag != 1 {
			return m, "end_sub_flag", false
		}
		// Captured end fragments use the firmware's fixed marker value 16,
		// even when totalFrags is 1 or much larger than 17.
		if m.fragIdx != 16 {
			return m, "end_frag_not_16", false
		}
		if !hasTrailer {
			return m, "end_missing_frameinfo", false
		}
	}
	return m, "accepted", true
}

func decodeNormalMedia(inner []byte) (m decodedMedia, ok bool) {
	if len(inner) < 36 {
		return m, false
	}
	m.b1 = inner[1]
	m.channel = inner[16]
	m.subFlag = inner[17]
	m.subWire = binary.LittleEndian.Uint16(inner[18:20])
	m.totalFrags = uint16(inner[20])
	m.fragIdx = binary.LittleEndian.Uint16(inner[22:24])
	m.payloadLen = binary.LittleEndian.Uint16(inner[24:26])
	m.frameNum = binary.LittleEndian.Uint32(inner[28:32])

	payloadFrom := func(start int) []byte {
		if m.payloadLen != 0 && start+int(m.payloadLen) <= len(inner) {
			return inner[start : start+int(m.payloadLen)]
		}
		return inner[start:]
	}
	switch {
	case (m.channel == innerChKey || m.channel == innerChInter) &&
		(m.b1 == 0x00 || m.b1 == 0x04 || m.b1 == 0x05):
		if m.payloadLen == 0 || 36+int(m.payloadLen) > len(inner) {
			return m, false
		}
		m.payload = payloadFrom(36)
	case (m.channel == innerChKey || m.channel == innerChInter) &&
		m.b1 == 0x01 && m.subFlag == 0x01:
		m.payload = payloadFrom(36)
		m.isEnd = true
	case m.channel == innerChAudio && m.subFlag == 0x01 &&
		len(inner) >= 38 && inner[36] == 0xFF && inner[37] == 0xF1:
		m.isAudio = true
		m.payload = payloadFrom(36)
	case m.channel == innerChInter && m.subFlag == 0x00 && m.b1 == 0x0d &&
		len(inner) >= 46 && inner[44] == 0xFF && inner[45] == 0xF1:
		m.isAudio = true
		full := payloadFrom(36)
		if len(full) > 8 {
			m.payload = full[8:]
		}
	default:
		return m, false
	}
	if m.isAudio && len(m.payload) == 0 {
		return m, false
	}
	return m, true
}

func (c *Client) traceExtendedMedia(m decodedMedia, accepted bool, reason string) {
	if !c.verbose || !c.tracePackets {
		return
	}
	log.Trace().
		Str("type", fmt.Sprintf("0c%02x", m.b1)).
		Bool("accepted", accepted).
		Uint8("channel", m.channel).
		Uint16("subWire", m.subWire).
		Uint16("totalFrags", m.totalFrags).
		Uint16("fragIdx", m.fragIdx).
		Uint16("payloadLen", m.payloadLen).
		Uint32("frameNum", m.frameNum).
		Uint32("nextFrameLike", m.nextFrameLike).
		Int("payloadOffset", 44).
		Int("extraLen", m.extraLen).
		Str("reason", reason).
		Msg("petlibro extendedMedia")
}

func decodeBootstrapMedia(inner []byte) (decodedMedia, bool) {
	if len(inner) == 0 || inner[0] != 0x0c {
		return decodedMedia{}, false
	}
	if isExtendedMediaFamily(inner[1]) {
		if media, _, ok := decodeExtendedMedia(inner); ok {
			return media, true
		}
	}
	return decodeNormalMedia(inner)
}

// reliableControlSequence recognizes the IOCtrl/reliable family by its wire
// structure rather than by sequence magnitude. Media and audio sequence
// values legitimately cross the old 0x4000 heuristic.
func reliableControlSequence(inner []byte) (uint16, bool) {
	if len(inner) < 20 || inner[0] != 0x0c || inner[1]&0x08 != 0 {
		return 0, false
	}

	// AVAPI's reliable family appears in two Petlibro forms. Short transport
	// control packets use a zero high byte and one of the observed family
	// values below. IOCtrl responses retain the request's 0x1000/0x7000
	// channel-family word and carry the normal 36-byte IOCtrl envelope. Media
	// uses channel words 0x0003/0x0005/0x0007 instead, so checking the complete
	// structure avoids the old and incorrect "sequence < 0x4000" heuristic.
	channelFamily := binary.LittleEndian.Uint16(inner[16:18])
	switch channelFamily {
	case 0x0000, 0x2000, 0x2100, 0x7100, 0x7200:
		return binary.LittleEndian.Uint16(inner[18:20]), true
	case 0x1000, 0x7000:
		// These are the IOCtrl channel families used by PLAF203.
	default:
		return 0, false
	}
	if len(inner) < 36 || inner[20] != 1 {
		return 0, false
	}
	payloadLen := binary.LittleEndian.Uint32(inner[24:28])
	if uint64(payloadLen) > uint64(len(inner)-36) {
		return 0, false
	}
	return binary.LittleEndian.Uint16(inner[18:20]), true
}

func timingProbeResponse(inner []byte) []byte {
	if len(inner) < 10 || inner[0] != 0x0a || inner[1]&0x08 == 0 {
		return nil
	}
	// Both AF203 and upstream Session25 echo the probe's low-16 timing
	// stamp at +8. Other response statistics may safely remain zero.
	return innerTimingFeedback(0, 0, uint32(binary.LittleEndian.Uint16(inner[8:10])), 0)
}

func (c *Client) handleTransportControl(inner []byte) []byte {
	switch inner[0] {
	case 0x09:
		c.stats.transportACKReceived.Add(1)
	case 0x0a:
		response := timingProbeResponse(inner)
		if response != nil {
			c.stats.timingProbesReceived.Add(1)
		}
		return response
	case 0x0b:
		c.stats.timingFeedbackReceived.Add(1)
	}
	return nil
}

// parseDatagram handles one already-decrypted packet.  Splitting the
// crypto pass from the parser lets tests feed plaintext fixtures
// straight in without re-encrypting; assembler_test.go relies on it.
func (c *Client) parseDatagram(pkt []byte) {
	if len(pkt) < 0x1C+1 || pkt[3] != flagsRecv {
		return
	}
	if binary.LittleEndian.Uint16(pkt[8:]) != msgSessionD2C {
		return
	}
	outerPayloadLen := int(binary.LittleEndian.Uint16(pkt[4:6]))
	if outerPayloadLen <= 0x0c || outerPayloadLen-0x0c > len(pkt)-0x1c {
		return
	}
	inner := pkt[0x1c : 0x1c+outerPayloadLen-0x0c]
	switch inner[0] {
	case 0x09, 0x0a, 0x0b:
		if response := c.handleTransportControl(inner); response != nil {
			if err := c.sendInner(response); err == nil {
				c.stats.timingResponsesSent.Add(1)
			}
		}
		return
	case 0x0c:
	default:
		return
	}
	if seq, ok := reliableControlSequence(inner); ok {
		c.acceptReliableControl(seq)
		c.debugIOCtrlResponse(inner)
		c.stats.otherFrags.Add(1)
		return
	}
	if len(inner) < 36 {
		return
	}

	var media decodedMedia
	if isExtendedMediaFamily(inner[1]) {
		extended, reason, accepted := decodeExtendedMedia(inner)
		if accepted {
			media = extended
			c.stats.extendedMediaCandidates.Add(1)
			c.stats.extendedMediaParsed.Add(1)
			if media.isEnd {
				c.stats.extendedMediaEndPackets.Add(1)
			} else {
				c.stats.extendedMediaDataPackets.Add(1)
			}
			if media.b1 == 0x09 || media.b1 == 0x0c {
				c.stats.extendedMediaRarePackets.Add(1)
			}
			c.traceExtendedMedia(media, true, reason)
		} else if normal, normalOK := decodeNormalMedia(inner); normalOK {
			// b1=0x0d is also used by the original-layout AAC variant.
			// A valid normal packet wins over a failed extended candidate.
			media = normal
			c.stats.normalMediaPackets.Add(1)
		} else {
			c.stats.extendedMediaCandidates.Add(1)
			c.stats.extendedMediaRejected.Add(1)
			if len(inner) >= 28 {
				c.stats.sequenceSeenButUnhandled.Add(1)
			}
			if extended.b1 == 0x08 {
				c.stats.unknown0c08Remaining.Add(1)
			} else if extended.b1 == 0x0d {
				c.stats.unknown0c0dRemaining.Add(1)
			}
			c.stats.otherFrags.Add(1)
			c.traceExtendedMedia(extended, false, reason)
			return
		}
	} else {
		normal, ok := decodeNormalMedia(inner)
		if !ok {
			return
		}
		media = normal
		c.stats.normalMediaPackets.Add(1)
	}

	switch media.channel {
	case innerChKey:
		c.stats.keyFrags.Add(1)
	case innerChInter:
		c.stats.interFrags.Add(1)
	case innerChAudio:
		c.stats.audioFrags.Add(1)
	default:
		c.stats.otherFrags.Add(1)
	}

	// No filter by subWire — audio fragments legitimately arrive at
	// wire values < 0x4000 alongside the high AV indices, and dropping
	// those would silence the audio stream.
	subExt, ok := c.wrap.extend(media.subWire)
	if !ok {
		// Wire counter landed before the sequence start — most likely
		// wire corruption.  Loud-drop instead of saturating to zero
		// (which would have hashed into avBuffer at a slot
		// drainContiguous can never reach since avNextExt starts at
		// >=0x4000).
		c.stats.otherFrags.Add(1)
		c.stats.sequenceSeenButUnhandled.Add(1)
		return
	}
	if subExt > c.avHighExt {
		c.avHighExt = subExt
		c.wrap.advanceTo(media.subWire)
	}
	// ACK receipt is tracked before assembler late/drop decisions. A
	// retransmitted fragment that arrives after forceDrain advanced
	// avNextExt is still real wire evidence and may close an ACK gap.
	c.markACKReceived(subExt)
	if subExt < c.avNextExt {
		c.stats.deferredDrop.Add(1)
		c.stats.sequenceSeenButUnhandled.Add(1)
		return // late dup
	}
	if c.verbose && c.tracePackets {
		log.Trace().Uint8("innerType", inner[0]).Uint8("b1", media.b1).Uint8("channel", media.channel).
			Bool("extended", media.extended).
			Uint16("subWire", media.subWire).Uint64("subExt", subExt).Uint32("frameNum", media.frameNum).
			Uint16("fragIdx", media.fragIdx).Uint16("totalFrags", media.totalFrags).
			Uint16("paylen", media.payloadLen).Msg("petlibro D2C media")
	}
	c.avBuffer[subExt] = &pendingFrag{
		channel: media.channel, isAudio: media.isAudio,
		subExt: subExt, frameNum: media.frameNum, fragIdx: media.fragIdx,
		totalFrags: media.totalFrags, payload: media.payload,
	}
	c.stats.sequenceSeenAndAssembled.Add(1)
	c.drainContiguous()
}

func (c *Client) drainContiguous() {
	for {
		e, ok := c.avBuffer[c.avNextExt]
		if !ok {
			return
		}
		delete(c.avBuffer, c.avNextExt)
		c.avNextExt++
		c.avNextObserved.Store(c.avNextExt)
		c.emit(e)
	}
}

// forceDrain flushes any avBuffer entries that have fallen too far
// behind the current high-water mark — the missing packet that would
// have filled the contiguous slot is presumed lost.  If avHighExt
// hasn't moved across forceDrainStallTicks consecutive ticks AND the
// buffer is non-empty, we flush EVERYTHING regardless of the
// high-water threshold — recovers from a stalled camera leaving
// partial AUs stranded forever.
func (c *Client) forceDrain() {
	if len(c.avBuffer) == 0 {
		c.stallTicks = 0
		c.lastHighExt = c.avHighExt
		return
	}
	if c.avHighExt == c.lastHighExt {
		c.stallTicks++
	} else {
		c.stallTicks = 0
		c.lastHighExt = c.avHighExt
	}
	stalled := c.stallTicks >= forceDrainStallTicks

	threshold := c.avHighExt
	if !stalled {
		if threshold < 8 {
			return
		}
		threshold -= 8
	}

	var keys []uint64
	for k := range c.avBuffer {
		if stalled || k <= threshold {
			keys = append(keys, k)
		}
	}
	if len(keys) == 0 {
		return
	}
	c.stats.forceDrains.Add(1)
	c.stats.forceDrainFlush.Add(1)
	c.stats.forceDrainEntries.Add(uint64(len(keys)))
	slices.Sort(keys)
	type drainSummary struct {
		channel byte
		frame   uint32
		total   uint16
		count   int
		first   uint16
		last    uint16
		trailer bool
	}
	summaries := make([]drainSummary, 0)
	summaryIndex := make(map[[2]uint64]int)
	for _, k := range keys {
		e := c.avBuffer[k]
		delete(c.avBuffer, k)
		c.avNextExt = k + 1
		c.avNextObserved.Store(c.avNextExt)
		asm := &c.keyAsm
		if e.channel == innerChInter {
			asm = &c.interAsm
		}
		_, _, onlineNum, hasTrailer := stripFragmentMetadataTrailer(e.payload)
		c.debugFrameEvent("force_drain_flush", e, asm, onlineNum, hasTrailer, nil)
		if c.verbose && !c.traceFrag {
			key := [2]uint64{uint64(e.channel), uint64(e.frameNum)}
			i, ok := summaryIndex[key]
			if !ok {
				summaries = append(summaries, drainSummary{channel: e.channel, frame: e.frameNum, total: e.totalFrags, first: e.fragIdx})
				i = len(summaries) - 1
				summaryIndex[key] = i
			}
			s := &summaries[i]
			s.count++
			s.last = e.fragIdx
			s.trailer = s.trailer || hasTrailer
		}
		c.emit(e)
	}
	for _, s := range summaries {
		log.Trace().Uint8("channel", s.channel).Uint32("frameNum", s.frame).
			Uint16("totalFrags", s.total).Int("flushedEntries", s.count).
			Uint16("firstFrag", s.first).Uint16("lastFrag", s.last).
			Bool("hasTrailer", s.trailer).Bool("isKeyframe", s.channel == innerChKey).
			Msg("petlibro forceDrain")
	}
	if stalled {
		c.stallTicks = 0
	}
}

// emit handles one in-order, fully-classified fragment.  Video frame
// structure on the wire:
//
//   - 0..N-1 "data" fragments with b1 in {0x00, 0x04, 0x05}.  Slice
//     bytes; paylen=1024 except possibly the last one; no trailer.
//   - 1 final "end" fragment with b1=0x01 sub17=0x01, paylen smaller
//     than 1024, ending in a 16-byte metadata trailer
//     (codec_id 0x4e + 4-byte variant prefix + 7 zero bytes + 4-byte
//     ms-ts). AF203 identifies FRAMEINFO byte 4 as onlineNum. SPS
//     dimensions, not this count/state field, identify the resolution.
//
// The end fragment is identified by the trailer signature on its
// payload tail, NOT by fragIdx (which can wrap when N>16 and reuse
// "16" both as a data-fragment index and the end marker).  inner[20]
// gives the camera's reported total fragment count, used as a
// secondary sanity check for end-fragment detection and as the basis
// for the "all data fragments lost" hard-floor in the end-fragment
// path.
//
// CRITICAL: the camera time-multiplexes IDR fragments on ch=0x05 with
// P-frame fragments on ch=0x07 in WIRE ORDER — e.g. mid-IDR a P-frame
// can arrive.  Each channel MUST have its own assembly buffer or the
// constant frame_num switching would drop every partial IDR.  Both
// channels' completed frames are emitted into one PTS-ordered output
// stream, since they share the camera's frame_num counter.
func (c *Client) emit(e *pendingFrag) {
	if c.startedAt.IsZero() {
		c.startedAt = time.Now()
	}
	if e.isAudio {
		c.queuePacket(&Packet{
			Codec:   CodecAACADTS,
			Payload: e.payload,
			FrameNo: c.emitAudioSeq,
		})
		c.emitAudioSeq++
		return
	}
	if e.channel != innerChKey && e.channel != innerChInter {
		return
	}
	c.stats.vidFrags.Add(1)

	stripped, frameTs, onlineNum, hasTrailer := stripFragmentMetadataTrailer(e.payload)
	c.debugAVFrameInfo(e, e.payload, hasTrailer)

	asm := &c.keyAsm
	if e.channel == innerChInter {
		asm = &c.interAsm
	}

	// Frame_num change on THIS channel without having seen the
	// previous frame's end-fragment.  For ch=0x05 this is normally a
	// back-to-back IDR where the previous IDR's end-fragment was lost
	// AND the cross-channel ch=0x07 flush below didn't fire — try to
	// emit the partial previous IDR via flushPendingIDR rather than
	// silently drop it (it might still be decodable with localised
	// artefacts).  For ch=0x07 it means a P-frame was abandoned mid-
	// assembly; just reset and count it as a drop.
	if asm.framePending && e.frameNum != asm.curFrameNum {
		isKeyframe := e.channel == innerChKey
		reason := "frame_num_jump_inter"
		if e.channel == innerChKey {
			reason = "frame_num_jump_key"
			c.stats.frameNumJumpKey.Add(1)
		} else {
			c.stats.frameNumJumpInter.Add(1)
		}
		c.debugFrameEvent(reason, e, asm, onlineNum, hasTrailer, &isKeyframe)
		if len(asm.buf) > 0 {
			if e.channel == innerChKey {
				c.flushPendingIDR(0)
			} else {
				asm.curAUGapped = true
				c.logAVLoss(asm, e.channel, false, nil, asm.buf)
				c.stats.fragSkips.Add(1)
				c.stats.fragsLost.Add(1)
				c.stats.vidDropped.Add(1)
				if c.strict {
					c.gopPoisoned = true
				}
				asm.reset()
			}
		}
	}
	if !asm.framePending || e.frameNum != asm.curFrameNum {
		asm.curFrameNum = e.frameNum
		asm.framePending = true
		c.stats.vidFramesIn.Add(1)
		asm.expectFragIdx = 0
		asm.curAUGapped = false
		asm.curAUDataCount = 0
		asm.curAUTotal = e.totalFrags
		asm.receivedData = make(map[uint16]struct{})
	}
	if asm.curAUTotal == 0 && e.totalFrags != 0 {
		asm.curAUTotal = e.totalFrags
	}

	// ch=0x07 arrival: flush any pending ch=0x05 IDR first (its end
	// fragment never came on this firmware), then process this P-frame.
	if e.channel == innerChInter && c.keyAsm.framePending && len(c.keyAsm.buf) > 0 {
		c.flushPendingIDR(frameTs)
	}

	if !hasTrailer {
		// Data fragment.  fragIdx==16 is exempt from gap-detection
		// because the camera reuses it as a position index when N>16.
		if e.fragIdx != asm.expectFragIdx && e.fragIdx != 16 {
			c.stats.fragSkips.Add(1)
			c.stats.fragIdxGap.Add(1)
			if e.fragIdx > asm.expectFragIdx {
				c.stats.fragsLost.Add(uint64(e.fragIdx - asm.expectFragIdx))
			} else {
				c.stats.fragsLost.Add(1)
			}
			asm.curAUGapped = true
			isKeyframe := e.channel == innerChKey
			c.debugFrameEvent("frag_idx_gap", e, asm, onlineNum, hasTrailer, &isKeyframe)
		}
		asm.expectFragIdx = e.fragIdx + 1
		asm.curAUDataCount++
		if asm.receivedData == nil {
			asm.receivedData = make(map[uint16]struct{})
		}
		asm.receivedData[e.fragIdx] = struct{}{}
		asm.buf = append(asm.buf, e.payload...)
		return
	}

	asm.buf = append(asm.buf, stripped...)
	expectedData := uint16(0)
	if asm.curAUTotal > 0 {
		expectedData = asm.curAUTotal - 1
	}
	if asm.curAUDataCount < expectedData {
		lost := uint64(expectedData - asm.curAUDataCount)
		c.stats.fragSkips.Add(1)
		c.stats.fragsLost.Add(lost)
		c.stats.expectedDataShortfall.Add(1)
		asm.curAUGapped = true
		isKeyframe := e.channel == innerChKey
		c.debugFrameEvent("expected_data_shortfall", e, asm, onlineNum, hasTrailer, &isKeyframe)
	}
	trailerBytes := e.payload[len(e.payload)-16:]
	c.logAVLoss(asm, e.channel, true, trailerBytes, asm.buf)

	// Hard floor: if we got the end-fragment but ZERO data fragments
	// for a multi-fragment frame, the AU has no slice header — only
	// trailing bits.  The decoder can't make sense of that and
	// produces a cascade of "out of range intra chroma pred mode" /
	// "mb_type X in I slice too large" / "top block unavailable"
	// errors that contaminate playback well past the next IDR.  Drop
	// it instead of emitting garbage.
	if expectedData >= 1 && asm.curAUDataCount == 0 {
		c.stats.fragSkips.Add(1)
		c.stats.fragsLost.Add(uint64(expectedData))
		c.stats.vidDropped.Add(1)
		c.stats.zeroDataHardDrop.Add(1)
		isKeyframe := e.channel == innerChKey
		c.debugFrameEvent("zero_data_hard_drop", e, asm, onlineNum, hasTrailer, &isKeyframe)
		asm.reset()
		if c.strict && e.channel == innerChKey {
			c.gopPoisoned = true
		}
		return
	}

	// Deep-copy boundary: asm.buf is reused for the next AU as
	// fragments arrive, so the slice we hand to emitAU / queuePacket
	// must own its bytes.  Packet.Payload therefore has no shared
	// backing storage with the assembler's working buffer and
	// consumers may retain it after the assembler advances.
	au := append([]byte(nil), asm.buf...)
	gapped := asm.curAUGapped
	wasKey := e.channel == innerChKey
	asmState := *asm
	asm.reset()

	if gapped {
		// Strict mode: drop the entire GOP for pristine pixels.
		if c.strict {
			c.gopPoisoned = true
			c.stats.vidDropped.Add(1)
			if wasKey {
				c.stats.strictIDRDrop.Add(1)
				isKeyframe := true
				c.debugFrameEvent("gapped_idr_drop", e, &asmState, onlineNum, hasTrailer, &isKeyframe)
			} else {
				c.stats.strictPDrop.Add(1)
				isKeyframe := false
				c.debugFrameEvent("strict_gapped_p_drop", e, &asmState, onlineNum, hasTrailer, &isKeyframe)
			}
			return
		}
		// Non-strict mode (default), per channel:
		//   * IDR on ch=0x05: emit the partial frame.  A slice with a
		//     mid-frame hole still gives the decoder SOMETHING to
		//     reference for the next ~25-50 P-frames, and macroblock
		//     artefacts at the hole location are far less disruptive
		//     than the multi-second freeze that results from dropping
		//     the IDR and waiting for the next clean one.
		//   * P-frame on ch=0x07: DROP.  A truncated P-frame slice
		//     causes the decoder to mis-parse the bitstream mid-way
		//     and the resulting errors ("mb_type 533 in I slice too
		//     large", "P sub_mb_type 11 out of range", etc) cascade
		//     into every subsequent P-frame in the GOP via reference
		//     prediction.  The decoder recovers automatically at the
		//     next clean P-frame (P-frames only reference the IDR,
		//     not each other for slice-data validity), so dropping
		//     ONE gapped P-frame loses one frame; emitting it loses
		//     the rest of the GOP to cascading decoder errors.
		c.stats.vidDropped.Add(1)
		if e.channel != innerChKey {
			return
		}
		isKeyframe := true
		c.debugFrameEvent("gapped_idr_emit", e, &asmState, onlineNum, hasTrailer, &isKeyframe)
	}
	// In strict mode only, drop P-frames in a poisoned GOP until the
	// next clean IDR.  In non-strict (default), let them through.
	if c.strict && c.gopPoisoned && !wasKey {
		c.stats.vidDropped.Add(1)
		c.stats.strictPDrop.Add(1)
		isKeyframe := false
		c.debugFrameEvent("strict_gop_poisoned_drop", e, &asmState, onlineNum, hasTrailer, &isKeyframe)
		return
	}
	c.emitAU(au, e.frameNum, e.channel, onlineNum, frameTs, hasTrailer)
}

// flushPendingIDR emits the accumulated ch=0x05 IDR buffer in the
// fallback case where we never received its b1=0x01 sub17=0x01
// end-fragment — either it was lost on the wire, or this camera
// firmware variant doesn't send one and we noticed an unrelated
// cross-channel signal (a ch=0x07 P-frame arrival, or a new ch=0x05
// frame_num) telling us the IDR is "as complete as it'll get".  The
// missing end-fragment carries the slice's bottom MB rows plus the
// rbsp_trailing_bits stop byte, so the AU we have is truncated; the
// decoder will show macroblock artefacts in the bottom strip.
//
// Strict mode (?strict=1) drops the truncated IDR and poisons the
// GOP — pristine pixels at the cost of a multi-second freeze until
// the next clean IDR.  Non-strict mode (default) emits it anyway.
func (c *Client) flushPendingIDR(nextPFrameTs uint32) {
	if len(c.keyAsm.buf) == 0 {
		c.keyAsm.reset()
		return
	}
	au := append([]byte(nil), c.keyAsm.buf...)
	midGapped := c.keyAsm.curAUGapped
	tailMissing := false
	if c.keyAsm.curAUTotal > 0 && c.keyAsm.curAUDataCount+1 < c.keyAsm.curAUTotal {
		tailMissing = true
		c.stats.fragSkips.Add(1)
		c.stats.fragsLost.Add(uint64(c.keyAsm.curAUTotal - 1 - c.keyAsm.curAUDataCount))
		c.stats.expectedDataShortfall.Add(1)
		c.debugFlushPendingIDR("flush_key_idr_tail_missing", &c.keyAsm)
	}
	if midGapped || tailMissing {
		c.keyAsm.curAUGapped = true
		c.logAVLoss(&c.keyAsm, innerChKey, false, nil, au)
	}
	asmState := c.keyAsm
	c.keyAsm.reset()
	if c.strict && (midGapped || tailMissing) {
		// Strict mode: drop the IDR if anything was lost — pristine
		// pixels over fluency.  Cascading inter-frame errors are
		// avoided by also poisoning the GOP so subsequent P-frames
		// are dropped until the next clean IDR.
		c.gopPoisoned = true
		c.stats.vidDropped.Add(1)
		c.stats.strictIDRDrop.Add(1)
		c.debugFlushPendingIDR("gapped_idr_drop", &asmState)
		return
	}
	if midGapped {
		// Non-strict: emit the partial IDR anyway.  ffmpeg will show
		// macroblock artefacts at the gap location for ~50 P-frames
		// until the next clean IDR arrives — visibly noisy but vastly
		// better than the multi-second video freeze that dropping the
		// IDR would produce.
		c.stats.vidDropped.Add(1)
	}
	if midGapped || tailMissing {
		c.debugFlushPendingIDR("gapped_idr_emit", &asmState)
	}
	// Guard the unsigned underflow: the camera clock at boot starts
	// near zero, and a P-frame whose ts is < 40 ms would naively
	// produce nextPFrameTs - 40 = ~0xFFFFFFC0 and lock the rest of
	// the session's PTS into the wrap-around regime.
	var cameraTimeMS uint32
	hasCameraTime := nextPFrameTs >= 40
	if hasCameraTime {
		cameraTimeMS = nextPFrameTs - 40
	}
	c.emitAU(au, asmState.curFrameNum, innerChKey, 0, cameraTimeMS, hasCameraTime)
}

// emitAU finalises one access unit and queues it for the consumer.
func (c *Client) emitAU(au []byte, cameraFrameNum uint32, channel, onlineNum byte, cameraTimeMS uint32, hasCameraTime bool) {
	if len(au) < 5 {
		return
	}
	isKey := annexbContainsNALType(au, h264.NALUTypeIFrame)
	if isKey {
		// A fresh IDR clears the GOP-poisoned state.
		c.gopPoisoned = false
	} else if c.strict && c.gopPoisoned {
		// Strict only: drop P-frames referencing a dropped IDR.
		// Non-strict lets the decoder conceal.
		c.stats.vidDropped.Add(1)
		c.stats.strictPDrop.Add(1)
		if c.verbose && c.traceFrag {
			log.Trace().
				Str("reason", "strict_gop_poisoned_drop").
				Str("quality", c.quality).
				Bool("strict", c.strict).
				Uint8("channel", 0).
				Uint32("frameNum", 0).
				Uint16("fragIdx", 0).
				Uint16("expectedFragIdx", 0).
				Uint16("totalFrags", 0).
				Uint16("curAUDataCount", 0).
				Uint8("onlineNum", 0).
				Bool("hasTrailer", false).
				Bool("isKeyframe", false).
				Bool("frameContextAvailable", false).
				Msg("petlibro frame decision")
		}
		return
	}

	c.queuePacket(&Packet{
		Codec:         CodecH264,
		Payload:       au,
		FrameNo:       c.emitSeq,
		CameraFrameNo: cameraFrameNum,
		Channel:       channel,
		OnlineNum:     onlineNum,
		CameraTimeMS:  cameraTimeMS,
		HasCameraTime: hasCameraTime,
		IsKeyframe:    isKey,
	})
	c.emitSeq++
	c.stats.vidFramesOut.Add(1)
}

// stripFragmentMetadataTrailer removes the 16-byte per-frame metadata
// block the Petlibro firmware appends to the LAST video fragment of
// each frame.  The block is `<codec_id 1B> <variant prefix 4B>
// <7B zeros> <4B LE ms ts>`:
//
//	P-frame:  4e  00 <p_or_k> 00 <onlineNum>  00*7  <ts>
//	IDR/key:  4e  00 <p_or_k> 00 <onlineNum>  00*7  <ts>
//
// codec_id is 0x4e (= CodecH264) for video.  Byte 1 of the variant
// prefix is 0x00 for P-frames and 0x01 for IDR keyframes.  Byte 3
// (last byte of the variant prefix) is named onlineNum by the public
// TUTK FRAMEINFO_t. AF203 names the corresponding source value gOnlineNum;
// it is an online AV-client count/state and can legitimately be values other
// than 1 or 2. Resolution is learned from the H.264 SPS.
//
// Stripping only 15 trailer bytes would leave the 0x4e codec_id in
// the slice tail; decoders read it as a stray NAL-14 prefix and bail
// on the next frame with "mb_skip_run invalid at MB 0,0".  Strip all
// 16 bytes.
//
// Callers should only invoke this on the frame's end fragment (whose
// tail unambiguously matches the signature) — in mid-frame fragments
// a coincidental match could shear real slice bytes. The codec, frame flag,
// camera index, and seven reserved zero bytes remain a strong signature;
// onlineNum is intentionally not part of it.
func stripFragmentMetadataTrailer(p []byte) (stripped []byte, ts uint32, onlineNum byte, hasTs bool) {
	if len(p) < 16 {
		return p, 0, 0, false
	}
	t := p[len(p)-15:] // 15-byte trailer right after the codec_id byte
	prefixOK := t[0] == 0x00 && (t[1] == 0x00 || t[1] == 0x01) &&
		t[2] == 0x00
	zerosOK := t[4] == 0 && t[5] == 0 && t[6] == 0 && t[7] == 0 &&
		t[8] == 0 && t[9] == 0 && t[10] == 0
	codecIDOK := p[len(p)-16] == CodecH264
	if prefixOK && zerosOK && codecIDOK {
		ts = binary.LittleEndian.Uint32(t[11:15])
		return p[:len(p)-16], ts, t[3], true
	}
	return p, 0, 0, false
}

// annexbContainsNALType walks an Annex-B buffer and reports whether
// any NAL unit's nal_unit_type matches want.  pkg/h264.NALUType only
// looks at the FIRST NAL of an AU; petlibro AUs may carry AUD/SEI/SPS
// before the IDR slice, so we have to scan all of them.  This is the
// single consolidated NAL-walker for the package — see also the
// AVCC walker used by probe() in producer.go which leans on the same
// h264.NALUType* constants.
func annexbContainsNALType(b []byte, want byte) bool {
	return annexbNAL(b, want) != nil
}

func annexbNAL(b []byte, want byte) []byte {
	for i := 0; i+3 < len(b); i++ {
		if b[i] != 0 || b[i+1] != 0 {
			continue
		}
		var nalPos int
		switch {
		case b[i+2] == 1:
			nalPos = i + 3
		case b[i+2] == 0 && i+4 < len(b) && b[i+3] == 1:
			nalPos = i + 4
		default:
			continue
		}
		if nalPos >= len(b) {
			return nil
		}
		if b[nalPos]&0x1F == want {
			end := len(b)
			for j := nalPos + 1; j+3 < len(b); j++ {
				if b[j] == 0 && b[j+1] == 0 && (b[j+2] == 1 || (b[j+2] == 0 && j+3 < len(b) && b[j+3] == 1)) {
					end = j
					break
				}
			}
			return b[nalPos:end]
		}
	}
	return nil
}

// queuePacket hands an assembled Packet to the consumer.  Selecting on
// done first means Close cannot race the send onto the now-closed
// frames channel — recover() and a separate closed-bool aren't needed.
func (c *Client) queuePacket(p *Packet) {
	select {
	case <-c.done:
		return
	default:
	}
	select {
	case <-c.done:
		return
	case c.frames <- p:
	default:
		c.stats.emitDrops.Add(1)
	}
}

// wrapSeq tracks a monotonic 64-bit counter that follows a 16-bit
// wire counter through wrap-arounds.  Single-step jumps >0x8000 are
// treated as wraps.
type wrapSeq struct {
	ext uint64
}

// extend turns a 16-bit wire counter into the monotonic 64-bit
// extended counter.  Returns ok=false when the wire value would land
// before the start of the sequence (corrupt or stale fragment); the
// caller MUST drop it rather than silently saturate to zero — feeding
// frag 0 into avBuffer poisons drainContiguous because avNextExt
// starts at >= 0x4000.
func (w *wrapSeq) extend(wire uint16) (ext uint64, ok bool) {
	lastWire := uint16(w.ext)
	fwd := (wire - lastWire) & 0xFFFF
	if fwd < 0x8000 {
		return w.ext + uint64(fwd), true
	}
	back := uint64((lastWire - wire) & 0xFFFF)
	if back > w.ext {
		return 0, false
	}
	return w.ext - back, true
}

func (w *wrapSeq) advanceTo(wire uint16) {
	if newExt, ok := w.extend(wire); ok && newExt > w.ext {
		w.ext = newExt
	}
}
