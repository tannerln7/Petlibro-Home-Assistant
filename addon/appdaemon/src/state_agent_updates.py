"""Signed OTA release verification and upload orchestration for the state agent."""

from __future__ import annotations

import hashlib
import json
import re
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from state_agent import (
    StateAgentBadResponse,
    StateAgentError,
    StateAgentTimeout,
    StateAgentUnavailable,
    StateAgentUpdateStatus,
)

MANIFEST_SCHEMA_VERSION = 1
MANIFEST_PRODUCT = "plaf203-state-agent"
MANIFEST_CHANNEL = "stable"
MANIFEST_API_VERSION = 1
MANIFEST_UPDATE_API_VERSION = 1
MANIFEST_PLATFORM = "linux-armv7-eabihf"

MANIFEST_BYTES_LIMIT = 16 * 1024
SIGNATURE_BYTES_LIMIT = 256
DEFAULT_HTTPS_TIMEOUT_SECONDS = 10.0
ARTIFACT_ABSOLUTE_BYTES_LIMIT = 32 * 1024 * 1024
ARTIFACT_DOWNLOAD_SLACK_BYTES = 64 * 1024

UPDATE_TOPIC = "state_agent/update"
CHECK_CONNECT_THROTTLE_SECONDS = 10 * 60
STATUS_POLL_INTERVAL_SECONDS = 5
STATUS_POLL_MAX_ATTEMPTS = 24

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class UpdateValidationError(RuntimeError):
    """Raised when OTA release metadata or content fails validation."""


class _UpdateSubmissionOutcomeUnknown(RuntimeError):
    """The update request may have reached the State Agent."""


class _RejectRedirectHandler(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UpdateValidationError("HTTPS release downloads must not redirect")


def _semver_components(value: str) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        raise UpdateValidationError("manifest version must be SemVer")
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease):
        raise UpdateValidationError("numeric SemVer prerelease identifiers must not have leading zeros")
    return (
        (int(match.group(1)), int(match.group(2)), int(match.group(3))),
        prerelease,
    )


def compare_semver(left: str, right: str) -> int:
    left_core, left_pre = _semver_components(left)
    right_core, right_pre = _semver_components(right)
    if left_core != right_core:
        return -1 if left_core < right_core else 1
    if not left_pre and not right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    for left_part, right_part in zip(left_pre, right_pre):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_part) < int(right_part) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left_pre) == len(right_pre):
        return 0
    return -1 if len(left_pre) < len(right_pre) else 1


@dataclass(frozen=True)
class StateAgentUpdateOptions:
    enabled: bool
    manifest_url: str
    check_on_connect: bool = True
    check_interval_hours: int = 24

    @classmethod
    def from_mapping(cls, raw: object) -> StateAgentUpdateOptions:
        if raw is None:
            return cls(enabled=False, manifest_url="")
        if not isinstance(raw, dict):
            raise ValueError("state_agent_updates must be an object")
        enabled = bool(raw.get("enabled", False))
        manifest_url = str(raw.get("manifest_url", "")).strip()
        check_on_connect = bool(raw.get("check_on_connect", True))
        try:
            interval = int(raw.get("check_interval_hours", 24))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "state_agent_updates.check_interval_hours must be an integer"
            ) from exc
        if not 1 <= interval <= 168:
            raise ValueError(
                "state_agent_updates.check_interval_hours must be between 1 and 168"
            )
        if enabled and not manifest_url:
            raise ValueError(
                "state_agent_updates.manifest_url is required when updates are enabled"
            )
        if manifest_url:
            parsed_manifest_url = _validate_https_url(
                manifest_url,
                field_name="state_agent_updates.manifest_url",
                immutable_only=False,
            )
            if parsed_manifest_url.query:
                raise ValueError(
                    "state_agent_updates.manifest_url must not include query parameters"
                )
        return cls(
            enabled=enabled,
            manifest_url=manifest_url,
            check_on_connect=check_on_connect,
            check_interval_hours=interval,
        )


