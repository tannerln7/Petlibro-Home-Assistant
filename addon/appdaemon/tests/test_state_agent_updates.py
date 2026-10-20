import inspect
import json
import struct
from types import SimpleNamespace
from pathlib import Path

import pytest
import state_agent_updates as update_module
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from state_agent import StateAgentUnavailable
from state_agent_updates import (
    SignedReleaseManifest,
    StateAgentUpdateCoordinator,
    StateAgentUpdateOptions,
    UpdateStateSnapshot,
    UpdateValidationError,
    build_update_frame,
    compare_semver,
    fetch_verified_release_bundle,
)


def _manifest_bytes(**overrides) -> bytes:
    payload = {
        "schema_version": 1,
        "product": "plaf203-state-agent",
        "channel": "stable",
        "version": "1.2.3",
        "api_version": 1,
        "update_api_version": 1,
        "platform": "linux-armv7-eabihf",
        "artifact": {
            "url": "https://downloads.example.invalid/plaf203/1.2.3/plaf203-state-agent",
            "sha256": "0" * 64,
            "size": 4,
        },
        "release_url": "https://github.com/example/releases/tag/v1.2.3",
    }
    payload.update(overrides)
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def test_signed_manifest_rejects_duplicate_keys():
    with pytest.raises(UpdateValidationError, match="duplicate key"):
        SignedReleaseManifest.from_bytes(
            b'{"schema_version":1,"product":"plaf203-state-agent","channel":"stable","version":"1.2.3","api_version":1,"update_api_version":1,"platform":"linux-armv7-eabihf","artifact":{"url":"https://x/y","sha256":"0000000000000000000000000000000000000000000000000000000000000000","sha256":"0000000000000000000000000000000000000000000000000000000000000000","size":1},"release_url":"https://x/z"}'
        )


def test_signed_manifest_validation_is_strict():
    with pytest.raises(UpdateValidationError, match="schema_version"):
        SignedReleaseManifest.from_bytes(_manifest_bytes(schema_version=2))
    with pytest.raises(UpdateValidationError, match="must be SemVer"):
        SignedReleaseManifest.from_bytes(_manifest_bytes(version="1.2"))
    with pytest.raises(UpdateValidationError, match="lower-case SHA-256"):
        SignedReleaseManifest.from_bytes(
            _manifest_bytes(
                artifact={"url": "https://x/y", "sha256": "A" * 64, "size": 4}
            )
        )
    with pytest.raises(UpdateValidationError, match="artifact_size"):
        SignedReleaseManifest.from_bytes(
            _manifest_bytes(
                artifact={"url": "https://x/y", "sha256": "0" * 64, "size": 0}
            )
        )


def test_fetch_bundle_verifies_raw_manifest_signature(monkeypatch):
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    manifest_bytes = _manifest_bytes(
        artifact={
            "url": "https://downloads.example.invalid/plaf203/1.2.3/plaf203-state-agent",
            "sha256": "3a6eb0790f39ac87c94f3856b2dd2c5d110e6811602261a9a923d3bb23adc8b7",
            "size": 4,
        }
    )
    signature = private_key.sign(manifest_bytes)
    artifact = b"data"

    def fake_download(url: str, **_kwargs):
        if url.endswith(".sig"):
            return signature
        if url.endswith("latest.json"):
            return manifest_bytes
        return artifact

    monkeypatch.setattr("state_agent_updates._download_https_bytes", fake_download)

    bundle = fetch_verified_release_bundle(
        manifest_url="https://updates.example.invalid/latest.json",
        public_key=public_key,
        fetch_artifact=True,
    )
    assert bundle.manifest.version == "1.2.3"
    assert bundle.artifact_bytes == artifact

    monkeypatch.setattr(
        "state_agent_updates._download_https_bytes",
        lambda url, **kwargs: (
            signature
            if url.endswith(".sig")
            else (manifest_bytes + b" ")
            if url.endswith("latest.json")
            else artifact
        ),
    )
    with pytest.raises(UpdateValidationError, match="signature verification failed"):
        fetch_verified_release_bundle(
            manifest_url="https://updates.example.invalid/latest.json",
            public_key=public_key,
            fetch_artifact=False,
        )

    wrong_public = Ed25519PrivateKey.generate().public_key()
    monkeypatch.setattr("state_agent_updates._download_https_bytes", fake_download)
    with pytest.raises(UpdateValidationError, match="signature verification failed"):
        fetch_verified_release_bundle(
            manifest_url="https://updates.example.invalid/latest.json",
            public_key=wrong_public,
            fetch_artifact=False,
        )


