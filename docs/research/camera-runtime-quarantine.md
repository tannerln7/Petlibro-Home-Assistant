# Camera runtime quarantine

This note indexes reverse-engineering machinery intentionally kept out of the
live camera path. Git remains the source for exact old code; this is not a
second implementation.

## Experimental AV ACK modes

- Historical surface: `ack_mode=high|contig|hybrid` in the Petlibro client.
- Why it existed: captures showed type-`0x09` traffic before its receive-window
  fields and resend behavior were understood.
- Superseding evidence: AF203 firmware analysis established the prior receive
  position, upper receive endpoint, relative NACK list, separate reliable
  control ACK, and timing fields. Commit `085954d` replaced the mode matrix with
  that model and capture-backed tests.
- Restore only if: firmware or packet evidence shows a distinct device family
  with demonstrably different type-`0x09` semantics. Add a protocol variant at
  the transport boundary, not a general tuning matrix.

## FRAMEINFO stream filtering and main/sub media families

- Historical surface: `wrongStreamDrop` filtered FRAMEINFO `onlineNum`, and
  media families `0x05`/`0x07` were named or handled as main/sub streams.
- Why it existed: both observations were plausible while stream identity was
  inferred from captures alone.
- Superseding evidence: firmware-backed work in `085954d` identifies
  `onlineNum` as online AV-client count/state and the families as key/IDR and
  inter-frame transport for one H.264 stream. SPS is the resolution authority.
- Restore only if: a new model supplies capture plus decoded video evidence
  that a different metadata field is a real stream discriminator.

## Selectable stream-control and pacing variants

- Historical surface: `streamctrl_variant`, `streamctrl_quality`, and
  `send_delay_ctrl` URL/add-on options in `client.go`, `bootstrap.go`, and
  generated configuration. These were present in the pre-component history
  carried through `2d3ffeb`; the add-on baseline originated in `fb4ad79`.
- Why they existed: they allowed comparison of the captured Petlibro `0x0024`
  request, stock AVAPI `0x0320`, no request, arbitrary quality bytes, and the
  AVAPI pacing control while startup behavior was unresolved.
- Superseding evidence: the production path is the captured Petlibro HD/SD body
  on channel `0x1000`, followed by the standard zero-valued pacing IOCtrl and
  `IPCAM_START`. Resolution is verified from SPS rather than FRAMEINFO.
- Current decision: the known path is unconditional transport behavior. User
  configuration retains only the real intent, `quality=hd|sd`.
- Restore only if: a capture from supported firmware proves the fixed sequence
  fails and identifies a stable model/firmware discriminator.

## Producer probe wait and first-AU replay

- Historical surface: public `hd_probe_wait_ms`, producer `probe()`,
  `Producer.firstAU`, producer-side keyframe gating, and startup-error string
  matching. The option began with the initial add-on backend (`fb4ad79`) and
  entered runtime metadata in `7986955`.
- Why it existed: go2rtc could advertise the first 640x360 SPS from an HD
  session and probing consumed the only immediately usable IDR.
- Current decision: the hardware behavior remains real, but the producer
  workaround does not. `camera.go` now owns a fixed 15-second HD stabilization
  preference window and owns typed startup retries. It may retain the newest
  SPS-bearing IDR seen during that window only while no later video has been
  discarded. Otherwise it waits at the live edge for the next SPS-bearing IDR,
  preventing a replayed IDR from being followed by P-frames whose references
  preparation consumed. The go2rtc producer simply forwards the first unit it
  reads. The
  15-second value preserves the former packaged add-on default; direct sources
  previously defaulted to no wait, and the bound does not guarantee HD. Live
  GOP resynchronization is separately bounded by the normal five-second camera
  readiness timeout.
- Restore only if: hardware measurements justify changing the adapter policy.
  Record time-to-SPS distributions and whether late epochs update each target
  consumer correctly; do not reintroduce a go2rtc probe-duration option.

## Deliberately retained transport policies

`forceDrain`, independent receive ACK tracking, pending-IDR flush, strict GOP
poisoning, and gapped-IDR/non-strict recovery remain live. They protect observed
loss/reordering and incomplete-end-fragment behavior; the newer ACK model does
not prove those media conditions impossible. They remain below the camera
boundary, with tests for the crucial invariant that force-draining output never
acknowledges an unreceived wire sequence.

The pending-IDR timestamp estimate (`next P-frame camera time - 40 ms`) also
remains transport evidence for a missing trailer, but transport now labels it
as raw camera time. Monotonic RTP-clock repair and AAC clock synthesis are owned
by the camera adapter. Reconsider the estimate only with paired plaintext
captures and decoded frame-timing measurements showing a different cadence.
