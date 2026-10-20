import datetime
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = [
    json.loads(line)
    for line in (ROOT / 'tests' / 'fixtures' / 'protocol_3_1_48.sample.jsonl').read_text().splitlines()
]


def captured(cmd, direction, predicate=lambda payload: True):
    return next(
        row for row in FIXTURE
        if row['cmd'] == cmd
        and row['direction'] == direction
        and predicate(row['payload'])
    )


def topic_suffix(row):
    return '/' + '/'.join(row['topic'].split('/')[-2:])


NTP_REQUEST = captured('NTP', 'device_to_server')
NTP_RESPONSE = captured('NTP', 'server_to_device')
NTP_SYNC_REQUEST = captured('NTP_SYNC', 'server_to_device')
NTP_SYNC_RESPONSE = captured('NTP_SYNC', 'device_to_server')
BOOT_REQUEST = captured('DEVICE_START_EVENT', 'device_to_server')
DEVICE_CONFIG_REQUEST = captured('DEVICE_CONFIG_SYNC', 'server_to_device')
DEVICE_CONFIG_RESPONSE = captured('DEVICE_CONFIG_SYNC', 'device_to_server')
GET_PLAN_REQUEST = captured('GET_FEEDING_PLAN_EVENT', 'device_to_server')
GET_PLAN_RESPONSE = captured('GET_FEEDING_PLAN_EVENT', 'server_to_device')
FEEDING_PLAN_REQUEST = captured(
    'FEEDING_PLAN_SERVICE', 'server_to_device', lambda payload: bool(payload['plans']))
FEEDING_PLAN_RESPONSE = captured(
    'FEEDING_PLAN_SERVICE', 'device_to_server', lambda payload: bool(payload['plans']))
MANUAL_FEED_REQUEST = captured('MANUAL_FEEDING_SERVICE', 'server_to_device')
MANUAL_FEED_RESPONSE = captured('MANUAL_FEEDING_SERVICE', 'device_to_server')
GRAIN_REQUEST = captured('GRAIN_OUTPUT_EVENT', 'device_to_server')
GRAIN_RESPONSE = captured('GRAIN_OUTPUT_EVENT', 'server_to_device')
ATTR_PUSH_REQUEST = captured(
    'ATTR_PUSH_EVENT', 'device_to_server', lambda payload: 'enableLight' in payload)
ATTR_PUSH_STORAGE_REQUEST = captured(
    'ATTR_PUSH_EVENT', 'device_to_server',
    lambda payload: 'sdCardFileSystem' in payload and 'bowlMode' in payload)
ATTR_PUSH_RESPONSE = captured('ATTR_PUSH_EVENT', 'server_to_device')
DEVICE_LOG_REQUEST = captured('DEVICE_LOG_REPORT_EVENT', 'device_to_server')
sys.path.insert(0, str(ROOT / 'src'))
import backend as backend_module
import commands as command_module
import ha_entities
import mqtt_client
import protocol as p
import storage as storage_module
from petlibro_logging import PetlibroLogger


class FakeAd:
    def __init__(self):
        self.logs = []
        self.errors = []
        self.scheduled = []
        self.cancelled = []

    def log(self, value):
        self.logs.append(value)

    def error(self, value):
        self.errors.append(value)

    def run_in(self, callback, delay, **kwargs):
        handle = f"timer-{len(self.scheduled) + 1}"
        self.scheduled.append((handle, callback, delay, kwargs))
        return handle

    def cancel_timer(self, handle, silent):
        self.cancelled.append((handle, silent))


class FakeMqtt:
    def __init__(self):
        self.published = []

    def mqtt_publish(self, topic, payload, **kwargs):
        self.published.append((topic, json.loads(payload)))


class ProtocolCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.ad = FakeAd()
        self.mqtt = FakeMqtt()
        self.client = mqtt_client.Client(self.ad, self.mqtt, 'SERIAL')

    def test_home_assistant_discovery_identity_is_unique_per_serial(self):
        first = ha_entities.HomeAssistantDiscoveryMqtt(self.mqtt, 'SERIAL_ONE')
        second = ha_entities.HomeAssistantDiscoveryMqtt(self.mqtt, 'SERIAL_TWO')

        self.assertNotEqual(
            first._device_info_get()['identifiers'],
            second._device_info_get()['identifiers'],
        )
        self.assertEqual(
            'homeassistant/sensor/plaf203_SERIAL_ONE/device_uuid/config',
            first._ha_config_topic_base_path_get('sensor', 'device_uuid'),
        )

    def test_resolution_discovery_uses_friendly_labels_without_changing_topics(self):
        discovery = ha_entities.HomeAssistantDiscoveryMqtt(self.mqtt, 'SERIAL')
        discovery.discovery_issue()

        config = next(
            payload for topic, payload in self.mqtt.published
            if topic.endswith('/camera_resolution/config')
        )
        self.assertEqual('Feeder camera resolution', config['name'])
        self.assertEqual(['720p', '1080p'], config['options'])
        self.assertEqual(
            '{{ {"P720":"720p","P1080":"1080p"}.get(value, value) }}',
            config['value_template'],
        )
        self.assertEqual(
            '{{ {"720p":"P720","1080p":"P1080"}.get(value, value) }}',
            config['command_template'],
        )
        self.assertEqual(
            'plaf203/SERIAL/camera/cmd/resolution',
            config['command_topic'],
        )
        self.assertEqual(
            'plaf203/SERIAL/camera/resolution',
            config['state_topic'],
        )

    def test_bowl_mode_discovery_exposes_writable_device_configuration(self):
        discovery = ha_entities.HomeAssistantDiscoveryMqtt(self.mqtt, 'SERIAL')
        discovery.discovery_issue()

        config = next(
            payload for topic, payload in self.mqtt.published
            if topic.endswith('/food_bowl_mode/config')
        )
        self.assertEqual('Bowl setup', config['name'])
        self.assertEqual(
            ['Single bowl', 'Dual bowl'],
            config['options'],
        )
        self.assertIn('SINGLE_BOWL', config['value_template'])
        self.assertIn('DOUBLE_BOWL', config['value_template'])
        self.assertIn('SINGLE_BOWL', config['command_template'])
        self.assertIn('DOUBLE_BOWL', config['command_template'])
        self.assertEqual(
            'plaf203/SERIAL/food/cmd/bowl_mode',
            config['command_topic'],
        )
        self.assertEqual(
            'plaf203/SERIAL/food/bowl_mode',
            config['state_topic'],
        )

    def test_ntp_and_ntp_sync_include_capture_dst_schema(self):
        response = NTP_RESPONSE['payload']
        sync_request = NTP_SYNC_REQUEST['payload']
        value = datetime.datetime.fromtimestamp(response['ts'] / 1000, ZoneInfo('America/New_York'))
        timestamp = p.Timestamp(value)

        ntp = p.NtpOut(p.Code.OK, timestamp, False).to_mqtt_payload()
        ntp_sync = p.NtpSyncOut(p.MessageId('sync-id'), timestamp).to_mqtt_payload()
        fields = ('timezone', 'timezoneOffsetSeconds', 'nextDSTOffsetSeconds',
                  'nextDSTTransitionTs', 'secondNextDSTOffsetSeconds',
                  'secondNextDSTTransitionTs')
        for field in fields:
            self.assertEqual(response[field], ntp[field])
            self.assertEqual(response[field], ntp_sync[field])
            self.assertEqual(sync_request[field], ntp_sync[field])

        parsed_ntp = p.NtpIn.from_mqtt_payload(NTP_REQUEST['payload'])
        parsed_sync = p.NtpSyncIn.from_mqtt_payload(NTP_SYNC_RESPONSE['payload'])
        self.assertEqual(NTP_REQUEST['payload']['ts'], parsed_ntp.timestamp.to_timestamp_epoch_ms())
        self.assertEqual(sync_request['msgId'], parsed_sync.message_id.data)

        fixed = p.NtpOut(p.Code.OK, p.Timestamp(value.astimezone(datetime.timezone.utc)), False).to_mqtt_payload()
        self.assertEqual(0, fixed['nextDSTTransitionTs'])
        self.assertEqual(0, fixed['secondNextDSTTransitionTs'])

    def test_boot_ack_preserves_existing_device_endpoints_by_default(self):
        calls = []
        fake_client = types.SimpleNamespace(
            device_start_event_send=lambda message: calls.append(('event', message.to_mqtt_payload())),
            device_config_sync_send=lambda message: calls.append(('service', message.to_mqtt_payload())),
        )
        backend = backend_module.Backend()
        backend.client = fake_client
        backend.persist_feeder_mqtt = False
        backend.tutk_p2p_region = 'REGION_US'
        backend.device_info_callback = None
        backend.device_wifi_info_callback = None
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None

        boot = p.DeviceStartEventIn.from_mqtt_payload(BOOT_REQUEST['payload'])
        backend._device_start_event_cb(boot)

        self.assertEqual(['event'], [channel for channel, _ in calls])
        self.assertEqual(boot.message_id.data, calls[0][1]['msgId'])

    def test_boot_ack_defers_explicit_persistence_to_controller_coordinator(self):
        calls = []
        fake_client = types.SimpleNamespace(
            device_start_event_send=lambda message: calls.append(('event', message.to_mqtt_payload())),
            device_config_sync_send=lambda message: calls.append(('service', message.to_mqtt_payload())),
        )
        backend = backend_module.Backend()
        backend.client = fake_client
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "debug")
        backend.persist_feeder_mqtt = True
        backend.feeder_mqtt_host = 'mqtt.example.test'
        backend.feeder_mqtt_port = 1883
        backend.feeder_https_addr = 'api.example.test'
        backend.tutk_p2p_region = 'REGION_US'
        backend.device_info_callback = None
        backend.device_wifi_info_callback = None
        backend.device_started_callback = lambda: calls.append(('ready', None))
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None

        boot = p.DeviceStartEventIn.from_mqtt_payload(BOOT_REQUEST['payload'])
        backend._device_start_event_cb(boot)

        self.assertEqual(['event', 'ready'], [channel for channel, _ in calls])
        self.assertEqual(boot.message_id.data, calls[0][1]['msgId'])
        self.assertNotIn('service', [channel for channel, _ in calls])

    def test_device_config_sync_omits_unspecified_https_override(self):
        payload = p.DeviceConfigSyncOut.create(
            mqtt_addr=[p.MqttAddr('mqtt.example.test', 1883)],
            https_addr=None,
            tutk_p2p_region='REGION_US',
        ).to_mqtt_payload()

        self.assertNotIn('httpsAddr', payload)

    def test_storage_uses_entity_exists_before_creating_first_install_state(self):
        class StorageAD:
            def __init__(self):
                self.created = []

            def set_namespace(self, _namespace):
                return None

            def entity_exists(self, name, **_kwargs):
                return False

            def get_state(self, *_args, **_kwargs):
                raise AssertionError("get_state must not be used as an existence check")

            def set_state(self, name, **kwargs):
                self.created.append((name, kwargs))

        ad = StorageAD()
        storage = storage_module.Storage(ad, 'plaf203', 'SERIAL')
        storage.initialize()
        self.assertEqual(1, len(ad.created))
        self.assertTrue(all(
            created[1]["check_existence"] is False
            for created in ad.created
        ))

    def _heartbeat_backend(self):
        calls = []
        client = types.SimpleNamespace(
            get_config_send=lambda message: calls.append(
                ('get_config', message.to_mqtt_payload())
            ),
            attr_get_service_send=lambda message: calls.append(
                ('attr_get', message.to_mqtt_payload())
            ),
            ntp_sync_send=lambda message: calls.append(
                ('ntp_sync', message.to_mqtt_payload())
            ),
        )
        backend = backend_module.Backend()
        backend.ad = self.ad
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "debug")
        backend.client = client
        backend.device_serial = 'SERIAL'
        backend.last_heartbeat_count = 0
        backend.is_online = False
        backend.went_online_callback = None
        backend.went_offline_callback = None
        backend.ntp_sync_status_callback = None
        backend.ntp_sync_pending_message_id = None
        backend.ntp_sync_timeout_handle = None
        backend.device_info_callback = None
        backend.device_wifi_info_callback = None
        backend.heartbeat_callback = None
        backend.heartbeat_watchdog = types.SimpleNamespace(
            reset=lambda: calls.append(('watchdog_reset', None))
        )
        return backend, calls

    def test_initial_heartbeat_drift_starts_one_correction_without_false_failure(self):
        backend, calls = self._heartbeat_backend()
        statuses = []
        backend.ntp_sync_status_callback = statuses.append
        heartbeat_timestamp = p.Timestamp(
            datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc)
        )
        heartbeat = p.HeartbeatIn(
            heartbeat_timestamp, 1, -50, p.WifiType.TYPE_0
        )
        now = p.Timestamp(datetime.datetime(2026, 8, 11, tzinfo=datetime.timezone.utc))

        with patch.object(p.Timestamp, 'now', return_value=now):
            backend._heartbeat_cb(heartbeat)

        self.assertEqual([], statuses)
        self.assertEqual([], self.ad.errors)
        self.assertFalse(
            any("device NTP synchronization failed" in message for message in self.ad.logs)
        )
        self.assertEqual(1, len([name for name, _payload in calls if name == 'ntp_sync']))
        backend._device_timestamp_sync_drift_check_and_adjust(heartbeat_timestamp)
        self.assertEqual(1, len([name for name, _payload in calls if name == 'ntp_sync']))
        self.assertTrue(backend.is_online)
        self.assertIn(('watchdog_reset', None), calls)

    def test_ntp_correction_reports_failure_only_after_bad_ack_or_timeout(self):
        backend, calls = self._heartbeat_backend()
        statuses = []
        backend.ntp_sync_status_callback = statuses.append
        stale = p.Timestamp(datetime.datetime(2020, 1, 2, tzinfo=datetime.timezone.utc))
        now = p.Timestamp(datetime.datetime(2026, 8, 11, tzinfo=datetime.timezone.utc))

        with patch.object(p.Timestamp, 'now', return_value=now):
            self.assertTrue(backend._request_ntp_sync())
            message_id = backend.ntp_sync_pending_message_id
            backend._ntp_sync_cb(
                p.NtpSyncIn(p.MessageId(message_id), stale, p.Code.OK)
            )

        self.assertEqual([False], statuses)
        self.assertIsNone(backend.ntp_sync_pending_message_id)
        self.assertTrue(
            any("phase=acknowledgement" in message for message in self.ad.logs)
        )

        statuses.clear()
        with patch.object(p.Timestamp, 'now', return_value=now):
            self.assertTrue(backend._request_ntp_sync())
        message_id = backend.ntp_sync_pending_message_id
        backend._ntp_sync_timeout({"message_id": message_id})
        self.assertEqual([False], statuses)
        self.assertTrue(any("phase=timeout" in message for message in self.ad.logs))

    def test_heartbeat_offline_online_transitions_are_idempotent(self):
        backend, _calls = self._heartbeat_backend()
        online = []
        offline = []
        backend.went_online_callback = lambda: online.append(True)
        backend.went_offline_callback = lambda: offline.append(True)

        backend._heartbeat_cb(
            p.HeartbeatIn(p.Timestamp.now(), 5, -50, p.WifiType.TYPE_0)
        )
        backend._heartbeat_watchdog_trigger()
        backend._heartbeat_watchdog_trigger()
        backend._heartbeat_cb(
            p.HeartbeatIn(p.Timestamp.now(), 1, -50, p.WifiType.TYPE_0)
        )

        self.assertEqual([True, True], online)
        self.assertEqual([True], offline)

    def test_first_heartbeat_does_not_push_unconfigured_empty_plan(self):
        backend, calls = self._heartbeat_backend()
        heartbeat = p.HeartbeatIn(p.Timestamp.now(), 1, -50, p.WifiType.TYPE_0)

        backend._heartbeat_cb(heartbeat)

        self.assertNotIn('feeding_plan', [name for name, _payload in calls])

    def test_device_config_response_parser_and_service_post_dispatch(self):
        request = DEVICE_CONFIG_REQUEST['payload']
        outgoing = p.DeviceConfigSyncOut(
            p.MessageId(request['msgId']), p.Timestamp.from_timestamp_epoch_ms(request['ts']),
            [p.MqttAddr(**address) for address in request['mqttAddr']],
            request['httpsAddr'], request['tutkP2pRegion'])
        self.client.device_config_sync_send(outgoing)
        self.assertTrue(self.mqtt.published[0][0].endswith(topic_suffix(DEVICE_CONFIG_REQUEST)))
        self.assertEqual(request, self.mqtt.published[0][1])

        response = DEVICE_CONFIG_RESPONSE['payload']
        parsed = p.DeviceConfigSyncIn.from_mqtt_payload(response)
        self.assertEqual(response['msgId'], parsed.message_id.data)
        self.assertEqual(p.Code.OK, parsed.code)

        received = []
        self.client.device_config_sync_listen(received.append)
        self.client._mqtt_recv_service_cb('', {'payload': json.dumps(response)}, {})
        self.assertEqual(response['msgId'], received[0].message_id.data)

    def test_sparse_persistent_setting_push_only_requests_truth_refresh(self):
        hints = []
        backend = backend_module.Backend()
        backend.capabilities_callback = None
        backend.state_power_callback = None
        backend.state_food_callback = None
        backend.device_wifi_info_callback = None
        backend.device_sd_card_info_callback = None
        backend.persistent_state_hint_callback = lambda: hints.append(True)
        acknowledgements = []
        backend.client = types.SimpleNamespace(
            attr_push_event_send=acknowledgements.append
        )
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None

        sparse = p.AttrPushEventIn.from_mqtt_payload({
            'msgId': 'resolution-only',
            'ts': p.Timestamp.now().to_timestamp_epoch_ms(),
            'resolution': 'P720',
        })
        backend._attr_push_event_cb(sparse)

        self.assertEqual([True], hints)
        self.assertEqual(1, len(acknowledgements))

    def test_sparse_sound_push_without_wifi_is_acknowledged_and_hints_truth_refresh(self):
        order = []
        capability_states = []
        backend = backend_module.Backend()
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "debug")
        backend.capabilities_callback = lambda values: (
            order.append("telemetry"), capability_states.append(values)
        )
        backend.state_power_callback = None
        backend.state_food_callback = None
        backend.device_wifi_info_callback = lambda **_kwargs: self.fail(
            "absent Wi-Fi telemetry was emitted"
        )
        backend.device_sd_card_info_callback = None
        backend.persistent_state_hint_callback = lambda: order.append("hint")
        backend.client = types.SimpleNamespace(
            attr_push_event_send=lambda _message: order.append("ack")
        )
        backend._device_timestamp_sync_drift_check_and_adjust = lambda _timestamp: None

        sparse = p.AttrPushEventIn.from_mqtt_payload({
            "cmd": "ATTR_PUSH_EVENT",
            "msgId": "sound-only",
            "ts": p.Timestamp.now().to_timestamp_epoch_ms(),
            "soundSwitch": True,
            "enableSound": True,
        })
        backend._attr_push_event_cb(sparse)

        self.assertEqual(["ack", "hint", "telemetry"], order)
        self.assertTrue(sparse.has("soundSwitch"))
        self.assertFalse(sparse.has("wifiSsid"))
        self.assertIsNone(sparse.get("wifiSsid"))
        self.assertEqual(
            True, capability_states[0]["sound/feature_enabled"]
        )
        self.assertNotIn("sound/enable", capability_states[0])

    def test_sparse_mixed_push_preserves_field_names_not_sensitive_values(self):
        sensitive_value = "sensitive-camera-auth-value"
        sparse = p.AttrPushEventIn.from_mqtt_payload({
            "cmd": "ATTR_PUSH_EVENT",
            "msgId": "mixed-sparse",
            "ts": p.Timestamp.now().to_timestamp_epoch_ms(),
            "soundSwitch": True,
            "enableSound": True,
            "nightVision": "CLOSE",
            "cameraAuthInfo": sensitive_value,
            "futureUnknownField": 42,
        })

        self.assertTrue(sparse.sound_switch)
        self.assertTrue(sparse.enable_sound)
        self.assertEqual(p.NightVision.CLOSE, sparse.night_vision)
        self.assertTrue(sparse.has("cameraAuthInfo"))
        self.assertTrue(sparse.has("futureUnknownField"))
        self.assertEqual(42, sparse.get("futureUnknownField"))
        self.assertEqual(sensitive_value, sparse.get("cameraAuthInfo"))
        self.assertNotIn(sensitive_value, repr(sparse))

    def test_optional_telemetry_failure_cannot_prevent_push_ack_or_hint(self):
        order = []
        backend = backend_module.Backend()
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "debug")

        def fail_telemetry(_values):
            order.append("telemetry")
            raise RuntimeError("synthetic telemetry failure")

        backend.capabilities_callback = fail_telemetry
        backend.state_power_callback = None
        backend.state_food_callback = None
        backend.device_wifi_info_callback = None
        backend.device_sd_card_info_callback = None
        backend.persistent_state_hint_callback = lambda: order.append("hint")
        backend.client = types.SimpleNamespace(
            attr_push_event_send=lambda _message: order.append("ack")
        )
        backend._device_timestamp_sync_drift_check_and_adjust = lambda _timestamp: None

        sparse = p.AttrPushEventIn.from_mqtt_payload({
            "msgId": "telemetry-failure",
            "ts": p.Timestamp.now().to_timestamp_epoch_ms(),
            "enableSound": True,
        })
        backend._attr_push_event_cb(sparse)

        self.assertEqual(["ack", "hint", "telemetry"], order)
        self.assertTrue(any(
            "optional feeder telemetry callback failed" in message
            for message in self.ad.logs
        ))

    def test_sparse_food_push_preserves_absent_boolean_fields(self):
        food_states = []
        backend = backend_module.Backend()
        backend.capabilities_callback = None
        backend.state_power_callback = None
        backend.device_wifi_info_callback = None
        backend.device_sd_card_info_callback = None
        backend.persistent_state_hint_callback = None
        backend.state_food_callback = lambda **kwargs: food_states.append(kwargs)
        backend.client = types.SimpleNamespace(attr_push_event_send=lambda message: None)
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None

        sparse = p.AttrPushEventIn.from_mqtt_payload({
            'msgId': 'motor-only',
            'ts': p.Timestamp.now().to_timestamp_epoch_ms(),
            'motorState': 2,
        })
        backend._attr_push_event_cb(sparse)

        self.assertEqual([{
            'motor_state': 2,
            'outlet_blocked': None,
            'low_fill_level': None,
        }], food_states)

    def test_sparse_bowl_mode_push_does_not_bypass_feeder_truth(self):
        hints = []
        backend = backend_module.Backend()
        backend.capabilities_callback = None
        backend.state_power_callback = None
        backend.state_food_callback = None
        backend.device_wifi_info_callback = None
        backend.device_sd_card_info_callback = None
        backend.persistent_state_hint_callback = lambda: hints.append(True)
        backend.client = types.SimpleNamespace(attr_push_event_send=lambda message: None)
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None

        sparse = p.AttrPushEventIn.from_mqtt_payload({
            'msgId': 'bowl-only',
            'ts': p.Timestamp.now().to_timestamp_epoch_ms(),
            'bowlMode': 'SINGLE_BOWL',
        })
        backend._attr_push_event_cb(sparse)

        self.assertEqual([True], hints)

    def test_bowl_mode_command_serializes_attr_set_without_food_quantity(self):
        sent = []
        backend = backend_module.Backend()
        backend.client = types.SimpleNamespace(attr_set_service_send=sent.append)

        backend.settings_bowl_mode(p.BowlMode.DOUBLE_BOWL)

        payload = sent[0].to_mqtt_payload()
        self.assertEqual('DOUBLE_BOWL', payload['bowlMode'])
        self.assertNotIn('grainNum', payload)

    def test_bowl_mode_mqtt_command_reaches_backend(self):
        selected = []
        requests = []
        backend = types.SimpleNamespace(
            settings_bowl_mode=lambda **kwargs: selected.append(kwargs)
        )
        coordinator = types.SimpleNamespace(
            request_persistent_write=lambda request: (
                requests.append(request), request.publisher(None)
            )
        )
        router = command_module.CommandRouter.__new__(command_module.CommandRouter)
        router.backend = backend
        router.coordinator = coordinator
        router.logger = PetlibroLogger(self.ad, "petlibro.controller", "debug")
        spec = next(
            item for item in command_module.SETTING_COMMANDS
            if item.topic == "food/cmd/bowl_mode"
        )

        router._setting_handler(spec)('', {'payload': 'DOUBLE_BOWL'}, {})

        self.assertEqual([{'mode': p.BowlMode.DOUBLE_BOWL}], selected)
        self.assertEqual(
            [('food.bowl_mode', 'bowl_mode', 'dual_bowl')],
            [
                (request.control, request.predicate.field, request.target)
                for request in requests
            ],
        )

    def test_dual_bowl_wire_alias_normalizes_to_double_bowl(self):
        parsed = p.AttrPushEventIn.from_mqtt_payload({
            'msgId': 'bowl-alias',
            'ts': p.Timestamp.now().to_timestamp_epoch_ms(),
            'bowlMode': 'DUAL_BOWL',
        })

        self.assertEqual(p.BowlMode.DOUBLE_BOWL, parsed.bowl_mode)

    def test_invalid_feeding_plan_json_logs_position_without_payload(self):
        router = command_module.CommandRouter.__new__(command_module.CommandRouter)
        router.logger = PetlibroLogger(self.ad, "petlibro.controller", "debug")
        invalid = '{"minute":01}'

        router.plan_handler(1)('', {'payload': invalid}, {})

        message = self.ad.logs[-1]
        self.assertIn('invalid feeding-plan JSON ignored', message)
        self.assertIn('line=1', message)
        self.assertIn('column=12', message)
        self.assertIn('payload_length=13', message)
        self.assertNotIn(invalid, message)

    def test_all_nine_feeding_plan_slots_route_to_fresh_preflight(self):
        requests = []
        router = command_module.CommandRouter.__new__(command_module.CommandRouter)
        router.coordinator = types.SimpleNamespace(
            request_persistent_write=lambda request: requests.append(request) or True
        )
        router.backend = types.SimpleNamespace()
        router.logger = PetlibroLogger(self.ad, "petlibro.controller", "debug")

        for plan_slot in range(1, 10):
            payload = {
                'id': plan_slot,
                'execution_time': {'hour': 6 + plan_slot, 'minute': plan_slot},
                'scheduled_days': ['MONDAY'],
                'grain_num': plan_slot,
            }
            router.plan_handler(plan_slot)(
                '', {'payload': json.dumps(payload)}, {}
            )

        self.assertEqual(9, len(requests))
        self.assertEqual(list(range(1, 10)), [
            request.plan_patch.plan_id for request in requests
        ])
        self.assertTrue(all(request.requires_fresh_preflight for request in requests))
        self.assertTrue(all(request.plan_patch is not None for request in requests))

    def test_feeding_plan_slot_id_mismatch_is_rejected(self):
        writes = []
        router = command_module.CommandRouter.__new__(command_module.CommandRouter)
        router.coordinator = types.SimpleNamespace(
            request_persistent_write=lambda request: writes.append(request)
        )
        router.backend = types.SimpleNamespace()
        router.logger = PetlibroLogger(self.ad, "petlibro.controller", "debug")
        payload = {
            'id': 1,
            'execution_time': {'hour': 7, 'minute': 0},
            'scheduled_days': ['MONDAY'],
            'enable_audio': False,
            'play_audio_times': 1,
            'grain_num': 1,
        }

        router.plan_handler(2)('', {'payload': json.dumps(payload)}, {})

        self.assertEqual([], writes)
        message = self.ad.logs[-1]
        self.assertIn('feeding-plan slot/id mismatch ignored', message)
        self.assertIn('slot=2', message)
        self.assertIn('plan_id=1', message)
        self.assertNotIn(json.dumps(payload), message)

    def test_feeding_plan_subscriptions_bind_each_slot(self):
        router = command_module.CommandRouter.__new__(command_module.CommandRouter)
        subscriptions = []
        received_slots = []
        router._subscribe = (
            lambda topic, callback: subscriptions.append((topic, callback))
        )
        router.plan_handler = lambda plan_slot: (
            lambda eventname, data, kwargs: received_slots.append(plan_slot)
        )
        router.backend = types.SimpleNamespace()

        router.start()

        plan_subscriptions = [
            (topic, callback)
            for topic, callback in subscriptions
            if topic.startswith('food/cmd/plan_')
        ]
        self.assertEqual(9, len(plan_subscriptions))
        for expected_slot, (topic, callback) in enumerate(
            plan_subscriptions,
            start=1,
        ):
            self.assertEqual(
                'food/cmd/plan_{}'.format(expected_slot),
                topic,
            )
            callback('', {'payload': '{}'}, {})

        self.assertEqual(list(range(1, 10)), received_slots)

    def test_discovery_labels_do_not_change_backend_contract(self):
        discovery = ha_entities.HomeAssistantDiscoveryMqtt(self.mqtt, 'SERIAL')
        discovery.discovery_issue()

        configs = {
            topic: payload
            for topic, payload in self.mqtt.published
        }
        schedule = configs[
            'homeassistant/select/plaf203_SERIAL/camera_aging_type/config'
        ]
        self.assertEqual('Camera schedule mode', schedule['name'])
        self.assertEqual(['Always active', 'Scheduled'], schedule['options'])
        self.assertEqual(
            'plaf203/SERIAL/camera/cmd/aging_type',
            schedule['command_topic'],
        )
        self.assertIn('NON_SCHEDULED_ENABLED', schedule['value_template'])
        self.assertIn('SCHEDULED_ENABLED', schedule['command_template'])

        power = configs[
            'homeassistant/sensor/plaf203_SERIAL/power_type/config'
        ]
        self.assertEqual('Connected power sources', power['name'])
        self.assertEqual(
            'plaf203/SERIAL/power/type',
            power['state_topic'],
        )
        self.assertIn('USB_AND_BATTERY', power['value_template'])

        plan = configs[
            'homeassistant/text/plaf203_SERIAL/food_plan_9/config'
        ]
        self.assertEqual('Feeding schedule 9', plan['name'])
        self.assertEqual(
            'plaf203/SERIAL/food/cmd/plan_9',
            plan['command_topic'],
        )
        self.assertEqual(
            'plaf203/SERIAL/food/plan_9',
            plan['state_topic'],
        )

    def test_device_config_acknowledgement_is_forwarded_for_coordinator_matching(self):
        backend = backend_module.Backend()
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "debug")
        backend._device_timestamp_sync_drift_check_and_adjust = lambda timestamp: None
        errors = []
        acknowledgements = []
        backend._error_report = errors.append
        backend.device_config_ack_callback = acknowledgements.append

        backend._device_config_sync_cb(p.DeviceConfigSyncIn(
            p.MessageId('stale-config-id'), p.Timestamp.now(), p.Code.OK
        ))

        self.assertEqual(1, len(acknowledgements))
        self.assertEqual('stale-config-id', acknowledgements[0].message_id)
        self.assertEqual(p.Commands.DEVICE_CONFIG_SYNC, acknowledgements[0].ack_kind)
        self.assertTrue(acknowledgements[0].success)
        self.assertEqual([], errors)

    def test_get_plan_and_grain_ack_use_event_sub_and_echo_fields(self):
        get_request = p.GetFeedingPlanEventIn.from_mqtt_payload(GET_PLAN_REQUEST['payload'])
        get_response = p.GetFeedingPlanEventOut(
            get_request.message_id, p.Timestamp.now(), p.Code.OK, [])
        self.client.get_feeding_plan_event_send(get_response)

        grain_request = p.GrainOutputEventIn.from_mqtt_payload(GRAIN_REQUEST['payload'])
        grain_response = p.GrainOutputEventOut.create(
            message_id=grain_request.message_id, code=p.Code.OK, exec_step=grain_request.exec_step)
        self.client.grain_output_event_send(grain_response)

        self.assertTrue(self.mqtt.published[0][0].endswith(topic_suffix(GET_PLAN_RESPONSE)))
        self.assertEqual(get_request.message_id.data, self.mqtt.published[0][1]['msgId'])
        self.assertTrue(self.mqtt.published[1][0].endswith(topic_suffix(GRAIN_RESPONSE)))
        self.assertEqual('GRAIN_START', self.mqtt.published[1][1]['execStep'])

    def test_feeding_plan_round_trip_and_empty_replacement(self):
        response = p.FeedingPlanServiceIn.from_mqtt_payload(FEEDING_PLAN_RESPONSE['payload'])
        self.assertIsInstance(response.plans[0].sync_time, p.Timestamp)

        request = FEEDING_PLAN_REQUEST['payload']['plans'][0]
        plan = p.FeedingPlanOut(
            request['planId'], p.HourMinTimestamp.from_mqtt_payload_value(request['executionTime']),
            p.WeekdaySchedule.create(*[p.Weekday(day) for day in request['repeatDay']]),
            request['enableAudio'], request['audioTimes'], request['grainNum'],
            p.Timestamp.from_timestamp_epoch_ms(request['syncTime']))
        payload = p.FeedingPlanServiceOut.create([plan]).to_mqtt_payload()
        self.assertNotIn('skipEndTime', payload['plans'][0])
        self.assertEqual([], p.FeedingPlanServiceOut.create([]).to_mqtt_payload()['plans'])

    def test_manual_feed_shape_matches_capture(self):
        request = MANUAL_FEED_REQUEST['payload']
        message = p.ManualFeedingServiceOut(
            p.MessageId(request['msgId']), p.Timestamp.from_timestamp_epoch_ms(request['ts']), request['grainNum'])
        self.assertEqual(request, message.to_mqtt_payload())
        parsed = p.ManualFeedingServiceIn.from_mqtt_payload(MANUAL_FEED_RESPONSE['payload'])
        self.assertEqual(p.Code.OK.value, parsed.code)

        rejected = p.ManualFeedingServiceIn.from_mqtt_payload({
            "cmd": "MANUAL_FEEDING_SERVICE",
            "msgId": "manual-busy",
            "ts": request["ts"],
            "code": 2002,
        })
        self.assertEqual(2002, rejected.code)

    def test_error_event_parses_and_routes_without_application_ack(self):
        received = []
        self.client.error_event_listen(received.append)
        payload = {
            "cmd": "ERROR_EVENT",
            "msgId": "feed-error-1",
            "ts": 1786354621000,
            "errorCode": 2031,
            "triggerTime": 1786354620000,
            "extend": "diagnostic",
        }

        self.client._mqtt_recv_event_cb(
            "MQTT_MESSAGE",
            {
                "topic": "dl/PLAF203/SERIAL/device/event/post",
                "payload": json.dumps(payload),
            },
            {},
        )

        self.assertEqual(1, len(received))
        self.assertEqual(2031, received[0].error_code)
        self.assertEqual("diagnostic", received[0].extend)
        self.assertEqual([], self.mqtt.published)

    def test_grain_optional_error_code_and_numeric_retried_are_preserved(self):
        payload = dict(GRAIN_REQUEST["payload"])
        payload.update({"errorCode": 2031, "retried": 2})

        parsed = p.GrainOutputEventIn.from_mqtt_payload(payload)

        self.assertEqual(2031, parsed.error_code)
        self.assertEqual(2, parsed.retried)

    def test_error_event_rejects_oversized_extension_without_logging_contents(self):
        self.client.error_event_listen(lambda _message: None)
        secret = "x" * 4097
        payload = {
            "cmd": "ERROR_EVENT",
            "msgId": "oversized-error",
            "ts": 1786354621000,
            "errorCode": 9999,
            "triggerTime": 1786354620000,
            "extend": secret,
        }

        self.client._mqtt_recv_event_cb(
            "MQTT_MESSAGE",
            {
                "topic": "dl/PLAF203/SERIAL/device/event/post",
                "payload": json.dumps(payload),
            },
            {},
        )

        rendered = "\n".join(self.ad.logs)
        self.assertIn("invalid MQTT command payload ignored", rendered)
        self.assertNotIn(secret, rendered)

    def _error_backend(self):
        backend = backend_module.Backend()
        backend.logger = PetlibroLogger(self.ad, "petlibro.backend", "trace")
        backend.client = types.SimpleNamespace(
            grain_output_event_send=lambda message: self.mqtt.published.append(
                ("event/sub", message.to_mqtt_payload())
            )
        )
        backend.food_output_log_start_callback = None
        backend.food_output_log_end_callback = None
        backend.food_output_progress_callback = None
        backend._device_timestamp_sync_drift_check_and_adjust = lambda _timestamp: None
        return backend

    def test_routine_error_diagnostics_do_not_report_device_errors(self):
        backend = self._error_backend()
        errors = []
        backend._error_report = errors.append

        for code in (2046, 2048):
            backend._error_event_cb(p.ErrorEventIn(
                p.MessageId(f"diagnostic-{code}"),
                p.Timestamp.now(),
                code,
                p.Timestamp.now(),
                "details",
            ))

        self.assertEqual([], errors)
        rendered = "\n".join(self.ad.logs)
        self.assertIn("cloud notification diagnostic", rendered)
        self.assertIn("ISP exposure diagnostic", rendered)
        self.assertNotIn("details", rendered)

    def test_error_and_grain_duplicate_produce_one_operational_report(self):
        backend = self._error_backend()
        errors = []
        backend._error_report = errors.append
        message_id = p.MessageId("duplicate-feed-error")
        event_time = p.Timestamp.now()
        backend._remember_grain_operation(message_id.data)
        backend._error_event_cb(p.ErrorEventIn(
            message_id,
            p.Timestamp.now(),
            2031,
            event_time,
        ))
        grain = p.GrainOutputEventIn(
            message_id=message_id,
            timestamp=p.Timestamp.now(),
            finished=True,
            type_=p.GrainOutputType.MANUAL_FEED,
            actual_grain_num=0,
            expected_grain_num=1,
            exec_time=event_time,
            exec_step=p.ExecStep.GRAIN_END,
            error_code=2031,
        )

        backend._grain_output_event_cb(grain)

        self.assertEqual(1, len(errors))
        self.assertIn("motor overcurrent", errors[0])
        self.assertEqual("GRAIN_OUTPUT_EVENT", self.mqtt.published[0][1]["cmd"])

    def test_same_error_code_at_different_event_times_is_not_suppressed(self):
        backend = self._error_backend()
        errors = []
        backend._error_report = errors.append
        message_id = p.MessageId("reused-operation-id")
        first = p.Timestamp.from_timestamp_epoch_ms(1786354620000)
        second = p.Timestamp.from_timestamp_epoch_ms(1786354621000)

        for event_time in (first, second):
            backend._error_event_cb(p.ErrorEventIn(
                message_id,
                p.Timestamp.now(),
                2031,
                event_time,
            ))

        self.assertEqual(2, len(errors))

    def test_grain_ack_precedes_and_survives_projection_failure(self):
        backend = self._error_backend()
        order = []
        backend.client.grain_output_event_send = lambda _message: order.append("ack")
        backend.food_output_log_start_callback = lambda *_args: (
            order.append("projection"),
            (_ for _ in ()).throw(RuntimeError("synthetic")),
        )[-1]
        backend.food_output_progress_callback = lambda _progress: order.append(
            "progress"
        )
        grain = p.GrainOutputEventIn.from_mqtt_payload(GRAIN_REQUEST["payload"])

        backend._grain_output_event_cb(grain)

        self.assertEqual(["ack", "projection", "progress"], order)
        self.assertIn("projection callback failed", "\n".join(self.ad.logs))

    def test_grain_end_projection_failure_does_not_prevent_idle_or_error(self):
        backend = self._error_backend()
        order = []
        backend.client.grain_output_event_send = lambda _message: order.append("ack")
        backend.food_output_log_end_callback = lambda *_args: (
            order.append("log_end"),
            (_ for _ in ()).throw(RuntimeError("synthetic")),
        )[-1]
        backend.food_output_progress_callback = lambda progress: order.append(
            progress
        )
        backend._error_report = lambda detail: order.append(("error", detail))
        grain = p.GrainOutputEventIn(
            message_id=p.MessageId("failed-terminal-operation"),
            timestamp=p.Timestamp.now(),
            finished=True,
            type_=p.GrainOutputType.MANUAL_FEED,
            actual_grain_num=0,
            expected_grain_num=1,
            exec_time=p.Timestamp.now(),
            exec_step=p.ExecStep.GRAIN_END,
            error_code=2031,
        )

        backend._grain_output_event_cb(grain)

        self.assertEqual("ack", order[0])
        self.assertEqual("log_end", order[1])
        self.assertEqual(backend_module.FoodOutputProgress.IDLE, order[2])
        self.assertEqual("error", order[3][0])

    def test_transient_grain_error_warns_without_latching_device_error(self):
        backend = self._error_backend()
        errors = []
        backend._error_report = errors.append
        grain = p.GrainOutputEventIn(
            message_id=p.MessageId("busy-manual-feed"),
            timestamp=p.Timestamp.now(),
            finished=True,
            type_=p.GrainOutputType.MANUAL_FEED,
            actual_grain_num=0,
            expected_grain_num=1,
            exec_time=p.Timestamp.now(),
            exec_step=p.ExecStep.GRAIN_END,
            error_code=2002,
        )

        backend._grain_output_event_cb(grain)

        self.assertEqual([], errors)
        self.assertIn("feeding request warning", "\n".join(self.ad.logs))

    def test_attr_push_capture_fields_ack_and_serializers(self):
        request = ATTR_PUSH_REQUEST['payload']
        parsed = p.AttrPushEventIn.from_mqtt_payload(request)
        self.assertFalse(parsed.disable_hardware_button)
        self.assertTrue(parsed.enable_light)

        storage = p.AttrPushEventIn.from_mqtt_payload(ATTR_PUSH_STORAGE_REQUEST['payload'])
        self.assertEqual(p.BowlMode.SINGLE_BOWL, storage.bowl_mode)
        self.assertEqual(p.SdCardFileSystem.UNKNOWN, storage.sd_card_file_system)

        self.client.attr_push_event_send(p.AttrPushEventOut.create(
            message_id=parsed.message_id, code=p.Code.OK))
        self.assertTrue(self.mqtt.published[0][0].endswith(topic_suffix(ATTR_PUSH_RESPONSE)))

        set_payload = p.AttrSetServiceOut.create(
            enable_audio=True,
            light_aging_type=p.AgingType.NON_SCHEDULED_ENABLED,
            bowl_mode=p.BowlMode.SINGLE_BOWL,
        ).to_mqtt_payload()
        self.assertIs(set_payload['enableAudio'], True)
        self.assertEqual(1, set_payload['lightAgingType'])
        self.assertEqual('SINGLE_BOWL', set_payload['bowlMode'])
        json.dumps(set_payload)

    def test_device_log_is_parsed_and_ignored_without_ack(self):
        request = DEVICE_LOG_REQUEST['payload']
        self.client._mqtt_recv_event_cb('', {'payload': json.dumps(request)}, {})
        self.assertEqual([], self.mqtt.published)
        self.assertEqual([], self.ad.errors)
        self.assertEqual([], self.ad.logs)

        self.client.logger.level = "trace"
        self.client._mqtt_recv_event_cb('', {'payload': json.dumps(request)}, {})
        self.assertIn('ignored device log report', self.ad.logs[-1])

    def test_mqtt_handler_failure_is_contained_without_logging_camera_auth(self):
        sensitive_value = "sensitive-camera-auth-value"
        self.client.logger.level = "trace"
        self.client.attr_push_event_listen(
            lambda _message: (_ for _ in ()).throw(RuntimeError("synthetic"))
        )
        payload = {
            "cmd": "ATTR_PUSH_EVENT",
            "msgId": "contained-failure",
            "ts": p.Timestamp.now().to_timestamp_epoch_ms(),
            "soundSwitch": True,
            "enableSound": True,
            "cameraAuthInfo": sensitive_value,
        }

        self.client._mqtt_recv_event_cb(
            "MQTT_MESSAGE",
            {
                "topic": "dl/PLAF203/SERIAL/device/event/post",
                "payload": json.dumps(payload),
            },
            {},
        )

        rendered = "\n".join(self.ad.logs)
        self.assertNotIn(sensitive_value, rendered)
        self.assertIn("MQTT command handler failed", rendered)
        self.assertIn("cmd=ATTR_PUSH_EVENT", rendered)
        self.assertIn("msg_id=contained-failure", rendered)
        self.assertIn("exception_type=RuntimeError", rendered)

    def test_mqtt_parser_failure_is_contained_without_logging_camera_auth(self):
        sensitive_value = "sensitive-camera-auth-value"
        self.client.logger.level = "trace"
        self.client.attr_push_event_listen(lambda _message: None)
        payload = {
            "cmd": "ATTR_PUSH_EVENT",
            "msgId": "invalid-payload",
            "ts": p.Timestamp.now().to_timestamp_epoch_ms(),
            "nightVision": "NOT_A_MODE",
            "cameraAuthInfo": sensitive_value,
        }

        self.client._mqtt_recv_event_cb(
            "MQTT_MESSAGE",
            {
                "topic": "dl/PLAF203/SERIAL/device/event/post",
                "payload": json.dumps(payload),
            },
            {},
        )

        rendered = "\n".join(self.ad.logs)
        self.assertNotIn(sensitive_value, rendered)
        self.assertIn("invalid MQTT command payload ignored", rendered)
        self.assertIn("cmd=ATTR_PUSH_EVENT", rendered)
        self.assertIn("msg_id=invalid-payload", rendered)
        self.assertIn("exception_type=KeyError", rendered)

    def test_hour_minute_wire_parser_handles_late_hour(self):
        self.assertEqual(23, p.HourMinTimestamp.from_mqtt_payload_value('23:00').time.hour)


if __name__ == '__main__':
    unittest.main()
