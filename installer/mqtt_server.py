"""Single-device MQTT 3.1/3.1.1 server used only during OTA bootstrap."""

from __future__ import annotations

import dataclasses
import json
import secrets
import socket
import socketserver
import struct
import threading
import time
from collections.abc import Callable

try:
    from .protocol import (
        MqttConnect,
        ProtocolError,
        build_connack,
        build_puback,
        build_publish,
        build_suback,
        encode_remaining_length,
        encode_utf8,
        parse_connect,
        parse_publish,
        parse_subscribe,
    )
except ImportError:  # pragma: no cover - direct script execution support
    from protocol import (  # type: ignore[no-redef]
        MqttConnect,
        ProtocolError,
        build_connack,
        build_puback,
        build_publish,
        build_suback,
        encode_remaining_length,
        encode_utf8,
        parse_connect,
        parse_publish,
        parse_subscribe,
    )


MAX_PACKET_BYTES = 256 * 1024
CONNECT_TIMEOUT_SECONDS = 30.0
KEEPALIVE_GRACE_MULTIPLIER = 1.5
# Keep the protocol grace boundary from becoming a race with the socket timer.
KEEPALIVE_SCHEDULING_TOLERANCE_SECONDS = 1.0
EVENT_COMMANDS_WITH_SIMPLE_SUCCESS_RESPONSE = {
    "ATTR_PUSH_EVENT",
    "DEVICE_START_EVENT",
}


def build_noncalibrating_ntp_response(now_ms: int) -> bytes:
    """Build the stock-shaped NTP response without changing feeder time."""

    return json.dumps(
        {
            "cmd": "NTP",
            "ts": now_ms,
            "code": 0,
            "calibrationTag": False,
            # The cloud includes the complete timezone schema even when
            # calibrationTag is false. UTC fixed-offset values keep this
            # bootstrap response deterministic; firmware ignores them when
            # calibration is disabled.
            "timezoneOffsetSeconds": 0,
            "nextDSTOffsetSeconds": 0,
            "nextDSTTransitionTs": 0,
            "secondNextDSTOffsetSeconds": 0,
            "secondNextDSTTransitionTs": 0,
            "timezone": 0,
        },
        separators=(",", ":"),
    ).encode("ascii")


def recv_packet(connection: socket.socket) -> bytes:
    first = connection.recv(1)
    if not first:
        return b""
    length_bytes = bytearray()
    multiplier = 1
    remaining = 0
    for _ in range(4):
        current = connection.recv(1)
        if not current:
            raise ProtocolError("connection closed in MQTT remaining length")
        digit = current[0]
        length_bytes.append(digit)
        remaining += (digit & 0x7F) * multiplier
        if remaining > MAX_PACKET_BYTES:
            raise ProtocolError("MQTT packet exceeds bootstrap limit")
        if not digit & 0x80:
            break
        multiplier *= 128
    else:
        raise ProtocolError("invalid MQTT remaining length")
    body = bytearray()
    while len(body) < remaining:
        chunk = connection.recv(remaining - len(body))
        if not chunk:
            raise ProtocolError("connection closed in MQTT packet body")
        body.extend(chunk)
    return first + length_bytes + body


@dataclasses.dataclass(frozen=True)
class CapturedIdentity:
    client_id: str
    username: str
    password: bytes
    peer_ip: str
    protocol_level: int = 4


