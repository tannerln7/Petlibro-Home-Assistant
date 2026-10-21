from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import Mock

import pytest
from compatibility import CompatibilityCoordinator
from ha_entities import HomeAssistantDiscoveryMqtt
from petlibro_logging import PetlibroLogger
from state_agent import (
    CompatibilityStatus,
    StateAgentConflict,
    StateAgentUnsupported,
)


class FakeAd:
    def __init__(self):
        self.logs = []
        self.executor_errors = []
        self.timers = []

    def log(self, message, **_kwargs):
        self.logs.append(message)

    def submit_to_executor(self, worker, callback):
        try:
            result = worker()
        except Exception as error:
            # AppDaemon logs the worker error and does not dispatch its callback.
            self.executor_errors.append(error)
            return
        callback(result)

    def run_in(self, callback, delay, **kwargs):
        timer = (callback, delay, kwargs)
        self.timers.append(timer)
        return timer

    def cancel_timer(self, timer, _silent):
        self.timers.remove(timer)


class FakeState:
    def __init__(self):
        self.messages = []

    def publish(self, topic, value, *, retain=False):
        self.messages.append((topic, value, retain))


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.status = CompatibilityStatus(
            asset_version="0.4.0",
            original_ping_path="/bin/ping",
            supported=True,
            deployed=True,
            shim_current=True,
            startup_current=True,
            backup_available=True,
            enabled=False,
            active=False,
            reboot_required=False,
            last_reason="already_current",
        )

    def compatibility_status(self):
        self.calls.append("status")
        return self.status

    def compatibility_reconcile(self):
        self.calls.append("reconcile")
        return self.status

    def compatibility_deploy(self):
        self.calls.append("deploy")
        return self.status

    def compatibility_enable(self):
        self.calls.append("enable")
        return self.status

    def compatibility_disable(self):
        self.calls.append("disable")
        return self.status


class FakeMqtt:
    def __init__(self):
        self.published = []

    def mqtt_publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


def test_compatibility_coordinator_reconciles_and_projects_only_active_state():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.start()
    coordinator.on_usable_connection()

    assert agent.calls == ["reconcile"]
    latest = {topic: value for topic, value, _retain in state.messages}
    assert latest == {"compatibility/active": False}
    assert all(retain for _topic, _value, retain in state.messages)


def test_compatibility_coordinator_sanitizes_operation_failure_projection():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    agent.compatibility_reconcile = lambda: (_ for _ in ()).throw(
        RuntimeError("sensitive implementation detail")
    )
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.request_reconcile(reason="test")

    assert state.messages == []
    assert coordinator._busy is False
    assert ad.executor_errors == []
    assert "sensitive implementation detail" not in "\n".join(ad.logs)


def test_reconcile_falls_back_to_fixed_operations_on_rc1_and_rc2_agents():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    agent.compatibility_reconcile = lambda: (_ for _ in ()).throw(
        StateAgentUnsupported("not available")
    )
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.on_usable_connection()

    assert agent.calls == ["status", "deploy", "enable"]
    assert state.messages[-1][0:2] == ("compatibility/active", False)


def test_update_conflict_schedules_a_bounded_reconciliation_retry():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    agent.compatibility_reconcile = lambda: (_ for _ in ()).throw(
        StateAgentConflict("state_agent_update_in_progress")
    )
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.on_usable_connection()

    assert len(ad.timers) == 1
    callback, delay, kwargs = ad.timers.pop()
    assert delay == 30
    agent.compatibility_reconcile = lambda: agent.status
    callback(kwargs)
    assert state.messages[-1][0:2] == ("compatibility/active", False)


