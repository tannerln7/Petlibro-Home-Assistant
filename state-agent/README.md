# PLAF203 State Agent

The State Agent is the small feeder-resident service that gives the Home
Assistant controller authenticated access to persistent feeder truth. It
decodes a fixed allowlist of OEM state files and supports a tightly constrained
signed self-update transaction.

It is deliberately **not** a remote shell or general filesystem API. It never
accepts caller-selected paths or commands and does not expose MQTT, camera, or
TUTK credentials.

## Responsibilities and boundaries

The agent:

- reads each known file below `/user/data` once per request and derives decoded
  values and revisions from those same buffers;
- validates exact file sizes before decoding;
- returns semantic values, optional raw bytes, and stable revisions;
- classifies decoded fields as `persistent`, `effective_cached`, or `runtime`;
- exposes both pending normal and error firmware feed-event rings, not durable
  history;
- deploys a fixed allowlist of release-embedded compatibility assets; and
- stages signed agent updates for activation by its runit supervisor.

Only `persistent` fields may verify a user command. Firmware-calculated cache
fields and runtime bytes can be stale on disk. In particular,
`/v1/core.motor_state_raw` is informational and is not used to reconstruct
current dispensing status; that uses solicited live firmware telemetry in the
Home Assistant controller.

## API

The normal API listens on TCP port `8765` and requires both a bearer token and
a configured source-IP allowlist. A disallowed peer is closed immediately
after `accept`, before its HTTP request is parsed or it can occupy the single
request-processing slot. Bearer authentication remains a second boundary for
allowed peers. Principal routes are:

- `GET /health`: process and source-file health;
- `GET /v1/rev`: persistent snapshot revisions;
- `GET /v1/core`: decoded core state and complete feeding-plan collection
  (`?raw=1` adds diagnostic bytes);
- `GET /v1/feed-events`: pending outbound firmware event slots (`?raw=1` adds
  bounded raw slot bytes);
- `GET /v1/compatibility`: managed-file deployment and ping-mitigation status;
- `POST /v1/compatibility/reconcile`: idempotently deploy all assets and enable
  required compatibility features owned by this signed release;
- `POST /v1/compatibility/deploy`: install the release-embedded ping shim and
  recognized guarded startup script;
- `POST /v1/compatibility/enable` and `disable`: change only the opt-in marker;
- `POST /v1/compatibility/restore-startup`: restore the validated legacy
  startup backup;
- `GET /v1/version`: installed agent/API/platform metadata;
- `GET /v1/update-status`: durable update transaction state;
- `POST /v1/update`: fixed-format signed update upload.

`state.bin` must be exactly 236 bytes. Feeding plans are decoded as exact
47-byte records; schedule equality excludes runtime execution and regenerated
sync metadata. `feed_rec.bin` and `err_feed_rec.bin` are rings of pending
93-byte event slots and must not be presented as permanent feed history. Their
firmware range is `(head, tail]`: `head` is the last consumed slot and `tail`
is the last enqueued slot. `/v1/feed-events` retains `events` for the normal
ring and adds `error_events`; every item includes `source_ring`.

The OEM firmware owns these files and may update them independently. Shape,
bounds, and semantic validation reject malformed inputs. Feed-event reads
sample all four queue indices before and after the two record files and retry
up to three times; `snapshot_consistent` reports whether the index generations
matched. This cannot make a multi-file read atomic or prove that record bytes
did not change while indices remained fixed. Persistent writes therefore rely on AppDaemon's fresh
preflight and post-acknowledgement verification rather than a State Agent
stabilization heuristic.

## Installation

The recommended installation path is the repository's
[unified no-UART installer](../installer/README.md). It packages the ARM
binaries, runit services, token, source-IP ACL, and minimal startup integration
into the one-time OEM OTA bootstrap.

For authorized shell-based development, install these artifacts under
`/user/data/local-state-agent`:

- `plaf203-state-agent`;
- `plaf203-update-fs`;
- `runit/`;
- a mode-0600 `token`; and
- the startup integration in [`app_start_snippet.sh`](app_start_snippet.sh).

