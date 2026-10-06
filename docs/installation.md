# Installation

The supported setup path installs the Home Assistant add-on, then runs the
guided no-UART bootstrap on an already claimed stock PLAF203. The temporary
payload installs local services and returns the feeder to OEM `AF203_FW`; it
does not replace the long-running feeder firmware.

The guided production path currently configures one feeder per add-on
deployment. Although the runtime controller can discover multiple feeder MQTT
identities, the add-on exposes one State Agent bearer-token option and the
installer binds its protected token to one feeder/Home Assistant address pair.
Multi-feeder State Agent provisioning therefore remains an advanced manual
deployment rather than part of this guide.

> [!CAUTION]
> The OTA path has been demonstrated on firmware 3.1.48 and the payload has
> automated transaction tests, but not every stock variant or power-loss point
> has been physically fault-injected. Use stable power, read the
> [recovery design](../installer/README.md#safety-and-recovery), and proceed only
> on a feeder you own and can recover.

## 1. Prepare Home Assistant and MQTT

1. Install and start an MQTT broker such as the Mosquitto broker add-on.
2. Enable Home Assistant's MQTT integration.
3. Create a dedicated broker account for the Petlibro Local add-on.
4. Add this repository to Home Assistant's add-on repositories:

   ```text
   https://github.com/tannerln7/Petlibro-Home-Assistant
   ```

5. Install **Petlibro Local backend**.
6. Enter the add-on broker account in its configuration, but leave the add-on
   stopped until bootstrap generates the remaining values.

The broker needs two accounts with different roles:

| Account | Used by | When it is known |
| --- | --- | --- |
| Backend account | Petlibro Local add-on | Create before running the installer |
| Feeder factory account | OEM firmware | Captured by the installer during setup |

Do not configure the feeder with the backend account. The installer pauses
after capturing the feeder's factory MQTT `CONNECT`, writes the credentials to
a protected local file, and lets you create the exact second broker account
before any OTA command is sent.

Use a stable LAN IPv4 address or DHCP reservation for the feeder, Home
Assistant, setup machine, and broker. The final broker may run on Home
Assistant. The setup machine must be able to bind its own LAN address on
TCP/1883; if it is also the broker host already listening on that exact
address/port, use a different setup machine or otherwise eliminate the bind
conflict before starting.

## 2. Prepare the setup machine

Use Linux x86_64 or ARM64 on the same trusted LAN as the feeder, Home Assistant,
and final broker. The installer checks for:

- Python 3.12 or newer with the `venv` and `pip` modules;
- Git and GNU Make;
- GNU binutils providing `readelf`;
- `ssh-keygen`; and
- the `arm-linux-gnueabihf-gcc` cross compiler.

Clone the repository:

```bash
git clone https://github.com/tannerln7/Petlibro-Home-Assistant.git
cd Petlibro-Home-Assistant
```

The first preparation needs Internet access. The installer clones a pinned
official Dropbear revision and downloads a checksum-pinned official Zig
toolchain to build static ARMv7 hard-float SSH binaries. It also builds the
State Agent with `arm-linux-gnueabihf-gcc`; users do not need to locate or
download a prebuilt Dropbear binary.

Ensure local firewall rules permit:

- outbound UDP/17484 to the feeder;
- inbound temporary MQTT from the feeder (TCP/1883; required by AF203_FW);
- inbound temporary HTTP from the feeder (TCP/18080 by default); and
- feeder access to the final MQTT broker and Home Assistant after bootstrap.

The complete network path is:

| Source | Destination | Protocol/port | Purpose |
| --- | --- | --- | --- |
| Setup machine | Feeder | UDP/17484 | OEM endpoint provisioning |
| Feeder | Setup machine | TCP/1883 | Temporary source-restricted MQTT |
| Feeder | Setup machine | TCP/18080 by default | One-time payload download |
| Feeder and add-on | Final broker | TCP/1883 by default | Production MQTT |
| Home Assistant host | Feeder | TCP/8765 | State Agent API |
| Recovery workstation | Feeder | TCP/2222 | Key-only SSH |
| Home Assistant clients | Add-on host | TCP/1984, TCP/8554, TCP+UDP/8555 | go2rtc/RTSP/WebRTC |

Both temporary and final feeder-facing MQTT listeners must use TCP/1883.
AF203_FW persists other four-digit ports accepted by the OEM parser but does
not attempt an MQTT connection to them.

## 3. Run the guided installer

From the repository root:

```bash
./installer/install.sh
```

The wrapper creates `.venv/` in the repository when needed, activates it,
installs the dependencies declared in `installer/requirements.txt`, and starts
the Python installer. The virtual environment remains inside the repository and
is ignored by Git. Before doing so, it checks all host build tools listed above.
If any are missing, Debian/Ubuntu users can approve their installation through
`apt-get`; declining or an incomplete installation exits with the complete
missing-dependency list. Other distributions must install the reported packages
with their system package manager and rerun the wrapper.

The prompts collect:

- feeder, setup-host, Home Assistant, and final-broker IPv4 addresses;
- add-on MQTT port, username, and password;
- the temporary HTTP listener port;
- either a newly generated dedicated recovery key or an existing OpenSSH
  public key supplied as a file path or pasted directly into the terminal.

State Agent updates use the project's signed release feed by default. This is
a launch setting rather than saved feeder configuration. Maintainers can
override it for a controlled release feed without adding it to the wizard or
protected configuration:

```bash
./installer/install.sh \
  --state-agent-manifest-url https://example.invalid/state-agent/latest.json
```

The final broker IPv4 address is used by both the feeder and the add-on. The
installer does not ask for a separate add-on-only broker hostname.

SSH recovery is mandatory. Password authentication is not currently offered;
Dropbear is built and started in public-key-only mode on feeder TCP/2222. If
the installer generates a key, back up
`build/bootstrap/output/ssh/plaf203_recovery_ed25519` securely.

The accepted imported key types are `ssh-ed25519`, `ssh-rsa`, and
`ecdsa-sha2-nistp256`. Only the public key enters the payload. The installer
does not read, copy, or modify a corresponding private key.

After confirmation, the installer builds and validates all artifacts, then
asks before changing feeder state. It uses the OEM LAN command to redirect the
feeder to its temporary MQTT listener. If the feeder does not connect promptly,
reboot it once without factory-resetting it.

If the process exits or times out after this redirect, simply rerun it with the
saved configuration. The feeder may be waiting for the temporary broker, and
the rerun starts that broker and first allows an already-redirected feeder to
reconnect without sending another provisioning command. Do not factory-reset
the feeder. A previously installed State Agent or SSH server does not affect
this stage.

## 4. Authorize the captured feeder account

The installer writes the captured account to:

```text
build/bootstrap/output/feeder-mqtt-credentials.json
```

Create that exact username and password on the final broker and grant only the
feeder's required PLAF203 device topic tree. The file contains a live secret:
do not paste it into an issue or add-on log. Return to the open installer and
choose **Retry**. The normal flow continues in the same process; it does not
require restarting the command.

Where the broker supports per-topic ACLs, the feeder identity needs to publish
its exact `dl/PLAF203/<serial>/device/+/post` tree and subscribe to the matching
`dl/PLAF203/<serial>/device/+/sub` tree. The backend account needs the inverse
device access plus its Home Assistant discovery/state/command prefixes. ACL
syntax is broker-specific; do not grant anonymous access merely to pass this
checkpoint.

No OTA is sent until the installer proves the final broker accepts those exact
credentials. You may choose **Stop safely** at this checkpoint. A later run
reuses configuration and build artifacts but repeats feeder connection capture.

## 5. OTA transaction and handoff

Once broker proof succeeds, the installer:

1. publishes a correlated OEM OTA command;
2. serves one randomized URL only to the configured feeder;
3. waits for download, PUBACK, and the OEM terminal result;
4. runs the one-time payload from the inactive slot;
5. preserves the first feeder-local settings and feeding-plan snapshot under
   `/user/data/plaf203-bootstrap/preinstall-state/`;
6. installs the State Agent and key-only Dropbear;
7. restores both OTA slots to OEM firmware and selects OTA1;
8. observes the restored OEM startup event;
9. subscribes to the feeder's exact heartbeat topic with the configured
   backend broker account, redirects OEM MQTT to the final broker, and waits
   for a new non-retained heartbeat; and
10. verifies State Agent health when the setup host has the allowlisted Home
   Assistant source IP.

During temporary-broker startup, the feeder may request the server's complete
feeding-plan collection. The installer acknowledges MQTT delivery but does not
send an application-level success because it has no authoritative plan source
on a stock feeder. The production add-on answers from fresh State Agent truth
after handoff, or leaves the request unanswered if that truth is unavailable.
This preserves schedules across bootstrap and transient State Agent failures.

Final heartbeat verification is best-effort. A broker connection,
authentication, subscription, or heartbeat timeout produces a warning and the
installer still completes after restoring the endpoint. In that case, verify
the feeder connection in Home Assistant before assuming the handoff succeeded.
The OEM firmware publishes the heartbeat; the State Agent does not create or
relay it.

Do not remove power while this transaction is active.

All installer downloads, source trees, caches, staging files, generated keys,
credentials, and outputs stay beneath the repository's ignored
`build/bootstrap/` tree. They are not written to arbitrary home-directory
locations. These files contain secrets despite being ignored; do not share or
commit them.

Important outputs are:

| File | Purpose |
| --- | --- |
| `bootstrap-config.json` | Protected, reusable installer answers |
| `output/addon-options.patch.json` | Values to merge into Home Assistant add-on options |
| `output/state-agent-token.txt` | State Agent bearer token |
| `output/state-agent-token.binding.json` | Feeder/Home Assistant binding that prevents unsafe token reuse |
| `output/feeder-mqtt-credentials.json` | Captured feeder broker identity |
| `output/ssh/` | Generated recovery key pair, if selected |
| `output/BOOTSTRAP_COMPLETE` | Marker written after the transaction finishes |

## 6. Configure and start the add-on

On completion, open:

```text
build/bootstrap/output/addon-options.patch.json
```

Merge those values into the add-on configuration, preserving any unrelated
camera or logging preferences, then start the add-on. The patch supplies the
State Agent token and URL, final feeder broker endpoint, and update settings.
Once the add-on reports its MQTT plugin ready, reboot the feeder once so the
controller observes the non-retained OEM startup event containing camera
identity and can perform the initial endpoint acknowledgement.
The generated patch enables `persist_feeder_mqtt` so the add-on can confirm and
repair the endpoint after its first reconciled feeder startup. After the logs
show a successful `DEVICE_CONFIG_SYNC` acknowledgement, you may set this option
back to `false` to preserve the established endpoint on later feeder boots.

Verify:

1. the feeder and add-on both connect to the final broker;
2. add-on logs show authenticated State Agent reconciliation;
3. feeder entities and controls appear through MQTT discovery;
4. camera UID/address discovery completes and the RTSP stream opens; and
5. `ssh -p 2222 root@FEEDER_IP` works with the selected recovery key.

Healthy add-on logs include `feeder state API recovered`, a transition to
`READY`, and `feeder reconciliation complete`. An immediate
`StateAgentUnauthorized` almost always means the token entered in Home
Assistant is stale or differs from the current generated patch. Update the
add-on option and reload it; do not weaken State Agent authentication.

Expected first-run behavior:

- **Dispensing status** is reacquired from fresh OEM runtime telemetry and
  should become Idle, Dispensing, or Recovering without waiting for a feed.
- The four **Last dispense ...** entities are retained historical facts. On a
  new broker they remain unknown until the first feed supplies those facts;
  afterward they survive Home Assistant restarts.
- The camera producer is lazy. Open the generated stream before expecting live
  camera runtime metadata. Automatic stream names use
  `petlibro_plaf203_<sanitized_serial>`.

If Home Assistant is not the setup machine, perform State Agent health checks
from Home Assistant; requests from other source IPs are rejected by design.

## Updating after installation

The one-time OEM bootstrap is not used for routine State Agent updates. The
installer defaults to the project's signed release manifest at
`https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/state-agent-releases/state-agent/latest.json`.
The add-on and State Agent use the signed manifest/update transaction documented
in the [State Agent README](../state-agent/README.md). Add-on updates follow
normal Home Assistant add-on updates.

The token is intentionally reused only when its protected binding matches the
same feeder and Home Assistant addresses. Use
`./installer/install.sh --rotate-state-agent-token` only when deliberately
replacing it, and immediately apply the newly generated token to the add-on.

For configuration details and diagnostics, continue with:

- [Add-on configuration](../addon/DOCS.md)
- [Installer reference](../installer/README.md)
- [Troubleshooting](troubleshooting.md)
