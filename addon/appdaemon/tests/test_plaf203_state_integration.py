import json
from dataclasses import replace

import backend as backend_module
import commands as command_module
from ha_entities import HomeAssistantStatePublisher
from petlibro_logging import PetlibroLogger
from protocol import Code, GetFeedingPlanEventIn, MessageId, Timestamp
from state_agent import FeederTruth
from state_coordinator import CommandReceipt, build_plan_id_normalization
from settings_map import SETTING_COMMANDS
from telemetry import TelemetryPublisher
from test_state_agent import core_payload


class TrapStorage:
    def __getattr__(self, name):
        raise AssertionError(f"feeding-plan command consulted storage: {name}")


class CapturingCoordinator:
    def __init__(self):
        self.requests = []

    def request_persistent_write(self, request):
        self.requests.append(request)
        return True

    def suppressing_writeback(self):
        return False


class Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


class CapturingClient:
    def __init__(self):
        self.attribute_messages = []
        self.plan_messages = []
        self.plan_responses = []

    def attr_set_service_send(self, message):
        self.attribute_messages.append(message.to_mqtt_payload())

    def feeding_plan_service_send(self, message):
        self.plan_messages.append(message.to_mqtt_payload())

    def get_feeding_plan_event_send(self, message):
        self.plan_responses.append(message.to_mqtt_payload())


class CapturingMqtt:
    def __init__(self):
        self.callback = None
        self.published = []

    def listen_event(self, callback, *_args, **_kwargs):
        self.callback = callback

    def mqtt_publish(self, topic, payload, **kwargs):
        self.published.append((topic, payload, kwargs))


class CapturingState:
    def __init__(self):
        self.values = []

    def publish(self, topic, value, **kwargs):
        self.values.append((topic, value, kwargs))

    def topic(self, relative_topic):
        return f"plaf203/SERIAL/{relative_topic}"


class ManualFeedStorage:
    def __init__(self, amount=1):
        self.amount = amount

    def food_manual_feed_grain_num_get(self):
        return self.amount

    def food_manual_feed_grain_num_set(self, amount):
        self.amount = amount


class ManualFeedBackend:
    def __init__(self):
        self.amounts = []

    def food_manual_feed_now(self, amount):
        self.amounts.append(amount)


def test_plan_command_never_consults_storage_or_retained_state():
    router = object.__new__(command_module.CommandRouter)
    router.storage = TrapStorage()
    router.coordinator = CapturingCoordinator()
    router.logger = Logger()
    router.backend = object()
    payload = {
        "id": 1,
        "execution_time": {"hour": 7, "minute": 0},
        "scheduled_days": ["MONDAY", "WEDNESDAY", "FRIDAY"],
        "grain_num": 12,
        # Legacy retained values are intentionally ignored; they are not truth.
        "enable_audio": False,
        "play_audio_times": 99,
    }

    router.plan_handler(1)(
        "MQTT_MESSAGE",
        {"payload": json.dumps(payload), "retain": False},
        {},
    )

    assert len(router.coordinator.requests) == 1
    request = router.coordinator.requests[0]
    assert request.requires_fresh_preflight
    assert request.plan_patch.plan_id == 1
    assert request.plan_patch.days_raw == (1, 3, 5)
    assert request.plan_patch.portions == 12


def test_persistent_setting_command_map_is_unambiguous():
    topics = [spec.topic for spec in SETTING_COMMANDS]
    controls = [spec.control for spec in SETTING_COMMANDS]

    assert len(topics) == len(set(topics))
    assert len(controls) == len(set(controls))
    assert all(spec.state_field for spec in SETTING_COMMANDS)


def test_switch_commands_verify_binary_backed_persistent_fields():
    by_control = {spec.control: spec.state_field for spec in SETTING_COMMANDS}

    assert by_control["sound.enable"] == "sound_switch"
    assert by_control["button_lights.enable"] == "light_switch"
    assert by_control["camera.enable"] == "camera_switch"
    assert by_control["recording.enable"] == "video_record_switch"
    assert by_control["motion_detection.enable"] == "motion_detection_switch"
    assert by_control["sound_detection.enable"] == "sound_detection_switch"
    assert by_control["audio.enable"] == "feeding_audio_enabled"
    assert "sound_effective_cached" not in by_control.values()


