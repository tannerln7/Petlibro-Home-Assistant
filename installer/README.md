# PLAF203 installer/bootstrap

This directory contains the guided end-user workflow for moving an already
claimed stock PLAF203 feeder to Petlibro Local without UART access. It builds
the production feeder payload, conducts the guarded OEM OTA transaction, and
generates the matching Home Assistant add-on configuration.

The installer is the only production component that uses the OEM LAN
provisioning and OTA surfaces. The installed State Agent and running add-on do
not expose those capabilities.

The guided production path provisions one feeder per add-on deployment. The
installer binds its protected State Agent token to one feeder/Home Assistant
address pair; multi-feeder authentication is an advanced manual deployment and
is not covered by this workflow.

## Guided workflow

Run this command from the repository root:

```bash
./installer/install.sh
```

The wrapper creates a repository-local `.venv/` when needed, activates it,
installs `installer/requirements.txt`, and starts the Python installer. The
environment stays inside the repository and is ignored by Git. It first checks
Python, Git, GNU Make, binutils, OpenSSH key tooling, and the ARM hard-float
cross compiler. If anything is missing, it displays the complete list and asks
before installing the corresponding Debian/Ubuntu packages with `apt-get`.
Declining, lacking a supported package manager or privilege path, or still
failing preflight after installation stops before the installer starts.

The terminal interface guides the normal sequence in one session:

1. collect feeder, setup-host, Home Assistant, and MQTT settings;
2. generate a dedicated Ed25519 recovery key or import an existing OpenSSH
   public key;
3. build the static ARM State Agent and pinned static Dropbear;
4. prepare the one-time payload and add-on options;
5. point the feeder at a source-restricted temporary MQTT server with the OEM
   LAN command;
6. capture the feeder's factory MQTT CONNECT identity during normal startup;
7. pause in place while the operator creates that account on the final broker,
   retrying until the broker accepts it;
8. publish one correlated OEM `OTA_UPGRADE` request and serve the payload only
   to the configured feeder;
9. install the State Agent and SSH, restore both firmware slots to OEM firmware,
   select OTA1, and reboot; and
10. subscribe to the exact feeder heartbeat with the configured backend broker
    account, redirect OEM MQTT to the final broker, and write the add-on options
    patch.

No DNS rewrite or OEM account metadata is required by the direct-provisioning
implementation. The installer supplies all protocol-internal compatibility
values without prompting the user.

The OEM request supplies the payload URL and MD5. That digest provides
transfer-corruption detection, not authenticity; the temporary listener
restrictions and trusted LAN are therefore important.

## Prerequisites

- A PLAF203 already onboarded to Wi-Fi and claimed in the Petlibro app.
- A trusted LAN shared by the feeder, setup machine, Home Assistant, and final
  MQTT broker.
- A dedicated final-broker account for the Home Assistant add-on.
- Permission to create the separately captured factory feeder account and ACL.
- Linux x86_64 or ARM64 with Python 3.12+ (`venv` and `pip` included), Git,
  GNU Make, `readelf`,
  `ssh-keygen`, and `arm-linux-gnueabihf-gcc`.
- Internet access during the first artifact preparation so the installer can
  clone the pinned official Dropbear source and download its pinned Zig
  toolchain.

Both the temporary and final feeder-facing MQTT listeners must use TCP/1883.
Live testing confirmed that AF203_FW persists another four-digit port accepted
by the OEM endpoint parser but does not attempt the MQTT connection. The setup
machine must be able to send to feeder UDP/17484 and receive feeder connections
on TCP/1883 and temporary TCP/18080 by default.

The temporary listener must be able to bind the configured setup-host
IPv4/TCP/1883 pair. If the setup machine is also the final broker host already
listening on that address and port, use another setup machine or resolve the
bind conflict first.

## SSH recovery access

SSH is mandatory because it is the supported device-recovery path after
bootstrap. The installer offers:

- generation of a dedicated unencrypted Ed25519 key under
  `build/bootstrap/output/ssh/`; or
- use of an existing OpenSSH public key, either as a file path or pasted
  directly into the terminal.