The run command must set `--allow-ip` to the Home Assistant host that originates
API requests. The runtime also requires the feeder's fixed
`/usr/bin/flock`, `/usr/bin/nc`, and `/usr/bin/sv` tools.

## Build and test

Host build:

```bash
make -C state-agent clean all
```

Static ARMv7 hard-float release build:

```bash
make -C state-agent clean arm-release
```

The cross-build requires `arm-linux-gnueabihf-gcc` and emits
`plaf203-state-agent` plus the narrow durability helper
`plaf203-update-fs`. Both outputs and `build/` are ignored; do not commit local
binaries.

Tests run the host binary against synthetic feeder files:

```bash
python3 -m pytest state-agent/tests -q
```

The agent embeds the version from [`VERSION`](VERSION), the Ed25519 candidate
trust anchor from [`release-public-key.hex`](release-public-key.hex), and the
vendored audited crypto implementation in [`vendor/monocypher`](vendor/monocypher).

## Signed updates

The add-on downloads an immutable artifact and signed manifest over HTTPS,
verifies them, then uploads one fixed binary frame to `POST /v1/update`. The
agent verifies the detached Ed25519 signature, strict manifest schema,
platform/API compatibility, SemVer progression, artifact size/SHA-256, and a
basic ARMv7 ELF shape before staging anything.

The runit supervisor—not the HTTP process—owns activation:

1. serialize with `update/transaction.lock`;
2. stop the old service;
3. durably create one rolling backup;
4. activate the candidate through same-directory atomic replacement;
5. start and probe authenticated `/health` and exact `/v1/version`;
6. commit success or durably restore the validated backup.

Transaction phases are persisted so reboot recovery can finish a proven
candidate or resume rollback. The fixed-path `plaf203-update-fs` helper performs
file/directory fsync around transaction-significant replacement and cleanup.

### Stable and development feeds

The two feeds use the same manifest schema, Ed25519 trust anchor, update frame,
and feeder-side verification:

- stable: `state-agent/latest.json` on `state-agent-releases`;
- development: `state-agent/dev/latest.json` on `state-agent-releases`.

The manifest `channel` remains `stable` in both feeds for protocol
compatibility. Development selection is explicitly controlled by its URL and a
SemVer prerelease such as `0.4.0-rc.1`; existing installations remain on the
stable URL unless reconfigured.

Development builds are produced without signing credentials by
[`build-state-agent-release.yml`](../.github/workflows/build-state-agent-release.yml).
Every relevant `develop` push builds its exact revision automatically. Record
the successful run for that source commit:

```bash
SOURCE_SHA="$(git rev-parse HEAD)"
gh run list --workflow build-state-agent-release.yml --limit 5
```

Once this workflow exists on the default branch, `workflow_dispatch` can also
rebuild an explicitly supplied full commit SHA without changing source.

The workflow runs strict validation, builds the static ARMv7 hard-float ELF,
and uploads the binary plus metadata binding its SHA-256, size, version, trust
anchor, and source revision. It never has access to the private signing key.

The local publisher requires authenticated `gh` and Git push access, Python
with `cryptography`, and the `file` and `readelf` commands. Keep the private key
in a mode-restricted location outside the repository and pass only its path or
seed through `PETLIBRO_STATE_AGENT_SIGNING_KEY`; do not put it in workflow
secrets, command arguments, logs, release worktrees, or shell history.

After the workflow succeeds, prepare a clean release-branch worktree and run
the local publisher with the established key outside this repository:

```bash
git fetch origin state-agent-releases
git worktree add /srv/dev/worktrees/petlibro-state-agent-releases \
  state-agent-releases

export PETLIBRO_STATE_AGENT_SIGNING_KEY=/secure/path/state-agent-ed25519.pem
python3 state-agent/scripts/validate_development_release.py publish \
  --workflow-run-id RUN_ID \
  --source-revision "$SOURCE_SHA" \
  --release-worktree /srv/dev/worktrees/petlibro-state-agent-releases
```

