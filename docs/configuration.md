# Configuration reference

Home Assistant users configure the backend in the add-on options UI. Docker
users configure the same values in the ignored `docker/.env` file.

## Option mapping

| Home Assistant option                  | Docker variable                        | Consumer                                                      |
| -------------------------------------- | -------------------------------------- | ------------------------------------------------------------- |
| `mqtt_host`                            | `MQTT_HOST`                            | AppDaemon MQTT plugin only                                    |
| `mqtt_port`                            | `MQTT_PORT`                            | AppDaemon MQTT plugin only                                    |
| `mqtt_username`                        | `MQTT_USERNAME`                        | AppDaemon MQTT plugin                                         |
| `mqtt_password`                        | `MQTT_PASSWORD`                        | AppDaemon MQTT plugin secrets file                            |
| `mqtt_client_id`                       | `MQTT_CLIENT_ID`                       | Unique AppDaemon broker client ID                             |
| `persist_feeder_mqtt`                  | `PERSIST_FEEDER_MQTT`                  | Explicit feeder endpoint persistence gate                     |
| `feeder_mqtt_host`                     | `FEEDER_MQTT_HOST`                     | Broker address sent to the physical feeder only               |
| `feeder_mqtt_port`                     | `FEEDER_MQTT_PORT`                     | Broker port sent to the physical feeder only                  |
| `feeder_https_addr`                    | `FEEDER_HTTPS_ADDR`                    | Optional HTTPS value in an explicit endpoint update           |
| `petlibro_state_agent_url`             | `PETLIBRO_STATE_AGENT_URL`             | Optional feeder truth API URL or `{ip}` template              |
| `petlibro_state_agent_token`           | `PETLIBRO_STATE_AGENT_TOKEN`           | Bearer token for the feeder truth API                         |
| `petlibro_state_agent_timeout_seconds` | `PETLIBRO_STATE_AGENT_TIMEOUT_SECONDS` | Per-request state API timeout                                 |
| `state_agent_updates`                  | `STATE_AGENT_UPDATES_JSON`             | Optional signed State Agent release configuration object      |
| `device_discovery`                     | `DEVICE_DISCOVERY`                     | MQTT identity discovery coordinator                           |
| `product_filter`                       | `PRODUCT_FILTER`                       | Accepted feeder topic product                                 |
| `lan_cidr`                             | `LAN_CIDR`                             | UID-specific LAN_SEARCH3/KNOCK2 camera-address search network |
| `ip_resolve_timeout_seconds`           | `IP_RESOLVE_TIMEOUT_SECONDS`           | Per-search timeout                                            |
| `ip_discovery_broadcast_seconds`       | `IP_DISCOVERY_BROADCAST_SECONDS`       | Broadcast-first stage duration                                |
| `ip_discovery_max_unicast_per_second`  | `IP_DISCOVERY_MAX_UNICAST_PER_SECOND`  | Paced subnet fallback rate                                    |
| `ip_refresh_interval_minutes`          | `IP_REFRESH_INTERVAL_MINUTES`          | Healthy address refresh age                                   |
| `ip_retry_backoff_seconds`             | `IP_RETRY_BACKOFF_SECONDS`             | Failed-search retry floor                                     |
| `devices`                              | `DEVICES_JSON`                         | Optional manual device override array                         |
| `go2rtc_stream_name`                   | `GO2RTC_STREAM_NAME`                   | go2rtc stream and RTSP URL                                    |
| `camera_quality`                       | `CAMERA_QUALITY`                       | Petlibro URL `quality` query                                  |
| `go2rtc_api_port`                      | `GO2RTC_API_PORT`                      | go2rtc API listener                                           |
| `go2rtc_rtsp_port`                     | `GO2RTC_RTSP_PORT`                     | go2rtc RTSP listener                                          |
| `go2rtc_webrtc_port`                   | `GO2RTC_WEBRTC_PORT`                   | go2rtc WebRTC listener                                        |
| `publish_camera_metadata`              | `PUBLISH_CAMERA_METADATA`              | go2rtc status export and AppDaemon MQTT publisher             |
| `camera_metadata_topic_prefix`         | `CAMERA_METADATA_TOPIC_PREFIX`         | Optional MQTT camera topic override                           |
| `camera_metadata_interval_seconds`     | `CAMERA_METADATA_INTERVAL_SECONDS`     | Retained state/availability heartbeat and staleness basis     |
| `log_level`                            | `LOG_LEVEL`                            | Petlibro application logging threshold                        |
| `verbose_logs`                         | `VERBOSE_LOGS`                         | Deprecated compatibility switch                               |
| `enable_debug_dumps`                   | `ENABLE_DEBUG_DUMPS`                   | Decrypted protocol dump output                                |

