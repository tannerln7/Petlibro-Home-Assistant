// Package petlibro is a clean-room LAN P2P client for Petlibro pet
// cameras (PLAF203 / PLAF103 / etc).  These cameras run a Kalay/TUTK
// firmware variant: same luffy crypto as Wyze (we reuse pkg/tutk's
// TransCodePartial / ReverseTransCodePartial directly) but with a
// different protocol-version byte, direction flag, LOGIN structure
// and bootstrap IOCtrl sequence.
//
// Pkg layout:
//
//	templates.go  — protocol constants + LOGIN templates + frame builders
//	client.go     — Client struct + Dial + send/recv helpers + public surface
//	handshake.go  — LAN_SEARCH3 / KNOCK2 / LOGIN A+B sequencing
//	bootstrap.go  — post-LOGIN IOCtrl bootstrap (SETSTREAMCTRL → IPCAM_START)
//	recv.go       — UDP recv/processor goroutines + maintenance loop + stats
//	assembler.go  — fragment reassembly (wrapSeq, channelAsm, parseDatagram)
//	camera.go     — PLAF203 lifecycle/readiness/timestamp normalization adapter
//	producer.go   — exposes normalized Camera media to go2rtc
//	runtime_status.go — atomic structured camera status for backend consumers
//
// petlibro divergence vs pkg/tutk: petlibro shares pkg/tutk's Luffy
// crypto (tutk.TransCodePartial / tutk.ReverseTransCodePartial) but
// reimplements every other layer because the wire protocol differs.
// Each new file above carries a short "petlibro divergence vs pkg/tutk"
// header comment pointing at the parallel tutk site for future
// consolidation reference; the user-locked decision #5 was to keep
// petlibro standalone and add these breadcrumbs rather than reshape
// pkg/tutk in this PR.
package petlibro

import (
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/AlexxIT/go2rtc/pkg/tutk"
	"github.com/rs/zerolog"
)

// Codec IDs we surface to the producer (subset of pkg/tutk's table).
const (
	CodecH264    byte = 0x4E
	CodecAACADTS byte = 0x87
)

const (
	defaultACKInterval = 25 * time.Millisecond
	// This local bound keeps type-0x09 comfortably below a UDP MTU. If more
	// holes exist, AVHigh is reduced before the first hole that
	// cannot be represented, so no missing packet is falsely acknowledged.
	maxTransportNACKs = 256
	// A range represents any number of consecutive received packets, so the
	// common one-hole case stays constant-size. The cap prevents a hostile or
	// severely reordered stream from creating an unbounded number of ranges.
	maxACKSeenRanges = 256
)

type ackSeenRange struct {
	first uint64
	last  uint64
}

// log is the package-level zerolog instance.  internal/petlibro's
// Init() injects app.GetLogger("petlibro") via SetLogger so library
// messages reach the same sink as the rest of go2rtc.  Default is a
// no-op so unit tests don't depend on internal/ being loaded.
var log = zerolog.Nop()

// SetLogger lets the internal/petlibro glue inject the
// app.GetLogger("petlibro") instance so library messages go through
// the same zerolog sink as the rest of go2rtc.  Matches the wyze/tapo
// pattern of internal-side logger configuration, just made explicit
// instead of hidden behind a fmt.Printf chain.
func SetLogger(l zerolog.Logger) { log = l }

// Packet is one fully-assembled media frame from the camera.
type Packet struct {
	Codec         byte
	Payload       []byte
	CameraTimeMS  uint32
	HasCameraTime bool
	FrameNo       uint32
	CameraFrameNo uint32
	Channel       byte
	OnlineNum     byte
	IsKeyframe    bool
}

