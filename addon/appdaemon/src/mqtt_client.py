"""MQTT transport and command dispatch for the PLAF203 wire protocol."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from petlibro_logging import PetlibroLogger
from protocol import (
    AttrGetServiceIn,
    AttrPushEventIn,
    AttrSetServiceIn,
    Commands,
    DeviceConfigSyncIn,
    DeviceRebootIn,
    DeviceStartEventIn,
    FeedingPlanServiceIn,
    GetConfigIn,
    GetFeedingPlanEventIn,
    GrainOutputEventIn,
    HeartbeatIn,
    InitializeSdCardServiceIn,
    ManualFeedingServiceIn,
    MessageTopics,
    NtpIn,
    NtpSyncIn,
    RestoreIn,
    WifiReconnectServiceIn,
)


@dataclass(frozen=True)
class InboundMessage:
    channel: str
    parser: Callable[[dict], object]


INBOUND_MESSAGES = {
    Commands.HEARTBEAT: InboundMessage("heart", HeartbeatIn.from_mqtt_payload),
    Commands.NTP: InboundMessage("ntp", NtpIn.from_mqtt_payload),
    Commands.NTP_SYNC: InboundMessage("ntp", NtpSyncIn.from_mqtt_payload),
    Commands.ATTR_SET_SERVICE: InboundMessage(
        "service", AttrSetServiceIn.from_mqtt_payload
    ),
    Commands.DEVICE_CONFIG_SYNC: InboundMessage(
        "service", DeviceConfigSyncIn.from_mqtt_payload
    ),
    Commands.DEVICE_REBOOT: InboundMessage("service", DeviceRebootIn.from_mqtt_payload),
    Commands.FEEDING_PLAN_SERVICE: InboundMessage(
        "service", FeedingPlanServiceIn.from_mqtt_payload
    ),
    Commands.INITIALIZE_SD_CARD_SERVICE: InboundMessage(
        "service", InitializeSdCardServiceIn.from_mqtt_payload
    ),
    Commands.MANUAL_FEEDING_SERVICE: InboundMessage(
        "service", ManualFeedingServiceIn.from_mqtt_payload
    ),
    Commands.WIFI_RECONNECT_SERVICE: InboundMessage(
        "service", WifiReconnectServiceIn.from_mqtt_payload
    ),
    Commands.ATTR_GET_SERVICE: InboundMessage(
        "event", AttrGetServiceIn.from_mqtt_payload
    ),
    Commands.ATTR_PUSH_EVENT: InboundMessage(
        "event", AttrPushEventIn.from_mqtt_payload
    ),
    Commands.DEVICE_START_EVENT: InboundMessage(
        "event", DeviceStartEventIn.from_mqtt_payload
    ),
    Commands.GET_FEEDING_PLAN_EVENT: InboundMessage(
        "event", GetFeedingPlanEventIn.from_mqtt_payload
    ),
    Commands.GRAIN_OUTPUT_EVENT: InboundMessage(
        "event", GrainOutputEventIn.from_mqtt_payload
    ),
    Commands.GET_CONFIG: InboundMessage("config", GetConfigIn.from_mqtt_payload),
    Commands.RESTORE: InboundMessage("system", RestoreIn.from_mqtt_payload),
}


class Client:
    """Route decoded feeder messages and serialize plaintext MQTT JSON bodies."""

    def __init__(
        self,
        ad: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        device_serial_number: str,
        log_level: str = "info",
    ):
        self.mqtt = mqtt
        self.message_topics = MessageTopics(device_serial_number)
        self.logger = PetlibroLogger(ad, "petlibro.mqtt", log_level)
        self._callbacks: dict[str, Callable[[object], None]] = {}

    def initialize(self) -> None:
        listeners = {
            "heart": self._mqtt_recv_heart_cb,
            "ntp": self._mqtt_recv_ntp_cb,
            "config": self._mqtt_recv_config_cb,
            "event": self._mqtt_recv_event_cb,
            "service": self._mqtt_recv_service_cb,
            "system": self._mqtt_recv_system_cb,
        }
        for channel, callback in listeners.items():
            self.mqtt.listen_event(
                callback,
                "MQTT_MESSAGE",
                topic=self.message_topics.post(channel),
                namespace="mqtt",
            )

    def _listen(self, command: str, callback) -> None:
        self._callbacks[command] = callback

    def heartbeat_listen(self, callback):
        self._listen(Commands.HEARTBEAT, callback)

    def ntp_listen(self, callback):
        self._listen(Commands.NTP, callback)

    def ntp_sync_listen(self, callback):
        self._listen(Commands.NTP_SYNC, callback)

    def attr_get_service_listen(self, callback):
        self._listen(Commands.ATTR_GET_SERVICE, callback)

    def attr_push_event_listen(self, callback):
        self._listen(Commands.ATTR_PUSH_EVENT, callback)

    def attr_set_service_listen(self, callback):
        self._listen(Commands.ATTR_SET_SERVICE, callback)

    def device_config_sync_listen(self, callback):
        self._listen(Commands.DEVICE_CONFIG_SYNC, callback)

    def device_reboot_listen(self, callback):
        self._listen(Commands.DEVICE_REBOOT, callback)

    def device_start_event_listen(self, callback):
        self._listen(Commands.DEVICE_START_EVENT, callback)

    def feeding_plan_service_listen(self, callback):
        self._listen(Commands.FEEDING_PLAN_SERVICE, callback)

    def get_config_listen(self, callback):
        self._listen(Commands.GET_CONFIG, callback)

    def get_feeding_plan_event_listen(self, callback):
        self._listen(Commands.GET_FEEDING_PLAN_EVENT, callback)

    def grain_output_event_listen(self, callback):
        self._listen(Commands.GRAIN_OUTPUT_EVENT, callback)

    def initialize_sd_card_service_listen(self, callback):
        self._listen(Commands.INITIALIZE_SD_CARD_SERVICE, callback)

    def manual_feeding_service_listen(self, callback):
        self._listen(Commands.MANUAL_FEEDING_SERVICE, callback)

    def restore_listen(self, callback):
        self._listen(Commands.RESTORE, callback)

    def wifi_reconnect_service_listen(self, callback):
        self._listen(Commands.WIFI_RECONNECT_SERVICE, callback)

    def ntp_send(self, message):
        self._send("ntp", message)

    def ntp_sync_send(self, message):
        self._send("ntp", message)

    def attr_get_service_send(self, message):
        self._send("event", message)

    def attr_push_event_send(self, message):
        self._send("event", message)

    def attr_set_service_send(self, message):
        self._send("service", message)

    def device_config_sync_send(self, message):
        self._send("service", message)

    def device_reboot_send(self, message):
        self._send("service", message)

    def device_start_event_send(self, message):
        self._send("event", message)

    def feeding_plan_service_send(self, message):
        self._send("service", message)

    def get_config_send(self, message):
        self._send("config", message)

    def get_feeding_plan_event_send(self, message):
        self._send("event", message)

    def grain_output_event_send(self, message):
        self._send("event", message)

    def initialize_sd_card_service_send(self, message):
        self._send("service", message)

    def manual_feeding_service_send(self, message):
        self._send("service", message)

    def restore_send(self, message):
        self._send("system", message)

    def wifi_reconnect_service_send(self, message):
        self._send("service", message)

    def _mqtt_recv_heart_cb(self, _eventname, data, _kwargs):
        self._receive("heart", data)

    def _mqtt_recv_ntp_cb(self, _eventname, data, _kwargs):
        self._receive("ntp", data)

    def _mqtt_recv_config_cb(self, _eventname, data, _kwargs):
        self._receive("config", data)

    def _mqtt_recv_event_cb(self, _eventname, data, _kwargs):
        self._receive("event", data)

    def _mqtt_recv_service_cb(self, _eventname, data, _kwargs):
        self._receive("service", data)

    def _mqtt_recv_system_cb(self, _eventname, data, _kwargs):
        self._receive("system", data)

    def _receive(self, channel: str, data: dict) -> None:
        try:
            payload = json.loads(data["payload"])
            command = payload["cmd"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            self.logger.warning(
                "invalid MQTT message ignored",
                topic=data.get("topic"),
                channel=channel,
                exception_type=type(error).__name__,
            )
            return

        if command == Commands.DEVICE_LOG_REPORT_EVENT:
            self.logger.trace("ignored device log report")
            return
        self._trace_mqtt("rx", data, payload)

        spec = INBOUND_MESSAGES.get(command)
        callback = self._callbacks.get(command)
        if spec is None or spec.channel != channel or callback is None:
            self.logger.warning(
                "unknown or unhandled MQTT command",
                channel=channel,
                command=command,
            )
            return
        context = {
            "topic": data.get("topic"),
            "cmd": command,
            "msg_id": payload.get("msgId"),
        }
        try:
            message = spec.parser(payload)
        except Exception as error:
            self.logger.warning(
                "invalid MQTT command payload ignored",
                **context,
                exception_type=type(error).__name__,
            )
            return
        try:
            callback(message)
        except Exception as error:
            # Never let AppDaemon format the original callback arguments: they
            # can contain cameraAuthInfo and other sensitive device material.
            self.logger.error(
                "MQTT command handler failed",
                **context,
                exception_type=type(error).__name__,
            )

    def _send(self, channel: str, message) -> None:
        payload = message.to_mqtt_payload()
        topic = self.message_topics.sub(channel)
        encoded = json.dumps(payload)
        self.logger.trace(
            "MQTT message metadata",
            direction="tx",
            topic=topic,
            command=payload.get("cmd"),
            payload_bytes=len(encoded.encode("utf-8")),
            payload_fields=sorted(str(key) for key in payload),
        )
        self.mqtt.mqtt_publish(topic, encoded, namespace="mqtt")

    def _trace_mqtt(self, direction: str, data: dict, payload: dict) -> None:
        raw_payload = data.get("payload")
        payload_bytes = (
            len(raw_payload.encode("utf-8"))
            if isinstance(raw_payload, str)
            else None
        )
        self.logger.trace(
            "MQTT message metadata",
            direction=direction,
            topic=data.get("topic"),
            command=payload.get("cmd"),
            payload_bytes=payload_bytes,
            payload_fields=sorted(str(key) for key in payload),
        )
