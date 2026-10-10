package petlibro

import (
	"encoding/binary"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
)

// Petlibro / Kalay LAN protocol constants.  Byte layouts are
// summarised in the per-builder comments below; field semantics
// were reverse-engineered from PCAPdroid captures of the official
// Petlibro Android app against PLAF103/PLAF203 cameras.

const (
	lanPort = 32761

	// Outer Kalay header (28 bytes) - byte 2 is the protocol version
	// (0x1D for Petlibro firmware vs 0x19 for Wyze).
	//
	// petlibro divergence vs pkg/tutk: tutk hard-codes the magic prefix
	// "\x04\x02\x12\x0a" or "\x04\x02\x19\x0a" inline in
	// pkg/tutk/session16.go (Session16.Msg) and elsewhere; byte 2 there
	// is 0x12/0x19 and byte 3 is 0x0a.  Petlibro firmware insists on
	// 0x1D at byte 2 and varies byte 3 by message kind
	// (flagsControl/flagsSession/flagsRecv).  The byte-2 fork is the
	// load-bearing constant for any future pkg/tutk consolidation.
	magicVersion = 0x1D

	// flags at outer byte 3
	flagsControl = 0x02 // LAN_SEARCH3, KNOCK2
	flagsSession = 0x0B // 0x407 phone→cam data
	flagsRecv    = 0x0A // 0x408 cam→phone
	flagsAlive   = 0x0A // 0x427 phone→cam keepalive (same wire value as flagsRecv)

	// IOTC msg-type field at offset 8..9 of the outer header
	msgLANSearch3 uint16 = 0x0601
	msgLANSearchR uint16 = 0x0602
	msgKnock2     uint16 = 0x0402
	msgKnockRR2   uint16 = 0x0404
	msgSessionC2D uint16 = 0x0407
	msgSessionD2C uint16 = 0x0408
	msgAliveC2D   uint16 = 0x0427
)

// sdkVersion is what we put in the LAN_SEARCH3 / KNOCK2 packets at
// offset 0x34/0x30 — matches what the Petlibro Android app sends.
var sdkVersion = []byte{0x00, 0x08, 0x03, 0x04}

// IOCtrl IDs used during the bootstrap sequence (verified against
// PCAPdroid captures of the official Petlibro Android app). Names for
// standard IDs follow TUTK's public AVAPIs.h / AVIOCTRLDEFs.h. The
// observed Petlibro SETSTREAMCTRL value remains 0x0024 rather than the
// stock IOTYPE_USER_IPCAM_SETSTREAMCTRL_REQ value 0x0320.
//
// Removing them from the bootstrap doesn't prevent the stream from
// starting, but matching the app's sequence avoids edge-case
// firmware quirks on some camera models.
const (
	ioctlInnerSendDataDelay   uint32 = 0x00FF // IOTYPE_INNER_SND_DATA_DELAY
	ioctlPetlibroStreamCtrl   uint32 = 0x0024 // captured Petlibro SETSTREAMCTRL body ID
	ioctlSetStreamCtrlReq     uint32 = 0x0320 // standard IOTYPE_USER_IPCAM_SETSTREAMCTRL_REQ
	ioctlSetStreamCtrlResp    uint32 = 0x0321
	ioctlStart                uint32 = 0x01FF // IOTYPE_USER_IPCAM_START
	ioctlStop                 uint32 = 0x02FF // IOTYPE_USER_IPCAM_STOP
	ioctlAudioOn              uint32 = 0x0300 // IOTYPE_USER_IPCAM_AUDIOSTART
	ioctlGetStreamCtrlReq     uint32 = 0x0322 // IOTYPE_USER_IPCAM_GETSTREAMCTRL_REQ
	ioctlGetStreamCtrlResp    uint32 = 0x0323
	ioctlGetAudioOutFormatReq uint32 = 0x032A // IOTYPE_USER_IPCAM_GETAUDIOOUTFORMAT_REQ
	ioctlGetVideoModeReq      uint32 = 0x0372 // IOTYPE_USER_IPCAM_GET_VIDEOMODE_REQ
)

