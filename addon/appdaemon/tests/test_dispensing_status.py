from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import backend as backend_module
from dispensing_status import (
    DispensingStatusProjector,
    FoodOutputProgress,
    RuntimeSnapshotRequest,
    RuntimeSnapshotResult,
)
from ha_entities import HomeAssistantDiscoveryMqtt, HomeAssistantStatePublisher
from plaf203 import Plaf203
from protocol import Code, ExecStep, MessageId, Timestamp
from telemetry import TelemetryPublisher


class Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class CapturingState:
    def __init__(self):
        self.published = []
        self.cleared = []

    def publish(self, topic, value, *, retain=False):
        self.published.append((topic, value, retain))

    def clear_retained(self, topic):
        self.cleared.append(topic)


class TimerAd:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def run_in(self, callback, delay, **kwargs):
        handle = f"timer-{len(self.scheduled) + 1}"
        self.scheduled.append((handle, callback, delay, kwargs))
        return handle

    def cancel_timer(self, handle, silent):
        self.cancelled.append((handle, silent))


class RuntimeClient:
    def __init__(self):
        self.attr_get = []
        self.attr_push_ack = []
        self.grain_ack = []

    def attr_get_service_send(self, message):
        self.attr_get.append(message)

    def attr_push_event_send(self, message):
        self.attr_push_ack.append(message)

    def grain_output_event_send(self, message):
        self.grain_ack.append(message)


class DiscoveryMqtt:
    def __init__(self):
        self.published = []

    def mqtt_publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


def request(message_id="request", connection=1, runtime_generation=0):
    return RuntimeSnapshotRequest(
        message_id=message_id,
        connection_generation=connection,
        runtime_event_generation=runtime_generation,
        reason="test",
    )


def connected_projector():
    state = CapturingState()
    projector = DispensingStatusProjector(state, Logger())
    projector.initialize()
    projector.on_feeder_connected(1)
    state.published.clear()
    return projector, state


def test_automatic_reboot_idle_proof_expires_and_is_invalidated_by_runtime_events(
    monkeypatch,
):
    import dispensing_status

    clock = [100.0]
    monkeypatch.setattr(dispensing_status.time, "monotonic", lambda: clock[0])
    projector, _state = connected_projector()
    assert not projector.idle_confirmed_recently(20)
    projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=request(), motor_state=2)
    )
    assert projector.idle_confirmed_recently(20)
    clock[0] = 121
    assert not projector.idle_confirmed_recently(20)
    clock[0] = 110
    projector.apply_grain_event(FoodOutputProgress.IDLE, 1)
    assert not projector.idle_confirmed_recently(20)
    projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=request(runtime_generation=1), motor_state=2)
    )
    assert projector.idle_confirmed_recently(20)
    projector.mark_unavailable()
    assert not projector.idle_confirmed_recently(20)


@pytest.mark.parametrize(
    ("motor_state", "expected"),
    [
        (1, FoodOutputProgress.RUNNING),
        (2, FoodOutputProgress.IDLE),
        (3, FoodOutputProgress.RECOVERING),
    ],
)
def test_fresh_motor_state_mapping_establishes_progress_and_availability(
    motor_state, expected
):
    projector, state = connected_projector()

    accepted = projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=request(), motor_state=motor_state)
    )

    assert accepted
    assert projector.current_state is expected
    assert state.published == [
        ("food_output/progress", expected, False),
        ("food_output/progress_available", True, False),
    ]


@pytest.mark.parametrize("motor_state", [0, 4, -1, None, "bad"])
def test_unknown_motor_state_is_unavailable_and_never_infers_idle(motor_state):
    projector, state = connected_projector()

    accepted = projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=request(), motor_state=motor_state)
    )

    assert not accepted
    assert projector.current_state is None
    assert state.published == [("food_output/progress_available", False, False)]


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (FoodOutputProgress.RUNNING, FoodOutputProgress.RUNNING),
        (FoodOutputProgress.BLOCKED, FoodOutputProgress.BLOCKED),
        (FoodOutputProgress.IDLE, FoodOutputProgress.IDLE),
    ],
)
def test_grain_events_drive_immediate_progress(progress, expected):
    projector, state = connected_projector()

    assert projector.apply_grain_event(progress, 1)

    assert projector.runtime_event_generation == 1
    assert projector.current_state is expected
    assert state.published[-2:] == [
        ("food_output/progress", expected, False),
        ("food_output/progress_available", True, False),
    ]