def test_release_download_redirects_are_rejected():
    handler = update_module._RejectRedirectHandler()
    with pytest.raises(UpdateValidationError, match="must not redirect"):
        handler.redirect_request(
            None,
            None,
            302,
            "Found",
            {},
            "http://other.example.invalid/latest.json",
        )


def test_signature_url_appends_to_path_before_query():
    assert update_module._signature_url(
        "https://updates.example.invalid/latest.json?channel=stable"
    ) == "https://updates.example.invalid/latest.json.sig?channel=stable"


def test_release_download_rejects_changed_effective_url(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "https://other.example.invalid/latest.json"

        def read(self, _size):
            return b""

    class FakeOpener:
        def open(self, _request, timeout):
            assert timeout > 0
            return FakeResponse()

    monkeypatch.setattr(update_module.request, "build_opener", lambda *_: FakeOpener())

    with pytest.raises(UpdateValidationError, match="URL changed"):
        update_module._download_https_bytes(
            "https://updates.example.invalid/latest.json", byte_limit=1024
        )


def test_update_frame_has_exact_header_and_lengths():
    manifest = b'{"a":1}'
    signature = b"s" * 64
    artifact = b"abc123"
    frame = build_update_frame(manifest, signature, artifact)
    assert frame[:8] == b"PLAFOTA1"
    assert struct.unpack(">III", frame[8:20]) == (
        len(manifest),
        len(signature),
        len(artifact),
    )
    assert frame[20 : 20 + len(manifest)] == manifest


class _FakeAD:
    def __init__(self):
        self.executor_calls = 0
        self.executor_errors = []
        self.timers = []

    def submit_to_executor(self, func, *args, callback=None):
        self.executor_calls += 1
        try:
            value = func(*args)
        except Exception as error:
            # AppDaemon logs the worker error and does not dispatch its callback.
            self.executor_errors.append(error)
            return object()
        if callback is not None:
            params = inspect.signature(callback).parameters.values()
            has_expanded_kwargs = any(
                param.kind is inspect.Parameter.VAR_KEYWORD
                for param in params
            )
            if has_expanded_kwargs:
                callback(result=value)
            else:
                callback({"result": value})
        return object()

    def run_in(self, callback, delay, **kwargs):
        handle = object()
        self.timers.append((handle, callback, delay, kwargs))
        return handle

    def cancel_timer(self, _handle, _silent):
        return None


class _FakeStatePublisher:
    def __init__(self):
        self.messages = []

    def publish(self, topic, value, retain=False):
        self.messages.append((topic, value, retain))


class _FakeLogger:
    def __init__(self):
        self.events = []

    def debug(self, message, **kwargs):
        self.events.append(("debug", message, kwargs))

    def info(self, message, **kwargs):
        self.events.append(("info", message, kwargs))

    def warning(self, message, **kwargs):
        self.events.append(("warning", message, kwargs))


class _FakeAgent:
    def __init__(self, version="1.0.0"):
        self.update_payloads = []
        self.installed_version = version
        self.status_calls = 0

    class _Version:
        def __init__(self, version):
            self.version = version

    class _Status:
        def __init__(self, in_progress=False, reason="none"):
            self.in_progress = in_progress
            self.status = "pending" if in_progress else "idle"
            self.reason = reason
            self.candidate_version = "1.2.3" if in_progress else None
            self.previous_version = "1.0.0" if in_progress else None

        @property
        def last_error(self):
            if self.status in {"failed", "rolled_back"}:
                return self.reason
            return ""

    class _Submit:
        accepted = True
        message = "accepted"

    def version(self):
        return self._Version(self.installed_version)

    def update_status(self):
        self.status_calls += 1
        return self._Status(in_progress=False)

    def submit_update(self, payload: bytes):
        self.update_payloads.append(payload)
        return self._Submit()


def _coordinator(tmp_path: Path, *, agent=None, update_complete_callback=None):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.hex"
    key_path.write_text(key.public_key().public_bytes_raw().hex(), encoding="utf-8")
    return StateAgentUpdateCoordinator(
        _FakeAD(),
        _FakeStatePublisher(),
        agent or _FakeAgent(),
        _FakeLogger(),
        StateAgentUpdateOptions(
            enabled=True,
            manifest_url="https://updates.example.invalid/latest.json",
        ),
        public_key_path=str(key_path),
        update_complete_callback=update_complete_callback,
    )


def test_coordinator_throttle_and_force(monkeypatch, tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.hex"
    key_path.write_text(
        key.public_key().public_bytes_raw().hex(),
        encoding="utf-8",
    )

    calls = {"check": 0, "install": 0}

    def fake_bundle(*, manifest_url, public_key, fetch_artifact):
        assert manifest_url.startswith("https://")
        assert isinstance(public_key, Ed25519PublicKey)
        if fetch_artifact:
            calls["install"] += 1
            artifact = b"agent"
        else:
            calls["check"] += 1
            artifact = None
        manifest_bytes = _manifest_bytes(
            artifact={
                "url": "https://downloads.example.invalid/plaf203/1.2.3/plaf203-state-agent",
                "size": 5,
                "sha256": "f8758f659f64f1d1ea2525c2849983f2dace98791a30e13899b8f2f4cae4f390",
            },
        )
        return type(
            "Bundle",
            (),
            {
                "manifest_bytes": manifest_bytes,
                "signature_bytes": b"s" * 64,
                "manifest": SignedReleaseManifest.from_bytes(manifest_bytes),
                "artifact_bytes": artifact,
            },
        )()

    monkeypatch.setattr(
        "state_agent_updates.fetch_verified_release_bundle", fake_bundle
    )
    monkeypatch.setattr(
        "state_agent_updates.download_verified_artifact",
        lambda _manifest: calls.__setitem__("install", calls["install"] + 1)
        or b"agent",
    )

    agent = _FakeAgent()
    coordinator = StateAgentUpdateCoordinator(
        _FakeAD(),
        _FakeStatePublisher(),
        agent,
        _FakeLogger(),
        StateAgentUpdateOptions(
            enabled=True,
            manifest_url="https://updates.example.invalid/latest.json",
            check_on_connect=True,
            check_interval_hours=24,
        ),
        public_key_path=str(key_path),
    )

    # Simulate a fresh boot with less than 10 minutes of uptime.
    # The first check must run even when monotonic time is near zero.
    monkeypatch.setattr(
        update_module, "time", SimpleNamespace(monotonic=lambda: 5.0)
    )

    coordinator.request_check(force=False, reason="connect")
    coordinator.request_check(force=False, reason="connect")
    coordinator.request_check(force=True, reason="manual")
    assert calls["check"] == 2

    coordinator.request_install()
    assert calls["install"] == 1
    assert len(coordinator.state_agent.update_payloads) == 1
    assert coordinator.state_agent.update_payloads[0][:8] == b"PLAFOTA1"
    assert coordinator._latest_state.in_progress is True
    assert agent.status_calls == 2  # update checks only; accepted upload starts polling later
    assert (
        "info",
        "state-agent update started",
        {"previous_version": "1.0.0", "target_version": "1.2.3"},
    ) in coordinator.logger.events


def test_status_poll_queries_only_the_feeder(monkeypatch, tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.hex"
    key_path.write_text(
        key.public_key().public_bytes_raw().hex(),
        encoding="utf-8",
    )
    agent = _FakeAgent()
    agent.update_status = lambda: agent._Status(in_progress=True)
    coordinator = StateAgentUpdateCoordinator(
        _FakeAD(),
        _FakeStatePublisher(),
        agent,
        _FakeLogger(),
        StateAgentUpdateOptions(
            enabled=True,
            manifest_url="https://updates.example.invalid/latest.json",
        ),
        public_key_path=str(key_path),
    )
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )
    monkeypatch.setattr(
        "state_agent_updates.fetch_verified_release_bundle",
        lambda **_kwargs: pytest.fail("status polling must not fetch a manifest"),
    )

    coordinator._poll_update_status({})

    assert coordinator._latest_state.in_progress is True
    assert coordinator._latest_state.latest_version == "1.2.3"


def test_status_poll_error_preserves_progress_and_retries(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )

    coordinator._on_status_poll_finished(result=RuntimeError("agent restarting"))

    assert coordinator._latest_state.in_progress is True
    assert coordinator._status_poll_attempts == 1
    assert len(coordinator.ad.timers) == 1
    assert coordinator.logger.events[-1] == (
        "debug",
        "state-agent update status temporarily unavailable",
        {
            "error_type": "RuntimeError",
            "attempt": 1,
            "attempt_limit": update_module.STATUS_POLL_MAX_ATTEMPTS,
        },
    )


def test_executor_error_during_install_reaches_callback_and_clears_busy(
    tmp_path: Path,
):
    coordinator = _coordinator(tmp_path)
    coordinator._install_latest = lambda: (_ for _ in ()).throw(
        StateAgentUnavailable("agent unavailable")
    )

    coordinator.request_install()

    assert coordinator.ad.executor_errors == []
    assert coordinator._busy is False
    assert coordinator._latest_state.in_progress is False
    assert coordinator._latest_state.last_error == "update action failed"


def test_unknown_upload_outcome_starts_bounded_status_poll(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    coordinator._install_latest = lambda: (_ for _ in ()).throw(
        update_module._UpdateSubmissionOutcomeUnknown()
    )

    coordinator.request_install()

    assert coordinator.ad.executor_errors == []
    assert coordinator._busy is False
    assert coordinator._latest_state.in_progress is True
    assert coordinator._status_poll_attempts == 0
    assert len(coordinator.ad.timers) == 1


def test_executor_error_while_agent_restarts_keeps_status_polling(tmp_path: Path):
    agent = _FakeAgent()
    agent.update_status = lambda: (_ for _ in ()).throw(
        StateAgentUnavailable("agent restarting")
    )
    coordinator = _coordinator(tmp_path, agent=agent)
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )

    coordinator._poll_update_status({})

    assert coordinator.ad.executor_errors == []
    assert coordinator._busy is False
    assert coordinator._latest_state.in_progress is True
    assert coordinator._status_poll_attempts == 1
    assert len(coordinator.ad.timers) == 1


def test_status_poll_recovers_after_expected_agent_restart(tmp_path: Path):
    agent = _FakeAgent()
    agent.update_status = lambda: (_ for _ in ()).throw(
        StateAgentUnavailable("agent restarting")
    )
    coordinator = _coordinator(tmp_path, agent=agent)
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )

    coordinator._poll_update_status({})
    _handle, retry, _delay, retry_kwargs = coordinator.ad.timers.pop(0)
    agent.installed_version = "1.2.3"
    agent.update_status = lambda: agent._Status(in_progress=False)
    retry(retry_kwargs)

    assert coordinator.ad.executor_errors == []
    assert coordinator._busy is False
    assert coordinator._latest_state.installed_version == "1.2.3"
    assert coordinator._latest_state.in_progress is False
    assert coordinator._status_poll_attempts == 0
    assert coordinator.ad.timers == []
    assert coordinator.logger.events[-1] == (
        "info",
        "state-agent update complete",
        {"previous_version": "1.0.0", "installed_version": "1.2.3"},
    )


def test_confirmed_update_schedules_release_compatibility_reconciliation(
    tmp_path: Path,
):
    completed = []
    coordinator = _coordinator(
        tmp_path, update_complete_callback=completed.append
    )
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )

    coordinator._on_status_poll_finished(
        result=UpdateStateSnapshot(
            installed_version="1.2.3",
            latest_version="1.2.3",
            release_url="https://example.invalid/releases/1.2.3",
            in_progress=False,
        )
    )

    assert completed == ["1.2.3"]