Imported keys are validated and limited to `ssh-ed25519`, `ssh-rsa`, or
`ecdsa-sha2-nistp256`. The installer never needs the corresponding private key.

Only the public key is included in the payload. Back up a generated private key
before removing the build workspace. Password authentication is intentionally
not supported: it is disabled at compile time and Dropbear starts with `-s`.
The feeder listens on TCP/2222.

The installer clones official Dropbear from
`https://github.com/mkj/dropbear.git` at the exact tag and commit recorded in
[`dropbear.py`](dropbear.py). It downloads an official checksum-pinned Zig
release and builds one static ARMv7 hard-float `dropbearmulti` containing only
the server and key generator. The cached bundle includes the upstream license
and a build manifest whose binary SHA-256 is checked before reuse.

## Repository-local workspace

The installer does not scatter generated files through the user's home
directory. Persistent installer state is contained beneath the ignored path:

```text
build/bootstrap/
├── bootstrap-config.json       # protected installer configuration
├── cache/                      # source, Zig toolchain/caches, and Dropbear build
├── dropbear-build.log          # created when a source build runs
└── output/
    ├── AF203_FW.bootstrap
    ├── addon-options.patch.json
    ├── state-agent-token.txt
    ├── state-agent-token.binding.json  # token's feeder/HA address binding
    ├── feeder-mqtt-credentials.json   # after capture
    ├── BOOTSTRAP_COMPLETE             # after success
    └── ssh/                            # generated recovery key, when selected
```

Files containing secrets are created with restrictive permissions. The whole
`build/` tree is ignored by Git, but it remains sensitive and should not be
uploaded to issues or logs. Build staging is also created inside this tree and
removed after use. An existing SSH public key may be read from another location;
the installer does not modify or copy its private key.

## Broker account checkpoint

The feeder has a factory MQTT identity separate from the add-on account. Once
captured, it is written to protected
`build/bootstrap/output/feeder-mqtt-credentials.json`. Create that exact broker
account and apply a least-privilege ACL for its PLAF203 device topic tree, then
select **Retry**. The installer remains active and does not send OTA until the
final broker accepts the credentials.

Choosing **Stop safely** exits before OTA. Rerunning the guided installer later
reuses protected configuration and cached artifacts, but it must repeat the
temporary broker connection because no feeder runtime session is retained.

If a run stops after command `0x02` redirects the feeder but before OTA starts,
the feeder may remain pointed at the now-stopped temporary broker. This is
recoverable: rerun the installer with the same configuration. The temporary
listener starts before the redirect command is repeated, so the feeder can
reconnect without a factory reset. Existing State Agent or SSH installations
do not interfere with this MQTT capture stage.

## Safety and recovery

> [!CAUTION]
> Arbitrary OTA execution and return to untouched OEM firmware have been
> demonstrated on firmware 3.1.48. Host-side transactions are tested, but the
> complete production path has not been power-loss fault-injected on every
> firmware variant. Use stable power and only modify hardware you own and can
> recover.

Automatic bootstrap admission is therefore limited to observed firmware
`3.1.48`. The existing `target_software_version` override is a deliberate lab
escape hatch for recovery-capable investigation of another observed `3.x.y`;
it is not a claim that the selected firmware has compatible slots, startup,
provisioning commands, or persistent-state files.

The payload validates the opposite slot as an ELF donor and durably selects it
before modifying the payload slot. It installs production services, then hands
execution to a separate shell so it can unlink the temporary OTA payload before
copying and verifying the donor directly into that slot. This avoids allocating
a third full-size firmware file on the compressed JFFS2 volume; raw OEM file
size is not a reliable free-flash admission check. Both slots end with OEM
firmware and OTA1 is selected only after verification. A handled failure leaves
the untouched donor selected and preserves previously installed State Agent/SSH
startup hooks wherever the existing service files remain available. Do not
remove feeder power during this window.