// Client is one LAN session against a Petlibro camera.
type Client struct {
	sessionID      string
	adapterID      string
	producerID     uint32
	startupAttempt int
	connectedAt    time.Time
	established    atomic.Bool

	conn  *net.UDPConn
	cam   *net.UDPAddr
	uid   string
	nonce []byte

	kseq     uint16 // outer kalay seq (byte 6..7)
	icounter uint16 // inner cmd counter (byte 4..5)
	txMu     sync.Mutex
	innerMu  sync.Mutex // serializes live IOCtrl counters and shutdown

	// in-order reassembly
	wrap           wrapSeq
	avBuffer       map[uint64]*pendingFrag
	avNextExt      uint64
	avHighExt      uint64
	avNextObserved atomic.Uint64 // synchronized mirror for maintenance ACK diagnostics

	// Receive-side ACK tracking is deliberately independent from the
	// assembler cursor. forceDrain may skip a missing packet so output
	// can continue, but only packets actually observed on the wire may
	// advance ackWatermarkExt.
	ackMu           sync.Mutex
	ackReportedExt  uint64 // contiguous position used as +8 in the last successfully sent ACK
	ackWatermarkExt uint64
	ackHighExt      uint64
	ackSeenRanges   []ackSeenRange
	ackSeenPending  uint64
	reliableRecvACK uint16
	reliableRecvSet bool
	reliableSeen    map[uint16]struct{}
	ackSendCount    uint16
	ackGapStarted   time.Time
	ackPendingWarn  uint64

	// monotonic counters for outgoing Packets (camera's per-channel
	// frame_num is independent for the key/inter families, so we use our own)
	emitSeq      uint32
	emitAudioSeq uint32
	startedAt    time.Time

	// frames carries assembled AUs to the camera adapter. done is the sole
	// closure signal. frames remains open because processor goroutines are not
	// joined and closing it would permit a send-on-closed-channel race.
	frames    chan *Packet
	done      chan struct{}
	closeOnce sync.Once
	closeErr  error
	// mediaStartSent means an IPCAM_START datagram was successfully written,
	// so shutdown owes the session a best-effort IPCAM_STOP. It is not proof
	// that the camera received START or that physical media became active.
	mediaStartSent atomic.Bool
	d2cPlainDump   *os.File
	d2cDumpMu      sync.Mutex
	c2dPlainDump   *os.File
	c2dDumpMu      sync.Mutex
	audio          bool
	quality        string
	strict         bool
	verbose        bool
	traceACK       bool
	traceFrag      bool
	traceFrameInfo bool
	tracePackets   bool
	keyAsm         channelAsm // key/IDR-family assembly state for marker 0x05
	interAsm       channelAsm // inter-frame assembly state for marker 0x07
	gopPoisoned    bool       // strict mode only: a fragment was lost in this GOP — drop P-frames until next clean IDR

	stats         counters
	prevStats     countersSnapshot
	havePrevStats bool
	lastStatsAt   time.Time

	// forceDrain stall tracking.  If avHighExt hasn't advanced for
	// forceDrainStallTicks consecutive force-drain ticks and the
	// buffer is non-empty, we flush regardless of the high-water
	// threshold — recovers from a stalled camera leaving partial AUs
	// stranded forever.
	stallTicks  uint8
	lastHighExt uint64

	// Compact runtime diagnostics. These are processor-goroutine owned.
	frameInfoSeen       bool
	frameInfoCodec      uint16
	frameInfoFlag       byte
	frameInfoOnlineNum  byte
	ackCurrStallWarned  bool
	stallStatsActive    bool
	stallStatsRepeat    uint64
	stallControlPkts    uint64
	stallStatsWatermark uint64
	stallStatsHigh      uint64
	stallStatsCurrent   uint64
	stallStatsPending   uint64
}

var errLoginResponseTimeout = errors.New("petlibro: LOGIN_RESP timeout")