See [the add-on option guide](../addon/DOCS.md) for defaults and
behavior.

### Removed camera experiments

Upgrades may leave `send_delay_ctrl`, `hd_probe_wait_ms`,
`streamctrl_variant`, or `streamctrl_quality` in an existing `options.json` or
Compose environment. The renderer ignores unknown persisted keys and no longer
emits those URL parameters; removed environment variables are likewise
ignored. The add-on schema no longer presents them. If Home Assistant reports
an obsolete-option warning while importing an older backup, save the current
options once to discard the stale keys.

The supported camera surface now expresses only `camera_quality` (`hd` or
`sd`). That is a request to the physical device, not a guarantee of the SPS
resolution that will be observed.

## Generated files

| Path                                         | Contents                                                  |
| -------------------------------------------- | --------------------------------------------------------- |
| `/data/go2rtc.yaml`                          | Listener configuration and generated Petlibro stream URL  |
| `/data/appdaemon.yaml`                       | AppDaemon core, namespace, and MQTT plugin configuration  |
| `/data/apps.yaml`                            | Discovery coordinator and one PLAF203 app per device      |
| `/data/appdaemon-secrets.yaml`               | MQTT credentials and feeder state-agent bearer token      |
| `/data/devices.json`                         | Atomic mode-0600 device identity/address registry         |
| `/data/appdaemon/`                           | AppDaemon app links and persistent namespace state        |
| `/data/petlibro_camera_status_<stream>.json` | Per-stream atomic go2rtc camera runtime status; mode 0600 |
| `/data/petlibro_c2d_<stream>.dat`            | Optional decrypted client-to-device dump                  |
| `/data/petlibro_d2c_<stream>.dat`            | Optional decrypted device-to-client dump                  |

Generated YAML and secrets files are written with mode 0600. Do not copy them
into Git or attach them to public issues.

The generated AppDaemon configuration explicitly marks the `plaf203` namespace
as persistent with safe writeback. The manual-feed portion preference and a
diagnostic copy of the last verified feeder truth survive an add-on restart.
That copy is never used to generate a device write or feeding-plan response;
fresh feeder state from `/v1/core` is authoritative.

## Feeder state API and reconciliation

Persistent settings and feeding plans use the feeder-side State Agent snapshot
API.
When `petlibro_state_agent_url` is empty, the renderer derives
`http://<discovered-feeder-ip>:8765` for each device. A custom URL may include
one `{ip}` placeholder. A fixed URL is suitable only when one feeder is
configured. The URL must use HTTP or HTTPS and cannot contain credentials,
query parameters, or a fragment.

Set `petlibro_state_agent_token` to the bearer token configured in the feeder
agent. The renderer places it in the mode-0600 AppDaemon secrets file and
references it with `!secret`; application logs never include it. The default
request timeout is two seconds.

The backend requires the current tracked State Agent schema. That agent rejects
any `state.bin` whose length is not exactly 236 bytes and labels decoded fields
as `persistent`, `effective_cached`, or `runtime`. Only persistent fields can
complete a setting verification; firmware-calculated cached enable flags and
runtime telemetry cannot.

### State Agent updates

`state_agent_updates` is disabled by default. Enable it only after bootstrapping
a compatible State Agent and its runit update supervisor on the feeder. Use the
[unified installer](../installer/README.md) or an authorized shell for a manual
development installation; the running add-on's
signed-update API cannot install that initial feeder-side service. The object
has these fields:

```yaml
state_agent_updates:
  enabled: true
  manifest_url: https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/state-agent-releases/state-agent/latest.json
  check_on_connect: true
  check_interval_hours: 24
```

