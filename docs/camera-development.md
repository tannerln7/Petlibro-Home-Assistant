# Development guide

This guide covers the Petlibro-specific parts of the go2rtc fork. For general
go2rtc architecture and APIs, use the existing module documentation under
[`internal/`](../addon/go2rtc/internal/README.md) and package documentation under
[`pkg/`](../addon/go2rtc/pkg/README.md).

## Toolchain

- Go 1.24 or newer, as declared by [`go.mod`](../addon/go2rtc/go.mod)
- Git
- FFmpeg/ffprobe for optional RTSP verification
- Docker only when validating the local container image

Run the Go commands below from `addon/go2rtc`.

## Source layout

| Path | Responsibility |
| --- | --- |
| [`main.go`](../addon/go2rtc/main.go) | Registers the Petlibro module in the standalone binary |
| [`internal/petlibro/`](../addon/go2rtc/internal/petlibro/) | Connects the `petlibro://` URL handler to go2rtc logging and stream routing |
| [`pkg/petlibro/client.go`](../addon/go2rtc/pkg/petlibro/client.go) | Transport state, addressing/diagnostic URL parsing, socket setup, counters, and plaintext C2D writes |
| [`pkg/petlibro/camera.go`](../addon/go2rtc/pkg/petlibro/camera.go) | PLAF203 lifecycle, codec readiness, SPS epochs, startup retry, and timestamp normalization |
| [`pkg/petlibro/handshake.go`](../addon/go2rtc/pkg/petlibro/handshake.go) | LAN discovery session handshake and login |
| [`pkg/petlibro/bootstrap.go`](../addon/go2rtc/pkg/petlibro/bootstrap.go) | IOCtrl ordering, stream control, AV-ready state, and initial sequence cursors |
| [`pkg/petlibro/recv.go`](../addon/go2rtc/pkg/petlibro/recv.go) | Datagram receive loop, receive-side ACK tracking, maintenance ACKs, and stats |
| [`pkg/petlibro/assembler.go`](../addon/go2rtc/pkg/petlibro/assembler.go) | Media-header decoding, sequence reordering, frame assembly, loss accounting, and raw camera timestamps |
| [`pkg/petlibro/producer.go`](../addon/go2rtc/pkg/petlibro/producer.go) | Conventional conversion of normalized camera units to go2rtc media packets |
| [`pkg/petlibro/templates.go`](../addon/go2rtc/pkg/petlibro/templates.go) | Wire constants and packet builders |
| [`pkg/petlibro/*_test.go`](../addon/go2rtc/pkg/petlibro/) | Unit, regression, dump-summary, and offline replay tests |

The high-level receive path is:

```text
UDP datagram
  -> decrypt once
  -> classify normal or extended media header
  -> mark the wire sequence as received for ACK tracking
  -> reorder by extended sequence
  -> assemble fragments per media channel
  -> emit complete H.264/AAC access unit with raw timing
  -> normalize camera readiness and clocks
  -> convert to go2rtc media
```

## Protocol invariants

Preserve these boundaries when modifying the implementation:

- `ackWatermarkExt` represents packets actually received contiguously on the
  wire. It must not advance when the assembler skips a hole.
- `avNextExt` is the assembler output cursor. `forceDrain()` may advance it to
  preserve output liveness without changing the receive watermark.
- ACK tracking happens after a media packet's real `subWire` is decoded and
  before assembly or drop decisions.
- Received positions above an ACK hole are stored as consecutive ranges rather
  than one map entry per packet. The range count is capped; `overflow` in the
  TRACE transport diagnostics report positions omitted after that cap. The type-0x09
  upper endpoint is also bounded by NACK-list capacity, so an unrepresentable
  hole is never silently acknowledged.
- The receive loop decrypts each D2C datagram once. Plaintext capture records
  the same bytes passed to `parseDatagram`.
- Normal and extended media headers feed the same assembly path only after
  structural validation. Rejected candidates must not create false evidence of
  successful assembly.
- Key/IDR-family video (`0x05`), inter-frame video (`0x07`), and audio (`0x03`)
  keep independent frame-assembly state.
- The regular reorder drain cadence is 100 ms and the force-drain buffer
  threshold is 8 entries. Change either only as an isolated, measured
  experiment.
- `strict=1` changes damaged-GOP output policy; it must not change packet
  classification, receive tracking, or ACK semantics.
- The transport must not import go2rtc consumer lifecycle. The camera adapter
  must return an SPS-bearing keyframe followed by an unbroken live GOP, and
  media with forward modular RTP clock progression, including normal `uint32`
  rollover. Replaying a keyframe after preparation discarded later frames from
  that GOP does not satisfy this invariant.
- HD startup stabilization is a PLAF203 behavior, not a go2rtc probe option.
  Keep its policy in `camera.go`; do not expose transport timing as user intent.