def test_newer_grain_event_wins_over_matching_older_runtime_response():
    projector, state = connected_projector()
    pending = request(runtime_generation=projector.runtime_event_generation_get())
    projector.runtime_snapshot_started(pending)
    assert projector.apply_grain_event(FoodOutputProgress.RUNNING, 1)

    accepted = projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=pending, motor_state=2)
    )

    assert not accepted
    assert projector.current_state is FoodOutputProgress.RUNNING
    assert projector.available
    assert not any(
        topic == "food_output/progress" and value is FoodOutputProgress.IDLE
        for topic, value, _retain in state.published
    )


def test_timeout_after_newer_grain_event_does_not_make_progress_unavailable():
    projector, _state = connected_projector()
    pending = request(runtime_generation=projector.runtime_event_generation_get())
    projector.runtime_snapshot_started(pending)
    projector.apply_grain_event(FoodOutputProgress.BLOCKED, 1)

    accepted = projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=pending, failure_reason="timeout")
    )

    assert not accepted
    assert projector.current_state is FoodOutputProgress.BLOCKED
    assert projector.available


def test_later_runtime_request_can_refine_blocked_to_recovering():
    projector, _state = connected_projector()
    projector.apply_grain_event(FoodOutputProgress.BLOCKED, 1)
    later = request(runtime_generation=projector.runtime_event_generation_get())

    assert projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=later, motor_state=3)
    )
    assert projector.current_state is FoodOutputProgress.RECOVERING


def test_reconnect_rejects_prior_connection_runtime_response():
    projector, _state = connected_projector()
    old = request(connection=1)
    projector.on_feeder_disconnected()
    projector.on_feeder_connected(2)

    assert not projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=old, motor_state=2)
    )
    assert not projector.available


def configured_backend(runtime_generation=0):
    backend = backend_module.Backend()
    backend.ad = TimerAd()
    backend.logger = Logger()
    backend.client = RuntimeClient()
    backend.is_online = True
    backend.connection_generation = 7
    backend.runtime_snapshot_pending = None
    backend.runtime_snapshot_timeout_handle = None
    backend.runtime_snapshot_started_callback = None
    backend.runtime_snapshot_result_callback = None
    backend.runtime_event_generation_getter = lambda: runtime_generation
    backend.persistent_state_hint_callback = None
    diagnostics = []
    backend._publish_attr_telemetry = diagnostics.append
    backend._device_timestamp_sync_drift_check_and_adjust = lambda _timestamp: None
    return backend, diagnostics


def runtime_response(message_id, *, code=Code.OK, motor_state=2):
    return SimpleNamespace(
        message_id=MessageId(message_id),
        timestamp=Timestamp.now(),
        code=code,
        motor_state=motor_state,
    )


def test_runtime_request_records_all_local_ordering_fields():
    backend, _diagnostics = configured_backend(runtime_generation=12)
    started = []
    results = []
    backend.runtime_snapshot_listen(started.append, results.append, lambda: 12)

    assert backend.request_runtime_snapshot("HA birth")

    pending = backend.runtime_snapshot_pending
    assert pending is not None
    assert pending.message_id == backend.client.attr_get[0].message_id.data
    assert pending.connection_generation == 7
    assert pending.runtime_event_generation == 12
    assert pending.reason == "HA birth"
    assert started == [pending]
    assert results == []


def test_only_matching_successful_runtime_response_satisfies_request():
    backend, diagnostics = configured_backend()
    results = []
    backend.runtime_snapshot_result_callback = results.append
    backend.request_runtime_snapshot("test")
    pending = backend.runtime_snapshot_pending

    backend._attr_get_service_cb(runtime_response("different", motor_state=1))
    assert backend.runtime_snapshot_pending is pending
    assert results == []

    backend._attr_get_service_cb(runtime_response(pending.message_id, motor_state=1))
    assert backend.runtime_snapshot_pending is None
    assert results == [RuntimeSnapshotResult(request=pending, motor_state=1)]
    assert len(diagnostics) == 2


