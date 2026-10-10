# Petlibro debugging guide

Use this guide when a Petlibro stream fails to start, advertises an unexpected
resolution, stalls, or produces decoder errors. Start with compact health logs;
packet traces and plaintext dumps are intentionally opt-in because they are
high volume and may contain device or session data.

## Recommended diagnostic baseline

The following options are a useful PLAF203 HD baseline:

```yaml
log:
  level: info
  petlibro: debug
  streams: debug
  webrtc: debug

streams:
  petlibro_feeder: petlibro://192.168.1.42?uid=PLAF20300000000ABCD0&quality=hd&verbose=1
```

Replace the placeholder IP and UID. Use one viewer at a time while comparing
protocol changes so reconnects and concurrent consumers do not obscure the
timeline.

## Logging model and correlation

Petlibro camera logs use the existing go2rtc Zerolog pipeline and its central
secret filter. Levels describe the abstraction of an event:

- **INFO** records quiet, externally meaningful transitions such as an
  established physical viewing session, a stream becoming available, a
  runtime codec change, or a physical session closing.
- **DEBUG** records component behavior: transport/bootstrap phases, camera
  readiness, producer replacement, consumer attachment, and compact health.
- **TRACE** records implementation decisions such as ACK state, assembly,
  repeated SPS observations, and track forwarding. TRACE does not by itself
  enable decrypted payload or per-packet protocol logging.

Correlation fields identify different lifetimes rather than treating every
connection as the same object:

| Field | Meaning |
| --- | --- |
| `stream_id` | Process-local logical go2rtc stream |
| `stream_producer_id` | Producer slot owned by that stream |
| `producer_generation` | Connection/reconnect generation within the slot |
| `producer_id` | Concrete go2rtc producer instance |
| `camera_adapter_id` | PLAF203 adapter spanning its bounded startup attempts |
| `physical_session_id` | One physical Petlibro transport/login/bootstrap session |
| `consumer_id` | Concrete downstream go2rtc consumer, including WebRTC |
| `webrtc_connection_id` | WebRTC peer before its producer/consumer role is known |

All are opaque, process-local values. They do not contain the UID, camera
address, credentials, or session keys. During reconnect, go2rtc may prepare a
replacement producer and transfer tracks before stopping the previous
producer; the old/new IDs in DEBUG logs make this overlap explicit without
changing its ordering.

## Diagnostic URL options

### Compact logging

| Option | Default | Effect |
| --- | --- | --- |
| `verbose=1` | off | Enables component bootstrap, readiness, codec, and health diagnostics when the Petlibro module level permits DEBUG |
| `trace_ack=1` | off | Logs detailed maintenance ACK fields without payload bytes; requires `verbose=1` |
| `trace_frag=1` | off | Logs important fragment/frame decisions; requires `verbose=1` |
| `trace_frameinfo=1` | off | Logs frame-info observations and changes; requires `verbose=1` |
| `trace_packets=1` | off | Logs packet metadata and accepted/rejected extended-media candidates, not decrypted payload bytes; requires `verbose=1` |

Enable traces one at a time unless full wire correlation is necessary.
Packaged add-on users can set `camera_protocol_tracing: true` alongside
`log_level: trace` to apply all four metadata trace flags to generated Petlibro
sources for a bounded reproduction. The switch is independent:
`log_level: trace` alone keeps the deep protocol flags disabled, and the switch
alone does not lower the logging threshold. Plaintext dump files remain a
separate option.

### Camera controls

| Option | Default | Effect |
| --- | --- | --- |
| `quality=hd` or `quality=sd` | `hd` | Selects the requested stream and builds the corresponding stream-control command |
| `audio=true` | `false` | Requests AAC audio after video start |
| `strict=1` | off | Drops damaged IDRs and dependent P-frames until a clean GOP begins; may freeze on genuine loss |

The Petlibro stream-control body is capture-backed. The zero-valued data-delay
control follows the public TUTK AVAPI sequence and was the packaged add-on's
validated default; the repository does not claim that every firmware requires
it. Both are fixed in the supported production profile rather than exposed as
user policy. HD startup stabilization is camera-adapter behavior. Their former
URL switches were reverse-engineering experiments and no longer affect runtime
policy; see the
[quarantine note](research/camera-runtime-quarantine.md).

### Transport ACK diagnostics

The client has one protocol implementation rather than selectable ACK modes.
Type `0x09` reports the highest contiguous AV sequence as its base, the highest
advertised receive position as its upper endpoint, and every unresolved packet
between them as a relative NACK offset. The upper endpoint is reduced when the
bounded NACK list cannot describe all holes, so an omitted hole is never
silently acknowledged. Reliable IOCtrl messages use the separate cumulative
ACK field recovered from AF203 firmware.

With `trace_ack=1`, logs include the extended base/high positions, relative
NACK offsets, reliable receive ACK, primary ordinal, secondary send counter,
and low-16 timing value. Normal debug summaries report these as aggregate
health values without logging every packet.

