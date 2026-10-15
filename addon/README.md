# Home Assistant add-on

This directory contains the production **Petlibro Local backend** Home
Assistant add-on. It combines five independently supervised services in one
image:

- a Python/AppDaemon controller for the PLAF203 MQTT protocol, Home Assistant
  discovery, commands, telemetry, schedules, and State Agent reconciliation;
- a maintained go2rtc fork that converts the feeder camera's local Petlibro
  transport into RTSP, WebRTC, and other go2rtc outputs.
- the Petlibro Management Node application and durable SQLite journal;
- a private native-TypeScript device-capability worker; and
- an Nginx gateway for Home Assistant Ingress and optional direct access.

The add-on is the Home Assistant side of the system. It does not modify feeder
firmware or install the State Agent. Use the [unified installer](../installer/README.md)
for a stock feeder.

## Runtime responsibilities

At startup the add-on renders AppDaemon and go2rtc configuration from Home
Assistant options, then starts both services under s6. The controller:

1. receives feeder MQTT traffic through the user's broker;
2. discovers and validates compatible feeders;
3. reconciles persistent settings and plans with the feeder-local State Agent;
4. publishes Home Assistant MQTT discovery and state;
5. sends validated feeder commands; and
6. coordinates signed State Agent updates when enabled.

Camera identity arrives through feeder MQTT events. The go2rtc fork then finds
the feeder on the LAN and opens the proprietary local camera transport only
when a consumer requests the stream.

Persistent feeder settings are never reconstructed from retained Home
Assistant state. They are read from the State Agent before publication or
write verification. Transient dispensing state is non-retained and is rebuilt
from a solicited live `ATTR_GET_SERVICE.motorState`; subsequent
`GRAIN_OUTPUT_EVENT` messages provide immediate transitions. Durable last-feed
observations remain retained.

## Configuration

The add-on's Home Assistant configuration schema is defined in
[`config.yaml`](config.yaml). Important groups include:

- backend MQTT connection and credentials;
- optional durable feeder MQTT endpoint migration;
- State Agent URL, bearer token, and update policy;
- feeder discovery and manual overrides;
- camera stream and protocol tuning; and
- management direct-access exposure; and
- logging and bounded diagnostic capture.

See [add-on options](DOCS.md) for the user-facing option reference and
[configuration internals](../docs/configuration.md) for generated files and
reconciliation behavior. Secrets belong in Home Assistant add-on options or a
local ignored configuration file, never in source control.

## Interfaces

The image normally exposes:

- `8099/tcp`: management Ingress listener, restricted to the Supervisor proxy;
- an optional configurable management direct-access TCP port (default `8098`);
- `1984/tcp`: go2rtc web interface and API;
- `8554/tcp`: RTSP;
- `8555/tcp` and `8555/udp`: WebRTC transports.

All services use host networking because feeder discovery and camera setup use
LAN broadcast/UDP traffic. Do not expose these ports to an untrusted network.
The management application itself provides neither authentication nor TLS.
Leave direct access disabled unless its allowed CIDR is a trusted LAN or an
externally authenticated reverse proxy.

## Development

Source areas:

- [`appdaemon/src`](appdaemon/src): controller implementation;
- [`appdaemon/tests`](appdaemon/tests): controller and protocol tests;
- [`go2rtc`](go2rtc): maintained camera transport fork;
- [`rootfs`](rootfs): s6 service definitions;
- [`templates`](templates): generated runtime configuration;
- [`render_config.py`](render_config.py): option-to-runtime renderer;
- [`Dockerfile`](Dockerfile): add-on image build.

Run the repository validation suite from the repository root:

```bash
./scripts/validate.sh
```

Focused Python tests can be run with:

```bash
python3 -m pytest addon/tests addon/appdaemon/tests -q
```

See the [development guide](../docs/development.md) for prerequisites and local
container workflows. Camera-specific work is documented in the
[camera development guide](../docs/camera-development.md).

## Constraints

- The packaged add-on currently targets `amd64`.
- The guided installer and add-on credential model currently support one feeder
  per add-on deployment. See the
  [installation guide](../docs/installation.md) before planning multiple
  feeders.
- The State Agent must be installed separately on the feeder.
- The broker-facing feeder account is distinct from the add-on account.
- The State Agent write/update APIs are deliberately narrow and authenticated.
- The camera implementation is firmware-specific; preserve packet and ACK
  invariants when changing it.

The add-on package metadata is under the repository's MIT license. The
AppDaemon controller retains its [Unlicense](appdaemon/LICENSE), and the
embedded go2rtc fork retains its [MIT license](go2rtc/LICENSE).
