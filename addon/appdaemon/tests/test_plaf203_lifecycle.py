from dataclasses import replace
import datetime
from unittest.mock import Mock

import pytest
from backend import Backend
from compatibility import CompatibilityCoordinator
from dispensing_status import (
    DispensingStatusProjector, FoodOutputProgress,
    RuntimeSnapshotRequest, RuntimeSnapshotResult,
)
from ha_entities import HomeAssistantStatePublisher
from plaf203 import Plaf203
from protocol import ErrorEventIn, MessageId, Timestamp
from state_agent import FeederTruth, RevisionSnapshot, StateAgentUnavailable
from state_coordinator import (
    CommandReceipt,
    FeederState,
    FeederStateCoordinator,
    MqttAck,
    PersistentWriteRequest,
    SettingEqualsPredicate,
)
from test_plaf203_state_integration import CapturingMqtt, Logger
from test_state_agent import core_payload
from test_state_coordinator import FakeAD, FakeAgent
from test_compatibility import FakeAgent as CompatibilityAgent
from storage import Storage


def night_vision_truth(mode="auto"):
    payload = core_payload()
    payload["settings"]["night_vision_mode"] = mode
    payload["setting_classes"]["persistent"].append("night_vision_mode")
    return FeederTruth.from_dict(payload)


@pytest.fixture
def controller():
    app = object.__new__(Plaf203)
    app.ad = FakeAD()
    app.logger = Logger()
    app.serial_number = "SERIAL"
    app.mqtt = CapturingMqtt()
    app.state = HomeAssistantStatePublisher(app.mqtt, app.serial_number, app.logger)
    app._overall_available = False
    app._operational_errors = {}
    app._ntp_sync_failed = False
    app._publish_device_error()
    app.persist_feeder_mqtt = False
    app._feeder_mqtt_persistence_due = False
    app.updater = Mock()
    app.compatibility = Mock()
    app.discovery = Mock()
    app.backend = Backend()
    app.backend.logger = app.logger
    app.backend.error_listen(app._device_error)
    app.backend.feed_success_listen(app._feed_succeeded)
    app.backend.request_runtime_snapshot = Mock()
    app.dispensing_status = DispensingStatusProjector(app.state, app.logger)
    app.coordinator = FeederStateCoordinator(
        app.ad,
        FakeAgent([night_vision_truth()]),
        app.logger,
        app._apply_feeder_truth,
        app._coordinator_availability_set,
    )
    return app


def last_value(app, topic):
    return next(
        payload
        for published_topic, payload, _kwargs in reversed(app.mqtt.published)
        if published_topic == app.state.topic(topic)
    )


def report_feeding_error(app):
    app.backend._error_event_cb(
        ErrorEventIn(
            message_id=MessageId("failed-feed"),
            timestamp=Timestamp.now(),
            error_code=2027,
            trigger_time=Timestamp.now(),
        )
    )
    assert last_value(app, "device/error_state") == "true"
    return last_value(app, "device/error_message")


def set_activation_clock(monkeypatch, hour=12, minute=0):
    class Clock(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2030, 1, 1, hour, minute, tzinfo=datetime.timezone.utc)

    monkeypatch.setattr("plaf203.datetime.datetime", Clock)


@pytest.fixture
def activation_controller(controller, monkeypatch):
    app = controller
    set_activation_clock(monkeypatch)
    values = {}
    storage_ad = Mock()
    storage_ad.entity_exists.side_effect = lambda name, **kw: name in values
    storage_ad.get_state.side_effect = lambda name, **kw: values[name]
    storage_ad.set_state.side_effect = lambda name, state, **kw: values.update(
        {name: state}
    )
    storage_ad.save_namespace.return_value = True
    app.storage = Storage(storage_ad, "plaf203", "SERIAL")
    app._compatibility_snapshot_request = None
    app.updater.update_in_progress = False
    app.backend.is_online = True
    app.backend.connection_generation = 1
    app.backend.runtime_snapshot_pending = None
    app.backend.device_reboot = Mock()
    app._went_online()
    app.dispensing_status.apply_runtime_snapshot(
        RuntimeSnapshotResult(
            request=RuntimeSnapshotRequest("idle", 1, 0, "test"), motor_state=2
        )
    )
    return app


