from __future__ import annotations

import json
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from installer import mqtt_server
from installer.mqtt_server import (
    BootstrapMqttServer,
    probe_mqtt_credentials,
    recv_packet,
    wait_for_mqtt_heartbeat,
)
from installer.protocol import (
    MqttConnect,
    build_ota_command,
    build_publish,
    encode_remaining_length,
    encode_utf8,
)


class FakeSession:
    def __init__(self, subscriptions: set[str], *, generation: int = 1) -> None:
        self.subscriptions = subscriptions
        self.connect = MqttConnect("SERIAL", "user", b"password", 90, 4)
        self.connection_generation = generation
        self.published: list[tuple[str, bytes, int, int]] = []

    def publish(self, topic: str, payload: bytes, packet_id: int = 1, qos: int = 1) -> None:
        self.published.append((topic, payload, packet_id, qos))


class FakeSocket:
    def __init__(self, incoming: bytes = b"", *, timeout_when_empty: bool = False) -> None:
        self.incoming = bytearray(incoming)
        self.timeout_when_empty = timeout_when_empty
        self.sent = bytearray()
        self.timeouts: list[float | None] = []

    def settimeout(self, timeout: float | None) -> None:
        self.timeouts.append(timeout)

    def recv(self, size: int) -> bytes:
        if not self.incoming:
            if self.timeout_when_empty:
                raise socket.timeout("synthetic inactivity timeout")
            return b""
        result = bytes(self.incoming[:size])
        del self.incoming[:size]
        return result

    def sendall(self, packet: bytes) -> None:
        self.sent.extend(packet)


def build_connect(*, keep_alive: int) -> bytes:
    flags = 0xC2
    variable = (
        b"\x00\x04MQTT\x04"
        + bytes((flags,))
        + keep_alive.to_bytes(2, "big")
    )
    payload = encode_utf8("SERIAL") + encode_utf8("user") + encode_utf8(b"password")
    return b"\x10" + encode_remaining_length(len(variable + payload)) + variable + payload


class BootstrapMqttServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = BootstrapMqttServer(("127.0.0.1", 0), expected_feeder_ip=None)

    def tearDown(self) -> None:
        self.server.server_close()

    def handle_fake_connection(self, connection: FakeSocket) -> None:
        mqtt_server._Handler(  # type: ignore[arg-type]
            connection,
            ("127.0.0.1", 1883),
            self.server,
        )

    def test_connect_handshake_keeps_an_independent_timeout(self) -> None:
        connection = FakeSocket(timeout_when_empty=True)

        self.handle_fake_connection(connection)

        self.assertEqual([mqtt_server.CONNECT_TIMEOUT_SECONDS], connection.timeouts)
        self.assertEqual("synthetic inactivity timeout", self.server.last_session_error)
        self.assertFalse(self.server.identity_ready.is_set())

    def test_long_negotiated_keepalive_replaces_fixed_timeout(self) -> None:
        connection = FakeSocket(build_connect(keep_alive=90) + b"\xe0\x00")

        self.handle_fake_connection(connection)

        self.assertEqual(
            [mqtt_server.CONNECT_TIMEOUT_SECONDS, 136.0],
            connection.timeouts,
        )
        self.assertEqual(b"\x20\x02\x00\x00", connection.sent)
        self.assertIsNone(self.server.last_session_error)

    def test_short_keepalive_gets_one_and_a_half_intervals_plus_tolerance(self) -> None:
        connection = FakeSocket(build_connect(keep_alive=10), timeout_when_empty=True)

        self.handle_fake_connection(connection)

        self.assertEqual(
            [mqtt_server.CONNECT_TIMEOUT_SECONDS, 16.0],
            connection.timeouts,
        )
        session_timeout = connection.timeouts[1]
        assert session_timeout is not None
        self.assertGreater(session_timeout, 10 * 1.5)
        self.assertEqual("synthetic inactivity timeout", self.server.last_session_error)

    def test_zero_keepalive_disables_session_inactivity_timeout(self) -> None:
        connection = FakeSocket(build_connect(keep_alive=0) + b"\xc0\x00\xe0\x00")

        self.handle_fake_connection(connection)

        self.assertEqual(
            [mqtt_server.CONNECT_TIMEOUT_SECONDS, None],
            connection.timeouts,
        )
        self.assertEqual(b"\x20\x02\x00\x00\xd0\x00", connection.sent)
        self.assertIsNone(self.server.last_session_error)

    def test_normal_disconnect_allows_same_feeder_to_reconnect(self) -> None:
        first = FakeSocket(build_connect(keep_alive=60) + b"\xe0\x00")
        second = FakeSocket(build_connect(keep_alive=60) + b"\xe0\x00")

        self.handle_fake_connection(first)
        self.handle_fake_connection(second)

        self.assertEqual(2, self.server.connection_attempts)
        self.assertEqual(2, self.server._connection_count)
        self.assertIsNone(self.server.session)
        self.assertIsNone(self.server.last_session_error)
        self.assertEqual(b"\x20\x02\x00\x00", first.sent)
        self.assertEqual(b"\x20\x02\x00\x00", second.sent)

    def test_accepts_stock_legacy_mqtt_31_connect(self) -> None:
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        try:
            flags = 0xC2
            variable = b"\x00\x06MQIsdp\x03" + bytes((flags,)) + b"\x00\x3c"
            payload = encode_utf8("SERIAL") + encode_utf8("user") + encode_utf8(b"password")
            packet = b"\x10" + encode_remaining_length(len(variable + payload)) + variable + payload
            with socket.create_connection(self.server.server_address, timeout=1) as connection:
                connection.sendall(packet)
                self.assertEqual(b"\x20\x02\x00\x00", recv_packet(connection))

            self.assertTrue(self.server.identity_ready.wait(1))
            assert self.server.identity is not None
            self.assertEqual(3, self.server.identity.protocol_level)
            self.assertEqual(1, self.server.connection_attempts)
            self.assertIsNone(self.server.last_session_error)
        finally:
            self.server.shutdown()
            thread.join(timeout=1)

    def test_final_broker_probe_uses_captured_legacy_protocol(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.response = bytearray(b"\x20\x02\x00\x00")
                self.sent = b""

            def __enter__(self) -> "FakeConnection":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

            def settimeout(self, _timeout: float) -> None:
                return None

            def sendall(self, packet: bytes) -> None:
                self.sent += packet

            def recv(self, size: int) -> bytes:
                result = bytes(self.response[:size])
                del self.response[:size]
                return result

        connection = FakeConnection()
        identity = SimpleNamespace(
            client_id="SERIAL",
            username="user",
            password=b"password",
            protocol_level=3,
        )
        with patch("installer.mqtt_server.socket.create_connection", return_value=connection):
            probe_mqtt_credentials("192.0.2.10", 1883, identity)

        self.assertIn(b"\x00\x06MQIsdp\x03", connection.sent)
        self.assertNotIn(b"\x00\x04MQTT\x04", connection.sent)

    def test_final_broker_observer_waits_for_exact_live_heartbeat(self) -> None:
        topic = "dl/PLAF203/AF0301/device/heart/post"

        class FakeConnection:
            def __init__(self) -> None:
                self.response = bytearray(
                    b"\x20\x02\x00\x00"
                    + b"\x90\x03\x00\x01\x00"
                    + build_publish(
                        "dl/PLAF203/OTHER/device/heart/post",
                        b'{"cmd":"HEARTBEAT"}',
                        qos=0,
                    )
                    + build_publish(topic, b'{"cmd":"HEARTBEAT"}', qos=1, packet_id=7)
                )
                self.sent = bytearray()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def settimeout(self, _timeout):
                return None

            def sendall(self, packet):
                self.sent.extend(packet)

            def recv(self, size):
                result = bytes(self.response[:size])
                del self.response[:size]
                return result

        connection = FakeConnection()
        subscribed = []
        with patch("installer.mqtt_server.socket.create_connection", return_value=connection):
            wait_for_mqtt_heartbeat(
                host="192.0.2.10",
                port=1883,
                username="backend",
                password="secret",
                feeder_client_id="AF0301",
                timeout=1,
                on_subscribed=lambda: subscribed.append(True),
            )

        self.assertEqual([True], subscribed)
        self.assertIn(encode_utf8("backend"), connection.sent)
        self.assertIn(encode_utf8(topic), connection.sent)
        self.assertIn(b"\x40\x02\x00\x07", connection.sent)

    def test_final_broker_observer_rejects_topic_wildcards_in_client_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "exact topic"):
            wait_for_mqtt_heartbeat(
                host="192.0.2.10",
                port=1883,
                username="backend",
                password="secret",
                feeder_client_id="AF/#",
                timeout=1,
                on_subscribed=lambda: None,
            )

    def test_start_event_records_version_and_gets_correlated_qos0_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession({sub})
        payload = json.dumps(
            {
                "cmd": "DEVICE_START_EVENT",
                "msgId": "boot-1",
                "softwareVersion": "3.1.48",
            }
        ).encode()

        self.server._record_publication(session, post, payload)  # type: ignore[arg-type]

        self.assertTrue(self.server.device_start_ready.is_set())
        self.assertEqual("3.1.48", self.server.software_version)
        self.assertEqual(1, len(session.published))
        topic, response, _packet_id, qos = session.published[0]
        self.assertEqual(sub, topic)
        self.assertEqual(0, qos)
        self.assertEqual("boot-1", json.loads(response)["msgId"])

    def test_start_response_waits_for_the_matching_subscription(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession(set())
        self.server._record_publication(  # type: ignore[arg-type]
            session,
            post,
            b'{"cmd":"DEVICE_START_EVENT","msgId":"boot-1","softwareVersion":"3.1.48"}',
        )
        self.assertEqual([], session.published)
        session.subscriptions.add(sub)
        self.server._subscriptions_changed(session)  # type: ignore[arg-type]
        self.assertEqual(sub, session.published[0][0])

    def test_plan_request_never_gets_fabricated_success_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            post,
            b'{"cmd":"GET_FEEDING_PLAN_EVENT","msgId":"plans-1","ts":123}',
        )

        self.assertEqual([], session.published)
        self.assertEqual(1, len(self.server.publications))
        self.assertEqual(post, self.server.publications[0][0])

    def test_grain_event_response_echoes_exec_step(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            post,
            b'{"cmd":"GRAIN_OUTPUT_EVENT","msgId":"feed-1",'
            b'"execStep":"GRAIN_START","ts":123}',
        )

        self.assertEqual(1, len(session.published))
        topic, response, _packet_id, qos = session.published[0]
        self.assertEqual(sub, topic)
        self.assertEqual(0, qos)
        self.assertEqual(
            {
                "cmd": "GRAIN_OUTPUT_EVENT",
                "msgId": "feed-1",
                "code": 0,
                "execStep": "GRAIN_START",
            },
            {key: value for key, value in json.loads(response).items() if key != "ts"},
        )

    def test_malformed_grain_event_does_not_get_success_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/event/post"
        sub = "dl/PLAF203/SERIAL/device/event/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            post,
            b'{"cmd":"GRAIN_OUTPUT_EVENT","msgId":"feed-1","ts":123}',
        )

        self.assertEqual([], session.published)

    def test_ntp_request_gets_noncalibrating_stock_shaped_response(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP","ts":123}'
        )

        self.assertEqual(1, len(session.published))
        topic, response, _packet_id, qos = session.published[0]
        self.assertEqual(sub, topic)
        self.assertEqual(0, qos)
        value = json.loads(response)
        self.assertEqual("NTP", value["cmd"])
        self.assertEqual(0, value["code"])
        self.assertIs(False, value["calibrationTag"])
        self.assertEqual(0, value["timezoneOffsetSeconds"])
        self.assertEqual(0, value["nextDSTTransitionTs"])
        self.assertNotIn("msgId", value)

    def test_ntp_response_waits_for_matching_subscription(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession(set())

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP","ts":123}'
        )
        self.assertEqual([], session.published)

        session.subscriptions.add(sub)
        self.server._subscriptions_changed(session)  # type: ignore[arg-type]
        self.assertEqual(sub, session.published[0][0])

    def test_ntp_sync_cannot_be_mistaken_for_initial_ntp_request(self) -> None:
        post = "dl/PLAF203/SERIAL/device/ntp/post"
        sub = "dl/PLAF203/SERIAL/device/ntp/sub"
        session = FakeSession({sub})

        self.server._record_publication(  # type: ignore[arg-type]
            session, post, b'{"cmd":"NTP_SYNC","msgId":"sync-1","code":0}'
        )

        self.assertEqual([], session.published)

    def test_start_event_on_wrong_topic_cannot_prove_oem_startup(self) -> None:
        session = FakeSession(set())

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            "dl/PLAF203/SERIAL/device/ota/post",
            b'{"cmd":"DEVICE_START_EVENT","softwareVersion":"3.1.48"}',
        )

        self.assertFalse(self.server.device_start_ready.is_set())

    def test_ota_terminal_result_is_correlated_by_message_id(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        ota_post = "dl/PLAF203/SERIAL/device/ota/post"
        session = FakeSession({ota_sub})
        self.server.session = session  # type: ignore[assignment]
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        self.assertEqual(ota_sub, self.server.publish_ota(command))

        self.server._record_publication(  # type: ignore[arg-type]
            session, ota_post, b'{"cmd":"OTA_INFORM","msgId":"ota-old","state":1}'
        )
        self.assertFalse(self.server.ota_result.is_set())

        self.server._record_publication(  # type: ignore[arg-type]
            session,
            ota_post,
            b'{"cmd":"OTA_INFORM","msgId":"ota-current","state":2,"errorMsg":"bad md5"}',
        )
        self.assertTrue(self.server.ota_result.is_set())
        self.assertEqual("bad md5", self.server.ota_error)

    def test_post_ota_start_requires_a_new_connection_generation(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        event_post = "dl/PLAF203/SERIAL/device/event/post"
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        original = FakeSession({ota_sub}, generation=4)
        self.server.session = original  # type: ignore[assignment]
        self.server.publish_ota(command)

        startup = b'{"cmd":"DEVICE_START_EVENT","msgId":"boot","softwareVersion":"3.1.48"}'
        self.server._record_publication(original, event_post, startup)  # type: ignore[arg-type]
        self.assertFalse(self.server.post_ota_start_ready.is_set())

        restored = FakeSession(set(), generation=5)
        self.server._record_publication(restored, event_post, startup)  # type: ignore[arg-type]
        self.assertTrue(self.server.post_ota_start_ready.is_set())

    def test_ota_puback_requires_the_sending_connection_generation(self) -> None:
        ota_sub = "dl/PLAF203/SERIAL/device/ota/sub"
        command = build_ota_command(
            msg_id="ota-current",
            artifact_url="http://192.0.2.10:18080/payload",
            artifact_md5="0" * 32,
            target_version="3.1.48",
        )
        sending = FakeSession({ota_sub}, generation=4)
        self.server.session = sending  # type: ignore[assignment]
        self.server.publish_ota(command)

        self.server._record_puback(FakeSession(set(), generation=3), 1)  # type: ignore[arg-type]
        self.assertFalse(self.server.ota_publish_acked.is_set())
        self.server._record_puback(sending, 1)  # type: ignore[arg-type]
        self.assertTrue(self.server.ota_publish_acked.is_set())


if __name__ == "__main__":
    unittest.main()
