"""Feeder protocol lifecycle, commands, acknowledgements, and telemetry callbacks."""

from __future__ import annotations

import datetime
import time

from appdaemon import adapi
from appdaemon.plugins.mqtt import mqttapi
from dispensing_status import (
    FoodOutputProgress,
    RuntimeSnapshotRequest,
    RuntimeSnapshotResult,
)
from feed_plans import build_plan_response, build_plan_service
from mqtt_client import Client
from petlibro_logging import PetlibroLogger
from protocol import (
    AgingType,
    AttrGetServiceIn,
    AttrGetServiceOut,
    AttrPushEventIn,
    AttrPushEventOut,
    AttrSetServiceIn,
    AttrSetServiceOut,
    BowlMode,
    Code,
    Commands,
    DeviceConfigSyncIn,
    DeviceConfigSyncOut,
    DeviceRebootIn,
    DeviceRebootOut,
    DeviceStartEventIn,
    DeviceStartEventOut,
    ErrorEventIn,
    ExecStep,
    FeedingPlanServiceIn,
    GetConfigIn,
    GetConfigOut,
    GetFeedingPlanEventIn,
    GrainOutputEventIn,
    GrainOutputEventOut,
    HeartbeatIn,
    InitializeSdCardServiceIn,
    InitializeSdCardServiceOut,
    ManualFeedingServiceIn,
    ManualFeedingServiceOut,
    MotionDetectionRange,
    MotionDetectionSensitivity,
    MqttAddr,
    NightVision,
    NtpIn,
    NtpOut,
    NtpSyncIn,
    NtpSyncOut,
    PercentageInt,
    Resolution,
    RestoreIn,
    RestoreOut,
    SoundDetectionSensitivity,
    Timestamp,
    VideoRecordMode,
    WifiReconnectServiceIn,
    WifiReconnectServiceOut,
)
from state_agent import FeederPlan as AgentFeederPlan
from state_coordinator import CommandReceipt, MqttAck


class Watchdog:
    def __init__(
        self, ad: adapi.ADAPI, name: str, period_sec: int, log_level: str = "info"
    ):
        self.ad: adapi.ADAPI = ad
        self.name = name
        self.period_sec = period_sec
        self.logger = PetlibroLogger(ad, "petlibro.watchdog", log_level)

        self.handle = None
        self.trigger_callback = None

    def trigger_listen(self, callback):
        self.trigger_callback = callback

    def reset(self):
        self.logger.trace("watchdog reset", name=self.name)

        self._cancel()
        self._schedule()

    def cancel(self):
        self._cancel()

    def _schedule(self):
        self.handle = self.ad.run_in(self._watchdog_run, self.period_sec)

    def _watchdog_run(self, cb_args):
        self.handle = None

        self.logger.warning("watchdog triggered", name=self.name)

        if self.trigger_callback != None:
            self.trigger_callback()

    def _cancel(self):
        self.ad.cancel_timer(self.handle, True)
        self.handle = None


