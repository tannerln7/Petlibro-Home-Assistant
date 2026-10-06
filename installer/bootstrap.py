#!/usr/bin/env python3
"""Guided no-UART bootstrap for an already claimed stock PLAF203 feeder."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import http.server
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE_AGENT_DIR = REPO_ROOT / "state-agent"
DEFAULT_BUILD_DIR = REPO_ROOT / "build" / "bootstrap"
DEFAULT_CONFIG_PATH = DEFAULT_BUILD_DIR / "bootstrap-config.json"
DEFAULT_STATE_AGENT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/"
    "state-agent-releases/state-agent/latest.json"
)
OEM_PROVISIONING_MEMBER_ID = "1"
STATE_AGENT_TOKEN_RE = re.compile(r"^[0-9a-f]{64}$")
DIRECT_URL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


try:
    from .dropbear import build_dropbear
    from .mqtt_server import (
        BootstrapMqttServer,
        probe_mqtt_credentials,
        wait_for,
        wait_for_mqtt_heartbeat,
    )
    from .payload import build_payload, validate_public_key
    from .protocol import build_ota_command, build_set_mqtt_server_frame
    from .tui import clear_screen, select
except ImportError:  # direct execution from installer
    from dropbear import build_dropbear
    from mqtt_server import (  # type: ignore[no-redef]
        BootstrapMqttServer,
        probe_mqtt_credentials,
        wait_for,
        wait_for_mqtt_heartbeat,
    )
    from payload import build_payload, validate_public_key
    from protocol import build_ota_command, build_set_mqtt_server_frame
    from tui import clear_screen, select


class BootstrapPaused(RuntimeError):
    """Raised when the operator safely stops before the OTA is published."""


@dataclass(frozen=True)
class BootstrapConfig:
    feeder_ip: str
    bootstrap_host_ip: str
    home_assistant_ip: str
    feeder_mqtt_host: str
    feeder_mqtt_port: int = 1883
    backend_mqtt_port: int = 1883
    backend_mqtt_username: str = ""
    backend_mqtt_password: str = ""
    bootstrap_mqtt_port: int = 1883
    bootstrap_http_port: int = 18080
    state_agent_dir: str = "state-agent"
    ssh_public_key: str = ""
    target_software_version: str = ""
    output_dir: str = "build/bootstrap/output"

    @classmethod
    def load(cls, path: Path) -> "BootstrapConfig":
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("bootstrap config must be a JSON object")
        # Configurations generated before the member ID became an internal
        # protocol detail, or before both MQTT clients shared the final broker
        # IPv4 address, may contain these fields. They are deliberately ignored.
        document.pop("feeder_mqtt_member_id", None)
        document.pop("backend_mqtt_host", None)
        # This used to be persisted by the wizard. It is now a launch-time
        # option so an old protected configuration remains loadable without
        # silently pinning future runs to its saved update source.
        document.pop("state_agent_manifest_url", None)
        allowed = {field.name for field in fields(cls)}
        unknown = set(document) - allowed
        if unknown:
            raise ValueError(f"unknown bootstrap config fields: {', '.join(sorted(unknown))}")
        config = cls(**document)
        config.validate()
        return config

    def validate(self) -> None:
        for name in ("feeder_ip", "bootstrap_host_ip", "home_assistant_ip"):
            value = ipaddress.ip_address(getattr(self, name))
            if value.version != 4 or value.is_unspecified or value.is_multicast:
                raise ValueError(f"{name} must be a usable IPv4 address")
        # The OEM parser splits on ':' and its downloader supports direct IPv4.
        broker = ipaddress.ip_address(self.feeder_mqtt_host)
        if broker.version != 4:
            raise ValueError("feeder_mqtt_host must be an IPv4 address")
        # Live AF203_FW testing showed that command 0x02 persists other
        # four-digit ports but the MQTT client does not attempt a connection.
        for name in ("feeder_mqtt_port", "bootstrap_mqtt_port"):
            if int(getattr(self, name)) != 1883:
                raise ValueError(f"{name} must be 1883 for AF203_FW")
        for name in ("backend_mqtt_port", "bootstrap_http_port"):
            if not 1 <= int(getattr(self, name)) <= 65535:
                raise ValueError(f"{name} must be between 1 and 65535")
        if not self.backend_mqtt_username or not self.backend_mqtt_password:
            raise ValueError("backend MQTT username and password are required")
        if not self.ssh_public_key.strip():
            raise ValueError("ssh_public_key is required; SSH recovery access cannot be disabled")
        validate_public_key(self.ssh_public_key)
        if self.target_software_version and not re.fullmatch(
            r"3\.[0-9]+\.[0-9]+", self.target_software_version
        ):
            raise ValueError("target_software_version must be an optional 3.x.y value")


def _validate_manifest_url(value: str) -> str:
    value = value.strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "state-agent manifest URL must be HTTPS without credentials, query, or fragment"
        )
    return value


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    reader = getpass.getpass if secret else input
    value = reader(f"{label}{suffix}: ").strip()
    return value or default


def _generate_recovery_key(output_dir: Path) -> str:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        raise RuntimeError("ssh-keygen is required to generate the recovery key")
    key_dir = output_dir / "ssh"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_dir.chmod(0o700)
    private_key = key_dir / "plaf203_recovery_ed25519"
    public_key = private_key.with_suffix(".pub")
    if private_key.exists() != public_key.exists():
        raise RuntimeError(f"incomplete recovery keypair exists in {key_dir}")
    if not private_key.exists():
        subprocess.run(
            [
                executable,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                "plaf203-recovery",
                "-f",
                str(private_key),
            ],
            check=True,
        )
    private_key.chmod(0o600)
    public_key.chmod(0o600)
    print(f"Recovery private key: {private_key}")
    print("Back up this file securely; it is not installed on the feeder.")
    return validate_public_key(public_key.read_text(encoding="ascii"))


def _read_recovery_key(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("an SSH public key or public-key file path is required")
    if candidate.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-nistp256 ")):
        return validate_public_key(candidate)
    key_path = Path(candidate).expanduser()
    if not key_path.is_file():
        raise ValueError(
            "SSH public key input is neither a supported OpenSSH key nor an existing file: "
            f"{key_path}"
        )
    try:
        key = key_path.read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as err:
        raise ValueError(f"SSH public-key file is not readable ASCII: {key_path}") from err
    return validate_public_key(key)


def _choose_recovery_key(output_dir: Path) -> str:
    choice = select(
        "SSH recovery authentication",
        (
            "Generate a dedicated Ed25519 key (recommended)",
            "Use an existing OpenSSH public key (file path or pasted key)",
            "Back",
        ),
    )
    if choice == 2:
        raise KeyboardInterrupt
    if choice == 0:
        return _generate_recovery_key(output_dir)
    return _read_recovery_key(_prompt("SSH public key file path or OpenSSH public key"))


def _repository_path(path: Path, description: str) -> Path:
    expanded = path.expanduser()
    resolved = (expanded if expanded.is_absolute() else REPO_ROOT / expanded).resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as err:
        raise ValueError(f"{description} must stay inside the repository: {REPO_ROOT}") from err
    return resolved


def configure(output: Path) -> BootstrapConfig:
    print("PLAF203 no-UART bootstrap configuration")
    print("All devices must be on the same trusted LAN. Secrets are written mode 0600.\n")
    output = _repository_path(output, "bootstrap configuration")
    output_dir = DEFAULT_BUILD_DIR / "output"
    config = BootstrapConfig(
        feeder_ip=_prompt("Feeder IPv4 address"),
        bootstrap_host_ip=_prompt("This setup computer's LAN IPv4 address"),
        home_assistant_ip=_prompt("Home Assistant IPv4 address"),
        feeder_mqtt_host=_prompt("Final MQTT broker LAN IPv4 address"),
        feeder_mqtt_port=1883,
        backend_mqtt_port=int(_prompt("Add-on MQTT port", "1883")),
        backend_mqtt_username=_prompt("Add-on MQTT username"),
        backend_mqtt_password=_prompt("Add-on MQTT password", secret=True),
        bootstrap_mqtt_port=1883,
        bootstrap_http_port=int(_prompt("Temporary bootstrap HTTP port", "18080")),
        ssh_public_key=_choose_recovery_key(output_dir),
        output_dir="build/bootstrap/output",
    )
    config.validate()
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_private_text(output, json.dumps(asdict(config), indent=2) + "\n")
    print(f"Wrote protected configuration to {output}")
    return config


def _print_config_summary(config: BootstrapConfig, manifest_url: str) -> None:
    print("\nConfiguration summary")
    print(f"  Feeder:           {config.feeder_ip}")
    print(f"  Setup host:       {config.bootstrap_host_ip}")
    print(f"  Home Assistant:   {config.home_assistant_ip}")
    print(f"  Feeder broker:    {config.feeder_mqtt_host}:{config.feeder_mqtt_port}")
    print(f"  Add-on broker:    {config.feeder_mqtt_host}:{config.backend_mqtt_port}")
    print(f"  State Agent OTA:  {manifest_url} (launch option)")
    print("  SSH recovery:     public-key only (configured)")
    print(f"  Output workspace: {config.output_dir}")


def _ensure_arm_elf(path: Path) -> None:
    if not path.is_file() or path.read_bytes()[:4] != b"\x7fELF":
        raise ValueError(f"missing ARM ELF artifact: {path}")
    data = path.read_bytes()[:20]
    if len(data) < 20 or data[4] != 1 or int.from_bytes(data[18:20], "little") != 40:
        raise ValueError(f"artifact is not ELF32 ARM: {path}")


def _write_private_text(path: Path, value: str) -> None:
    temporary = path.with_name(path.name + ".new")
    temporary.unlink(missing_ok=True)
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    temporary.replace(path)


def _load_or_create_state_agent_token(
    config: BootstrapConfig,
    output_dir: Path,
    *,
    rotate: bool = False,
) -> str:
    token_path = output_dir / "state-agent-token.txt"
    binding_path = output_dir / "state-agent-token.binding.json"
    expected_binding = {
        "schema_version": 1,
        "feeder_ip": config.feeder_ip,
        "home_assistant_ip": config.home_assistant_ip,
    }

    if token_path.exists() and not rotate:
        try:
            token_file = token_path.read_text(encoding="ascii")
        except (OSError, UnicodeDecodeError) as err:
            raise ValueError(f"State Agent token is unreadable: {token_path}") from err
        if token_file.endswith("\r\n"):
            token = token_file[:-2]
        elif token_file.endswith("\n"):
            token = token_file[:-1]
        else:
            token = token_file
        if token_file not in {token, token + "\n", token + "\r\n"}:
            raise ValueError(
                "existing State Agent token must contain exactly one token line; rerun with "
                "--rotate-state-agent-token to replace it"
            )
        if STATE_AGENT_TOKEN_RE.fullmatch(token) is None:
            raise ValueError(
                "existing State Agent token is invalid; rerun with "
                "--rotate-state-agent-token to replace it"
            )
        if binding_path.exists():
            try:
                binding = json.loads(binding_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as err:
                raise ValueError(
                    "State Agent token binding is invalid; rerun with "
                    "--rotate-state-agent-token to replace it"
                ) from err
            if binding != expected_binding:
                raise ValueError(
                    "existing State Agent token belongs to a different feeder or Home Assistant "
                    "address; rerun with --rotate-state-agent-token to replace it"
                )
        else:
            _write_private_text(binding_path, json.dumps(expected_binding, indent=2) + "\n")
        token_path.chmod(0o600)
        binding_path.chmod(0o600)
        return token

    token = secrets.token_hex(32)
    _write_private_text(token_path, token + "\n")
    _write_private_text(binding_path, json.dumps(expected_binding, indent=2) + "\n")
    return token


def prepare(
    config: BootstrapConfig,
    manifest_url: str = DEFAULT_STATE_AGENT_MANIFEST_URL,
    *,
    rotate_state_agent_token: bool = False,
) -> tuple[Path, str, Path]:
    manifest_url = _validate_manifest_url(manifest_url)
    output_dir = _repository_path(Path(config.output_dir), "bootstrap output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    state_agent_dir = _repository_path(Path(config.state_agent_dir), "State Agent directory")
    if state_agent_dir == DEFAULT_STATE_AGENT_DIR.resolve():
        if shutil.which("arm-linux-gnueabihf-gcc") is None:
            raise RuntimeError(
                "arm-linux-gnueabihf-gcc is required to build the State Agent release"
            )
        compiler_tmp = DEFAULT_BUILD_DIR / "cache" / "tmp"
        compiler_tmp.mkdir(parents=True, exist_ok=True)
        build_env = os.environ | {"TMPDIR": str(compiler_tmp)}
        print("Building the static ARM State Agent release.")
        subprocess.run(
            ["make", "-C", str(state_agent_dir), "arm-release"],
            check=True,
            env=build_env,
        )
    for name in ("plaf203-state-agent", "plaf203-update-fs"):
        _ensure_arm_elf(state_agent_dir / name)
    print("Acquiring and building pinned static ARM Dropbear (cached after the first build).")
    dropbear_dir = build_dropbear()
    token = _load_or_create_state_agent_token(
        config,
        output_dir,
        rotate=rotate_state_agent_token,
    )
    payload = output_dir / "AF203_FW.bootstrap"
    build_payload(
        output=payload,
        state_agent_dir=state_agent_dir,
        home_assistant_ip=config.home_assistant_ip,
        state_agent_token=token,
        ssh_public_key=config.ssh_public_key,
        dropbear_dir=dropbear_dir,
    )
    options_patch = {
        "mqtt_host": config.feeder_mqtt_host,
        "mqtt_port": config.backend_mqtt_port,
        "mqtt_username": config.backend_mqtt_username,
        "mqtt_password": config.backend_mqtt_password,
        "persist_feeder_mqtt": True,
        "feeder_mqtt_host": config.feeder_mqtt_host,
        "feeder_mqtt_port": config.feeder_mqtt_port,
        "petlibro_state_agent_url": "http://{ip}:8765",
        "petlibro_state_agent_token": token,
        "state_agent_updates": {
            "enabled": True,
            "manifest_url": manifest_url,
            "check_on_connect": True,
            "check_interval_hours": 24,
        },
    }
    patch_path = output_dir / "addon-options.patch.json"
    _write_private_text(patch_path, json.dumps(options_patch, indent=2) + "\n")
    return payload, token, patch_path


class _ArtifactServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], artifact: Path, route: str, feeder_ip: str):
        self.artifact = artifact
        self.route = route
        self.feeder_ip = feeder_ip
        self.downloaded = threading.Event()
        super().__init__(address, _ArtifactHandler)


class _ArtifactHandler(http.server.BaseHTTPRequestHandler):
    server: _ArtifactServer

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.client_address[0] != self.server.feeder_ip or self.path != self.server.route:
            self.send_error(404)
            return
        size = self.server.artifact.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Connection", "close")
        self.end_headers()
        with self.server.artifact.open("rb") as source:
            while chunk := source.read(64 * 1024):
                self.wfile.write(chunk)
        self.server.downloaded.set()

    def log_message(self, message: str, *args: Any) -> None:
        print("Bootstrap HTTP: " + (message % args))


def _send_provisioning(config: BootstrapConfig, host: str, port: int) -> None:
    frame = build_set_mqtt_server_frame(host, port, OEM_PROVISIONING_MEMBER_ID)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        # This command persists configuration and signals a live MQTT
        # reconfiguration. Repeating it is not an idempotent reliability aid:
        # every accepted datagram posts another firmware reconfiguration.
        udp.sendto(frame, (config.feeder_ip, 17484))


def _write_identity(config: BootstrapConfig, identity: Any) -> Path:
    output = (
        _repository_path(Path(config.output_dir), "bootstrap output directory")
        / "feeder-mqtt-credentials.json"
    )
    document = {
        "client_id": identity.client_id,
        "username": identity.username,
        "password": identity.password.decode("utf-8", errors="strict"),
    }
    _write_private_text(output, json.dumps(document, indent=2) + "\n")
    return output


def _probe_state_agent(config: BootstrapConfig, token: str) -> None:
    request = urllib.request.Request(
        f"http://{config.feeder_ip}:8765/health",
        headers={"Authorization": f"Bearer {token}", "Connection": "close"},
    )
    try:
        # Never forward the State Agent bearer token through an ambient HTTP
        # proxy. The configured endpoint is a direct feeder LAN address.
        with DIRECT_URL_OPENER.open(request, timeout=5) as response:
            document = json.load(response)
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as err:
        raise RuntimeError(f"State Agent health check failed: {err}") from err
    if response.status != 200 or document.get("ok") is not True:
        raise RuntimeError("State Agent health check did not return ok=true")


def _select_target_version(config: BootstrapConfig, detected: str | None) -> str:
    if detected is None or not re.fullmatch(r"3\.[0-9]+\.[0-9]+", detected):
        raise RuntimeError("the feeder did not publish a valid 3.x DEVICE_START_EVENT version")
    return config.target_software_version or detected


def _wait_for_feeder_identity(
    mqtt: BootstrapMqttServer, config: BootstrapConfig, timeout: float = 180
) -> None:
    try:
        wait_for(mqtt.identity_ready, timeout, "the feeder MQTT CONNECT")
    except TimeoutError as err:
        if mqtt.connection_attempts == 0:
            detail = (
                "The temporary broker received no TCP connection. Verify that "
                f"{config.bootstrap_host_ip}:{config.bootstrap_mqtt_port} is this setup "
                "computer's feeder-reachable LAN address, allow that TCP port through the "
                "host firewall, and confirm the feeder still has the configured IP."
            )
        else:
            detail = (
                f"The temporary broker received {mqtt.connection_attempts} TCP connection "
                f"attempt(s); the last peer was {mqtt.last_connection_peer}."
            )
            if mqtt.last_session_error:
                detail += f" Last MQTT error: {mqtt.last_session_error}."
            if mqtt.last_connection_peer != config.feeder_ip:
                detail += (
                    f" The configured feeder IP is {config.feeder_ip}; update the saved "
                    "configuration if DHCP changed it."
                )
        raise TimeoutError(f"timed out waiting for the feeder MQTT CONNECT. {detail}") from err


def _acquire_feeder_identity(
    mqtt: BootstrapMqttServer,
    config: BootstrapConfig,
    *,
    reconnect_grace: float = 10,
) -> None:
    print(
        "Waiting briefly for a feeder already configured for the temporary "
        "broker to reconnect."
    )
    if mqtt.identity_ready.wait(reconnect_grace):
        print("Feeder reconnected without provisioning; skipping OEM command 0x02.")
        return

    print("Redirecting the feeder to the temporary bootstrap broker with OEM command 0x02.")
    _send_provisioning(config, config.bootstrap_host_ip, config.bootstrap_mqtt_port)
    print("If it does not connect promptly, reboot the feeder once; do not factory-reset it.")
    _wait_for_feeder_identity(mqtt, config)


def _wait_for_final_broker(
    config: BootstrapConfig, identity: Any, identity_path: Path
) -> None:
    while True:
        try:
            probe_mqtt_credentials(config.feeder_mqtt_host, config.feeder_mqtt_port, identity)
            print("Final broker accepted the feeder credentials.")
            return
        except (OSError, PermissionError) as err:
            print("\nThe final broker does not yet accept the captured feeder account.")
            print(f"Create the exact account shown in {identity_path}, then retry.\nReason: {err}")
            choice = select(
                "Broker account setup",
                (
                    "Retry the final broker now",
                    "Stop safely and resume the installer later",
                ),
            )
            if choice == 1:
                raise BootstrapPaused(
                    "bootstrap paused before OTA; configure the feeder broker account and resume"
                ) from err


def _restore_final_broker_and_verify_heartbeat(
    config: BootstrapConfig,
    identity: Any,
    *,
    timeout: float = 90,
) -> bool:
    provisioning_attempted = False
    provisioning_completed = False

    def restore_endpoint() -> None:
        nonlocal provisioning_attempted, provisioning_completed
        provisioning_attempted = True
        _send_provisioning(config, config.feeder_mqtt_host, config.feeder_mqtt_port)
        provisioning_completed = True
        print("Restored the OEM MQTT endpoint to the final broker.")

    try:
        wait_for_mqtt_heartbeat(
            host=config.feeder_mqtt_host,
            port=config.backend_mqtt_port,
            username=config.backend_mqtt_username,
            password=config.backend_mqtt_password,
            feeder_client_id=identity.client_id,
            timeout=timeout,
            on_subscribed=restore_endpoint,
        )
    except (OSError, PermissionError, TimeoutError, ValueError) as err:
        if provisioning_attempted and not provisioning_completed:
            raise RuntimeError("failed to restore the feeder MQTT endpoint") from err
        if not provisioning_attempted:
            restore_endpoint()
        print(
            "WARNING: The installer could not verify a feeder heartbeat through the "
            f"configured broker account ({type(err).__name__}). The installation will "
            "finish; verify the feeder connection and entities in Home Assistant."
        )
        return False

    print("Verified the feeder heartbeat through the configured final broker account.")
    return True


def run(
    config: BootstrapConfig,
    prepared: tuple[Path, str, Path] | None = None,
    manifest_url: str = DEFAULT_STATE_AGENT_MANIFEST_URL,
    *,
    rotate_state_agent_token: bool = False,
) -> None:
    payload, token, options_patch = prepared or prepare(
        config,
        manifest_url,
        rotate_state_agent_token=rotate_state_agent_token,
    )
    artifact_md5 = hashlib.md5(payload.read_bytes()).hexdigest()  # OEM protocol requirement
    route = "/" + secrets.token_urlsafe(18)
    artifact_url = f"http://{config.bootstrap_host_ip}:{config.bootstrap_http_port}{route}"

    mqtt = BootstrapMqttServer(
        (config.bootstrap_host_ip, config.bootstrap_mqtt_port),
        expected_feeder_ip=config.feeder_ip,
    )
    httpd = _ArtifactServer(
        (config.bootstrap_host_ip, config.bootstrap_http_port), payload, route, config.feeder_ip
    )
    mqtt_thread = threading.Thread(target=mqtt.serve_forever, daemon=True)
    http_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    mqtt_thread.start()
    http_thread.start()
    try:
        _acquire_feeder_identity(mqtt, config)
        assert mqtt.identity is not None
        identity_path = _write_identity(config, mqtt.identity)
        print(f"Captured credentials in protected file {identity_path}")
        wait_for(mqtt.device_start_ready, 180, "the feeder DEVICE_START_EVENT")
        target_version = _select_target_version(config, mqtt.software_version)
        print(f"Observed stock firmware {mqtt.software_version}; OTA family value is {target_version}.")
        _wait_for_final_broker(config, mqtt.identity, identity_path)

        wait_for(mqtt.ota_subscription_ready, 60, "the feeder OTA subscription")
        ota_payload = build_ota_command(
            msg_id="bootstrap-" + secrets.token_hex(8),
            artifact_url=artifact_url,
            artifact_md5=artifact_md5,
            target_version=target_version,
        )
        topic = mqtt.publish_ota(ota_payload)
        print(f"Published correlated OTA bootstrap command on {topic}")
        wait_for(mqtt.ota_publish_acked, 30, "the feeder MQTT OTA PUBACK")
        wait_for(httpd.downloaded, 60, "the feeder payload download")
        wait_for(mqtt.ota_result, 180, "OEM OTA terminal report")
        if mqtt.ota_error is not None:
            raise RuntimeError(f"OEM rejected or failed the OTA bootstrap: {mqtt.ota_error}")
        print("OEM accepted the payload. Waiting for the production OTA1 startup event.")
        wait_for(
            mqtt.post_ota_start_ready,
            240,
            "restored OEM firmware to publish DEVICE_START_EVENT after bootstrap",
        )

        _restore_final_broker_and_verify_heartbeat(config, mqtt.identity)
        if config.bootstrap_host_ip == config.home_assistant_ip:
            deadline = time.monotonic() + 60
            while True:
                try:
                    _probe_state_agent(config, token)
                    print("Authenticated State Agent health check passed.")
                    break
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
        else:
            print(
                "Skipped the State Agent HTTP check because this setup host is not the configured "
                "Home Assistant allowlisted IP. Verify /health from Home Assistant."
            )
        complete = (
            _repository_path(Path(config.output_dir), "bootstrap output directory")
            / "BOOTSTRAP_COMPLETE"
        )
        _write_private_text(
            complete,
            "Bootstrap payload installed; OEM endpoint restoration sent.\n"
            f"Apply the generated add-on options from {options_patch}.\n",
        )
        print(f"Bootstrap transaction complete. Apply {options_patch} in the add-on configuration.")
    finally:
        mqtt.shutdown()
        httpd.shutdown()
        mqtt.server_close()
        httpd.server_close()


def wizard(
    config_path: Path,
    manifest_url: str = DEFAULT_STATE_AGENT_MANIFEST_URL,
    *,
    rotate_state_agent_token: bool = False,
) -> int:
    config_path = _repository_path(config_path, "bootstrap configuration")
    manifest_url = _validate_manifest_url(manifest_url)
    print("\nPLAF203 Local guided installer")
    print("This session configures, builds, bootstraps, and verifies one feeder.\n")
    if config_path.exists():
        choice = select(
            f"Protected configuration already exists at {config_path}",
            (
                "Continue with the existing configuration",
                "Replace it with new configuration",
                "Quit",
            ),
        )
        if choice == 2:
            return 0
        if choice == 0:
            try:
                config = BootstrapConfig.load(config_path)
            except (TypeError, ValueError) as err:
                print(f"\nThe saved configuration is not safe to use: {err}")
                print("Replace it before any provisioning packet is sent.\n")
                config = configure(config_path)
        else:
            config = configure(config_path)
    else:
        config = configure(config_path)

    clear_screen()
    while True:
        _print_config_summary(config, manifest_url)
        choice = select(
            "Review the configuration before building",
            (
                "Continue to artifact preparation",
                "Replace the configuration",
                "Quit",
            ),
        )
        if choice == 0:
            break
        if choice == 2:
            return 0
        config = configure(config_path)
        clear_screen()

    clear_screen()
    print("\nPreparing verified ARM artifacts and the one-time OTA payload.")
    prepared = prepare(
        config,
        manifest_url,
        rotate_state_agent_token=rotate_state_agent_token,
    )
    clear_screen()
    choice = select(
        "Preparation complete. The next step redirects and updates the feeder.",
        (
            "Start the guarded feeder bootstrap",
            "Stop here; keep the prepared files for later",
        ),
    )
    if choice == 1:
        return 0
    clear_screen()
    run(config, prepared, manifest_url)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="./installer/install.sh", description=__doc__)
    parser.add_argument(
        "--state-agent-manifest-url",
        default=DEFAULT_STATE_AGENT_MANIFEST_URL,
        metavar="HTTPS_URL",
        help="signed State Agent latest.json feed used in generated add-on options",
    )
    parser.add_argument(
        "--rotate-state-agent-token",
        action="store_true",
        help="replace the protected State Agent token and bind it to this feeder setup",
    )
    subparsers = parser.add_subparsers(dest="command")
    wizard_parser = subparsers.add_parser("wizard", help="run the guided end-to-end installer")
    wizard_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    configure_parser = subparsers.add_parser("configure", help="interactively create a protected config")
    configure_parser.add_argument("--output", type=Path, default=DEFAULT_CONFIG_PATH)
    command_help = {
        "prepare": "validate inputs and build the payload without contacting the feeder",
        "run": "perform the guarded feeder bootstrap transaction",
    }
    for command in ("prepare", "run"):
        child = subparsers.add_parser(command, help=command_help[command])
        child.add_argument("--config", type=Path, required=True)
        if command == "run":
            child.add_argument(
                "--yes",
                action="store_true",
                help="confirm the recoverability warning and perform the OEM OTA transaction",
            )
    args = parser.parse_args()
    try:
        manifest_url = _validate_manifest_url(args.state_agent_manifest_url)
    except ValueError as err:
        parser.error(str(err))
    if args.command is None:
        return wizard(
            DEFAULT_CONFIG_PATH,
            manifest_url,
            rotate_state_agent_token=args.rotate_state_agent_token,
        )
    if args.command == "wizard":
        return wizard(
            args.config.resolve(),
            manifest_url,
            rotate_state_agent_token=args.rotate_state_agent_token,
        )
    if args.command == "configure":
        configure(args.output.resolve())
        return 0
    config = BootstrapConfig.load(args.config.resolve())
    if args.command == "prepare":
        payload, _, patch = prepare(
            config,
            manifest_url,
            rotate_state_agent_token=args.rotate_state_agent_token,
        )
        print(f"Prepared {payload}")
        print(f"Prepared protected add-on options patch {patch}")
        return 0
    if not args.yes:
        answer = input(
            "This experimental operation changes feeder startup and both OTA slots. "
            "Continue only on a recoverable lab-tested path [y/N]? "
        ).strip().lower()
        if answer not in {"y", "yes"}:
            print("Cancelled without sending provisioning or OTA commands.")
            return 2
    run(
        config,
        manifest_url=manifest_url,
        rotate_state_agent_token=args.rotate_state_agent_token,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BootstrapPaused as err:
        print(f"\n{err}")
        raise SystemExit(3) from None
    except KeyboardInterrupt:
        print("\nInstaller cancelled.")
        raise SystemExit(130) from None