// counters holds running totals for a stream-health summary. Atomic
// fields because the reader goroutine writes bytesIn/pktsIn/
// recvTimeouts/readerDrops while the processor writes every other
// field — plain uint64 access would trip `go test -race`.
type counters struct {
	bytesIn      atomic.Uint64 // bytes read from the UDP socket (encrypted)
	pktsIn       atomic.Uint64 // UDP datagrams successfully read
	keyFrags     atomic.Uint64 // fragments in the key/IDR family (ch=0x05)
	interFrags   atomic.Uint64 // fragments in the inter-frame family (ch=0x07)
	audioFrags   atomic.Uint64 // fragments on the audio channel (ch=0x03)
	otherFrags   atomic.Uint64 // anything else (control, etc.)
	vidFrags     atomic.Uint64 // video fragments (ch=0x05 + ch=0x07) reaching emit
	vidFramesIn  atomic.Uint64 // distinct frame_num values seen on video channel
	vidFramesOut atomic.Uint64 // AUs successfully queued to consumers
	vidDropped   atomic.Uint64 // AUs discarded because a frag_idx gap was detected
	fragSkips    atomic.Uint64 // count of frag_idx-skip events (fragments lost on the wire)
	fragsLost    atomic.Uint64 // total fragments lost (sum of frag_idx gap sizes)
	forceDrains  atomic.Uint64 // times forceDrain ran with buffered packets to flush
	recvTimeouts atomic.Uint64 // SetReadDeadline-triggered timeouts (no UDP data)
	readerDrops  atomic.Uint64 // raw UDP packets dropped because the processor queue was full
	emitDrops    atomic.Uint64 // assembled AUs dropped because the consumer queue was full

	// Reason-specific evidence counters. These intentionally coexist
	// with the aggregate fragSkips/vidDropped counters above so live
	// stats retain their historical totals while identifying the
	// decision that produced each gap or drop.
	fragIdxGap                 atomic.Uint64
	frameNumJumpKey            atomic.Uint64
	frameNumJumpInter          atomic.Uint64
	expectedDataShortfall      atomic.Uint64
	zeroDataHardDrop           atomic.Uint64
	strictIDRDrop              atomic.Uint64
	strictPDrop                atomic.Uint64
	forceDrainFlush            atomic.Uint64
	forceDrainEntries          atomic.Uint64
	deferredDrop               atomic.Uint64 // AV datagrams arriving after avNextExt already advanced
	framesWithLoss             atomic.Uint64
	idrFramesWithLoss          atomic.Uint64
	pFramesWithLoss            atomic.Uint64
	missingFragmentsTotal      atomic.Uint64
	maxMissingFragmentsInFrame atomic.Uint64

	// Media-header diagnostics distinguish the original 36-byte layout
	// from the 44-byte extended layout observed in PLAF203 captures. The
	// sequence counters make it visible when a packet carried a usable AV
	// sequence but could not be handed to the assembler.
	normalMediaPackets       atomic.Uint64
	extendedMediaCandidates  atomic.Uint64
	extendedMediaParsed      atomic.Uint64
	extendedMediaRejected    atomic.Uint64
	extendedMediaDataPackets atomic.Uint64
	extendedMediaEndPackets  atomic.Uint64
	extendedMediaRarePackets atomic.Uint64
	unknown0c08Remaining     atomic.Uint64
	unknown0c0dRemaining     atomic.Uint64
	sequenceSeenButUnhandled atomic.Uint64
	sequenceSeenAndAssembled atomic.Uint64

	frameInfoChanges    atomic.Uint64
	frameInfoUnexpected atomic.Uint64
	frameInfoCodec      atomic.Uint64
	frameInfoFlag       atomic.Uint64
	frameInfoOnlineNum  atomic.Uint64

	transportACKReceived   atomic.Uint64
	timingProbesReceived   atomic.Uint64
	timingResponsesSent    atomic.Uint64
	timingFeedbackReceived atomic.Uint64
	reliableRecvACK        atomic.Uint64
	ackNACKCount           atomic.Uint64
	ackSendCount           atomic.Uint64

	ackWatermark        atomic.Uint64 // current extended contiguous receive watermark (gauge)
	ackSeenPending      atomic.Uint64 // received entries above a gap (gauge)
	ackSeenRanges       atomic.Uint64 // disjoint receive ranges retained above the watermark (gauge)
	ackTrackingOverflow atomic.Uint64 // unique positions omitted after the range cap is reached
	ackDuplicateOrOld   atomic.Uint64 // duplicate or already-contiguously-ACKed AV packets
	ackAdvanced         atomic.Uint64 // sequence positions by which the receive watermark advanced
	ackHigh             atomic.Uint64 // highest extended AV sequence observed (gauge)
	ackSent             atomic.Uint64 // maintenance ACK datagrams successfully sent
	ackPrevLow16        atomic.Uint64 // previous/lower field in the latest maintenance ACK (gauge)
	ackCurrentLow16     atomic.Uint64 // current/upper field in the latest maintenance ACK (gauge)
}