- Once `IPCAM_START` has been transmitted, closing a camera session attempts
  `IPCAM_STOP` once before closing UDP. UDP transmission confirms neither that
  START was activated nor that STOP physically ended the camera session.

The AV ACK base/high interval and relative NACK list are established from the
AF203 resend dispatcher. The reserved field at +6, transport state at +16, and
some type-0x0b statistics remain only partially understood; keep their names
and zero/default behavior conservative.

Repository tests use constructed packets, fake camera transports, local UDP
sockets, and optional offline dumps; they do not prove that a physical feeder
acted on a datagram. Hardware observations are called out explicitly in these
guides. In particular, PLAF203 audio has been observed as 44.1 kHz AAC, but no
firmware guarantee for that rate is known; the adapter and published ADTS codec
therefore use native 1024-sample AAC clock steps instead of a 44.1 kHz formula.

## Media header layouts

Normal media packets expose their media fields in the 36-byte inner header.
The PLAF203 also emits an extended 44-byte layout in inner families `0c08`,
`0c09`, `0c0c`, and `0c0d`:

| Offset | Size | Current interpretation |
| --- | ---: | --- |
| `24` | 1 | Channel |
| `25` | 1 | Sub flag |
| `26` | 2 | Wire sequence (`subWire`, little-endian) |
| `28` | 2 | Total fragments |
| `30` | 2 | Fragment index |
| `32` | 2 | Payload length |
| `36` | 4 | Frame number |
| `40` | 4 | `nextFrameLike` (meaning not proven) |
| `44` | variable | Payload followed by optional frame-info bytes |

`nextFrameLike` remains an uncertainty marker. FRAMEINFO byte 4 is `onlineNum`
(the AF203 online AV-client count/state), not an HD/SD stream selector.

## Tests

Run the focused package suite during development:

```bash
go test ./pkg/petlibro -count=1
```

Use the race detector after concurrency, socket, ACK, dump, or lifecycle
changes:

```bash
go test -race ./pkg/petlibro -count=1
```

Useful focused test groups include:

```bash
go test ./pkg/petlibro -run 'TestExtendedMedia|TestEndToEnd|TestForceDrain' -count=1
go test ./pkg/petlibro -run 'TestACK|TestParseACK' -count=1
go test ./pkg/petlibro -run 'TestBootstrap|TestCamera|TestClientClose' -count=1
```

Dump-backed tests intentionally skip when their environment variable is unset,
so the normal package suite never requires a live camera or local capture.
See the [debugging guide](camera-debugging.md#offline-replay-and-summaries) for
their inputs.

## Build checks

After the final relevant edit:

```bash
go test ./pkg/petlibro -count=1
go test -race ./pkg/petlibro -count=1
go build -o ./go2rtc .
git diff --check
```

Run `gofmt` on changed Go files before these checks. The repository ignores the
root `go2rtc` binary, but remove it after validation to keep the worktree free
of build artifacts.

## Live test workflow

1. Reproduce the behavior from a fixed config and record all Petlibro query
   parameters.
2. Enable `verbose=1` for component diagnostics. Enable high-volume protocol
   flags separately and only for a bounded reproduction.
3. Capture D2C and C2D plaintext only when packet-level evidence is needed.
4. Run a bounded viewer test and save both go2rtc and viewer timestamps.
5. Replay the same D2C dump after each parser/assembler change.
6. Compare loss, media-header, and ACK counters rather than judging only by
   visual playback.
7. Remove or securely retain dumps outside the repository.

For HD tests, record both the selected startup SPS and any later codec-change
line. Repeated identical SPS units are intentionally TRACE-only.
The adapter waits up to 15 seconds when an HD request begins below 1920x1080;
the camera can transition later, so the first advertised resolution alone does
not prove that stream control failed. This bound preserves the former packaged
add-on default; direct `petlibro://` sources previously defaulted to no wait.
The bound limits startup latency and is a preference window, not a guarantee
that the camera has reached its requested resolution. If video arrived after
the last retained IDR, the adapter discards that stale candidate and waits up
to the normal five-second readiness timeout for a new SPS-bearing IDR. This can
add up to one GOP after the 15-second preference window, but avoids handing a
decoder an IDR followed by P-frames with missing references. Requested audio
discovery has its own readiness interval, so when both audio and video
resynchronization are pending the total post-stabilization delay can be longer.

## Adding configuration options

When adding a Petlibro URL parameter:

1. Decide whether it is user camera intent (`camera.go`) or a narrowly useful
   transport diagnostic (`Dial()`); do not expose protocol hypotheses.
2. Keep the default compatible unless the change is intentionally behavioral.
3. Add a focused parsing or behavior test.
4. Log the effective value at the appropriate component level when it affects
   protocol behavior; never include credentials, camera identifiers, or raw
   payloads.
5. Document user-facing options in `go2rtc.example.yaml` and stable behavior in
   the module reference.
6. Put experimental diagnostics in the debugging guide instead of expanding
   the root README.