class Backend:
    """Handle feeder MQTT lifecycle and low-level commands without owning state."""

    # Heartbeats arrive about every 51 seconds; tolerate one delayed interval.
    HEARTBEAT_WATCHDOG_PERIOD_SEC: int = 51 + 30
    NTP_SYNC_TIME_DIFF_THRESHOLD_SEC: int = 10
    NTP_SYNC_ACK_TIMEOUT_SEC: int = 10
    RUNTIME_SNAPSHOT_TIMEOUT_SEC: int = 10
    ERROR_DEDUP_TTL_SEC: int = 30 * 60
    ERROR_LOG_RATE_LIMIT_SEC: int = 5 * 60
    ERROR_TRACKING_LIMIT: int = 256

    _OPERATIONAL_ERROR_MESSAGES = {
        2003: "Scheduled feeding failed after firmware recovery attempts",
        2008: "Feeding failed: food outlet or blocking timeout",
        2011: "Manual feeding failed: food outlet sensor timeout",
        2012: "Scheduled feeding failed: food outlet sensor timeout",
        2024: "Local-button feeding failed: food outlet sensor timeout",
        2025: "Local-button feeding failed: no food output detected",
        2026: "Manual feeding failed: no food output detected",
        2027: "Scheduled feeding failed: no food output detected",
        2031: "Feeding failed: motor overcurrent recovery was unsuccessful",
    }
    _TRANSIENT_ERROR_MESSAGES = {
        2002: "Manual feeding request rejected while the feeder was busy",
        2007: "Manual feeding request ended without dispensing",
        2022: "Local-button feeding request rejected in the current feeder state",
        2023: "Local-button feeding request ended without dispensing",
    }

    def __init__(self) -> None:
        # Keep request-ordering state valid for narrow unit adapters that build
        # the backend before the full AppDaemon initialize lifecycle runs.
        self.connection_generation: int = 0
        self.runtime_snapshot_pending: RuntimeSnapshotRequest | None = None
        self.runtime_snapshot_timeout_handle = None
        self.runtime_snapshot_started_callback = None
        self.runtime_snapshot_result_callback = None
        self.runtime_event_generation_getter = lambda: 0
        self._recent_grain_operations: dict[str, float] = {}
        self._reported_feed_errors: dict[tuple[str, int, int], float] = {}
        self._unknown_error_log_times: dict[int, float] = {}

    def initialize(
        self,
        ad: adapi.ADAPI,
        mqtt: mqttapi.Mqtt,
        device_serial: str,
        log_level: str = "info",
    ):
        self.ad: adapi.ADAPI = ad
        self.logger = PetlibroLogger(ad, "petlibro.backend", log_level)

        self.device_serial = device_serial

        self.client = Client(ad, mqtt, device_serial, log_level)

        self.client.heartbeat_listen(self._heartbeat_cb)

        self.client.ntp_listen(self._ntp_cb)
        self.client.ntp_sync_listen(self._ntp_sync_cb)

        self.client.device_start_event_listen(self._device_start_event_cb)
        self.client.device_reboot_listen(self._device_reboot_cb)
        self.client.restore_listen(self._restore_cb)
        self.client.initialize_sd_card_service_listen(
            self._initialize_sd_card_service_cb
        )
        self.client.wifi_reconnect_service_listen(self._wifi_reconnect_service_cb)

        self.client.attr_get_service_listen(self._attr_get_service_cb)
        self.client.attr_push_event_listen(self._attr_push_event_cb)
        self.client.attr_set_service_listen(self._attr_set_service_cb)
        self.client.get_config_listen(self._get_config_cb)

        self.client.get_feeding_plan_event_listen(self._get_feeding_plan_event_cb)

        self.client.manual_feeding_service_listen(self._manual_feeding_service_cb)
        self.client.feeding_plan_service_listen(self._feeding_plan_service_cb)
        self.client.device_config_sync_listen(self._device_config_sync_cb)
        self.client.grain_output_event_listen(self._grain_output_event_cb)
        self.client.error_event_listen(self._error_event_cb)

        # Determine when the device is considered offline. The device sends a periodic
        # heartbeat message that is used to reset the watchdog
        self.heartbeat_watchdog = Watchdog(
            ad, "Heartbeat", Backend.HEARTBEAT_WATCHDOG_PERIOD_SEC, log_level
        )
        # Ensure this is always reset, will fire if not coming (back) online after a restart
        # to update the state
        self.heartbeat_watchdog.reset()
        self.heartbeat_watchdog.trigger_listen(self._heartbeat_watchdog_trigger)

        self.went_online_callback = None
        self.went_offline_callback = None
        self.ntp_sync_status_callback = None
        self.error_callback = None

        self.device_info_callback = None
        self.device_wifi_info_callback = None
        self.device_sd_card_info_callback = None

        self.capabilities_callback = None
        self.state_power_callback = None
        self.state_food_callback = None

        self.food_output_log_start_callback = None
        self.food_output_log_end_callback = None
        self.food_output_progress_callback = None
        self.heartbeat_callback = None
        self.persistent_state_hint_callback = None
        self.attr_set_ack_callback = None
        self.feeding_plan_ack_callback = None
        self.device_config_ack_callback = None
        self.feeding_plan_request_callback = None
        self.device_started_callback = None
        self.runtime_snapshot_started_callback = None
        self.runtime_snapshot_result_callback = None
        self.runtime_event_generation_getter = lambda: 0

        self.last_heartbeat_count: int = 0
        self.is_online: bool = False
        self.connection_generation = 0
        self.runtime_snapshot_pending = None
        self.runtime_snapshot_timeout_handle = None
        self.ntp_sync_pending_message_id: str | None = None
        self.ntp_sync_timeout_handle = None
        self._recent_grain_operations = {}
        self._reported_feed_errors = {}
        self._unknown_error_log_times = {}

        self.client.initialize()

    def went_online_listen(self, callback):
        self.went_online_callback = callback

    def went_offline_listen(self, callback):
        self.went_offline_callback = callback

    def ntp_sync_status_listen(self, callback):
        self.ntp_sync_status_callback = callback

    def error_listen(self, callback):
        self.error_callback = callback

    def device_info_listen(self, callback):
        self.device_info_callback = callback

    def device_wifi_info_listen(self, callback):
        self.device_wifi_info_callback = callback

    def device_sd_card_info_listen(self, callback):
        self.device_sd_card_info_callback = callback

    def capabilities_listen(self, callback):
        self.capabilities_callback = callback

    def state_power_listen(self, callback):
        self.state_power_callback = callback

    def state_food_listen(self, callback):
        self.state_food_callback = callback

    def food_output_log_start_listen(self, callback):
        self.food_output_log_start_callback = callback

    def food_output_log_end_listen(self, callback):
        self.food_output_log_end_callback = callback

    def food_output_progress_listen(self, callback):
        self.food_output_progress_callback = callback

    def heartbeat_event_listen(self, callback):
        self.heartbeat_callback = callback

    def persistent_state_hint_listen(self, callback):
        self.persistent_state_hint_callback = callback

    def attr_set_ack_listen(self, callback):
        self.attr_set_ack_callback = callback

    def feeding_plan_ack_listen(self, callback):
        self.feeding_plan_ack_callback = callback

    def device_config_ack_listen(self, callback):
        self.device_config_ack_callback = callback

    def feeding_plan_request_listen(self, callback):
        self.feeding_plan_request_callback = callback

    def device_started_listen(self, callback):
        self.device_started_callback = callback

    def runtime_snapshot_listen(
        self,
        started_callback,
        result_callback,
        runtime_event_generation_getter,
    ) -> None:
        self.runtime_snapshot_started_callback = started_callback
        self.runtime_snapshot_result_callback = result_callback
        self.runtime_event_generation_getter = runtime_event_generation_getter

    def settings_audio(
        self, enable: bool | None = None, file_url: str | None = None
    ) -> CommandReceipt:
        return self._send_attributes(
            enable_audio=enable,
            audio_url=file_url,
        )

    def settings_camera(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
        night_vision: NightVision | None = None,
        resolution: Resolution | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            camera_switch=enable,
            camera_aging_type=aging_type,
            night_vision=night_vision,
            resolution=resolution,
        )

    def settings_recording(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
        mode: VideoRecordMode | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            video_record_switch=enable,
            video_record_aging_type=aging_type,
            video_record_mode=mode,
        )

    def settings_sound(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
        volume: PercentageInt | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            sound_switch=enable,
            sound_aging_type=aging_type,
            volume=volume,
        )

    def settings_motion_detection(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
        range_: MotionDetectionRange | None = None,
        sensitivity: MotionDetectionSensitivity | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            motion_detection_switch=enable,
            motion_detection_aging_type=aging_type,
            motion_detection_range=range_,
            motion_detection_sensitivity=sensitivity,
        )

    def settings_sound_detection(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
        sensitivity: SoundDetectionSensitivity | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            sound_detection_switch=enable,
            sound_detection_aging_type=aging_type,
            sound_detection_sensitivity=sensitivity,
        )

    def settings_cloud_video_recording(
        self, enable: bool | None = None
    ) -> CommandReceipt:
        return self._send_attributes(cloud_video_record_switch=enable)

    def settings_button_lights(
        self,
        enable: bool | None = None,
        aging_type: AgingType | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            light_switch=enable,
            light_aging_type=aging_type,
        )

    def settings_buttons_auto_lock(
        self, enable: bool | None = None, threshold: int | None = None
    ) -> CommandReceipt:
        return self._send_attributes(
            auto_change_mode=enable,
            auto_threshold=threshold,
        )

    def settings_bowl_mode(self, mode: BowlMode) -> CommandReceipt:
        return self._send_attributes(bowl_mode=mode)

    def settings_feeding_video(
        self,
        enable: bool | None = None,
        video_on_start_feeding_plan: bool | None = None,
        video_after_manual_feeding: bool | None = None,
        recording_length_before_feeding_plan_time: int | None = None,
        recording_length_after_manual_feeding_time: int | None = None,
        video_watermark: bool | None = None,
        automatic_recording: int | None = None,
    ) -> CommandReceipt:
        return self._send_attributes(
            feeding_video_switch=enable,
            enable_video_start_feeding_plan=video_on_start_feeding_plan,
            enable_video_after_manual_feeding=video_after_manual_feeding,
            before_feeding_plan_time=recording_length_before_feeding_plan_time,
            automatic_recording=automatic_recording,
            after_manual_feeding_time=recording_length_after_manual_feeding_time,
            video_watermark_switch=video_watermark,
        )

    def _send_attributes(self, **attributes) -> CommandReceipt:
        message = AttrSetServiceOut.create(**attributes)
        self.client.attr_set_service_send(message)
        return CommandReceipt(Commands.ATTR_SET_SERVICE, message.message_id.data)

    def feeding_plans_send(self, plans: tuple[AgentFeederPlan, ...]) -> CommandReceipt:
        message = build_plan_service(plans)
        self.client.feeding_plan_service_send(message)
        return CommandReceipt(Commands.FEEDING_PLAN_SERVICE, message.message_id.data)

    def feeding_plan_request_respond(
        self,
        request_message: GetFeedingPlanEventIn,
        plans: tuple[AgentFeederPlan, ...] | None,
    ) -> bool:
        # AF203_FW ignores the response code and treats every present `plans`
        # array as authoritative. In particular, an error response containing
        # `plans: []` clears the persistent schedule. Silence is the only safe
        # response while feeder-local truth is unavailable.
        if plans is None:
            self.logger.warning(
                "feeding-plan request left unanswered because fresh feeder truth is unavailable"
            )
            return False
        response = build_plan_response(request_message, plans)
        self.client.get_feeding_plan_event_send(response)
        return True

    def device_config_sync(
        self,
        mqtt_host: str,
        mqtt_port: int,
        https_addr: str | None,
        tutk_p2p_region: str,
    ) -> CommandReceipt:
        message = DeviceConfigSyncOut.create(
            mqtt_addr=[MqttAddr(mqtt_host, mqtt_port)],
            https_addr=https_addr,
            tutk_p2p_region=tutk_p2p_region,
        )
        self.client.device_config_sync_send(message)
        return CommandReceipt(Commands.DEVICE_CONFIG_SYNC, message.message_id.data)

    def food_manual_feed_now(self, grain_num: int):
        manual_feeding_service_out = ManualFeedingServiceOut.create(grain_num=grain_num)
        self.client.manual_feeding_service_send(manual_feeding_service_out)

    def device_reboot(self):
        device_reboot_out = DeviceRebootOut.create()
        self.client.device_reboot_send(device_reboot_out)

    def device_factory_reset(self):
        restore_out = RestoreOut.create()
        self.client.restore_send(restore_out)

    def device_wifi_reconnect(self):
        wifi_reconnect_service_out = WifiReconnectServiceOut.create()
        self.client.wifi_reconnect_service_send(wifi_reconnect_service_out)

    def device_sd_card_format(self):
        initialize_sd_card_service_out = InitializeSdCardServiceOut.create()
        self.client.initialize_sd_card_service_send(initialize_sd_card_service_out)

    def _heartbeat_cb(self, heartbeat_in: HeartbeatIn):
        # Check if device restarted which might have not been detected by the watchdog
        # between two heartbeat messages
        if heartbeat_in.count < self.last_heartbeat_count:
            if self.is_online:
                self.is_online = False
                self._clear_runtime_snapshot_pending()
                if self.went_offline_callback != None:
                    self.went_offline_callback()
            self._clear_ntp_sync_pending()

        self.last_heartbeat_count = heartbeat_in.count

        if self.is_online == False:
            # Steps to bring the device "online"

            # Full state sync
            # Two cases:
            # - Device got rebooted and needs to be re-initialized
            # - Integration/backend got restarted and the device is already initialized and doing fine
            # The following covers for both cases though a re-init will also trigger the
            # DEVICE_START_EVENT. In any case, this ensures that the backend will always sync all
            # device state before considering the device online
            get_config_out = GetConfigOut.create()
            self.client.get_config_send(get_config_out)

            self.is_online = True
            self.connection_generation += 1

            if self.went_online_callback != None:
                self.went_online_callback()

            self.request_runtime_snapshot("feeder connected")

            if self.device_info_callback != None:
                self.device_info_callback(device_serial=self.device_serial)

            if self.device_wifi_info_callback != None:
                self.device_wifi_info_callback(
                    rssi=heartbeat_in.rssi, type_=heartbeat_in.wifi_type
                )

            # Force NTP sync because we don't know when that happened the last time to ensure that
            # the feeding plans are executed correctly
            if self._device_timestamp_sync_drift_check(
                Timestamp.now(), heartbeat_in.timestamp
            ):
                if self.ntp_sync_status_callback != None:
                    self.ntp_sync_status_callback(True)
            else:
                self.logger.debug(
                    "device NTP drift detected; correction in progress",
                    phase="heartbeat",
                )
                self._request_ntp_sync()
        # Periodic heartbeat resets the watchdog as long as the device keeps responding
        self.heartbeat_watchdog.reset()
        heartbeat_callback = getattr(self, "heartbeat_callback", None)
        if heartbeat_callback is not None:
            heartbeat_callback()

    def _ntp_cb(self, ntp_in: NtpIn):
        timestamp_now = Timestamp.now()
        force_time_calibration = None

        # Initial NTP package is also a check if the device has to re-sync the time
        # Don't consider this an error, yet
        if not self._device_timestamp_sync_drift_check(timestamp_now, ntp_in.timestamp):
            self.logger.debug("device NTP drift detected; requesting calibration")

            force_time_calibration = True
        else:
            force_time_calibration = False

        ntp_out = NtpOut(
            code=Code.OK,
            timestamp=timestamp_now,
            calibration_tag=force_time_calibration,
        )

        self.client.ntp_send(ntp_out)

        if self.ntp_sync_status_callback != None:
            self.ntp_sync_status_callback(True)

    def _ntp_sync_cb(self, ntp_sync_in: NtpSyncIn):
        pending_message_id = self.ntp_sync_pending_message_id
        if (
            pending_message_id is None
            or ntp_sync_in.message_id.data != pending_message_id
        ):
            self.logger.debug("ignored stale NTP synchronization acknowledgement")
            return

        self._clear_ntp_sync_pending()
        timestamp_now = Timestamp.now()

        # Basically just an ack from the device, but check again that drift is actually fine
        if ntp_sync_in.code != Code.OK or not self._device_timestamp_sync_drift_check(
            timestamp_now, ntp_sync_in.timestamp
        ):
            self.logger.error(
                "device NTP synchronization failed", phase="acknowledgement"
            )

            if self.ntp_sync_status_callback != None:
                self.ntp_sync_status_callback(False)
        else:
            if self.ntp_sync_status_callback != None:
                self.ntp_sync_status_callback(True)

    def _device_start_event_cb(self, device_start_event: DeviceStartEventIn):
        if device_start_event.success == True:
            if self.device_info_callback != None:
                self.device_info_callback(
                    product_id=device_start_event.pid,
                    uuid=device_start_event.uuid,
                    hardware_version=device_start_event.hardware_version,
                    software_version=device_start_event.software_version,
                )

            if self.device_wifi_info_callback != None:
                self.device_wifi_info_callback(mac_address=device_start_event.mac)
        else:
            self._error_report("Device initialization failed")

        device_start_event_out = DeviceStartEventOut.create(
            message_id=device_start_event.message_id, code=Code.OK
        )
        self.client.device_start_event_send(device_start_event_out)

        device_started_callback = getattr(self, "device_started_callback", None)
        if device_started_callback is not None:
            device_started_callback()

        self._device_timestamp_sync_drift_check_and_adjust(device_start_event.timestamp)

    def _device_reboot_cb(self, device_reboot_in: DeviceRebootIn):
        if device_reboot_in.code != Code.OK:
            self._error_report("Rebooting failed")
            return

        self.is_online = False
        self._clear_runtime_snapshot_pending()

        if self.went_offline_callback != None:
            self.went_offline_callback()

        # No need for sync drift check on device reboot

    def _restore_cb(self, restore_in: RestoreIn):
        if restore_in.code != Code.OK:
            self._error_report("Factory reset failed")
            return

        self.is_online = False
        self._clear_runtime_snapshot_pending()

        if self.went_offline_callback != None:
            self.went_offline_callback()

        # No need for sync drift check on device restore

    def _initialize_sd_card_service_cb(
        self, initialize_sd_card_service_in: InitializeSdCardServiceIn
    ):
        if initialize_sd_card_service_in.code != Code.OK:
            self._error_report("Formatting SD card failed")
            return

        self._device_timestamp_sync_drift_check_and_adjust(
            initialize_sd_card_service_in.timestamp
        )

    def _wifi_reconnect_service_cb(
        self, wifi_reconnect_service_in: WifiReconnectServiceIn
    ):
        if wifi_reconnect_service_in.code != Code.OK:
            self._error_report("Wifi force reconnect failed")
            return

        self.is_online = False
        self._clear_runtime_snapshot_pending()

        if self.went_offline_callback != None:
            self.went_offline_callback()

        # No need for sync drift check, reconnect triggers NTP call again

    def _attr_get_service_cb(self, message: AttrGetServiceIn):
        self._publish_attr_telemetry(message)
        self._device_timestamp_sync_drift_check_and_adjust(message.timestamp)
        self._notify_persistent_state_hint()

        pending = self.runtime_snapshot_pending
        if pending is None or message.message_id.data != pending.message_id:
            self.logger.debug("ignored uncorrelated attribute snapshot response")
            return
        if pending.connection_generation != self.connection_generation:
            self.logger.debug("ignored attribute snapshot from stale connection")
            self._clear_runtime_snapshot_pending()
            return

        self._clear_runtime_snapshot_pending()
        callback = self.runtime_snapshot_result_callback
        if callback is None:
            return
        if message.code != Code.OK:
            callback(
                RuntimeSnapshotResult(
                    request=pending,
                    failure_reason="feeder rejected runtime snapshot request",
                )
            )
            return
        callback(RuntimeSnapshotResult(request=pending, motor_state=message.motor_state))

    def _attr_push_event_cb(self, message: AttrPushEventIn):
        # The event acknowledgement is protocol-critical. Sparse telemetry and
        # state hints are best-effort and must never delay or prevent it.
        self.client.attr_push_event_send(
            AttrPushEventOut.create(
                message_id=message.message_id,
                code=Code.OK,
            )
        )
        self._notify_persistent_state_hint()
        self._publish_attr_telemetry(message)
        self._device_timestamp_sync_drift_check_and_adjust(message.timestamp)

    def _publish_attr_telemetry(
        self, message: AttrGetServiceIn | AttrPushEventIn
    ) -> None:
        field_value = lambda name: getattr(message, name, None)
        capabilities = {
            "camera/feature_enabled": field_value("enable_camera"),
            "recording/feature_enabled": field_value("enable_video_record"),
            "motion_detection/feature_enabled": field_value("enable_motion_detection"),
            "sound_detection/feature_enabled": field_value("enable_sound_detection"),
            "sound/feature_enabled": field_value("enable_sound"),
            "button_lights/feature_enabled": field_value("enable_light"),
        }
        if self.capabilities_callback is not None and any(
            value is not None for value in capabilities.values()
        ):
            self._emit_optional_telemetry(
                "capabilities", self.capabilities_callback, capabilities
            )

        if self.state_power_callback is not None and any(
            item is not None
            for item in (
                field_value("electric_quantity"),
                field_value("power_mode"),
                field_value("power_type"),
            )
        ):
            self._emit_optional_telemetry(
                "power",
                self.state_power_callback,
                battery_level=field_value("electric_quantity"),
                mode=field_value("power_mode"),
                type_=field_value("power_type"),
            )

        if self.state_food_callback is not None and any(
            item is not None
            for item in (
                field_value("motor_state"),
                field_value("grain_outlet_state"),
                field_value("surplus_grain"),
            )
        ):
            grain_outlet_state = field_value("grain_outlet_state")
            surplus_grain = field_value("surplus_grain")
            self._emit_optional_telemetry(
                "food",
                self.state_food_callback,
                motor_state=field_value("motor_state"),
                outlet_blocked=(
                    None if grain_outlet_state is None else not grain_outlet_state
                ),
                low_fill_level=(None if surplus_grain is None else not surplus_grain),
            )

        wifi_ssid = field_value("wifi_ssid")
        if self.device_wifi_info_callback is not None and wifi_ssid is not None:
            self._emit_optional_telemetry(
                "wifi", self.device_wifi_info_callback, ssid=wifi_ssid
            )

        if self.device_sd_card_info_callback is not None and any(
            item is not None
            for item in (
                field_value("sd_card_state"),
                field_value("sd_card_file_system"),
                field_value("sd_card_total_capacity"),
                field_value("sd_card_used_capacity"),
            )
        ):
            self._emit_optional_telemetry(
                "sd_card",
                self.device_sd_card_info_callback,
                state=field_value("sd_card_state"),
                file_system=field_value("sd_card_file_system"),
                total_capacity_mb=field_value("sd_card_total_capacity"),
                used_capacity_mb=field_value("sd_card_used_capacity"),
            )

    def _emit_optional_telemetry(self, kind: str, callback, *args, **kwargs) -> None:
        try:
            callback(*args, **kwargs)
        except Exception as error:
            self.logger.warning(
                "optional feeder telemetry callback failed",
                telemetry=kind,
                error_type=type(error).__name__,
            )

    def _notify_persistent_state_hint(self) -> None:
        if self.persistent_state_hint_callback is not None:
            self.persistent_state_hint_callback()

    def _attr_set_service_cb(self, attr_set_service_in: AttrSetServiceIn):
        success = attr_set_service_in.code == Code.OK
        if not success:
            self._error_report("Updating device attribute(s) failed")
        else:
            self._device_timestamp_sync_drift_check_and_adjust(
                attr_set_service_in.timestamp
            )
        attr_set_ack_callback = getattr(self, "attr_set_ack_callback", None)
        if attr_set_ack_callback is not None:
            attr_set_ack_callback(
                MqttAck(
                    Commands.ATTR_SET_SERVICE,
                    attr_set_service_in.message_id.data,
                    success,
                    "attribute update rejected" if not success else "",
                )
            )

    def _get_config_cb(self, get_config_in: GetConfigIn):
        if self.device_info_callback != None:
            self.device_info_callback(
                product_id=get_config_in.product_id,
                hardware_version=get_config_in.hardware_version,
                software_version=get_config_in.software_version,
            )

        if self.device_wifi_info_callback != None:
            self.device_wifi_info_callback(mac_address=get_config_in.mac_address)

        self._device_timestamp_sync_drift_check_and_adjust(get_config_in.timestamp)

    def _get_feeding_plan_event_cb(
        self, get_feeding_plan_event_in: GetFeedingPlanEventIn
    ):
        if self.feeding_plan_request_callback is not None:
            self.feeding_plan_request_callback(get_feeding_plan_event_in)
        else:
            self.logger.error(
                "feeding-plan truth unavailable; request left unanswered"
            )

        self._device_timestamp_sync_drift_check_and_adjust(
            get_feeding_plan_event_in.timestamp
        )

    def _manual_feeding_service_cb(
        self, manual_feeding_service_in: ManualFeedingServiceIn
    ):
        if manual_feeding_service_in.code != Code.OK.value:
            if manual_feeding_service_in.code == 2002:
                self.logger.warning(
                    "manual feeding request rejected",
                    error_code=manual_feeding_service_in.code,
                    reason="feeder_busy",
                )
            else:
                self._error_report(
                    f"Manual feeding failed (code {manual_feeding_service_in.code})"
                )
            return

        self._device_timestamp_sync_drift_check_and_adjust(
            manual_feeding_service_in.timestamp
        )

    def _feeding_plan_service_cb(self, feeding_plan_service_in: FeedingPlanServiceIn):
        success = feeding_plan_service_in.code == Code.OK
        if not success:
            self._error_report("Configuring feeding plan failed")
        else:
            self._device_timestamp_sync_drift_check_and_adjust(
                feeding_plan_service_in.timestamp
            )
        feeding_plan_ack_callback = getattr(self, "feeding_plan_ack_callback", None)
        if feeding_plan_ack_callback is not None:
            feeding_plan_ack_callback(
                MqttAck(
                    Commands.FEEDING_PLAN_SERVICE,
                    feeding_plan_service_in.message_id.data,
                    success,
                    feeding_plan_service_in.msg or "",
                )
            )

    def _device_config_sync_cb(self, device_config_sync_in: DeviceConfigSyncIn):
        success = device_config_sync_in.code == Code.OK
        if not success:
            self._error_report("Configuring device endpoints failed")
        else:
            self._device_timestamp_sync_drift_check_and_adjust(
                device_config_sync_in.timestamp
            )
        device_config_ack_callback = getattr(self, "device_config_ack_callback", None)
        if device_config_ack_callback is not None:
            device_config_ack_callback(
                MqttAck(
                    Commands.DEVICE_CONFIG_SYNC,
                    device_config_sync_in.message_id.data,
                    success,
                    "device endpoint configuration rejected" if not success else "",
                )
            )

    def _grain_output_event_cb(self, grain_output_event_in: GrainOutputEventIn):
        # The firmware commits persistent feed records only after accepting the
        # GRAIN_END response. Best-effort state projection must not delay or
        # prevent this protocol-critical acknowledgement.
        grain_output_event_out = GrainOutputEventOut.create(
            message_id=grain_output_event_in.message_id,
            code=Code.OK,
            exec_step=grain_output_event_in.exec_step,
        )
        self.client.grain_output_event_send(grain_output_event_out)

        if grain_output_event_in.exec_step == ExecStep.GRAIN_START:
            self._remember_grain_operation(grain_output_event_in.message_id.data)
            self._invoke_grain_projection(
                "log_start",
                self.food_output_log_start_callback,
                grain_output_event_in.type_,
                grain_output_event_in.expected_grain_num,
            )
            self._invoke_grain_projection(
                "progress_running",
                self.food_output_progress_callback,
                FoodOutputProgress.RUNNING,
            )
        elif grain_output_event_in.exec_step == ExecStep.GRAIN_BLOCKING:
            self._invoke_grain_projection(
                "progress_blocked",
                self.food_output_progress_callback,
                FoodOutputProgress.BLOCKED,
            )
        elif grain_output_event_in.exec_step == ExecStep.GRAIN_END:
            # Complete the operation projection before reporting errors. A
            # Home Assistant/reporting callback failure must not leave the
            # feeder stuck in RUNNING after the firmware has sent GRAIN_END.
            self._invoke_grain_projection(
                "log_end",
                self.food_output_log_end_callback,
                grain_output_event_in.type_,
                grain_output_event_in.actual_grain_num,
            )
            self._invoke_grain_projection(
                "progress_idle",
                self.food_output_progress_callback,
                FoodOutputProgress.IDLE,
            )

            error_was_reported = False
            event_time_ms = grain_output_event_in.exec_time.to_timestamp_epoch_ms()
            grain_error_code = getattr(grain_output_event_in, "error_code", None)
            if grain_error_code not in (None, 0, 0xFFFF):
                transient_message = self._TRANSIENT_ERROR_MESSAGES.get(
                    grain_error_code
                )
                if transient_message is not None:
                    error_was_reported = self._report_transient_feed_error_once(
                        grain_output_event_in.message_id.data,
                        grain_error_code,
                        transient_message,
                        event_time_ms,
                    )
                else:
                    error_was_reported = self._report_feed_error_once(
                        grain_output_event_in.message_id.data,
                        grain_error_code,
                        self._OPERATIONAL_ERROR_MESSAGES.get(
                            grain_error_code,
                            f"Feeding failed (firmware code {grain_error_code})",
                        ),
                        event_time_ms,
                    )

            if (
                not error_was_reported
                and not self._operation_has_reported_error(
                    grain_output_event_in.message_id.data
                )
                and grain_output_event_in.expected_grain_num
                != grain_output_event_in.actual_grain_num
            ):
                self._error_report(
                    f"Food output actual != expected: {grain_output_event_in.actual_grain_num} != {grain_output_event_in.expected_grain_num}"
                )
        else:
            self.logger.warning(
                "unhandled grain-output step", step=grain_output_event_in.exec_step
            )

        self._device_timestamp_sync_drift_check_and_adjust(
            grain_output_event_in.timestamp
        )

    def _invoke_grain_projection(self, name: str, callback, *args) -> None:
        if callback is None:
            return
        try:
            callback(*args)
        except Exception as exc:
            self.logger.warning(
                "grain-output projection callback failed",
                projection=name,
                error_type=type(exc).__name__,
            )

    def _error_event_cb(self, message: ErrorEventIn) -> None:
        code = message.error_code
        trigger_time_ms = message.trigger_time.to_timestamp_epoch_ms()
        metadata = {
            "error_code": code,
            "trigger_time_ms": trigger_time_ms,
            "extend_present": message.extend is not None,
            "extend_bytes": (
                len(message.extend.encode("utf-8"))
                if message.extend is not None
                else 0
            ),
        }
        if code == 2046:
            self.logger.trace("firmware cloud notification diagnostic", **metadata)
            return
        if code == 2048:
            self.logger.trace("firmware ISP exposure diagnostic", **metadata)
            return
        operational_message = self._OPERATIONAL_ERROR_MESSAGES.get(code)
        if operational_message is not None:
            self._report_feed_error_once(
                message.message_id.data,
                code,
                operational_message,
                trigger_time_ms,
            )
            return
        transient_message = self._TRANSIENT_ERROR_MESSAGES.get(code)
        if transient_message is not None:
            self._report_transient_feed_error_once(
                message.message_id.data,
                code,
                transient_message,
                trigger_time_ms,
                metadata,
            )
            return
        if self._is_recent_grain_operation(message.message_id.data):
            self._report_feed_error_once(
                message.message_id.data,
                code,
                f"Feeding failed (firmware code {code})",
                trigger_time_ms,
            )
            return
        self._log_unknown_error_event(message, metadata)

    def _tracking_maps(self):
        # Keep narrow unit adapters that instantiate Backend without initialize
        # compatible with the production lifecycle.
        if not hasattr(self, "_recent_grain_operations"):
            self._recent_grain_operations = {}
        if not hasattr(self, "_reported_feed_errors"):
            self._reported_feed_errors = {}
        if not hasattr(self, "_unknown_error_log_times"):
            self._unknown_error_log_times = {}
        return (
            self._recent_grain_operations,
            self._reported_feed_errors,
            self._unknown_error_log_times,
        )

    def _prune_error_tracking(self, now: float) -> None:
        operations, reported, unknown = self._tracking_maps()
        cutoff = now - self.ERROR_DEDUP_TTL_SEC
        for values in (operations, reported):
            for key, observed in list(values.items()):
                if observed < cutoff:
                    del values[key]
            while len(values) > self.ERROR_TRACKING_LIMIT:
                del values[next(iter(values))]
        while len(unknown) > self.ERROR_TRACKING_LIMIT:
            del unknown[next(iter(unknown))]

    def _remember_grain_operation(self, message_id: str) -> None:
        now = time.monotonic()
        operations, _, _ = self._tracking_maps()
        operations[message_id] = now
        self._prune_error_tracking(now)

    def _is_recent_grain_operation(self, message_id: str) -> bool:
        now = time.monotonic()
        self._prune_error_tracking(now)
        operations, _, _ = self._tracking_maps()
        observed = operations.get(message_id)
        return observed is not None and now - observed <= self.ERROR_DEDUP_TTL_SEC

    def _mark_feed_error_once(
        self, message_id: str, code: int, event_time_ms: int
    ) -> bool:
        now = time.monotonic()
        self._prune_error_tracking(now)
        _, reported, _ = self._tracking_maps()
        key = (message_id, code, event_time_ms)
        if key in reported:
            return False
        reported[key] = now
        while len(reported) > self.ERROR_TRACKING_LIMIT:
            del reported[next(iter(reported))]
        return True

    def _operation_has_reported_error(self, message_id: str) -> bool:
        self._prune_error_tracking(time.monotonic())
        _, reported, _ = self._tracking_maps()
        return any(
            key_message_id == message_id
            for key_message_id, _code, _event_time_ms in reported
        )

    def _report_feed_error_once(
        self, message_id: str, code: int, detail: str, event_time_ms: int
    ) -> bool:
        if not self._mark_feed_error_once(message_id, code, event_time_ms):
            return False
        reported_detail = (
            detail if f"code {code}" in detail else f"{detail} (firmware code {code})"
        )
        self._error_report(reported_detail)
        return True

    def _report_transient_feed_error_once(
        self,
        message_id: str,
        code: int,
        detail: str,
        event_time_ms: int,
        metadata: dict[str, object] | None = None,
    ) -> bool:
        if not self._mark_feed_error_once(message_id, code, event_time_ms):
            return False
        self.logger.warning(
            "firmware feeding request warning",
            detail=detail,
            **(metadata or {"error_code": code}),
        )
        return True

    def _log_unknown_error_event(
        self, message: ErrorEventIn, metadata: dict[str, object]
    ) -> None:
        now = time.monotonic()
        self._prune_error_tracking(now)
        _, _, unknown = self._tracking_maps()
        previous = unknown.get(message.error_code)
        if previous is not None and now - previous < self.ERROR_LOG_RATE_LIMIT_SEC:
            return
        unknown[message.error_code] = now
        while len(unknown) > self.ERROR_TRACKING_LIMIT:
            del unknown[next(iter(unknown))]
        self.logger.warning("unknown firmware error event", **metadata)

    def _heartbeat_watchdog_trigger(self):
        if self.is_online:
            self.is_online = False
            self._clear_runtime_snapshot_pending()
            if self.went_offline_callback != None:
                self.went_offline_callback()

    def request_runtime_snapshot(self, reason: str) -> bool:
        if not self.is_online:
            self.logger.debug(
                "runtime snapshot not requested while feeder is offline", reason=reason
            )
            return False

        self._clear_runtime_snapshot_pending()
        message = AttrGetServiceOut.create()
        request = RuntimeSnapshotRequest(
            message_id=message.message_id.data,
            connection_generation=self.connection_generation,
            runtime_event_generation=self.runtime_event_generation_getter(),
            reason=reason,
        )
        self.runtime_snapshot_pending = request
        if self.runtime_snapshot_started_callback is not None:
            self.runtime_snapshot_started_callback(request)

        try:
            self.runtime_snapshot_timeout_handle = self.ad.run_in(
                self._runtime_snapshot_timeout,
                self.RUNTIME_SNAPSHOT_TIMEOUT_SEC,
                message_id=request.message_id,
                connection_generation=request.connection_generation,
            )
            self.client.attr_get_service_send(message)
        except Exception as error:
            self._clear_runtime_snapshot_pending()
            self.logger.warning(
                "runtime snapshot could not be requested",
                reason=reason,
                error_type=type(error).__name__,
            )
            if self.runtime_snapshot_result_callback is not None:
                self.runtime_snapshot_result_callback(
                    RuntimeSnapshotResult(
                        request=request,
                        failure_reason="runtime snapshot request failed",
                    )
                )
            return False
        return True

    def _runtime_snapshot_timeout(self, kwargs: dict) -> None:
        pending = self.runtime_snapshot_pending
        if pending is None:
            return
        if (
            str(kwargs.get("message_id", "")) != pending.message_id
            or int(kwargs.get("connection_generation", -1))
            != pending.connection_generation
        ):
            return

        self.runtime_snapshot_pending = None
        self.runtime_snapshot_timeout_handle = None
        self.logger.warning(
            "runtime snapshot request timed out", reason=pending.reason
        )
        if self.runtime_snapshot_result_callback is not None:
            self.runtime_snapshot_result_callback(
                RuntimeSnapshotResult(
                    request=pending,
                    failure_reason="runtime snapshot request timed out",
                )
            )

    def _clear_runtime_snapshot_pending(self) -> None:
        self.runtime_snapshot_pending = None
        if self.runtime_snapshot_timeout_handle is not None:
            self.ad.cancel_timer(self.runtime_snapshot_timeout_handle, True)
            self.runtime_snapshot_timeout_handle = None

    def _device_timestamp_sync_drift_check_and_adjust(
        self, timestamp_device: Timestamp
    ):
        timestamp_now = Timestamp.now()

        if not self._device_timestamp_sync_drift_check(timestamp_now, timestamp_device):
            self.logger.debug("device time drift detected; forcing NTP sync")

            self._request_ntp_sync()

    def _request_ntp_sync(self) -> bool:
        if self.ntp_sync_pending_message_id is not None:
            self.logger.debug("device NTP correction already in progress")
            return False

        ntp_sync_out = NtpSyncOut.create()
        self.ntp_sync_pending_message_id = ntp_sync_out.message_id.data
        try:
            self.client.ntp_sync_send(ntp_sync_out)
            self.ntp_sync_timeout_handle = self.ad.run_in(
                self._ntp_sync_timeout,
                self.NTP_SYNC_ACK_TIMEOUT_SEC,
                message_id=ntp_sync_out.message_id.data,
            )
        except Exception as err:
            self.ntp_sync_pending_message_id = None
            self.ntp_sync_timeout_handle = None
            self.logger.warning(
                "device NTP correction could not be requested",
                phase="request",
                error_type=type(err).__name__,
            )
            if self.ntp_sync_status_callback != None:
                self.ntp_sync_status_callback(False)
            return False
        return True

    def _ntp_sync_timeout(self, kwargs: dict):
        message_id = str(kwargs.get("message_id", ""))
        if message_id != self.ntp_sync_pending_message_id:
            return
        self.ntp_sync_pending_message_id = None
        self.ntp_sync_timeout_handle = None
        self.logger.error("device NTP synchronization failed", phase="timeout")
        if self.ntp_sync_status_callback != None:
            self.ntp_sync_status_callback(False)

    def _clear_ntp_sync_pending(self):
        self.ntp_sync_pending_message_id = None
        if self.ntp_sync_timeout_handle is not None:
            self.ad.cancel_timer(self.ntp_sync_timeout_handle, True)
            self.ntp_sync_timeout_handle = None

    def _device_timestamp_sync_drift_check(
        self, timestamp_backend: Timestamp, timestamp_device: Timestamp
    ) -> bool:
        delta = timestamp_backend.abs_delta(timestamp_device)

        return delta < datetime.timedelta(seconds=self.NTP_SYNC_TIME_DIFF_THRESHOLD_SEC)

    def _error_report(self, message: str):
        if self.error_callback != None:
            self.error_callback(message)