// countersSnapshot is a plain-value snapshot of counters for diff
// reporting in dumpStats. atomic.Uint64 cannot be copied.
type countersSnapshot struct {
	bytesIn      uint64
	pktsIn       uint64
	keyFrags     uint64
	interFrags   uint64
	audioFrags   uint64
	otherFrags   uint64
	vidFrags     uint64
	vidFramesIn  uint64
	vidFramesOut uint64
	vidDropped   uint64
	fragSkips    uint64
	fragsLost    uint64
	forceDrains  uint64
	recvTimeouts uint64
	readerDrops  uint64
	emitDrops    uint64

	fragIdxGap                 uint64
	frameNumJumpKey            uint64
	frameNumJumpInter          uint64
	expectedDataShortfall      uint64
	zeroDataHardDrop           uint64
	strictIDRDrop              uint64
	strictPDrop                uint64
	forceDrainFlush            uint64
	forceDrainEntries          uint64
	deferredDrop               uint64
	framesWithLoss             uint64
	idrFramesWithLoss          uint64
	pFramesWithLoss            uint64
	missingFragmentsTotal      uint64
	maxMissingFragmentsInFrame uint64

	normalMediaPackets       uint64
	extendedMediaCandidates  uint64
	extendedMediaParsed      uint64
	extendedMediaRejected    uint64
	extendedMediaDataPackets uint64
	extendedMediaEndPackets  uint64
	extendedMediaRarePackets uint64
	unknown0c08Remaining     uint64
	unknown0c0dRemaining     uint64
	sequenceSeenButUnhandled uint64
	sequenceSeenAndAssembled uint64

	frameInfoChanges    uint64
	frameInfoUnexpected uint64
	frameInfoCodec      uint64
	frameInfoFlag       uint64
	frameInfoOnlineNum  uint64

	transportACKReceived   uint64
	timingProbesReceived   uint64
	timingResponsesSent    uint64
	timingFeedbackReceived uint64
	reliableRecvACK        uint64
	ackNACKCount           uint64
	ackSendCount           uint64

	ackWatermark        uint64
	ackSeenPending      uint64
	ackSeenRanges       uint64
	ackTrackingOverflow uint64
	ackDuplicateOrOld   uint64
	ackAdvanced         uint64
	ackHigh             uint64
	ackSent             uint64
	ackPrevLow16        uint64
	ackCurrentLow16     uint64
}

func (c *counters) snapshot() countersSnapshot {
	return countersSnapshot{
		bytesIn:      c.bytesIn.Load(),
		pktsIn:       c.pktsIn.Load(),
		keyFrags:     c.keyFrags.Load(),
		interFrags:   c.interFrags.Load(),
		audioFrags:   c.audioFrags.Load(),
		otherFrags:   c.otherFrags.Load(),
		vidFrags:     c.vidFrags.Load(),
		vidFramesIn:  c.vidFramesIn.Load(),
		vidFramesOut: c.vidFramesOut.Load(),
		vidDropped:   c.vidDropped.Load(),
		fragSkips:    c.fragSkips.Load(),
		fragsLost:    c.fragsLost.Load(),
		forceDrains:  c.forceDrains.Load(),
		recvTimeouts: c.recvTimeouts.Load(),
		readerDrops:  c.readerDrops.Load(),
		emitDrops:    c.emitDrops.Load(),

		fragIdxGap:                 c.fragIdxGap.Load(),
		frameNumJumpKey:            c.frameNumJumpKey.Load(),
		frameNumJumpInter:          c.frameNumJumpInter.Load(),
		expectedDataShortfall:      c.expectedDataShortfall.Load(),
		zeroDataHardDrop:           c.zeroDataHardDrop.Load(),
		strictIDRDrop:              c.strictIDRDrop.Load(),
		strictPDrop:                c.strictPDrop.Load(),
		forceDrainFlush:            c.forceDrainFlush.Load(),
		forceDrainEntries:          c.forceDrainEntries.Load(),
		deferredDrop:               c.deferredDrop.Load(),
		framesWithLoss:             c.framesWithLoss.Load(),
		idrFramesWithLoss:          c.idrFramesWithLoss.Load(),
		pFramesWithLoss:            c.pFramesWithLoss.Load(),
		missingFragmentsTotal:      c.missingFragmentsTotal.Load(),
		maxMissingFragmentsInFrame: c.maxMissingFragmentsInFrame.Load(),

		normalMediaPackets:       c.normalMediaPackets.Load(),
		extendedMediaCandidates:  c.extendedMediaCandidates.Load(),
		extendedMediaParsed:      c.extendedMediaParsed.Load(),
		extendedMediaRejected:    c.extendedMediaRejected.Load(),
		extendedMediaDataPackets: c.extendedMediaDataPackets.Load(),
		extendedMediaEndPackets:  c.extendedMediaEndPackets.Load(),
		extendedMediaRarePackets: c.extendedMediaRarePackets.Load(),
		unknown0c08Remaining:     c.unknown0c08Remaining.Load(),
		unknown0c0dRemaining:     c.unknown0c0dRemaining.Load(),
		sequenceSeenButUnhandled: c.sequenceSeenButUnhandled.Load(),
		sequenceSeenAndAssembled: c.sequenceSeenAndAssembled.Load(),

		frameInfoChanges:    c.frameInfoChanges.Load(),
		frameInfoUnexpected: c.frameInfoUnexpected.Load(),
		frameInfoCodec:      c.frameInfoCodec.Load(),
		frameInfoFlag:       c.frameInfoFlag.Load(),
		frameInfoOnlineNum:  c.frameInfoOnlineNum.Load(),

		transportACKReceived:   c.transportACKReceived.Load(),
		timingProbesReceived:   c.timingProbesReceived.Load(),
		timingResponsesSent:    c.timingResponsesSent.Load(),
		timingFeedbackReceived: c.timingFeedbackReceived.Load(),
		reliableRecvACK:        c.reliableRecvACK.Load(),
		ackNACKCount:           c.ackNACKCount.Load(),
		ackSendCount:           c.ackSendCount.Load(),

		ackWatermark:        c.ackWatermark.Load(),
		ackSeenPending:      c.ackSeenPending.Load(),
		ackSeenRanges:       c.ackSeenRanges.Load(),
		ackTrackingOverflow: c.ackTrackingOverflow.Load(),
		ackDuplicateOrOld:   c.ackDuplicateOrOld.Load(),
		ackAdvanced:         c.ackAdvanced.Load(),
		ackHigh:             c.ackHigh.Load(),
		ackSent:             c.ackSent.Load(),
		ackPrevLow16:        c.ackPrevLow16.Load(),
		ackCurrentLow16:     c.ackCurrentLow16.Load(),
	}
}