@dataclass(frozen=True)
class ReleaseArtifact:
    url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class SignedReleaseManifest:
    schema_version: int
    product: str
    channel: str
    version: str
    api_version: int
    update_api_version: int
    platform: str
    artifact: ReleaseArtifact
    release_url: str

    @classmethod
    def from_bytes(cls, data: bytes) -> SignedReleaseManifest:
        try:
            decoded = json.loads(
                data.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except UnicodeDecodeError as exc:
            raise UpdateValidationError("manifest must be UTF-8 JSON") from exc
        except json.JSONDecodeError as exc:
            raise UpdateValidationError("manifest JSON is invalid") from exc
        if not isinstance(decoded, dict):
            raise UpdateValidationError("manifest must be a JSON object")
        expected_keys = {
            "schema_version",
            "product",
            "channel",
            "version",
            "api_version",
            "update_api_version",
            "platform",
            "artifact",
            "release_url",
        }
        if set(decoded) != expected_keys:
            raise UpdateValidationError("manifest fields are invalid")

        schema_version = _json_int(decoded, "schema_version")
        product = _json_str(decoded, "product")
        channel = _json_str(decoded, "channel")
        api_version = _json_int(decoded, "api_version")
        update_api_version = _json_int(decoded, "update_api_version")
        platform = _json_str(decoded, "platform")
        version = _json_str(decoded, "version")
        artifact_obj = _json_object(decoded, "artifact")
        if set(artifact_obj) != {"url", "sha256", "size"}:
            raise UpdateValidationError("artifact fields are invalid")
        artifact_url = _json_str(artifact_obj, "url")
        artifact_sha256 = _json_str(artifact_obj, "sha256")
        artifact_size = _json_int(artifact_obj, "size")
        release_url = _json_str(decoded, "release_url")

        if schema_version != MANIFEST_SCHEMA_VERSION:
            raise UpdateValidationError("unsupported manifest schema_version")
        if product != MANIFEST_PRODUCT:
            raise UpdateValidationError("unsupported manifest product")
        if channel != MANIFEST_CHANNEL:
            raise UpdateValidationError("unsupported manifest channel")
        if api_version != MANIFEST_API_VERSION:
            raise UpdateValidationError("unsupported manifest api_version")
        if update_api_version != MANIFEST_UPDATE_API_VERSION:
            raise UpdateValidationError("unsupported manifest update_api_version")
        if platform != MANIFEST_PLATFORM:
            raise UpdateValidationError("unsupported manifest platform")
        _semver_components(version)
        _validate_https_url(
            artifact_url, field_name="artifact_url", immutable_only=True
        )
        _validate_https_url(release_url, field_name="release_url", immutable_only=False)
        if not SHA256_RE.fullmatch(artifact_sha256):
            raise UpdateValidationError("artifact_sha256 must be lower-case SHA-256")
        if artifact_size <= 0:
            raise UpdateValidationError("artifact_size must be positive")

        return cls(
            schema_version=schema_version,
            product=product,
            channel=channel,
            version=version,
            api_version=api_version,
            update_api_version=update_api_version,
            platform=platform,
            artifact=ReleaseArtifact(
                url=artifact_url,
                sha256=artifact_sha256,
                size=artifact_size,
            ),
            release_url=release_url,
        )


@dataclass(frozen=True)
class UpdateStateSnapshot:
    installed_version: str
    latest_version: str
    release_url: str
    in_progress: bool
    last_error: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "installed_version": self.installed_version,
            "latest_version": self.latest_version,
            "in_progress": self.in_progress,
        }
        if self.release_url:
            payload["release_url"] = self.release_url
        if self.last_error:
            payload["last_error"] = self.last_error
        return payload


@dataclass(frozen=True)
class VerifiedReleaseBundle:
    manifest_bytes: bytes
    signature_bytes: bytes
    manifest: SignedReleaseManifest
    artifact_bytes: bytes | None = None


