"""Home Assistant MQTT discovery definitions for PLAF203 entities."""

from __future__ import annotations

import datetime
import enum
import json

from appdaemon.plugins.mqtt import mqttapi
from dispensing_status import DispensingStatusProjector, FoodOutputProgress
from feed_plans import plan_state_payload
from protocol import (
    AgingType,
    BowlMode,
    GrainOutputType,
    MotionDetectionRange,
    MotionDetectionSensitivity,
    NightVision,
    PowerMode,
    PowerType,
    Resolution,
    SdCardState,
    SoundDetectionSensitivity,
    VideoRecordMode,
)
from settings_map import TRUTH_PROJECTIONS
from state_agent import FeederTruth


class HomeAssistantDiscoveryMqtt:
    def __init__(self, mqtt: mqttapi.Mqtt, serial_number: str):
        self.mqtt: mqttapi.Mqtt = mqtt
        self.serial_number: str = serial_number

    def discovery_issue(self):
        schedule_labels = {
            AgingType.NON_SCHEDULED_ENABLED.name: "Always active",
            AgingType.SCHEDULED_ENABLED.name: "Scheduled",
        }
        sensitivity_labels = {
            MotionDetectionSensitivity.LOW.name: "Low",
            MotionDetectionSensitivity.MEDIUM.name: "Medium",
            MotionDetectionSensitivity.HIGH.name: "High",
        }

        self._ha_switch_config_publish(
            "Meal call", "mdi:account-voice", "audio", "enable", "config"
        )
        self._ha_text_config_publish(
            "Meal call audio URL", "mdi:account-voice", "audio", "file_url", "config"
        )

        self._ha_switch_config_publish(
            "Camera", "mdi:cctv", "camera", "enable", "config"
        )
        self._ha_binary_sensor_config_publish(
            "Camera reported state", "mdi:cctv", "camera", "enable", "diagnostic"
        )
        self._ha_select_config_publish(
            "Camera schedule mode",
            "mdi:cctv",
            "camera",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )
        self._ha_select_config_publish(
            "IR night vision",
            "mdi:weather-night",
            "camera",
            "night_vision",
            [NightVision.AUTOMATIC.name, NightVision.OPEN.name, NightVision.CLOSE.name],
            "config",
            {
                NightVision.AUTOMATIC.name: "Automatic",
                NightVision.OPEN.name: "On",
                NightVision.CLOSE.name: "Off",
            },
        )
        self._ha_select_config_publish(
            "Feeder camera resolution",
            "mdi:cctv",
            "camera",
            "resolution",
            [Resolution.P720.name, Resolution.P1080.name],
            "config",
            {
                Resolution.P720.name: "720p",
                Resolution.P1080.name: "1080p",
            },
        )
        self._ha_switch_config_publish(
            "Local video recording", "mdi:camera", "recording", "enable", "config"
        )
        self._ha_binary_sensor_config_publish(
            "Local recording available",
            "mdi:camera",
            "recording",
            "feature_enabled",
            "diagnostic",
        )
        self._ha_select_config_publish(
            "Local recording schedule mode",
            "mdi:camera",
            "recording",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )
        self._ha_select_config_publish(
            "Local recording mode",
            "mdi:camera",
            "recording",
            "mode",
            [VideoRecordMode.CONTINUOUS.name, VideoRecordMode.MOTION_DETECTION.name],
            "config",
            {
                VideoRecordMode.CONTINUOUS.name: "Continuous",
                VideoRecordMode.MOTION_DETECTION.name: "Motion-triggered",
            },
        )
        self._ha_sensor_config_publish(
            "SD card status",
            "mdi:micro-sd",
            "sd_card",
            "state",
            value_labels={
                SdCardState.NOT_AVAILABLE.name: "Not available",
                SdCardState.AVAILABLE.name: "Ready",
                SdCardState.INITIALIZING.name: "Initializing",
            },
        )
        self._ha_sensor_config_publish(
            "SD card file system", "mdi:micro-sd", "sd_card", "file_system"
        )
        self._ha_sensor_config_publish(
            "SD card total capacity",
            "mdi:micro-sd",
            "sd_card",
            "total_capacity",
            unit_of_measurement="MB",
        )
        self._ha_sensor_config_publish(
            "SD card used capacity",
            "mdi:micro-sd",
            "sd_card",
            "used_capacity",
            unit_of_measurement="MB",
        )

        self._ha_switch_config_publish(
            "Meal video recording", "mdi:movie", "feeding_video", "enable", "config"
        )
        self._ha_switch_config_publish(
            "Record scheduled meals",
            "mdi:movie",
            "feeding_video",
            "on_feeding_plan_trigger_enable",
            "config",
        )
        self._ha_switch_config_publish(
            "Record manual feeds",
            "mdi:movie",
            "feeding_video",
            "on_manual_feeding_trigger_enable",
            "config",
        )
        self._ha_number_box_config_publish(
            "Scheduled meal pre-roll",
            "mdi:movie",
            "feeding_video",
            "time_before_feeding_plan_trigger",
            0,
            60,
            "config",
        )
        self._ha_number_box_config_publish(
            "Manual feed post-roll",
            "mdi:movie",
            "feeding_video",
            "time_after_manual_feeding_trigger",
            0,
            60,
            "config",
        )
        self._ha_number_box_config_publish(
            "Automatic recording duration",
            "mdi:movie",
            "feeding_video",
            "time_automatic_recording",
            0,
            60,
            "config",
        )
        self._ha_switch_config_publish(
            "Video watermark", "mdi:movie", "feeding_video", "watermark", "config"
        )

        self._ha_switch_config_publish(
            "Cloud video recording",
            "mdi:cloud",
            "cloud_video_recording",
            "enable",
            "config",
        )

        self._ha_switch_config_publish(
            "Motion detection",
            "mdi:motion-sensor",
            "motion_detection",
            "enable",
            "config",
        )
        self._ha_binary_sensor_config_publish(
            "Motion detection available",
            "mdi:motion-sensor",
            "motion_detection",
            "feature_enabled",
            "diagnostic",
        )
        self._ha_select_config_publish(
            "Motion detection schedule mode",
            "mdi:motion-sensor",
            "motion_detection",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )
        self._ha_select_config_publish(
            "Motion detection range",
            "mdi:motion-sensor",
            "motion_detection",
            "range",
            [
                MotionDetectionRange.SMALL.name,
                MotionDetectionRange.MEDIUM.name,
                MotionDetectionRange.LARGE.name,
            ],
            "config",
            {
                MotionDetectionRange.SMALL.name: "Small",
                MotionDetectionRange.MEDIUM.name: "Medium",
                MotionDetectionRange.LARGE.name: "Large",
            },
        )
        self._ha_select_config_publish(
            "Motion detection sensitivity",
            "mdi:motion-sensor",
            "motion_detection",
            "sensitivity",
            [
                MotionDetectionSensitivity.LOW.name,
                MotionDetectionSensitivity.MEDIUM.name,
                MotionDetectionSensitivity.HIGH.name,
            ],
            "config",
            sensitivity_labels,
        )
        self._ha_switch_config_publish(
            "Sound detection", "mdi:bullhorn", "sound_detection", "enable", "config"
        )
        self._ha_binary_sensor_config_publish(
            "Sound detection available",
            "mdi:bullhorn",
            "sound_detection",
            "feature_enabled",
            "diagnostic",
        )
        self._ha_select_config_publish(
            "Sound detection schedule mode",
            "mdi:bullhorn",
            "sound_detection",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )
        self._ha_select_config_publish(
            "Sound detection sensitivity",
            "mdi:bullhorn",
            "sound_detection",
            "sensitivity",
            [
                SoundDetectionSensitivity.LOW.name,
                SoundDetectionSensitivity.MEDIUM.name,
                SoundDetectionSensitivity.HIGH.name,
            ],
            "config",
            sensitivity_labels,
        )
        self._ha_switch_config_publish(
            "Device sound", "mdi:speaker", "sound", "enable", "config"
        )
        self._ha_binary_sensor_config_publish(
            "Device sound available",
            "mdi:speaker",
            "sound",
            "feature_enabled",
            "diagnostic",
        )
        self._ha_select_config_publish(
            "Device sound schedule mode",
            "mdi:speaker",
            "sound",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )
        self._ha_number_slider_config_publish(
            "Device sound volume", "mdi:speaker", "sound", "volume", 0, 100, "config"
        )
        self._ha_switch_config_publish(
            "Button lights", "mdi:lightbulb", "button_lights", "enable", "config"
        )
        self._ha_select_config_publish(
            "Button lights schedule mode",
            "mdi:lightbulb",
            "button_lights",
            "aging_type",
            [AgingType.NON_SCHEDULED_ENABLED.name, AgingType.SCHEDULED_ENABLED.name],
            "config",
            schedule_labels,
        )

        self._ha_switch_config_publish(
            "Automatic change mode",
            "mdi:auto-fix",
            "buttons_auto_lock",
            "enable",
            "config",
        )
        self._ha_number_slider_config_publish(
            "Automatic change threshold",
            "mdi:tune",
            "buttons_auto_lock",
            "threshold",
            0,
            100,
            "config",
        )

        self._ha_sensor_config_publish(
            "Battery level",
            "mdi:battery",
            "power",
            "battery_level",
            unit_of_measurement="%",
        )
        self._ha_sensor_config_publish(
            "Active power source",
            "mdi:power-plug-battery",
            "power",
            "mode",
            value_labels={
                PowerMode.USB.name: "USB power",
                PowerMode.BATTERY.name: "Battery power",
            },
        )
        self._ha_sensor_config_publish(
            "Connected power sources",
            "mdi:power-plug-battery",
            "power",
            "type",
            value_labels={
                PowerType.INVALID.name: "Unknown",
                PowerType.USB_ONLY.name: "USB only",
                PowerType.BATTERY_ONLY.name: "Battery only",
                PowerType.USB_AND_BATTERY.name: "USB and battery",
            },
        )

        self._ha_connection_sensor_config_publish("Connection", "device", "online")
        self._ha_binary_sensor_config_publish(
            "Error state", "mdi:alert", "device", "error_state"
        )
        self._ha_sensor_config_publish(
            "Error message",
            "mdi:alert",
            "device",
            "error_message",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "Serial number",
            "mdi:identifier",
            "device",
            "serial_number",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "Software version",
            "mdi:identifier",
            "device",
            "software_version",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "Hardware version",
            "mdi:identifier",
            "device",
            "hardware_version",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "Product ID",
            "mdi:identifier",
            "device",
            "product_id",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "UUID", "mdi:identifier", "device", "uuid", entity_category="diagnostic"
        )
        self._ha_sensor_timestamp_config_publish(
            "Last clock sync",
            "mdi:clock",
            "device",
            "ntp_last_correct",
            entity_category="diagnostic",
        )

        self._ha_sensor_config_publish(
            "Wi-Fi SSID", "mdi:wifi", "wifi", "ssid", entity_category="diagnostic"
        )
        self._ha_sensor_config_publish(
            "Wi-Fi RSSI",
            "mdi:wifi",
            "wifi",
            "rssi",
            unit_of_measurement="dBm",
            entity_category="diagnostic",
        )
        self._ha_sensor_config_publish(
            "Wi-Fi type", "mdi:wifi-cog", "wifi", "type", entity_category="diagnostic"
        )
        self._ha_sensor_config_publish(
            "Wi-Fi MAC address",
            "mdi:wifi",
            "wifi",
            "mac_address",
            entity_category="diagnostic",
        )

        self._ha_button_config_publish(
            "Reboot", "mdi:power", "device", "reboot", "diagnostic"
        )
        self._ha_button_config_publish(
            "Factory reset", "mdi:factory", "device", "factory_reset", "diagnostic"
        )
        self._ha_button_config_publish(
            "Reconnect Wi-Fi",
            "mdi:wifi-cancel",
            "device",
            "wifi_reconnect",
            "diagnostic",
        )
        self._ha_button_config_publish(
            "Format SD card", "mdi:delete", "device", "sd_card_format", "diagnostic"
        )
        self._ha_update_config_publish(
            "State Agent firmware",
            "mdi:chip",
            "state_agent",
            "update",
            "diagnostic",
        )
        self._ha_button_config_publish(
            "Check for State Agent updates",
            "mdi:update",
            "state_agent",
            "check_updates",
            "diagnostic",
            payload_press="check",
        )
        self._remove_obsolete_compatibility_discovery()
        self._ha_binary_sensor_config_publish(
            "Ping mitigation active",
            "mdi:shield-check-outline",
            "compatibility",
            "active",
            "diagnostic",
        )

        self._ha_sensor_config_publish(
            "Feeder motor state",
            "mdi:food",
            "food",
            "motor_state",
            entity_category="diagnostic",
        )
        self._ha_binary_sensor_config_publish(
            "Food outlet blocked", "mdi:food", "food", "outlet_blocked"
        )
        self._ha_binary_sensor_config_publish(
            "Low food", "mdi:food", "food", "low_fill_level"
        )
        self._ha_select_config_publish(
            "Bowl setup",
            "mdi:bowl-mix",
            "food",
            "bowl_mode",
            [BowlMode.SINGLE_BOWL.name, BowlMode.DOUBLE_BOWL.name],
            "config",
            {
                BowlMode.SINGLE_BOWL.name: "Single bowl",
                BowlMode.DOUBLE_BOWL.name: "Dual bowl",
            },
        )

        for plan_slot in range(1, 10):
            self._ha_text_config_publish(
                f"Feeding schedule {plan_slot}",
                "mdi:food",
                "food",
                f"plan_{plan_slot}",
                "config",
            )
        self._ha_button_config_publish(
            "Dispense food now", "mdi:food", "food", "manual_feed"
        )
        self._ha_number_slider_config_publish(
            "Manual feed portions",
            "mdi:hamburger-plus",
            "food",
            "manual_feed_grain_num",
            1,
            24,
        )

        self._ha_sensor_config_publish(
            "Dispensing status",
            "mdi:food",
            "food_output",
            "progress",
            value_labels={
                FoodOutputProgress.IDLE.name: "Idle",
                FoodOutputProgress.RUNNING.name: "Dispensing",
                FoodOutputProgress.BLOCKED.name: "Blocked",
                FoodOutputProgress.ERROR.name: "Error",
                FoodOutputProgress.RECOVERING.name: "Recovering",
            },
            availability_topic=DispensingStatusProjector.AVAILABILITY_TOPIC,
        )
        self._ha_sensor_timestamp_config_publish(
            "Last dispense started", "mdi:food", "food_output", "last_start"
        )
        self._ha_sensor_timestamp_config_publish(
            "Last dispense completed", "mdi:food", "food_output", "last_end"
        )
        self._ha_sensor_config_publish(
            "Last dispense portions", "mdi:food", "food_output", "last_grain_count"
        )
        self._ha_sensor_config_publish(
            "Last dispense source",
            "mdi:food",
            "food_output",
            "last_trigger",
            value_labels={
                GrainOutputType.INVALID.name: "Unknown",
                GrainOutputType.FEED_PLAN.name: "Feeding schedule",
                GrainOutputType.MANUAL_FEED.name: "Manual feed from app",
                GrainOutputType.MANUAL_FEED_BUTTON.name: "Feeder button",
            },
        )

    def _ha_connection_sensor_config_publish(
        self, user_friendly_name: str, group: str, name: str
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "device_class": "connectivity",
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "payload_on": "true",
            "payload_off": "false",
        }

        merged_payload = payload | self._device_flags_get()

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("binary_sensor", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_binary_sensor_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "icon": icon,
            "payload_on": "true",
            "payload_off": "false",
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("binary_sensor", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_button_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
        payload_press: str = "press",
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "payload_press": payload_press,
            "icon": icon,
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("button", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_update_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "icon": icon,
            "device_class": "firmware",
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "command_topic": self._device_base_path_get(f"{group}/cmd/install"),
            "payload_install": "install",
        }

        if entity_category is not None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )
        self._mqtt_publish(
            self._ha_config_topic_base_path_get("update", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_number_slider_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        min: int,
        max: int,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "min": min,
            "max": max,
            "mode": "slider",
            "icon": icon,
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("number", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_number_box_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        min: int,
        max: int,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "min": min,
            "max": max,
            "mode": "box",
            "icon": icon,
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("number", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_select_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        options: list[str],
        entity_category: str = None,
        option_labels: dict = None,
    ):
        displayed_options = options
        value_template = None
        command_template = None
        if option_labels is not None:
            missing_labels = set(options) - set(option_labels)
            if missing_labels:
                raise ValueError(
                    f"Missing display labels for select options: {sorted(missing_labels)}"
                )
            displayed_options = [option_labels[option] for option in options]
            value_template = self._ha_value_template_get(option_labels)
            command_template = self._ha_value_template_get(
                {label: raw_value for raw_value, label in option_labels.items()}
            )

        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "icon": icon,
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "optimistic": "false",
            "options": displayed_options,
        }

        if value_template is not None:
            payload["value_template"] = value_template
            payload["command_template"] = command_template

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("select", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_sensor_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        unit_of_measurement: str = None,
        entity_category: str = None,
        value_labels: dict = None,
        availability_topic: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "icon": icon,
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
        }

        if not unit_of_measurement == None:
            payload = payload | {"unit_of_measurement": unit_of_measurement}

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        if value_labels is not None:
            payload["value_template"] = self._ha_value_template_get(value_labels)

        merged_payload = (
            payload
            | self._device_flags_get()
            | self._availability_flags_get(availability_topic)
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("sensor", f"{group}_{name}"),
            merged_payload,
        )

    @staticmethod
    def _ha_value_template_get(value_labels: dict) -> str:
        encoded_labels = json.dumps(value_labels, separators=(",", ":"))
        return f"{{{{ {encoded_labels}.get(value, value) }}}}"

    def _ha_sensor_timestamp_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "icon": icon,
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "device_class": "timestamp",
            "value_template": "{{ as_datetime(value) }}",
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("sensor", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_switch_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "device_class": "switch",
            "icon": icon,
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
            "optimistic": "false",
            "payload_on": "true",
            "payload_off": "false",
            "state_on": "true",
            "state_off": "false",
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("switch", f"{group}_{name}"),
            merged_payload,
        )

    def _ha_text_config_publish(
        self,
        user_friendly_name: str,
        icon: str,
        group: str,
        name: str,
        entity_category: str = None,
    ):
        payload = {
            "name": user_friendly_name,
            "unique_id": self._config_unique_id_get(group, name),
            "icon": icon,
            "mode": "text",
            "command_topic": self._device_base_path_get(f"{group}/cmd/{name}"),
            "state_topic": self._device_base_path_get(f"{group}/{name}"),
        }

        if not entity_category == None:
            payload = payload | {"entity_category": entity_category}

        merged_payload = (
            payload | self._device_flags_get() | self._availability_flags_get()
        )

        self._mqtt_publish(
            self._ha_config_topic_base_path_get("text", f"{group}_{name}"),
            merged_payload,
        )

    def _device_flags_get(self):
        return {
            "device": self._device_info_get(),
        }

    def _availability_flags_get(self, relative_topic: str = None):
        return {
            "availability_topic": self._device_base_path_get(
                relative_topic or "device/online"
            ),
            "payload_available": "true",
            "payload_not_available": "false",
        }

    def _device_info_get(self):
        return {
            "identifiers": f"plaf203_{self.serial_number}",
            "name": f"Petlibro cat feeder {self.serial_number}",
            "model": "PLAF203",
            "manufacturer": "Pet libro",
            "sw_version": "unknown",
            "serial_number": self.serial_number,
        }

    def _remove_obsolete_compatibility_discovery(self) -> None:
        for component, name in (
            ("button", "compatibility_deploy"),
            ("switch", "compatibility_ping_mitigation"),
            ("binary_sensor", "compatibility_deployed"),
            ("binary_sensor", "compatibility_reboot_required"),
            ("sensor", "compatibility_deployment"),
            ("sensor", "compatibility_last_reason"),
        ):
            self._mqtt_publish(
                self._ha_config_topic_base_path_get(component, name), ""
            )

    def _mqtt_publish(self, topic: str, payload: dict | str):
        payload_json = json.dumps(payload) if isinstance(payload, dict) else payload
        self.mqtt.mqtt_publish(topic, payload_json, namespace="mqtt", retain=True)

    def _config_unique_id_get(self, type_: str, name: str):
        return f"plaf203_{self.serial_number}_{type_}_{name}"

    def _device_base_path_get(self, topic: str):
        return f"plaf203/{self.serial_number}/{topic}"

    def _ha_config_topic_base_path_get(self, component: str, name: str):
        return f"homeassistant/{component}/plaf203_{self.serial_number}/{name}/config"


class HomeAssistantStatePublisher:
    """Publish the HA mirror without owning or inferring feeder truth."""

    def __init__(self, mqtt: mqttapi.Mqtt, serial_number: str, logger):
        self.mqtt = mqtt
        self.serial_number = serial_number
        self.logger = logger

    def apply_feeder_truth(self, truth: FeederTruth) -> None:
        for projection in TRUTH_PROJECTIONS:
            value = truth.settings.get(projection.field)
            if value is None:
                continue
            try:
                converted = projection.converter(value)
            except (KeyError, TypeError, ValueError) as error:
                self.logger.warning(
                    "unsupported feeder truth value ignored",
                    field=projection.field,
                    value=value,
                    error_type=type(error).__name__,
                )
                continue
            self.publish(projection.topic, converted)

        plans_by_id = {plan.id: plan for plan in truth.plans.semantic_records}
        for plan_slot in range(1, 10):
            plan = plans_by_id.get(plan_slot)
            self.publish(
                f"food/plan_{plan_slot}",
                "unknown" if plan is None else plan_state_payload(plan),
                retain=True,
            )

    def publish(self, topic: str, value: object, *, retain: bool = False) -> None:
        if isinstance(value, bool):
            payload = "true" if value else "false"
        elif isinstance(value, datetime.datetime):
            payload = int(value.astimezone(datetime.timezone.utc).timestamp())
        elif isinstance(value, enum.Enum):
            payload = value.name
        elif isinstance(value, dict):
            payload = json.dumps(value)
        else:
            payload = value
        self.mqtt.mqtt_publish(
            self.topic(topic), payload, namespace="mqtt", retain=retain
        )

    def clear_retained(self, topic: str) -> None:
        self.mqtt.mqtt_publish(self.topic(topic), "", namespace="mqtt", retain=True)

    def topic(self, relative_topic: str) -> str:
        return f"plaf203/{self.serial_number}/{relative_topic}"
