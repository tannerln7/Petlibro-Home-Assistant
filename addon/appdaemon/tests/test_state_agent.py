import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from state_agent import (
    FeederTruth,
    StateAgentBadResponse,
    StateAgentClient,
    StateAgentUpdateStatus,
    diff_settings_raw,
)


@pytest.mark.parametrize(
    "phase",
    [
        "pending",
        "activating",
        "candidate_active",
        "probation_confirmed",
        "rollback_in_progress",
    ],
)
def test_update_status_reports_all_transaction_phases_in_progress(phase):
    status = StateAgentUpdateStatus(phase, "test", "1.0.0", "0.3.0")
    assert status.in_progress is True


class FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.body


def core_payload(*, enable_audio_raw=1, audio_times=2, settings_raw=None):
    payload = {
        "ok": True,
        "read_ms": 1,
        "revisions": {
            "core_rev": "fnv64:core",
            "settings_rev": "fnv64:settings",
            "plans_rev": "fnv64:plans",
            "queue_index_rev": "fnv64:queue",
        },
        "settings": {
            "audio_url": "https://example.invalid/meal-call.aac",
            "feeding_audio_enabled": "enabled",
            "volume": 76,
            "camera_resolution": "1080p",
            "sound_switch": "enabled",
        },
        "setting_classes": {
            "persistent": [
                "audio_url",
                "feeding_audio_enabled",
                "volume",
                "camera_resolution",
                "sound_switch",
            ],
            "effective_cached": [],
            "runtime": [],
        },
        "plans": {
            "ok": True,
            "count": 1,
            "plan_bin_size": 47,
            "record_size": 47,
            "even_split": True,
            "semantic_records": [
                {
                    "id": 1,
                    "minute": 0,
                    "hour_utc": 11,
                    "one_shot": False,
                    "one_shot_raw": 0,
                    "time_utc": "11:00",
                    "time_local_candidate": "07:00",
                    "days_raw": [1, 2, 3, 4, 5, 6, 7],
                    "days": [
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                        "sunday",
                    ],
                    "portions": 10,
                    "enable_audio_raw": enable_audio_raw,
                    "audio_times": audio_times,
                    "execution_state": 0,
                    "sync_time": 1_700_000_000_000,
                    "skip_end_time": 0,
                    "opaque_hex": "00 01 02 03 04 05 06 07 08 09",
                }
            ],
        },
        "queue": {"head": 23, "tail": 23, "pending": False},
    }
    if settings_raw is not None:
        payload["settings_raw"] = settings_raw
    return payload


def test_core_parser_builds_explicit_truth_models():
    truth = FeederTruth.from_dict(core_payload(settings_raw={"attr_state_0x0012": 0}))

    assert truth.settings["volume"] == 76
    assert truth.revisions.core_rev == "fnv64:core"
    assert truth.plans.count == 1
    assert truth.plans.by_id(1).enable_audio_raw == 1
    assert truth.plans.by_id(1).audio_times == 2
    assert truth.settings.is_persistent("sound_switch")
    assert truth.settings_raw == {"attr_state_0x0012": 0}


def test_raw_settings_diff_identifies_changed_file_fields_without_semantic_guessing():
    before = {
        "attr_state_0x0012": 0,
        "attr_state_0x0013": 7,
    }
    after = {
        "attr_state_0x0012": 1,
        "attr_state_0x0013": 7,
    }

    assert diff_settings_raw(before, after) == {"attr_state_0x0012": (0, 1)}


def test_core_parser_rejects_count_mismatch_and_duplicate_days():
    payload = core_payload()
    payload["plans"]["count"] = 2
    with pytest.raises(StateAgentBadResponse, match="plans.count"):
        FeederTruth.from_dict(payload)

    payload = core_payload()
    payload["plans"]["semantic_records"][0]["days_raw"] = [1, 1]
    with pytest.raises(StateAgentBadResponse, match="duplicates"):
        FeederTruth.from_dict(payload)


def test_plan_semantic_equality_excludes_execution_state_and_sync_metadata():
    plan = FeederTruth.from_dict(core_payload()).plans.by_id(1)

    runtime_changed = replace(
        plan,
        execution_state=0xAABBCCDD,
        sync_time=plan.sync_time + 5000,
    )
    schedule_changed = replace(plan, portions=plan.portions + 1)

    assert runtime_changed.semantic_fingerprint() == plan.semantic_fingerprint()
    assert runtime_changed.stable_fingerprint() == plan.stable_fingerprint()
    assert schedule_changed.semantic_fingerprint() != plan.semantic_fingerprint()


def test_plan_opaque_tail_is_preserved_but_not_called_schedule_semantics():
    plan = FeederTruth.from_dict(core_payload()).plans.by_id(1)
    opaque_changed = replace(plan, opaque_hex="ff" * 10)

    assert opaque_changed.semantic_fingerprint() == plan.semantic_fingerprint()
    assert opaque_changed.stable_fingerprint() != plan.stable_fingerprint()


def test_client_rejects_credentials_in_url_and_never_exposes_token():
    token = "top-secret-token"
    with pytest.raises(ValueError, match="credentials") as error:
        StateAgentClient(
            "http://user:password@192.0.2.1:8765", token, timeout_seconds=1
        )
    assert token not in str(error.value)