class StateAgentUpdateCoordinator:
    """Dedicated OTA checker/uploader independent from feeder truth reconciliation."""

    def __init__(
        self,
        ad,
        state_publisher,
        state_agent,
        logger,
        options: StateAgentUpdateOptions,
        *,
        public_key_path: str = "/opt/petlibro-local/release-public-key.hex",
    ):
        self.ad = ad
        self.state = state_publisher
        self.state_agent = state_agent
        self.logger = logger
        self.options = options
        self._public_key = _load_public_key(public_key_path)
        self._check_timer = None
        self._status_poll_timer = None
        self._status_poll_attempts = 0
        self._busy = False
        self._last_check_monotonic: float | None = None
        self._latest_state = UpdateStateSnapshot(
            installed_version="unknown",
            latest_version="unknown",
            release_url="",
            in_progress=False,
            last_error="",
        )

    def start(self) -> None:
        self._publish_state()
        if not self.options.enabled:
            return
        self._schedule_next_periodic_check()

    def stop(self) -> None:
        if self._check_timer is not None:
            self.ad.cancel_timer(self._check_timer, True)
            self._check_timer = None
        if self._status_poll_timer is not None:
            self.ad.cancel_timer(self._status_poll_timer, True)
            self._status_poll_timer = None

    def on_usable_connection(self) -> None:
        if not self.options.enabled or not self.options.check_on_connect:
            return
        self.request_check(force=False, reason="connect")

    def on_home_assistant_birth(self) -> None:
        self._publish_state()
        if self.options.enabled and self.options.check_on_connect:
            self.request_check(force=False, reason="ha_birth")

    def request_check(self, *, force: bool, reason: str) -> None:
        if not self.options.enabled:
            self.logger.warning(
                "state-agent update check ignored because updates are disabled",
                reason=reason,
            )
            return
        if self._busy:
            self.logger.debug(
                "state-agent update check skipped while busy", reason=reason
            )
            return
        now = time.monotonic()
        if (
            not force
            and self._last_check_monotonic is not None
            and (now - self._last_check_monotonic) < self._minimum_spacing(reason)
        ):
            self.logger.debug("state-agent update check throttled", reason=reason)
            return

        self.logger.info(
            "state-agent update check started",
            reason=reason,
            manifest_url=self.options.manifest_url,
        )

        def worker() -> UpdateStateSnapshot:
            return self._check_for_updates(fetch_artifact=False)

        self._busy = True
        self._submit_to_executor(
            worker,
            callback=lambda result, **_kwargs: self._on_check_finished(result=result),
        )

    def request_install(self) -> None:
        if not self.options.enabled:
            self.logger.warning(
                "state-agent install ignored because updates are disabled"
            )
            return
        if self._busy:
            self.logger.warning(
                "state-agent install ignored while another update action is running"
            )
            return

        self._busy = True
        self._set_in_progress(True)

        def worker() -> UpdateStateSnapshot:
            return self._install_latest()

        self._submit_to_executor(
            worker,
            callback=lambda result, **_kwargs: self._on_install_finished(result=result),
        )

    def _on_check_finished(self, *, result: object) -> None:
        self._busy = False
        snapshot = self._extract_result(result)
        if snapshot is None:
            return
        self._latest_state = snapshot
        self._last_check_monotonic = time.monotonic()
        self.logger.info(
            "state-agent update check complete",
            installed_version=snapshot.installed_version,
            latest_version=snapshot.latest_version,
            update_available=(
                compare_semver(
                    snapshot.latest_version,
                    snapshot.installed_version,
                ) > 0
            ),
            in_progress=snapshot.in_progress,
            last_error=snapshot.last_error or "none",
        )
        self._publish_state()

    def _on_install_finished(self, *, result: object) -> None:
        self._busy = False
        if isinstance(result, _UpdateSubmissionOutcomeUnknown):
            self.logger.warning(
                "state-agent update submission outcome is unknown; polling status"
            )
            self._status_poll_attempts = 0
            self._schedule_status_poll()
            return
        snapshot = self._extract_result(result)
        if snapshot is None:
            self._set_in_progress(False)
            return
        self._latest_state = snapshot
        self._last_check_monotonic = time.monotonic()
        self._publish_state()
        if snapshot.in_progress:
            self._status_poll_attempts = 0
            self._schedule_status_poll()

    def _schedule_next_periodic_check(self) -> None:
        if not self.options.enabled:
            return
        delay_seconds = int(self.options.check_interval_hours * 3600)
        self._check_timer = self.ad.run_in(
            self._periodic_check,
            delay_seconds,
            interval_seconds=delay_seconds,
        )

    def _periodic_check(self, kwargs: dict[str, object]) -> None:
        interval_raw = kwargs.get("interval_seconds")
        if isinstance(interval_raw, int):
            interval_seconds = interval_raw
        else:
            interval_seconds = int(self.options.check_interval_hours * 3600)
        self.request_check(force=False, reason="periodic")
        self._check_timer = self.ad.run_in(
            self._periodic_check,
            interval_seconds,
            interval_seconds=interval_seconds,
        )

    def _schedule_status_poll(self) -> None:
        self._status_poll_timer = self.ad.run_in(
            self._poll_update_status,
            STATUS_POLL_INTERVAL_SECONDS,
        )

    def _poll_update_status(self, _kwargs: dict[str, object]) -> None:
        self._status_poll_timer = None
        if self._busy:
            self._schedule_status_poll()
            return
        self._busy = True

        def worker() -> UpdateStateSnapshot:
            return self._poll_feeder_update_status()

        self._submit_to_executor(
            worker,
            callback=lambda result, **_kwargs: self._on_status_poll_finished(result=result),
        )

    def _submit_to_executor(self, operation, *, callback) -> None:
        # AppDaemon logs executor exceptions without dispatching the callback.
        # Returning the exception keeps coordinator busy/progress state recoverable.
        def captured_operation():
            try:
                return operation()
            except Exception as exc:
                return exc

        self.ad.submit_to_executor(captured_operation, callback=callback)

    def _on_status_poll_finished(self, *, result: object) -> None:
        self._busy = False
        if isinstance(result, Exception):
            self.logger.warning(
                "state-agent update status temporarily unavailable",
                error_type=type(result).__name__,
            )
            self._continue_status_poll_or_timeout()
            return
        if not isinstance(result, UpdateStateSnapshot):
            self.logger.warning(
                "state-agent update status returned an unexpected result"
            )
            self._continue_status_poll_or_timeout()
            return
        snapshot = result
        self._latest_state = snapshot
        self._publish_state()
        if snapshot.in_progress:
            self._continue_status_poll_or_timeout()
        else:
            self._status_poll_attempts = 0

    def _continue_status_poll_or_timeout(self) -> None:
        self._status_poll_attempts += 1
        if self._status_poll_attempts < STATUS_POLL_MAX_ATTEMPTS:
            self._schedule_status_poll()
            return
        self._latest_state = UpdateStateSnapshot(
            installed_version=self._latest_state.installed_version,
            latest_version=self._latest_state.latest_version,
            release_url=self._latest_state.release_url,
            in_progress=False,
            last_error="update status timed out",
        )
        self.logger.warning(
            "state-agent update status polling timed out",
            attempts=self._status_poll_attempts,
        )
        self._publish_state()

    def _extract_result(self, result: object) -> UpdateStateSnapshot | None:
        if isinstance(result, UpdateStateSnapshot):
            return result
        if isinstance(result, Exception):
            self.logger.warning(
                "state-agent update action failed", error_type=type(result).__name__
            )
            self._latest_state = UpdateStateSnapshot(
                installed_version=self._latest_state.installed_version,
                latest_version=self._latest_state.latest_version,
                release_url=self._latest_state.release_url,
                in_progress=False,
                last_error="update action failed",
            )
            self._publish_state()
            return None
        self.logger.warning("state-agent update action returned an unexpected result")
        return None

    def _set_in_progress(self, in_progress: bool) -> None:
        self._latest_state = UpdateStateSnapshot(
            installed_version=self._latest_state.installed_version,
            latest_version=self._latest_state.latest_version,
            release_url=self._latest_state.release_url,
            in_progress=in_progress,
            last_error="" if in_progress else self._latest_state.last_error,
        )
        self._publish_state()

    def _minimum_spacing(self, reason: str) -> float:
        if reason in {"connect", "ha_birth"}:
            return min(
                float(self.options.check_interval_hours * 3600),
                CHECK_CONNECT_THROTTLE_SECONDS,
            )
        return float(self.options.check_interval_hours * 3600)

    def _publish_state(self) -> None:
        self.state.publish(UPDATE_TOPIC, self._latest_state.to_payload(), retain=True)

    def _check_for_updates(self, *, fetch_artifact: bool) -> UpdateStateSnapshot:
        bundle = fetch_verified_release_bundle(
            manifest_url=self.options.manifest_url,
            public_key=self._public_key,
            fetch_artifact=fetch_artifact,
        )
        installed_version = self.state_agent.version().version
        status = self._safe_update_status()
        update_available = compare_semver(bundle.manifest.version, installed_version) > 0
        return UpdateStateSnapshot(
            installed_version=installed_version,
            latest_version=(
                bundle.manifest.version if update_available else installed_version
            ),
            release_url=bundle.manifest.release_url if update_available else "",
            in_progress=status.in_progress,
            last_error=status.last_error,
        )

    def _install_latest(self) -> UpdateStateSnapshot:
        bundle = fetch_verified_release_bundle(
            manifest_url=self.options.manifest_url,
            public_key=self._public_key,
            fetch_artifact=False,
        )
        installed_version = self.state_agent.version().version
        if compare_semver(bundle.manifest.version, installed_version) <= 0:
            status = self._safe_update_status()
            return UpdateStateSnapshot(
                installed_version=installed_version,
                latest_version=installed_version,
                release_url="",
                in_progress=status.in_progress,
                last_error=status.last_error,
            )
        artifact_bytes = download_verified_artifact(bundle.manifest)
        frame = build_update_frame(
            bundle.manifest_bytes,
            bundle.signature_bytes,
            artifact_bytes,
        )
        try:
            submit_result = self.state_agent.submit_update(frame)
        except (StateAgentTimeout, StateAgentUnavailable) as exc:
            raise _UpdateSubmissionOutcomeUnknown from exc
        if not submit_result.accepted:
            raise StateAgentBadResponse("state agent rejected update upload")
        return UpdateStateSnapshot(
            installed_version=installed_version,
            latest_version=bundle.manifest.version,
            release_url=bundle.manifest.release_url,
            in_progress=True,
            last_error="",
        )

    def _safe_update_status(self) -> StateAgentUpdateStatus:
        try:
            return self.state_agent.update_status()
        except StateAgentError:
            return StateAgentUpdateStatus(
                status="idle",
                reason="none",
                candidate_version=None,
                previous_version=None,
            )

    def _poll_feeder_update_status(self) -> UpdateStateSnapshot:
        status = self.state_agent.update_status()
        installed_version = self._latest_state.installed_version
        if not status.in_progress:
            installed_version = self.state_agent.version().version
        return UpdateStateSnapshot(
            installed_version=installed_version,
            latest_version=self._latest_state.latest_version,
            release_url=self._latest_state.release_url,
            in_progress=status.in_progress,
            last_error=status.last_error,
        )


