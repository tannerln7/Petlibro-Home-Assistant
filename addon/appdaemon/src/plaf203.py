"""AppDaemon wiring for the Petlibro PLAF203 local backend."""

from __future__ import annotations

import datetime

from appdaemon import adapi, adbase
from appdaemon.plugins.mqtt import mqttapi
from backend import Backend
from camera_metadata import CameraMetadataPublisher
from commands import CommandRouter
from dispensing_status import DispensingStatusProjector, FoodOutputProgress
from feeder_mqtt_validation import validate_feeder_mqtt_destination
from ha_entities import HomeAssistantDiscoveryMqtt, HomeAssistantStatePublisher
from petlibro_logging import PetlibroLogger
from protocol import GetFeedingPlanEventIn
from state_agent import (
    FeederPlan,
    FeederTruth,
    StateAgentClient,
    UnavailableStateAgentClient,
)
from state_agent_updates import StateAgentUpdateCoordinator, StateAgentUpdateOptions
from state_coordinator import (
    FeederStateCoordinator,
    PersistentWriteRequest,
    VerificationMode,
)
from storage import Storage
from telemetry import TelemetryPublisher


class Plaf203(adbase.ADBase):
    """Compose protocol, authoritative state, HA mirroring, and actions."""

    def initialize(self) -> None:
        self.serial_number = str(self.args["serial_number"])
        self.ad: adapi.ADAPI = self.get_ad_api()
        self.mqtt: mqttapi.Mqtt = self.get_plugin_api("MQTT")
        log_level = self.args.get("petlibro_log_level", "info")
        self.logger = PetlibroLogger(self.ad, "petlibro.controller", log_level)

        self._configure_feeder_mqtt_persistence()
        self.state = HomeAssistantStatePublisher(
            self.mqtt, self.serial_number, self.logger
        )
        self.dispensing_status = DispensingStatusProjector(self.state, self.logger)
        self.dispensing_status.initialize()
        self._overall_available = False
        self.storage = Storage(self.ad, "plaf203", self.serial_number)
        self.storage.initialize()

        state_agent_url = str(self.args.get("petlibro_state_agent_url", "")).strip()
        if state_agent_url:
            state_agent = StateAgentClient(
                state_agent_url,
                str(self.args.get("petlibro_state_agent_token", "")),
                timeout_seconds=float(
                    self.args.get("petlibro_state_agent_timeout_seconds", 2)
                ),
            )
        else:
            state_agent = UnavailableStateAgentClient()
            self.logger.info(
                "feeder state API address unavailable until IP discovery completes"
            )

        self.coordinator = FeederStateCoordinator(
            self.ad,
            state_agent,
            PetlibroLogger(self.ad, "petlibro.state", log_level),
            self._apply_feeder_truth,
            self._coordinator_availability_set,
            self.storage.verified_truth_set,
            plan_collection_publisher=lambda plans: self.backend.feeding_plans_send(
                plans
            ),
        )
        update_options = StateAgentUpdateOptions.from_mapping(
            self.args.get("state_agent_updates")
        )
        self.updater = StateAgentUpdateCoordinator(
            self.ad,
            self.state,
            state_agent,
            PetlibroLogger(self.ad, "petlibro.updates", log_level),
            update_options,
        )
        self.updater.start()
        self.backend = Backend()
        self.backend.initialize(
            self.ad, self.mqtt, self.serial_number, log_level=log_level
        )

        self.telemetry = TelemetryPublisher(self.state, self.logger)
        self.telemetry.register(self.backend)
        self._register_backend_listeners()

        self.discovery = HomeAssistantDiscoveryMqtt(self.mqtt, self.serial_number)
        self.discovery.discovery_issue()
        self.mqtt.listen_event(
            self._home_assistant_status_cb,
            "MQTT_MESSAGE",
            topic="homeassistant/status",
            namespace="mqtt",
        )
        configured_uid = str(self.args.get("device_uid", "")).strip()
        if len(configured_uid) == 20 and configured_uid.isalnum():
            self.state.publish("device/uuid", configured_uid)
        self.state.publish("device/online", False)

        self.commands = CommandRouter(
            self.mqtt,
            self.serial_number,
            self.backend,
            self.coordinator,
            self.storage,
            self.state,
            self.logger,
            updater=self.updater,
            raw_settings_diagnostics=self.logger.enabled("debug"),
        )
        self.commands.restore_local_preferences()
        self.commands.start()

        self.camera_metadata = CameraMetadataPublisher(
            self.ad,
            self.mqtt,
            enabled=bool(self.args.get("publish_camera_metadata", True)),
            product=self.args.get("product", "PLAF203"),
            serial=self.serial_number,
            stream_name=self.args.get("go2rtc_stream_name", "petlibro_feeder"),
            requested_quality=self.args.get("camera_quality", "hd"),
            configured_hd_probe_wait_ms=int(self.args.get("hd_probe_wait_ms", 15000)),
            rtsp_port=int(self.args.get("go2rtc_rtsp_port", 8554)),
            status_file=self.args.get(
                "camera_status_file", "/data/petlibro_camera_status.json"
            ),
            topic_prefix=self.args.get(
                "camera_metadata_topic_prefix",
                "petlibro_local/{}/{}/camera".format(
                    self.args.get("product", "PLAF203"), self.serial_number
                ),
            ),
            heartbeat_seconds=int(
                self.args.get("camera_metadata_interval_seconds", 30)
            ),
            log_level=log_level,
        )
        self.camera_metadata.start()
        self.logger.info("controller initialized", serial=self.serial_number)

    def terminate(self) -> None:
        self.camera_metadata.stop()
        self.updater.stop()
        self.coordinator.shutdown()
        self.storage.terminate()
        self.dispensing_status.on_feeder_disconnected()
        self.state.publish("device/online", False)

    def _configure_feeder_mqtt_persistence(self) -> None:
        enabled = bool(self.args.get("persist_feeder_mqtt", False))
        host = str(self.args.get("feeder_mqtt_host", "")).strip()
        port = int(self.args.get("feeder_mqtt_port", 1883))
        if enabled:
            try:
                host, addresses = validate_feeder_mqtt_destination(host, port)
            except ValueError as error:
                enabled = False
                self.logger.error(
                    "feeder MQTT persistence blocked by validation",
                    reason=str(error),
                )
            else:
                self.logger.info(
                    "feeder MQTT persistence destination validated",
                    broker=f"{host}:{port}",
                    resolved_addresses=list(addresses),
                )

        self.persist_feeder_mqtt = enabled
        self.feeder_mqtt_host = host
        self.feeder_mqtt_port = port
        self.feeder_https_addr = (
            str(self.args.get("feeder_https_addr", "")).strip() or None
        )
        self.tutk_p2p_region = str(self.args.get("tutk_p2p_region", "REGION_US"))
        self._feeder_mqtt_persistence_due = False

    def _register_backend_listeners(self) -> None:
        self.backend.went_online_listen(self._went_online)
        self.backend.went_offline_listen(self._went_offline)
        self.backend.ntp_sync_status_listen(self._ntp_sync_status)
        self.backend.error_listen(self._device_error)
        self.backend.heartbeat_event_listen(self.coordinator.on_heartbeat)
        self.backend.persistent_state_hint_listen(
            self.coordinator.on_persistent_state_hint
        )
        self.backend.attr_set_ack_listen(self.coordinator.on_mqtt_ack)
        self.backend.feeding_plan_ack_listen(self.coordinator.on_mqtt_ack)
        self.backend.device_config_ack_listen(self.coordinator.on_mqtt_ack)
        self.backend.feeding_plan_request_listen(self._feeding_plan_requested)
        self.backend.device_started_listen(self._device_started)
        self.backend.food_output_progress_listen(self._food_output_progress)
        self.backend.runtime_snapshot_listen(
            self.dispensing_status.runtime_snapshot_started,
            self.dispensing_status.apply_runtime_snapshot,
            self.dispensing_status.runtime_event_generation_get,
        )

    def _apply_feeder_truth(self, truth: FeederTruth) -> None:
        self.state.apply_feeder_truth(truth)

    def _coordinator_availability_set(self, available: bool) -> None:
        self._overall_available = available
        self.state.publish("device/online", available)
        if not available:
            return
        self.updater.on_usable_connection()
        self.state.publish("device/error_state", False)
        self.state.publish("device/error_message", "No error")
        if self.persist_feeder_mqtt and self._feeder_mqtt_persistence_due:
            request = PersistentWriteRequest(
                control="device.feeder_mqtt_destination",
                target=f"{self.feeder_mqtt_host}:{self.feeder_mqtt_port}",
                publisher=lambda _truth: self.backend.device_config_sync(
                    self.feeder_mqtt_host,
                    self.feeder_mqtt_port,
                    self.feeder_https_addr,
                    self.tutk_p2p_region,
                ),
                predicate=None,
                verification_mode=VerificationMode.ACK_ONLY_UNVERIFIED,
                command_summary="DEVICE_CONFIG_SYNC",
            )
            if self.coordinator.request_persistent_write(request):
                self._feeder_mqtt_persistence_due = False
                self.logger.info(
                    "feeder MQTT persistence request queued after reconciliation",
                    broker=f"{self.feeder_mqtt_host}:{self.feeder_mqtt_port}",
                )

    def _device_started(self) -> None:
        self.coordinator.on_feeder_connected()
        if self.persist_feeder_mqtt:
            self._feeder_mqtt_persistence_due = True
            if self.coordinator.is_ready_for_user_write():
                self._coordinator_availability_set(True)

    def _feeding_plan_requested(self, request: GetFeedingPlanEventIn) -> None:
        def respond(plans: tuple[FeederPlan, ...] | None) -> None:
            if plans is None:
                self.logger.warning(
                    "feeding-plan request left unanswered because fresh feeder truth is unavailable"
                )
                return
            try:
                self.backend.feeding_plan_request_respond(request, plans)
            except ValueError as error:
                self.logger.error(
                    "feeding-plan request left unanswered because feeder truth could not be represented safely",
                    reason=str(error),
                )

        self.coordinator.request_plan_snapshot(respond)

    def _went_online(self) -> None:
        self.logger.info("device online", serial=self.serial_number)
        self.dispensing_status.on_feeder_connected(self.backend.connection_generation)
        self.coordinator.on_feeder_connected()

    def _went_offline(self) -> None:
        self.logger.info("device offline", serial=self.serial_number)
        self.dispensing_status.on_feeder_disconnected()
        self.coordinator.on_feeder_disconnected("feeder MQTT heartbeat lost")

    def _food_output_progress(self, progress: FoodOutputProgress) -> None:
        self.dispensing_status.apply_grain_event(
            progress,
            self.backend.connection_generation,
        )

    def _home_assistant_status_cb(self, _eventname, data, _kwargs) -> None:
        payload = data.get("payload") if isinstance(data, dict) else None
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8", errors="replace")
        if not isinstance(payload, str) or payload.strip().lower() != "online":
            return

        self.logger.info("Home Assistant online; restoring MQTT projections")
        self.discovery.discovery_issue()
        self.state.publish("device/online", self._overall_available)
        latest_truth = self.coordinator.latest_truth()
        if latest_truth is not None:
            self._apply_feeder_truth(latest_truth)
        updater = getattr(self, "updater", None)
        if updater is not None:
            updater.on_home_assistant_birth()
        self.dispensing_status.mark_unavailable()
        self.backend.request_runtime_snapshot("Home Assistant birth")

    def _ntp_sync_status(self, successful: bool) -> None:
        if successful:
            self.state.publish(
                "device/ntp_last_correct", datetime.datetime.now().astimezone()
            )
            return
        self.logger.warning("device NTP correction failed")
        self.state.publish("device/error_state", True)
        self.state.publish("device/error_message", "NTP sync with device failed")

    def _device_error(self, message: str) -> None:
        self.logger.error("device error", detail=message)
        self.state.publish("device/error_state", True)
        self.state.publish("device/error_message", message)
