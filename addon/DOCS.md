# Petlibro Local add-on options

The add-on generates go2rtc and AppDaemon configuration in `/data` each time a
service starts. Users should edit options through Home Assistant rather than
changing files inside the container.

For a stock feeder, complete the repository's
[unified installation flow](../docs/installation.md) before configuring these
options. The installer provisions the feeder-side broker settings and State
Agent that the add-on expects.

## Installation and updates

The repository configuration points Supervisor at the prebuilt amd64 image:

```text
ghcr.io/tannerln7/ha-addon-petlibro-local:<add-on-version>
```

Normal installation and updates pull that image instead of compiling go2rtc
and installing AppDaemon dependencies on the Home Assistant machine. The GHCR
packages must be public so Supervisor can pull them without registry
credentials.

Local builds are intended only for development. In a copied local add-on,
comment out the `image:` field in `config.yaml` to make Supervisor build the
Dockerfile. Such builds can consume all available CPU temporarily, particularly
on Raspberry Pi-class and other low-resource installations.

## Discovery-first setup

| Option                                | Default          | Description                                                                         |
| ------------------------------------- | ---------------- | ----------------------------------------------------------------------------------- |
| `device_discovery`                    | `true`           | Observe supported feeder MQTT topics and configure discovered devices               |
| `product_filter`                      | `PLAF203`        | Supported product family; currently fixed to PLAF203                                |
| `lan_cidr`                            | `192.168.1.0/24` | IPv4 network searched by the UID-specific LAN probe; maximum size `/16`             |
| `ip_resolve_timeout_seconds`          | `15`             | Timeout for each UID-to-IP lookup; covers one paced `/24` sweep at the default rate |
| `ip_discovery_broadcast_seconds`      | `2`              | Time reserved for low-impact broadcast discovery before fallback                    |
| `ip_discovery_max_unicast_per_second` | `32`             | Maximum paced fallback probes per second                                            |
| `ip_refresh_interval_minutes`         | `360`            | Maximum age of a healthy cached address                                             |
| `ip_retry_backoff_seconds`            | `60`             | Minimum delay after a failed lookup                                                 |
| `devices`                             | `[]`             | Optional advanced manual overrides                                                  |

Normal setup does not require a serial, UID, or IP address. Start the backend,
then reboot the feeder so the coordinator can observe `DEVICE_START_EVENT`.
The generated stream appears after serial, UID, and LAN address discovery.
If the feeder topic is observed without its non-retained startup UID, the
add-on logs one request to power-cycle the feeder after the add-on is
subscribed. Once learned, the registry UID is passed into dynamically rendered
controllers so retained Home Assistant identity does not depend on the
controller having observed the original startup event.
The resolver checks the cached address first, starts its receiver before
sending broadcast probes, tries other known Petlibro addresses, and scans the
configured subnet at most once. Each target receives the firmware-required
`LAN_SEARCH3(w3=1)`, `LAN_SEARCH3(w3=2)`, `KNOCK2` sequence; a matching
`KNOCK_RR2` UID and nonce identifies the feeder. All stages share one absolute
deadline.

An override item may contain `name`, `product`, `serial`, `uid`, and
`ip_address`. `serial` identifies the record; supplied UID/IP fields win over
automatic values. Existing installs with the former top-level `serial`, `uid`,
and `device_ip` options are migrated into this registry when all three exist.

## MQTT