Before changing startup or sidecar services, every payload execution captures
and validates the current feeder-local `attr/` and `feed_plan/` trees under the
mode-restricted `/user/data/plaf203-bootstrap/preinstall-state/` directory.
Attempts use collision-safe names such as `snapshot-000001`; each new attempt
uses the next available number and never changes an earlier directory. The
`complete` marker is written only after the required state and plan files pass
structural validation. An interrupted or invalid capture remains without that
marker, and a retry creates another snapshot instead of reusing it. Snapshots
are not automatically restored, selected, migrated, or pruned. They are
recovery evidence for explicit maintenance decisions, not a second live state
source; normal operation and reconciliation continue to use the OEM files.

The temporary broker never claims to have authoritative feeding plans. It
PUBACKs the feeder's `GET_FEEDING_PLAN_EVENT` delivery but sends no application
response; after installation, the add-on answers that request only when a
fresh State Agent snapshot is available. This prevents bootstrap or a failed
handoff read from replacing an existing schedule with fabricated empty plan
data.

The payload intentionally excludes telnet, packet capture, boot timing, live
firmware logging, debug mounts, and other research tooling.

## Completion and verification

Success requires a correlated OTA PUBACK and terminal report, a payload request
from the configured feeder IP, a post-install OEM `DEVICE_START_EVENT`, and
final broker redirection. The installer attempts to prove the handoff by using
the backend broker account to subscribe to the feeder's exact heartbeat topic
before restoring the endpoint. It ignores retained, malformed, and unrelated
messages. Broker or heartbeat verification failure is nonfatal: the endpoint is
still restored and the installer finishes with an explicit warning to verify
the connection in Home Assistant. The heartbeat is produced by OEM firmware,
not the State Agent.

When the setup host is also the configured Home Assistant source address, the
installer additionally verifies authenticated State Agent `/health`.

Apply `build/bootstrap/output/addon-options.patch.json` to the installed add-on
configuration, start the add-on, and verify:

- the feeder connects to the final broker;
- State Agent `/v1/core` reconciliation succeeds;
- Home Assistant discovery and controls appear;
- SSH connects to feeder TCP/2222 with the selected key; and
- camera identity and LAN discovery complete.

If the setup host and Home Assistant have different source IPs, run the State
Agent health verification from Home Assistant because the agent allowlist is
intentionally source restricted.

## Advanced commands

The no-argument guided flow is recommended. Maintainers can split phases for
inspection or controlled automation:

```bash
./installer/install.sh configure
./installer/install.sh prepare --config build/bootstrap/bootstrap-config.json
./installer/install.sh run --config build/bootstrap/bootstrap-config.json
```

The generated add-on options use the project's signed State Agent release feed
by default. Override it only at launch, before the subcommand if one is used:

```bash
./installer/install.sh \
  --state-agent-manifest-url https://example.invalid/state-agent/latest.json
```

The manifest URL is intentionally not prompted for or persisted in
`bootstrap-config.json`.

The State Agent token is reused only when its protected binding file matches
the configured feeder and Home Assistant IP addresses. To replace it
deliberately, run with `--rotate-state-agent-token`, then apply the newly
generated token to the add-on before expecting reconciliation to succeed.

`run --yes` bypasses only its interactive recoverability confirmation. It does
not bypass broker proof, source restrictions, correlation, or payload checks.
All configuration and output paths must remain inside the repository.

The example schema is
[`bootstrap-config.example.json`](bootstrap-config.example.json). Main modules:

- [`bootstrap.py`](bootstrap.py): configuration, orchestration, and evidence;
- [`tui.py`](tui.py): dependency-free menu navigation;
- [`dropbear.py`](dropbear.py): pinned acquisition and static ARM build;
- [`mqtt_server.py`](mqtt_server.py): source-restricted temporary broker;
- [`protocol.py`](protocol.py): OEM MQTT commands and correlation; and
- [`payload.py`](payload.py): deterministic production transaction payload.

## Development and tests

```bash
python3 -m pytest installer/tests -q
```

Keep device-facing operations explicit and source restricted. Add production
files through the payload manifest, never from an ambient feeder directory.
Preserve the donor-first recovery invariant and add negative-path transaction
tests for slot or startup changes. See the
[development guide](../docs/development.md#installer-development).