// dialTransport parses Petlibro addressing/diagnostics, opens a UDP socket, optionally
// discovers the camera address by UID, runs LAN_SEARCH3 + KNOCK2 +
// LOGIN A/B + the Petlibro bootstrap, then starts the receive
// worker. Returns once IPCAM_START has been sent and the AV-ready ack is
// acknowledged. The camera adapter supplies requested quality and audio state.
//
// URL shape:
//
//	petlibro://<host>?uid=<UID>[&strict=1][&verbose=1]
//	petlibro://?uid=<UID>[&subnet=192.168.1.0/24][&strict=1][&verbose=1]
//
//	strict=1 — drop any IDR with a fragment loss and poison the GOP
//	           (pristine pixels at the cost of multi-second freezes
//	           on lossy networks).  Default is to emit gapped IDRs
//	           with localised macroblock artefacts and drop gapped
//	           P-frames (avoids cascading inter-frame errors).
//
//	dump_plain and dump_d2c_plain record decrypted device-to-client packets.
//	dump_c2d_plain records timestamped client-to-device inner bodies.
//
// petlibro divergence vs pkg/tutk: pkg/tutk's Dial
// (pkg/tutk/conn.go:12) takes (host, uid, username, password)
// positionally and goes through Nebula/relay if the port is 10001 —
// neither applies here. Petlibro is LAN-only; dialTransport parses connection
// and diagnostic options while camera.go supplies normalized media intent.
func dialTransport(rawURL, quality string, audio bool, correlation cameraCorrelation) (*Client, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, fmt.Errorf("petlibro: bad url: %w", err)
	}
	q := u.Query()
	uid := q.Get("uid")
	if uid == "" {
		return nil, fmt.Errorf("petlibro: uid query parameter required")
	}
	// 20-character TUTK UID — anything else is a user typo, and
	// silently truncating / zero-padding it through buildLANSearch3
	// just makes the camera not respond, leaving the user to debug a
	// LOGIN_RESP timeout with no hint.  Fail early with a clear msg.
	if len(uid) != 20 {
		return nil, fmt.Errorf("petlibro: uid must be 20 chars (got %d)", len(uid))
	}
	var cam *net.UDPAddr
	if host := u.Host; host != "" {
		if _, _, err := net.SplitHostPort(host); err != nil {
			host = net.JoinHostPort(host, strconv.Itoa(lanPort))
		}
		cam, err = net.ResolveUDPAddr("udp", host)
		if err != nil {
			return nil, fmt.Errorf("petlibro: resolve %s: %w", host, err)
		}
	}
	udp, err := net.ListenUDP("udp4", nil)
	if err != nil {
		return nil, fmt.Errorf("petlibro: bind: %w", err)
	}
	// HD video bursts > 3 Mbps; the default 768 KiB recv buffer is
	// only ~2 seconds of headroom and easily overflowed by a slow
	// drain.  Ask for 4 MiB — the kernel will clamp if it can't.
	const wantBuf = 4 * 1024 * 1024
	_ = udp.SetReadBuffer(wantBuf)
	verbose := boolQuery(q, "verbose")
	// Verify what the kernel actually granted us — SetReadBuffer
	// silently clamps to net.core.rmem_max and the symptom is
	// readerDrops climbing with no other signal.  Warn
	// unconditionally on clamp so users notice the kernel limit.
	if sc, err := udp.SyscallConn(); err == nil {
		var actualBuf int
		_ = sc.Control(func(fd uintptr) {
			actualBuf, _ = syscall.GetsockoptInt(int(fd),
				syscall.SOL_SOCKET, syscall.SO_RCVBUF)
		})
		// Linux SO_RCVBUF reports double the granted size (man 7
		// socket); halve before comparing.
		granted := actualBuf
		if granted >= 2*wantBuf {
			granted /= 2
		}
		if granted < wantBuf {
			log.Warn().Msgf("SO_RCVBUF clamped: requested=%d granted=%d (consider raising net.core.rmem_max)", wantBuf, granted)
		} else if verbose {
			log.Debug().Msgf("SO_RCVBUF requested=%d granted=%d", wantBuf, granted)
		}
	}

	nonce := make([]byte, 8)
	if _, err := rand.Read(nonce); err != nil {
		_ = udp.Close()
		return nil, err
	}
	if cam == nil {
		cam, err = discoverByUID(udp, uid, nonce, q["subnet"], defaultDiscoveryTimeout, verbose)
		if err != nil {
			_ = udp.Close()
			return nil, err
		}
	}
	d2cDumpPath := q.Get("dump_d2c_plain")
	if d2cDumpPath == "" {
		d2cDumpPath = q.Get("dump_plain")
	}
	var d2cPlainDump *os.File
	if d2cDumpPath != "" {
		d2cPlainDump, err = os.Create(d2cDumpPath)
		if err != nil {
			_ = udp.Close()
			return nil, fmt.Errorf("petlibro: create D2C plaintext dump %q: %w", d2cDumpPath, err)
		}
	}
	var c2dPlainDump *os.File
	if path := q.Get("dump_c2d_plain"); path != "" {
		c2dPlainDump, err = os.Create(path)
		if err != nil {
			if d2cPlainDump != nil {
				_ = d2cPlainDump.Close()
			}
			_ = udp.Close()
			return nil, fmt.Errorf("petlibro: create C2D plaintext dump %q: %w", path, err)
		}
	}
	c := &Client{
		sessionID:      newSessionID(),
		adapterID:      correlation.adapterID,
		producerID:     correlation.producerID,
		startupAttempt: correlation.startupAttempt,
		connectedAt:    time.Now(),
		conn:           udp,
		cam:            cam,
		uid:            uid,
		nonce:          nonce,
		kseq:           2,
		audio:          audio,
		quality:        quality,
		strict:         boolQuery(q, "strict"),
		verbose:        verbose,
		traceACK:       boolQuery(q, "trace_ack"),
		traceFrag:      boolQuery(q, "trace_frag"),
		traceFrameInfo: boolQuery(q, "trace_frameinfo"),
		tracePackets:   boolQuery(q, "trace_packets"),
		frames:         make(chan *Packet, 1024),
		done:           make(chan struct{}),
		d2cPlainDump:   d2cPlainDump,
		c2dPlainDump:   c2dPlainDump,
	}
	log.Debug().Str("physical_session_id", c.sessionID).
		Str("camera_adapter_id", c.adapterID).Uint32("producer_id", c.producerID).
		Int("startup_attempt", c.startupAttempt).Str("quality", c.quality).
		Bool("audio", c.audio).Msg("petlibro transport connection started")
	if c.verbose {
		log.Debug().Str("physical_session_id", c.sessionID).
			Bool("strict", c.strict).Bool("trace_ack", c.traceACK).
			Bool("trace_frag", c.traceFrag).Bool("trace_frameinfo", c.traceFrameInfo).
			Bool("trace_packets", c.tracePackets).Msg("petlibro transport diagnostics configured")
	}
	log.Debug().Str("physical_session_id", c.sessionID).Msg("petlibro handshake started")
	if err := c.handshake(); err != nil {
		log.Debug().Str("physical_session_id", c.sessionID).Err(err).Msg("petlibro handshake failed")
		clearDiscoveryCache(uid, q["subnet"])
		_ = c.Close()
		return nil, err
	}
	log.Debug().Str("physical_session_id", c.sessionID).
		Dur("elapsed", time.Since(c.connectedAt)).Msg("petlibro handshake completed")
	log.Debug().Str("physical_session_id", c.sessionID).Msg("petlibro AV bootstrap started")
	if err := c.bootstrap(); err != nil {
		log.Debug().Str("physical_session_id", c.sessionID).Err(err).Msg("petlibro AV bootstrap failed")
		clearDiscoveryCache(uid, q["subnet"])
		_ = c.Close()
		return nil, err
	}
	c.established.Store(true)
	log.Debug().Str("physical_session_id", c.sessionID).
		Dur("elapsed", time.Since(c.connectedAt)).Msg("petlibro AV bootstrap completed")
	log.Info().Str("physical_session_id", c.sessionID).
		Str("camera_adapter_id", c.adapterID).Uint32("producer_id", c.producerID).
		Str("quality", c.quality).Bool("audio", c.audio).
		Msg("petlibro physical viewing session established")

	go c.recvLoop()
	go c.maintenanceLoop()
	return c, nil
}

