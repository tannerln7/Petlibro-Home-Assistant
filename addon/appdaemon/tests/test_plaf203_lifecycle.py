from dataclasses import replace
from unittest.mock import Mock

import pytest
from backend import Backend
from dispensing_status import DispensingStatusProjector
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
