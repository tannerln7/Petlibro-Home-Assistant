from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import backend as backend_module
from backend import ErrorCategory
from protocol import (
    Code, ErrorEventIn, ExecStep, GrainOutputEventIn, GrainOutputType,
    MessageId, NtpIn, NtpSyncIn, Timestamp,
)
from test_plaf203_lifecycle import controller, last_value, night_vision_truth
from test_state_coordinator import FakeLogger


class Clock:
    ms = 1_800_000_000_000
    monotonic = 1000.0

    def advance(self, ms=1000):
        self.ms += ms
        self.monotonic += ms / 1000

    def timestamp(self):
        return Timestamp.from_timestamp_epoch_ms(self.ms)


@pytest.fixture
def recovery(controller, monkeypatch):
    app = controller
    clock = Clock()
    monkeypatch.setattr(Timestamp, "now", staticmethod(clock.timestamp))
    monkeypatch.setattr(backend_module.time, "monotonic", lambda: clock.monotonic)
    app.logger = FakeLogger()
    app.backend.logger = app.logger
    app.backend.client = Mock()
    app.backend.ad = app.ad
    app.backend.is_online = True
    app.backend.food_output_log_start_callback = None
    app.backend.food_output_log_end_callback = None
    app.backend.food_output_progress_callback = None
    app.backend._device_timestamp_sync_drift_check_and_adjust = lambda _ts: None
    app.backend.ntp_sync_status_callback = app._ntp_sync_status
    app.backend.ntp_sync_pending_message_id = None
    app.backend.ntp_sync_timeout_handle = None
    app._went_online()
    return app, clock


def failure(app, clock, code=2026, *, message_id="failed-feed", at=None):
    app.backend._error_event_cb(ErrorEventIn(
        MessageId(message_id), clock.timestamp(), code,
        Timestamp.from_timestamp_epoch_ms(clock.ms if at is None else at),
    ))


def grain(clock, *, step=ExecStep.GRAIN_START, type_=GrainOutputType.MANUAL_FEED,
          message_id="new-feed", **changes):
    event = GrainOutputEventIn(
        message_id=MessageId(message_id), timestamp=clock.timestamp(),
        finished=step == ExecStep.GRAIN_END, type_=type_,
        actual_grain_num=1 if step == ExecStep.GRAIN_END else 0,
        expected_grain_num=1, exec_time=clock.timestamp(), exec_step=step,
        plan_id=1 if type_ == GrainOutputType.FEED_PLAN else None,
    )
    return replace(event, **changes)


def success(app, clock, **context):
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, **context))
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END, **context))


@pytest.mark.parametrize("code", [2008, 2011, 2012, 2024, 2025, 2026, 2027, 2031])
def test_later_full_feed_resolves_known_dispensing_incident(recovery, code):
    app, clock = recovery
    failure(app, clock, code)
    success(app, clock)

    assert last_value(app, "device/error_state") == "false"
    assert any("dispensing capability observed" in msg for _, msg, _ in app.logger.records)


def test_scheduled_failure_requires_successful_scheduled_feed(recovery):
    app, clock = recovery
    failure(app, clock, 2003)
    success(app, clock)
    assert ErrorCategory.SCHEDULED in app._operational_errors

    success(app, clock, type_=GrainOutputType.FEED_PLAN, message_id="scheduled-feed")

    assert last_value(app, "device/error_state") == "false"


@pytest.mark.parametrize("changes", [
    {"actual_grain_num": 0}, {"actual_grain_num": 2},
    {"expected_grain_num": 0, "actual_grain_num": 0},
    {"finished": False}, {"error_code": 2031}, {"error_code": 0xFFFF},
    {"message_id": MessageId("unmatched")},
    {"type_": GrainOutputType.MANUAL_FEED_BUTTON}, {"plan_id": 3},
    {"retried": 1}, {"expected_grain_num": 2, "actual_grain_num": 2},
])
def test_incomplete_failed_or_inconsistent_end_cannot_resolve(recovery, changes):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock))
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END, **changes))

    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_end_without_start_is_not_recovery(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_intermediate_blocking_and_correlated_error_disqualify_candidate(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock))
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_BLOCKING))
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors

    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, message_id="another-feed"))
    failure(app, clock, 2031, message_id="another-feed")
    clock.advance()
    app.backend._grain_output_event_cb(grain(
        clock, step=ExecStep.GRAIN_END, message_id="another-feed"
    ))
    assert ErrorCategory.MOTOR in app._operational_errors


def test_multiple_incidents_keep_unknown_device_and_ntp_after_feed_recovery(recovery):
    app, clock = recovery
    failure(app, clock)
    failure(app, clock, 2031)
    app.backend._remember_grain_operation("unknown-feed")
    failure(app, clock, 9001, message_id="unknown-feed")
    app.backend._error_report("Unclassified device failure")
    app._ntp_sync_status(False)

    success(app, clock)

    assert set(app._operational_errors) == {ErrorCategory.UNKNOWN, ErrorCategory.DEVICE}
    assert last_value(app, "device/error_message") == "Unclassified device failure"
    app._ntp_sync_status(True)
    assert last_value(app, "device/error_state") == "true"


