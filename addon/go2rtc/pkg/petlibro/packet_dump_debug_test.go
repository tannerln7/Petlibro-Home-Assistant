package petlibro

import (
	"encoding/binary"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
	"testing"
)

// TestDumpPacketSummary turns the plaintext debug dumps into a stable protocol
// inventory. Run from the repository root, for example:
//
//	cd /path/to/go2rtc
//	PETLIBRO_C2D_DUMP=/tmp/c2d.dat PETLIBRO_D2C_DUMP=/tmp/d2c.dat go test ./pkg/petlibro -run TestDumpPacketSummary -v
func TestDumpPacketSummary(t *testing.T) {
	c2d := os.Getenv("PETLIBRO_C2D_DUMP")
	d2c := os.Getenv("PETLIBRO_D2C_DUMP")
	if c2d == "" && d2c == "" {
		t.Skip("PETLIBRO_C2D_DUMP and PETLIBRO_D2C_DUMP are unset")
	}
	counts := map[string]int{}
	unknown := map[string][]string{}
	var ackMin, ackMax, ackCurrent uint16
	var haveACK bool
	if c2d != "" {
		readC2DDump(t, c2d, func(body []byte) {
			class := classifyC2D(body)
			counts[class]++
			if class == "ack" {
				curr := binary.LittleEndian.Uint16(body[10:12])
				if !haveACK || curr < ackMin {
					ackMin = curr
				}
				if !haveACK || curr > ackMax {
					ackMax = curr
				}
				ackCurrent = curr
				haveACK = true
			}
			if strings.HasPrefix(class, "unknown") && len(unknown[class]) < 3 {
				unknown[class] = append(unknown[class], hex.EncodeToString(body))
			}
		})
		logSortedCounts(t, "C2D", counts)
		if haveACK {
			t.Logf("C2D ACK avCurr min=0x%04x max=0x%04x current=0x%04x", ackMin, ackMax, ackCurrent)
		}
		keys := sortedKeys(unknown)
		for _, key := range keys {
			t.Logf("C2D %s examples=%v", key, unknown[key])
		}
	}

	if d2c != "" {
		counts = map[string]int{}
		unknown = map[string][]string{}
		var lastSub uint16
		var haveSub bool
		var seqGaps uint64
		var seqStarted bool
		var seqWrap wrapSeq
		var watermark, high uint64
		seqSeen := map[uint64]struct{}{}
		var extendedCandidates, extendedParsed, extendedRejected uint64
		var unknown0c08, unknown0c0d uint64
		lastFrame := map[byte]uint32{}
		frameGaps := map[byte]uint64{}
		readD2CDump(t, d2c, func(pkt []byte) {
			class, sub, channel, frame, media := classifyD2C(pkt)
			counts[class]++
			if strings.HasPrefix(class, "extended_media_") {
				extendedCandidates++
				if strings.HasPrefix(class, "extended_media_rejected_") {
					extendedRejected++
					if strings.Contains(class, "b1_0x08") {
						unknown0c08++
					} else if strings.Contains(class, "b1_0x0d") {
						unknown0c0d++
					}
				} else {
					extendedParsed++
				}
			}
			if media {
				if haveSub && uint16(lastSub+1) != sub {
					seqGaps++
				}
				lastSub, haveSub = sub, true
				if !seqStarted {
					seqWrap.ext = uint64(sub)
					watermark = seqWrap.ext - 1
					high = watermark
					seqStarted = true
				}
				if ext, ok := seqWrap.extend(sub); ok {
					if ext > high {
						high = ext
						seqWrap.advanceTo(sub)
					}
					if ext > watermark {
						seqSeen[ext] = struct{}{}
						for {
							next := watermark + 1
							if _, exists := seqSeen[next]; !exists {
								break
							}
							delete(seqSeen, next)
							watermark = next
						}
					}
				}
				if prior, ok := lastFrame[channel]; ok && frame != prior && frame != prior+1 {
					frameGaps[channel]++
				}
				lastFrame[channel] = frame
			}
			if strings.HasPrefix(class, "unknown") && len(unknown[class]) < 3 {
				unknown[class] = append(unknown[class], hex.EncodeToString(pkt))
			}
		})
		logSortedCounts(t, "D2C", counts)
		t.Logf("D2C media sequence discontinuities=%d frame-number discontinuities key=%d inter=%d audio=%d",
			seqGaps, frameGaps[innerChKey], frameGaps[innerChInter], frameGaps[innerChAudio])
		t.Logf("D2C extendedMediaCandidates=%d extendedMediaParsed=%d extendedMediaRejected=%d unknown0c08Remaining=%d unknown0c0dRemaining=%d",
			extendedCandidates, extendedParsed, extendedRejected, unknown0c08, unknown0c0d)
		if seqStarted {
			t.Logf("D2C contiguousWatermarkProgressEstimate watermark=0x%x high=0x%x pending=%d",
				watermark, high, len(seqSeen))
		}
		for _, key := range sortedKeys(unknown) {
			t.Logf("D2C %s examples=%v", key, unknown[key])
		}
	}
}

