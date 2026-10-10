#!/usr/bin/env python3
"""Render runtime configuration without exposing secret option values."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from string import Template
from urllib.parse import urlencode, urlsplit

DEFAULT_STATE_AGENT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/"
    "state-agent-releases/state-agent/latest.json"
)

DEFAULTS = {
    "mqtt_host": "core-mosquitto",
    "mqtt_port": 1883,
    "mqtt_username": "",
    "mqtt_password": "",
    "mqtt_client_id": "petlibro_local_backend",
    "persist_feeder_mqtt": False,
    "feeder_mqtt_host": "",
    "feeder_mqtt_port": 1883,
    "feeder_https_addr": "",
    "petlibro_state_agent_url": "",
    "petlibro_state_agent_token": "",
    "petlibro_state_agent_timeout_seconds": 2,
    "state_agent_updates": {
        "enabled": False,
        "manifest_url": DEFAULT_STATE_AGENT_MANIFEST_URL,
        "check_on_connect": True,
        "check_interval_hours": 24,
    },
    "device_discovery": True,
    "product_filter": "PLAF203",
    "lan_cidr": "192.168.1.0/24",
    "ip_resolve_timeout_seconds": 15,
    "ip_discovery_broadcast_seconds": 2,
    "ip_discovery_max_unicast_per_second": 32,
    "ip_refresh_interval_minutes": 360,
    "ip_retry_backoff_seconds": 60,
    "devices": [],
    "go2rtc_stream_name": "petlibro_feeder",
    "camera_quality": "hd",
    "go2rtc_api_port": 1984,
    "go2rtc_rtsp_port": 8554,
    "go2rtc_webrtc_port": 8555,
    "publish_camera_metadata": True,
    "camera_metadata_topic_prefix": "",
    "camera_metadata_interval_seconds": 30,
    "log_level": "info",
    "verbose_logs": False,
    "enable_debug_dumps": False,
}

ENV_KEYS = {key: key.upper() for key in DEFAULTS}
ENV_KEYS["devices"] = "DEVICES_JSON"
ENV_KEYS["state_agent_updates"] = "STATE_AGENT_UPDATES_JSON"
LEGACY_ENV_KEYS = {
    "device_ip": "DEVICE_IP",
    "product": "PRODUCT",
    "serial": "SERIAL",
    "uid": "UID",
}
BOOL_KEYS = {
    "device_discovery",
    "persist_feeder_mqtt",
    "publish_camera_metadata",
    "verbose_logs",
    "enable_debug_dumps",
}
INT_KEYS = {
    "mqtt_port",
    "feeder_mqtt_port",
    "petlibro_state_agent_timeout_seconds",
    "ip_resolve_timeout_seconds",
    "ip_discovery_broadcast_seconds",
    "ip_discovery_max_unicast_per_second",
    "ip_refresh_interval_minutes",
    "ip_retry_backoff_seconds",
    "go2rtc_api_port",
    "go2rtc_rtsp_port",
    "go2rtc_webrtc_port",
    "camera_metadata_interval_seconds",
}


def parse_bool(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true or false")


def load_options(data_dir: Path) -> dict[str, object]:
    options_path = data_dir / "options.json"
    if options_path.is_file():
        raw = json.loads(options_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("/data/options.json must contain an object")
    else:
        raw = {
            key: os.environ[env_key]
            for key, env_key in ENV_KEYS.items()
            if env_key in os.environ
        }
        raw.update(
            {
                key: os.environ[env_key]
                for key, env_key in LEGACY_ENV_KEYS.items()
                if env_key in os.environ
            }
        )

    options = DEFAULTS | raw
    if isinstance(options["devices"], str):
        try:
            options["devices"] = json.loads(options["devices"])
        except json.JSONDecodeError as err:
            raise ValueError("devices must be a JSON array") from err
    if isinstance(options["state_agent_updates"], str):
        try:
            options["state_agent_updates"] = json.loads(options["state_agent_updates"])
        except json.JSONDecodeError as err:
            raise ValueError("state_agent_updates must be a JSON object") from err
    for key in BOOL_KEYS:
        options[key] = parse_bool(options[key], key)
    for key in INT_KEYS:
        try:
            options[key] = int(options[key])
        except (TypeError, ValueError) as err:
            raise ValueError(f"{key} must be an integer") from err
    options["log_level"] = str(options["log_level"]).strip().lower()
    if options["verbose_logs"] and (
        "log_level" not in raw or str(options["log_level"]).lower() == "info"
    ):
        options["log_level"] = "debug"
    return options


def validate(options: dict[str, object]) -> None:
    if options["product_filter"] != "PLAF203":
        raise ValueError("product_filter must be PLAF203")

    try:
        network = ipaddress.ip_network(str(options["lan_cidr"]), strict=False)
    except ValueError as err:
        raise ValueError("lan_cidr must be a valid IPv4 CIDR") from err
    if network.version != 4 or network.prefixlen < 16:
        raise ValueError("lan_cidr must be an IPv4 network no larger than /16")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", str(options["mqtt_client_id"])):
        raise ValueError("mqtt_client_id contains unsupported characters")
    if options["persist_feeder_mqtt"] and not str(options["feeder_mqtt_host"]).strip():
        raise ValueError(
            "feeder_mqtt_host is required when persist_feeder_mqtt is enabled"
        )
    state_agent_url = str(options["petlibro_state_agent_url"]).strip()
    if state_agent_url:
        candidate_url = state_agent_url.replace("{ip}", "192.0.2.1")
        parsed_state_agent_url = urlsplit(candidate_url)
        if (
            parsed_state_agent_url.scheme not in {"http", "https"}
            or not parsed_state_agent_url.hostname
            or parsed_state_agent_url.username
            or parsed_state_agent_url.password
            or parsed_state_agent_url.query
            or parsed_state_agent_url.fragment
        ):
            raise ValueError(
                "petlibro_state_agent_url must be an HTTP(S) URL without credentials, query, or fragment"
            )
        if "{" in state_agent_url.replace("{ip}", "") or "}" in state_agent_url.replace(
            "{ip}", ""
        ):
            raise ValueError(
                "petlibro_state_agent_url only supports the {ip} placeholder"
            )
    if not 1 <= int(options["petlibro_state_agent_timeout_seconds"]) <= 10:
        raise ValueError(
            "petlibro_state_agent_timeout_seconds must be between 1 and 10"
        )
    update_options = options.get("state_agent_updates")
    if not isinstance(update_options, dict):
        raise ValueError("state_agent_updates must be an object")
    enabled = parse_bool(
        update_options.get("enabled", False), "state_agent_updates.enabled"
    )
    manifest_url = str(update_options.get("manifest_url", "")).strip()
    check_on_connect = parse_bool(
        update_options.get("check_on_connect", True),
        "state_agent_updates.check_on_connect",
    )
    try:
        interval_hours = int(update_options.get("check_interval_hours", 24))
    except (TypeError, ValueError) as err:
        raise ValueError(
            "state_agent_updates.check_interval_hours must be an integer"
        ) from err
    if not 1 <= interval_hours <= 168:
        raise ValueError(
            "state_agent_updates.check_interval_hours must be between 1 and 168"
        )
    if enabled and not manifest_url:
        raise ValueError(
            "state_agent_updates.manifest_url is required when state_agent_updates.enabled is true"
        )
    if manifest_url:
        parsed_manifest_url = urlsplit(manifest_url)
        if (
            parsed_manifest_url.scheme != "https"
            or not parsed_manifest_url.hostname
            or parsed_manifest_url.username
            or parsed_manifest_url.password
            or parsed_manifest_url.query
            or parsed_manifest_url.fragment
        ):
            raise ValueError(
                "state_agent_updates.manifest_url must be an HTTPS URL without credentials, query, or fragment"
            )
    options["state_agent_updates"] = {
        "enabled": enabled,
        "manifest_url": manifest_url,
        "check_on_connect": check_on_connect,
        "check_interval_hours": interval_hours,
    }
    if not 1 <= int(options["ip_resolve_timeout_seconds"]) <= 60:
        raise ValueError("ip_resolve_timeout_seconds must be between 1 and 60")
    if not 1 <= int(options["ip_discovery_broadcast_seconds"]) <= 10:
        raise ValueError("ip_discovery_broadcast_seconds must be between 1 and 10")
    if not 1 <= int(options["ip_discovery_max_unicast_per_second"]) <= 512:
        raise ValueError(
            "ip_discovery_max_unicast_per_second must be between 1 and 512"
        )
    if not 1 <= int(options["ip_refresh_interval_minutes"]) <= 10080:
        raise ValueError("ip_refresh_interval_minutes must be between 1 and 10080")
    if not 5 <= int(options["ip_retry_backoff_seconds"]) <= 3600:
        raise ValueError("ip_retry_backoff_seconds must be between 5 and 3600")

    devices = options["devices"]
    if not isinstance(devices, list):
        raise ValueError("devices must be a list")
    device_keys = set()
    stream_names = set()
    for index, device in enumerate(devices):
        if not isinstance(device, dict):
            raise ValueError(f"devices[{index}] must be an object")
        product = str(device.get("product", "PLAF203"))
        if product != "PLAF203":
            raise ValueError(f"devices[{index}].product must be PLAF203")
        serial = str(device.get("serial", ""))
        if not serial or not re.fullmatch(r"[A-Za-z0-9_-]+", serial):
            raise ValueError(
                f"devices[{index}].serial must contain letters, numbers, underscores, or hyphens"
            )
        device_key = f"{product}/{serial}"
        if device_key in device_keys:
            raise ValueError(f"devices[{index}] duplicates {device_key}")
        device_keys.add(device_key)
        stream_name = stream_name_for(product, serial, str(device.get("name", "")))
        if stream_name in stream_names:
            raise ValueError(f"devices[{index}] has a duplicate stream name")
        stream_names.add(stream_name)
        uid = str(device.get("uid", ""))
        if uid and not re.fullmatch(r"[A-Za-z0-9]{20}", uid):
            raise ValueError(
                f"devices[{index}].uid must contain exactly 20 letters or numbers"
            )
        ip_address = str(device.get("ip_address", ""))
        if ip_address:
            try:
                ipaddress.ip_address(ip_address)
            except ValueError as err:
                raise ValueError(f"devices[{index}].ip_address is invalid") from err

    stream_name = str(options["go2rtc_stream_name"])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", stream_name):
        raise ValueError("go2rtc_stream_name contains unsupported characters")

    if options["camera_quality"] not in {"hd", "sd"}:
        raise ValueError("camera_quality must be hd or sd")
    if options["log_level"] not in {
        "critical",
        "error",
        "warning",
        "info",
        "debug",
        "trace",
    }:
        raise ValueError(
            "log_level must be critical, error, warning, info, debug, or trace"
        )
    if not 5 <= int(options["camera_metadata_interval_seconds"]) <= 300:
        raise ValueError("camera_metadata_interval_seconds must be between 5 and 300")

    topic_prefix = str(options["camera_metadata_topic_prefix"])
    if topic_prefix and not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", topic_prefix
    ):
        raise ValueError(
            "camera_metadata_topic_prefix must contain MQTT path segments without wildcards"
        )

    for key in (
        "mqtt_port",
        "feeder_mqtt_port",
        "go2rtc_api_port",
        "go2rtc_rtsp_port",
        "go2rtc_webrtc_port",
    ):
        if not 1 <= int(options[key]) <= 65535:
            raise ValueError(f"{key} must be between 1 and 65535")


def yaml_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def atomic_write(path: Path, content: str, mode: int = 0o600) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    try:
        if path.read_bytes() == encoded:
            os.chmod(path, mode)
            return False
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=path.parent, delete=False
    ) as handle:
        handle.write(encoded)
        handle.flush()
        os.fchmod(handle.fileno(), mode)
        temporary = Path(handle.name)
    temporary.replace(path)
    return True


def render_template(template_dir: Path, name: str, values: dict[str, str]) -> str:
    source = (template_dir / name).read_text(encoding="utf-8")
    return Template(source).substitute(values)


def stream_name_for(product: str, serial: str, preferred: str = "") -> str:
    if preferred:
        candidate = re.sub(r"[^A-Za-z0-9_-]+", "_", preferred).strip("_")
        if candidate:
            return candidate[:96]
    product_part = re.sub(r"[^A-Za-z0-9]+", "_", product).strip("_").lower()
    serial_part = re.sub(r"[^A-Za-z0-9]+", "_", serial).strip("_").lower()
    return f"petlibro_{product_part}_{serial_part}"[:96]


def status_file_for(stream_name: str) -> str:
    return f"/data/petlibro_camera_status_{stream_name}.json"


def _empty_registry() -> dict[str, object]:
    return {"schema_version": 1, "devices": {}}


def _default_ready() -> dict[str, bool]:
    return {
        "mqtt_discovered": False,
        "uid_discovered": False,
        "ip_resolved": False,
        "stream_configured": False,
        "camera_online": False,
    }


def load_registry(data_dir: Path) -> dict[str, object]:
    path = data_dir / "devices.json"
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_registry()
    if not isinstance(registry, dict) or registry.get("schema_version") != 1:
        raise ValueError("devices.json has an unsupported schema")
    devices = registry.get("devices")
    if not isinstance(devices, dict):
        raise ValueError("devices.json devices must be an object")
    registry["devices"] = {
        key: _sanitize_registry_device(device)
        for key, device in devices.items()
        if isinstance(key, str) and isinstance(device, dict)
    }
    return registry


def _sanitize_registry_device(device: dict[str, object]) -> dict[str, object]:
    allowed = {
        "product",
        "serial",
        "uid",
        "uid_source",
        "ip_address",
        "ip_resolution_method",
        "stream_name",
        "mqtt_topic_root",
        "last_mqtt_seen",
        "last_ip_resolved",
        "last_ip_attempt",
        "manual_override",
        "ready",
    }
    clean = {key: value for key, value in device.items() if key in allowed}
    ready = clean.get("ready")
    clean["ready"] = _default_ready() | (ready if isinstance(ready, dict) else {})
    return clean


def _manual_devices(options: dict[str, object]) -> list[dict[str, object]]:
    devices = [dict(item) for item in options["devices"]]
    legacy_serial = str(options.get("serial", ""))
    legacy_uid = str(options.get("uid", ""))
    legacy_ip = str(options.get("device_ip", ""))
    if legacy_serial and legacy_uid and legacy_ip:
        devices.append(
            {
                "name": str(options.get("go2rtc_stream_name", "petlibro_feeder")),
                "product": str(options.get("product", options["product_filter"])),
                "serial": legacy_serial,
                "uid": legacy_uid,
                "ip_address": legacy_ip,
                "legacy": True,
            }
        )
    return devices


def prepare_registry(options: dict[str, object], data_dir: Path) -> dict[str, object]:
    registry = load_registry(data_dir)
    devices = registry["devices"]
    assert isinstance(devices, dict)
    manual_devices = _manual_devices(options)
    manual_keys = {
        f"{item.get('product', options['product_filter'])}/{item.get('serial', '')}"
        for item in manual_devices
    }
    for key, current in devices.items():
        if isinstance(current, dict) and key not in manual_keys:
            current.pop("manual_override", None)
    for manual in manual_devices:
        product = str(manual.get("product", options["product_filter"]))
        serial = str(manual["serial"])
        key = f"{product}/{serial}"
        current = devices.get(key)
        if not isinstance(current, dict):
            current = {}
        preferred = str(manual.get("name", ""))
        current.update(
            {
                "product": product,
                "serial": serial,
                "stream_name": stream_name_for(product, serial, preferred),
                "mqtt_topic_root": f"dl/{product}/{serial}/device",
            }
        )
        uid = str(manual.get("uid", ""))
        if uid:
            current["uid"] = uid
            current["uid_source"] = "manual_override"
        ip_address = str(manual.get("ip_address", ""))
        if ip_address:
            current["ip_address"] = ip_address
            current["ip_resolution_method"] = "manual_override"
        ready = current.get("ready")
        ready = _default_ready() | (ready if isinstance(ready, dict) else {})
        ready["uid_discovered"] = bool(current.get("uid"))
        ready["ip_resolved"] = bool(current.get("ip_address"))
        ready["stream_configured"] = ready["uid_discovered"] and ready["ip_resolved"]
        current["ready"] = ready
        current["manual_override"] = True
        devices[key] = current
    atomic_write(
        data_dir / "devices.json",
        json.dumps(registry, indent=2, sort_keys=True) + "\n",
    )
    return registry


def _registry_devices(registry: dict[str, object]) -> list[dict[str, object]]:
    devices = registry["devices"]
    assert isinstance(devices, dict)
    return [
        device
        for _key, device in sorted(devices.items())
        if isinstance(device, dict)
        and device.get("product") == "PLAF203"
        and re.fullmatch(r"[A-Za-z0-9_-]+", str(device.get("serial", "")))
    ]


def _valid_ip(value: object) -> bool:
    try:
        ipaddress.ip_address(str(value))
    except ValueError:
        return False
    return True


def render_go2rtc(
    options: dict[str, object], data_dir: Path, template_dir: Path
) -> bool:
    registry = prepare_registry(options, data_dir)
    streams = []
    status_paths = []
    for device in _registry_devices(registry):
        uid = str(device.get("uid", ""))
        ip_address = str(device.get("ip_address", ""))
        serial = str(device.get("serial", ""))
        product = str(device.get("product", options["product_filter"]))
        if not re.fullmatch(r"[A-Za-z0-9]{20}", uid) or not _valid_ip(ip_address):
            continue
        stream_name = stream_name_for(
            product, serial, str(device.get("stream_name", ""))
        )
        status_path = status_file_for(stream_name)
        local_status_path = data_dir / Path(status_path).name
        status_paths.append(local_status_path)

        query = {
            "uid": uid,
            "quality": str(options["camera_quality"]),
        }
        if options["publish_camera_metadata"]:
            query["status_file"] = status_path
        if options["log_level"] in {"debug", "trace"}:
            query["verbose"] = "1"
        if options["log_level"] == "trace":
            query.update(
                trace_packets="1", trace_ack="1", trace_frag="1", trace_frameinfo="1"
            )
        if options["enable_debug_dumps"]:
            query["dump_c2d_plain"] = f"/data/petlibro_c2d_{stream_name}.dat"
            query["dump_d2c_plain"] = f"/data/petlibro_d2c_{stream_name}.dat"
        stream_url = f"petlibro://{ip_address}?{urlencode(query)}"
        streams.append(f"  {stream_name}: {yaml_string(stream_url)}")

    values = {
        "LOG_LEVEL": yaml_string("info"),
        "PETLIBRO_LOG_LEVEL": yaml_string(options["log_level"]),
        "API_LISTEN": yaml_string(f":{options['go2rtc_api_port']}"),
        "RTSP_LISTEN": yaml_string(f":{options['go2rtc_rtsp_port']}"),
        "WEBRTC_LISTEN": yaml_string(f":{options['go2rtc_webrtc_port']}"),
        "STREAMS": "\n".join(streams) if streams else "  {}",
    }
    changed = atomic_write(
        data_dir / "go2rtc.yaml",
        render_template(template_dir, "go2rtc.yaml.tmpl", values),
    )
    if changed:
        for status_path in status_paths:
            if status_path.is_symlink() or status_path.exists():
                status_path.unlink()
    return changed


class SecretRef:
    def __init__(self, name: str):
        self.name = name


def _yaml_entry(name: str, values: dict[str, object]) -> list[str]:
    lines = [f"{name}:"]
    for key, value in values.items():
        if isinstance(value, SecretRef):
            rendered = f"!secret {value.name}"
            lines.append(f"  {key}: {rendered}")
            continue
        if isinstance(value, dict):
            lines.append(f"  {key}:")
            for nested_key, nested_value in value.items():
                if isinstance(nested_value, bool):
                    nested_rendered = "true" if nested_value else "false"
                elif isinstance(nested_value, int):
                    nested_rendered = str(nested_value)
                else:
                    nested_rendered = yaml_string(nested_value)
                lines.append(f"    {nested_key}: {nested_rendered}")
            continue
        elif isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, int):
            rendered = str(value)
        else:
            rendered = yaml_string(value)
        lines.append(f"  {key}: {rendered}")
    return lines


def _camera_topic_prefix(options: dict[str, object], product: str, serial: str) -> str:
    configured = str(options["camera_metadata_topic_prefix"]).rstrip("/")
    if configured:
        return f"{configured}/{product}/{serial}/camera"
    return f"petlibro_local/{product}/{serial}/camera"


def state_agent_url_for(options: dict[str, object], device: dict[str, object]) -> str:
    configured = str(options["petlibro_state_agent_url"]).strip().rstrip("/")
    ip_address = str(device.get("ip_address", "")).strip()
    if configured:
        if "{ip}" in configured:
            return configured.replace("{ip}", ip_address) if ip_address else ""
        return configured
    return f"http://{ip_address}:8765" if ip_address else ""


def render_appdaemon(
    options: dict[str, object], data_dir: Path, template_dir: Path
) -> bool:
    registry = prepare_registry(options, data_dir)
    common = {
        "MQTT_HOST": yaml_string(options["mqtt_host"]),
        "MQTT_PORT": str(options["mqtt_port"]),
        "MQTT_CLIENT_ID": yaml_string(options["mqtt_client_id"]),
    }
    appdaemon_changed = atomic_write(
        data_dir / "appdaemon.yaml",
        render_template(template_dir, "appdaemon.yaml.tmpl", common),
    )

    apps_lines = _yaml_entry(
        "petlibro_discovery",
        {
            "module": "device_discovery",
            "class": "PetlibroDiscovery",
            "enabled": options["device_discovery"],
            "product_filter": options["product_filter"],
            "lan_cidr": options["lan_cidr"],
            "registry_file": "/data/devices.json",
            "resolver_command": "/usr/local/bin/petlibro-resolve",
            "renderer_command": "/usr/local/bin/petlibro-render-config",
            "go2rtc_service": "/run/service/go2rtc",
            "ip_resolve_timeout_seconds": options["ip_resolve_timeout_seconds"],
            "ip_discovery_broadcast_seconds": options["ip_discovery_broadcast_seconds"],
            "ip_discovery_max_unicast_per_second": options[
                "ip_discovery_max_unicast_per_second"
            ],
            "ip_refresh_interval_minutes": options["ip_refresh_interval_minutes"],
            "ip_retry_backoff_seconds": options["ip_retry_backoff_seconds"],
            "petlibro_log_level": options["log_level"],
            "publish_discovery_ip": True,
            "persist_feeder_mqtt": options["persist_feeder_mqtt"],
            "feeder_mqtt_host": options["feeder_mqtt_host"],
            "feeder_mqtt_port": options["feeder_mqtt_port"],
        },
    )
    for device in _registry_devices(registry):
        product = str(device.get("product", options["product_filter"]))
        serial = str(device.get("serial", ""))
        if not serial or product != "PLAF203":
            continue
        stream_name = stream_name_for(
            product, serial, str(device.get("stream_name", ""))
        )
        app_name = f"plaf203_{re.sub(r'[^A-Za-z0-9_]+', '_', serial).lower()}"
        apps_lines.extend(
            _yaml_entry(
                app_name,
                {
                    "module": "plaf203",
                    "class": "Plaf203",
                    "product": product,
                    "serial_number": serial,
                    "device_uid": str(device.get("uid", "")),
                    "persist_feeder_mqtt": options["persist_feeder_mqtt"],
                    "feeder_mqtt_host": options["feeder_mqtt_host"],
                    "feeder_mqtt_port": options["feeder_mqtt_port"],
                    "feeder_https_addr": options["feeder_https_addr"],
                    "petlibro_state_agent_url": state_agent_url_for(options, device),
                    "petlibro_state_agent_token": SecretRef(
                        "petlibro_state_agent_token"
                    ),
                    "petlibro_state_agent_timeout_seconds": options[
                        "petlibro_state_agent_timeout_seconds"
                    ],
                    "state_agent_updates": options["state_agent_updates"],
                    "tutk_p2p_region": "REGION_US",
                    "go2rtc_stream_name": stream_name,
                    "camera_quality": options["camera_quality"],
                    "go2rtc_rtsp_port": options["go2rtc_rtsp_port"],
                    "publish_camera_metadata": options["publish_camera_metadata"],
                    "camera_status_file": status_file_for(stream_name),
                    "camera_metadata_topic_prefix": _camera_topic_prefix(
                        options, product, serial
                    ),
                    "camera_metadata_interval_seconds": options[
                        "camera_metadata_interval_seconds"
                    ],
                    "petlibro_log_level": options["log_level"],
                },
            )
        )
    apps_changed = atomic_write(data_dir / "apps.yaml", "\n".join(apps_lines) + "\n")
    atomic_write(
        data_dir / "appdaemon-secrets.yaml",
        "mqtt_username: "
        + yaml_string(options["mqtt_username"])
        + "\nmqtt_password: "
        + yaml_string(options["mqtt_password"])
        + "\npetlibro_state_agent_token: "
        + yaml_string(options["petlibro_state_agent_token"])
        + "\n",
    )

    app_dir = data_dir / "appdaemon" / "apps"
    app_dir.mkdir(parents=True, exist_ok=True)
    links = {
        app_dir / "apps.yaml": data_dir / "apps.yaml",
        app_dir / "plaf203.py": Path(
            os.environ.get(
                "PETLIBRO_APP_SOURCE", "/opt/petlibro-local/appdaemon/plaf203.py"
            )
        ),
        app_dir / "camera_metadata.py": Path(
            os.environ.get(
                "PETLIBRO_CAMERA_METADATA_SOURCE",
                "/opt/petlibro-local/appdaemon/camera_metadata.py",
            )
        ),
        app_dir / "device_discovery.py": Path(
            os.environ.get(
                "PETLIBRO_DEVICE_DISCOVERY_SOURCE",
                "/opt/petlibro-local/appdaemon/device_discovery.py",
            )
        ),
        app_dir / "dispensing_status.py": Path(
            os.environ.get(
                "PETLIBRO_DISPENSING_STATUS_SOURCE",
                "/opt/petlibro-local/appdaemon/dispensing_status.py",
            )
        ),
        app_dir / "petlibro_logging.py": Path(
            os.environ.get(
                "PETLIBRO_LOGGING_SOURCE",
                "/opt/petlibro-local/appdaemon/petlibro_logging.py",
            )
        ),
        app_dir / "feeder_mqtt_validation.py": Path(
            os.environ.get(
                "PETLIBRO_FEEDER_MQTT_VALIDATION_SOURCE",
                "/opt/petlibro-local/appdaemon/feeder_mqtt_validation.py",
            )
        ),
        app_dir / "state_agent.py": Path(
            os.environ.get(
                "PETLIBRO_STATE_AGENT_SOURCE",
                "/opt/petlibro-local/appdaemon/state_agent.py",
            )
        ),
        app_dir / "state_agent_updates.py": Path(
            os.environ.get(
                "PETLIBRO_STATE_AGENT_UPDATES_SOURCE",
                "/opt/petlibro-local/appdaemon/state_agent_updates.py",
            )
        ),
        app_dir / "state_coordinator.py": Path(
            os.environ.get(
                "PETLIBRO_STATE_COORDINATOR_SOURCE",
                "/opt/petlibro-local/appdaemon/state_coordinator.py",
            )
        ),
        app_dir / "backend.py": Path(
            os.environ.get(
                "PETLIBRO_BACKEND_SOURCE",
                "/opt/petlibro-local/appdaemon/backend.py",
            )
        ),
        app_dir / "commands.py": Path(
            os.environ.get(
                "PETLIBRO_COMMANDS_SOURCE",
                "/opt/petlibro-local/appdaemon/commands.py",
            )
        ),
        app_dir / "feed_plans.py": Path(
            os.environ.get(
                "PETLIBRO_FEED_PLANS_SOURCE",
                "/opt/petlibro-local/appdaemon/feed_plans.py",
            )
        ),
        app_dir / "ha_entities.py": Path(
            os.environ.get(
                "PETLIBRO_HA_ENTITIES_SOURCE",
                "/opt/petlibro-local/appdaemon/ha_entities.py",
            )
        ),
        app_dir / "mqtt_client.py": Path(
            os.environ.get(
                "PETLIBRO_MQTT_CLIENT_SOURCE",
                "/opt/petlibro-local/appdaemon/mqtt_client.py",
            )
        ),
        app_dir / "protocol.py": Path(
            os.environ.get(
                "PETLIBRO_PROTOCOL_SOURCE",
                "/opt/petlibro-local/appdaemon/protocol.py",
            )
        ),
        app_dir / "settings_map.py": Path(
            os.environ.get(
                "PETLIBRO_SETTINGS_MAP_SOURCE",
                "/opt/petlibro-local/appdaemon/settings_map.py",
            )
        ),
        app_dir / "storage.py": Path(
            os.environ.get(
                "PETLIBRO_STORAGE_SOURCE",
                "/opt/petlibro-local/appdaemon/storage.py",
            )
        ),
        app_dir / "telemetry.py": Path(
            os.environ.get(
                "PETLIBRO_TELEMETRY_SOURCE",
                "/opt/petlibro-local/appdaemon/telemetry.py",
            )
        ),
    }
    for link, target in links.items():
        if link.is_symlink() and link.readlink() == target:
            continue
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target)

    marker = data_dir / ".verbose_logs"
    if marker.exists():
        marker.unlink()
    return appdaemon_changed or apps_changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "component",
        choices=("all", "go2rtc", "appdaemon"),
        nargs="?",
        default="all",
    )
    args = parser.parse_args()

    data_dir = Path(os.environ.get("PETLIBRO_DATA_DIR", "/data"))
    template_dir = Path(
        os.environ.get("PETLIBRO_TEMPLATE_DIR", "/opt/petlibro-local/templates")
    )
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        options = load_options(data_dir)
        validate(options)
        if args.component in {"all", "go2rtc"}:
            render_go2rtc(options, data_dir, template_dir)
        if args.component in {"all", "appdaemon"}:
            render_appdaemon(options, data_dir, template_dir)
    except (OSError, ValueError, json.JSONDecodeError, KeyError) as err:
        print(f"Petlibro Local configuration error: {err}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