| Option                                     | Default                  | Description                                                                              |
| ------------------------------------------ | ------------------------ | ---------------------------------------------------------------------------------------- |
| `mqtt_host`                                | `core-mosquitto`         | Broker hostname or address used by AppDaemon only                                        |
| `mqtt_port`                                | `1883`                   | Broker TCP port used by AppDaemon only                                                   |
| `mqtt_username`                            | empty                    | AppDaemon's broker username                                                              |
| `mqtt_password`                            | empty                    | AppDaemon's broker password; stored in a mode-0600 secrets file                          |
| `mqtt_client_id`                           | `petlibro_local_backend` | Unique MQTT client ID for this backend instance                                          |
| `persist_feeder_mqtt`                      | `false`                  | Opt in to writing a validated broker destination to the physical feeder                  |
| `feeder_mqtt_host`                         | empty                    | Stable LAN IP or multi-label DNS name reachable by the feeder                            |
| `feeder_mqtt_port`                         | `1883`                   | Feeder-facing broker TCP port                                                            |
| `feeder_https_addr`                        | empty                    | Optional HTTPS endpoint included only in an explicit feeder update                       |
| `petlibro_state_agent_url`                 | empty                    | Optional state API URL; empty derives `http://<feeder-ip>:8765`, and `{ip}` is supported |
| `petlibro_state_agent_token`               | empty                    | Bearer token configured in the feeder-side State Agent                                   |
| `petlibro_state_agent_timeout_seconds`     | `2`                      | Local API request timeout, from 1 to 10 seconds                                          |
| `state_agent_updates.enabled`              | `false`                  | Enable signed State Agent release checking after feeder-side bootstrap                   |
| `state_agent_updates.manifest_url`         | signed project feed      | HTTPS `latest.json` URL used when updates are enabled                                     |
| `state_agent_updates.check_on_connect`     | `true`                   | Allow a throttled release check after connection or Home Assistant birth                 |
| `state_agent_updates.check_interval_hours` | `24`                     | Periodic check interval, from 1 to 168 hours                                             |

These credentials authenticate the backend to the broker. They do not provision
the feeder or replace its factory-provisioned broker credentials. The feeder's
own username and product-secret-based password must be configured as a separate
account in the external broker.

The first five MQTT options configure only AppDaemon's broker connection.
`mqtt_host` is deliberately never reused as the physical feeder's destination.
The add-on default leaves `persist_feeder_mqtt` disabled, logs that the existing
feeder configuration is being preserved, and does not send
`DEVICE_CONFIG_SYNC` at startup. The unified installer is the exception: its
generated options patch enables this gate for the first local-broker handoff.

The guided installer currently supports one feeder per add-on deployment. The
controller can discover multiple feeder identities, but this add-on supplies
one State Agent token to all generated controllers. See the
[installation guide](../docs/installation.md) before attempting a manual
multi-feeder deployment.
After the add-on logs the matching acknowledgement, it may be disabled again to
preserve the established endpoint on later feeder boots.

Only enable persistence when intentionally migrating or recovering the
feeder's stored broker address. Restart the add-on after saving the options,
then reboot the feeder so it emits the startup event that gates the update. The
add-on rejects single-label
container names such as `core-mosquitto`, unsafe special-use addresses, known
Home Assistant internal networks, DNS failures, and destinations with no
reachable listener on `feeder_mqtt_port`. A failure blocks the device update
but leaves discovery, control, and camera services running. When enabled and
validated, the log records the configured destination, the send attempt, and
the feeder's acknowledgement. Leave `feeder_https_addr` empty to omit that
field from the update.

## Camera and go2rtc

| Option               | Default           | Description                                                                                      |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------ |
| `go2rtc_stream_name` | `petlibro_feeder` | Compatibility name for a migrated legacy device; discovered devices derive a product/serial name |
| `camera_quality`     | `hd`              | Requested `hd` or `sd` stream                                                                    |
| `go2rtc_api_port`    | `1984`            | Web interface and API TCP port                                                                   |
| `go2rtc_rtsp_port`   | `8554`            | RTSP TCP port                                                                                    |
| `go2rtc_webrtc_port` | `8555`            | WebRTC TCP/UDP port                                                                              |

The tested HD stream may first emit a 640x360 SPS and switch to 1920x1080 later.
The camera adapter automatically waits up to 15 seconds for that transition.
Some observed sessions transitioned after several minutes and were initially
advertised as 640x360. `camera_quality` is the request sent to the device;
runtime `actual_resolution` is the observed SPS and is not forced by that
request.

## Home Assistant controls

The controller publishes readable entity names and select choices through MQTT
discovery. These labels are presentation-only: existing entity unique IDs,
MQTT topics, and protocol payloads do not change. In particular, Home Assistant
shows **Always active** / **Scheduled** for the protocol aging modes, **720p** /
**1080p** for camera resolution, and **Single bowl** / **Dual bowl** for bowl
setup while MQTT continues to carry the original enum values.

