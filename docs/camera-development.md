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
| [`pkg/petlibro/client.go`](../addon/go2rtc/pkg/petlibro/client.go) | Client state, URL parsing, socket setup, counters, and plaintext C2D writes |
| [`pkg/petlibro/handshake.go`](../addon/go2rtc/pkg/petlibro/handshake.go) | LAN discovery session handshake and login |
| [`pkg/petlibro/bootstrap.go`](../addon/go2rtc/pkg/petlibro/bootstrap.go) | IOCtrl ordering, stream control, AV-ready state, and initial sequence cursors |
| [`pkg/petlibro/recv.go`](../addon/go2rtc/pkg/petlibro/recv.go) | Datagram receive loop, receive-side ACK tracking, maintenance ACKs, and stats |
| [`pkg/petlibro/assembler.go`](../addon/go2rtc/pkg/petlibro/assembler.go) | Media-header decoding, sequence reordering, frame assembly, loss accounting, and SPS logging |
| [`pkg/petlibro/producer.go`](../addon/go2rtc/pkg/petlibro/producer.go) | Codec probe, optional HD stabilization, and conversion to go2rtc media packets |
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
  -> emit H.264/AAC packet
  -> probe/convert to go2rtc media
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
  five-second ACK stats reports positions omitted after that cap. The type-0x09
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

The AV ACK base/high interval and relative NACK list are established from the
AF203 resend dispatcher. The reserved field at +6, transport state at +16, and
some type-0x0b statistics remain only partially understood; keep their names
and zero/default behavior conservative.

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
go test ./pkg/petlibro -run 'TestBootstrap|TestStreamCtrl|TestParseHDProbe' -count=1
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
2. Enable `verbose=1` before enabling high-volume trace flags.
3. Capture D2C and C2D plaintext only when packet-level evidence is needed.
4. Run a bounded viewer test and save both go2rtc and viewer timestamps.
5. Replay the same D2C dump after each parser/assembler change.
6. Compare loss, media-header, and ACK counters rather than judging only by
   visual playback.
7. Remove or securely retain dumps outside the repository.

For HD tests, record both the first SPS and any later `spsChange` line. The
camera can begin at 640x360 before moving to 1920x1080, so the first advertised
resolution alone does not prove that stream control failed.

## Adding configuration options

When adding a Petlibro URL parameter:

1. Parse and validate it in `Dial()` with a clear error for invalid input.
2. Keep the default compatible unless the change is intentionally behavioral.
3. Add a focused parsing or behavior test.
4. Log the effective value under `verbose=1` when it affects protocol behavior.
5. Document user-facing options in `go2rtc.example.yaml` and stable behavior in
   the module reference.
6. Put experimental diagnostics in the debugging guide instead of expanding
   the root README.