def test_terminal_update_failure_retains_warning_severity(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )

    coordinator._on_status_poll_finished(
        result=UpdateStateSnapshot(
            installed_version="1.0.0",
            latest_version="1.2.3",
            release_url="https://example.invalid/releases/1.2.3",
            in_progress=False,
            last_error="rolled_back",
        )
    )

    assert coordinator.logger.events[-1] == (
        "warning",
        "state-agent update failed",
        {
            "previous_version": "1.0.0",
            "target_version": "1.2.3",
            "installed_version": "1.0.0",
            "reason": "rolled_back",
        },
    )


def test_busy_status_poll_is_rescheduled_instead_of_dropped(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    coordinator._busy = True

    coordinator._poll_update_status({})

    assert coordinator.ad.executor_calls == 0
    assert len(coordinator.ad.timers) == 1


def test_status_poll_exhaustion_publishes_terminal_timeout(tmp_path: Path):
    coordinator = _coordinator(tmp_path)
    coordinator._latest_state = UpdateStateSnapshot(
        installed_version="1.0.0",
        latest_version="1.2.3",
        release_url="https://example.invalid/releases/1.2.3",
        in_progress=True,
    )
    coordinator._status_poll_attempts = update_module.STATUS_POLL_MAX_ATTEMPTS - 1

    coordinator._on_status_poll_finished(result=RuntimeError("still restarting"))

    assert coordinator._latest_state.in_progress is False
    assert coordinator._latest_state.last_error == "update status timed out"
    assert coordinator.state.messages[-1][1]["last_error"] == "update status timed out"
    warning_messages = [
        message
        for level, message, _fields in coordinator.logger.events
        if level == "warning"
    ]
    assert warning_messages == ["state-agent update status polling timed out"]


def test_manifest_option_rejects_query_parameters():
    with pytest.raises(ValueError, match="query"):
        StateAgentUpdateOptions.from_mapping(
            {
                "enabled": True,
                "manifest_url": "https://updates.example.invalid/latest.json?channel=stable",
            }
        )


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.2.3", "1.2.3-rc.1", 1),
        ("1.2.3-rc.2", "1.2.3-rc.1", 1),
        ("1.2.3-rc.10", "1.2.3-rc.2", 1),
        ("1.2.3-1", "1.2.3-alpha", -1),
        ("1.2.3-alpha", "1.2.3-alpha.1", -1),
        ("1.2.3+build.2", "1.2.3+build.1", 0),
        ("0.10.0", "0.9.0", 1),
        ("0.4.0-rc.1", "0.3.2", 1),
    ],
)
def test_semver_precedence(left, right, expected):
    assert compare_semver(left, right) == expected