class BootstrapSession:
    def __init__(self, connection: socket.socket, peer_ip: str, owner: "BootstrapMqttServer"):
        self.connection = connection
        self.peer_ip = peer_ip
        self.owner = owner
        self.connect: MqttConnect | None = None
        self.connection_generation = 0
        self.subscriptions: set[str] = set()
        self._send_lock = threading.Lock()

    def send(self, packet: bytes) -> None:
        with self._send_lock:
            self.connection.sendall(packet)

    def publish(self, topic: str, payload: bytes, packet_id: int = 1, qos: int = 1) -> None:
        self.send(build_publish(topic, payload, packet_id=packet_id, qos=qos))


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: BootstrapMqttServer = self.server  # type: ignore[assignment]
        peer_ip = str(self.client_address[0])
        server._record_connection_attempt(peer_ip)
        if server.expected_feeder_ip and peer_ip != server.expected_feeder_ip:
            server.report(f"Rejected MQTT connection from unexpected host {peer_ip}")
            return
        # Bound clients that connect but never complete an MQTT CONNECT. This
        # is deliberately separate from the negotiated session keepalive.
        self.request.settimeout(CONNECT_TIMEOUT_SECONDS)
        session = BootstrapSession(self.request, peer_ip, server)
        try:
            first = recv_packet(self.request)
            connection = parse_connect(first)
            if connection.username is None or connection.password is None:
                raise ProtocolError("feeder CONNECT did not include username and password")
            session.connect = connection
            identity = CapturedIdentity(
                connection.client_id,
                connection.username,
                connection.password,
                peer_ip,
                connection.protocol_level,
            )
            session.connection_generation = server._set_identity(identity)
            if connection.keep_alive == 0:
                # MQTT disables protocol-level inactivity handling when the
                # negotiated keepalive is zero. Installer operation deadlines
                # remain independently bounded by bootstrap.py.
                self.request.settimeout(None)
            else:
                self.request.settimeout(
                    connection.keep_alive * KEEPALIVE_GRACE_MULTIPLIER
                    + KEEPALIVE_SCHEDULING_TOLERANCE_SECONDS
                )
            session.send(build_connack())
            protocol_name = "3.1" if connection.protocol_level == 3 else "3.1.1"
            server.report(
                "Accepted feeder MQTT CONNECT and sent CONNACK "
                f"(protocol {protocol_name})"
            )
            server._set_session(session)
            while True:
                packet = recv_packet(self.request)
                if not packet:
                    server.report("Feeder closed the bootstrap MQTT connection")
                    return
                packet_type = packet[0] >> 4
                if packet_type == 3:
                    publication = parse_publish(packet)
                    server._record_publication(session, publication.topic, publication.payload)
                    if publication.qos == 1 and publication.packet_id is not None:
                        session.send(build_puback(publication.packet_id))
                elif packet_type == 8:
                    subscription = parse_subscribe(packet)
                    session.subscriptions.update(topic for topic, _ in subscription.topics)
                    session.send(
                        build_suback(
                            subscription.packet_id,
                            (min(qos, 1) for _, qos in subscription.topics),
                        )
                    )
                    server._subscriptions_changed(session)
                elif packet_type == 12:  # PINGREQ
                    session.send(b"\xd0\x00")
                elif packet_type == 4:  # PUBACK for our OTA command
                    if len(packet) == 4:
                        server._record_puback(
                            session, struct.unpack("!H", packet[2:])[0]
                        )
                elif packet_type == 14:  # DISCONNECT
                    server.report("Feeder sent MQTT DISCONNECT")
                    return
        except (OSError, ProtocolError) as err:
            server._record_session_error(str(err))
            server.report(f"Bootstrap MQTT session ended: {err}")
        finally:
            server._clear_session(session)


class BootstrapMqttServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        expected_feeder_ip: str | None,
        report: Callable[[str], None] = print,
    ) -> None:
        super().__init__(address, _Handler)
        self.expected_feeder_ip = expected_feeder_ip
        self.report = report
        self.identity: CapturedIdentity | None = None
        self.session: BootstrapSession | None = None
        self.identity_ready = threading.Event()
        self.device_start_ready = threading.Event()
        self.ota_subscription_ready = threading.Event()
        self.ota_publish_acked = threading.Event()
        self.ota_result = threading.Event()
        self.reconnected = threading.Event()
        self.post_ota_start_ready = threading.Event()
        self.software_version: str | None = None
        self.ota_error: str | None = None
        self._lock = threading.Lock()
        self._connection_count = 0
        self._ota_sent = False
        self._ota_sent_generation: int | None = None
        self._ota_msg_id: str | None = None
        self._pending_event_responses: list[tuple[BootstrapSession, str, bytes]] = []
        self.publications: list[tuple[str, bytes]] = []
        self.connection_attempts = 0
        self.last_connection_peer: str | None = None
        self.last_session_error: str | None = None

    def _record_connection_attempt(self, peer_ip: str) -> None:
        with self._lock:
            self.connection_attempts += 1
            self.last_connection_peer = peer_ip
            attempt = self.connection_attempts
        self.report(f"Accepted bootstrap TCP connection #{attempt} from {peer_ip}")

    def _record_session_error(self, message: str) -> None:
        with self._lock:
            self.last_session_error = message

    def _set_identity(self, identity: CapturedIdentity) -> int:
        with self._lock:
            if self.identity is None:
                self.identity = identity
                self.identity_ready.set()
                self.report(
                    f"Captured feeder MQTT identity for client {identity.client_id!r}; "
                    "password is stored only in the protected setup output"
                )
            elif identity != self.identity:
                raise ProtocolError("a second MQTT identity attempted to use the bootstrap server")
            self._connection_count += 1
            generation = self._connection_count
            if (
                self._ota_sent_generation is not None
                and generation > self._ota_sent_generation
            ):
                self.reconnected.set()
            return generation

    def _set_session(self, session: BootstrapSession) -> None:
        with self._lock:
            self.session = session

    def _clear_session(self, session: BootstrapSession) -> None:
        with self._lock:
            if self.session is session:
                self.session = None
            self._pending_event_responses = [
                item for item in self._pending_event_responses if item[0] is not session
            ]

    def _subscriptions_changed(self, session: BootstrapSession) -> None:
        if any(topic.endswith("/device/ota/sub") for topic in session.subscriptions):
            self.ota_subscription_ready.set()

        with self._lock:
            ready = [
                (topic, payload)
                for owner, topic, payload in self._pending_event_responses
                if owner is session and topic in session.subscriptions
            ]
            self._pending_event_responses = [
                item
                for item in self._pending_event_responses
                if item[0] is not session or item[1] not in session.subscriptions
            ]
        for topic, payload in ready:
            self._publish_application_response(session, topic, payload)

    def _record_publication(
        self, session: BootstrapSession, topic: str, payload: bytes
    ) -> None:
        with self._lock:
            self.publications.append((topic, payload))
            self.publications[:] = self.publications[-200:]
        try:
            value = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(value, dict):
            return

        command = value.get("cmd")
        if isinstance(command, str):
            self.report(f"Feeder published {command} on {topic}")
        if topic.endswith("/device/ntp/post") and command == "NTP":
            response_topic = topic[:-4] + "sub"
            self._queue_or_publish_application_response(
                session,
                response_topic,
                build_noncalibrating_ntp_response(int(time.time() * 1000)),
            )

        if (
            topic.endswith("/device/event/post")
            and command in EVENT_COMMANDS_WITH_SIMPLE_SUCCESS_RESPONSE
        ):
            msg_id = value.get("msgId")
            if isinstance(msg_id, str) and msg_id:
                response_topic = topic[:-4] + "sub"
                response = json.dumps(
                    {
                        "cmd": command,
                        "ts": int(time.time() * 1000),
                        "msgId": msg_id,
                        "code": 0,
                    },
                    separators=(",", ":"),
                ).encode("ascii")
                self._queue_or_publish_application_response(session, response_topic, response)

        if topic.endswith("/device/event/post") and command == "GRAIN_OUTPUT_EVENT":
            msg_id = value.get("msgId")
            exec_step = value.get("execStep")
            if (
                isinstance(msg_id, str)
                and msg_id
                and isinstance(exec_step, str)
                and exec_step
            ):
                response_topic = topic[:-4] + "sub"
                response = json.dumps(
                    {
                        "cmd": command,
                        "ts": int(time.time() * 1000),
                        "msgId": msg_id,
                        "code": 0,
                        "execStep": exec_step,
                    },
                    separators=(",", ":"),
                ).encode("ascii")
                self._queue_or_publish_application_response(
                    session, response_topic, response
                )

        # GET_FEEDING_PLAN_EVENT asks the server for its authoritative complete
        # schedule. A stock feeder has no State Agent yet, so the bootstrap
        # broker cannot answer it truthfully. In particular, a code-0 response
        # containing an empty plans array replaces the feeder's persisted
        # schedule. Omitting plans is non-destructive, but silence avoids
        # claiming application-level success. The MQTT handler still PUBACKs
        # the feeder's QoS-1 PUBLISH,
        # but intentionally sends no application response. Once bootstrap has
        # completed, the add-on answers from fresh State Agent truth.

        if topic.endswith("/device/event/post") and command == "DEVICE_START_EVENT":
            software_version = value.get("softwareVersion")
            if isinstance(software_version, str) and software_version.startswith("3"):
                self.software_version = software_version
                self.device_start_ready.set()
                if (
                    self._ota_sent_generation is not None
                    and session.connection_generation > self._ota_sent_generation
                ):
                    self.post_ota_start_ready.set()

        if topic.endswith("/device/ota/post") and value.get("msgId") == self._ota_msg_id:
            if command == "OTA_INFORM" and value.get("state") == 1:
                self.ota_error = None
                self.ota_result.set()
            elif command == "OTA_INFORM" and value.get("state") == 2:
                self.ota_error = str(value.get("errorMsg") or "OEM reported OTA failure")
                self.ota_result.set()
            elif command == "OTA_UPGRADE" and value.get("code") not in (None, 0):
                self.ota_error = str(value.get("errorMsg") or f"OEM OTA code {value.get('code')}")
                self.ota_result.set()

    def _queue_or_publish_application_response(
        self, session: BootstrapSession, topic: str, payload: bytes
    ) -> None:
        with self._lock:
            subscribed = topic in session.subscriptions
            if not subscribed:
                self._pending_event_responses.append((session, topic, payload))
        if subscribed:
            self._publish_application_response(session, topic, payload)

    def _publish_application_response(
        self, session: BootstrapSession, topic: str, payload: bytes
    ) -> None:
        # Captured OEM/cloud application responses use QoS 0.
        session.publish(topic, payload, qos=0)

    def _record_puback(self, session: BootstrapSession, packet_id: int) -> None:
        with self._lock:
            if (
                packet_id == 1
                and self._ota_sent_generation is not None
                and session.connection_generation == self._ota_sent_generation
            ):
                self.ota_publish_acked.set()

    def publish_ota(self, payload: bytes) -> str:
        try:
            document = json.loads(payload)
            msg_id = document["msgId"]
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as err:
            raise ValueError("OTA payload is missing a valid msgId") from err
        if not isinstance(msg_id, str) or not msg_id:
            raise ValueError("OTA payload is missing a valid msgId")
        with self._lock:
            session = self.session
            if session is None or session.connect is None:
                raise RuntimeError("feeder is not connected to bootstrap MQTT")
            topics = sorted(
                topic for topic in session.subscriptions if topic.endswith("/device/ota/sub")
            )
            if len(topics) != 1:
                raise RuntimeError("feeder has not established one OTA subscription")
            self._ota_sent = True
            self._ota_sent_generation = session.connection_generation
            self._ota_msg_id = msg_id
            self.ota_error = None
            self.ota_result.clear()
            self.ota_publish_acked.clear()
            self.reconnected.clear()
            self.post_ota_start_ready.clear()
            topic = topics[0]
        session.publish(topic, payload, packet_id=1)
        return topic