## Reading periodic health logs

At DEBUG, periodic `transport health` and `camera health` events provide a
compact view of rates, queue pressure, loss, recovery, current resolution, and
progress through the server-side media path:

| Field | Interpretation |
| --- | --- |
| `packets_per_second` / `video_frames_per_second` | Transport input and complete assembled-video rates |
| `adapter_video_units` | Video access units handed out by the camera adapter |
| `producer_video_units` | Video access units successfully written to a matched go2rtc receiver track |
| `reader_queue_drops` / `output_queue_drops` | Local receive or assembled-output queue overflow |
| `missing_fragments` / `dropped_frames` | Transport loss inferred during assembly |
| `ack_pending` / `ack_nacks` | Current receive backlog and recovery requests |
| `media_stalled` | No media observed during the health interval |

The complete ACK, packet-family, media-header, and assembler counter set remains
available in the TRACE `transport diagnostics` event. Its fields include:

| Field | Interpretation |
| --- | --- |
| `video: frames in -> out` | Frames reaching the assembler and complete access units emitted to the adapter queue |
| `qDrops reader/emit` | Local userspace receive or output-channel overflow; both should normally be zero |
| `loss: frames/idr/p/missing` | Frames with fragment loss and the total missing fragments inferred by assembly |
| `fragIdxGap` | Fragment indices skipped within a frame |
| `expectedDataShortfall` | End fragment arrived before all expected data fragments |
| `zeroDataHardDrop` | A multi-fragment frame ended with no usable data fragments |
| `strictIDRDrop/strictPDrop` | Frames suppressed by strict GOP policy |
| `deferredDrop` | Packet arrived after the assembler output cursor had already passed it |
| `extendedMedia parsed/rejected` | Alternate 44-byte media-header candidates accepted or rejected |
| `unknown0c08/unknown0c0d` | Remaining unparsed members of the common extended-media families |
| `seqAssembled/seqUnhandled` | Recognized wire sequences delivered to assembly or seen but not handled |
| `ack watermark/high/pending/nacks` | Highest contiguous receive sequence, highest observed sequence, unresolved received positions, and holes advertised for retransmission |
| `reliable/sendCount` | Separate cumulative reliable-control ACK and monotonic type-0x09 send counter |
| `probeRx/responseTx` | Camera timing probes received and type-0x0b responses sent |
| `ack ranges/overflow` | Compressed disjoint receive ranges retained above a hole, and positions omitted if the fixed range cap is exhausted |
| `ack base/highWire` | The low-16-bit AV base and upper endpoint most recently sent |

Healthy live behavior is not defined by one number, but these are useful signs:

- `readerDrops=0` and `emitDrops=0`
- extended-media candidates are parsed rather than left as unknown `0c08` or
  `0c0d`
- `missingFragmentsTotal`, gapped IDRs, and decoder errors remain near zero
- ACK watermark follows high-water with little or no pending backlog
- the selected startup configuration and any actual SPS configuration change
  are logged; recurring identical SPS units are TRACE-only

Normal logs pass through one centralized sanitizer. Registered endpoint values
such as host, UID, serial-like path tail, credentials, and diagnostic paths are
removed while generic URL shape, query names, and non-identifying options are
retained. Secret-shaped structured fields are also redacted as a safety net.
This is best-effort for identifiers, so logs still require review before
sharing. Authentication keys, passwords, tokens, and credentials must never be
added intentionally to an event.

Plaintext dump files deliberately bypass the logger and can contain identifiers,
authentication/session material, and media. Handle them as sensitive artifacts.

## Normal lifecycle ordering

A successful new source normally reports these boundaries in order:

1. go2rtc producer dial and Petlibro producer construction;
2. a physical session connection, handshake, and AV bootstrap;
3. physical viewing session established;
4. adapter readiness, startup codec/IDR selection, and stream available;
5. producer media description and forwarding start;
6. downstream consumer attachment and WebRTC state transitions.

Shutdown proceeds from consumer detachment to producer/adapter stop, transport
shutdown, an IPCAM_STOP write attempt when START was sent, and UDP closure.
`stop_datagram_written=true` proves only that the local UDP write succeeded. It
does not prove camera receipt or physical deactivation. A reconnect can show
old and replacement physical sessions concurrently because the stream manager
builds and transfers a replacement before stopping the previous producer.

A repeating `transport health` event with `media_stalled=true` and
control-only packets means the session is alive but the camera is no longer
sending media. Inspect the TRACE transport diagnostics before changing
assembly policy.

## Common failure modes

### Login timeout

`petlibro: LOGIN_RESP timeout` means the camera did not acknowledge the login
pair. Check, in order:

1. The camera completed provisioning in the Petlibro app and remains online.
2. The 20-character UID is exact.
3. A fixed camera IP is current, or discovery traffic can reach the camera's
   broadcast domain.
4. UDP port `32761` is not blocked between go2rtc and the camera.

### RTSP returns 404