`manifest_url` is required when enabled and must be HTTPS without credentials,
query parameters, or a fragment. `check_interval_hours` is an integer from 1
through 168. `check_on_connect` permits a throttled check after a usable feeder
connection or Home Assistant birth. The Home Assistant MQTT discovery surface
adds a firmware Update entity and a separate diagnostic **Check for State Agent
Updates** button. Install and check commands ignore retained MQTT messages.

The add-on verifies a detached Ed25519 signature over the exact `latest.json`
bytes before parsing it, then verifies the downloaded artifact hash and size.
The manifest must identify `plaf203-state-agent`, stable channel, API versions
1, `linux-armv7-eabihf`, and a strictly valid SemVer version. The artifact and
release URLs must both use HTTPS. The feeder receives only the already verified
binary frame and never accesses the release URL. While a submitted update is in
progress, the controller queries only `/v1/update-status` and `/v1/version` on
the local feeder.

On a feeder startup event or first heartbeat, the controller reads `/v1/core`
before accepting a persistent write. It then mirrors feeder settings and
semantic plan records into Home Assistant. Subsequent heartbeats use `/v1/rev`
and fetch the full state only when `core_rev` changes, during reconnect, or to
verify a pending write.

Persistent writes follow `READY -> PENDING_WRITE -> VERIFYING_WRITE`. An MQTT
acknowledgement only confirms protocol receipt. The controller reads
`/v1/core` after the acknowledgement and commits the result only when the
feeder-local value matches. A mismatch enters `DIVERGED`, republishes the
actual feeder value, and returns to `READY`. If the API is unavailable, the
controller remains outside `READY` and blocks persistent writes rather than
falling back to retained MQTT, Home Assistant, or AppDaemon storage.

### Feeding-plan edits

Home Assistant exposes plan IDs 1 through 9. Stock Petlibro software may create
the same records with large opaque IDs. During reconciliation, the controller
reserves every existing ID already in the 1-9 range, then maps noncanonical
records in feeder record order to the lowest free IDs. The proposed mapping is
logged explicitly and is executed through the same serialized MQTT
acknowledgement and State Agent readback path as a user-originated persistent
write. A successful readback must contain the mapped IDs and preserve every
known persistent field.

Automatic normalization is deliberately fail-closed. It is refused for more
than nine or duplicate records, unsupported raw flag values, inconsistent
derived fields, a non-idle runtime execution state, noncanonical weekday
ordering, or any nonzero ten-byte opaque tail. The MQTT plan schema cannot
carry that tail or execution state, so rewriting such a record cannot be
demonstrated safe. Refusal leaves feeder truth unchanged and is reported in the
add-on log with the proposed mapping and reason. Runtime `execution_state`,
derived local time, and the firmware update marker `sync_time` may differ on
readback if feeder activity begins after the command; normalization regenerates
`sync_time` only for records whose IDs change.

Before every Home Assistant plan command the controller performs a fresh
`/v1/core` preflight and builds one full-collection MQTT payload from that
response. If the requested ID exists, it mutates only that record's UTC hour,
minute, weekday set, portions, derived one-shot flag, and update timestamp. If
the ID is absent, it appends a new record with safe feeder-field defaults: Meal
Call disabled, zero audio repetitions, zero skip end time, no opaque tail, and
idle execution state. Clearing a schedule field to an exactly empty value
deletes that ID from the fresh collection; whitespace, `null`, and malformed
JSON are rejected rather than interpreted destructively.

For existing records, `enable_audio_raw` passes through as the existing
`enableAudio` field and must be 0 or 1; `audio_times` and the 64-bit
`skip_end_time` pass through unchanged. The ten-byte opaque tail remains in the
coordinator's cloned truth model and must remain unchanged in the post-write
readback; the current MQTT schema has no field that exposes it. Runtime
`execution_state` and regenerated `sync_time` are excluded from semantic
schedule equality.

