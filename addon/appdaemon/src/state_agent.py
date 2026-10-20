"""Read-only client and validated models for the feeder state agent."""

from __future__ import annotations

import enum
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib import error, parse, request

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_UPDATE_UPLOAD_BYTES = 32 * 1024 * 1024 + 16 * 1024 + 64 + 20


class StateAgentError(RuntimeError):
    """Base class for sanitized state-agent failures."""


class StateAgentUnavailable(StateAgentError):
    pass


class StateAgentUnauthorized(StateAgentError):
    pass


class StateAgentBadResponse(StateAgentError):
    pass


class StateAgentTimeout(StateAgentError):
    pass


class SettingClass(enum.Enum):
    """Binary-backed authority class for a decoded state.bin field."""

    PERSISTENT = "persistent"
    EFFECTIVE_CACHED = "effective_cached"
    RUNTIME = "runtime"


@dataclass(frozen=True)
class FeederRevisions:
    core_rev: str
    settings_rev: str
    plans_rev: str
    queue_index_rev: str

    @classmethod
    def from_dict(cls, data: object) -> FeederRevisions:
        obj = _object(data, "revisions")
        values = {
            key: _nonempty_string(obj.get(key), f"revisions.{key}")
            for key in (
                "core_rev",
                "settings_rev",
                "plans_rev",
                "queue_index_rev",
            )
        }
        return cls(**values)

    def to_dict(self) -> dict[str, str]:
        return {
            "core_rev": self.core_rev,
            "settings_rev": self.settings_rev,
            "plans_rev": self.plans_rev,
            "queue_index_rev": self.queue_index_rev,
        }


@dataclass(frozen=True)
class FeederQueue:
    head: int
    tail: int
    pending: bool
    valid: bool = True

    @classmethod
    def from_dict(cls, data: object, name: str = "queue") -> FeederQueue:
        obj = _object(data, name)
        return cls(
            head=_integer(obj.get("head"), f"{name}.head", 0, 255),
            tail=_integer(obj.get("tail"), f"{name}.tail", 0, 255),
            pending=_boolean(obj.get("pending"), f"{name}.pending"),
            valid=(
                _boolean(obj.get("valid"), f"{name}.valid")
                if "valid" in obj
                else True
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "head": self.head,
            "tail": self.tail,
            "pending": self.pending,
            "valid": self.valid,
        }


@dataclass(frozen=True)
class FeederSettings:
    values: Mapping[str, object]
    classes: Mapping[str, SettingClass] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object, classes_data: object = None) -> FeederSettings:
        obj = _object(data, "settings")
        classes_obj = _optional_object(classes_data, "setting_classes")
        classes: dict[str, SettingClass] = {}
        for setting_class in SettingClass:
            names = classes_obj.get(setting_class.value, [])
            if not isinstance(names, list) or not all(
                isinstance(name, str) and name for name in names
            ):
                raise StateAgentBadResponse(
                    f"setting_classes.{setting_class.value} must be an array of strings"
                )
            for name in names:
                if name in classes:
                    raise StateAgentBadResponse(
                        f"setting class is duplicated for {name}"
                    )
                classes[name] = setting_class
        return cls(values=dict(obj), classes=classes)

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def __getitem__(self, key: str) -> object:
        return self.values[key]

    def classification(self, key: str) -> SettingClass | None:
        return self.classes.get(key)

    def is_persistent(self, key: str) -> bool:
        return self.classification(key) is SettingClass.PERSISTENT

    def to_dict(self) -> dict[str, object]:
        return dict(self.values)


