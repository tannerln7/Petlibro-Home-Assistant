# PLAF203 controller

This package is the MQTT and Home Assistant controller embedded in the
[Petlibro Local add-on](../README.md). It runs under AppDaemon and translates
between the feeder's local protocol, the feeder-local State Agent, and Home
Assistant MQTT discovery/state topics.

## Responsibilities

- validate feeder identity and protocol messages;
- publish stable Home Assistant entities through MQTT discovery;
- translate Home Assistant commands into correlated feeder requests;
- read persistent settings and schedules from the State Agent;
- verify persistent writes against fresh State Agent revisions;
- project live telemetry and feed events;
- reconstruct transient dispensing state after startup or Home Assistant birth;
- coordinate signed State Agent updates; and
- publish camera identity/readiness metadata consumed by go2rtc.

The controller does not act as an MQTT broker, install software on the feeder,
or expose generic feeder filesystem access.

## Runtime truth model

The controller deliberately separates several sources of truth:

- **Persistent state and plans** come from State Agent `/v1/core` snapshots.
  Retained Home Assistant values are never treated as feeder truth.
- **Fresh dispensing bootstrap state** comes from a correlated, solicited
  `ATTR_GET_SERVICE.motorState` response.
- **Immediate feed transitions** come from `GRAIN_OUTPUT_EVENT` messages.
- **Historical observations**, such as the last feed start/end, portion count,
  and source, are retained MQTT facts.

`food_output/progress` and its dedicated availability topic are intentionally
non-retained. On startup, reconnect, or `homeassistant/status = online`, the
controller reacquires live motor state before marking that entity available.
A local event generation prevents a delayed solicited response from
overwriting a newer grain event.

Feeding-plan commands always start with a fresh `/v1/core` preflight and send a
complete collection. They can update an existing slot or create a missing plan
ID while preserving every existing record; deletion is not exposed. The
post-ack snapshot must prove the intended collection transition before Home
Assistant accepts it.

If reconciliation finds stock-app plan IDs outside Home Assistant's canonical
1-9 range, the coordinator can assign free IDs in feeder record order. This is
not a best-effort conversion: it runs as a serialized, acknowledged, verified
persistent write only when every known record field can round-trip through the
MQTT plan schema. Unsupported raw values or opaque bytes cause a logged refusal
and leave the feeder unchanged.

## Source map

Key modules under [`src`](src):

- `plaf203.py`: AppDaemon application lifecycle and event wiring;
- `backend.py`: protocol request/response handling and runtime acquisition;
- `state_coordinator.py`: persistent snapshot reconciliation and writes;
- `state_agent.py`: typed State Agent HTTP client;
- `state_agent_updates.py`: signed-update orchestration;
- `dispensing_status.py`: single-owner dispensing projection and ordering;
- `feed_plans.py`: schedule projection and editing;
- `ha_entities.py`: discovery definitions and Home Assistant presentation;
- `telemetry.py`: runtime telemetry projection;
- `device_discovery.py`: feeder and camera LAN discovery;
- `protocol.py`: protobuf-like wire structures and protocol constants.

## Configuration and operation

Normal users configure the enclosing add-on. See [add-on options](../DOCS.md).
For standalone AppDaemon development, copy [`src/apps.example.yaml`](src/apps.example.yaml)
into an AppDaemon apps directory and provide MQTT and State Agent settings
appropriate for an isolated test environment. Do not put credentials in this
repository.

The controller expects:

- an MQTT broker reachable by both the add-on and feeder;
- a feeder-specific MQTT account already provisioned on that broker;
- the State Agent on the feeder, normally at port `8765`; and
- host/LAN access for discovery and camera coordination.

## Testing

Install development dependencies and run:

```bash
python3 -m pip install -r addon/appdaemon/requirements-dev.txt
python3 -m pytest addon/appdaemon/tests -q
```

Run commands from the repository root so shared fixtures and paths match CI.
Protocol fixture provenance and sanitization are documented in
[`../../docs/protocol/mqtt-firmware-3.1.48.md`](../../docs/protocol/mqtt-firmware-3.1.48.md).

Additional architecture and development guidance lives in
[`../../docs/architecture.md`](../../docs/architecture.md) and
[`../../docs/development.md`](../../docs/development.md).

## License

This controller is released under the [Unlicense](LICENSE).