def test_old_connection_response_cannot_satisfy_runtime_request():
    backend, _diagnostics = configured_backend()
    results = []
    backend.runtime_snapshot_result_callback = results.append
    backend.request_runtime_snapshot("test")
    pending = backend.runtime_snapshot_pending
    backend.connection_generation += 1

    backend._attr_get_service_cb(runtime_response(pending.message_id))

    assert results == []
    assert backend.runtime_snapshot_pending is None


def test_non_ok_runtime_response_and_timeout_report_failure():
    backend, _diagnostics = configured_backend()
    results = []
    backend.runtime_snapshot_result_callback = results.append
    backend.request_runtime_snapshot("rejected")
    rejected = backend.runtime_snapshot_pending
    backend._attr_get_service_cb(
        runtime_response(rejected.message_id, code=Code.ERROR_1)
    )
    assert results[-1].failure_reason is not None

    backend.request_runtime_snapshot("timeout")
    timed_out = backend.runtime_snapshot_pending
    backend._runtime_snapshot_timeout(
        {
            "message_id": timed_out.message_id,
            "connection_generation": timed_out.connection_generation,
        }
    )
    assert results[-1].request is timed_out
    assert results[-1].failure_reason == "runtime snapshot request timed out"


def test_new_runtime_request_replaces_old_and_old_response_cannot_complete_new():
    backend, _diagnostics = configured_backend()
    results = []
    backend.runtime_snapshot_result_callback = results.append
    backend.request_runtime_snapshot("old")
    old = backend.runtime_snapshot_pending
    backend.request_runtime_snapshot("new")
    new = backend.runtime_snapshot_pending

    backend._attr_get_service_cb(runtime_response(old.message_id, motor_state=1))

    assert backend.runtime_snapshot_pending is new
    assert results == []


def test_sparse_attr_push_cannot_satisfy_solicited_runtime_request():
    backend, diagnostics = configured_backend()
    backend.request_runtime_snapshot("test")
    pending = backend.runtime_snapshot_pending
    sparse = SimpleNamespace(message_id=MessageId("push"), timestamp=Timestamp.now())

    backend._attr_push_event_cb(sparse)

    assert backend.runtime_snapshot_pending is pending
    assert len(backend.client.attr_push_ack) == 1
    assert diagnostics == [sparse]


@pytest.mark.parametrize(
    ("step", "expected"),
    [
        (ExecStep.GRAIN_START, FoodOutputProgress.RUNNING),
        (ExecStep.GRAIN_BLOCKING, FoodOutputProgress.BLOCKED),
        (ExecStep.GRAIN_END, FoodOutputProgress.IDLE),
    ],
)
def test_backend_preserves_grain_event_mappings(step, expected):
    backend, _diagnostics = configured_backend()
    progress = []
    backend.food_output_progress_callback = progress.append
    backend.food_output_log_start_callback = None
    backend.food_output_log_end_callback = None
    backend.error_callback = None
    event = SimpleNamespace(
        exec_step=step,
        type_=SimpleNamespace(),
        expected_grain_num=1,
        actual_grain_num=1,
        message_id=MessageId("grain"),
        timestamp=Timestamp.now(),
        exec_time=Timestamp.now(),
        error_code=None,
        finished=step == ExecStep.GRAIN_END,
    )

    backend._grain_output_event_cb(event)

    assert progress == [expected]


def test_ha_birth_republishes_availability_and_requests_runtime_without_reconnect():
    app = object.__new__(Plaf203)
    app.logger = Logger()
    app._overall_available = True
    app._operational_errors = {}
    app._ntp_sync_failed = False
    app.state = CapturingState()
    app.discovery = SimpleNamespace(calls=0)
    app.discovery.discovery_issue = lambda: setattr(
        app.discovery, "calls", app.discovery.calls + 1
    )
    app.dispensing_status = DispensingStatusProjector(app.state, app.logger)
    app.dispensing_status.on_feeder_connected(4)
    app.state.published.clear()
    requests = []
    app.backend = SimpleNamespace(
        request_runtime_snapshot=lambda reason: requests.append(reason)
    )
    app.coordinator = SimpleNamespace(latest_truth=lambda: None)

    app._home_assistant_status_cb(
        "MQTT_MESSAGE", {"payload": "online"}, {}
    )

    assert app.discovery.calls == 1
    assert ("device/online", True, False) in app.state.published
    assert (
        "food_output/progress_available",
        False,
        False,
    ) in app.state.published
    assert requests == ["Home Assistant birth"]