func TestPacketSummaryClassifiers(t *testing.T) {
	ack := (transportACK{Ordinal: 3, AVBase: 0x4000, AVHigh: 0x4010, Tick: 9}).marshal()
	if got := classifyC2D(ack); got != "ack" {
		t.Fatalf("ACK classified as %q", got)
	}
	data := innerData(4, 0x7000, 1, ioctlBody12(ioctlStart))
	if got := classifyC2D(data); got != "ioctrl_ipcam_start_0x01ff" {
		t.Fatalf("START classified as %q", got)
	}
	lost := make([]byte, 14)
	binary.LittleEndian.PutUint32(lost, 123)
	binary.LittleEndian.PutUint16(lost[8:], 2)
	binary.LittleEndian.PutUint16(lost[10:], 4)
	binary.LittleEndian.PutUint16(lost[12:], 7)
	if !looksLikeLegacyLostPos(lost) {
		t.Fatal("legacy lost-position candidate was not recognized")
	}
	extended := makeExtendedPkt(extendedFragSpec{
		b1: 0x08, channel: innerChKey, subFlag: 0,
		subWire: 0x4000, totalFrags: 2, fragIdx: 0,
		frameNum: 1, nextFrameLike: 2, payload: []byte{1, 2, 3},
	})
	class, sub, channel, frame, media := classifyD2C(extended)
	if class != "extended_media_data_ch0x05_b1_0x08" || !media || sub != 0x4000 || channel != innerChKey || frame != 1 {
		t.Fatalf("extended packet classification=%q sub=0x%x channel=0x%x frame=%d media=%t", class, sub, channel, frame, media)
	}
}

func classifyC2D(body []byte) string {
	if len(body) >= 2 && body[1] == 0x07 {
		return "candidate_legacy_resend_wrapper_type_0x07"
	}
	if len(body) > 500 && body[0] == 0x00 && (body[1] == 0x00 || body[1] == 0x20) {
		if body[1] == 0x20 {
			return "login_1"
		}
		return "login"
	}
	if len(body) >= 24 && body[0] == 0x09 {
		return "ack"
	}
	if len(body) >= 1 && body[0] == 0x0a {
		return "timing_probe"
	}
	if len(body) >= 1 && body[0] == 0x0b {
		return "timing_feedback"
	}
	if len(body) >= 40 && body[0] == 0x0c {
		n := int(binary.LittleEndian.Uint32(body[24:28]))
		if n >= 4 && 36+n <= len(body) {
			plain := xorBody(body[36 : 36+n])
			id := binary.LittleEndian.Uint32(plain)
			switch id {
			case ioctlInnerSendDataDelay:
				return "ioctrl_send_data_delay_0x00ff"
			case ioctlStart:
				return "ioctrl_ipcam_start_0x01ff"
			case 0x02ff:
				return "ioctrl_ipcam_stop_0x02ff"
			case ioctlPetlibroStreamCtrl:
				return "ioctrl_petlibro_streamctrl_0x0024"
			case ioctlSetStreamCtrlReq:
				return "ioctrl_setstreamctrl_req_0x0320"
			case ioctlGetStreamCtrlReq:
				return "ioctrl_getstreamctrl_req_0x0322"
			case ioctlGetAudioOutFormatReq:
				return "ioctrl_getaudiooutformat_req_0x032a"
			case ioctlGetVideoModeReq:
				return "ioctrl_getvideomode_req_0x0372"
			default:
				return fmt.Sprintf("ioctrl_0x%04x", id)
			}
		}
		return "data_malformed"
	}
	// The public 14W36 client uses AV packet type 0x07 and a payload of
	// frameNo:u32, reserved:u32, count:u16, lostPositions:[count]u16.
	// This only identifies a candidate; Petlibro's newer wrapper is unmapped.
	if looksLikeLegacyLostPos(body) {
		return "candidate_legacy_lost_pos"
	}
	return unknownKey("unknown", body)
}

