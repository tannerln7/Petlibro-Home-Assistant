"""Small level-aware, privacy-safe logger for Petlibro AppDaemon apps."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


LOG_LEVELS = ("critical", "error", "warning", "info", "debug", "trace")
_PRIORITY = {name: index for index, name in enumerate(LOG_LEVELS)}
_SECRET_MARKERS = (
    "password",
    "passwd",
    "secret",
    "credential",
    "token",
    "authorization",
    "cameraauthinfo",
    "authkey",
    "apikey",
    "accesskey",
    "privatekey",
    "sessionkey",
    "wifi_ssid",
    "ssid",
)
_IDENTIFIER_MARKERS = (
    "uid",
    "uuid",
    "serial",
    "deviceid",
    "userid",
    "ipaddress",
)
_ENDPOINT_FIELDS = ("topic", "url", "source", "endpoint", "address", "host")
_EXACT_SECRET_FIELDS = ("auth", "authentication")


def _normalize_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return normalized in _EXACT_SECRET_FIELDS or any(
        marker in normalized for marker in _SECRET_MARKERS
    )


def _is_identifier_key(key: object) -> bool:
    normalized = _normalize_key(key)
    return any(marker in normalized for marker in _IDENTIFIER_MARKERS)


def _identifier_segment(segment: str, previous: str = "") -> bool:
    if previous.upper().startswith("PLAF"):
        return True
    return (
        len(segment) >= 8
        and any(char.isdigit() for char in segment)
        and all(char.isalnum() or char in "-_." for char in segment)
    )


def _sanitize_path(value: str) -> str:
    segments = value.split("/")
    for index, segment in enumerate(segments):
        previous = segments[index - 1] if index else ""
        if segment and _identifier_segment(segment, previous):
            segments[index] = "<redacted>"
    return "/".join(segments)


def _sanitize_endpoint(value: object) -> object:
    if not isinstance(value, str):
        return value
    if "://" not in value:
        return _sanitize_path(value)
    try:
        parsed = urlsplit(value)
        port = f":{parsed.port}" if parsed.port is not None else ""
        query = urlencode(
            [
                (
                    query_key,
                    "<redacted>"
                    if _is_sensitive_key(query_key)
                    or _is_identifier_key(query_key)
                    else query_value,
                )
                for query_key, query_value in parse_qsl(
                    parsed.query, keep_blank_values=True
                )
            ]
        )
        return urlunsplit(
            (
                parsed.scheme,
                f"<redacted>{port}" if parsed.netloc else "",
                _sanitize_path(parsed.path),
                query,
                "",
            )
        )
    except (TypeError, ValueError):
        return "<redacted-endpoint>"


def normalize_log_level(value: object, *, legacy_verbose: bool = False) -> str:
    if value is None or str(value).strip() == "":
        return "debug" if legacy_verbose else "info"
    level = str(value).strip().lower()
    if level not in _PRIORITY:
        raise ValueError("log_level must be one of " + ", ".join(LOG_LEVELS))
    return level


def redact(value: object, key: str = "") -> object:
    if _is_sensitive_key(key) or _is_identifier_key(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if _normalize_key(key) in _ENDPOINT_FIELDS:
        return _sanitize_endpoint(value)
    return value


class PetlibroLogger:
    """Filter Petlibro messages without enabling AppDaemon's global DEBUG flood."""

    def __init__(self, ad, component: str, level: object = "info"):
        self.ad = ad
        self.component = component
        self.level = normalize_log_level(level)

    def enabled(self, level: str) -> bool:
        return _PRIORITY[level] <= _PRIORITY[self.level]

    def log(self, level: str, message: str, **fields: object) -> None:
        level = normalize_log_level(level)
        if not self.enabled(level):
            return
        safe_fields = redact(fields)
        suffix = "".join(
            f" {key}={self._render(value)}"
            for key, value in safe_fields.items()
            if value is not None
        )
        rendered = f"[{level.upper()}] [{self.component}]: {message}{suffix}"
        # Keep AppDaemon itself at INFO so its scheduler/state internals do not
        # flood the add-on log. PetlibroLogger performs application filtering.
        appdaemon_level = {
            "critical": "CRITICAL",
            "error": "ERROR",
            "warning": "WARNING",
        }.get(level, "INFO")
        try:
            self.ad.log(rendered, level=appdaemon_level)
        except TypeError:
            # Test doubles and older AppDaemon releases may only accept text.
            self.ad.log(rendered)

    def critical(self, message: str, **fields: object) -> None:
        self.log("critical", message, **fields)

    def error(self, message: str, **fields: object) -> None:
        self.log("error", message, **fields)

    def warning(self, message: str, **fields: object) -> None:
        self.log("warning", message, **fields)

    def info(self, message: str, **fields: object) -> None:
        self.log("info", message, **fields)

    def debug(self, message: str, **fields: object) -> None:
        self.log("debug", message, **fields)

    def trace(self, message: str, **fields: object) -> None:
        self.log("trace", message, **fields)

    @staticmethod
    def _render(value: object) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, separators=(",", ":"))
        return str(value).replace("\n", "\\n")