def test_compatibility_activation_saves_guard_before_reboot_and_survives_restart(
    activation_controller,
):
    app = activation_controller
    app.backend.device_reboot.side_effect = lambda: (
        app.storage.ad.save_namespace.assert_called_once_with(namespace="plaf203")
    )
    assert app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_called_once_with()
    # A new Storage instance uses the same persisted guard, not session memory.
    app.storage = Storage(app.storage.ad, "plaf203", "SERIAL")
    assert app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_called_once_with()


@pytest.mark.parametrize("unsafe", [
    "offline", "unavailable", "write", "update", "ntp", "running", "unknown",
])
def test_compatibility_activation_defers_while_safety_is_unknown_or_busy(
    activation_controller, unsafe,
):
    app = activation_controller
    if unsafe == "offline":
        app.backend.is_online = False
    elif unsafe == "unavailable":
        app._overall_available = False
    elif unsafe == "write":
        app.coordinator.state = FeederState.PENDING_WRITE
    elif unsafe == "update":
        app.updater.update_in_progress = True
    elif unsafe == "ntp":
        app._ntp_sync_failed = True
    elif unsafe == "running":
        app.dispensing_status.apply_grain_event(FoodOutputProgress.RUNNING, 1)
    else:
        app.dispensing_status.mark_unavailable()
    assert not app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_not_called()
    app.storage.ad.save_namespace.assert_not_called()


@pytest.mark.parametrize("hour,minute,plan_hour,plan_minute", [
    (10, 58, 11, 0), (11, 2, 11, 0), (0, 1, 23, 59), (23, 59, 0, 1),
])
def test_compatibility_activation_protects_schedule_including_midnight(
    activation_controller, monkeypatch, hour, minute, plan_hour, plan_minute,
):
    app = activation_controller
    set_activation_clock(monkeypatch, hour, minute)
    truth = app.coordinator.latest_truth()
    plan = replace(
        truth.plans.semantic_records[0], hour_utc=plan_hour, minute=plan_minute
    )
    app.coordinator._latest_truth = replace(
        truth, plans=replace(truth.plans, semantic_records=(plan,))
    )
    assert not app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_not_called()


def test_snapshot_failure_does_not_cause_endless_activation_probe_warnings(
    activation_controller,
):
    app = activation_controller
    app.dispensing_status.mark_unavailable()
    for _ in range(3):
        assert not app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.request_runtime_snapshot.assert_called_once_with(
        "compatibility activation"
    )
    app.dispensing_status.apply_grain_event(FoodOutputProgress.IDLE, 1)
    assert not app._activate_compatibility_if_safe("0.4.0-rc.3")
    assert app.backend.request_runtime_snapshot.call_count == 2
    app.backend.device_reboot.assert_not_called()


def test_failed_reboot_guard_save_prevents_reboot_and_allows_safe_retry(
    activation_controller,
):
    app = activation_controller
    app.storage.ad.save_namespace.return_value = False
    with pytest.raises(RuntimeError, match="could not be saved"):
        app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_not_called()
    assert app.storage.compatibility_reboot_version_get() != "0.4.0-rc.3"
    app.storage.ad.save_namespace.return_value = True
    assert app._activate_compatibility_if_safe("0.4.0-rc.3")
    app.backend.device_reboot.assert_called_once_with()


def test_fresh_correlated_runtime_result_wakes_pending_activation(activation_controller):
    app = activation_controller
    app._runtime_snapshot_result(RuntimeSnapshotResult(
        RuntimeSnapshotRequest("fresh", 1, 0, "test"), motor_state=2,
    ))
    app.compatibility.on_runtime_snapshot.assert_called_once_with()
    app.compatibility.on_runtime_snapshot.reset_mock()
    app._runtime_snapshot_result(RuntimeSnapshotResult(
        RuntimeSnapshotRequest("stale", 0, 0, "test"), motor_state=2,
    ))
    app.compatibility.on_runtime_snapshot.assert_not_called()