A `404 Not Found` on the configured RTSP name commonly means the Petlibro
producer failed during startup or codec probe, so go2rtc could not expose a
usable stream. Inspect the go2rtc log before the RTSP request for:

- bootstrap or IOCtrl failure
- camera readiness timeout or EOF
- absence of an SPS-bearing IDR
- repeated camera startup retries

### HD request advertises 640x360

The camera can begin with a 640x360 SPS and switch to 1920x1080 several seconds
later. Enable Petlibro debug logging and look for the selected startup
configuration or `camera codec configuration changed`. The
adapter waits up to 15 seconds for this transition before publishing the
initial track. A later transition is still forwarded in-band, but the original
go2rtc codec/SDP remains based on the startup SPS. In-band SPS is sufficient
for some consumers, but the adapter does not currently renegotiate a changed
profile, level, or resolution.

### Corrupt H.264 or concealment warnings

Corruption plus nonzero fragment-loss counters indicates an incomplete access
unit, not necessarily a viewer problem. Check extended-media reject counters
and sequence backlog first. `strict=1` can suppress damaged GOPs but is a
presentation policy, not packet recovery, and may replace corruption with a
freeze.

## Plaintext packet captures

Add capture paths to the Petlibro URL:

```text
dump_d2c_plain=/tmp/petlibro_d2c.dat
dump_c2d_plain=/tmp/petlibro_c2d.dat
```

`dump_plain` remains an alias for `dump_d2c_plain`. Each file is created or
truncated when the client starts.

The formats are intentionally simple and replayable:

```text
D2C record:
  uint32 little-endian decrypted datagram length
  decrypted datagram bytes

C2D record:
  uint64 little-endian Unix timestamp in nanoseconds
  uint32 little-endian plaintext inner-body length
  plaintext inner-body bytes
```

These files may contain camera identifiers, session values, media, network
metadata, or protocol state. Store them outside the repository, review before
sharing, and delete them when no longer needed.

## Offline replay and summaries

Replay a D2C dump through the real unexported parser and assembler without a
camera:

```bash
PETLIBRO_REPLAY_DUMP=/tmp/petlibro_d2c.dat \
PETLIBRO_REPLAY_QUALITY=hd \
go test ./pkg/petlibro -run TestReplayPlainDump -v -count=1
```

Set `PETLIBRO_REPLAY_STRICT=1` to compare strict GOP behavior against the exact
same packet sequence. `PETLIBRO_D2C_DUMP` is accepted as an alias for the replay
path.

Print a stable inventory of recognized and unknown packet families:

```bash
PETLIBRO_C2D_DUMP=/tmp/petlibro_c2d.dat \
PETLIBRO_D2C_DUMP=/tmp/petlibro_d2c.dat \
go test ./pkg/petlibro -run TestDumpPacketSummary -v -count=1
```

Print only decoded maintenance ACK bodies and their timing:

```bash
PETLIBRO_C2D_DUMP=/tmp/petlibro_c2d.dat \
go test ./pkg/petlibro -run TestDumpAckSummary -v -count=1
```

The dump-backed tests skip when their environment variables are unset and do
not require a live camera.

## Extended media packets

The PLAF203 alternates between a normal 36-byte media header and a structurally
validated 44-byte header. The extended path recognizes inner families `0c08`,
`0c09`, `0c0c`, and `0c0d`; the common `0c08` packets carry data fragments and
`0c0d` packets commonly carry the final fragment and frame-info bytes.

When `trace_packets=1`, each candidate logs structural metadata: its type,
acceptance, channel,
`subWire`, fragment counts, payload length, frame number, `nextFrameLike`, extra
length, and rejection reason. A valid extended packet is marked received for
ACK purposes before assembly decisions, preventing parser rejection from
creating an artificial ACK hole.

## What server logs can and cannot prove

The stages are intentionally distinct: received packets become assembled
access units; the adapter may consume or discard units during readiness; the
producer writes selected units into go2rtc receiver tracks; and downstream
consumers attach to those tracks. A count at one stage does not prove progress
at the next. In particular, a WebRTC consumer attachment and forwarded RTP do
not prove that the browser decoded or rendered a frame. Browser diagnostics or
a synchronized client-side capture are required for that conclusion.

FRAMEINFO `onlineNum` is firmware-provided AV session state. It is useful as a
camera-side observation but is not an exact count of add-on producers or
WebRTC viewers.

See the [development guide](camera-development.md#media-header-layouts) for the current
offset map and naming rules.

## Preparing a useful report

For a bounded live test, retain:

- sanitized Petlibro URL options
- camera model and firmware
- correlated bootstrap, codec/readiness, and transport/camera health events
- viewer/FFmpeg diagnostics from the same interval
- `TestReplayPlainDump` and `TestDumpPacketSummary` output
- the smallest unknown packet examples needed to support a new parser rule

Do not upload raw dumps by default. If a maintainer needs one, agree on a secure
transfer and disclose what identifiers or media it contains.
