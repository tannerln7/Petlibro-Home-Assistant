# MQTT camera runtime contract

This contract lets frontend integrations consume Petlibro camera runtime state
without depending on go2rtc APIs, logs, or internal files. Schema version 2 is
published by the backend add-on and is intended for the future HACS integration.

## Topics and delivery

With the default topic prefix, the backend publishes:

```text
petlibro_local/<product>/<serial>/camera/state
petlibro_local/<product>/<serial>/camera/availability
```

For example, documentation and tests use:

```text
petlibro_local/PLAF203/YOUR_DEVICE_SERIAL/camera/state
petlibro_local/PLAF203/YOUR_DEVICE_SERIAL/camera/availability
```

Both messages use QoS 1 and are retained. `availability` is `online` only when
the camera runtime status is `online`; every other state maps to `offline`.
State changes are published within the local polling interval, while unchanged
state and availability are refreshed at the configured heartbeat interval.

## State payload

```json
{
  "schema_version": 2,
  "product": "PLAF203",
  "serial": "YOUR_DEVICE_SERIAL",
  "stream_name": "petlibro_plaf203_your_device_serial",
  "status": "online",
  "requested_quality": "hd",
  "probe_resolution": {
    "width": 640,
    "height": 360,
    "profile_idc": 66,
    "level_idc": 30,
    "observed_at": "2026-01-01T00:00:00Z"
  },
  "actual_resolution": {
    "width": 1920,
    "height": 1080,
    "profile_idc": 66,
    "level_idc": 41,
    "observed_at": "2026-01-01T00:00:10Z"
  },
  "hd_transition": {
    "observed": true,
    "elapsed_ms": 10000
  },
  "rtsp_path": "petlibro_plaf203_your_device_serial",
  "rtsp_url_hint": "rtsp://<backend-host>:8554/petlibro_plaf203_your_device_serial",
  "last_update": "2026-01-01T00:00:10Z",
  "health": {
    "ffmpeg_errors": null,
    "gapped_idrs": 0,
    "dropped_frames": 0,
    "missing_fragments": 0,
    "ack_pending": 0,
    "extended_media_rejected": 0
  }
}
```

Important fields remain present with `null` when unknown. Consumers should
ignore fields they do not recognize so compatible fields can be added later.

## Field semantics

| Field | Meaning |
|---|---|
| `schema_version` | Contract version; currently `2`; version 2 removes the former configurable probe-wait field |
| `status` | `idle`, `starting`, `probing`, `online`, `offline`, or `error` |
| `requested_quality` | Configured `hd` or `sd` request, not measured resolution |
| `probe_resolution` | First valid SPS resolution observed in the current camera session |
| `actual_resolution` | Most recent valid SPS resolution observed in the session |
| `hd_transition` | Whether a later SPS reached at least 1920x1080 after a lower initial SPS, and elapsed milliseconds |
| `rtsp_path` | Stream identifier to append to the configured RTSP listener |
| `rtsp_url_hint` | Host-neutral display hint; it deliberately contains no LAN address |
| `last_update` | UTC ISO-8601 timestamp for the latest runtime state observation |
| `health` | Cumulative counters for the current camera session; `ack_pending` is a gauge |

`ffmpeg_errors` is `null` because go2rtc does not run or inspect a downstream
FFmpeg decoder. The other health fields come directly from the Petlibro media
assembler and receive-window counters.

## Offline and error behavior

go2rtc starts a camera producer lazily when a consumer opens the stream. Before
that happens, the status file may not exist and the backend publishes
`status: offline`. It also publishes offline when the last go2rtc update is
older than three metadata heartbeat intervals.

A malformed or unsupported internal status file produces `status: error`.
Missing, stale, or malformed files do not stop the PLAF203 controller. AppDaemon
allowlists every MQTT payload field, so unknown internal fields and legacy
configuration values cannot leak through the contract.

The JSON file under `/data` is an internal service boundary, not a frontend API.
Frontend integrations should consume only the MQTT topics documented here.

## Discovery readiness contract

Device setup progress is published separately as retained QoS 1 JSON:

```text
petlibro_local/<product>/<serial>/discovery/state
petlibro_local/discovery/devices
```

The per-device payload contains operational identity and readiness only. It
does not publish the camera UID, broker credentials, or product secrets.

```json
{
  "schema_version": 1,
  "product": "PLAF203",
  "serial": "YOUR_DEVICE_SERIAL",
  "mqtt_topic_root": "dl/PLAF203/YOUR_DEVICE_SERIAL/device",
  "uid_discovered": true,
  "uid_source": "DEVICE_START_EVENT.uuid",
  "ip_resolved": true,
  "ip_resolution_method": "lan_search3",
  "ip_address": "192.168.1.100",
  "stream_name": "petlibro_plaf203_your_device_serial",
  "stream_configured": true,
  "camera_online": false,
  "last_mqtt_seen": "2026-01-01T00:00:00Z",
  "last_ip_resolved": "2026-01-01T00:00:05Z",
  "last_update": "2026-01-01T00:00:10Z"
}
```

`petlibro_local/discovery/devices` wraps all current per-device payloads in a
`devices` array. Publish `all`, a serial, or `{"serial":"..."}` to
`petlibro_local/discovery/refresh` to request LAN address resolution.