def test_release_reconcile_to_live_idle_to_reboot_to_actual_active_projection(
    activation_controller,
):
    app = activation_controller
    agent = CompatibilityAgent()
    agent.status = replace(agent.status, enabled=True, reboot_required=True)
    app.compatibility = CompatibilityCoordinator(
        app.ad, app.state, agent, app.logger, app._activate_compatibility_if_safe
    )
    app.dispensing_status.mark_unavailable()
    app.compatibility.on_state_agent_update_complete("0.4.0")
    app.backend.device_reboot.assert_not_called()
    assert last_value(app, "compatibility/active") == "false"

    app._runtime_snapshot_result(RuntimeSnapshotResult(
        RuntimeSnapshotRequest("fresh", 1, 0, "compatibility activation"), motor_state=2,
    ))
    app.backend.device_reboot.assert_called_once_with()
    assert agent.calls == ["reconcile", "reconcile"]
    assert app.compatibility._activation_timer is None
    assert last_value(app, "compatibility/active") == "false"

    agent.status = replace(agent.status, active=True, reboot_required=False)
    app.compatibility.on_usable_connection()
    app.backend.device_reboot.assert_called_once_with()
    assert last_value(app, "compatibility/active") == "true"


def test_initial_usable_connection_runs_lifecycle_once(controller):
    app = controller
    assert last_value(app, "device/error_state") == "false"
    app._went_online()
    app._device_started()

    assert app.coordinator.state == FeederState.READY
    assert last_value(app, "device/online") == "true"
    app.updater.on_usable_connection.assert_called_once_with()
    app.compatibility.on_usable_connection.assert_called_once_with()


def test_verified_night_vision_writes_preserve_error_and_do_not_reconnect(controller):
    app = controller
    app._went_online()
    error = report_feeding_error(app)

    for mode in ("on", "off", "auto"):
        app.coordinator.state_agent.cores.append(night_vision_truth(mode))
        request = PersistentWriteRequest(
            control="camera.night_vision",
            target=mode,
            publisher=lambda _truth: CommandReceipt("ATTR_SET_SERVICE", mode),
            predicate=SettingEqualsPredicate("night_vision_mode", mode),
        )
        assert app.coordinator.request_persistent_write(request)
        app.coordinator.on_mqtt_ack(MqttAck("ATTR_SET_SERVICE", mode, True))
        assert app.coordinator.state == FeederState.VERIFYING_WRITE
        app.ad.run_next_timer()

        assert app.coordinator.state == FeederState.READY
        assert app.coordinator.pending_write is None
        assert app.coordinator.latest_truth().settings["night_vision_mode"] == mode
        assert last_value(app, "device/online") == "true"
        assert last_value(app, "device/error_state") == "true"
        assert last_value(app, "device/error_message") == error

    assert last_value(app, "camera/night_vision") == "AUTOMATIC"
    app.updater.on_usable_connection.assert_called_once_with()
    app.compatibility.on_usable_connection.assert_called_once_with()


def test_revision_refresh_does_not_run_connection_lifecycle(controller):
    app = controller
    app._went_online()
    error = report_feeding_error(app)
    current = app.coordinator.latest_truth()
    changed = replace(current, revisions=replace(current.revisions, core_rev="new"))
    app.coordinator.state_agent.cores.append(changed)
    app.coordinator.state_agent.revision_value = RevisionSnapshot(
        read_ms=1, revisions=changed.revisions, queue=changed.queue
    )

    app.coordinator.on_heartbeat()

    assert app.coordinator.latest_truth() is changed
    assert app.coordinator.state == FeederState.READY
    assert last_value(app, "device/error_message") == error
    app.updater.on_usable_connection.assert_called_once_with()
    app.compatibility.on_usable_connection.assert_called_once_with()


def test_reconnection_runs_lifecycle_without_clearing_feeding_error(controller):
    app = controller
    app._went_online()
    error = report_feeding_error(app)

    app._went_offline()
    assert last_value(app, "device/online") == "false"
    assert last_value(app, "food_output/progress_available") == "false"
    app.coordinator.state_agent.cores.append(night_vision_truth())
    app._went_online()

    assert last_value(app, "device/online") == "true"
    assert last_value(app, "device/error_state") == "true"
    assert last_value(app, "device/error_message") == error
    assert app.updater.on_usable_connection.call_count == 2
    assert app.compatibility.on_usable_connection.call_count == 2


