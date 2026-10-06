# Changelog

## 0.3.10-beta.1

- Normalize stock-app feeding-plan IDs into Home Assistant slots 1-9 during
  fresh reconciliation. The add-on logs an explicit record/ID mapping and uses
  the normal serialized MQTT acknowledgement and State Agent readback path.
- Refuse automatic normalization unless every known persistent record field can
  round-trip without loss, including rejecting nonzero opaque tails and
  unsupported raw values. Failed verification does not retry indefinitely.
- Add separate stable (`main`) and development (`develop`) add-on release
  channels with fail-closed SemVer validation. Development images are
  versioned prereleases and never update `latest`.

## 0.3.9

- Prevent a failed State Agent plan read from clearing feeder schedules. The
  add-on now leaves `GET_FEEDING_PLAN_EVENT` unanswered unless it has fresh,
  authoritative feeder-local truth; AF203 firmware ignores a response error
  code when a `plans` array is present and treats `plans: []` as authoritative.
- Keep the temporary bootstrap broker non-authoritative for feeding plans and
  acknowledge grain-output events with their original execution step.
- Preserve a mode-restricted, one-time copy of feeder settings and feeding-plan
  files before future bootstrap installations, without replacing that recovery
  snapshot during retries.
- Document the firmware's plan-response persistence behavior, safe bootstrap
  handoff semantics, and recovery guidance for installations made before this
  safeguard.

## 0.3.8

- Add the guided, no-UART stock-feeder installer with a single TUI workflow,
  repository-local build workspace, automatic dependency preflight, and
  generated add-on configuration.
- Build and install a pinned static ARMv7 hard-float Dropbear from official
  source, require key-only SSH recovery, and accept either generated keys,
  public-key files, or pasted OpenSSH public keys.
- Capture the feeder's factory MQTT identity through a source-restricted
  temporary broker, verify it against the final broker before OTA, restore the
  final endpoint after bootstrap, and best-effort verify a fresh OEM heartbeat
  with the configured backend broker account.
- Install the State Agent and update supervisor while preserving OEM firmware
  in both slots and selecting OTA1 for production startup. Bind protected State
  Agent tokens to the feeder/Home Assistant addresses and default generated
  update settings to the signed project release feed.
- Reject disallowed State Agent source IPs before HTTP parsing, tighten startup
  service matching and fixed helper paths, and retry transient update-status
  failures without abandoning an active update transaction.
- Harden installer secret-file creation, temporary listener restrictions,
  broker evidence validation, payload serving, and failure/recovery behavior.
- Refresh repository documentation around the production architecture,
  installation, broker-account roles, runtime state ownership, plan creation,
  retention, recovery, and development workflows.

## 0.3.6

fix: support creating new feeding plans from state-agent truth

The feeding plan write path previously assumed all PlanPatch operations
targeted an existing feeder-owned plan. This caused writes to fail when the
feeder had no stored plans or when adding a new plan ID.

Update desired-state construction to support plan creation by allowing:

- empty feeder state -> first feeding plan
- existing plans -> existing plans plus a new feeding plan

Extend PlanCollectionPredicate with explicit CREATE/UPDATE operation modes
so verification continues enforcing strict state transitions:

- UPDATE preserves existing plan IDs and feeder-owned fields
- CREATE preserves existing plans while allowing the target plan to appear

Add test coverage for new plan creation, multi-plan additions, and invalid
creation transitions while preserving existing mutation verification behavior.


## 0.3.2

- Reconstruct Home Assistant dispensing status after AppDaemon startup, feeder
  reconnect, and Home Assistant birth from a correlated live
  `ATTR_GET_SERVICE.motorState` response.
- Keep grain-output events authoritative for immediate runtime transitions and
  reject solicited responses that belong to an old connection, another
  request, or an earlier local event generation.
- Add dedicated non-retained dispensing availability and a distinct Recovering
  state while retaining durable last-dispense observations.

## 0.3.1

