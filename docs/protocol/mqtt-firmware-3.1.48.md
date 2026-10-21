# PLAF203 firmware 3.1.48 MQTT capture analysis

## Executive summary

This capture records a PLAF203/AF203 feeder with hardware `1.0.7` and software
`3.1.48` using plaintext MQTT 3.1 (`MQIsdp`, protocol level 3) on TCP port 1883.
The topic grammar remains:

```text
dl/PLAF203/<device-serial>/device/<channel>/<direction>
```

`post` means feeder-to-server and `sub` means server-to-feeder. The capture
contains 172 JSON PUBLISH messages covering 12 commands.

The most consequential findings for the daemon are:

- `GET_FEEDING_PLAN_EVENT` responses go to `event/sub`, not `service/sub`.
- `GRAIN_OUTPUT_EVENT` acknowledgements go to `event/sub`, not `service/sub`.
- Firmware 3.1.48 uses `FEEDING_PLAN_SERVICE`, a top-level `plans` array, and a
  per-request response that returns `code` plus `planId`/`syncTime` pairs.
- Startup includes a `DEVICE_CONFIG_SYNC` request on `service/sub`, followed by
  a `service/post` JSON acknowledgement. The one capture proves this is part of
  the 3.1.48 cloud boot sequence, but not that the feeder refuses to operate
  without it.
- Both `NTP` and server-originated `NTP_SYNC` carry current timezone and two DST
  transition records: `timezone`, `timezoneOffsetSeconds`,
  `nextDSTOffsetSeconds`, `nextDSTTransitionTs`,
  `secondNextDSTOffsetSeconds`, and `secondNextDSTTransitionTs`.
- Heartbeats need MQTT QoS handling only. No JSON heartbeat response occurred.
- `MANUAL_FEEDING_SERVICE` has exactly the expected request shape: `cmd`, `ts`,
  `msgId`, and `grainNum`.
- Most feeder PUBLISH messages use QoS 1 and receive a topicless PUBACK from the
  server. Server PUBLISH messages use QoS 0. Application JSON responses are a
  separate layer and remain necessary for several commands.
- `DEVICE_LOG_REPORT_EVENT` is new to the current daemon. It is published at
  QoS 0 and had no MQTT PUBACK or application response in this capture.

No credentials, personal identifiers, or camera authorization data are
reproduced here. The source capture artifacts contained a real MQTT CONNECT
password and device/network identifiers, so they remain local and are excluded
from version control. Tests use the sanitized synthetic fixture in
`addon/appdaemon/tests/fixtures/protocol_3_1_48.sample.jsonl`.

## Local capture files inspected

The following source artifacts were used during the original analysis. They are
not part of this repository and their names are listed only to document the
evidence behind the findings.

| File | Use | Result |
|---|---|---|
| `capture/petlibro_real_mqtt_mitm.pcap` | Authoritative packet boundaries, timestamps, MQTT flags, topics, and payloads | 470 packets over 360.241 seconds, from `09:35:27` through `09:41:27` UTC |
| `capture/petlibro_mqtt_decoded.tsv` | Fast decoded-field inventory and cross-check | 272 tshark frame rows; fields were usable, but two TCP frames contain multiple MQTT packets |
| `capture/petlibro_socat_hex.log` | Raw direction and byte confirmation | 6,074 lines; confirmed feeder/server socket direction, CONNECT bytes, and coalesced MQTT frames |

The local extraction pass identified the feeder from its MQTT CONNECT packet,
selected JSON PUBLISH messages, mapped `post`/`sub`, and recursively redacted
keys that looked like passwords, secrets, tokens, or authorization data. It
also redacted Wi-Fi SSID and account member ID values. The raw and extracted
capture data were deliberately deleted after the implementation was validated.

## Transport behavior

- The feeder attempted CONNECT at `09:35:28.367740Z` and received no MQTT
  response in the capture. It retried at `09:36:27.168459Z`.
- CONNECT used the device serial as the client ID, clean-session false, keepalive
  90 seconds, and username/password authentication. Credentials are redacted.
- The second CONNECT was accepted. The raw CONNACK was `20 02 01 00`.
- A server `FEEDING_PLAN_SERVICE` PUBLISH followed in the same TCP frame as the
  CONNACK, before the feeder issued this connection's SUBSCRIBE packets. Because
  clean-session was false, this is consistent with resumed broker state, though
  the capture alone does not prove why the message was delivered then.
- The feeder subscribed at requested QoS 1, in order, to `ntp/sub`, `ota/sub`,
  `broadcast/sub`, `config/sub`, `event/sub`, `service/sub`, and `system/sub`.
  Every SUBSCRIBE received SUBACK.
- There were 83 feeder QoS 1 PUBLISH packets and exactly 83 server PUBACKs.
  Ten `DEVICE_LOG_REPORT_EVENT` packets were feeder QoS 0. All 79 server
  PUBLISH packets were QoS 0, so the feeder sent no PUBACKs for them.