def test_resolving_feeding_error_reveals_independent_ntp_failure(recovery):
    app, clock = recovery
    failure(app, clock)
    app._ntp_sync_status(False)
    success(app, clock)
    assert last_value(app, "device/error_message") == "NTP sync with device failed"


def test_delayed_or_duplicate_older_success_cannot_clear_newer_failure(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    old_start = grain(clock)
    app.backend._grain_output_event_cb(old_start)
    clock.advance()
    old_end = grain(clock, step=ExecStep.GRAIN_END)
    app.backend._grain_output_event_cb(old_end)
    clock.advance()
    failure(app, clock, 2031, message_id="newer-failure")

    app.backend._grain_output_event_cb(old_start)
    app.backend._grain_output_event_cb(old_end)

    assert ErrorCategory.MOTOR in app._operational_errors


def test_duplicate_start_cannot_move_start_past_later_error_receipt(recovery):
    app, clock = recovery
    start = grain(clock)
    app.backend._grain_output_event_cb(start)
    observed = app.backend._recovery_start[1]
    clock.advance()
    failure(app, clock, at=clock.ms - 2000)
    app.backend._grain_output_event_cb(replace(start, timestamp=clock.timestamp()))
    assert app.backend._recovery_start[1] == observed
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_older_failure_does_not_replace_newer_incident_metadata(recovery):
    app, clock = recovery
    failure(app, clock)
    newest = app._operational_errors[ErrorCategory.NO_FOOD]
    failure(app, clock, 2027, message_id="old", at=clock.ms - 1000)
    assert app._operational_errors[ErrorCategory.NO_FOOD] == newest


def test_stale_execution_time_and_clock_correction_prevent_recovery(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    start = grain(clock, exec_time=Timestamp.from_timestamp_epoch_ms(clock.ms - 60_000))
    app.backend._grain_output_event_cb(start)
    assert app.backend._recovery_start is None

    app.backend._grain_output_event_cb(grain(clock))
    app.backend._request_ntp_sync()
    assert app.backend._recovery_start is None
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_disconnect_invalidates_candidate_without_clearing_incident(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock))
    app._went_offline()
    app.coordinator.state_agent.cores.append(night_vision_truth())
    app._went_online()
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_uncoded_mismatch_is_deduplicated_and_recoverable(recovery):
    app, clock = recovery
    event = grain(clock, step=ExecStep.GRAIN_END, actual_grain_num=0)
    app.backend._grain_output_event_cb(event)
    first = app._operational_errors[ErrorCategory.PORTIONS]
    clock.advance()
    app.backend._grain_output_event_cb(event)
    assert app._operational_errors[ErrorCategory.PORTIONS] == first
    success(app, clock, message_id="recovery-feed")
    assert not app._operational_errors


def test_structured_failure_preserves_code_context_and_timestamps(recovery):
    app, clock = recovery
    event = grain(clock, step=ExecStep.GRAIN_END, error_code=2031)
    app.backend._grain_output_event_cb(event)
    error, _received = app._operational_errors[ErrorCategory.MOTOR]
    assert error.code == 2031
    assert error.message_id == event.message_id.data
    assert error.event_time_ms == clock.ms
    assert error.received_time_ms == clock.ms
    assert error.feed_event is event


@pytest.mark.parametrize("offset", [-60_000, 60_000, -1_800_000_000_000])
def test_ambiguous_failure_clock_cannot_be_resolved_automatically(recovery, offset):
    app, clock = recovery
    failure(app, clock, at=clock.ms + offset)
    success(app, clock)
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_new_failure_during_feed_is_not_cleared_by_its_end(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock))
    clock.advance()
    failure(app, clock, 2031, message_id="newer-failure")
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.MOTOR in app._operational_errors
    assert ErrorCategory.NO_FOOD not in app._operational_errors


def test_duplicate_error_and_end_do_not_reopen_resolved_incident(recovery):
    app, clock = recovery
    original_time = clock.ms
    failure(app, clock)
    success(app, clock)
    failure(app, clock, at=original_time)
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert not app._operational_errors


@pytest.mark.parametrize("reconnect", [False, True])
def test_completed_feed_replay_cannot_resolve_late_arriving_failure(recovery, reconnect):
    app, clock = recovery
    start = grain(clock)
    app.backend._grain_output_event_cb(start)
    clock.advance()
    end = grain(clock, step=ExecStep.GRAIN_END)
    app.backend._grain_output_event_cb(end)
    clock.advance()
    failure(app, clock, 2031, at=start.exec_time.to_timestamp_epoch_ms() - 1000)
    if reconnect:
        app._went_offline()
        app.coordinator.state_agent.cores.append(night_vision_truth())
        app._went_online()

    clock.advance()
    app.backend._grain_output_event_cb(start)
    app.backend._grain_output_event_cb(end)

    assert ErrorCategory.MOTOR in app._operational_errors