@dataclass(frozen=True)
class FeederPlan:
    id: int
    minute: int
    hour_utc: int
    one_shot: bool
    one_shot_raw: int
    time_utc: str
    time_local_candidate: str
    days_raw: tuple[int, ...]
    days: tuple[str, ...]
    portions: int
    enable_audio_raw: int
    audio_times: int
    execution_state: int
    sync_time: int
    skip_end_time: int
    opaque_hex: str | None

    @classmethod
    def from_dict(cls, data: object, index: int) -> FeederPlan:
        name = f"plans.semantic_records[{index}]"
        obj = _object(data, name)
        days_raw_obj = obj.get("days_raw")
        days_obj = obj.get("days")
        if not isinstance(days_raw_obj, list):
            raise StateAgentBadResponse(f"{name}.days_raw must be an array")
        if not isinstance(days_obj, list):
            raise StateAgentBadResponse(f"{name}.days must be an array")
        days_raw = tuple(
            _integer(value, f"{name}.days_raw[{day_index}]", 1, 7)
            for day_index, value in enumerate(days_raw_obj)
        )
        if len(days_raw) != len(set(days_raw)):
            raise StateAgentBadResponse(f"{name}.days_raw contains duplicates")
        days = tuple(
            _nonempty_string(value, f"{name}.days[{day_index}]")
            for day_index, value in enumerate(days_obj)
        )
        return cls(
            id=_integer(obj.get("id"), f"{name}.id", 1, 0xFFFFFFFF),
            minute=_integer(obj.get("minute"), f"{name}.minute", 0, 59),
            hour_utc=_integer(obj.get("hour_utc"), f"{name}.hour_utc", 0, 23),
            one_shot=_boolean(obj.get("one_shot"), f"{name}.one_shot"),
            one_shot_raw=_integer(
                obj.get("one_shot_raw"), f"{name}.one_shot_raw", 0, 255
            ),
            time_utc=_nonempty_string(obj.get("time_utc"), f"{name}.time_utc"),
            time_local_candidate=_nonempty_string(
                obj.get("time_local_candidate"), f"{name}.time_local_candidate"
            ),
            days_raw=days_raw,
            days=days,
            portions=_integer(obj.get("portions"), f"{name}.portions", 0, 255),
            enable_audio_raw=_integer(
                obj.get("enable_audio_raw"), f"{name}.enable_audio_raw", 0, 255
            ),
            audio_times=_integer(
                obj.get("audio_times"),
                f"{name}.audio_times",
                0,
                255,
            ),
            execution_state=_integer(
                obj.get("execution_state"),
                f"{name}.execution_state",
                0,
                0xFFFFFFFF,
            ),
            sync_time=_integer(
                obj.get("sync_time"), f"{name}.sync_time", 0, 0xFFFFFFFFFFFFFFFF
            ),
            skip_end_time=_integer(
                obj.get("skip_end_time"),
                f"{name}.skip_end_time",
                0,
                0xFFFFFFFFFFFFFFFF,
            ),
            opaque_hex=_fixed_hex(obj.get("opaque_hex"), f"{name}.opaque_hex", 10),
        )

    def semantic_fingerprint(self) -> tuple[object, ...]:
        return (
            self.id,
            self.hour_utc,
            self.minute,
            self.one_shot,
            tuple(sorted(self.days_raw)),
            self.portions,
            self.enable_audio_raw,
            self.audio_times,
            self.skip_end_time,
        )

    def stable_fingerprint(self) -> tuple[object, ...]:
        """Persistent/protocol equality excluding runtime and regenerated sync metadata."""

        return self.semantic_fingerprint() + (self.opaque_hex,)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "minute": self.minute,
            "hour_utc": self.hour_utc,
            "one_shot": self.one_shot,
            "one_shot_raw": self.one_shot_raw,
            "time_utc": self.time_utc,
            "time_local_candidate": self.time_local_candidate,
            "days_raw": list(self.days_raw),
            "days": list(self.days),
            "portions": self.portions,
            "enable_audio_raw": self.enable_audio_raw,
            "audio_times": self.audio_times,
            "execution_state": self.execution_state,
            "sync_time": self.sync_time,
            "skip_end_time": self.skip_end_time,
            "opaque_hex": self.opaque_hex,
        }