- One MQTT PINGREQ/PINGRESP pair occurred during subscription setup. The JSON
  `HEARTBEAT` messages are application traffic and are distinct from MQTT ping.
- No PUBLISH had DUP or RETAIN set.

## Topic inventory

The observed serial has been replaced with `EXAMPLE_DEVICE_SERIAL`.

| Full topic | Flow | Channel | Observed MQTT operations | PUBLISH count and QoS | Commands / response behavior |
|---|---|---|---|---|---|
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/ntp/sub` | server → feeder | ntp | Feeder SUBSCRIBE; server PUBLISH | 2, QoS 0 | `NTP_SYNC` request and `NTP` response; feeder answers on `ntp/post` |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/ota/sub` | server → feeder | ota | Feeder SUBSCRIBE only | 0 | No OTA command observed |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/broadcast/sub` | server → feeder | broadcast | Feeder SUBSCRIBE only | 0 | No broadcast payload observed |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/config/sub` | server → feeder | config | Feeder SUBSCRIBE only | 0 | No config payload observed |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/event/sub` | server → feeder | event | Feeder SUBSCRIBE; server PUBLISH | 42, QoS 0 | JSON responses for `DEVICE_START_EVENT` (1), `ATTR_PUSH_EVENT` (38), `GET_FEEDING_PLAN_EVENT` (1), and `GRAIN_OUTPUT_EVENT` (2) |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/service/sub` | server → feeder | service | Feeder SUBSCRIBE; server PUBLISH | 35, QoS 0 | Requests for `DEVICE_CONFIG_SYNC` (1), `FEEDING_PLAN_SERVICE` (9), `MANUAL_FEEDING_SERVICE` (1), and `ATTR_SET_SERVICE` (24) |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/system/sub` | server → feeder | system | Feeder SUBSCRIBE only | 0 | No system payload observed |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/ntp/post` | feeder → server | ntp | Feeder PUBLISH; server PUBACK | 2, QoS 1 | `NTP` request and `NTP_SYNC` response |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/heart/post` | feeder → server | heart | Feeder PUBLISH; server PUBACK | 5, QoS 1 | `HEARTBEAT`; no JSON response |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/event/post` | feeder → server | event | Feeder PUBLISH; server PUBACK for QoS 1 messages | 52: 42 QoS 1, 10 QoS 0 | `DEVICE_START_EVENT`, `ATTR_PUSH_EVENT`, `GET_FEEDING_PLAN_EVENT`, and `GRAIN_OUTPUT_EVENT` require JSON responses; `DEVICE_LOG_REPORT_EVENT` does not appear to |
| `dl/PLAF203/EXAMPLE_DEVICE_SERIAL/device/service/post` | feeder → server | service | Feeder PUBLISH; server PUBACK | 34, QoS 1 | JSON responses to all observed service requests |

MQTT control packets do not carry topics. Across the capture there were two
CONNECTs, one CONNACK, seven SUBSCRIBEs, seven SUBACKs, 172 PUBLISHes, 83
PUBACKs, one PINGREQ, and one PINGRESP.

## Command and schema inventory

“Required” below means present in every captured message of that role. It is an
observed invariant, not a claim that omitted fields are rejected by firmware.
All timestamps are Unix epoch milliseconds.

### NTP

**Command:** `NTP`<br>
**Topic:** `ntp/post` request; `ntp/sub` response<br>
**Direction:** feeder → server, then server → feeder<br>
**Purpose:** feeder clock check and optional recalibration<br>
**Observed request fields:** `cmd`, `ts`<br>
**Required request fields:** `cmd`, `ts`<br>
**Optional/conditional request fields:** none observed<br>
**Example request:**

```json
{"cmd":"NTP","ts":1786354587000}
```

**Expected response:** JSON application response on `ntp/sub`; the server also
PUBACKs the QoS 1 request.<br>
**Response payload:**

```json
{
  "cmd": "NTP",
  "ts": 1786354588271,
  "code": 0,
  "calibrationTag": false,
  "timezoneOffsetSeconds": -14400,
  "nextDSTOffsetSeconds": -18000,
  "nextDSTTransitionTs": 1793512800000,
  "secondNextDSTOffsetSeconds": -14400,
  "secondNextDSTTransitionTs": 1805007600000,
  "timezone": -4
}
```

**Notes:** no `msgId` is used. `timezone` is hours while
`timezoneOffsetSeconds` is seconds. Even with `calibrationTag:false`, all
timezone/DST fields were present.

### NTP_SYNC

