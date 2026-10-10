"""Publish a stable, privacy-safe camera runtime contract over MQTT."""

from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
from typing import Callable

from petlibro_logging import PetlibroLogger


SCHEMA_VERSION = 2
VALID_STATUSES = {"idle", "starting", "probing", "online", "offline", "error"}
POLL_INTERVAL_SECONDS = 2


def _format_timestamp(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime.datetime:
    if not isinstance(value, str):
        raise ValueError("last_update must be an ISO-8601 string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("last_update must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _resolution(value: object, field: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object or null")
    width = _nonnegative_int(value.get("width"), f"{field}.width")
    height = _nonnegative_int(value.get("height"), f"{field}.height")
    profile_idc = _nonnegative_int(value.get("profile_idc"), f"{field}.profile_idc")
    level_idc = _nonnegative_int(value.get("level_idc"), f"{field}.level_idc")
    if width == 0 or height == 0 or width > 16384 or height > 16384:
        raise ValueError(f"{field} dimensions are out of range")
    if profile_idc > 255 or level_idc > 255:
        raise ValueError(f"{field} codec identifiers are out of range")
    observed_at = value.get("observed_at")
    _parse_timestamp(observed_at)
    return {
        "width": width,
        "height": height,
        "profile_idc": profile_idc,
        "level_idc": level_idc,
        "observed_at": observed_at,
    }


class CameraMetadataPublisher:
    """Poll go2rtc's atomic status file and publish a retained MQTT contract."""

    def __init__(
        self,
        ad,
        mqtt,
        *,
        enabled: bool,
        product: str,
        serial: str,
        stream_name: str,
        requested_quality: str,
        rtsp_port: int,
        status_file: str,
        topic_prefix: str,
        heartbeat_seconds: int,
        log_level: str = "info",
        now: Callable[[], datetime.datetime] | None = None,
    ):
        self.ad = ad
        self.mqtt = mqtt
        self.enabled = enabled
        self.product = product
        self.serial = serial
        self.stream_name = stream_name
        self.requested_quality = requested_quality
        self.rtsp_port = rtsp_port
        self.status_file = Path(status_file)
        self.topic_prefix = topic_prefix.rstrip("/") or (
            f"petlibro_local/{product}/{serial}/camera"
        )
        self.heartbeat_seconds = heartbeat_seconds
        self.now = now or (lambda: datetime.datetime.now(datetime.timezone.utc))
        self.logger = PetlibroLogger(ad, "petlibro.camera", log_level)
        self.timer = None
        self.last_fingerprint = None
        self.last_availability = None
        self.last_publish_at = None
        self.last_availability_publish_at = None
        self.last_source_problem = None
        self.has_seen_runtime_status = False
        self.last_publish_problem = None
        self.last_payload = None

    @property
    def state_topic(self) -> str:
        return f"{self.topic_prefix}/state"

    @property
    def availability_topic(self) -> str:
        return f"{self.topic_prefix}/availability"

    def start(self) -> None:
        if not self.enabled:
            return
        self.timer = self.ad.run_every(
            self._scheduled_poll, "immediate", POLL_INTERVAL_SECONDS
        )

    def stop(self) -> None:
        if not self.enabled:
            return
        if self.timer is not None:
            self.ad.cancel_timer(self.timer, True)
            self.timer = None
        payload = (
            copy.deepcopy(self.last_payload)
            if self.last_payload
            else self._empty_payload()
        )
        payload["status"] = "offline"
        payload["last_update"] = _format_timestamp(self.now())
        self._publish(payload, force=True)

    def _scheduled_poll(self, _kwargs=None) -> None:
        self.poll()

    def poll(self) -> None:
        if not self.enabled:
            return
        now = self.now().astimezone(datetime.timezone.utc)
        payload = self._load_payload(now)
        self._publish(payload, force=self._heartbeat_due(now))

    def _load_payload(self, now: datetime.datetime) -> dict:
        try:
            raw = json.loads(self.status_file.read_text(encoding="utf-8"))
            payload, source_update = self._sanitize(raw)
            self.has_seen_runtime_status = True
            if now - source_update > datetime.timedelta(
                seconds=3 * self.heartbeat_seconds
            ):
                payload["status"] = "offline"
                payload["last_update"] = _format_timestamp(now)
                self._log_source_problem(
                    "stale", "camera runtime status is stale; publishing offline"
                )
            else:
                self._clear_source_problem()
            return payload
        except FileNotFoundError:
            self._log_source_problem(
                "missing",
                "camera runtime status is not available; publishing offline",
                level="warning" if self.has_seen_runtime_status else "info",
            )
            return self._empty_payload(status="offline", now=now)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as err:
            self._log_source_problem(
                "malformed",
                "camera runtime status is invalid; publishing error "
                f"({type(err).__name__})",
            )
            return self._empty_payload(status="error", now=now)

    def _sanitize(self, raw: object) -> tuple[dict, datetime.datetime]:
        if not isinstance(raw, dict):
            raise ValueError("camera runtime status must be an object")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("unsupported camera runtime status schema")
        status = raw.get("status")
        if status not in VALID_STATUSES:
            raise ValueError("invalid camera runtime status")
        requested_quality = raw.get("requested_quality")
        if requested_quality not in {"hd", "sd"}:
            raise ValueError("invalid requested camera quality")
        if requested_quality != self.requested_quality:
            raise ValueError("camera runtime quality does not match configuration")
        source_update = _parse_timestamp(raw.get("last_update"))

        transition = raw.get("hd_transition")
        if not isinstance(transition, dict) or not isinstance(
            transition.get("observed"), bool
        ):
            raise ValueError("hd_transition must contain an observed boolean")
        elapsed = transition.get("elapsed_ms")
        if elapsed is not None:
            elapsed = _nonnegative_int(elapsed, "hd_transition.elapsed_ms")

        health = raw.get("health")
        if not isinstance(health, dict):
            raise ValueError("health must be an object")

        payload = self._empty_payload()
        payload.update(
            {
                "status": status,
                "probe_resolution": _resolution(
                    raw.get("probe_resolution"), "probe_resolution"
                ),
                "actual_resolution": _resolution(
                    raw.get("actual_resolution"), "actual_resolution"
                ),
                "hd_transition": {
                    "observed": transition["observed"],
                    "elapsed_ms": elapsed,
                },
                "last_update": raw["last_update"],
                "health": {
                    "ffmpeg_errors": None,
                    "gapped_idrs": _nonnegative_int(
                        health.get("gapped_idrs"), "health.gapped_idrs"
                    ),
                    "dropped_frames": _nonnegative_int(
                        health.get("dropped_frames"), "health.dropped_frames"
                    ),
                    "missing_fragments": _nonnegative_int(
                        health.get("missing_fragments"),
                        "health.missing_fragments",
                    ),
                    "ack_pending": _nonnegative_int(
                        health.get("ack_pending"), "health.ack_pending"
                    ),
                    "extended_media_rejected": _nonnegative_int(
                        health.get("extended_media_rejected"),
                        "health.extended_media_rejected",
                    ),
                },
            }
        )
        return payload, source_update

    def _empty_payload(
        self,
        *,
        status: str = "offline",
        now: datetime.datetime | None = None,
    ) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "product": self.product,
            "serial": self.serial,
            "stream_name": self.stream_name,
            "status": status,
            "requested_quality": self.requested_quality,
            "probe_resolution": None,
            "actual_resolution": None,
            "hd_transition": {"observed": False, "elapsed_ms": None},
            "rtsp_path": self.stream_name,
            "rtsp_url_hint": f"rtsp://<backend-host>:{self.rtsp_port}/{self.stream_name}",
            "last_update": _format_timestamp(now or self.now()),
            "health": {
                "ffmpeg_errors": None,
                "gapped_idrs": None,
                "dropped_frames": None,
                "missing_fragments": None,
                "ack_pending": None,
                "extended_media_rejected": None,
            },
        }

    def _heartbeat_due(self, now: datetime.datetime) -> bool:
        return self.last_publish_at is None or (
            now - self.last_publish_at
        ).total_seconds() >= self.heartbeat_seconds

    def _publish(self, payload: dict, *, force: bool) -> None:
        fingerprint_payload = copy.deepcopy(payload)
        fingerprint_payload.pop("last_update", None)
        fingerprint = json.dumps(
            fingerprint_payload, sort_keys=True, separators=(",", ":")
        )
        availability = "online" if payload["status"] == "online" else "offline"
        now = self.now().astimezone(datetime.timezone.utc)

        if force or fingerprint != self.last_fingerprint:
            try:
                self.mqtt.mqtt_publish(
                    self.state_topic,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    namespace="mqtt",
                    qos=1,
                    retain=True,
                )
            except Exception as err:
                self._log_publish_problem(
                    "publish",
                    f"camera metadata MQTT publish failed ({type(err).__name__})",
                )
            else:
                self.last_publish_problem = None
                self.last_fingerprint = fingerprint
                self.last_payload = copy.deepcopy(payload)
                self.last_publish_at = now

        availability_heartbeat_due = self.last_availability_publish_at is None or (
            now - self.last_availability_publish_at
        ).total_seconds() >= self.heartbeat_seconds
        if (
            force
            or availability_heartbeat_due
            or availability != self.last_availability
        ):
            try:
                self.mqtt.mqtt_publish(
                    self.availability_topic,
                    availability,
                    namespace="mqtt",
                    qos=1,
                    retain=True,
                )
            except Exception as err:
                self._log_publish_problem(
                    "publish",
                    f"camera availability MQTT publish failed ({type(err).__name__})",
                )
            else:
                self.last_publish_problem = None
                self.last_availability = availability
                self.last_availability_publish_at = now

    def _log_source_problem(
        self, problem: str, message: str, *, level: str = "warning"
    ) -> None:
        if problem != self.last_source_problem:
            self.logger.log(level, message)
            self.last_source_problem = problem

    def _clear_source_problem(self) -> None:
        self.last_source_problem = None

    def _log_publish_problem(self, problem: str, message: str) -> None:
        if problem != self.last_publish_problem:
            self.logger.error(message)
            self.last_publish_problem = problem