def test_sound_setting_enables_raw_preflight_diagnostics_only_when_configured():
    router = object.__new__(command_module.CommandRouter)
    router.coordinator = CapturingCoordinator()
    router.logger = Logger()
    router.backend = object()
    router.raw_settings_diagnostics = True
    sound_spec = next(
        spec for spec in SETTING_COMMANDS if spec.control == "sound.enable"
    )

    router._setting_handler(sound_spec)(
        "MQTT_MESSAGE",
        {"payload": "true", "retain": False},
        {},
    )

    request = router.coordinator.requests[0]
    assert request.requires_fresh_preflight
    assert request.raw_settings_diagnostics


def test_meal_call_enable_uses_fresh_persistent_audio_url():
    router = object.__new__(command_module.CommandRouter)
    router.coordinator = CapturingCoordinator()
    router.logger = Logger()
    router.backend = backend_module.Backend()
    router.backend.client = CapturingClient()
    router.raw_settings_diagnostics = False
    audio_spec = next(
        spec for spec in SETTING_COMMANDS if spec.control == "audio.enable"
    )

    router._setting_handler(audio_spec)(
        "MQTT_MESSAGE",
        {"payload": "true", "retain": False},
        {},
    )

    request = router.coordinator.requests[0]
    assert request.requires_fresh_preflight
    request.publisher(FeederTruth.from_dict(core_payload()))
    assert len(router.backend.client.attribute_messages) == 1
    payload = router.backend.client.attribute_messages[0]
    assert payload["cmd"] == "ATTR_SET_SERVICE"
    assert payload["enableAudio"] is True
    assert payload["audioUrl"] == "https://example.invalid/meal-call.aac"


def test_retained_plan_command_is_not_treated_as_user_intent():
    router = object.__new__(command_module.CommandRouter)
    router.logger = Logger()
    router.mqtt = CapturingMqtt()
    router.coordinator = CapturingCoordinator()
    router.state = type("State", (), {"topic": lambda _self, value: value})()
    calls = []
    router._subscribe(
        "food/cmd/plan_1",
        lambda *_args: calls.append(True),
    )

    router.mqtt.callback(
        "MQTT_MESSAGE",
        {"payload": "{}", "retain": True},
        {},
    )

    assert calls == []


def test_backend_has_no_authoritative_plan_cache_api():
    assert not hasattr(backend_module.Backend, "food_plans_set")


def test_mqtt_plan_adapter_serializes_full_collection_protocol_metadata():
    backend = backend_module.Backend()
    backend.client = CapturingClient()
    truth = FeederTruth.from_dict(core_payload())

    receipt = backend.feeding_plans_send(truth.plans.semantic_records)

    assert isinstance(receipt, CommandReceipt)
    message = backend.client.plan_messages[0]
    assert len(message["plans"]) == truth.plans.count
    assert message["plans"][0]["enableAudio"] is True
    assert message["plans"][0]["audioTimes"] == 2
    assert message["plans"][0]["grainNum"] == 10
    assert message["plans"][0]["syncTime"] == 1_700_000_000_000
    assert "skipEndTime" not in message["plans"][0]

    skipped_plan = replace(
        truth.plans.semantic_records[0], skip_end_time=1_700_000_100_000
    )
    backend.feeding_plans_send((skipped_plan,))
    assert (
        backend.client.plan_messages[1]["plans"][0]["skipEndTime"]
        == 1_700_000_100_000
    )

    disabled_truth = FeederTruth.from_dict(core_payload(enable_audio_raw=0))
    backend.feeding_plans_send(disabled_truth.plans.semantic_records)
    assert backend.client.plan_messages[2]["plans"][0]["enableAudio"] is False


def test_mqtt_plan_adapter_preserves_known_fields_during_id_normalization():
    backend = backend_module.Backend()
    backend.client = CapturingClient()
    truth = FeederTruth.from_dict(core_payload())
    source = replace(
        truth.plans.semantic_records[0],
        id=1_648_218,
        opaque_hex="00" * 10,
        skip_end_time=1_700_000_100_000,
    )
    normalization = build_plan_id_normalization((source,))

    backend.feeding_plans_send(normalization.normalized)

    message = backend.client.plan_messages[0]["plans"][0]
    assert message["planId"] == 1
    assert message["executionTime"] == source.time_utc
    assert message["repeatDay"] == [1, 2, 3, 4, 5, 6, 7]
    assert message["grainNum"] == source.portions
    assert message["enableAudio"] is bool(source.enable_audio_raw)
    assert message["audioTimes"] == source.audio_times
    assert message["skipEndTime"] == source.skip_end_time
    assert message["syncTime"] > source.sync_time