// boolQuery parses a URL query bool using strconv.ParseBool's
// canonical set (1/0/true/false/...).  Empty value returns false.
// Stand-in for the four `q.Get("x") == "true" || q.Get("x") == "1"`
// chains that used to live in NewProducer.
func boolQuery(q url.Values, key string) bool {
	v := q.Get(key)
	if v == "" {
		return false
	}
	b, _ := strconv.ParseBool(v)
	return b
}

// --- I/O helpers (use pkg/tutk for the luffy crypto) ----------------------

// send writes a single Kalay datagram to the camera. Byte count is
// discarded: all packets we generate fit comfortably under MTU (the
// largest is the 572-byte LOGIN_1 packet), so a short write would
// only happen on a closed/broken socket — and the returned error
// already signals that.
//
// petlibro divergence vs pkg/tutk: pkg/tutk wraps the same primitive
// at pkg/tutk/conn.go:91 (Conn.Write), but uses tutk.TransCodePartial
// as the encrypt direction — identical here because the Luffy primitive
// is symmetric across protocol variants.  The only thing that differs
// is the inverted naming convention in pkg/tutk (TransCodePartial is
// the encrypt direction in tutk too — see crypto.go:81); we point this
// out at every call site for the inevitable consolidation pass.
func (c *Client) send(p []byte) error {
	_, err := c.conn.WriteToUDP(tutk.TransCodePartial(nil, p), c.cam)
	return err
}