def wait_for(event: threading.Event, timeout: float, description: str) -> None:
    if not event.wait(timeout):
        raise TimeoutError(f"timed out waiting for {description}")


def probe_mqtt_credentials(
    host: str,
    port: int,
    identity: CapturedIdentity,
    *,
    timeout: float = 5.0,
) -> None:
    """Verify that the final broker accepts the captured feeder credentials."""

    flags = 0xC2  # username, password, clean session
    protocol_level = getattr(identity, "protocol_level", 4)
    if protocol_level == 3:
        protocol = b"\x00\x06MQIsdp\x03"
    elif protocol_level == 4:
        protocol = b"\x00\x04MQTT\x04"
    else:  # CapturedIdentity should make this unreachable.
        raise ValueError(f"unsupported captured MQTT protocol level {protocol_level}")
    variable = protocol + bytes((flags,)) + b"\x00\x0a"
    payload = (
        encode_utf8(identity.client_id)
        + encode_utf8(identity.username)
        + encode_utf8(identity.password)
    )
    packet = b"\x10" + encode_remaining_length(len(variable) + len(payload)) + variable + payload
    with socket.create_connection((host, port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        connection.sendall(packet)
        response = recv_packet(connection)
    if len(response) != 4 or response[:3] != b"\x20\x02\x00" or response[3] != 0:
        code = response[3] if len(response) == 4 else "malformed"
        raise PermissionError(f"final broker rejected feeder credentials (CONNACK {code})")


def _build_client_connect(client_id: str, username: str, password: str) -> bytes:
    variable = b"\x00\x04MQTT\x04" + bytes((0xC2,)) + b"\x00\x5a"
    payload = encode_utf8(client_id) + encode_utf8(username) + encode_utf8(password)
    return b"\x10" + encode_remaining_length(len(variable) + len(payload)) + variable + payload


def _build_subscribe(topic: str, packet_id: int = 1) -> bytes:
    body = struct.pack("!H", packet_id) + encode_utf8(topic) + b"\x00"
    return b"\x82" + encode_remaining_length(len(body)) + body


def wait_for_mqtt_heartbeat(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    feeder_client_id: str,
    timeout: float,
    on_subscribed: Callable[[], None],
) -> None:
    """Subscribe with the configured backend account and await a live feeder heartbeat."""

    if timeout <= 0:
        raise ValueError("heartbeat timeout must be positive")
    if not feeder_client_id or any(
        character in feeder_client_id for character in ("/", "+", "#", "\x00")
    ):
        raise ValueError("feeder MQTT client ID cannot be used in an exact topic subscription")
    topic = f"dl/PLAF203/{feeder_client_id}/device/heart/post"
    observer_id = "plaf203-bootstrap-" + secrets.token_hex(8)
    deadline = time.monotonic() + timeout
    with socket.create_connection((host, port), timeout=min(timeout, 10.0)) as connection:
        connection.settimeout(max(0.1, deadline - time.monotonic()))
        connection.sendall(_build_client_connect(observer_id, username, password))
        connack = recv_packet(connection)
        if len(connack) != 4 or connack[:3] != b"\x20\x02\x00" or connack[3] != 0:
            code = connack[3] if len(connack) == 4 else "malformed"
            raise PermissionError(
                f"final broker rejected configured backend credentials (CONNACK {code})"
            )

        connection.sendall(_build_subscribe(topic))
        suback = recv_packet(connection)
        if suback != b"\x90\x03\x00\x01\x00":
            raise PermissionError("final broker rejected the feeder heartbeat subscription")

        on_subscribed()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for the feeder heartbeat")
            connection.settimeout(remaining)
            packet = recv_packet(connection)
            if not packet:
                raise ConnectionError("final broker closed the heartbeat observer connection")
            packet_type = packet[0] >> 4
            if packet_type == 3:
                publication = parse_publish(packet)
                if publication.qos == 1 and publication.packet_id is not None:
                    connection.sendall(build_puback(publication.packet_id))
                if publication.topic != topic or packet[0] & 0x01:
                    continue
                try:
                    document = json.loads(publication.payload)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(document, dict) and document.get("cmd") == "HEARTBEAT":
                    return
            elif packet_type == 13:  # PINGRESP
                continue