def fetch_verified_release_bundle(
    *,
    manifest_url: str,
    public_key: Ed25519PublicKey,
    fetch_artifact: bool,
) -> VerifiedReleaseBundle:
    _validate_https_url(manifest_url, field_name="manifest_url", immutable_only=False)
    manifest_bytes = _download_https_bytes(
        manifest_url, byte_limit=MANIFEST_BYTES_LIMIT
    )
    signature_bytes = _download_https_bytes(
        _signature_url(manifest_url), byte_limit=SIGNATURE_BYTES_LIMIT
    )
    if len(signature_bytes) != 64:
        raise UpdateValidationError("manifest signature must be 64 bytes")
    try:
        public_key.verify(signature_bytes, manifest_bytes)
    except InvalidSignature as exc:
        raise UpdateValidationError("manifest signature verification failed") from exc

    manifest = SignedReleaseManifest.from_bytes(manifest_bytes)
    artifact_bytes = None
    if fetch_artifact:
        artifact_bytes = download_verified_artifact(manifest)
    return VerifiedReleaseBundle(
        manifest_bytes=manifest_bytes,
        signature_bytes=signature_bytes,
        manifest=manifest,
        artifact_bytes=artifact_bytes,
    )


def download_verified_artifact(manifest: SignedReleaseManifest) -> bytes:
    artifact_limit = min(
        ARTIFACT_ABSOLUTE_BYTES_LIMIT,
        manifest.artifact.size + ARTIFACT_DOWNLOAD_SLACK_BYTES,
    )
    return _download_https_bytes(
        manifest.artifact.url,
        byte_limit=artifact_limit,
        expected_size=manifest.artifact.size,
        expected_sha256=manifest.artifact.sha256,
    )