The publisher verifies the successful workflow run and exact source revision,
rejects stable versions and non-ARM artifacts, and requires the signing key to
match the committed trust anchor through `build_release_manifest.py`. It then:

1. rejects an existing immutable `state-agent/dev/VERSION` path or a version
   not newer than the current development pointer;
2. commits and pushes the immutable binary first;
3. downloads the commit-pinned raw URL without redirects and verifies it;
4. signs the canonical manifest locally;
5. commits and pushes only `state-agent/dev/latest.json` and its detached
   `latest.json.sig`;
6. downloads the public feed and artifact and verifies them with the add-on's
   release validation code.

It snapshots the stable manifest and signature and aborts if either changes.
Normal Git pushes are used, so concurrent or non-fast-forward publication also
fails closed. If publication stops after the immutable artifact commit but
before the manifest commit, inspect the release worktree and rerun with
`--resume`; the existing artifact must match exactly. Never replace a published
version directory. If an incorrect manifest was already published, restore
service by publishing a higher prerelease after correcting the source; do not
rewrite the artifact or force-push the release branch.

Promotion is a new stable build, not a rename: after hardware qualification,
set `VERSION` to `0.4.0`, rebuild from the promoted stable source, and follow
the existing stable publication process below. Stable `latest.json` moves only
in that separately reviewed operation.

## Release-managed compatibility files

State Agent 0.4.0 embeds a small, build-generated asset table in the normally
signed ELF. [`managed/manifest.json`](managed/manifest.json) is also consumed
by the bootstrap payload, so fresh and upgraded installations use the same
release-owned bytes, destinations, and modes. The authenticated API never
accepts a path, file body, script, or command. Adding a future managed file
requires adding it to the signed release source and a fixed installation rule.

Deployment compares SHA-256 before writing, stages in the destination
directory, applies the fixed mode, fsyncs the file, atomically renames it, and
fsyncs the directory. The boot-critical `/user/app_start.sh` is replaced only
when it exactly matches the recognized legacy installer template. A validated
mode-0700 backup is created first; unknown or user-modified startup content is
rejected. Repeating deployment does not rewrite matching files.

The initial asset is `/user/data/local-compat/bin/ping`. It immediately fails
only the exact confirmed `ping HOST -c 10` shape for the four known OEM probes,
without output, and delegates every other invocation unchanged to the verified
`/bin/ping` or `/usr/bin/ping`. The guarded startup script prepends the shim
directory to the inherited PATH only for `AF203_FW` and its children, and only
when `ping-shim.enabled` exists. The low-level `deploy` operation does not
create that marker; `reconcile` deploys all release assets and enables required
features. Neither operation restarts firmware or reboots the feeder.
Compatibility mutations return a conflict while a State Agent update
transaction is outstanding.

AppDaemon reconciles after a usable connection and after a confirmed State
Agent update. Enable/disable changes desired state only. `active` is set by the
boot launcher; `reboot_required` is true whenever desired and active state
differ. Existing installations therefore wait for a natural or explicit
operator reboot rather than interrupting OEM operation. The bootstrap payload
installs the same assets before its already-required final reboot.

Create a normal release manifest with:

```bash
python3 state-agent/scripts/build_release_manifest.py \
  --artifact-path state-agent/plaf203-state-agent \
  --artifact-url https://updates.example.invalid/plaf203/VERSION/plaf203-state-agent \
  --release-url https://example.invalid/releases/VERSION \
  --signing-key /secure/path/ed25519-private.pem \
  --public-key-file state-agent/release-public-key.hex \
  --output-dir /secure/output/release
```

Private signing keys must never enter this repository. Normal releases require
the signer-derived public key to match the embedded candidate key. Intentional
key rotation uses `--rotate-trust-anchor`: the current private key signs a
candidate that embeds the next public key. Deploy and verify that transition
before changing the add-on's trusted key material.

## Read-only verification

From an allowed host:

```bash
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/health
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/v1/rev
curl -fsS -H "Authorization: Bearer $TOKEN" http://FEEDER_IP:8765/v1/core
```

Use `?raw=1` only for bounded local diagnostics. Never paste raw responses or
tokens into public reports.