// SETSTREAMCTRL bodies (12 bytes) — IOCtrl 0x0024 wire payload.
// Byte 4 selects which stream the body configures (0x01 = main /
// HD, 0x02 = sub / SD); bytes 6..7 are a feature-flag bitmask.
// Sending qualityHD enables HD; sending qualitySD enables SD.  The
// camera will continue emitting whichever stream(s) it was last
// told to enable; the HD/SD on/off state is sticky in the camera's
// cloud config (set via the Petlibro app), not toggled per-session.
var (
	qualityHD = []byte{byte(ioctlPetlibroStreamCtrl), 0, 0, 0, 0x01, 0x00, 0xff, 0x3f, 0, 0, 0, 0}
	qualitySD = []byte{byte(ioctlPetlibroStreamCtrl), 0, 0, 0, 0x02, 0x00, 0x8a, 0x81, 0, 0, 0, 0}
)

// Inner media-family markers at offset 16. AF203 uses 0x05 for the
// key/IDR family and 0x07 for inter frames; these are not HD/SD selectors.
const (
	innerChKey   byte = 0x05
	innerChInter byte = 0x07
	innerChAudio byte = 0x03 // AAC audio
)

// XOR key applied to IOCtrl bodies (the "outer" Charlie key — same one
// pkg/tutk uses for its TransCodePartial, just here at the body level).
//
// petlibro divergence vs pkg/tutk: pkg/tutk uses the full 32-byte
// charlie string "Charlie is the designer of P2P!!" (pkg/tutk/crypto.go:9)
// as a Luffy round-key.  Petlibro instead takes only the first 16
// bytes ("Charlie is the d") and uses it as a plain repeating XOR
// stream against IOCtrl payloads — a second, weaker scrambling layer
// the camera firmware applies on top of the outer Luffy crypto.  Any
// pkg/tutk consolidation needs both keys exposed.
var xorKey = []byte("Charlie is the d")

func xorBody(b []byte) []byte {
	out := make([]byte, len(b))
	for i, x := range b {
		out[i] = x ^ xorKey[i%16]
	}
	return out
}

// petlibro divergence vs pkg/tutk: tutk's outer header is built
// inline by Session16.Msg (pkg/tutk/session16.go:65) — a 28-byte
// header that hardcodes msg_type 0x0407 ("\x07\x04\x21") at offset 8,
// has no datatype/channelID at offsets 14..15, no constant 0x0000000C
// at 16..19, and packs the 16-byte session id at 12..27.  Petlibro
// instead uses an 8-byte nonce at 20..27, datatype/channelID at
// 14..15 (the original SDK reserved datatype=1 for the DTLS-shaped
// first packet — see .omc/research/_dtls_skip_test/), and threads the
// msg_type as msgSessionC2D = 0x0407 only when flags=flagsSession.
// Functionally similar wrapper, four constants apart.
//
// buildOuter builds a 28-byte Kalay outer header + body.
//
//	0..3   04 02 1D <flags>
//	4..5   plen LE = body+12
//	6..7   seq LE
//	8..9   msg_type LE (0x0407 = phone→cam data)
//	10..11 subtype LE (0x0021)
//	12..13 nonce[0..1]
//	14     channel_id  (0 normal, 1 after PLAY re-handshake)
//	15     datatype    (always 0 in current paths; the SDK uses 1 for
//	                   the legacy DTLS-shaped first packet, which the
//	                   Petlibro camera turned out not to require — see
//	                   .omc/research/_dtls_skip_test/)
//	16..19 0x0000000C
//	20..27 full 8-byte nonce
//	28..   body
func buildOuter(nonce []byte, seq uint16, body []byte, datatype, channelID, flags byte) []byte {
	plen := uint16(len(body) + 0x0C)
	p := make([]byte, 0x1C+len(body))
	p[0] = 0x04
	p[1] = 0x02
	p[2] = magicVersion
	p[3] = flags
	binary.LittleEndian.PutUint16(p[4:], plen)
	binary.LittleEndian.PutUint16(p[6:], seq)
	binary.LittleEndian.PutUint16(p[8:], msgSessionC2D)
	binary.LittleEndian.PutUint16(p[10:], 0x0021)
	p[12] = nonce[0]
	p[13] = nonce[1]
	p[14] = channelID
	p[15] = datatype
	binary.LittleEndian.PutUint32(p[16:], 0x0000000C)
	copy(p[20:], nonce)
	copy(p[28:], body)
	return p
}