def test_discovery_exposes_only_actual_ping_mitigation_state_and_removes_old_entities():
    mqtt = FakeMqtt()
    HomeAssistantDiscoveryMqtt(mqtt, "SERIAL").discovery_issue()
    documents = {
        topic: json.loads(payload)
        for topic, payload, _kwargs in mqtt.published
        if topic.startswith("homeassistant/") and payload
    }
    mitigation = next(
        value
        for topic, value in documents.items()
        if topic.endswith("/compatibility_active/config")
    )
    assert mitigation["name"] == "Ping mitigation active"
    assert mitigation["state_topic"].endswith("/compatibility/active")
    assert "command_topic" not in mitigation

    removed = {
        topic
        for topic, payload, _kwargs in mqtt.published
        if topic.startswith("homeassistant/") and payload == ""
    }
    assert len(removed) == 6
    assert any(topic.endswith("/compatibility_deploy/config") for topic in removed)
    assert any(
        topic.endswith("/compatibility_ping_mitigation/config") for topic in removed
    )


def activation_coordinator():
    ad = FakeAd()
    agent = FakeAgent()
    agent.status = replace(agent.status, enabled=True, reboot_required=True)
    activate = Mock(return_value=False)
    coordinator = CompatibilityCoordinator(
        ad, FakeState(), agent, PetlibroLogger(ad, "test", "trace"), activate
    )
    return coordinator, ad, agent, activate


def test_activation_waits_for_safety_then_rechecks_status_on_fresh_runtime():
    coordinator, ad, agent, activate = activation_coordinator()
    coordinator.on_state_agent_update_complete("0.4.0")
    activate.assert_called_once_with("0.4.0")
    assert len(ad.timers) == 1

    activate.return_value = True
    coordinator.on_runtime_snapshot()

    assert agent.calls == ["reconcile", "reconcile"]
    assert activate.call_count == 2
    assert ad.timers == []
    assert coordinator.state.messages[-1] == ("compatibility/active", False, True)


@pytest.mark.parametrize("changes", [
    {"supported": False}, {"deployed": False}, {"enabled": False},
    {"active": True}, {"reboot_required": False}, {"backup_available": False},
    {"shim_current": False}, {"startup_current": False},
])
def test_automatic_activation_requires_complete_supported_pending_status(changes):
    coordinator, ad, agent, activate = activation_coordinator()
    agent.status = replace(agent.status, **changes)
    coordinator.on_usable_connection()
    activate.assert_not_called()
    assert ad.timers == []


def test_activation_timer_does_not_reboot_from_cached_status():
    coordinator, ad, agent, activate = activation_coordinator()
    coordinator.on_usable_connection()
    agent.status = replace(agent.status, active=True, reboot_required=False)
    callback, _delay, kwargs = ad.timers.pop()
    callback(kwargs)
    assert agent.calls == ["reconcile", "reconcile"]
    assert activate.call_count == 1
    assert ad.timers == []


def test_activation_never_uses_a_stale_result_during_update_conflict():
    coordinator, ad, agent, activate = activation_coordinator()
    coordinator.on_usable_connection()
    agent.compatibility_reconcile = Mock(
        side_effect=StateAgentConflict("state_agent_update_in_progress")
    )
    callback, _delay, kwargs = ad.timers.pop()
    callback(kwargs)
    assert activate.call_count == 1
    assert len(ad.timers) == 1


def test_stop_cancels_activation_and_ignores_late_executor_completion():
    coordinator, ad, agent, activate = activation_coordinator()
    coordinator.on_usable_connection()
    coordinator.stop()
    coordinator._finished(action="reconcile", result=agent.status)
    coordinator.on_runtime_snapshot()
    assert activate.call_count == 1
    assert ad.timers == []


def test_activation_callback_failure_is_sanitized_and_does_not_break_reconciliation():
    coordinator, ad, _agent, activate = activation_coordinator()
    activate.side_effect = RuntimeError("private detail")
    coordinator.on_usable_connection()
    assert len(ad.timers) == 1
    assert "private detail" not in "\n".join(ad.logs)
    assert ad.executor_errors == []


def test_older_agent_without_compatibility_api_never_requests_reboot():
    coordinator, ad, agent, activate = activation_coordinator()
    agent.compatibility_reconcile = Mock(side_effect=StateAgentUnsupported("old"))
    agent.compatibility_status = Mock(side_effect=StateAgentUnsupported("old"))
    coordinator.on_usable_connection()
    activate.assert_not_called()
    assert coordinator.state.messages == []
    assert ad.timers == []