def test_client_rejects_non_positive_request_timeouts():
    with pytest.raises(ValueError, match="timeout must be positive"):
        StateAgentClient("http://192.0.2.1:8765", "token", timeout_seconds=0)
    with pytest.raises(ValueError, match="update timeout must be positive"):
        StateAgentClient(
            "http://192.0.2.1:8765",
            "token",
            update_timeout_seconds=0,
        )


def test_client_sends_bearer_auth_and_parses_all_read_only_endpoints():
    responses = [
        FakeResponse({"ok": True, "agent": {"name": "state-agent"}}),
        FakeResponse(
            {
                "ok": True,
                "read_ms": 0,
                "revisions": core_payload()["revisions"],
                "queue": core_payload()["queue"],
            }
        ),
        FakeResponse(core_payload()),
        FakeResponse(core_payload(settings_raw={"attr_state_0x0012": 1})),
        FakeResponse(
            {
                "ok": True,
                "queue": core_payload()["queue"],
                "err_queue": {"head": 255, "tail": 255, "pending": False},
                "semantics": "pending_outbound_events_not_history",
                "events": [],
                "error_events": [],
                "snapshot_consistent": True,
                "normal_ring_valid": True,
                "error_ring_valid": False,
            }
        ),
    ]
    client = StateAgentClient(
        "http://192.0.2.1:8765",
        "synthetic-test-token",
        timeout_seconds=1,
        update_timeout_seconds=30,
    )

    with patch("state_agent.request.urlopen", side_effect=responses) as urlopen:
        assert client.health()["ok"] is True
        assert client.revisions().revisions.core_rev == "fnv64:core"
        assert client.core().plans.by_id(1).portions == 10
        assert client.core(raw=True).settings_raw == {"attr_state_0x0012": 1}
        assert client.feed_events().events == ()

    assert [call.args[0].full_url for call in urlopen.call_args_list] == [
        "http://192.0.2.1:8765/health",
        "http://192.0.2.1:8765/v1/rev",
        "http://192.0.2.1:8765/v1/core",
        "http://192.0.2.1:8765/v1/core?raw=1",
        "http://192.0.2.1:8765/v1/feed-events",
    ]
    assert all(
        call.args[0].get_header("Authorization") == "Bearer synthetic-test-token"
        for call in urlopen.call_args_list
    )


def test_client_parses_version_update_status_and_update_submit_result():
    responses = [
        FakeResponse(
            {
                "ok": True,
                "version": "1.2.3",
                "api_version": 1,
                "update_api_version": 1,
                "platform": "linux-armv7-eabihf",
            }
        ),
        FakeResponse(
            {
                "ok": True,
                "status": "idle",
                "reason": "none",
                "candidate_version": None,
                "previous_version": None,
            }
        ),
        FakeResponse(
            {
                "ok": True,
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "1.2.3",
                "previous_version": "1.2.2",
            }
        ),
    ]
    client = StateAgentClient(
        "http://192.0.2.1:8765", "synthetic-test-token", timeout_seconds=1
    )

    with patch("state_agent.request.urlopen", side_effect=responses) as urlopen:
        version = client.version()
        assert version.version == "1.2.3"
        assert version.platform == "linux-armv7-eabihf"
        status = client.update_status()
        assert status.status == "idle"
        assert status.candidate_version is None
        result = client.submit_update(b"PLAFOTA1" + b"x" * 64)
        assert result.accepted is True
        assert result.status == "pending"

    assert [call.args[0].full_url for call in urlopen.call_args_list] == [
        "http://192.0.2.1:8765/v1/version",
        "http://192.0.2.1:8765/v1/update-status",
        "http://192.0.2.1:8765/v1/update",
    ]
    post_request = urlopen.call_args_list[2].args[0]
    assert post_request.get_method() == "POST"
    header_pairs = {key.lower(): value for key, value in post_request.header_items()}
    assert header_pairs.get("content-type") == "application/octet-stream"
    assert [call.kwargs["timeout"] for call in urlopen.call_args_list] == [1, 1, 30]


def test_client_parses_and_invokes_fixed_compatibility_operations():
    payload = {
        "ok": True,
        "asset_version": "0.4.0",
        "original_ping_path": "/bin/ping",
        "supported": True,
        "deployed": True,
        "shim_current": True,
        "startup_current": True,
        "backup_available": True,
        "enabled": False,
        "active": False,
        "reboot_required": False,
        "last_reason": "already_current",
    }
    client = StateAgentClient(
        "http://192.0.2.1:8765", "synthetic-test-token", timeout_seconds=1
    )
    responses = [FakeResponse(payload) for _ in range(5)]

    with patch("state_agent.request.urlopen", side_effect=responses) as urlopen:
        assert client.compatibility_status().deployed is True
        assert client.compatibility_deploy().last_reason == "already_current"
        assert client.compatibility_enable().supported is True
        assert client.compatibility_disable().enabled is False
        assert client.compatibility_restore_startup().startup_current is True

    assert [call.args[0].full_url for call in urlopen.call_args_list] == [
        "http://192.0.2.1:8765/v1/compatibility",
        "http://192.0.2.1:8765/v1/compatibility/deploy",
        "http://192.0.2.1:8765/v1/compatibility/enable",
        "http://192.0.2.1:8765/v1/compatibility/disable",
        "http://192.0.2.1:8765/v1/compatibility/restore-startup",
    ]
    assert [call.args[0].method for call in urlopen.call_args_list] == [
        "GET",
        "POST",
        "POST",
        "POST",
        "POST",
    ]