// petlibro divergence vs pkg/tutk: pkg/tutk has NO LAN_SEARCH /
// KNOCK opcodes at all — tutk reaches the camera through Nebula
// (pkg/tutk/conn.go:31 connectDirect / connectRemote, which routes
// via Kalay's relay infrastructure when the camera isn't directly
// addressable).  Petlibro instead probes the LAN with msgLANSearch3
// (0x0601) and KNOCK2 (0x0402) before sending any inner-session data,
// matching what PCAPdroid captures show the official Petlibro app
// doing.  There is no tutk symbol to reuse here; the LAN_SEARCH3 /
// KNOCK2 wire format is petlibro-specific.
func buildLANSearch3(uid string, nonce []byte, w3 byte) []byte {
	p := make([]byte, 88)
	p[0] = 0x04
	p[1] = 0x02
	p[2] = magicVersion
	p[3] = flagsControl
	binary.LittleEndian.PutUint16(p[4:], 72)
	binary.LittleEndian.PutUint16(p[8:], msgLANSearch3)
	binary.LittleEndian.PutUint16(p[10:], 0x0021)
	copy(p[0x10:0x24], []byte(uid))
	copy(p[0x34:0x38], sdkVersion)
	copy(p[0x38:0x40], nonce)
	p[0x40] = w3
	copy(p[0x4A:0x52], []byte("00000000"))
	return p
}

// petlibro divergence vs pkg/tutk: see buildLANSearch3 above — KNOCK2
// (msgKnock2 = 0x0402) is the second leg of the petlibro LAN probe and
// has no pkg/tutk equivalent.  Carries a 16-bit subtype 0x0033 (vs
// 0x0021 for control msgs) and tail-pads the SDK version bytes at
// offset 0x30 instead of 0x34.
func buildKnock2(uid string, nonce []byte) []byte {
	p := make([]byte, 52)
	p[0] = 0x04
	p[1] = 0x02
	p[2] = magicVersion
	p[3] = flagsControl
	binary.LittleEndian.PutUint16(p[4:], 36)
	binary.LittleEndian.PutUint16(p[8:], msgKnock2)
	binary.LittleEndian.PutUint16(p[10:], 0x0033)
	copy(p[0x10:0x24], []byte(uid))
	copy(p[0x24:0x2C], nonce)
	copy(p[0x30:0x34], sdkVersion)
	return p
}

func buildAliveC2D(nonce []byte) []byte {
	p := make([]byte, 24)
	p[0] = 0x04
	p[1] = 0x02
	p[2] = magicVersion
	p[3] = flagsAlive
	binary.LittleEndian.PutUint16(p[4:], 8)
	binary.LittleEndian.PutUint16(p[8:], msgAliveC2D)
	binary.LittleEndian.PutUint16(p[10:], 0x0012)
	copy(p[0x10:0x18], nonce)
	return p
}

// Inner-body builders ----------------------------------------------------

// innerData wraps an IOCtrl-style body:
//
//	0c 00 0c 00 <cnt:2> 00 00  00 00 00 00 00 00 00 00
//	<channel_family:2> <message_seq:2>  01 00 00 00
//	<paylen:4>  <message_seq:4>  00 00 00 00
//	<xor'd payload>
func innerData(counter, channelFamily, messageSeq uint16, payload []byte) []byte {
	b := make([]byte, 36+len(payload))
	b[0] = 0x0c
	b[2] = 0x0c
	binary.LittleEndian.PutUint16(b[4:], counter)
	binary.LittleEndian.PutUint16(b[16:], channelFamily)
	binary.LittleEndian.PutUint16(b[18:], messageSeq)
	b[20] = 0x01
	binary.LittleEndian.PutUint32(b[24:], uint32(len(payload)))
	binary.LittleEndian.PutUint32(b[28:], uint32(messageSeq))
	copy(b[36:], xorBody(payload))
	return b
}

