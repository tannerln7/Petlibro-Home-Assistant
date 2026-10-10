# Petlibro module

The Petlibro module registers the `petlibro://` input with go2rtc. The transport
implementation is a clean-room LAN client for the Kalay/TUTK-family UDP protocol
used by Petlibro camera feeders on port `32761`.

Runtime streaming does not require a Petlibro account, vendor SDK, or CGO. The
camera must first be provisioned with the Petlibro app, and its firmware may
still depend on the vendor cloud connection to authorize LAN sessions.

## Support status

| Model | Status | Notes |
| --- | --- | --- |
| PLAF203 | Verified | H.264 HD/SD and optional AAC audio |
| PLAF103 | Protocol evidence only | Login and bootstrap captures informed the implementation; live playback is not yet verified |
| Other Petlibro cameras | Unknown | May work when they share the same firmware protocol, but should be treated as untested |

Two-way audio is not implemented.

## URL format

```text
petlibro://CAMERA_IP?uid=20_CHARACTER_UID&quality=hd
petlibro://?uid=20_CHARACTER_UID&subnet=192.168.1.0/24&quality=hd
```

Core options:

| Parameter | Required | Default | Description |
| --- | --- | --- | --- |
| URL host | no | discovery | Fixed camera address; UDP port defaults to `32761` |
| `uid` | yes | — | Exact 20-character camera UID |
| `subnet` | no | local broadcast | Additional IPv4 CIDR used for discovery; may be repeated |
| `quality` | no | `hd` | Requests and selects `hd` or `sd` video |
| `audio` | no | `false` | Requests AAC audio when `true` or `1` |
| `strict` | no | `false` | Drops damaged IDRs and dependent GOP frames instead of emitting a gapped IDR |
| `verbose` | no | `false` | Enables compact bootstrap, probe, health, and ACK diagnostics |

The tracked [`go2rtc.example.yaml`](../../go2rtc.example.yaml) contains a
recommended PLAF203 configuration. Trace and capture options are documented in the
[Petlibro debugging guide](../../../../docs/camera-debugging.md).

## Discovery

When the URL omits its host, go2rtc sends a UID-specific LAN search and accepts
the matching camera's response address. Local broadcast discovery requires the
camera and go2rtc process to share a broadcast domain. Add `subnet=` for routed
IPv4 networks or use a fixed address. Container discovery normally requires
host networking.

## Quality and codec probe

The transport sends the captured Petlibro stream-control body for the requested
quality before `IPCAM_START`, followed by the standard AVAPI pacing control.
Resolution is learned from H.264 SPS; FRAMEINFO `onlineNum` is an AV-client
count/state and is never used as a stream selector.

On the tested PLAF203, an HD session can start with a 640x360 SPS and switch to
1920x1080 after several seconds. The camera adapter owns this quirk and waits up
to 15 seconds for the higher-resolution SPS before exposing the source. It then
returns the selected SPS-bearing IDR as the first media unit. This is fixed
camera normalization rather than a go2rtc probe-duration setting.

Strict mode affects damaged-frame output only. It does not change stream
selection, packet parsing, ACK tracking, or recovery.

## Implementation notes

The post-login flow is:

```text
LAN_SEARCH3 -> KNOCK2 -> LOGIN A/B -> stream/bootstrap IOCtrls
  -> AV-ready ACK -> receive/maintenance loops -> H.264/AAC access units
  -> PLAF203 camera normalization -> go2rtc producer
```

The parser supports both the normal 36-byte media header and the alternate
44-byte media layout used by packet families `0c08`, `0c09`, `0c0c`, and
`0c0d`. Both layouts enter one assembler after structural validation. The
observed key/IDR, inter/P, and audio families retain independent frame state;
these labels describe packet behavior rather than official SDK terminology.

Receive ACK state is independent from the assembler output cursor. The
contiguous ACK watermark advances only for wire sequences actually received;
force-draining an assembler hole does not acknowledge that missing packet.
Positions above a hole are compressed into a capped set of consecutive ranges,
preventing a single permanent gap from growing memory once per received packet.

Wire constants and captured templates live in
[`pkg/petlibro/templates.go`](../../pkg/petlibro/templates.go). Parser,
assembler, ACK, and bootstrap tests live beside the implementation in
[`pkg/petlibro`](../../pkg/petlibro/).

Session close sends the known `IPCAM_STOP` IOCtrl once before UDP teardown. The
adapter, rather than a transient downstream consumer, owns that physical media
lifetime.

For packet formats, counters, dump replay, and failure diagnosis, see the
[debugging guide](../../../../docs/camera-debugging.md). For code structure and
validation, see the [development guide](../../../../docs/camera-development.md).