@pytest.mark.parametrize(
    ("installed", "offered", "available"),
    [
        ("1.2.3", "1.2.3", False),
        ("1.2.3", "1.2.2", False),
        ("1.2.3", "1.2.4", True),
    ],
)
def test_version_presentation_and_install_download_policy(
    monkeypatch, tmp_path, installed, offered, available
):
    key = Ed25519PrivateKey.generate()
    key_path = tmp_path / "release-key.hex"
    key_path.write_text(key.public_key().public_bytes_raw().hex(), encoding="utf-8")
    manifest_bytes = _manifest_bytes(
        version=offered,
        artifact={
            "url": "https://downloads.example.invalid/agent",
            "sha256": "f8758f659f64f1d1ea2525c2849983f2dace98791a30e13899b8f2f4cae4f390",
            "size": 5,
        },
    )
    manifest = SignedReleaseManifest.from_bytes(manifest_bytes)
    bundle = type(
        "Bundle",
        (),
        {
            "manifest_bytes": manifest_bytes,
            "signature_bytes": b"s" * 64,
            "manifest": manifest,
            "artifact_bytes": None,
        },
    )()
    downloads = []
    monkeypatch.setattr(
        "state_agent_updates.fetch_verified_release_bundle", lambda **_kwargs: bundle
    )
    monkeypatch.setattr(
        "state_agent_updates.download_verified_artifact",
        lambda _manifest: downloads.append(True) or b"agent",
    )
    agent = _FakeAgent(installed)
    coordinator = StateAgentUpdateCoordinator(
        _FakeAD(),
        _FakeStatePublisher(),
        agent,
        _FakeLogger(),
        StateAgentUpdateOptions(
            enabled=True,
            manifest_url="https://updates.example.invalid/latest.json",
        ),
        public_key_path=str(key_path),
    )

    check = coordinator._check_for_updates(fetch_artifact=False)
    install = coordinator._install_latest()

    assert check.latest_version == (offered if available else installed)
    assert bool(check.release_url) is available
    assert install.latest_version == (offered if available else installed)
    assert bool(downloads) is available
    assert bool(agent.update_payloads) is available


def test_release_public_key_is_packaged_from_sole_source():
    root = Path(__file__).resolve().parents[3]
    assert not (root / "addon" / "appdaemon" / "release-public-key.hex").exists()

    dockerfile = (root / "addon" / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "COPY state-agent/release-public-key.hex "
        "/opt/petlibro-local/release-public-key.hex"
    ) in dockerfile
    assert "COPY appdaemon/release-public-key.hex" not in dockerfile