// transportACK is the Petlibro type-0x09 receive report. AF203's live BBR
// dispatcher treats every AV sequence in (AVBase, AVHigh] as acknowledged
// unless its offset from AVBase appears in Missing. ReliableRecvACK belongs
// to a separate reassembled control-message sequence space.
type transportACK struct {
	Ordinal         uint16
	AVBase          uint16
	AVHigh          uint16
	ReliableRecvACK uint16
	Missing         []uint16
	State           uint16 // observed session transport state; zero is accepted
	SendCount       uint16
	Tick            uint16
}

func (a transportACK) marshal() []byte {
	n := 22 + 2*len(a.Missing)
	if n < 24 {
		n = 24
	}
	b := make([]byte, n)
	b[0] = 0x09
	// Captured Petlibro clients use format 0x000c. AF203's internal SDK
	// builder uses 0x000b, but the camera accepts this newer wire form.
	b[2] = 0x0c
	binary.LittleEndian.PutUint16(b[4:], a.Ordinal)
	binary.LittleEndian.PutUint16(b[8:], a.AVBase)
	binary.LittleEndian.PutUint16(b[10:], a.AVHigh)
	binary.LittleEndian.PutUint16(b[12:], a.ReliableRecvACK)
	binary.LittleEndian.PutUint16(b[14:], uint16(len(a.Missing)))
	binary.LittleEndian.PutUint16(b[16:], a.State)
	binary.LittleEndian.PutUint16(b[18:], a.SendCount)
	binary.LittleEndian.PutUint16(b[20:], a.Tick)
	for i, offset := range a.Missing {
		binary.LittleEndian.PutUint16(b[22+2*i:], offset)
	}
	return b
}

// innerTimingFeedback builds type 0x0b. In the reactive path the timing
// stamp at +8 is echoed from a type-0x0a probe. The remaining statistics are
// deliberately zero because their exact SDK meanings are not needed by AF203.
func innerTimingFeedback(ordinal, lastRecv uint16, timingStamp uint32, metric uint32) []byte {
	b := make([]byte, 20)
	b[0] = 0x0b
	b[2] = 0x0c
	binary.LittleEndian.PutUint16(b[4:], ordinal)
	binary.LittleEndian.PutUint16(b[6:], lastRecv)
	binary.LittleEndian.PutUint32(b[8:], timingStamp)
	binary.LittleEndian.PutUint32(b[12:], metric)
	return b
}

// innerTimingProbe builds the captured bootstrap type-0x0a timing probe.
// Ordinary session liveness is provided by outer message 0x0427 instead.
func innerTimingProbe(ordinal uint16, timingStamp uint32) []byte {
	b := make([]byte, 16)
	b[0] = 0x0a
	b[1] = 0x08
	b[2] = 0x0c
	binary.LittleEndian.PutUint16(b[4:], ordinal)
	binary.LittleEndian.PutUint32(b[8:], timingStamp)
	binary.LittleEndian.PutUint16(b[12:], 0x0032)
	return b
}

// IOCtrl helpers -------------------------------------------------------

// ioctlBody builds the plaintext carried by innerData for one avSendIOCtrl
// equivalent: the uint32 IO type followed by its message payload.
func ioctlBody(ctrlID uint32, payload []byte) []byte {
	b := make([]byte, 4+len(payload))
	binary.LittleEndian.PutUint32(b, ctrlID)
	copy(b[4:], payload)
	return b
}

// ioctlBody12 builds an IOCtrl with an 8-byte zero payload. For IPCAM_START
// this is exactly SMsgAVIoctrlAVStream{channel: 0, reserved: {0,0,0,0}}.
func ioctlBody12(ctrlID uint32) []byte {
	return ioctlBody(ctrlID, make([]byte, 8))
}