def test_mqtt_plan_adapter_rejects_unknown_enable_audio_raw_value():
    backend = backend_module.Backend()
    backend.client = CapturingClient()
    invalid_truth = FeederTruth.from_dict(core_payload(enable_audio_raw=2))

    try:
        backend.feeding_plans_send(invalid_truth.plans.semantic_records)
    except ValueError as error:
        assert "enable_audio_raw" in str(error)
    else:
        raise AssertionError("unsupported enable_audio_raw reached MQTT")

    assert backend.client.plan_messages == []


def test_get_plan_event_unavailable_sends_no_destructive_empty_plan_response():
    backend = backend_module.Backend()
    backend.client = CapturingClient()
    backend.logger = Logger()
    request = GetFeedingPlanEventIn(
        message_id=MessageId("request-id"),
        timestamp=Timestamp.now(),
    )

    assert backend.feeding_plan_request_respond(request, None) is False
    assert backend.client.plan_responses == []


def test_get_plan_event_authoritative_empty_truth_can_clear_plans():
    backend = backend_module.Backend()
    backend.client = CapturingClient()
    backend.logger = Logger()
    request = GetFeedingPlanEventIn(
        message_id=MessageId("request-id"),
        timestamp=Timestamp.now(),
    )

    assert backend.feeding_plan_request_respond(request, ()) is True

    response = backend.client.plan_responses[0]
    assert response["code"] == Code.OK.value
    assert response["plans"] == []


def test_feeder_truth_projection_updates_ha_mirror_without_commands():
    mqtt = CapturingMqtt()
    state = HomeAssistantStatePublisher(mqtt, "SERIAL", Logger())

    state.apply_feeder_truth(FeederTruth.from_dict(core_payload()))

    published = {topic: payload for topic, payload, _kwargs in mqtt.published}
    assert (
        published["plaf203/SERIAL/audio/file_url"]
        == "https://example.invalid/meal-call.aac"
    )
    assert published["plaf203/SERIAL/sound/volume"] == 76
    assert published["plaf203/SERIAL/camera/resolution"] == "P1080"
    assert json.loads(published["plaf203/SERIAL/food/plan_1"])["grain_num"] == 10
    assert published["plaf203/SERIAL/food/plan_2"] == "unknown"
    assert len([topic for topic in published if "/food/plan_" in topic]) == 9
    assert not any("/cmd/" in topic for topic in published)


def test_telemetry_publisher_keeps_operational_state_outside_truth_model():
    state = CapturingState()
    telemetry = TelemetryPublisher(state, Logger())

    telemetry.device_info(product_id="PLAF203", software_version="3.1.48")
    telemetry.food(motor_state=2, outlet_blocked=False, low_fill_level=True)

    assert ("device/product_id", "PLAF203", {}) in state.values
    assert ("device/software_version", "3.1.48", {}) in state.values
    assert ("food/motor_state", 2, {}) in state.values
    assert ("food/outlet_blocked", False, {}) in state.values
    assert ("food/low_fill_level", True, {}) in state.values


def test_manual_feed_remains_an_action_not_a_persistent_write():
    router = object.__new__(command_module.CommandRouter)
    router.backend = ManualFeedBackend()
    router.storage = ManualFeedStorage(amount=7)
    router.coordinator = CapturingCoordinator()

    router._manual_feed("MQTT_MESSAGE", {"payload": "PRESS"}, {})

    assert router.backend.amounts == [7]
    assert router.coordinator.requests == []


def test_logger_redacts_state_agent_tokens_from_structured_fields():
    class LogSink:
        def __init__(self):
            self.messages = []

        def log(self, message, **_kwargs):
            self.messages.append(message)

    sink = LogSink()
    logger = PetlibroLogger(sink, "petlibro.test", "trace")

    logger.error(
        "state agent request failed",
        petlibro_state_agent_token="never-log-this-token",
    )

    assert "never-log-this-token" not in sink.messages[0]
    assert "<redacted>" in sink.messages[0]
