# Development guide

This repository is the canonical source for the Home Assistant add-on,
feeder-resident State Agent, and unified installer. Camera source is maintained
in the standalone Petlibro go2rtc fork and consumed through one immutable,
checksum-verified source lock. Reverse-engineering workspaces are not source
dependencies.

## Repository map

| Path | Purpose |
| --- | --- |
| `addon/` | Home Assistant package, services, renderer, and packaging tests |
| `addon/appdaemon/` | Python feeder controller and protocol tests |
| `addon/go2rtc-source.lock` | Pinned standalone go2rtc revision and source checksum |
| `state-agent/` | C state decoder/API, update helper, runit files, tests |
| `installer/` | Guided no-UART bootstrap, payload builder, transaction tests |
| `docs/` | Maintained user, architecture, protocol, and development docs |
| `docker/` | Local Docker Compose deployment |
| `scripts/` | Build, validation, diagnostics, and stream test helpers |

Tests are colocated with the component they exercise. Shared production code
belongs in a shared package only when at least two runtime components genuinely
need the same implementation; do not create a generic utility layer for test
convenience.

## Prerequisites

- Python 3.12 or newer;
- Go 1.24 or newer for standalone camera checks;
- `curl` and `sha256sum` for pinned go2rtc source acquisition;
- a C99 host compiler;
- `arm-linux-gnueabihf-gcc`, `git`, GNU Make, `file`, and `readelf` for release
  and installer validation;
- Docker with Compose for image/config validation;
- FFmpeg for live stream checks;
- `shellcheck` for strict validation (optional for permissive local checks).