@dataclass(frozen=True)
class FeederPlans:
    count: int
    plan_bin_size: int
    record_size: int
    even_split: bool
    semantic_records: tuple[FeederPlan, ...]

    @classmethod
    def from_dict(cls, data: object) -> FeederPlans:
        obj = _object(data, "plans")
        if obj.get("ok") is not True:
            raise StateAgentBadResponse("plans.ok must be true")
        records_obj = obj.get("semantic_records")
        if not isinstance(records_obj, list):
            raise StateAgentBadResponse("plans.semantic_records must be an array")
        records = tuple(
            FeederPlan.from_dict(value, index)
            for index, value in enumerate(records_obj)
        )
        count = _integer(obj.get("count"), "plans.count", 0, 255)
        if count != len(records):
            raise StateAgentBadResponse(
                "plans.count does not match plans.semantic_records"
            )
        ids = [plan.id for plan in records]
        if len(ids) != len(set(ids)):
            raise StateAgentBadResponse("plans.semantic_records contains duplicate IDs")
        plan_bin_size = _integer(obj.get("plan_bin_size"), "plans.plan_bin_size", 0)
        record_size = _integer(obj.get("record_size"), "plans.record_size", 0)
        even_split = _boolean(obj.get("even_split"), "plans.even_split")
        if record_size != 47 or plan_bin_size != count * 47 or not even_split:
            raise StateAgentBadResponse("plans must contain exact 47-byte records")
        return cls(
            count=count,
            plan_bin_size=plan_bin_size,
            record_size=record_size,
            even_split=even_split,
            semantic_records=records,
        )

    def by_id(self, plan_id: int) -> FeederPlan | None:
        return next(
            (plan for plan in self.semantic_records if plan.id == plan_id), None
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "count": self.count,
            "plan_bin_size": self.plan_bin_size,
            "record_size": self.record_size,
            "even_split": self.even_split,
            "semantic_records": [plan.to_dict() for plan in self.semantic_records],
        }