Persistent controls become available only after the controller has read the
feeder-side state API and mirrored its `/v1/core` result. Home Assistant state
is not used as feeder truth. If the API becomes unreachable, the controller
keeps MQTT heartbeat handling active but blocks setting and plan writes until a
fresh reconciliation succeeds.

Install the matching State Agent build from the repository's
[`state-agent/`](../state-agent/README.md) component. It classifies fields as persistent,
effective-cached, or runtime; writable Home Assistant entities verify only the
persistent field. Cached `enableX` bytes remain diagnostic even if their value
temporarily differs from the user's persistent switch.

State Agent OTA is optional and does not replace its initial feeder
installation through the [unified installer](../installer/README.md). After its
runit supervisor is bootstrapped, enabling
`state_agent_updates` adds a firmware Update entity and a separate **Check for
State Agent Updates** button. The add-on validates signed HTTPS release metadata
and uploads the artifact to the authenticated feeder API. It never asks the
feeder to download a URL. See the [configuration reference](../docs/configuration.md#state-agent-updates)
and [State Agent guide](../state-agent/README.md#signed-updates).

The default manifest is the project's signed stable feed:

```text
https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/state-agent-releases/state-agent/latest.json
```

The nine **Feeding schedule** text entities accept flat JSON. Each document's
`id` must match its displayed slot number. Editing an existing ID updates it;
submitting a valid missing ID creates that plan with conservative defaults for
feeder-owned metadata. Clear the field completely and save it to delete that
slot. Only an exactly empty value means delete; whitespace, `null`, and malformed
JSON are rejected.
The user-editable fields are `execution_time`, `scheduled_days`, and
`grain_num`; legacy audio fields are ignored because their meanings are not
verified. Every edit preflights a fresh full plan collection, carries
`skip_end_time` and audio transport values through the MQTT schema, retains the
opaque tail in its cloned truth model, and sends the entire collection. The
post-ack readback requires that opaque tail and all non-target plans to remain
unchanged. Runtime plan execution state and the regenerated target `syncTime`
do not cause false divergence. A retained or stored Home Assistant value is
never used to construct the command. Deletion likewise starts from fresh feeder
truth, removes only the selected ID, and verifies that every remaining record
is unchanged; deleting the last slot sends an authoritative empty collection.

## Camera metadata publishing

| Option                             | Default | Description                                                               |
| ---------------------------------- | ------- | ------------------------------------------------------------------------- |
| `publish_camera_metadata`          | `true`  | Publish retained camera state and availability for frontend integrations  |
| `camera_metadata_topic_prefix`     | empty   | Optional prefix; empty derives `petlibro_local/<product>/<serial>/camera` |
| `camera_metadata_interval_seconds` | `30`    | Retained heartbeat interval; allowed range 5–300 seconds                  |

The backend marks metadata stale after three heartbeat intervals. It publishes
state at `<prefix>/state` and availability at `<prefix>/availability`, both
retained with QoS 1. See the [versioned MQTT contract](../docs/mqtt-camera-contract.md)
for payload fields and offline behavior.

## Diagnostics

| Option               | Default | Description                                                                    |
| -------------------- | ------- | ------------------------------------------------------------------------------ |
| `log_level`          | `info`  | `critical`, `error`, `warning`, `info`, `debug`, or `trace`                    |
| `verbose_logs`       | `false` | Deprecated compatibility key; `true` migrates normal `info` logging to `debug` |
| `enable_debug_dumps` | `false` | Writes decrypted C2D/D2C protocol dumps under `/data`                          |

`info` contains operational milestones. `debug` adds bounded resolver stats,
registry changes, camera metadata transitions, and five-second go2rtc summaries
without raw MQTT payloads. `trace` enables raw MQTT messages and the existing
targeted packet, ACK, fragment, and frame-info traces and can be extremely
noisy. AppDaemon itself remains at info level so its scheduler and state engine
do not overwhelm application diagnostics.

Debug dumps are independent of logging and may contain device and session
data. Disable the option and delete the files after collecting the evidence
needed for diagnosis.

## Network exposure

Host networking is required for reliable UDP camera discovery and WebRTC. The
generated go2rtc API and RTSP listeners do not use authentication. Restrict
ports 1984, 8554, and 8555 to trusted clients with network policy or a firewall.
