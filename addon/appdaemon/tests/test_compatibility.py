from __future__ import annotations

import json
from compatibility import CompatibilityCoordinator
from commands import CommandRouter
from ha_entities import HomeAssistantDiscoveryMqtt
from petlibro_logging import PetlibroLogger
from state_agent import CompatibilityStatus


class FakeAd:
    def __init__(self):
        self.logs = []
        self.executor_errors = []

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


def test_compatibility_coordinator_projects_fixed_operation_status():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.start()
    coordinator.request_deploy()
    coordinator.request_enabled(True)
    coordinator.request_enabled(False)

    assert agent.calls == ["deploy", "enable", "disable"]
    latest = {topic: value for topic, value, _retain in state.messages}
    assert latest["compatibility/deployment"] == "deployed"
    assert latest["compatibility/deployed"] is True
    assert latest["compatibility/ping_mitigation"] is False
    assert latest["compatibility/reboot_required"] is False
    assert all(retain for _topic, _value, retain in state.messages)


def test_compatibility_coordinator_sanitizes_operation_failure_projection():
    ad = FakeAd()
    state = FakeState()
    agent = FakeAgent()
    agent.compatibility_deploy = lambda: (_ for _ in ()).throw(
        RuntimeError("sensitive implementation detail")
    )
    agent.compatibility_status = agent.compatibility_deploy
    coordinator = CompatibilityCoordinator(
        ad, state, agent, PetlibroLogger(ad, "test", "trace")
    )

    coordinator.request_deploy()

    latest = {topic: value for topic, value, _retain in state.messages}
    assert latest["compatibility/deployment"] == "unknown"
    assert latest["compatibility/last_reason"] == "refresh_failed"
    assert coordinator._busy is False
    assert ad.executor_errors == []
    assert "sensitive implementation detail" not in "\n".join(ad.logs)


def test_discovery_exposes_separate_deploy_activation_and_status_entities():
    mqtt = FakeMqtt()
    HomeAssistantDiscoveryMqtt(mqtt, "SERIAL").discovery_issue()
    documents = {
        topic: json.loads(payload)
        for topic, payload, _kwargs in mqtt.published
        if topic.startswith("homeassistant/")
    }

    deploy = next(
        value
        for topic, value in documents.items()
        if topic.endswith("/compatibility_deploy/config")
    )
    mitigation = next(
        value
        for topic, value in documents.items()
        if topic.endswith("/compatibility_ping_mitigation/config")
    )
    assert deploy["command_topic"].endswith("/compatibility/cmd/deploy")
    assert mitigation["command_topic"].endswith(
        "/compatibility/cmd/ping_mitigation"
    )
    assert mitigation["state_topic"].endswith(
        "/compatibility/ping_mitigation"
    )


def test_command_router_keeps_deploy_and_activation_separate():
    calls = []
    router = CommandRouter.__new__(CommandRouter)
    compatibility_type = type(
        "SimpleCompatibility",
        (),
        {
            "request_deploy": lambda _self: calls.append("deploy"),
            "request_enabled": lambda _self, enabled: calls.append(enabled),
        },
    )
    router.compatibility = compatibility_type()
    ad = FakeAd()
    router.logger = PetlibroLogger(ad, "test", "trace")

    router._compatibility_deploy()
    router._compatibility_enabled("MQTT_MESSAGE", {"payload": "ON"}, {})
    router._compatibility_enabled("MQTT_MESSAGE", {"payload": "false"}, {})
    router._compatibility_enabled("MQTT_MESSAGE", {"payload": "invalid"}, {})

    assert calls == ["deploy", True, False]
    assert "invalid ping mitigation command ignored" in "\n".join(ad.logs)