def build_update_frame(
    manifest_bytes: bytes, signature_bytes: bytes, artifact_bytes: bytes
) -> bytes:
    if len(signature_bytes) != 64:
        raise UpdateValidationError("manifest signature must be 64 bytes")
    if len(manifest_bytes) == 0 or len(artifact_bytes) == 0:
        raise UpdateValidationError("manifest and artifact must be non-empty")
    if any(
        length > 0xFFFFFFFF
        for length in (len(manifest_bytes), len(signature_bytes), len(artifact_bytes))
    ):
        raise UpdateValidationError("update frame component is too large")
    header = b"PLAFOTA1" + struct.pack(
        ">III", len(manifest_bytes), len(signature_bytes), len(artifact_bytes)
    )
    return header + manifest_bytes + signature_bytes + artifact_bytes


def _download_https_bytes(
    url: str,
    *,
    byte_limit: int,
    expected_size: int | None = None,
    expected_sha256: str | None = None,
) -> bytes:
    parsed = _validate_https_url(url, field_name="url", immutable_only=False)
    req = request.Request(
        parse.urlunsplit(parsed),
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    opener = request.build_opener(_RejectRedirectHandler())
    try:
        with opener.open(req, timeout=DEFAULT_HTTPS_TIMEOUT_SECONDS) as response:
            effective_url = response.geturl()
            _validate_https_url(
                effective_url, field_name="effective download URL", immutable_only=False
            )
            if effective_url != parse.urlunsplit(parsed):
                raise UpdateValidationError("HTTPS release download URL changed")
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > byte_limit:
                    raise UpdateValidationError(
                        "download exceeds configured byte limit"
                    )
                digest.update(chunk)
    except (TimeoutError, error.URLError, OSError) as exc:
        raise UpdateValidationError("HTTPS download failed") from exc

    payload = b"".join(chunks)
    if expected_size is not None and len(payload) != expected_size:
        raise UpdateValidationError("download size does not match manifest")
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise UpdateValidationError("download sha256 does not match manifest")
    return payload


def _signature_url(manifest_url: str) -> str:
    parsed = _validate_https_url(
        manifest_url, field_name="manifest_url", immutable_only=False
    )
    return parse.urlunsplit(parsed._replace(path=parsed.path + ".sig"))


def _load_public_key(path: str) -> Ed25519PublicKey:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw_text = handle.read()
    except OSError as exc:
        raise UpdateValidationError("release public key file is unreadable") from exc
    normalized = "".join(raw_text.split())
    if len(normalized) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", normalized):
        raise UpdateValidationError("release public key must be 32-byte hex")
    key_bytes = bytes.fromhex(normalized)
    if len(key_bytes) != 32:
        raise UpdateValidationError("release public key must be 32 bytes")
    return Ed25519PublicKey.from_public_bytes(key_bytes)


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    decoded: dict[str, Any] = {}
    for key, value in pairs:
        if key in decoded:
            raise UpdateValidationError("manifest contains a duplicate key")
        decoded[key] = value
    return decoded


def _json_str(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise UpdateValidationError(f"manifest field {key} must be a non-empty string")
    return value


def _json_int(data: dict[str, object], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UpdateValidationError(f"manifest field {key} must be an integer")
    return value


def _json_object(data: dict[str, object], key: str) -> dict[str, object]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise UpdateValidationError(f"manifest field {key} must be an object")
    return value


def _validate_https_url(
    url: str, *, field_name: str, immutable_only: bool
) -> parse.SplitResult:
    parsed = parse.urlsplit(url)
    if parsed.scheme != "https":
        raise UpdateValidationError(f"{field_name} must use HTTPS")
    if not parsed.hostname:
        raise UpdateValidationError(f"{field_name} must include a hostname")
    if parsed.username or parsed.password:
        raise UpdateValidationError(f"{field_name} must not include credentials")
    if parsed.fragment:
        raise UpdateValidationError(f"{field_name} must not include a fragment")
    if immutable_only:
        if parsed.query:
            raise UpdateValidationError(
                f"{field_name} must not include query parameters"
            )
        if parsed.path in {"", "/"}:
            raise UpdateValidationError(
                f"{field_name} must include a concrete artifact path"
            )
    return parsed