**Command:** `NTP_SYNC`<br>
**Topic:** `ntp/sub` request; `ntp/post` response<br>
**Direction:** server → feeder, then feeder → server<br>
**Purpose:** force the feeder clock/timezone configuration<br>
**Observed and required request fields:** `cmd`, `ts`, `msgId`, `timezone`,
`timezoneOffsetSeconds`, `nextDSTOffsetSeconds`, `nextDSTTransitionTs`,
`secondNextDSTOffsetSeconds`, `secondNextDSTTransitionTs`<br>
**Optional/conditional request fields:** none observed<br>
**Example request:**

```json
{
  "cmd": "NTP_SYNC",
  "ts": 1786354587934,
  "msgId": "<msg_id>",
  "timezoneOffsetSeconds": -14400,
  "nextDSTOffsetSeconds": -18000,
  "nextDSTTransitionTs": 1793512800000,
  "secondNextDSTOffsetSeconds": -14400,
  "secondNextDSTTransitionTs": 1805007600000,
  "timezone": -4
}
```

**Expected response:** JSON application response on `ntp/post`, followed by a
server PUBACK because the feeder response is QoS 1.<br>
**Response payload:**

```json
{"cmd":"NTP_SYNC","msgId":"<same_msg_id>","code":0,"ts":1786354587000}
```

### DEVICE_START_EVENT

**Command:** `DEVICE_START_EVENT`<br>
**Topic:** `event/post` event; `event/sub` response<br>
**Direction:** feeder → server, then server → feeder<br>
**Purpose:** announce a successful boot/reconnect and firmware identity<br>
**Observed and required event fields:** `cmd`, `msgId`, `success`, `pid`,
`uuid`, `mac`, `wpa3`, `hardwareVersion`, `softwareVersion`, `tutkP2pRegion`,
`restartReason`, `ts`<br>
**Optional/conditional fields:** none distinguishable from one sample<br>
**Example event:**

```json
{
  "cmd": "DEVICE_START_EVENT",
  "msgId": "<device_generated_msg_id>",
  "success": true,
  "pid": "PLAF203",
  "uuid": "<device_uuid>",
  "mac": "<device_mac>",
  "wpa3": 1,
  "hardwareVersion": "1.0.7",
  "softwareVersion": "3.1.48",
  "tutkP2pRegion": "REGION_US",
  "restartReason": "powerCycle",
  "ts": 1786354587000
}
```

**Expected response:** MQTT PUBACK plus JSON on `event/sub`.<br>
**Response payload:**

```json
{"cmd":"DEVICE_START_EVENT","ts":1786354588537,"msgId":"<same_msg_id>","code":0}
```

### DEVICE_CONFIG_SYNC

**Command:** `DEVICE_CONFIG_SYNC`<br>
**Topic:** `service/sub` request; `service/post` response<br>
**Direction:** server → feeder, then feeder → server<br>
**Purpose:** supply broker/API endpoints and TUTK region after startup<br>
**Observed and required request fields:** `cmd`, `ts`, `msgId`, `mqttAddr`,
`httpsAddr`, `tutkP2pRegion`<br>
**Optional/conditional request fields:** none observed; older water-device fields
were not present<br>
**Example request:**

```json
{
  "cmd": "DEVICE_CONFIG_SYNC",
  "ts": 1786354588545,
  "msgId": "<msg_id>",
  "mqttAddr": [{"host":"mqtt.us.petlibro.com","port":1883}],
  "httpsAddr": "compliance-api.us.petlibro.com",
  "tutkP2pRegion": "REGION_US"
}
```

**Expected response:** JSON on `service/post`, then server PUBACK.<br>
**Response payload:**

```json
{"cmd":"DEVICE_CONFIG_SYNC","msgId":"<same_msg_id>","code":0,"ts":1786354588000}
```

**Notes:** sent about 8 ms after the cloud's `DEVICE_START_EVENT` JSON response.

### FEEDING_PLAN_SERVICE

**Command:** `FEEDING_PLAN_SERVICE`<br>
**Topic:** `service/sub` request; `service/post` response<br>
**Direction:** server → feeder, then feeder → server<br>
**Purpose:** replace/synchronize the complete feeding-plan collection<br>
**Observed and required request fields:** `cmd`, `ts`, `msgId`, `plans`<br>
**Required fields per non-empty plan:** `planId`, `executionTime`, `repeatDay`,
`enableAudio`, `audioTimes`, `grainNum`, `syncTime`<br>
**Optional/conditional plan fields:** none observed; `skipEndTime` was absent<br>
**Example request:**

```json
{
  "cmd": "FEEDING_PLAN_SERVICE",
  "ts": 1786354647499,
  "msgId": "<msg_id>",
  "plans": [{
    "planId": 5694341,
    "executionTime": "11:00",
    "repeatDay": [7,1,2,3,4,5,6],
    "enableAudio": true,
    "audioTimes": 2,
    "grainNum": 7,
    "syncTime": 1786354647000
  }]
}
```

**Expected response:** JSON on `service/post`, then server PUBACK.<br>
**Response payload:**