func (c *Client) dumpC2DInner(body []byte) {
	if c.verbose && c.tracePackets {
		event := log.Trace().Str("physical_session_id", c.sessionID).Int("plain_length", len(body))
		if len(body) >= 2 {
			event = event.Uint16("inner_type", binary.LittleEndian.Uint16(body))
		}
		event.Msg("petlibro C2D packet metadata")
	}
	if c.c2dPlainDump != nil {
		record := make([]byte, 12+len(body))
		binary.LittleEndian.PutUint64(record, uint64(time.Now().UnixNano()))
		binary.LittleEndian.PutUint32(record[8:], uint32(len(body)))
		copy(record[12:], body)

		c.c2dDumpMu.Lock()
		n, err := c.c2dPlainDump.Write(record)
		c.c2dDumpMu.Unlock()
		if err == nil && n != len(record) {
			err = io.ErrShortWrite
		}
		if err != nil {
			log.Warn().Err(err).Msg("petlibro: write C2D plaintext inner-body dump")
		}
	}
}

func (c *Client) sendInner(body []byte) error {
	c.txMu.Lock()
	defer c.txMu.Unlock()
	c.dumpC2DInner(body)
	out := buildOuter(c.nonce, c.kseq, body, 0x00, 0x00, flagsSession)
	c.kseq = (c.kseq + 1) & 0xFFFF
	return c.send(out)
}