Create a local Python environment:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r addon/appdaemon/requirements-dev.txt
python -m pip install -r installer/requirements.txt
```

Never put broker passwords, feeder credentials, State Agent tokens, signing
keys, or generated installer output in tracked files.

## Validation

Run the developer-friendly repository checks from the root:

```bash
./scripts/validate.sh
```

This permissive mode reports any unavailable AppDaemon, ARM, or ShellCheck
checks as explicit skips. Release qualification and the image publication
workflow use strict mode, which fails before validation when any of those
dependencies is unavailable:

```bash
./scripts/validate.sh --strict
```

Focused checks:

```bash
python3 -m pytest addon/tests addon/appdaemon/tests -q
python3 -m pytest state-agent/tests -q
python3 -m pytest installer/tests -q
make -C state-agent clean all
make -C state-agent clean arm-release
go2rtc_source=$(scripts/fetch-go2rtc.sh)
(cd "$go2rtc_source" && go test ./pkg/petlibro ./cmd/petlibro-resolve)
(cd "$go2rtc_source" && go test ./internal/streams -run '^Test(TimestampContinuity|StreamCorrelationIDs)')
(cd "$go2rtc_source" && go test ./pkg/rtsp -run '^Test(PacketWriter|PacketWriters|WriteInterleavedData)')
(cd "$go2rtc_source" && go vet ./internal/streams ./pkg/petlibro ./pkg/rtsp ./cmd/petlibro-resolve)
docker compose --env-file docker/.env.example -f docker/docker-compose.yml config --quiet
```

`scripts/fetch-go2rtc.sh` downloads the versioned source release named in
`addon/go2rtc-source.lock`, verifies its SHA-256 checksum, and reuses an ignored
revision-keyed cache under `build/go2rtc-source`. The maintained Go checks
target the Petlibro transport and resolver, the modified stream graph, and RTSP
packet delivery. The standalone repository owns its source, tests, CI, and
release lifecycle.

Build the add-on image with:

```bash
./scripts/build-local.sh
```

The local image build defaults to bounded Go compile parallelism. Override
`GO_BUILD_PROCS` only on a host with sufficient memory and CPU.

Live feeder tests are separate from deterministic validation. Supply values
only through ignored `docker/.env`, generated installer files, or Home
Assistant options. The bounded [live qualification harness](../addon/tests/live/README.md)
documents its additional FFmpeg, browser, and endpoint requirements.

## Add-on and controller development

[`addon/render_config.py`](../addon/render_config.py) validates Home Assistant
`/data/options.json` or equivalent Compose environment variables and writes all
runtime files atomically. Add a new option consistently to `config.yaml`, the
renderer, templates, Compose environment, docs, and renderer tests.

The controller's key ownership rule is:

```text
commands -> state coordinator -> backend protocol -> fresh State Agent verify
```

Do not let MQTT acknowledgments, retained Home Assistant state, or AppDaemon
storage become persistent feeder truth. Blocking State Agent and resolver calls
belong in AppDaemon's executor; completion callbacks must handle AppDaemon's
`callback(result=...)` calling convention and stale attempt tokens.

Logging policy:

- `info`: normal lifecycle and actionable outcomes;
- `debug`: bounded semantic diagnostics;
- `trace`: short-lived raw protocol evidence only.

Use centralized redaction in `petlibro_logging.py`. Do not enable global
AppDaemon debug output or add credential-bearing payloads to tests/logs.

## Camera development

Petlibro camera code lives in the standalone
[`tannerln7/go2rtc`](https://github.com/tannerln7/go2rtc) repository. Make
camera implementation and test changes there, publish a new immutable fork
release, then update `addon/go2rtc-source.lock` in a separate reviewed add-on
change. Publishing a fork release never publishes an add-on image by itself.

Use the [camera development guide](camera-development.md) for build/test
commands and the [camera debugging guide](camera-debugging.md) for bounded live
diagnostics. Keep packet captures outside Git; reduce reusable evidence to
sanitized synthetic fixtures.

## State Agent development

The State Agent must continue to:

- read only fixed allowlisted files;
- validate complete binary shapes;
- parse integers explicitly with the firmware's little-endian layout;
- derive decoded values and revisions from the same buffers;
- keep persistent, effective-cache, and runtime classifications distinct;
- expose no generic path, shell, or command interface.

Update routes must retain strict framing, signature-before-parse behavior,
pinned trust, fixed staging paths, kernel transaction locking, durable atomic
replacement, probation, and rollback. Test interrupted phases and malformed
input. See the [State Agent README](../state-agent/README.md).

## Installer development

The installer is device-modifying code. Keep network listeners bound to the
configured interfaces, restrict temporary services to the expected feeder IP,
correlate OTA evidence by request and connection generation, and prove the
final broker accepts the captured feeder identity before OTA. The post-install
heartbeat observer must authenticate with the backend identity, subscribe to
the exact feeder topic before restoring the endpoint, reject retained or
malformed evidence, and degrade to a clear nonfatal warning when observation is
not possible.

Payload changes must be explicit, deterministic, and covered by host-side
transaction tests. Never copy an ambient feeder development directory into a
production payload. Preserve the invariant that an OEM donor slot is validated
and selected before overwriting the one-time payload slot. See the
[installer README](../installer/README.md).

Installer downloads and generated artifacts belong only under the ignored
`build/bootstrap/` workspace. The Dropbear builder pins the upstream tag and
commit, verifies the Zig archive checksum, rejects dirty cached source, and
records a SHA-256 build manifest. Update those pins deliberately and test a
clean-cache build when changing the toolchain or Dropbear version.

## Local runtime and diagnostics

Copy [`docker/.env.example`](../docker/.env.example) to ignored `docker/.env`,
fill only local values, then use:

```bash
./scripts/run-local.sh
./scripts/test-stream.sh
./scripts/collect-logs.sh
```

Do not run mutating commands against a feeder unless that operation is the
explicit purpose of the test. Prefer synthetic protocol fixtures and temporary
filesystem roots for routine development.

## Protocol and research workflow

Raw packet captures, firmware images, filesystem dumps, decompiler projects,
live logs, proof payloads, and one-off investigation scripts belong in an
untracked research workspace. Do not commit credentials or device-specific
identifiers, even in historical evidence.

When a discovery changes maintained behavior:

1. record durable names/types in the local reverse-engineering project;
2. implement the smallest production change in the owning component;
3. add a sanitized synthetic fixture or focused regression test;
4. update maintained architecture/protocol documentation with conclusions, not
   a raw investigation transcript;
5. run component checks and the canonical validation suite.

## Packaging and releases

Home Assistant installations normally pull the prebuilt `amd64` image named in
`addon/config.yaml`. `main` is the stable channel; `develop` is the integrated
development and physical-testing channel. The image workflow validates the
branch/version pairing before publishing, and only stable releases update
`latest`. Follow the operational [release process](release-process.md) for
versioning, promotion, immutable tags, and the required hotfix carry-forward
rule.

Keep release image references enabled in committed metadata; only a local
Supervisor copy should omit `image:` to force a build.

State Agent releases are separate signed ARM artifacts. Update `VERSION`, build
the static binaries, publish the immutable artifact, then publish its signed
manifest. Follow the trust-anchor rotation sequence in the
[State Agent README](../state-agent/README.md#signed-updates).