@pytest.mark.parametrize(
    ("motor_state", "expected"),
    [
        (2, FoodOutputProgress.IDLE),
        (1, FoodOutputProgress.RUNNING),
        (3, FoodOutputProgress.RECOVERING),
    ],
)
def test_ha_birth_correlated_response_restores_each_recognized_state(
    motor_state, expected
):
    app = object.__new__(Plaf203)
    app.logger = Logger()
    app._overall_available = True
    app._operational_errors = {}
    app._ntp_sync_failed = False
    app.state = CapturingState()
    app.discovery = SimpleNamespace(discovery_issue=lambda: None)
    app.coordinator = SimpleNamespace(latest_truth=lambda: None)
    app.backend, _diagnostics = configured_backend()
    app.dispensing_status = DispensingStatusProjector(app.state, app.logger)
    app.dispensing_status.on_feeder_connected(app.backend.connection_generation)
    app.backend.runtime_snapshot_listen(
        app.dispensing_status.runtime_snapshot_started,
        app.dispensing_status.apply_runtime_snapshot,
        app.dispensing_status.runtime_event_generation_get,
    )

    app._home_assistant_status_cb(
        "MQTT_MESSAGE", {"payload": "online"}, {}
    )
    pending = app.backend.runtime_snapshot_pending
    app.backend._attr_get_service_cb(
        runtime_response(pending.message_id, motor_state=motor_state)
    )

    assert app.dispensing_status.current_state is expected
    assert app.dispensing_status.available
    assert ("food_output/progress", expected, False) in app.state.published


def test_progress_discovery_preserves_identity_and_uses_dedicated_availability():
    mqtt = DiscoveryMqtt()
    discovery = HomeAssistantDiscoveryMqtt(mqtt, "SERIAL")

    discovery.discovery_issue()

    topic, payload, kwargs = next(
        row for row in mqtt.published if row[0].endswith("/food_output_progress/config")
    )
    config = json.loads(payload)
    assert topic == "homeassistant/sensor/plaf203_SERIAL/food_output_progress/config"
    assert '"RECOVERING":"Recovering"' in config["value_template"]
    assert (
        config["availability_topic"]
        == "plaf203/SERIAL/food_output/progress_available"
    )
    assert kwargs["retain"] is True


def test_progress_is_non_retained_tombstone_is_exact_and_last_dispense_is_retained():
    state = CapturingState()
    projector = DispensingStatusProjector(state, Logger())
    projector.initialize()
    projector.initialize()
    projector.on_feeder_connected(1)
    projector.apply_runtime_snapshot(
        RuntimeSnapshotResult(request=request(), motor_state=1)
    )

    telemetry = TelemetryPublisher(state, Logger())
    source = SimpleNamespace()
    telemetry.food_output_start(source, 3)
    telemetry.food_output_end(source, 2)

    assert state.cleared == ["food_output/progress"]
    progress_publishes = [row for row in state.published if row[0] == "food_output/progress"]
    assert progress_publishes == [
        ("food_output/progress", FoodOutputProgress.RUNNING, False)
    ]
    retained_topics = {topic for topic, _value, retain in state.published if retain}
    assert retained_topics == {
        "food_output/last_start",
        "food_output/last_end",
        "food_output/last_grain_count",
        "food_output/last_trigger",
    }
    availability_publishes = [
        row for row in state.published if row[0] == "food_output/progress_available"
    ]
    assert availability_publishes
    assert all(not retain for _topic, _value, retain in availability_publishes)


def test_retained_progress_migration_tombstone_uses_only_the_exact_state_topic():
    mqtt = DiscoveryMqtt()
    state = HomeAssistantStatePublisher(mqtt, "SERIAL", Logger())

    state.clear_retained("food_output/progress")

    assert mqtt.published == [
        (
            "plaf203/SERIAL/food_output/progress",
            "",
            {"namespace": "mqtt", "retain": True},
        )
    ]
