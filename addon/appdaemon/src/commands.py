"""Home Assistant MQTT command parsing and coordinator routing."""

from __future__ import annotations

import json

from appdaemon.plugins.mqtt import mqttapi
from backend import Backend
from feed_plans import PlanSlotMismatch, parse_plan_patch
from ha_entities import HomeAssistantStatePublisher
from settings_map import SETTING_COMMANDS, SettingCommandSpec
from state_agent_updates import StateAgentUpdateCoordinator
from state_coordinator import (
    FeederStateCoordinator,
    PersistentWriteRequest,
    SettingEqualsPredicate,
)
from storage import Storage


class CommandRouter:
    """Translate user-originated HA commands into actions or verified writes."""

    def __init__(
        self,
        mqtt: mqttapi.Mqtt,
        serial_number: str,
        backend: Backend,
        coordinator: FeederStateCoordinator,
        storage: Storage,
        state: HomeAssistantStatePublisher,
        logger,
        updater: StateAgentUpdateCoordinator | None = None,
        raw_settings_diagnostics: bool = False,
    ):
        self.mqtt = mqtt
        self.serial_number = serial_number
        self.backend = backend
        self.coordinator = coordinator
        self.storage = storage
        self.state = state
        self.logger = logger
        self.updater = updater
        self.raw_settings_diagnostics = raw_settings_diagnostics

    def start(self) -> None:
        for spec in SETTING_COMMANDS:
            self._subscribe(spec.topic, self._setting_handler(spec))
        self._subscribe("audio/cmd/file_url", self._unsupported("audio.file_url"))
        for slot in range(1, 10):
            self._subscribe(f"food/cmd/plan_{slot}", self.plan_handler(slot))
        self._subscribe("food/cmd/manual_feed_grain_num", self._manual_feed_amount)
        self._subscribe("food/cmd/manual_feed", self._manual_feed)
        self._subscribe("device/cmd/reboot", lambda *_: self.backend.device_reboot())
        self._subscribe(
            "device/cmd/factory_reset", lambda *_: self.backend.device_factory_reset()
        )
        self._subscribe(
            "device/cmd/wifi_reconnect", lambda *_: self.backend.device_wifi_reconnect()
        )
        self._subscribe(
            "device/cmd/sd_card_format", lambda *_: self.backend.device_sd_card_format()
        )
        self._subscribe("state_agent/cmd/install", self._state_agent_install)
        self._subscribe("state_agent/cmd/check_updates", self._state_agent_check)

    def restore_local_preferences(self) -> None:
        self.state.publish(
            "food/manual_feed_grain_num",
            self.storage.food_manual_feed_grain_num_get(),
        )

    def plan_handler(self, plan_slot: int):
        def callback(_eventname: str, data: dict, _kwargs):
            raw_payload = data.get("payload")
            deleting = raw_payload == ""
            patch = None
            if not deleting:
                try:
                    patch = parse_plan_patch(raw_payload, plan_slot)
                except json.JSONDecodeError as error:
                    self.logger.warning(
                        "invalid feeding-plan JSON ignored",
                        reason=error.msg,
                        line=error.lineno,
                        column=error.colno,
                        position=error.pos,
                        payload_length=(
                            len(raw_payload) if isinstance(raw_payload, str) else None
                        ),
                    )
                    return
                except PlanSlotMismatch as error:
                    self.logger.warning(
                        "feeding-plan slot/id mismatch ignored",
                        slot=error.slot,
                        plan_id=error.plan_id,
                    )
                    return
                except (KeyError, TypeError, ValueError) as error:
                    self.logger.warning(
                        "invalid feeding-plan command ignored",
                        error_type=type(error).__name__,
                    )
                    return

            accepted = self.coordinator.request_persistent_write(
                PersistentWriteRequest(
                    control=f"food.plan_{plan_slot}",
                    target=plan_slot if deleting else patch,
                    publisher=lambda truth: self.backend.feeding_plans_send(
                        truth.plans.semantic_records
                    ),
                    predicate=None,
                    command_summary="FEEDING_PLAN_SERVICE full collection",
                    requires_fresh_preflight=True,
                    plan_patch=patch,
                    plan_delete_id=plan_slot if deleting else None,
                )
            )
            if accepted:
                self.logger.debug(
                    "feeding plan command queued for fresh feeder preflight",
                    slot=plan_slot,
                    operation="delete" if deleting else "upsert",
                )

        callback.__name__ = f"_mqtt_cmd_food_plan_{plan_slot}_cb"
        callback.plan_slot = plan_slot
        return callback

    def _setting_handler(self, spec: SettingCommandSpec):
        def callback(_eventname: str, data: dict, _kwargs):
            try:
                target = spec.parser(data.get("payload"))
                expected = spec.expected(target)
            except (KeyError, TypeError, ValueError) as error:
                self.logger.warning(
                    "invalid setting command ignored",
                    control=spec.control,
                    error_type=type(error).__name__,
                )
                return
            raw_settings_diagnostics = (
                getattr(self, "raw_settings_diagnostics", False)
                and spec.control == "sound.enable"
            )
            self.coordinator.request_persistent_write(
                PersistentWriteRequest(
                    control=spec.control,
                    target=expected,
                    publisher=lambda truth: (
                        spec.publisher_with_truth(self.backend, target, truth)
                        if spec.publisher_with_truth is not None
                        else spec.publisher(self.backend, target)
                    ),
                    predicate=SettingEqualsPredicate(spec.state_field, expected),
                    command_summary=spec.control,
                    requires_fresh_preflight=(
                        raw_settings_diagnostics or spec.requires_fresh_preflight
                    ),
                    raw_settings_diagnostics=raw_settings_diagnostics,
                )
            )

        callback.__name__ = (
            "_mqtt_cmd_" + spec.topic.replace("/cmd/", "_").replace("/", "_") + "_cb"
        )
        return callback

    def _unsupported(self, control: str):
        def callback(*_args):
            self.logger.warning(
                "persistent feeder write blocked",
                control=control,
                reason="field is not exposed by the feeder state API",
            )

        callback.__name__ = "_mqtt_cmd_unsupported_cb"
        return callback

    def _manual_feed_amount(self, _eventname: str, data: dict, _kwargs):
        try:
            amount = int(data.get("payload"))
        except (TypeError, ValueError):
            self.logger.warning("invalid manual-feed amount ignored")
            return
        self.storage.food_manual_feed_grain_num_set(amount)
        self.state.publish("food/manual_feed_grain_num", amount)

    def _manual_feed(self, *_args):
        self.backend.food_manual_feed_now(self.storage.food_manual_feed_grain_num_get())

    def _state_agent_install(self, *_args):
        if self.updater is None:
            self.logger.warning(
                "state-agent install ignored because updater is unavailable"
            )
            return
        self.updater.request_install()

    def _state_agent_check(self, *_args):
        if self.updater is None:
            self.logger.warning(
                "state-agent update check ignored because updater is unavailable"
            )
            return
        self.updater.request_check(force=True, reason="manual")

    def _subscribe(self, relative_topic: str, callback) -> None:
        topic = self.state.topic(relative_topic)

        def user_intent_only(eventname: str, data: dict, kwargs):
            if self.coordinator.suppressing_writeback():
                self.logger.debug(
                    "HA command ignored while applying feeder truth", topic=topic
                )
                return
            retained = data.get("retain")
            if retained is True or str(retained).lower() == "true":
                self.logger.debug("retained MQTT command ignored", topic=topic)
                return
            return callback(eventname, data, kwargs)

        user_intent_only.__name__ = callback.__name__
        self.mqtt.listen_event(
            user_intent_only,
            "MQTT_MESSAGE",
            topic=topic,
            namespace="mqtt",
        )