After the acknowledgement, verification requires exactly the expected plan
count and IDs and the requested target update, creation, or deletion. Every
pre-existing non-target record must remain byte-semantically equivalent. An
update also requires unchanged target opaque fields. `GET_FEEDING_PLAN_EVENT`
performs a fresh core
read. If that read fails, the controller sends no application response rather
than fabricating a schedule. AF203 firmware ignores the response `code` when a
`plans` member is present, so even an error response containing `plans: []`
would authoritatively clear the feeder's persisted plans.

The state API exposes `audio_url`, but audio URL writes remain blocked because
tested firmware can restart when given an unreachable URL. The Meal Call text
entity mirrors that feeder-owned URL instead of retaining a Home Assistant
value. Enabling Meal Call performs a fresh core read and sends the persistent
`audio_url` together with `enableAudio`, because firmware does not enable audio
when the URL is omitted. The existing text entity displays that value, but the
backend continues to reject URL edits.

The former "automatic button lock" controls keep their MQTT entity IDs for
compatibility, but now mirror the binary-backed `auto_change_mode` and
`auto_threshold` configuration fields without claiming an unproven lock
meaning. Manual feeding is an action and continues to use its MQTT
acknowledgement/event path instead of expecting a persistent `/v1/core` change.
Explicit feeder MQTT endpoint persistence remains a separately gated recovery
operation and is reported as acknowledged-but-not-locally-verifiable because
`/v1/core` does not expose endpoint configuration.

### Dispensing and last-feed state

Current dispensing state is live runtime data, not persistent feeder
configuration. On startup, reconnect, or Home Assistant birth, the controller
uses a correlated solicited `ATTR_GET_SERVICE.motorState` response to publish
Idle, Dispensing, or Recovering. Grain events then provide immediate
Dispensing, Blocked, and Idle transitions. Unknown values, malformed replies,
timeouts, and disconnects make only this entity unavailable; they never infer
Idle from stale state.

`food_output/progress` and its dedicated availability topic are non-retained.
The last dispense started, completed, portions, and source topics are retained
because they are historical observations. A new installation therefore shows
those four entities as unknown until the first completed feed unless the broker
already holds valid retained observations.

## Device discovery and overrides

The coordinator builds `serial -> UID -> IP -> stream` from feeder MQTT traffic
and the LAN_SEARCH3/KNOCK2 probe. A valid device gets a direct-IP URL, so normal
playback does not repeat the slower subnet scan. It periodically refreshes
cached addresses
and re-resolves after camera status becomes stale, offline, or error. A failed
lookup retains the last address, marks readiness false, and retries with
backoff. go2rtc restarts only when the rendered config bytes change.

Each lookup uses one absolute deadline and a staged target order: cached IP,
global and directed broadcasts, other known registry addresses, then one
rate-limited subnet sweep. Each target gets both LAN_SEARCH3 legs followed by
KNOCK2; KNOCK_RR2 must echo the expected UID and nonce. The receiver runs
throughout the send stages. The resolver subprocess runs in AppDaemon's
executor, and only its short completion callback mutates the registry and
generated configuration.

For Docker, `DEVICES_JSON` is a JSON array. For Home Assistant, use the
structured `devices` list. Example advanced override:

```yaml
devices:
  - name: kitchen_feeder
    product: PLAF203
    serial: YOUR_DEVICE_SERIAL
    uid: YOUR_DEVICE_UID
    ip_address: 192.168.1.100
```

Omit the list for normal automatic setup.

The controller can discover multiple MQTT identities, but the current add-on
schema supplies one State Agent bearer token to every generated controller.
The guided installer binds its generated token to one feeder. The supported
guided deployment is therefore one feeder per add-on instance; multi-feeder
State Agent authentication requires deliberate manual provisioning with a
shared token and is not covered by the installer.

The image includes the same resolver used by the coordinator:

```bash
petlibro-resolve \
  --uid YOUR_DEVICE_UID \
  --subnet 192.168.1.0/24 \
  --timeout 15s \
  --broadcast-duration 2s \
  --max-unicast-per-second 32 \
  --json
```