def test_conflicting_start_identity_disqualifies_candidate(recovery):
    app, clock = recovery
    failure(app, clock)
    clock.advance()
    start = grain(clock)
    app.backend._grain_output_event_cb(start)
    app.backend._grain_output_event_cb(replace(start, expected_grain_num=2))
    clock.advance()
    app.backend._grain_output_event_cb(grain(clock, step=ExecStep.GRAIN_END))
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_future_dated_end_cannot_poison_later_valid_recovery(recovery):
    app, clock = recovery
    failure(app, clock)
    future = Timestamp.from_timestamp_epoch_ms(clock.ms + 60_000)
    app.backend._grain_output_event_cb(grain(
        clock, step=ExecStep.GRAIN_END, exec_time=future, timestamp=future
    ))
    assert app.backend._last_feed_end_ms is None
    success(app, clock)
    assert not app._operational_errors


def test_error_projection_failure_is_isolated_after_grain_ack(recovery):
    app, clock = recovery
    app.backend.error_listen(Mock(side_effect=ValueError("synthetic")))
    event = grain(clock, step=ExecStep.GRAIN_END, error_code=2031)
    app.backend._grain_output_event_cb(event)
    app.backend.client.grain_output_event_send.assert_called_once()
    assert any(msg == "device error callback failed" for _, msg, _ in app.logger.records)


def test_ntp_calibration_response_is_not_sync_success(recovery):
    app, clock = recovery
    app._ntp_sync_status(False)
    stale = Timestamp.from_timestamp_epoch_ms(clock.ms - 60_000)
    app.backend._ntp_cb(NtpIn(stale))

    assert app.backend.client.ntp_send.call_args.args[0].calibration_tag is True
    assert app._ntp_sync_failed
    app.backend._ntp_cb(NtpIn(clock.timestamp()))
    assert not app._ntp_sync_failed


def test_only_correlated_in_tolerance_ntp_ack_clears_ntp_error(recovery):
    app, clock = recovery
    failure(app, clock)
    app._ntp_sync_status(False)
    app.backend._request_ntp_sync()
    pending = app.backend.ntp_sync_pending_message_id
    app.backend._ntp_sync_cb(NtpSyncIn(MessageId("old"), clock.timestamp(), Code.OK))
    assert app._ntp_sync_failed
    app.backend._ntp_sync_cb(NtpSyncIn(MessageId(pending), clock.timestamp(), Code.OK))
    assert not app._ntp_sync_failed
    assert ErrorCategory.NO_FOOD in app._operational_errors


def test_success_callback_failure_does_not_prevent_ack_or_terminal_projection(recovery):
    app, clock = recovery
    order = []
    app.backend.client.grain_output_event_send = lambda _: order.append("ack")
    app.backend.food_output_progress_callback = lambda _: order.append("progress")
    app.backend.feed_success_callback = Mock(side_effect=ValueError("synthetic"))
    success(app, clock)
    assert order == ["ack", "progress", "ack", "progress"]
    app.backend.feed_success_callback.assert_called_once()
    assert app.backend._recovery_start is None


@pytest.mark.parametrize("method", [
    "_manual_feeding_service_cb", "_device_reboot_cb", "_restore_cb",
    "_initialize_sd_card_service_cb", "_wifi_reconnect_service_cb",
    "_attr_set_service_cb", "_feeding_plan_service_cb", "_device_config_sync_cb",
])
def test_request_rejection_is_warning_not_persistent_hardware_fault(recovery, method):
    app, clock = recovery
    response = SimpleNamespace(
        code=2002, message_id=MessageId("rejected"), msg="rejected",
        timestamp=clock.timestamp(),
    )
    getattr(app.backend, method)(response)
    assert not app._operational_errors
    assert any(level == "warning" for level, _, _ in app.logger.records)


@pytest.mark.parametrize("field,value", [
    ("finished", "true"), ("finished", 1), ("actualGrainNum", True),
    ("actualGrainNum", 1.2), ("expectGrainNum", "1"),
    ("expectGrainNum", -1), ("actualGrainNum", 256),
    ("errorCode", 0.5), ("execTime", True), ("msgId", ""),
    ("type", True), ("retried", "1"), ("planId", 1.5),
])
def test_malformed_grain_cannot_be_parsed_as_recovery_evidence(field, value):
    payload = {
        "msgId": "feed", "ts": 1_800_000_000_000, "execTime": 1_800_000_000_000,
        "execStep": "GRAIN_END", "finished": True, "type": 2,
        "actualGrainNum": 1, "expectGrainNum": 1,
    }
    payload[field] = value
    with pytest.raises(ValueError):
        GrainOutputEventIn.from_mqtt_payload(payload)