- Send the fresh feeder-owned `audio_url` together with `enableAudio` when
  enabling Meal Call, matching the firmware's grouped attribute requirement.
- Mirror the State Agent's persistent Meal Call audio URL into Home Assistant
  while continuing to reject unsafe URL edits.

## 0.3.0

- Add the maintained feeder-resident State Agent source and replace the
  observational decoder with the audited 236-byte `state.bin`, 47-byte plan,
  and 51-slot feed-event queue layouts.
- Classify feeder fields as persistent, effective-cached, or runtime and verify
  writable controls only against persistent switch/configuration fields.
- Correct sound verification to `sound_switch` at offset `0x21`, decode
  unaligned multi-byte camera/detection fields at full width, and expose raw
  minute durations without enum assumptions.
- Preserve stable feeding-plan protocol/opaque fields while excluding runtime
  execution state and regenerated target sync metadata from schedule equality.
- Describe `/v1/feed-events` as a pending store-and-forward queue rather than
  durable feeding history.

## 0.2.9

- Acknowledge sparse `ATTR_PUSH_EVENT` messages before processing optional
  telemetry, tolerate missing and unknown fields, and isolate telemetry callback
  failures so they cannot break the feeder protocol exchange.
- Treat persistent fields in attribute pushes only as state-agent refresh hints;
  feeder-local `/v1/core` truth remains authoritative.
- Contain MQTT parser and handler failures with sanitized context so sensitive
  fields such as `cameraAuthInfo` cannot leak through AppDaemon exception
  formatting.
- Add opt-in raw state-agent reads and raw-settings comparison support for
  capture-backed investigation of unresolved persisted-field mappings.

## 0.2.8

- Make the authenticated feeder-side state API authoritative for persistent
  settings and feeding plans; retained Home Assistant and AppDaemon storage
  values are now mirror/diagnostic state only.
- Add explicit reconciliation, revision polling, serialized pending writes,
  MQTT acknowledgement correlation, feeder-local verification, and divergence
  handling. Persistent writes are blocked while the state API is unavailable.
- Build every feeding-plan edit and feeder plan-response from a fresh
  `/v1/core` read, preserve opaque per-plan fields, send the complete
  collection, and verify that no plan was added, removed, or changed
  collaterally.
- Add state-agent URL, bearer-token, and timeout options plus focused parser,
  coordinator, retained-command, configuration, and feed-plan regression tests.
- Split the AppDaemon controller into focused transport, protocol, backend,
  command, mapping, plan, HA projection, telemetry, and storage modules. Remove
  the old parallel setting/plan projection paths and centralize persistent
  command mappings around the coordinator authority model.

## 0.2.7

- Persist and republish accepted feeding-plan edits so all nine Home Assistant
  text entities retain their updated values after a page refresh.
- Bind every feeding-plan command topic to its corresponding slot and reject a
  JSON `id` that does not match the edited slot.
- Add concise success and mismatch diagnostics without logging feeding-plan
  contents.
- Replace opaque Home Assistant entity names and enum choices with readable
  labels while preserving entity IDs, MQTT topics, and raw protocol values.

## 0.2.6

- Apply sparse `ATTR_PUSH_EVENT` updates only to the settings groups actually
  present, preventing feeder-reported camera-resolution changes from
  republishing or clearing unrelated food, audio, recording, and detection
  state.
- Preserve absent food-state booleans and improve invalid feeding-plan JSON
  diagnostics without logging the submitted schedule.
- Clarify that the feeder-reported camera resolution is separate from the
  active go2rtc stream resolution.
- Add a writable Home Assistant bowl-configuration select backed by the
  feeder's `bowlMode` attribute, while keeping scheduled and manual-feed
  portion quantities unchanged.

## 0.2.5

- Stop copying the add-on-only `mqtt_host` into the physical feeder during
  startup. Endpoint persistence is now disabled by default and requires a
  separate feeder-facing host, an explicit opt-in, and DNS/address/TCP
  validation before `DEVICE_CONFIG_SYNC` can be sent.