def test_state_agent_recovery_without_mqtt_reconnect_runs_lifecycle(controller):
    app = controller
    app._went_online()
    agent = app.coordinator.state_agent
    agent.revisions = Mock(side_effect=StateAgentUnavailable("temporarily offline"))

    app.coordinator.on_heartbeat()
    assert app.coordinator.mqtt_connected
    assert last_value(app, "device/online") == "false"
    agent.cores.append(night_vision_truth())
    app.coordinator.on_heartbeat()

    assert app.coordinator.state == FeederState.READY
    assert last_value(app, "device/online") == "true"
    assert app.updater.on_usable_connection.call_count == 2
    assert app.compatibility.on_usable_connection.call_count == 2


@pytest.mark.parametrize("available", [True, False])
def test_ha_birth_restores_projections_and_keeps_dedicated_lifecycle(
    controller, available
):
    app = controller
    app._went_online()
    error = report_feeding_error(app)
    if not available:
        app._went_offline()
    app.mqtt.published.clear()

    app._home_assistant_status_cb("MQTT_MESSAGE", {"payload": b"online"}, {})

    assert last_value(app, "device/online") == str(available).lower()
    assert last_value(app, "device/error_state") == "true"
    assert last_value(app, "device/error_message") == error
    assert last_value(app, "camera/night_vision") == "AUTOMATIC"
    assert last_value(app, "food_output/progress_available") == "false"
    app.discovery.discovery_issue.assert_called_once_with()
    app.updater.on_usable_connection.assert_called_once_with()
    app.compatibility.on_usable_connection.assert_called_once_with()
    app.updater.on_home_assistant_birth.assert_called_once_with()
    app.compatibility.on_home_assistant_birth.assert_called_once_with()
    app.backend.request_runtime_snapshot.assert_called_once_with(
        "Home Assistant birth"
    )


def test_device_start_still_queues_mqtt_persistence_when_already_available(controller):
    app = controller
    app._went_online()
    app.persist_feeder_mqtt = True
    app.feeder_mqtt_host = "mqtt.example.invalid"
    app.feeder_mqtt_port = 1883
    app.feeder_https_addr = None
    app.tutk_p2p_region = "REGION_US"
    app.backend.device_config_sync = Mock(
        return_value=CommandReceipt("DEVICE_CONFIG_SYNC", "persist-mqtt")
    )

    app._device_started()

    assert not app._feeder_mqtt_persistence_due
    assert (
        app.coordinator.pending_write.request.control
        == "device.feeder_mqtt_destination"
    )
    app.backend.device_config_sync.assert_called_once_with(
        app.feeder_mqtt_host, 1883, None, "REGION_US"
    )
    app.updater.on_usable_connection.assert_called_once_with()
    app.compatibility.on_usable_connection.assert_called_once_with()


def test_successful_ntp_sync_resolves_only_ntp_failure(controller):
    app = controller
    app._ntp_sync_status(False)
    assert last_value(app, "device/error_state") == "true"
    assert last_value(app, "device/error_message") == "NTP sync with device failed"

    app._ntp_sync_status(True)

    assert last_value(app, "device/error_state") == "false"
    assert last_value(app, "device/error_message") == "No error"
    assert isinstance(last_value(app, "device/ntp_last_correct"), int)


@pytest.mark.parametrize("ntp_failure_first", [True, False])
def test_ntp_success_cannot_resolve_or_obscure_feeding_error(
    controller, ntp_failure_first
):
    app = controller
    if ntp_failure_first:
        app._ntp_sync_status(False)
    error = report_feeding_error(app)
    if not ntp_failure_first:
        app._ntp_sync_status(False)
    assert last_value(app, "device/error_message") == error

    app._ntp_sync_status(True)

    assert last_value(app, "device/error_state") == "true"
    assert last_value(app, "device/error_message") == error
