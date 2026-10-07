# Petlibro Local

Petlibro Local brings supported Petlibro camera feeders onto a user-controlled
LAN. It replaces the feeder's cloud MQTT connection with a local broker,
publishes feeder controls and observations to Home Assistant, exposes the camera
through go2rtc, and reads durable feeder configuration from a small on-device
State Agent.

The project currently targets the MQTT-based **PLAF203 / Granary Camera
Feeder** family. It is independent community software and is not affiliated
with Petlibro.

> [!IMPORTANT]
> **Feedback wanted:** If you set up—or attempt to set up—this project with your
> PLAF203, please share your experience in the
> [feedback discussion](https://github.com/tannerln7/Petlibro-Home-Assistant/discussions/1#discussion-10891796).
> Successful installations are helpful to hear about too. Tell us what worked,
> what failed or was confusing, and what could make the process easier. If you
> are stuck or need troubleshooting assistance, use the
> [ask-for-help discussion](https://github.com/tannerln7/Petlibro-Home-Assistant/discussions/2#discussion-10891804).
> If you identify a reproducible bug or other concrete project problem,
> [open an issue](https://github.com/tannerln7/Petlibro-Home-Assistant/issues/new/choose)
> with reproduction steps, relevant logs, firmware and add-on versions, and a
> link to any related discussion as supporting context.

## Components

| Component | Responsibility |
| --- | --- |
| [`addon/`](addon/) | Home Assistant add-on image, configuration renderer, AppDaemon MQTT controller, and patched go2rtc camera backend |
| [`state-agent/`](state-agent/) | Authenticated feeder-resident service that decodes persistent state and stages signed State Agent updates |
| [`installer/`](installer/) | Guided no-UART bootstrap that migrates an already claimed stock feeder to the local architecture |
| [`docker/`](docker/) | Docker Compose deployment for Linux hosts outside Home Assistant OS |

The add-on is the long-running coordinator. The State Agent does not replace the
OEM firmware: it runs beside it and exposes only narrowly defined local files.
The installer is a one-time provisioning tool; after bootstrap, State Agent
updates use the signed update path managed by the add-on.

```text
Home Assistant ─┐
                ├─ MQTT broker ───── OEM feeder firmware
Petlibro add-on ┘        │                    │
       │                 └─ feeder control   ├─ local state files
       ├─ AppDaemon MQTT controller          └─ State Agent HTTP API
       └─ patched go2rtc ───── LAN camera transport
```

See [Architecture](docs/architecture.md) for the trust and state-ownership
boundaries.

## Supported functionality

- Home Assistant MQTT discovery for feeder settings, schedules, diagnostics,
  feeding controls, and dispensing observations.
- Fresh dispensing-state reconstruction after add-on or Home Assistant restart.
- Persistent setting and feeding-plan verification against feeder-local state.
- PLAF203 H.264 SD/HD camera streaming through RTSP, WebRTC, and the go2rtc web
  interface; optional AAC is supported by the camera backend.
- Automatic feeder serial, camera UID, and LAN-address discovery.
- Signed, rollback-capable State Agent updates after initial bootstrap.
- Mandatory key-only SSH recovery access using a pinned, source-built Dropbear.

Current release constraints:

- Home Assistant add-on image: `amd64`.
- Tested feeder family: PLAF203; firmware variants may differ.
- The guided production bootstrap currently targets one feeder per add-on
  deployment. The controller can discover multiple MQTT identities, but the
  add-on currently has one State Agent token and the installer's protected
  token binding is single-feeder.
- The add-on remains marked `experimental`. The end-to-end bootstrap has been
  validated on firmware 3.1.48, but other firmware variants and every possible
  power-loss point have not been tested.

## Requirements

- A PLAF203 already onboarded to Wi-Fi and claimed in the Petlibro app.
- Home Assistant with an MQTT broker and MQTT integration, or a supported Linux
  host for the Docker deployment.
- A trusted LAN on which the feeder can reach the broker, Home Assistant, and
  the temporary installer host.
- A dedicated broker account for the backend. The installer captures the
  feeder's separate factory MQTT identity so it can also be authorized.
- A Linux x86_64 or ARM64 setup machine for initial no-UART bootstrap.
- Stable IPv4 addresses (or DHCP reservations) for the feeder, Home Assistant,
  setup machine, and broker.

The feeder uses plaintext MQTT on tested firmware. Keep it on a trusted or
isolated network and do not expose the broker, State Agent, go2rtc, or SSH
listeners directly to the Internet.

## Recommended installation

1. Add `https://github.com/tannerln7/Petlibro-Home-Assistant` to the Home
   Assistant app/add-on repository list and install **Petlibro Local backend**.
2. Create a broker account for the add-on and enter it in the add-on
   configuration. Leave the add-on stopped until bootstrap is ready.
3. On a trusted Linux machine, clone this repository and start the guided
   installer from its root:

   ```bash
   ./installer/install.sh
   ```

   The installer collects configuration, creates or imports an SSH public key,
   builds the State Agent and a pinned static Dropbear, prepares the payload,
   captures the feeder's factory broker account, and waits while you authorize
   that account. Normal installation continues in the same session. No UART,
   DNS override, Petlibro account token, or manually supplied feeder member ID
   is required.
4. Merge the generated `build/bootstrap/output/addon-options.patch.json` into
   the add-on configuration,
   start the add-on, reboot the feeder once after the add-on is ready, and
   verify State Agent reconciliation and camera discovery.

Installer downloads, source trees, compiler caches, generated keys, credentials,
and payloads remain under the repository's ignored `build/bootstrap/` directory.

The process intentionally preserves an OEM firmware donor slot until
installation succeeds, restores normal OEM firmware to both slots, and selects
OTA1 for production startup. Read the installer's safety and recovery notes
before proceeding.

A successful installation leaves the feeder running OEM firmware with:

- its MQTT endpoint set to the user-controlled broker;
- the authenticated, Home-Assistant-source-restricted State Agent;
- key-only Dropbear SSH recovery on TCP/2222; and
- generated add-on options containing the matching State Agent credentials.

See the [complete installation guide](docs/installation.md) and the
[installer reference](installer/README.md) before modifying a feeder.

## Runtime endpoints

With default host networking:

| Service | Default endpoint |
| --- | --- |
| go2rtc web/API | `http://HOME_ASSISTANT_HOST:1984/` |
| RTSP | `rtsp://HOME_ASSISTANT_HOST:8554/petlibro_plaf203_<serial>` |
| WebRTC | TCP and UDP port `8555` |
| State Agent | `http://FEEDER_IP:8765/` (bearer token and source-IP restricted) |

Camera sessions are lazy and begin when a consumer opens the generated stream.

## Documentation

### Users and operators

- [Installation](docs/installation.md)
- [Add-on configuration](addon/DOCS.md)
- [Configuration internals and advanced options](docs/configuration.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Docker / LXC deployment](docker/README.md)
- [Release history](addon/CHANGELOG.md)

### Components and interfaces

- [Home Assistant add-on](addon/README.md)
- [State Agent](state-agent/README.md)
- [Installer/bootstrap](installer/README.md)
- [MQTT camera contract](docs/mqtt-camera-contract.md)

### Development

- [Architecture](docs/architecture.md)
- [Development guide](docs/development.md)
- [Release process](docs/release-process.md)
- [Contributing](CONTRIBUTING.md)
- [Camera backend development](docs/camera-development.md)
- [Camera diagnostics](docs/camera-debugging.md)
- [Sanitized firmware 3.1.48 MQTT protocol reference](docs/protocol/mqtt-firmware-3.1.48.md)

## Security and privacy

Never commit or publish feeder MQTT credentials, State Agent tokens, private
signing keys, SSH private keys, serials, camera UIDs, raw packet captures, or
decrypted protocol dumps. Generated installer output is mode-restricted and
ignored by Git, but it remains sensitive.

The State Agent update design uses a compiled Ed25519 trust anchor, a detached
signature over exact manifest bytes, artifact hash/size verification, and a
fixed-path feeder-side transaction with rollback. The one-time OEM bootstrap is
a separate trust boundary and should run only on a trusted LAN.

## License

Repository packaging and documentation use the [MIT License](LICENSE). Bundled
go2rtc and the original AppDaemon controller retain the license files in their
component directories.