- Add startup, validation, send-attempt, and acknowledgement logging for safe
  feeder broker migration and recovery.
- Fix AppDaemon executor completion handling so successful address resolution
  updates the private registry, renders the go2rtc stream, and reloads go2rtc.
  Failed, malformed, exceptional, and stale completions now release their
  attempt guard so later retries are not suppressed.
- Propagate an already-discovered camera UID into dynamically rendered feeder
  controllers and add a one-time, actionable power-cycle message when startup
  identity traffic was missed.
- Make heartbeat restart transitions idempotent, avoid first-contact storage
  warnings, and treat initial NTP drift as a deduplicated correction in progress
  until its acknowledgement fails validation or times out.
- Log an absent camera runtime status as normal startup information until a
  status file has previously existed; malformed, stale, or lost status remains
  a warning.

## 0.2.4

- Fix AppDaemon startup by rendering the user-facing `log_level` option as the
  non-reserved `petlibro_log_level` app argument, preserving Petlibro's
  lowercase and `trace` levels without overriding AppDaemon's logger.

## 0.2.3

- Pull a prebuilt amd64 add-on image from GHCR for normal Home Assistant
  installs, while retaining an explicitly documented local-development build
  path.
- Fix automatic IP resolution with a bounded cached, broadcast, candidate, and
  single-sweep strategy using the feeder's complete LAN_SEARCH3/KNOCK2 probe
  sequence.
- Run resolver subprocesses through AppDaemon's executor so MQTT callbacks stay
  responsive, with structured timeout and helper-error handling.
- Add conventional `log_level` controls, secret redaction, and trace-only raw
  MQTT and per-packet diagnostics while preserving `verbose_logs` migration.
- Report per-leg discovery sends, LAN_SEARCH_R/KNOCK_RR2 receipts, UID and nonce
  rejections, aggregate traffic, send errors, and deadline state in resolver
  JSON and debug logs.

## 0.2.2

- Fix the first-heartbeat timestamp-drift error path so a controller callback
  cannot crash before camera UID discovery and go2rtc configuration complete.
- Prevent a newly initialized empty feeding-plan collection from being pushed
  to the feeder on first contact, while preserving explicit schedule updates
  and intentional schedule clearing.

## 0.2.1

- Fix automatic feeder discovery by registering the AppDaemon MQTT callback
  with a wildcard filter, allowing concrete `dl/PLAF203/<serial>/device/...`
  topics to reach the discovery coordinator.

## 0.2.0

- Make discovery-first setup the default so users no longer need to configure
  a feeder serial, camera UID, or device IP for normal installations.
- Discover feeder serials and camera UIDs from MQTT startup traffic.
- Resolve and refresh device IP addresses with the existing LAN_SEARCH3 Go
  implementation, then persist a private device registry.
- Generate direct-IP go2rtc streams and per-device controller/status mappings
  for multiple discovered feeders.
- Publish retained discovery readiness and restart go2rtc only when its
  generated configuration changes.
- Keep legacy single-device settings and explicit per-device overrides as
  compatibility fallbacks.

## 0.1.0

- Package patched go2rtc and the PLAF203 AppDaemon controller together.
- Generate runtime configuration from Home Assistant options or Docker Compose
  environment values.
- Publish a versioned, retained MQTT camera runtime contract through AppDaemon,
  backed by atomic go2rtc lifecycle, SPS-resolution, and health status exports.
- Fix container startup by relying on the Home Assistant base image's `/init`
  entrypoint instead of passing a second `/init` argument.
- Keep the feeder and AppDaemon MQTT credential roles separate; feeder broker
  account provisioning remains external to the add-on.
- Clarify that an HD SPS transition can occur after the configured probe window.
- Add amd64 Home Assistant and Debian LXC deployment paths.
- Add privacy-safe validation, troubleshooting, and debug collection tooling.