@dataclass(frozen=True)
class FeederTruth:
    read_ms: int
    revisions: FeederRevisions
    settings: FeederSettings
    plans: FeederPlans
    queue: FeederQueue
    settings_raw: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: object) -> FeederTruth:
        obj = _successful_object(data)
        return cls(
            read_ms=_integer(obj.get("read_ms"), "read_ms", 0),
            revisions=FeederRevisions.from_dict(obj.get("revisions")),
            settings=FeederSettings.from_dict(
                obj.get("settings"), obj.get("setting_classes")
            ),
            plans=FeederPlans.from_dict(obj.get("plans")),
            queue=FeederQueue.from_dict(obj.get("queue")),
            settings_raw=dict(
                _optional_object(obj.get("settings_raw"), "settings_raw")
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "read_ms": self.read_ms,
            "revisions": self.revisions.to_dict(),
            "settings": self.settings.to_dict(),
            "setting_classes": {
                setting_class.value: sorted(
                    name
                    for name, classified_as in self.settings.classes.items()
                    if classified_as is setting_class
                )
                for setting_class in SettingClass
            },
            "plans": self.plans.to_dict(),
            "queue": self.queue.to_dict(),
            "settings_raw": dict(self.settings_raw),
        }


@dataclass(frozen=True)
class RevisionSnapshot:
    read_ms: int
    revisions: FeederRevisions
    queue: FeederQueue

    @classmethod
    def from_dict(cls, data: object) -> RevisionSnapshot:
        obj = _successful_object(data)
        return cls(
            read_ms=_integer(obj.get("read_ms"), "read_ms", 0),
            revisions=FeederRevisions.from_dict(obj.get("revisions")),
            queue=FeederQueue.from_dict(obj.get("queue")),
        )


@dataclass(frozen=True)
class FeedEventSnapshot:
    queue: FeederQueue
    err_queue: FeederQueue
    events: tuple[Mapping[str, object], ...]
    semantics: str
    error_events: tuple[Mapping[str, object], ...] = ()
    snapshot_consistent: bool = True
    normal_ring_valid: bool = True
    error_ring_valid: bool = True

    @classmethod
    def from_dict(cls, data: object) -> FeedEventSnapshot:
        obj = _successful_object(data)
        events = obj.get("events")
        if not isinstance(events, list) or not all(
            isinstance(event_value, dict) for event_value in events
        ):
            raise StateAgentBadResponse("events must be an array of objects")
        error_events = obj.get("error_events", [])
        if not isinstance(error_events, list) or not all(
            isinstance(event_value, dict) for event_value in error_events
        ):
            raise StateAgentBadResponse("error_events must be an array of objects")
        return cls(
            queue=FeederQueue.from_dict(obj.get("queue")),
            err_queue=FeederQueue.from_dict(obj.get("err_queue"), "err_queue"),
            events=tuple(dict(event_value) for event_value in events),
            semantics=_nonempty_string(obj.get("semantics"), "semantics"),
            error_events=tuple(dict(event_value) for event_value in error_events),
            snapshot_consistent=(
                _boolean(obj.get("snapshot_consistent"), "snapshot_consistent")
                if "snapshot_consistent" in obj
                else True
            ),
            normal_ring_valid=(
                _boolean(obj.get("normal_ring_valid"), "normal_ring_valid")
                if "normal_ring_valid" in obj
                else True
            ),
            error_ring_valid=(
                _boolean(obj.get("error_ring_valid"), "error_ring_valid")
                if "error_ring_valid" in obj
                else True
            ),
        )


@dataclass(frozen=True)
class StateAgentVersion:
    version: str
    api_version: int
    update_api_version: int
    platform: str

    @classmethod
    def from_dict(cls, data: object) -> StateAgentVersion:
        obj = _successful_object(data)
        return cls(
            version=_nonempty_string(obj.get("version"), "version"),
            api_version=_integer(obj.get("api_version"), "api_version", 1),
            update_api_version=_integer(
                obj.get("update_api_version"), "update_api_version", 1
            ),
            platform=_nonempty_string(obj.get("platform"), "platform"),
        )


@dataclass(frozen=True)
class StateAgentUpdateStatus:
    status: str
    reason: str
    candidate_version: str | None
    previous_version: str | None

    @property
    def in_progress(self) -> bool:
        return self.status in {
            "pending",
            "activating",
            "candidate_active",
            "probation_confirmed",
            "rollback_in_progress",
        }

    @property
    def last_error(self) -> str:
        if self.status in {"failed", "rolled_back"}:
            return self.reason
        return ""

    @classmethod
    def from_dict(cls, data: object) -> StateAgentUpdateStatus:
        obj = _successful_object(data)
        return cls(
            status=_nonempty_string(obj.get("status"), "status"),
            reason=_nonempty_string(obj.get("reason"), "reason"),
            candidate_version=_optional_nonempty_string(
                obj.get("candidate_version"), "candidate_version"
            ),
            previous_version=_optional_nonempty_string(
                obj.get("previous_version"), "previous_version"
            ),
        )


@dataclass(frozen=True)
class StateAgentUpdateSubmitResult:
    status: str
    reason: str
    candidate_version: str | None
    previous_version: str | None

    @property
    def accepted(self) -> bool:
        return self.status == "pending"

    @classmethod
    def from_dict(cls, data: object) -> StateAgentUpdateSubmitResult:
        status = StateAgentUpdateStatus.from_dict(data)
        return cls(
            status=status.status,
            reason=status.reason,
            candidate_version=status.candidate_version,
            previous_version=status.previous_version,
        )


@dataclass(frozen=True)
class CompatibilityStatus:
    asset_version: str
    original_ping_path: str | None
    supported: bool
    deployed: bool
    shim_current: bool
    startup_current: bool
    backup_available: bool
    enabled: bool
    active: bool
    reboot_required: bool
    last_reason: str

    @classmethod
    def from_dict(cls, data: object) -> CompatibilityStatus:
        obj = _successful_object(data)
        return cls(
            asset_version=_nonempty_string(obj.get("asset_version"), "asset_version"),
            original_ping_path=_optional_nonempty_string(
                obj.get("original_ping_path"), "original_ping_path"
            ),
            supported=_boolean(obj.get("supported"), "supported"),
            deployed=_boolean(obj.get("deployed"), "deployed"),
            shim_current=_boolean(obj.get("shim_current"), "shim_current"),
            startup_current=_boolean(
                obj.get("startup_current"), "startup_current"
            ),
            backup_available=_boolean(
                obj.get("backup_available"), "backup_available"
            ),
            enabled=_boolean(obj.get("enabled"), "enabled"),
            active=_boolean(obj.get("active"), "active"),
            reboot_required=_boolean(
                obj.get("reboot_required"), "reboot_required"
            ),
            last_reason=_nonempty_string(obj.get("last_reason"), "last_reason"),
        )


class StateAgentClient:
    """Small blocking HTTP client intended to run in an AppDaemon executor."""

    def __init__(self, base_url: str, token: str, timeout_seconds: float = 2.0):
        parsed = parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("state-agent URL must be an HTTP(S) URL with a host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(
                "state-agent URL must not contain credentials, query, or fragment"
            )
        if timeout_seconds <= 0:
            raise ValueError("state-agent timeout must be positive")
        self._base_url = parse.urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "")
        )
        self._token = token
        self._timeout_seconds = float(timeout_seconds)

    def health(self) -> Mapping[str, object]:
        return _successful_object(self._get("/health"))

    def revisions(self) -> RevisionSnapshot:
        return RevisionSnapshot.from_dict(self._get("/v1/rev"))

    def core(self, *, raw: bool = False) -> FeederTruth:
        path = "/v1/core?raw=1" if raw else "/v1/core"
        return FeederTruth.from_dict(self._get(path))

    def feed_events(self) -> FeedEventSnapshot:
        return FeedEventSnapshot.from_dict(self._get("/v1/feed-events"))

    def version(self) -> StateAgentVersion:
        return StateAgentVersion.from_dict(self._get("/v1/version"))

    def update_status(self) -> StateAgentUpdateStatus:
        return StateAgentUpdateStatus.from_dict(self._get("/v1/update-status"))

    def compatibility_status(self) -> CompatibilityStatus:
        return CompatibilityStatus.from_dict(self._get("/v1/compatibility"))

    def compatibility_deploy(self) -> CompatibilityStatus:
        return CompatibilityStatus.from_dict(
            self._post_empty_json("/v1/compatibility/deploy")
        )

    def compatibility_enable(self) -> CompatibilityStatus:
        return CompatibilityStatus.from_dict(
            self._post_empty_json("/v1/compatibility/enable")
        )

    def compatibility_disable(self) -> CompatibilityStatus:
        return CompatibilityStatus.from_dict(
            self._post_empty_json("/v1/compatibility/disable")
        )

    def compatibility_restore_startup(self) -> CompatibilityStatus:
        return CompatibilityStatus.from_dict(
            self._post_empty_json("/v1/compatibility/restore-startup")
        )

    def submit_update(self, payload: bytes) -> StateAgentUpdateSubmitResult:
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("update payload must be bytes")
        if len(payload) == 0:
            raise ValueError("update payload must not be empty")
        if len(payload) > MAX_UPDATE_UPLOAD_BYTES:
            raise ValueError("update payload exceeds the size limit")
        body = self._request(
            "/v1/update",
            method="POST",
            body=bytes(payload),
            content_type="application/octet-stream",
            accept="application/json",
        )
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateAgentBadResponse("state agent returned invalid JSON") from None
        return StateAgentUpdateSubmitResult.from_dict(decoded)

    def _get(self, path: str) -> Mapping[str, object]:
        body = self._request(path, method="GET", accept="application/json")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateAgentBadResponse("state agent returned invalid JSON") from None
        return _object(decoded, "response")

    def _post_empty_json(self, path: str) -> Mapping[str, object]:
        body = self._request(
            path,
            method="POST",
            body=b"",
            content_type="application/json",
            accept="application/json",
        )
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise StateAgentBadResponse("state agent returned invalid JSON") from None
        return _object(decoded, "response")

    def _request(
        self,
        path: str,
        *,
        method: str,
        accept: str,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self._token}",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        req = request.Request(
            self._base_url + path,
            headers=headers,
            data=body,
            method=method,
        )
        try:
            with request.urlopen(req, timeout=self._timeout_seconds) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise StateAgentUnauthorized(
                    "state agent rejected authentication"
                ) from None
            raise StateAgentUnavailable(
                f"state agent returned HTTP status {exc.code}"
            ) from None
        except TimeoutError:
            raise StateAgentTimeout("state agent request timed out") from None
        except (error.URLError, OSError):
            raise StateAgentUnavailable("state agent is unavailable") from None
        if len(payload) > MAX_RESPONSE_BYTES:
            raise StateAgentBadResponse("state agent response exceeds size limit")
        return payload