```json
{
  "cmd": "FEEDING_PLAN_SERVICE",
  "code": 0,
  "msgId": "<same_msg_id>",
  "plans": [{"planId":5694341,"syncTime":1786354647000}],
  "ts": 1786354646000
}
```

**Notes:** `plans:[]` was used to delete all plans. Eight of nine requests had
matching responses. The unmatched request was the first post-CONNACK message;
another request with equivalent plans and a new `msgId` followed 23 ms later
and was acknowledged. No `DEVICE_FEEDING_PLAN_SERVICE` command was observed.

### GET_FEEDING_PLAN_EVENT

**Command:** `GET_FEEDING_PLAN_EVENT`<br>
**Topic:** `event/post` request; `event/sub` response<br>
**Direction:** feeder → server, then server → feeder<br>
**Purpose:** request the server's authoritative complete plan collection<br>
**Observed and required request fields:** `cmd`, `ts`, `msgId`<br>
**Optional/conditional request fields:** none observed<br>
**Example request:**

```json
{"cmd":"GET_FEEDING_PLAN_EVENT","msgId":"<msg_id>","ts":1786354588000}
```

**Expected response:** MQTT PUBACK plus JSON on `event/sub`.<br>
**Response payload:** `cmd`, same `msgId`, `ts`, `code:0`, and full `plans`
objects using the same per-plan schema as `FEEDING_PLAN_SERVICE` requests.

**Persistence warning:** Firmware handler `ApplyGetFeedingPlanResponse`
(`0x0006a270`) does not use the top-level `code` to qualify plan data. If
`plans` is absent, it returns without changing the on-device plan files. If
`plans` is present, it treats the array as authoritative and rewrites
`/user/data/feed_plan/index.bin` and `plan.bin`; consequently, `plans: []`
clears the persisted schedule even when `code` is nonzero. A server without a
fresh authoritative plan collection must omit the application response rather
than send an empty error form.

### MANUAL_FEEDING_SERVICE

**Command:** `MANUAL_FEEDING_SERVICE`<br>
**Topic:** `service/sub` request; `service/post` response<br>
**Direction:** server → feeder, then feeder → server<br>
**Purpose:** dispense a requested portion count<br>
**Observed and required request fields:** `cmd`, `ts`, `msgId`, `grainNum`<br>
**Optional/conditional request fields:** none observed<br>
**Example request:**

```json
{"cmd":"MANUAL_FEEDING_SERVICE","ts":1786354621439,"msgId":"<msg_id>","grainNum":1}
```

**Expected response:** JSON on `service/post`, then server PUBACK.<br>
**Response payload:**

```json
{"cmd":"MANUAL_FEEDING_SERVICE","msgId":"<same_msg_id>","code":0,"ts":1786354620000}
```

**Notes:** grain progress is reported separately through two
`GRAIN_OUTPUT_EVENT` messages that reuse this `msgId`.

### GRAIN_OUTPUT_EVENT

**Command:** `GRAIN_OUTPUT_EVENT`<br>
**Topic:** `event/post` event; `event/sub` response<br>
**Direction:** feeder → server, then server → feeder<br>
**Purpose:** report dispenser start/end and actual output<br>
**Observed and required event fields:** `cmd`, `msgId`, `finished`, `type`,
`actualGrainNum`, `expectGrainNum`, `execTime`, `execStep`, `ts`<br>
**Optional/conditional fields:** `planId` and `retried` exist in older code but
were not captured; firmware RE shows `retried` is numeric. `errorCode` is
conditionally emitted by failed end records.<br>
**Example event:**

```json
{
  "cmd": "GRAIN_OUTPUT_EVENT",
  "msgId": "<manual_feed_msg_id>",
  "finished": false,
  "type": 2,
  "actualGrainNum": 0,
  "expectGrainNum": 1,
  "execTime": 1786354620000,
  "execStep": "GRAIN_START",
  "ts": 1786354620000
}
```

**Expected response:** MQTT PUBACK plus JSON on `event/sub`.<br>
**Response payload:**

```json
{"cmd":"GRAIN_OUTPUT_EVENT","ts":1786354621925,"msgId":"<same_msg_id>","code":0,"execStep":"GRAIN_START"}
```

**Notes:** the end event used `finished:true`, `actualGrainNum:1`, and
`execStep:"GRAIN_END"`; its response echoed `GRAIN_END`.

### ERROR_EVENT (firmware-RE-backed)

**Command:** `ERROR_EVENT`<br>
**Topic:** `event/post`<br>
**Direction:** feeder → server<br>
**Fields:** `cmd`, `msgId`, `errorCode`, `triggerTime`, `ts`, and optional
producer-specific string `extend`.<br>

Firmware 3.1.48 directly publishes known error producers through MQTT QoS 1.
The inbound OEM response handler does not correlate `msgId`; it blindly
advances a separate in-memory FIFO. Petlibro Local therefore parses and
classifies `ERROR_EVENT` but intentionally sends no JSON application response.
The broker's MQTT QoS 1 PUBACK remains unchanged.