func classifyD2C(pkt []byte) (string, uint16, byte, uint32, bool) {
	if len(pkt) < 0x1c+4 || binary.LittleEndian.Uint16(pkt[8:10]) != msgSessionD2C {
		return unknownKey("unknown_outer", pkt), 0, 0, 0, false
	}
	inner := pkt[0x1c:]
	if len(inner) >= 2 && inner[1] == 0x07 {
		return "candidate_legacy_resend_wrapper_type_0x07", 0, 0, 0, false
	}
	if len(inner) >= 2 && inner[0] == 0x0c && isExtendedMediaFamily(inner[1]) {
		m, reason, ok := decodeExtendedMedia(inner)
		if ok {
			kind := "data"
			if m.isEnd {
				kind = "end"
			}
			class := fmt.Sprintf("extended_media_%s_ch0x%02x_b1_0x%02x", kind, m.channel, m.b1)
			if _, _, online, hasTrailer := stripFragmentMetadataTrailer(m.payload); hasTrailer {
				t := m.payload[len(m.payload)-16:]
				class += fmt.Sprintf("_frameinfo_codec0x%04x_flag%d_onlineNum_%d", binary.LittleEndian.Uint16(t), t[2], online)
			}
			return class, m.subWire, m.channel, m.frameNum, true
		}
		if _, normalOK := decodeNormalMedia(inner); !normalOK {
			return fmt.Sprintf("extended_media_rejected_b1_0x%02x_reason_%s", m.b1, reason), m.subWire, m.channel, m.frameNum, false
		}
	}
	if len(inner) >= 36 && inner[0] == 0x0c {
		ch := inner[16]
		sub := binary.LittleEndian.Uint16(inner[18:20])
		frame := binary.LittleEndian.Uint32(inner[28:32])
		if ch == innerChKey || ch == innerChInter || ch == innerChAudio {
			class := fmt.Sprintf("media_ch0x%02x_b1_0x%02x", ch, inner[1])
			paylen := int(binary.LittleEndian.Uint16(inner[24:26]))
			if paylen >= 16 && 36+paylen <= len(inner) {
				payload := inner[36 : 36+paylen]
				if _, _, online, ok := stripFragmentMetadataTrailer(payload); ok {
					t := payload[len(payload)-16:]
					class += fmt.Sprintf("_frameinfo_codec0x%04x_flag%d_onlineNum_%d", binary.LittleEndian.Uint16(t), t[2], online)
				}
			}
			return class, sub, ch, frame, true
		}
		paylen := int(binary.LittleEndian.Uint32(inner[24:28]))
		if paylen >= 4 && 36+paylen <= len(inner) {
			plain := xorBody(inner[36 : 36+paylen])
			return fmt.Sprintf("ioctrl_0x%04x", binary.LittleEndian.Uint32(plain)), sub, ch, frame, false
		}
		if looksLikeLegacyLostPos(inner) {
			return "candidate_legacy_lost_pos", sub, ch, frame, false
		}
	}
	return unknownKey("unknown_inner", inner), 0, 0, 0, false
}

func looksLikeLegacyLostPos(b []byte) bool {
	if len(b) < 10 {
		return false
	}
	count := int(binary.LittleEndian.Uint16(b[8:10]))
	return count > 0 && count <= 256 && len(b) == 10+count*2
}

func readC2DDump(t *testing.T, path string, fn func([]byte)) {
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for record := 1; ; record++ {
		var h [12]byte
		_, err := io.ReadFull(f, h[:])
		if errors.Is(err, io.EOF) {
			return
		}
		if err != nil {
			t.Fatalf("C2D record %d: %v", record, err)
		}
		n := binary.LittleEndian.Uint32(h[8:])
		if n > 1<<20 {
			t.Fatalf("C2D record %d too large: %d", record, n)
		}
		b := make([]byte, n)
		if _, err := io.ReadFull(f, b); err != nil {
			t.Fatalf("C2D record %d: %v", record, err)
		}
		fn(b)
	}
}

func readD2CDump(t *testing.T, path string, fn func([]byte)) {
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	for record := 1; ; record++ {
		var h [4]byte
		_, err := io.ReadFull(f, h[:])
		if errors.Is(err, io.EOF) {
			return
		}
		if err != nil {
			t.Fatalf("D2C record %d: %v", record, err)
		}
		n := binary.LittleEndian.Uint32(h[:])
		if n > 1<<20 {
			t.Fatalf("D2C record %d too large: %d", record, n)
		}
		b := make([]byte, n)
		if _, err := io.ReadFull(f, b); err != nil {
			t.Fatalf("D2C record %d: %v", record, err)
		}
		fn(b)
	}
}

func prefixHex(b []byte, n int) string {
	if len(b) < n {
		n = len(b)
	}
	return hex.EncodeToString(b[:n])
}
func unknownKey(prefix string, b []byte) string {
	return fmt.Sprintf("%s_len%d_first2_%s_first4_%s_first8_%s", prefix, len(b), prefixHex(b, 2), prefixHex(b, 4), prefixHex(b, 8))
}
func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
func logSortedCounts(t *testing.T, label string, counts map[string]int) {
	for _, key := range sortedKeys(counts) {
		t.Logf("%s %-36s %d", label, key, counts[key])
	}
}