class UnavailableStateAgentClient:
    """Used while discovery has not resolved a feeder address yet."""

    def _unavailable(self, *_args, **_kwargs):
        raise StateAgentUnavailable("state-agent URL is not configured")

    health = _unavailable
    revisions = _unavailable
    core = _unavailable
    feed_events = _unavailable
    version = _unavailable
    update_status = _unavailable
    submit_update = _unavailable
    compatibility_status = _unavailable
    compatibility_deploy = _unavailable
    compatibility_enable = _unavailable
    compatibility_disable = _unavailable
    compatibility_restore_startup = _unavailable


def _successful_object(data: object) -> Mapping[str, Any]:
    obj = _object(data, "response")
    if obj.get("ok") is not True:
        raise StateAgentBadResponse("state agent response did not report success")
    return obj


def _object(data: object, name: str) -> Mapping[str, Any]:
    if not isinstance(data, dict):
        raise StateAgentBadResponse(f"{name} must be an object")
    return data


def _optional_object(data: object, name: str) -> Mapping[str, Any]:
    if data is None:
        return {}
    return _object(data, name)


def diff_settings_raw(
    before: Mapping[str, object], after: Mapping[str, object]
) -> dict[str, tuple[object | None, object | None]]:
    """Return changed raw setting fields without assigning semantics to them."""

    return {
        key: (before.get(key), after.get(key))
        for key in sorted(set(before) | set(after))
        if key not in before or key not in after or before[key] != after[key]
    }