Codes 2046 (VSaaS/cloud notification failure) and 2048 (ISP exposure/day-night
diagnostic) are routine diagnostics on a WAN-isolated feeder. Confirmed
feeding codes are handled separately, and error-ring operations may emit the
same code through both `ERROR_EVENT` and `GRAIN_OUTPUT_EVENT.errorCode`.

The normal-ring publisher at `0x00066E70` and error-ring publisher at
`0x00067AB4` serialize queued observations, not necessarily live operations;
IDs can be generated at publication or taken from the runtime manual-request
buffer. Terminal error-code fields are cleared in memory after emission, so an
errorless repeat is not independent success evidence. `finished:true` and
`GRAIN_END` are also used by failed operations. Portion counts are backed by
mechanism-switch processing, not food-weight measurements. The integration's
[conservative recovery policy](../architecture.md#operational-error-lifecycle)
therefore requires coherent, fresh start/end evidence as well as counts and
error context. It changes neither grain acknowledgements nor the deliberate
absence of `ERROR_EVENT` application acknowledgements.

### ATTR_PUSH_EVENT

**Command:** `ATTR_PUSH_EVENT`<br>
**Topic:** `event/post` event; `event/sub` response<br>
**Direction:** feeder → server, then server → feeder<br>
**Purpose:** publish a full startup snapshot or sparse/full setting/state update<br>
**Observed invariant fields in all 38 events:** `cmd`, `ts`, `msgId`,
`disableHardwareButton`, `cameraAuthInfo`<br>
**Optional/conditional observed fields:**

- Power/food: `powerMode`, `powerType`, `electricQuantity`, `surplusGrain`,
  `motorState`, `grainOutletState`, `bowlMode`.
- Audio/sound: `enableAudio`, `audioUrl`, `volume`, `soundSwitch`,
  `enableSound`, `soundAgingType`.
- Lights/buttons: `enableLight`, `lightSwitch`, `lightAgingType`,
  `autoChangeMode`, `autoThreshold`.
- Camera/video: `cameraSwitch`, `enableCamera`, `cameraAgingType`,
  `nightVision`, `resolution`, `videoRecordSwitch`, `enableVideoRecord`,
  `videoRecordMode`, `videoRecordAgingType`, `feedingVideoSwitch`,
  `enableVideoStartFeedingPlan`, `enableVideoAfterManualFeeding`,
  `beforeFeedingPlanTime`, `afterManualFeedingTime`, `automaticRecording`,
  `videoWatermarkSwitch`, `cloudVideoRecordSwitch`, `cameraAuthInfo`.
- Detection: `motionDetectionSwitch`, `enableMotionDetection`,
  `motionDetectionAgingType`, `motionDetectionRange`,
  `motionDetectionSensitivity`, `soundDetectionSwitch`,
  `enableSoundDetection`, `soundDetectionAgingType`,
  `soundDetectionSensitivity`.
- Storage/network: `sdCardState`, `sdCardFileSystem`,
  `sdCardTotalCapacity`, `sdCardUsedCapacity`, `wifiSsid`.

**Example sparse event:**

```json
{
  "cmd": "ATTR_PUSH_EVENT",
  "msgId": "<msg_id>",
  "ts": 1786354703000,
  "disableHardwareButton": true,
  "cameraAuthInfo": "<redacted_camera_auth_info>"
}
```

**Expected response:** MQTT PUBACK plus JSON on `event/sub`.<br>
**Response payload:**

```json
{"cmd":"ATTR_PUSH_EVENT","msgId":"<same_msg_id>","code":0,"ts":1786354703676}
```

**Notes:** the firmware spells the unavailable filesystem string
`"unkown type"` (sic). Camera authorization information must not be logged or
persisted unnecessarily.

### ATTR_SET_SERVICE

**Command:** `ATTR_SET_SERVICE`<br>
**Topic:** `service/sub` request; `service/post` direct response<br>
**Direction:** server → feeder, then feeder → server<br>
**Purpose:** set one or a grouped collection of attributes<br>
**Observed invariant request fields:** `cmd`, `ts`, `msgId`, plus one or more
setting fields<br>
**Observed setting fields:** `disableHardwareButton`, `audioUrl`, `enableAudio`,
`volume`, `soundSwitch`, `soundAgingType`, camera/video fields, and
motion/sound-detection fields listed under `ATTR_PUSH_EVENT`<br>
**Optional/conditional fields:** every setting field is sparse/conditional;
the app sent complete related groups for camera/video and detection settings,
but single fields for button disable and volume<br>
**Example request:**

```json
{"cmd":"ATTR_SET_SERVICE","ts":1786354704519,"msgId":"<msg_id>","disableHardwareButton":true}
```

**Expected response:** first, JSON on `service/post`, then server PUBACK:

```json
{"cmd":"ATTR_SET_SERVICE","msgId":"<same_msg_id>","code":0,"ts":1786354703000}
```

The feeder then emits one or more `ATTR_PUSH_EVENT` messages on `event/post`.
The first push normally reused the service `msgId` and included a full related
setting group; a later sparse push could use a feeder-generated `msgId`. Every
push received its own `event/sub` JSON acknowledgement.

### HEARTBEAT

**Command:** `HEARTBEAT`<br>
**Topic:** `heart/post`<br>
**Direction:** feeder → server<br>
**Purpose:** periodic application liveness and radio status<br>
**Observed and required fields:** `cmd`, `ts`, `count`, `rssi`, `wifiType`<br>
**Optional/conditional fields:** none observed<br>
**Example payload:**

```json
{"cmd":"HEARTBEAT","count":1,"rssi":-61,"wifiType":2,"ts":1786354587000}
```

**Expected response:** MQTT PUBACK only. No JSON response topic or payload was
observed. Counts 1–5 were captured, at irregular intervals of approximately
70, 72, 72, and 72 seconds after the first startup heartbeat.

### DEVICE_LOG_REPORT_EVENT

**Command:** `DEVICE_LOG_REPORT_EVENT`<br>
**Topic:** `event/post`<br>
**Direction:** feeder → server<br>
**Purpose:** batched device telemetry/log reporting<br>
**Observed and required fields:** `cmd`, `ts`, `msgId`, `logs`<br>
**Required per log entry:** `type`, `content`, `time`<br>
**Observed content variants:** `WIFI_EVENT` with a string such as
`WIFI_EVENT_SCANNING`; `TUTK_DATA` with an array containing `commonData` and
`specificData` event objects<br>
**Example payload:**

```json
{
  "cmd": "DEVICE_LOG_REPORT_EVENT",
  "msgId": "<device_generated_msg_id>",
  "logs": [{"type":"WIFI_EVENT","content":"WIFI_EVENT_SCANNING","time":1786352729000}],
  "ts": 1786354587000
}
```

**Expected response:** none observed. These ten PUBLISH messages were QoS 0, so
there was neither MQTT PUBACK nor application JSON acknowledgement.

## Boot sequence timeline

| Time (UTC) | Flow | Event |
|---|---|---|
| 09:35:28.367740 | feeder → server | First CONNECT; no response captured |
| 09:36:27.168459 | feeder → server | CONNECT retry, clean-session false, keepalive 90 |
| 09:36:27.213374 | server → feeder | Accepted CONNACK and an immediate `FEEDING_PLAN_SERVICE` in one TCP frame |
| 09:36:27.227275–09:36:28.217006 | feeder → server | Seven sequential SUBSCRIBEs; each receives SUBACK |
| 09:36:27.236415 | server → feeder | Second `FEEDING_PLAN_SERVICE` request |
| 09:36:27.917953 | feeder → server | Successful plan response; device `ts` is still about 30 minutes behind capture time |
| 09:36:27.959146 | server → feeder | `NTP_SYNC` with timezone/DST schedule |
| 09:36:28.068095 | feeder → server | `NTP_SYNC` code-0 response |
| 09:36:28.261080 | feeder → server | `NTP` clock-check request |
| 09:36:28.336513 | server → feeder | Full `NTP` response, `calibrationTag:false` |
| 09:36:28.403527 | feeder → server | Heartbeat count 1; PUBACK only |
| 09:36:28.525053 | feeder → server | `DEVICE_START_EVENT` identifies hardware 1.0.7/software 3.1.48 |
| 09:36:28.562510 | server → feeder | `DEVICE_START_EVENT` code-0 response on `event/sub` |
| 09:36:28.570184 | server → feeder | `DEVICE_CONFIG_SYNC` on `service/sub` |
| 09:36:28.577999 | server → feeder | Another complete `FEEDING_PLAN_SERVICE` |
| 09:36:28.919190 | feeder → server | `DEVICE_CONFIG_SYNC` code-0 response |
| 09:36:29.029792 | feeder → server | Full `ATTR_PUSH_EVENT` startup snapshot |
| 09:36:29.069467 | server → feeder | Attribute-push code-0 response on `event/sub` |
| 09:36:29.137395 | feeder → server | Plan-service code-0 response |
| 09:36:29.247286 | feeder → server | `GET_FEEDING_PLAN_EVENT` |
| 09:36:29.292064 | server → feeder | Full plan response on `event/sub`; steady operation follows |

## Manual feed sequence timeline

| Relative step | Flow | Payload consequence |
|---|---|---|
| 09:37:01.465089 | server → feeder | `MANUAL_FEEDING_SERVICE`, `grainNum:1`, QoS 0 |
| +3 ms | feeder → server | Same `msgId`, `code:0` on `service/post`, QoS 1; server PUBACKs |
| +436 ms | feeder → server | `GRAIN_OUTPUT_EVENT`, `type:2`, `GRAIN_START`, expected 1/actual 0 |
| +486 ms | server → feeder | `event/sub` JSON acknowledgement echoing `GRAIN_START` |
| +1.65 s | feeder → server | `ATTR_PUSH_EVENT` reports outlet/storage/power state; acknowledged on `event/sub` |
| +9.91 s | feeder → server | `GRAIN_OUTPUT_EVENT`, `GRAIN_END`, expected 1/actual 1, `finished:true` |
| +9.95 s | server → feeder | `event/sub` JSON acknowledgement echoing `GRAIN_END` |

## Feed plan sync sequence timeline

Every app-driven change sent the entire desired `plans` array rather than an
incremental operation. Observed operations were:

1. Startup synchronized two plans and later answered
   `GET_FEEDING_PLAN_EVENT` with those same complete plan objects.
2. At `09:37:21`, the app sent one remaining plan (deleting one).
3. At `09:37:24`, it sent `plans:[]` (deleting all).
4. At `09:37:25`, it restored one plan.
5. At `09:37:27`, it restored the second plan with an updated `syncTime`.
6. At `09:37:54`, it added a temporary `09:37`, one-portion plan.
7. At `09:37:59`, it removed the temporary plan.

Each request received a `service/post` response containing `code:0` and only
the accepted `planId`/`syncTime` pairs. This capture therefore supports
`FEEDING_PLAN_SERVICE` and `plans`; it provides no evidence for singular
`plan` or `DEVICE_FEEDING_PLAN_SERVICE` on firmware 3.1.48.

## Attribute/settings update sequence timeline

The app changed these settings during the capture:

- disabled and re-enabled the hardware button;
- toggled the camera twice;
- toggled SD video recording;
- enabled feeding-video mode and changed record mode to motion detection;
- enabled recording at plan start and after manual feed, then disabled the
  feeding-video master switch;
- disabled the video watermark;
- toggled motion detection and sound detection;
- sent the detection group once without changing its values;
- changed volume from 50 to 51 and back;
- toggled sound output;
- enabled feeding audio with the default S3 URL, then changed to a Petlibro OSS
  audio URL.

The repeated exchange was:

```text
server service/sub ATTR_SET_SERVICE (QoS 0)
  -> feeder service/post code-0 response (QoS 1)
  -> server PUBACK
  -> feeder event/post ATTR_PUSH_EVENT (QoS 1)
  -> server PUBACK
  -> server event/sub code-0 ATTR_PUSH_EVENT response (QoS 0)
  -> sometimes a second sparse feeder ATTR_PUSH_EVENT with a new msgId
```

Camera/video and detection changes were sent as related groups, not necessarily
as a single changed field. Consumers should therefore parse both sparse and
full payloads, and should not infer that every field in an app request changed.

## Application-level acknowledgement rules

| Originating message | MQTT result | Required/observed JSON result |
|---|---|---|
| Feeder `NTP` | Server PUBACK | `NTP` on `ntp/sub`, no `msgId` |
| Server `NTP_SYNC` | No MQTT ACK (QoS 0) | `NTP_SYNC` code response on `ntp/post` with same `msgId`; server then PUBACKs that response |
| Feeder `DEVICE_START_EVENT` | Server PUBACK | Code response on `event/sub`, same `msgId` |
| Server `DEVICE_CONFIG_SYNC` | No MQTT ACK | Code response on `service/post`, same `msgId`; then PUBACK |
| Server `FEEDING_PLAN_SERVICE` | No MQTT ACK | Code plus accepted plans on `service/post`, same `msgId`; then PUBACK |
| Feeder `GET_FEEDING_PLAN_EVENT` | Server PUBACK | Code plus full plans on `event/sub`, same `msgId` |
| Server `MANUAL_FEEDING_SERVICE` | No MQTT ACK | Code response on `service/post`, same `msgId`; then PUBACK |
| Feeder `GRAIN_OUTPUT_EVENT` | Server PUBACK | Code response on `event/sub`, same `msgId`, echoing `execStep` |
| Feeder `ERROR_EVENT` | Server PUBACK | No JSON response; the firmware handler is correlation-blind and known producers use direct QoS 1 publication |
| Feeder `ATTR_PUSH_EVENT` | Server PUBACK | Code response on `event/sub`, same `msgId` |
| Server `ATTR_SET_SERVICE` | No MQTT ACK | Code response on `service/post`, same `msgId`; later attribute push is a separate acknowledged event |
| Feeder `HEARTBEAT` | Server PUBACK | No JSON response |
| Feeder `DEVICE_LOG_REPORT_EVENT` | None (QoS 0) | No JSON response |

PUBACK has no topic or JSON payload. It acknowledges only MQTT delivery of a
QoS 1 PUBLISH and cannot replace the command-level responses above.

## Camera, video, and TUTK observations

- No `TUTK_CONTRACT_SERVICE`, camera service command, video stream command, or
  camera token exchange was captured.
- Camera/video configuration was transported through `ATTR_SET_SERVICE` and
  reported through `ATTR_PUSH_EVENT`.
- `DEVICE_START_EVENT` and `DEVICE_CONFIG_SYNC` both carried
  `tutkP2pRegion:"REGION_US"`.
- Every captured device `ATTR_PUSH_EVENT` included `cameraAuthInfo`; it is
  redacted from extracted data and must be treated as secret.
- `DEVICE_LOG_REPORT_EVENT` contained `TUTK_DATA` telemetry for
  `businessName:"TUTK_LIVE"`, with `EVENT_START_TYPE`/`EVENT_END_TYPE` and
  `eventCode:0`. This is telemetry about live-session lifecycle, not a stream
  control protocol.

## Implementation status

The capture-backed compatibility work described in this document is implemented
in `src/protocol.py` and covered by the sanitized fixtures in `tests/`:

- `GET_FEEDING_PLAN_EVENT` and `GRAIN_OUTPUT_EVENT` responses use `event/sub`.
- `NTP` and `NTP_SYNC` serialize the current timezone offset and the next two
  offset transitions. Fixed-offset zones send zero transition timestamps.
- The capture sent `DEVICE_CONFIG_SYNC` after the `DEVICE_START_EVENT`
  acknowledgement. The maintained controller now preserves existing endpoint
  values by default and sends this command only after an explicit feeder MQTT
  persistence opt-in and destination validation.
- `disableHardwareButton`, `enableLight`, `bowlMode`, and the firmware's
  misspelled `"unkown type"` filesystem value are accepted. The observed
  `SINGLE_BOWL` value is exposed through Home Assistant. The writable
  `DOUBLE_BOWL` counterpart is inferred from the protocol naming and remains
  subject to live confirmation.
- Attribute serializers use JSON booleans and omit optional plan fields when
  absent. Feeding-plan response timestamps are normalized to `Timestamp`.
- `DEVICE_LOG_REPORT_EVENT` is parsed and summarized without an application
  acknowledgement. Incoming `cameraAuthInfo` is redacted before diagnostic
  logging.
- Adjacent serializer and negative-response failures found during the analysis
  are corrected and exercised by regression tests where practical.

The implementation intentionally remains permissive for fields not observed in
older firmware. The 3.1.48 fixture is synthetic and contains no live device or
account data; it preserves only the message shapes needed by the tests.

## Open questions and capture limitations

- This is one six-minute successful session from one device/region/timezone.
  Required-field claims need failure/omission experiments before becoming strict
  validators.
- `DEVICE_CONFIG_SYNC` was part of startup, but the capture does not prove
  whether it is mandatory, how often it is refreshed, or how the feeder behaves
  when endpoints are local/unreachable.
- The observed startup and acknowledgement payloads do not expose the feeder's
  current MQTT or HTTPS values. Omitting `httpsAddr` in an intentional
  MQTT-only update remains a live-test question; the default path avoids this
  uncertainty by omitting the entire command.
- The capture reports `bowlMode:"SINGLE_BOWL"`, but does not contain an
  `ATTR_SET_SERVICE` bowl-mode change or a dual-bowl report. The implementation
  sends the inferred counterpart `DOUBLE_BOWL` and accepts `DUAL_BOWL` as an
  inbound compatibility alias; a live dual-tray test is still required to
  confirm the canonical write value.
- No OTA PUBLISH occurred. `OTA_INFORM`, `OTA_PROGRESS`, and `OTA_UPGRADE`
  schemas and response topics remain unconfirmed for 3.1.48; only the feeder's
  `ota/sub` subscription was observed.
- No payload occurred on `broadcast/sub`, `config/sub`, or `system/sub`.
- No `DEVICE_FEEDING_PLAN_SERVICE`, `ATTR_GET_SERVICE`, detection event,
  binding, reset, reboot, restore, unbind, Wi-Fi change, or TUTK contract command
  occurred.
- No scheduled-plan grain output (`type:1`), physical-button output (`type:3`),
  `GRAIN_BLOCKING`, retry, partial output, or error code was captured.
- All captured plans repeated every day. Partial weekday ordering, `skipEndTime`,
  plan limits, and range validation are untested.
- No DST boundary occurred; the meaning of the two transition records is
  strongly suggested by names/values but not behaviorally tested.
- Heartbeat timing was not perfectly periodic in this short capture. Do not set
  a watchdog threshold from these five samples alone.
- The initial post-CONNACK plan request was unmatched. It may reflect a resumed
  session/startup race, but the capture does not identify the server-side cause.
- TUTK live-session telemetry was present, but no media signaling or camera
  control exchange was captured, so it is insufficient to implement streaming.