// AV-LOGIN wire constants. Verified byte-exact against 36 captured
// LOGIN buffers from the official Petlibro Android app on PLAF103 /
// PLAF203 cameras. See internal/petlibro/README.md "Wire protocol
// references" for the reverse-engineering trail and the full plaintext
// field map summarised here:
//
//	wrapper:      4 B inner-cmd magic + 12 B pad + 4 B AV header + 4 B login_serial
//	view_account: "admin" + zero pad, 257 B total (12 B head plaintext,
//	              245 B tail inside the Luffy region)
//	view_password: "888888" + zero pad, 257 B inside the Luffy region
//	trailer (32 B for LOGIN A, 34 B for LOGIN B):
//	  +0x00 u32 LE trailer_flag_0 = 1
//	  +0x04 u32 LE opcode_support_count = 4
//	  +0x08 [4]u32 LE opcode_support_bitmap = {0x001F07FB, 0, 0, 0x00030000}
//	  +0x18 u8 auth_type = 0 (AUTHPWD) + 3 B pad
//	  +0x1C u8 enable_video_on_connect = 1 + 3 B pad
//	  +0x20 u8 enable_audio_on_connect (LOGIN B only) + 1 B pad
const (
	// view_account / view_password are each padded to sdkPaddedLen
	// (acc_len = pw_len = 0x101 = 257 B in the modern TUTK SDK).
	sdkPaddedLen = 0x101

	// Petlibro firmware-default credentials. These are NOT a user
	// secret and MUST NOT be exposed as a URL parameter or env var.
	// The same "admin" / "888888" pair appears in all 36 captured
	// LOGIN buffers across multiple cameras and account owners — the
	// LAN AV-LOGIN check is effectively a no-op on this firmware.
	// Real access control happens upstream at the Nebula whitelist
	// gating UDP/32761 reachability; once the camera answers
	// LAN_SEARCH3 the LOGIN goes through with these constants.
	// Changing the camera password in the Petlibro app does NOT
	// change what the firmware accepts here.
	viewAccount  = "admin"
	viewPassword = "888888"

	// First 12 B of view_account live in the plaintext wrapper (outside
	// the cipher region); the remaining 245 B are zero-padding inside.
	viewAccountHeadLen = 12
	viewAccountTailLen = sdkPaddedLen - viewAccountHeadLen // 245

	// Offset within the encrypted body where the trailer fields begin.
	trailerOffset = viewAccountTailLen + sdkPaddedLen // 245 + 257 = 502

	// Trailer field byte sizes.
	trailerLenLogin  = 32 // LOGIN (A)
	trailerLenLogin1 = 34 // LOGIN_1 (B) — adds enable_audio_on_connect (1 B) + pad (1 B)

	// AUTHPWD — auth_type = 0 (per login_c.md line 416).
	authTypePassword byte = 0

	// trailer_flag_0 — u32 LE = 1 in every observed capture. Candidate
	// semantics (bResend / enable_2way) per login_c.md §3.3, neither
	// confirmed. See login_plaintext.md §6 Q2.
	trailerFlag0 uint32 = 1

	// opcode_support — bitmap of AV opcodes the client implements.
	// Decomposition into bit→opcode-ID is unknown; treat as opaque.
	// See login_plaintext.md §6 Q3.
	opcodeSupportCount uint32 = 4

	// enable_video_on_connect = TRUE in both A and B. enable_audio
	// = FALSE (B-only field; absent in A). See login_plaintext.md §6 Q4.
	enableVideoOnConnect byte = 1
	enableAudioOnConnect byte = 0
)

// opcode_support_bitmap (4 u32 LE) — opaque constant, identical for A/B.
var opcodeSupportBitmap = [4]uint32{0x001F07FB, 0, 0, 0x00030000}

// LOGIN A/B wrapper bytes [0x00:0x18] — everything BEFORE the
// view_account ASCII plaintext at [0x18:0x24]. Bytes [0x14:0x18] are a
// placeholder for login_serial, overwritten at build time.
//
// The AV-header bytes at [0x10:0x14] (`22 02 01 00` for A, `24 02 00 00`
// for B) discriminate the inner-cmd subtype on the wire. Their exact
// semantic decomposition is unverified — see login_plaintext.md §6 Q1.
var (
	loginAWrapperHead = []byte{
		0x00, 0x00, 0x0c, 0x00, // inner-cmd magic: kind=0x0C, subtype=0x00 (LOGIN)
		0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x22, 0x02, 0x01, 0x00, // AV header (A) — see §6 Q1
		0x00, 0x00, 0x00, 0x00, // login_serial placeholder
	}
	loginBWrapperHead = []byte{
		0x00, 0x20, 0x0c, 0x00, // inner-cmd magic: subtype=0x20 (LOGIN_1)
		0x00, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x24, 0x02, 0x00, 0x00, // AV header (B) — see §6 Q1
		0x00, 0x00, 0x00, 0x00, // login_serial placeholder
	}
)