It always emits one JSON result for a valid invocation. The result includes
`resolved`, nullable `ip_address`, `method`, `elapsed_ms`, `error_code`, and
`stats`. Per-leg fields are `lan_search_w3_1_sent`,
`lan_search_w3_2_sent`, `knock2_sent`, `lan_search_r_received`, and
`knock_rr2_received`. Rejection fields are `wrong_uid_rejected`,
`nonce_mismatch_rejected`, and aggregate `responses_rejected`. The remaining
aggregate fields are `broadcasts_sent`, `unicasts_sent`, `packets_received`,
`send_errors`, and `deadline_exceeded`. The send-leg fields count successful
UDP writes; broadcast/unicast fields count logical target exchanges. It exits
nonzero when resolution fails. The coordinator invokes it without a shell and
does not log the UID or raw result.

## Logging

`log_level` accepts `critical`, `error`, `warning`, `info`, `debug`, and
`trace`; the default is `info`. `debug` is intended for ongoing troubleshooting
and excludes raw MQTT payloads and per-packet camera traces. `trace` enables
those high-volume diagnostics and should normally be used only for a short
reproduction. The legacy `verbose_logs=true` setting migrates the normal
`info` setting to `debug`; set it to `false` after choosing `log_level`.

`enable_debug_dumps` does not follow `log_level`. It separately records
decrypted C2D and D2C protocol data under `/data`; treat those files as
sensitive.

## Camera metadata topics

Metadata publishing is enabled by default. When
`camera_metadata_topic_prefix` is empty, the renderer derives:

```text
petlibro_local/<product>/<serial>/camera
```

The default 30-second heartbeat refreshes retained state and availability.
AppDaemon considers the go2rtc status stale after three heartbeat intervals.
The supported interval range is 5–300 seconds. A custom prefix may contain only
letters, numbers, underscores, hyphens, and slash-separated path segments; MQTT
wildcards are rejected.

See the [MQTT camera contract](mqtt-camera-contract.md) before implementing a
consumer. The generated status file is internal and should not be parsed by a
frontend integration.

## Feeder connectivity

The normal running backend does not onboard the feeder or configure its factory
MQTT account. The feeder must already be connected to Wi-Fi, its MQTT hostname
must resolve or point to the local broker, and its own factory credential must
be provisioned in that broker. Do not enter the feeder credential as the
backend's MQTT password. The [unified installer](../installer/README.md)
captures that CONNECT identity from an already claimed feeder and persists a
direct local-broker endpoint before the backend takes over.

The configured `mqtt_username` and `mqtt_password` authenticate AppDaemon. That
account must be authorized to subscribe and publish on both the feeder's
`dl/PLAF203/...` topics and the controller's Home Assistant discovery/command
topics.

### Feeder MQTT endpoint persistence

`mqtt_host` and `mqtt_port` only tell AppDaemon how to reach the broker. They
are never copied into the physical feeder's configuration. By default,
`persist_feeder_mqtt` is false and the controller acknowledges
`DEVICE_START_EVENT` without sending `DEVICE_CONFIG_SYNC`, preserving the
feeder's existing MQTT and HTTPS endpoints.

The unified installer intentionally generates an add-on patch with this option
enabled for the initial handoff. This lets the first reconciled add-on startup
confirm or repair the final broker endpoint. After the matching acknowledgement
appears in the log, the operator may disable it so later feeder boots return to
preserve-only behavior.

Endpoint persistence is an advanced recovery/migration operation. To use it,
set `persist_feeder_mqtt: true`, provide a feeder-routable
`feeder_mqtt_host` and `feeder_mqtt_port`, restart the add-on, then reboot the
feeder so it emits a new startup event. The host must be an IP address or
multi-label DNS name. Before enabling the update, the
controller resolves it from the add-on, rejects loopback, link-local,
multicast, unspecified, and known Home Assistant internal addresses, then
opens a bounded TCP connection to the configured port. A validation failure is
logged and blocks the update without stopping the other backend services.

Do not use `core-mosquitto`: it is resolvable inside Home Assistant but not by
the physical feeder. Prefer a stable LAN IP or LAN DNS name whose record will
survive add-on, broker, and router changes. `feeder_https_addr` is optional; an
empty value is omitted from the update rather than being copied from another
setting. Current observed startup messages do not report the feeder's existing
endpoint values, so preservation means not sending an endpoint update when
those values cannot be read.