func (c *Client) recvOne(timeout time.Duration) ([]byte, error) {
	if timeout > 0 {
		_ = c.conn.SetReadDeadline(time.Now().Add(timeout))
	}
	buf := make([]byte, 65535)
	n, addr, err := c.conn.ReadFromUDP(buf)
	if err != nil {
		return nil, err
	}
	if c.cam.Port != addr.Port && addr.IP.Equal(c.cam.IP) {
		c.cam.Port = addr.Port
	}
	// petlibro divergence vs pkg/tutk: tutk's Conn.Read decrypts via
	// ReverseTransCodePartial at pkg/tutk/conn.go:84 — same direction,
	// same primitive.  Only difference is petlibro uses this synchronous
	// helper exclusively during handshake/bootstrap; post-bootstrap
	// reads go through the readerGoroutine/processor split in recv.go.
	return tutk.ReverseTransCodePartial(nil, buf[:n]), nil
}

// Close makes a best-effort request to stop media at the camera and then shuts
// the session down. A successful UDP write does not confirm physical shutdown.
// done is the sole closure signal; frames is deliberately not closed because
// UDP processor goroutines aren't joined and a select could otherwise choose
// a concurrent send after done became ready. Investigated
// pkg/wyze (closeMu+bool), pkg/tapo / pkg/dvrip (bare conn.Close()),
// and pkg/onvif (no Close).  None of those use a done channel today,
// but for a multi-goroutine UDP client this is the simpler equivalent of
// wyze's pattern.
func (c *Client) Close() error {
	c.closeOnce.Do(func() {
		started := c.established.Load()
		log.Debug().Str("physical_session_id", c.sessionID).
			Bool("established", started).Msg("petlibro transport shutdown started")
		if c.done != nil {
			close(c.done)
		}
		stopAttempted := false
		stopWritten := false
		if c.mediaStartSent.Swap(false) {
			stopAttempted = true
			c.innerMu.Lock()
			body := innerData(c.icounter, 0x7000, 0, ioctlBody12(ioctlStop))
			c.icounter++
			c.closeErr = c.sendInner(body)
			c.innerMu.Unlock()
			stopWritten = c.closeErr == nil
			if c.closeErr != nil {
				log.Warn().Str("physical_session_id", c.sessionID).Err(c.closeErr).
					Msg("petlibro IPCAM_STOP datagram write failed")
			} else {
				log.Debug().Str("physical_session_id", c.sessionID).
					Msg("petlibro IPCAM_STOP datagram written")
			}
		}
		if c.conn != nil {
			_ = c.conn.Close()
			log.Trace().Str("physical_session_id", c.sessionID).Msg("petlibro UDP socket closed")
		}
		c.d2cDumpMu.Lock()
		if c.d2cPlainDump != nil {
			_ = c.d2cPlainDump.Close()
		}
		c.d2cDumpMu.Unlock()
		c.c2dDumpMu.Lock()
		if c.c2dPlainDump != nil {
			_ = c.c2dPlainDump.Close()
		}
		c.c2dDumpMu.Unlock()
		event := log.Debug()
		if started {
			event = log.Info()
		}
		event.Str("physical_session_id", c.sessionID).
			Str("camera_adapter_id", c.adapterID).Uint32("producer_id", c.producerID).
			Bool("stop_attempted", stopAttempted).Bool("stop_datagram_written", stopWritten).
			Dur("uptime", time.Since(c.connectedAt)).
			Msg("petlibro physical viewing session closed")
	})
	return c.closeErr
}

func (c *Client) RemoteAddr() net.Addr             { return c.cam }
func (c *Client) Protocol() string                 { return "petlibro+udp" }
func (c *Client) SessionID() string                { return c.sessionID }
func (c *Client) packets() <-chan *Packet          { return c.frames }
func (c *Client) doneSignal() <-chan struct{}      { return c.done }
func (c *Client) healthSnapshot() countersSnapshot { return c.stats.snapshot() }