// buildLoginPair returns (loginA, loginB) inner bodies for the
// AV-LOGIN handshake with a fresh 32-bit session seed at wrapper
// offset 0x14. B's login_serial is always A's + 1 (invariant per the
// SDK at avConnect_inner:26754 and verified in 36/36 captured pairs).
//
// Each body has the shape:
//
//	[0x00:0x18]  wrapper (inner-cmd + AV header + login_serial), plaintext
//	[0x18:0x24]  view_account head ("admin" + 7 zero B), plaintext
//	[0x24:end]   Luffy-encrypted body (view_account tail, view_password,
//	             trailer fields). NOTE: encrypt = tutk.ReverseTransCodePartial;
//	             decrypt = tutk.TransCodePartial. The names in pkg/tutk
//	             are inverted relative to the directions they perform.
//
// The only consumer is client.go:363; the returned buffers go straight
// to buildOuter (NOT through innerData/xorBody), so the Luffy pass is
// the only transformation applied here.
func buildLoginPair(seed uint32) ([]byte, []byte) {
	a := buildLogin(loginAWrapperHead, seed, false)
	b := buildLogin(loginBWrapperHead, seed+1, true)
	return a, b
}

// buildLogin assembles one side of the LOGIN pair. isLogin1=true adds
// the LOGIN_1-only enable_audio_on_connect field (+ 1 B padding) to
// the trailer.
func buildLogin(wrapperHead []byte, loginSerial uint32, isLogin1 bool) []byte {
	// 1. Wrapper [0x00:0x18] + plaintext view_account head [0x18:0x24].
	wrapper := make([]byte, len(wrapperHead)+viewAccountHeadLen)
	copy(wrapper, wrapperHead)
	binary.LittleEndian.PutUint32(wrapper[0x14:], loginSerial)
	copy(wrapper[len(wrapperHead):], viewAccount) // zero-padded to 12 B by make

	// 2. Plaintext body to be encrypted [0x24:end].
	trailerLen := trailerLenLogin
	if isLogin1 {
		trailerLen = trailerLenLogin1
	}
	pt := make([]byte, trailerOffset+trailerLen)

	// view_account tail (245 zero B) is already zero from make.
	// view_password at body+viewAccountTailLen: "888888" + 251 zero B.
	copy(pt[viewAccountTailLen:], viewPassword)

	// Trailer fields begin at body+trailerOffset.
	tr := pt[trailerOffset:]
	binary.LittleEndian.PutUint32(tr[0x00:], trailerFlag0)
	binary.LittleEndian.PutUint32(tr[0x04:], opcodeSupportCount)
	binary.LittleEndian.PutUint32(tr[0x08:], opcodeSupportBitmap[0])
	binary.LittleEndian.PutUint32(tr[0x0C:], opcodeSupportBitmap[1])
	binary.LittleEndian.PutUint32(tr[0x10:], opcodeSupportBitmap[2])
	binary.LittleEndian.PutUint32(tr[0x14:], opcodeSupportBitmap[3])
	tr[0x18] = authTypePassword     // + 3 B alignment padding (already zero)
	tr[0x1C] = enableVideoOnConnect // + 3 B alignment padding (already zero)
	if isLogin1 {
		tr[0x20] = enableAudioOnConnect // + 1 B alignment padding (already zero)
	}

	// 3. Luffy-encrypt the plaintext body. The pkg/tutk names are
	// inverted: ReverseTransCodePartial is the encrypt direction here.
	ct := tutk.ReverseTransCodePartial(nil, pt)

	return append(wrapper, ct...)
}