def _nonempty_string(data: object, name: str) -> str:
    if not isinstance(data, str) or not data:
        raise StateAgentBadResponse(f"{name} must be a non-empty string")
    return data


def _optional_nonempty_string(data: object, name: str) -> str | None:
    if data is None:
        return None
    if not isinstance(data, str) or not data:
        raise StateAgentBadResponse(f"{name} must be null or a non-empty string")
    return data


def _fixed_hex(data: object, name: str, expected_bytes: int) -> str:
    value = _nonempty_string(data, name)
    compact = "".join(value.split()).lower()
    if len(compact) != expected_bytes * 2:
        raise StateAgentBadResponse(f"{name} must encode {expected_bytes} bytes")
    try:
        bytes.fromhex(compact)
    except ValueError:
        raise StateAgentBadResponse(f"{name} must be hexadecimal") from None
    return compact


def _integer(
    data: object, name: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    if isinstance(data, bool) or not isinstance(data, int):
        raise StateAgentBadResponse(f"{name} must be an integer")
    if minimum is not None and data < minimum:
        raise StateAgentBadResponse(f"{name} is below its minimum")
    if maximum is not None and data > maximum:
        raise StateAgentBadResponse(f"{name} exceeds its maximum")
    return data


def _boolean(data: object, name: str) -> bool:
    if not isinstance(data, bool):
        raise StateAgentBadResponse(f"{name} must be a boolean")
    return data
