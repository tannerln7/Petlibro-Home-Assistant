import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path
from urllib import error, request

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_DIR = REPO_ROOT / "state-agent"
sys.path.insert(0, str(REPO_ROOT / "addon" / "appdaemon" / "src"))

from settings_map import SETTING_COMMANDS

TOKEN = "synthetic-agent-test-token"
PLATFORM_ID = "linux-armv7-eabihf"
LOCAL_URL_OPENER = request.build_opener(request.ProxyHandler({}))


def _fnv64(data, initial=14695981039346656037):
    value = initial
    for byte in data:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def _settings_revision(state):
    ranges = (
        (0x09, 1),
        (0x10, 4),
        (0x15, 8),
        (0x21, 8),
        (0x2C, 100),
        (0x91, 17),
        (0xA3, 20),
        (0xB8, 17),
        (0xCA, 13),
        (0xE7, 1),
    )
    revision_input = bytearray()
    for offset, width in ranges:
        revision_input.extend((offset, width))
        revision_input.extend(state[offset : offset + width])
    return f"fnv64:{_fnv64(revision_input):016x}"


@pytest.fixture(scope="session")
def state_agent_build(tmp_path_factory):
    build_root = tmp_path_factory.mktemp("state-agent-build")
    build_tree = build_root / "state-agent"
    shutil.copytree(AGENT_DIR, build_tree)

    private_key = build_root / "test-ed25519-private.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(private_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    pub_der = subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-outform",
            "DER",
        ],
        check=True,
        capture_output=True,
    ).stdout
    public_key_hex = pub_der[-32:].hex()
    (build_tree / "release-public-key.hex").write_text(
        public_key_hex + "\n", encoding="ascii"
    )

    subprocess.run(
        ["make", "clean", "all"],
        cwd=build_tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        "binary": build_tree / "plaf203-state-agent",
        "helper": build_tree / "plaf203-update-fs",
        "private_key": private_key,
        "agent_version": (build_tree / "VERSION").read_text(encoding="ascii").strip(),
    }


@pytest.fixture(scope="session")
def state_agent_binary(state_agent_build):
    return state_agent_build["binary"]


@pytest.fixture(scope="session")
def ota_signing_private_key(state_agent_build):
    return state_agent_build["private_key"]


@pytest.fixture(scope="session")
def state_agent_version(state_agent_build):
    return state_agent_build["agent_version"]


def _sign_manifest(private_key, manifest_bytes):
    payload_file = private_key.parent / "manifest.payload"
    payload_file.write_bytes(manifest_bytes)
    signature = subprocess.run(
        [
            "openssl",
            "pkeyutl",
            "-provider",
            "default",
            "-sign",
            "-inkey",
            str(private_key),
            "-rawin",
            "-in",
            str(payload_file),
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert len(signature) == 64
    return signature


def _minimal_arm_elf(marker=0):
    elf = bytearray(128)
    elf[0:4] = b"\x7fELF"
    elf[4] = 1
    elf[5] = 1
    elf[6] = 1
    struct.pack_into("<H", elf, 16, 2)
    struct.pack_into("<H", elf, 18, 40)
    struct.pack_into("<I", elf, 20, 1)
    struct.pack_into("<H", elf, 40, 52)
    elf[64] = marker
    return bytes(elf)


def _update_manifest(version, artifact):
    return {
        "schema_version": 1,
        "product": "plaf203-state-agent",
        "channel": "stable",
        "version": version,
        "api_version": 1,
        "update_api_version": 1,
        "platform": PLATFORM_ID,
        "artifact": {
            "url": "https://example.invalid/plaf203-state-agent.bin",
            "sha256": hashlib.sha256(artifact).hexdigest(),
            "size": len(artifact),
        },
        "release_url": "https://example.invalid/releases/0.0.0",
    }


def _frame_update_body(manifest_bytes, signature, artifact):
    parts = [
        b"PLAFOTA1",
        struct.pack(">I", len(manifest_bytes)),
        struct.pack(">I", len(signature)),
        struct.pack(">I", len(artifact)),
        manifest_bytes,
        signature,
        artifact,
    ]
    return b"".join(parts)


def _post_raw(port, path, headers, body):
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        lines = [f"POST {path} HTTP/1.1", "Host: 127.0.0.1", "Connection: close"]
        for key, value in headers.items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("")
        sock.sendall("\r\n".join(lines).encode("ascii") + body)
        response = bytearray()
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response.extend(chunk)

    header, _, payload = bytes(response).partition(b"\r\n\r\n")
    status_line = header.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    status = int(status_line.split(" ")[1])
    parsed = json.loads(payload.decode("utf-8")) if payload else {}
    return status, parsed


def _state_bytes(size=236):
    state = bytearray(size)
    if size >= 0xE8:
        state[0x09] = 0
        state[0x10] = 50
        state[0x11] = 1
        state[0x12] = 20
        state[0x15] = 1
        state[0x16] = 1
        state[0x21] = 1
        state[0x22] = 1
        state[0x91] = 1
        struct.pack_into("<I", state, 0x92, 1)
        struct.pack_into("<I", state, 0x9A, 1)
        struct.pack_into("<I", state, 0x9E, 1)
        state[0xA3] = 1
        struct.pack_into("<I", state, 0xA4, 1)
        struct.pack_into("<I", state, 0xA8, 1)
        state[0xB8] = 1
        struct.pack_into("<I", state, 0xB9, 1)
        struct.pack_into("<I", state, 0xC1, 7)
        struct.pack_into("<I", state, 0xC5, 1)
        state[0xCA] = 1
        struct.pack_into("<I", state, 0xCB, 1)
        struct.pack_into("<I", state, 0xD3, 80)
    return state


def _plan_record(
    *,
    plan_id=1,
    minute=0,
    hour=11,
    one_shot=0,
    days=(1, 2, 3, 4, 5, 6, 7),
    enable_audio=1,
    audio_times=2,
    portions=10,
    execution_state=0,
    sync_time=0,
    skip_end_time=0,
    opaque=b"\x00" * 10,
):
    record = bytearray(47)
    struct.pack_into("<I", record, 0x00, plan_id)
    record[0x04] = minute
    record[0x05] = hour
    record[0x06] = one_shot
    record[0x07 : 0x07 + len(days)] = bytes(days)
    record[0x0E] = enable_audio
    record[0x0F] = audio_times
    record[0x10] = portions
    struct.pack_into("<I", record, 0x11, execution_state)
    struct.pack_into("<Q", record, 0x15, sync_time)
    struct.pack_into("<Q", record, 0x1D, skip_end_time)
    record[0x25:0x2F] = opaque
    return record


def _feed_phase(
    *,
    plan_id,
    finished,
    retried,
    phase_status,
    type_raw,
    actual,
    expected,
    exec_time,
    error_code=0,
):
    record = bytearray(31)
    struct.pack_into("<I", record, 0x00, plan_id)
    record[0x08] = finished
    record[0x09] = retried
    record[0x0A] = phase_status
    record[0x0B] = type_raw
    record[0x0C] = actual
    record[0x0D] = expected
    struct.pack_into("<Q", record, 0x0E, exec_time)
    struct.pack_into("<I", record, 0x1A, error_code)
    return record


def _write_snapshot(
    root,
    *,
    state=None,
    plans=(),
    feed=None,
    head=0,
    tail=0,
    error_feed=None,
    err_head=255,
    err_tail=255,
):
    (root / "attr").mkdir(parents=True)
    (root / "feed_plan").mkdir(parents=True)
    (root / "rtc").mkdir(parents=True)
    (root / "attr" / "state.bin").write_bytes(
        bytes(_state_bytes() if state is None else state)
    )
    (root / "feed_plan" / "index.bin").write_bytes(bytes([len(plans)]))
    (root / "feed_plan" / "plan.bin").write_bytes(b"".join(plans))
    (root / "feed_plan" / "rec_index_head.bin").write_bytes(bytes([head]))
    (root / "feed_plan" / "rec_index_tail.bin").write_bytes(bytes([tail]))
    (root / "feed_plan" / "err_rec_index_head.bin").write_bytes(bytes([err_head]))
    (root / "feed_plan" / "err_rec_index_tail.bin").write_bytes(bytes([err_tail]))
    (root / "feed_plan" / "feed_rec.bin").write_bytes(
        bytes(4743) if feed is None else bytes(feed)
    )
    (root / "feed_plan" / "err_feed_rec.bin").write_bytes(
        bytes(4743) if error_feed is None else bytes(error_feed)
    )
    (root / "rtc" / "rtc_time.bin").write_bytes(b"\x00" * 8)


class RunningAgent:
    def __init__(
        self,
        binary,
        root,
        *,
        allow_ip=None,
        socket_timeout_seconds=None,
        startup_script=None,
        compat_active_marker=None,
    ):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base_url = f"http://127.0.0.1:{self.port}"
        args = [
                str(binary),
                "--root",
                str(root),
                "--listen",
                f"127.0.0.1:{self.port}",
                "--token",
                TOKEN,
            ]
        if allow_ip is not None:
            args.extend(["--allow-ip", allow_ip])
        if socket_timeout_seconds is not None:
            args.extend(["--socket-timeout-seconds", str(socket_timeout_seconds)])
        if startup_script is not None:
            args.extend(["--startup-script", str(startup_script)])
        if compat_active_marker is not None:
            args.extend(["--compat-active-marker", str(compat_active_marker)])
        self.process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                self.get("/health")
                break
            except OSError:
                time.sleep(0.02)
        else:
            self.close()
            raise AssertionError("state agent did not start")

    def get(self, path):
        req = request.Request(
            self.base_url + path,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with LOCAL_URL_OPENER.open(req, timeout=1) as response:
            return json.load(response)

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def test_state_decoder_uses_persistent_switches_and_correct_authority_classes(
    state_agent_binary, tmp_path
):
    state = _state_bytes()
    state[0x0E] = 3
    state[0x14] = 0
    state[0x15] = 1
    state[0x20] = 0
    state[0x21] = 1
    state[0x90] = 0
    state[0x91] = 1
    state[0xA2] = 0
    state[0xA3] = 1
    state[0xB7] = 0
    state[0xB8] = 1
    state[0xC9] = 0
    state[0xCA] = 1
    _write_snapshot(tmp_path, state=state)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        core = agent.get("/v1/core?raw=1")

    settings = core["settings"]
    assert settings["motor_state_raw"] == 3
    assert "sound_enable_or_mode" not in settings
    assert settings["sound_effective_cached"] == "disabled"
    assert settings["sound_switch"] == "enabled"
    assert settings["light_effective_cached"] == "disabled"
    assert settings["light_switch"] == "enabled"
    assert settings["camera_effective_cached"] == "disabled"
    assert settings["camera_switch"] == "enabled"
    assert settings["video_record_effective_cached"] == "disabled"
    assert settings["video_record_switch"] == "enabled"
    assert settings["motion_detection_effective_cached"] == "disabled"
    assert settings["motion_detection_switch"] == "enabled"
    assert settings["sound_detection_effective_cached"] == "disabled"
    assert settings["sound_detection_switch"] == "enabled"
    assert "sound_switch" in core["setting_classes"]["persistent"]
    assert "sound_effective_cached" in core["setting_classes"]["effective_cached"]
    assert "motor_state_raw" in core["setting_classes"]["runtime"]


@pytest.mark.parametrize(
    ("raw", "kind", "enabled"),
    [(0, "disabled", "disabled"), (1, "builtin", "enabled"), (2, "custom", "enabled")],
)
def test_feeding_audio_subtypes_remain_distinguishable(
    state_agent_binary, tmp_path, raw, kind, enabled
):
    state = _state_bytes()
    state[0x13] = raw
    _write_snapshot(tmp_path, state=state)
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        settings = agent.get("/v1/core")["settings"]
    assert settings["feeding_audio_type"] == kind
    assert settings["feeding_audio_enabled"] == enabled


def test_unaligned_u32_fields_use_all_four_bytes_and_minutes_are_raw(
    state_agent_binary, tmp_path
):
    state = _state_bytes()
    for offset in (0x92, 0x9A, 0x9E, 0xA4, 0xA8, 0xB9, 0xC1, 0xC5, 0xCB, 0xD3):
        struct.pack_into("<I", state, offset, 0x01000001 | offset)
    state[0xB3:0xB6] = bytes([17, 29, 43])
    _write_snapshot(tmp_path, state=state)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        core = agent.get("/v1/core?raw=1")

    raw = core["settings_raw"]
    assert raw["camera_mode_u32le_0x92"] == struct.unpack_from("<I", state, 0x92)[0]
    assert raw["resolution_u32le_0x9a"] == struct.unpack_from("<I", state, 0x9A)[0]
    assert raw["night_vision_u32le_0x9e"] == struct.unpack_from("<I", state, 0x9E)[0]
    assert (
        raw["video_record_mode_u32le_0xa4"] == struct.unpack_from("<I", state, 0xA4)[0]
    )
    assert (
        raw["video_record_schedule_mode_u32le_0xa8"]
        == struct.unpack_from("<I", state, 0xA8)[0]
    )
    assert (
        raw["motion_detection_mode_u32le_0xb9"]
        == struct.unpack_from("<I", state, 0xB9)[0]
    )
    assert (
        raw["motion_sensitivity_u32le_0xc1"] == struct.unpack_from("<I", state, 0xC1)[0]
    )
    assert raw["motion_range_u32le_0xc5"] == struct.unpack_from("<I", state, 0xC5)[0]
    assert (
        raw["sound_detection_mode_u32le_0xcb"]
        == struct.unpack_from("<I", state, 0xCB)[0]
    )
    assert (
        raw["sound_detection_sensitivity_u32le_0xd3"]
        == struct.unpack_from("<I", state, 0xD3)[0]
    )
    assert core["settings"]["before_feeding_plan_minutes"] == 17
    assert core["settings"]["automatic_recording_minutes"] == 29
    assert core["settings"]["after_manual_feeding_minutes"] == 43


def test_core_decode_and_revision_are_derived_from_the_same_raw_snapshot(
    state_agent_binary, tmp_path
):
    state = _state_bytes()
    state[0x10] = 73
    state[0x21] = 0
    struct.pack_into("<I", state, 0xA4, 2)
    _write_snapshot(tmp_path, state=state)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        core = agent.get("/v1/core?raw=1")

    raw_state = bytes.fromhex(core["raw"]["attr/state.bin"]["hex"])
    assert raw_state == bytes(state)
    assert core["settings"]["volume"] == raw_state[0x10]
    assert core["settings"]["sound_switch"] == "disabled"
    assert core["settings"]["local_camera_recording_type"] == "motion_detection"
    assert core["revisions"]["settings_rev"] == _settings_revision(raw_state)

    persistent = set(core["setting_classes"]["persistent"])
    assert {spec.state_field for spec in SETTING_COMMANDS} <= persistent


@pytest.mark.parametrize("size", [235, 237])
def test_state_bin_requires_exact_236_byte_snapshot(state_agent_binary, tmp_path, size):
    _write_snapshot(tmp_path, state=_state_bytes(size))
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        health = agent.get("/health")
        core = agent.get("/v1/core")
    assert health["ok"] is False
    assert health["state_decode"]["actual_size"] == size
    assert core["ok"] is False
    assert core["expected_size"] == 236


def test_plan_decoder_models_full_47_byte_record(state_agent_binary, tmp_path):
    plan = _plan_record(
        plan_id=0x01020304,
        minute=37,
        hour=22,
        one_shot=1,
        days=(),
        enable_audio=1,
        audio_times=4,
        portions=13,
        execution_state=0xA1B2C3D4,
        sync_time=0x0102030405060708,
        skip_end_time=0x1112131415161718,
        opaque=bytes(range(10)),
    )
    _write_snapshot(tmp_path, plans=(plan,))

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        core = agent.get("/v1/core?raw=1")

    decoded = core["plans"]["semantic_records"][0]
    assert core["plans"]["record_size"] == 47
    assert decoded["id"] == 0x01020304
    assert decoded["one_shot"] is True
    assert decoded["execution_state"] == 0xA1B2C3D4
    assert decoded["sync_time"] == 0x0102030405060708
    assert decoded["skip_end_time"] == 0x1112131415161718
    assert decoded["opaque_hex"].replace(" ", "") == bytes(range(10)).hex()


def test_feed_event_queue_uses_51_slots_three_phases_u64_and_wraparound(
    state_agent_binary, tmp_path
):
    feed = bytearray(4743)
    for order, slot_index in enumerate((50, 0)):
        for phase in range(3):
            record = _feed_phase(
                plan_id=0x01020304 + order,
                finished=phase == 1,
                retried=phase == 2,
                phase_status=8 + phase,
                type_raw=phase + 1,
                actual=9 + phase,
                expected=12,
                exec_time=0x0102030405060708 + phase,
                error_code=0xAABBCCDD if phase == 2 else 0,
            )
            start = slot_index * 93 + phase * 31
            feed[start : start + 31] = record
    _write_snapshot(tmp_path, feed=feed, head=49, tail=0)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        result = agent.get("/v1/feed-events?raw=1")

    assert result["semantics"] == "pending_outbound_events_not_history"
    assert [event["slot_index"] for event in result["events"]] == [50, 0]
    assert [event["source_ring"] for event in result["events"]] == [
        "normal",
        "normal",
    ]
    assert result["snapshot_consistent"] is True
    assert result["normal_ring_valid"] is True
    phases = result["events"][0]["phases"]
    assert [phase["phase"] for phase in phases] == [
        "GRAIN_START",
        "GRAIN_END",
        "GRAIN_BLOCKING",
    ]
    assert phases[0]["exec_time"] == 0x0102030405060708
    assert [phase["type"] for phase in phases] == [
        "scheduled",
        "remote_manual",
        "local_button_manual",
    ]
    assert phases[2]["error_code"] == 0xAABBCCDD


def test_feed_event_error_ring_is_separate_and_uses_head_exclusive_tail_inclusive(
    state_agent_binary, tmp_path
):
    normal = bytearray(4743)
    error = bytearray(4743)
    normal[1 * 93 : 1 * 93 + 31] = _feed_phase(
        plan_id=1,
        finished=False,
        retried=False,
        phase_status=1,
        type_raw=1,
        actual=0,
        expected=1,
        exec_time=1000,
    )
    error[6 * 93 : 6 * 93 + 31] = _feed_phase(
        plan_id=2,
        finished=True,
        retried=False,
        phase_status=1,
        type_raw=2,
        actual=0,
        expected=1,
        exec_time=2000,
        error_code=2003,
    )
    _write_snapshot(
        tmp_path,
        feed=normal,
        head=0,
        tail=1,
        error_feed=error,
        err_head=5,
        err_tail=6,
    )

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        result = agent.get("/v1/feed-events")

    assert [event["slot_index"] for event in result["events"]] == [1]
    assert result["events"][0]["source_ring"] == "normal"
    assert [event["slot_index"] for event in result["error_events"]] == [6]
    assert result["error_events"][0]["source_ring"] == "error"
    assert result["error_events"][0]["phases"][0]["error_code"] == 2003
    assert result["error_ring_valid"] is True


def test_feed_event_invalid_indices_are_reported_without_iteration(
    state_agent_binary, tmp_path
):
    _write_snapshot(tmp_path, head=255, tail=1, err_head=0, err_tail=255)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        result = agent.get("/v1/feed-events")

    assert result["normal_ring_valid"] is False
    assert result["error_ring_valid"] is False
    assert result["queue"]["valid"] is False
    assert result["err_queue"]["valid"] is False
    assert result["events"] == []
    assert result["error_events"] == []


def _compatibility_fixture(tmp_path):
    data = tmp_path / "user" / "data"
    data.mkdir(parents=True)
    _write_snapshot(data)
    test_ping = data / "test-system" / "bin" / "ping"
    test_ping.parent.mkdir(parents=True)
    test_ping.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    test_ping.chmod(0o755)
    startup = tmp_path / "user" / "app_start.sh"
    startup.write_bytes((AGENT_DIR / "managed" / "app_start.legacy.sh").read_bytes())
    startup.chmod(0o755)
    active = tmp_path / "ping-shim-active"
    return data, startup, active


def test_compatibility_deploy_is_idempotent_opt_in_and_recoverable(
    state_agent_binary, tmp_path
):
    data, startup, active = _compatibility_fixture(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"}
    with RunningAgent(
        state_agent_binary,
        data,
        startup_script=startup,
        compat_active_marker=active,
    ) as agent:
        initial = agent.get("/v1/compatibility")
        assert initial["supported"] is True
        assert initial["original_ping_path"] == "test-system/bin/ping"
        assert initial["deployed"] is False
        assert initial["enabled"] is False

        status, deployed = _post_raw(
            agent.port, "/v1/compatibility/deploy", headers, b""
        )
        assert status == 200
        assert deployed["deployed"] is True
        assert deployed["enabled"] is False
        assert deployed["active"] is False
        assert deployed["reboot_required"] is False

        shim = data / "local-compat" / "bin" / "ping"
        backup = data / "local-compat" / "backup" / "app_start.sh"
        assert shim.read_bytes() == (AGENT_DIR / "managed" / "ping").read_bytes()
        assert startup.read_bytes() == (
            AGENT_DIR / "managed" / "app_start.sh"
        ).read_bytes()
        assert backup.read_bytes() == (
            AGENT_DIR / "managed" / "app_start.legacy.sh"
        ).read_bytes()
        assert shim.stat().st_mode & 0o777 == 0o755
        assert startup.stat().st_mode & 0o777 == 0o755
        assert backup.stat().st_mode & 0o777 == 0o700

        first_mtimes = (shim.stat().st_mtime_ns, startup.stat().st_mtime_ns)
        status, deployed_again = _post_raw(
            agent.port, "/v1/compatibility/deploy", headers, b""
        )
        assert status == 200
        assert deployed_again["last_reason"] == "already_current"
        assert first_mtimes == (shim.stat().st_mtime_ns, startup.stat().st_mtime_ns)

        shim.chmod(0o700)
        stale_stage = shim.with_name("ping.managed.tmp")
        stale_stage.write_text("interrupted", encoding="ascii")
        status, repaired = _post_raw(
            agent.port, "/v1/compatibility/deploy", headers, b""
        )
        assert status == 200
        assert repaired["deployed"] is True
        assert shim.stat().st_mode & 0o777 == 0o755
        assert not stale_stage.exists()

        status, enabled = _post_raw(
            agent.port, "/v1/compatibility/enable", headers, b""
        )
        assert status == 200
        assert enabled["enabled"] is True
        assert enabled["active"] is False
        assert enabled["reboot_required"] is True

        active.touch()
        active_status = agent.get("/v1/compatibility")
        assert active_status["active"] is True
        assert active_status["reboot_required"] is False

        status, disabled = _post_raw(
            agent.port, "/v1/compatibility/disable", headers, b""
        )
        assert status == 200
        assert disabled["enabled"] is False
        assert disabled["active"] is True
        assert disabled["reboot_required"] is True

        status, restored = _post_raw(
            agent.port, "/v1/compatibility/restore-startup", headers, b""
        )
        assert status == 200
        assert restored["deployed"] is False
        assert restored["active"] is True
        assert restored["reboot_required"] is True
        assert startup.read_bytes() == backup.read_bytes()

        active.unlink()
        after_reboot = agent.get("/v1/compatibility")
        assert after_reboot["active"] is False
        assert after_reboot["reboot_required"] is False


def test_compatibility_deploy_rejects_unrecognized_startup_without_replacing_it(
    state_agent_binary, tmp_path
):
    data, startup, active = _compatibility_fixture(tmp_path)
    startup.write_text("#!/bin/sh\n# user modified\n", encoding="ascii")
    startup.chmod(0o755)
    original = startup.read_bytes()
    with RunningAgent(
        state_agent_binary,
        data,
        startup_script=startup,
        compat_active_marker=active,
    ) as agent:
        status, response = _post_raw(
            agent.port,
            "/v1/compatibility/deploy",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"},
            b"",
        )

    assert status == 409
    assert response["error"] == "startup_predecessor_unrecognized"
    assert startup.read_bytes() == original
    assert not (data / "local-compat" / "bin" / "ping").exists()


def test_compatibility_deploy_requires_a_verified_original_ping(
    state_agent_binary, tmp_path
):
    data, startup, active = _compatibility_fixture(tmp_path)
    (data / "test-system" / "bin" / "ping").unlink()
    with RunningAgent(
        state_agent_binary,
        data,
        startup_script=startup,
        compat_active_marker=active,
    ) as agent:
        initial = agent.get("/v1/compatibility")
        status, response = _post_raw(
            agent.port,
            "/v1/compatibility/deploy",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"},
            b"",
        )

    assert initial["supported"] is False
    assert initial["original_ping_path"] is None
    assert status == 409
    assert response["error"] == "original_ping_not_verified"
    assert not (data / "local-compat").exists()


def test_original_ping_resolution_rejects_delegation_back_to_managed_shim(
    state_agent_binary, tmp_path
):
    data, startup, active = _compatibility_fixture(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"}
    with RunningAgent(
        state_agent_binary,
        data,
        startup_script=startup,
        compat_active_marker=active,
    ) as agent:
        status, _ = _post_raw(
            agent.port, "/v1/compatibility/deploy", headers, b""
        )
        assert status == 200
        shim = data / "local-compat" / "bin" / "ping"
        test_ping = data / "test-system" / "bin" / "ping"
        test_ping.unlink()
        test_ping.symlink_to(shim)

        detected = agent.get("/v1/compatibility")
        enable_status, enable_response = _post_raw(
            agent.port, "/v1/compatibility/enable", headers, b""
        )

    assert detected["supported"] is False
    assert detected["original_ping_path"] is None
    assert enable_status == 409
    assert enable_response["error"] == "compatibility_files_not_ready"


@pytest.mark.parametrize(
    "operation", ("deploy", "enable", "disable", "restore-startup")
)
def test_compatibility_mutations_are_blocked_during_state_agent_update(
    state_agent_binary, tmp_path, operation
):
    data, startup, active = _compatibility_fixture(tmp_path)
    update_dir = data / "local-state-agent" / "update"
    update_dir.mkdir(parents=True)
    (update_dir / "status.json").write_text(
        json.dumps(
            {
                "status": "activating",
                "reason": "pre_swap",
                "candidate_version": "0.4.0",
                "previous_version": "0.3.2",
            }
        ),
        encoding="utf-8",
    )
    original_startup = startup.read_bytes()

    with RunningAgent(
        state_agent_binary,
        data,
        startup_script=startup,
        compat_active_marker=active,
    ) as agent:
        status, response = _post_raw(
            agent.port,
            f"/v1/compatibility/{operation}",
            {"Authorization": f"Bearer {TOKEN}", "Content-Length": "0"},
            b"",
        )

    assert status == 409
    assert response["error"] == "state_agent_update_in_progress"
    assert startup.read_bytes() == original_startup
    assert not (data / "local-compat").exists()


def test_managed_ping_shim_fails_oem_hosts_quickly_and_delegates_other_hosts():
    shim = AGENT_DIR / "managed" / "ping"
    for host in (
        "aws.amazon.com",
        "www.cloudflare.com",
        "baidu.com",
        "114.114.114.114",
    ):
        started = time.monotonic()
        result = subprocess.run(
            ["/bin/sh", str(shim), host, "-c", "10"],
            capture_output=True,
            timeout=1,
        )
        assert result.returncode != 0
        assert result.stdout == b""
        assert time.monotonic() - started < 0.25

    delegated = subprocess.run(
        ["/bin/sh", str(shim), "127.0.0.1", "-c", "1"],
        capture_output=True,
        timeout=2,
    )
    assert delegated.returncode == 0
    assert b"127.0.0.1" in delegated.stdout

    recursion_guard = subprocess.run(
        ["/bin/sh", str(shim), "127.0.0.1", "-c", "1"],
        capture_output=True,
        timeout=1,
        env={**os.environ, "PLAF203_PING_SHIM_DELEGATING": "1"},
    )
    assert recursion_guard.returncode == 127
    assert recursion_guard.stdout == b""

    # The same destinations with any non-OEM shape must reach the original
    # executable unchanged. Invalid count/deadline values make this local,
    # deterministic, and independent of DNS or network access.
    for arguments in (
        ["114.114.114.114", "-c", "0"],
        ["-c", "0", "114.114.114.114"],
        ["114.114.114.114", "-c", "10", "--plaf203-test-invalid"],
    ):
        original = subprocess.run(
            ["/bin/ping", *arguments], capture_output=True, timeout=1
        )
        through_shim = subprocess.run(
            ["/bin/sh", str(shim), *arguments], capture_output=True, timeout=1
        )
        assert through_shim.returncode == original.returncode
        assert through_shim.stdout == original.stdout
        assert through_shim.stderr == original.stderr


def test_version_and_update_status_endpoints(
    state_agent_binary, state_agent_version, tmp_path
):
    _write_snapshot(tmp_path)
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        version = agent.get("/v1/version")
        status = agent.get("/v1/update-status")

    assert version == {
        "ok": True,
        "version": state_agent_version,
        "api_version": 1,
        "update_api_version": 1,
        "platform": PLATFORM_ID,
    }
    assert status["ok"] is True
    assert status["status"] == "idle"
    assert status["reason"] == "none"
    assert status["candidate_version"] is None
    assert status["previous_version"] is None



def test_authenticated_loopback_bypasses_remote_source_allowlist(
    state_agent_binary, tmp_path
):
    _write_snapshot(tmp_path)

    # The production service permits HA/AppDaemon as its remote source, while
    # the feeder-local update supervisor must also be able to probe 127.0.0.1.
    with RunningAgent(
        state_agent_binary,
        tmp_path,
        allow_ip="192.0.2.10",
    ) as agent:
        assert agent.get("/health")["ok"] is True
        assert agent.get("/v1/version")["ok"] is True

        bad_token = request.Request(
            agent.base_url + "/health",
            headers={"Authorization": "Bearer definitely-wrong"},
        )
        with pytest.raises(error.HTTPError) as auth_error:
            LOCAL_URL_OPENER.open(bad_token, timeout=1)

    assert auth_error.value.code == 401


def test_disallowed_source_is_closed_before_http_parsing(state_agent_binary, tmp_path):
    _write_snapshot(tmp_path)

    with RunningAgent(
        state_agent_binary,
        tmp_path,
        allow_ip="192.0.2.10",
    ) as agent:
        with socket.socket() as client:
            client.settimeout(1)
            client.bind(("127.0.0.2", 0))
            client.connect(("127.0.0.1", agent.port))
            client.sendall(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            try:
                assert client.recv(1) == b""
            except ConnectionResetError:
                pass


def test_routes_require_exact_path_and_raw_query_is_exact_match(
    state_agent_binary, tmp_path
):
    _write_snapshot(tmp_path)
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        exact = agent.get("/v1/core?raw=1")
        query_substring = agent.get("/v1/core?foo=raw=1")

        missing = request.Request(
            agent.base_url + "/v1/versionanything",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with pytest.raises(error.HTTPError) as version_err:
            LOCAL_URL_OPENER.open(missing, timeout=1)

        missing_health = request.Request(
            agent.base_url + "/healthx",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        with pytest.raises(error.HTTPError) as health_err:
            LOCAL_URL_OPENER.open(missing_health, timeout=1)

    assert "raw" in exact
    assert "raw" not in query_substring
    assert version_err.value.code == 404
    assert health_err.value.code == 404


def test_update_rejects_missing_authentication(state_agent_binary, tmp_path):
    _write_snapshot(tmp_path)
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status, body = _post_raw(
            agent.port,
            "/v1/update",
            {"Content-Type": "application/octet-stream", "Content-Length": "0"},
            b"",
        )
    assert status == 401
    assert body["ok"] is False


def test_update_rejects_invalid_framing_and_chunked_body(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    manifest = _update_manifest("9.9.9", artifact)
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign_manifest(ota_signing_private_key, manifest_bytes)
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status_chunked, body_chunked = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Transfer-Encoding": "chunked",
            },
            framed,
        )
        status_bad_magic, body_bad_magic = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            b"BADMAGIC" + framed[8:],
        )

    assert status_chunked == 400
    assert body_chunked["ok"] is False
    assert status_bad_magic == 400
    assert body_bad_magic["ok"] is False


def test_update_rejects_invalid_signature_before_manifest_parse(
    state_agent_binary, tmp_path
):
    _write_snapshot(tmp_path)
    manifest_bytes = b"{not-json}"
    artifact = _minimal_arm_elf()
    signature = b"\x00" * 64
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status, body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            framed,
        )

    assert status == 401
    assert body["error"] == "manifest signature invalid"


def test_update_signature_interop_and_tamper_rejection(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    manifest = _update_manifest("9.9.9", artifact)
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign_manifest(ota_signing_private_key, manifest_bytes)
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    modified_manifest = bytearray(manifest_bytes)
    modified_manifest[-2] = ord("8")
    modified_manifest_framed = _frame_update_body(
        bytes(modified_manifest), signature, artifact
    )

    modified_signature = bytearray(signature)
    modified_signature[0] ^= 0x01
    modified_signature_framed = _frame_update_body(
        manifest_bytes, bytes(modified_signature), artifact
    )

    other_key = tmp_path / "wrong-ed25519.pem"
    subprocess.run(
        ["openssl", "genpkey", "-algorithm", "Ed25519", "-out", str(other_key)],
        check=True,
        capture_output=True,
        text=True,
    )
    wrong_sig = _sign_manifest(other_key, manifest_bytes)
    wrong_key_framed = _frame_update_body(manifest_bytes, wrong_sig, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        ok_status, ok_body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            framed,
        )
        modified_manifest_status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(modified_manifest_framed)),
            },
            modified_manifest_framed,
        )
        modified_sig_status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(modified_signature_framed)),
            },
            modified_signature_framed,
        )
        wrong_key_status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(wrong_key_framed)),
            },
            wrong_key_framed,
        )

        status_doc = agent.get("/v1/update-status")

    assert ok_status == 202
    assert ok_body["status"] == "pending"
    assert modified_manifest_status == 401
    assert modified_sig_status == 401
    assert wrong_key_status == 401
    assert status_doc["status"] == "pending"


def test_update_rejects_signed_manifest_schema_violation(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    manifest = _update_manifest("9.9.9", artifact)
    manifest["platform"] = "linux-armv6-eabihf"
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign_manifest(ota_signing_private_key, manifest_bytes)
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status, body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            framed,
        )
    assert status == 400
    assert body["error"] == "manifest validation failed"


def test_update_rejects_hash_mismatch_and_version_downgrade(
    state_agent_binary, ota_signing_private_key, state_agent_version, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()

    mismatch_manifest = _update_manifest("9.9.9", artifact)
    mismatch_manifest["artifact"]["sha256"] = "0" * 64
    mismatch_bytes = json.dumps(
        mismatch_manifest, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    mismatch_sig = _sign_manifest(ota_signing_private_key, mismatch_bytes)
    mismatch_framed = _frame_update_body(mismatch_bytes, mismatch_sig, artifact)

    same_manifest = _update_manifest(state_agent_version, artifact)
    same_bytes = json.dumps(
        same_manifest, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    same_sig = _sign_manifest(ota_signing_private_key, same_bytes)
    same_framed = _frame_update_body(same_bytes, same_sig, artifact)

    old_manifest = _update_manifest("0.0.1", artifact)
    old_bytes = json.dumps(old_manifest, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    old_sig = _sign_manifest(ota_signing_private_key, old_bytes)
    old_framed = _frame_update_body(old_bytes, old_sig, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        mismatch_status, mismatch_body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(mismatch_framed)),
            },
            mismatch_framed,
        )
        same_status, same_body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(same_framed)),
            },
            same_framed,
        )
        old_status, old_body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(old_framed)),
            },
            old_framed,
        )

    assert mismatch_status == 400
    assert mismatch_body["error"] == "artifact hash mismatch"
    assert same_status == 409
    assert same_body["error"] == "update version equals installed version"
    assert old_status == 409
    assert old_body["error"] == "downgrade rejected"


def test_update_serializes_pending_activating_and_locked_transactions(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)

    def framed(version):
        artifact = _minimal_arm_elf(2)
        manifest_bytes = json.dumps(
            _update_manifest(version, artifact), separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        return _frame_update_body(
            manifest_bytes,
            _sign_manifest(ota_signing_private_key, manifest_bytes),
            artifact,
        )

    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    }
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        first = framed("1.0.0")
        first_status, _ = _post_raw(
            agent.port, "/v1/update", {**headers, "Content-Length": str(len(first))}, first
        )
        second = framed("1.0.1")
        pending_status, pending_body = _post_raw(
            agent.port,
            "/v1/update",
            {**headers, "Content-Length": str(len(second))},
            second,
        )
        assert first_status == 202
        assert pending_status == 409
        assert pending_body["error"] == "update transaction already in progress"

        update_dir = tmp_path / "local-state-agent" / "update"
        (update_dir / "pending.json").unlink()
        (update_dir / "candidate.bin").unlink()
        (update_dir / "status.json").write_text(
            json.dumps(
                {
                    "status": "activating",
                    "reason": "pre_swap",
                    "candidate_version": "1.0.0",
                    "previous_version": "0.3.0",
                }
            ),
            encoding="utf-8",
        )
        activating_status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {**headers, "Content-Length": str(len(second))},
            second,
        )
        assert activating_status == 409

        (update_dir / "status.json").write_text(
            '{"status":"idle","reason":"update_applied",'
            '"candidate_version":"","previous_version":""}',
            encoding="utf-8",
        )
        lock_fd = os.open(update_dir / "transaction.lock", os.O_RDWR | os.O_CREAT)
        import fcntl

        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            locked_status, _ = _post_raw(
                agent.port,
                "/v1/update",
                {**headers, "Content-Length": str(len(second))},
                second,
            )
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
        assert locked_status == 409

        allowed_status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {**headers, "Content-Length": str(len(second))},
            second,
        )
        assert allowed_status == 202


def test_update_rejects_empty_manifest_http_urls_and_fragments(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    empty_frame = _frame_update_body(b"", b"x" * 64, artifact)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    }
    with RunningAgent(state_agent_binary, tmp_path) as agent:
        empty_status, empty_body = _post_raw(
            agent.port,
            "/v1/update",
            {**headers, "Content-Length": str(len(empty_frame))},
            empty_frame,
        )
        assert empty_status == 400
        assert empty_body["error"] == "manifest must not be empty"

        for bad_url in (
            "http://example.invalid/agent",
            "https://example.invalid/agent#fragment",
            "https://user@example.invalid/agent",
            "https://example.invalid/",
            "https://example.invalid/agent?mutable=1",
        ):
            manifest = _update_manifest("1.0.0", artifact)
            manifest["artifact"]["url"] = bad_url
            manifest_bytes = json.dumps(
                manifest, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            frame = _frame_update_body(
                manifest_bytes,
                _sign_manifest(ota_signing_private_key, manifest_bytes),
                artifact,
            )
            status, body = _post_raw(
                agent.port,
                "/v1/update",
                {**headers, "Content-Length": str(len(frame))},
                frame,
            )
            assert status == 400
            assert body["error"] == "manifest validation failed"


def test_failed_artifact_verification_leaves_no_canonical_candidate(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    }
    cases = []
    good_elf = _minimal_arm_elf()
    bad_hash_manifest = _update_manifest("1.0.0", good_elf)
    bad_hash_manifest["artifact"]["sha256"] = "0" * 64
    cases.append((bad_hash_manifest, good_elf, "artifact hash mismatch"))
    bad_elf = b"not-an-arm-elf"
    cases.append(
        (_update_manifest("1.0.0", bad_elf), bad_elf, "artifact ELF sanity check failed")
    )

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        for manifest, artifact, expected_error in cases:
            manifest_bytes = json.dumps(
                manifest, separators=(",", ":"), sort_keys=True
            ).encode("utf-8")
            frame = _frame_update_body(
                manifest_bytes,
                _sign_manifest(ota_signing_private_key, manifest_bytes),
                artifact,
            )
            status, body = _post_raw(
                agent.port,
                "/v1/update",
                {**headers, "Content-Length": str(len(frame))},
                frame,
            )
            assert status == 400
            assert body["error"] == expected_error
            update_dir = tmp_path / "local-state-agent" / "update"
            assert not (update_dir / "candidate.bin").exists()
            assert not (update_dir / "candidate.tmp").exists()


def test_socket_header_and_body_timeouts_do_not_wedge_server(
    state_agent_binary, tmp_path
):
    _write_snapshot(tmp_path)
    with RunningAgent(
        state_agent_binary, tmp_path, socket_timeout_seconds=1
    ) as agent:
        with socket.create_connection(("127.0.0.1", agent.port), timeout=2) as sock:
            sock.sendall(b"GET /health HTTP/1.1\r\n")
            time.sleep(1.3)
        assert agent.get("/health")["ok"] is True

        with socket.create_connection(("127.0.0.1", agent.port), timeout=2) as sock:
            request_head = (
                "POST /v1/update HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                f"Authorization: Bearer {TOKEN}\r\n"
                "Content-Type: application/octet-stream\r\n"
                "Content-Length: 1024\r\n\r\n"
            ).encode("ascii")
            sock.sendall(request_head + b"PLAFOTA1")
            time.sleep(1.3)
        assert agent.get("/health")["ok"] is True


def test_fragmented_valid_update_succeeds(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf(2)
    manifest_bytes = json.dumps(
        _update_manifest("1.0.0", artifact), separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    frame = _frame_update_body(
        manifest_bytes,
        _sign_manifest(ota_signing_private_key, manifest_bytes),
        artifact,
    )
    request_bytes = (
        "POST /v1/update HTTP/1.1\r\n"
        "Host: 127.0.0.1\r\n"
        f"Authorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/octet-stream\r\n"
        f"Content-Length: {len(frame)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + frame

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        with socket.create_connection(("127.0.0.1", agent.port), timeout=2) as sock:
            for offset in range(0, len(request_bytes), 7):
                sock.sendall(request_bytes[offset : offset + 7])
            response = bytearray()
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
    status_line = bytes(response).split(b"\r\n", 1)[0]
    assert status_line == b"HTTP/1.1 202 Accepted"


def test_c_semver_precedence_for_prerelease_and_build_metadata(
    state_agent_build, tmp_path
):
    binary = _build_agent_with_version(
        tmp_path / "versioned-build",
        "1.2.3-rc.2",
        state_agent_build["private_key"],
    )
    root = tmp_path / "agent-root"
    _write_snapshot(root)
    artifact = _minimal_arm_elf(2)
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/octet-stream",
    }
    cases = (
        ("1.2.3-rc.1", 409),
        ("1.2.3-rc.2+build.9", 409),
        ("1.2.3-rc.10", 202),
        ("1.2.3-rc.alpha", 202),
        ("1.2.3-rc.2.1", 202),
        ("1.2.3", 202),
    )
    with RunningAgent(binary, root) as agent:
        for version, expected_status in cases:
            update_dir = root / "local-state-agent" / "update"
            if update_dir.exists():
                for name in ("pending.json", "candidate.bin", "status.json"):
                    (update_dir / name).unlink(missing_ok=True)
            manifest_bytes = json.dumps(
                _update_manifest(version, artifact),
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            frame = _frame_update_body(
                manifest_bytes,
                _sign_manifest(state_agent_build["private_key"], manifest_bytes),
                artifact,
            )
            status, _ = _post_raw(
                agent.port,
                "/v1/update",
                {**headers, "Content-Length": str(len(frame))},
                frame,
            )
            assert status == expected_status, version

    stable_binary = _build_agent_with_version(
        tmp_path / "stable-build",
        "1.2.3+installed-alpha",
        state_agent_build["private_key"],
    )
    stable_root = tmp_path / "stable-root"
    _write_snapshot(stable_root)
    candidate_manifest = json.dumps(
        _update_manifest("1.2.3+candidate-beta", artifact),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate_frame = _frame_update_body(
        candidate_manifest,
        _sign_manifest(state_agent_build["private_key"], candidate_manifest),
        artifact,
    )
    with RunningAgent(stable_binary, stable_root) as agent:
        status, _ = _post_raw(
            agent.port,
            "/v1/update",
            {**headers, "Content-Length": str(len(candidate_frame))},
            candidate_frame,
        )
    assert status == 409


def _build_agent_with_version(build_root, version, private_key):
    build_tree = build_root / "state-agent"
    shutil.copytree(AGENT_DIR, build_tree)
    (build_tree / "VERSION").write_text(version + "\n", encoding="ascii")
    pub_der = subprocess.run(
        [
            "openssl",
            "pkey",
            "-in",
            str(private_key),
            "-pubout",
            "-outform",
            "DER",
        ],
        check=True,
        capture_output=True,
    ).stdout
    (build_tree / "release-public-key.hex").write_text(
        pub_der[-32:].hex() + "\n", encoding="ascii"
    )
    subprocess.run(
        ["make", "clean", "all"],
        cwd=build_tree,
        check=True,
        capture_output=True,
        text=True,
    )
    return build_tree / "plaf203-state-agent"


def test_update_supervisor_applies_pending_transaction_deterministically(
    state_agent_build, tmp_path
):
    root = tmp_path
    local = root / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)

    active = local / "plaf203-state-agent"
    candidate = update / "candidate.bin"
    pending = update / "pending.json"
    token = local / "token"
    old_binary = _minimal_arm_elf(1)
    new_binary = _minimal_arm_elf(2)
    active.write_bytes(old_binary)
    candidate.write_bytes(new_binary)
    pending.write_text(
        json.dumps(
            {
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    (update / "status.json").write_text(
        pending.read_text(encoding="utf-8"), encoding="utf-8"
    )
    token.write_text(TOKEN + "\n", encoding="ascii")

    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    nc_stub = tmp_path / "nc-stub.sh"
    nc_stub.write_text(
        "#!/bin/sh\n"
        'req="$(cat)"\n'
        "first=\"$(printf '%s' \"$req\" | sed -n '1s/\\r$//p')\"\n"
        'case "$first" in\n'
        "  'GET /health HTTP/1.1')\n"
        "    printf 'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n{\"ok\":true}'\n"
        "    ;;\n"
        "  'GET /v1/version HTTP/1.1')\n"
        '    printf \'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n{"ok":true,"version":"9.9.9"}\'\n'
        "    ;;\n"
        "  *)\n"
        "    printf 'HTTP/1.1 404 Not Found\\r\\nContent-Type: application/json\\r\\n\\r\\n{\"ok\":false}'\n"
        "    ;;\n"
        "esac\n",
        encoding="ascii",
    )
    nc_stub.chmod(0o755)

    supervisor = (
        REPO_ROOT
        / "state-agent"
        / "runit"
        / "plaf203-update-supervisor"
        / "supervisor.sh"
    )
    env = {
        **os.environ,
        "PLAF203_SUPERVISOR_TEST_MODE": "1",
        "PLAF203_SUPERVISOR_RUN_ONCE": "1",
        "PLAF203_TEST_ROOT": str(root),
        "PLAF203_SV_BIN": str(sv_stub),
        "PLAF203_NC_BIN": str(nc_stub),
        "PLAF203_FS_HELPER": str(state_agent_build["helper"]),
        "PLAF203_PROBE_RETRY_DELAY_SECONDS": "0",
    }
    subprocess.run(["/bin/sh", str(supervisor)], check=True, env=env)

    status_doc = json.loads((update / "status.json").read_text(encoding="utf-8"))
    assert status_doc["status"] == "idle"
    assert status_doc["reason"] == "update_applied"
    assert not pending.exists()
    assert not candidate.exists()
    assert active.read_bytes() == new_binary
    assert (update / "previous.bin").read_bytes() == old_binary


def test_update_rejects_duplicate_nested_artifact_keys_after_signature_validation(
    state_agent_binary, ota_signing_private_key, tmp_path
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    duplicate_manifest = (
        '{"schema_version":1,"product":"plaf203-state-agent","channel":"stable",'
        '"version":"9.9.9","api_version":1,"update_api_version":1,'
        '"platform":"linux-armv7-eabihf","artifact":{'
        '"url":"https://example.invalid/a.bin",'
        '"sha256":"%s",'
        '"sha256":"%s",'
        '"size":%d},'
        '"release_url":"https://example.invalid/r"}'
    ) % (
        hashlib.sha256(artifact).hexdigest(),
        hashlib.sha256(artifact).hexdigest(),
        len(artifact),
    )
    manifest_bytes = duplicate_manifest.encode("utf-8")
    signature = _sign_manifest(ota_signing_private_key, manifest_bytes)
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status, body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            framed,
        )

    assert status == 400
    assert body["error"] == "manifest validation failed"


@pytest.mark.parametrize(
    "mutator",
    [
        lambda m: m.update({"product": "plaf203"}),
        lambda m: m.update({"channel": "beta"}),
        lambda m: m.update({"release_url": ""}),
        lambda m: m["artifact"].update({"url": ""}),
    ],
)
def test_update_rejects_signed_manifest_with_bad_product_channel_or_urls(
    state_agent_binary, ota_signing_private_key, tmp_path, mutator
):
    _write_snapshot(tmp_path)
    artifact = _minimal_arm_elf()
    manifest = _update_manifest("9.9.9", artifact)
    mutator(manifest)
    manifest_bytes = json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    signature = _sign_manifest(ota_signing_private_key, manifest_bytes)
    framed = _frame_update_body(manifest_bytes, signature, artifact)

    with RunningAgent(state_agent_binary, tmp_path) as agent:
        status, body = _post_raw(
            agent.port,
            "/v1/update",
            {
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(framed)),
            },
            framed,
        )

    assert status == 400
    assert body["error"] == "manifest validation failed"


def test_update_supervisor_rolls_back_when_candidate_version_probe_fails(
    state_agent_build, tmp_path
):
    root = tmp_path
    local = root / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)

    active = local / "plaf203-state-agent"
    candidate = update / "candidate.bin"
    pending = update / "pending.json"
    token = local / "token"
    old_binary = _minimal_arm_elf(1)
    active.write_bytes(old_binary)
    candidate.write_bytes(_minimal_arm_elf(2))
    pending.write_text(
        json.dumps(
            {
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    (update / "status.json").write_text(
        pending.read_text(encoding="utf-8"), encoding="utf-8"
    )
    token.write_text(TOKEN + "\n", encoding="ascii")

    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    nc_stub = tmp_path / "nc-stub.sh"
    nc_stub.write_text(
        "#!/bin/sh\n"
        'req="$(cat)"\n'
        "first=\"$(printf '%s' \"$req\" | sed -n '1s/\\r$//p')\"\n"
        'case "$first" in\n'
        "  'GET /health HTTP/1.1')\n"
        "    printf 'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n{\"ok\":true}'\n"
        "    ;;\n"
        "  'GET /v1/version HTTP/1.1')\n"
        '    printf \'HTTP/1.1 200 OK\\r\\nContent-Type: application/json\\r\\n\\r\\n{"ok":true,"version":"0.3.0"}\'\n'
        "    ;;\n"
        "  *)\n"
        "    printf 'HTTP/1.1 404 Not Found\\r\\nContent-Type: application/json\\r\\n\\r\\n{\"ok\":false}'\n"
        "    ;;\n"
        "esac\n",
        encoding="ascii",
    )
    nc_stub.chmod(0o755)

    supervisor = (
        REPO_ROOT
        / "state-agent"
        / "runit"
        / "plaf203-update-supervisor"
        / "supervisor.sh"
    )
    env = {
        **os.environ,
        "PLAF203_SUPERVISOR_TEST_MODE": "1",
        "PLAF203_SUPERVISOR_RUN_ONCE": "1",
        "PLAF203_TEST_ROOT": str(root),
        "PLAF203_SV_BIN": str(sv_stub),
        "PLAF203_NC_BIN": str(nc_stub),
        "PLAF203_FS_HELPER": str(state_agent_build["helper"]),
        "PLAF203_PROBE_RETRY_DELAY_SECONDS": "0",
    }
    subprocess.run(["/bin/sh", str(supervisor)], check=True, env=env)

    status_doc = json.loads((update / "status.json").read_text(encoding="utf-8"))
    assert status_doc["status"] == "rolled_back"
    assert status_doc["reason"] == "version_probe_failed"
    assert not pending.exists()
    assert not candidate.exists()
    assert active.read_bytes() == old_binary


def test_update_supervisor_missing_backup_fails_conservatively(
    state_agent_build, tmp_path
):
    root = tmp_path
    local = root / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)

    status = update / "status.json"
    status.write_text(
        json.dumps(
            {
                "status": "candidate_active",
                "reason": "probation",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )

    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    supervisor = (
        REPO_ROOT
        / "state-agent"
        / "runit"
        / "plaf203-update-supervisor"
        / "supervisor.sh"
    )
    env = {
        **os.environ,
        "PLAF203_SUPERVISOR_TEST_MODE": "1",
        "PLAF203_SUPERVISOR_RUN_ONCE": "1",
        "PLAF203_TEST_ROOT": str(root),
        "PLAF203_SV_BIN": str(sv_stub),
        "PLAF203_FS_HELPER": str(state_agent_build["helper"]),
    }
    subprocess.run(["/bin/sh", str(supervisor)], check=True, env=env)

    status_doc = json.loads(status.read_text(encoding="utf-8"))
    assert status_doc["status"] == "failed"
    assert status_doc["reason"] == "backup_invalid"
    assert status_doc["candidate_version"] == "9.9.9"
    assert status_doc["previous_version"] == "0.3.0"


def test_update_supervisor_rolls_back_interrupted_activation_without_retry(
    state_agent_build, tmp_path
):
    root = tmp_path
    local = root / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)

    active = local / "plaf203-state-agent"
    backup = update / "previous.bin"
    candidate = update / "candidate.bin"
    pending = update / "pending.json"
    status = update / "status.json"
    active.write_bytes(_minimal_arm_elf(2))
    previous_binary = _minimal_arm_elf(1)
    backup.write_bytes(previous_binary)
    candidate.write_bytes(_minimal_arm_elf(2))
    pending.write_text(
        json.dumps(
            {
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    status.write_text(
        json.dumps(
            {
                "status": "candidate_active",
                "reason": "probation",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )

    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    supervisor = (
        REPO_ROOT
        / "state-agent"
        / "runit"
        / "plaf203-update-supervisor"
        / "supervisor.sh"
    )
    env = {
        **os.environ,
        "PLAF203_SUPERVISOR_TEST_MODE": "1",
        "PLAF203_SUPERVISOR_RUN_ONCE": "1",
        "PLAF203_TEST_ROOT": str(root),
        "PLAF203_SV_BIN": str(sv_stub),
        "PLAF203_FS_HELPER": str(state_agent_build["helper"]),
    }
    subprocess.run(["/bin/sh", str(supervisor)], check=True, env=env)

    status_doc = json.loads(status.read_text(encoding="utf-8"))
    assert status_doc["status"] == "rolled_back"
    assert status_doc["reason"] == "reboot_during_probation"
    assert active.read_bytes() == previous_binary
    assert not pending.exists()
    assert not candidate.exists()


def test_update_supervisor_commits_confirmed_probation_after_reboot(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active = local / "plaf203-state-agent"
    active_binary = _minimal_arm_elf(2)
    active.write_bytes(active_binary)
    (update / "candidate.bin").write_bytes(active_binary)
    (update / "pending.json").write_text("{}", encoding="utf-8")
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": "probation_confirmed",
                "reason": "health_and_version_confirmed",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "idle"
    assert status_doc["reason"] == "update_applied"
    assert active.read_bytes() == active_binary
    assert not (update / "candidate.bin").exists()
    assert not (update / "pending.json").exists()


def test_update_supervisor_cleans_uncommitted_staging_debris(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active_binary = _minimal_arm_elf(1)
    (local / "plaf203-state-agent").write_bytes(active_binary)
    (update / "candidate.bin").write_bytes(_minimal_arm_elf(2))
    (update / "pending.json").write_text("{}", encoding="utf-8")
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    assert (local / "plaf203-state-agent").read_bytes() == active_binary
    assert not (update / "candidate.bin").exists()
    assert not (update / "pending.json").exists()


def test_update_supervisor_incomplete_committed_pending_fails_closed(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    (local / "plaf203-state-agent").write_bytes(_minimal_arm_elf(1))
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "failed"
    assert status_doc["reason"] == "staged_artifact_missing"


def test_update_helper_keeps_exactly_one_rolling_backup(state_agent_build, tmp_path):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active = local / "plaf203-state-agent"
    first = _minimal_arm_elf(1)
    second = _minimal_arm_elf(2)
    active.write_bytes(first)
    helper = state_agent_build["helper"]

    subprocess.run(
        [str(helper), "--root", str(tmp_path), "backup"], check=True
    )
    active.write_bytes(second)
    subprocess.run(
        [str(helper), "--root", str(tmp_path), "backup"], check=True
    )

    assert (update / "previous.bin").read_bytes() == second
    assert not (update / "previous.tmp").exists()
    assert [path.name for path in update.glob("previous*")] == ["previous.bin"]


def test_update_supervisor_resumes_after_backup_commit(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    old_binary = _minimal_arm_elf(1)
    new_binary = _minimal_arm_elf(2)
    (local / "plaf203-state-agent").write_bytes(old_binary)
    (local / "token").write_text(TOKEN + "\n", encoding="ascii")
    (update / "previous.bin").write_bytes(old_binary)
    (update / "candidate.bin").write_bytes(new_binary)
    (update / "pending.json").write_text("{}", encoding="utf-8")
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": "activating",
                "reason": "backup_committed",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)
    nc_stub = _write_nc_stub(tmp_path, "9.9.9")

    _run_supervisor_once(
        state_agent_build, tmp_path, sv_stub, nc_bin=nc_stub
    )

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "idle"
    assert (local / "plaf203-state-agent").read_bytes() == new_binary


@pytest.mark.parametrize("completed_phase", ["rolled_back", "failed"])
def test_update_supervisor_completed_failure_never_retries_stale_candidate(
    state_agent_build, tmp_path, completed_phase
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active_binary = _minimal_arm_elf(1)
    (local / "plaf203-state-agent").write_bytes(active_binary)
    (update / "candidate.bin").write_bytes(_minimal_arm_elf(2))
    (update / "pending.json").write_text("{}", encoding="utf-8")
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": completed_phase,
                "reason": "version_probe_failed",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == completed_phase
    assert (local / "plaf203-state-agent").read_bytes() == active_binary
    assert not (update / "candidate.bin").exists()
    assert not (update / "pending.json").exists()


def test_update_supervisor_resumes_rollback_without_retry(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active = local / "plaf203-state-agent"
    previous_binary = _minimal_arm_elf(1)
    active.write_bytes(_minimal_arm_elf(2))
    (update / "previous.bin").write_bytes(previous_binary)
    (update / "candidate.bin").write_bytes(_minimal_arm_elf(2))
    (update / "pending.json").write_text("{}", encoding="utf-8")
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": "rollback_in_progress",
                "reason": "version_probe_failed",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "rolled_back"
    assert status_doc["reason"] == "version_probe_failed"
    assert active.read_bytes() == previous_binary
    assert not (update / "candidate.bin").exists()
    assert not (update / "pending.json").exists()


def test_update_supervisor_refuses_corrupt_backup(state_agent_build, tmp_path):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active = local / "plaf203-state-agent"
    candidate_binary = _minimal_arm_elf(2)
    active.write_bytes(candidate_binary)
    (update / "previous.bin").write_bytes(b"truncated")
    (update / "candidate.bin").write_bytes(candidate_binary)
    (update / "pending.json").write_text("{}", encoding="utf-8")
    (update / "status.json").write_text(
        json.dumps(
            {
                "status": "candidate_active",
                "reason": "probation",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(state_agent_build, tmp_path, sv_stub)

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "failed"
    assert status_doc["reason"] == "backup_invalid"
    assert active.read_bytes() == candidate_binary


def test_update_supervisor_reports_missing_probe_tool(state_agent_build, tmp_path):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    active_binary = _minimal_arm_elf(1)
    (local / "plaf203-state-agent").write_bytes(active_binary)
    (update / "candidate.bin").write_bytes(_minimal_arm_elf(2))
    (update / "pending.json").write_text(
        json.dumps(
            {
                "status": "pending",
                "reason": "candidate_ready",
                "candidate_version": "9.9.9",
                "previous_version": "0.3.0",
            }
        ),
        encoding="utf-8",
    )
    (update / "status.json").write_text(
        (update / "pending.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    sv_stub = tmp_path / "sv-stub.sh"
    sv_stub.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    sv_stub.chmod(0o755)

    _run_supervisor_once(
        state_agent_build, tmp_path, sv_stub, nc_bin=tmp_path / "missing-nc"
    )

    status_doc = json.loads(
        (update / "status.json").read_text(encoding="utf-8")
    )
    assert status_doc["status"] == "failed"
    assert status_doc["reason"] == "probe_tool_unavailable"
    assert (local / "plaf203-state-agent").read_bytes() == active_binary


def test_transaction_flock_is_released_when_owner_is_terminated(
    state_agent_build, tmp_path
):
    local = tmp_path / "local-state-agent"
    update = local / "update"
    update.mkdir(parents=True)
    lock_file = update / "transaction.lock"
    holder = subprocess.Popen(
        ["/usr/bin/flock", "--no-fork", str(lock_file), "sleep", "30"]
    )
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            locked = subprocess.run(
                ["/usr/bin/flock", "-n", str(lock_file), "true"]
            ).returncode != 0
            if locked:
                break
            time.sleep(0.02)
        assert locked
    finally:
        holder.terminate()
        holder.wait(timeout=2)

    assert subprocess.run(
        ["/usr/bin/flock", "-n", str(lock_file), "true"]
    ).returncode == 0


def test_app_start_snippet_restores_caller_umask(tmp_path):
    local = tmp_path / "local-state-agent"
    local.mkdir()
    (tmp_path / "enable_state_agent").touch()
    for executable in ("plaf203-state-agent", "plaf203-update-fs"):
        path = local / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
        path.chmod(0o700)
    snippet = REPO_ROOT / "state-agent" / "app_start_snippet.sh"
    command = (
        "umask 0022; before=$(umask); "
        f". {snippet}; "
        "after=$(umask); printf '%s %s\\n' \"$before\" \"$after\""
    )
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PLAF203_STATE_AGENT_ROOT": str(tmp_path),
            "PLAF203_RUNSVDIR_ROOT": str(tmp_path / "runsvdir"),
            "PLAF203_SKIP_RUNSVDIR_START": "1",
            "EXTERNAL_STATE_AGENT_TOKEN": TOKEN,
        },
    )
    before, after = result.stdout.strip().split()
    assert before == after
    token = local / "token"
    assert token.read_text(encoding="ascii").strip() == TOKEN
    assert token.stat().st_mode & 0o777 == 0o600


def _run_supervisor_once(
    state_agent_build, root, sv_bin, *, nc_bin=None
):
    supervisor = (
        REPO_ROOT
        / "state-agent"
        / "runit"
        / "plaf203-update-supervisor"
        / "supervisor.sh"
    )
    env = {
        **os.environ,
        "PLAF203_SUPERVISOR_TEST_MODE": "1",
        "PLAF203_SUPERVISOR_RUN_ONCE": "1",
        "PLAF203_TEST_ROOT": str(root),
        "PLAF203_SV_BIN": str(sv_bin),
        "PLAF203_FS_HELPER": str(state_agent_build["helper"]),
        "PLAF203_PROBE_RETRY_DELAY_SECONDS": "0",
    }
    if nc_bin is not None:
        env["PLAF203_NC_BIN"] = str(nc_bin)
    subprocess.run(["/bin/sh", str(supervisor)], check=True, env=env)


def _write_nc_stub(root, version):
    path = root / "nc-stub.sh"
    path.write_text(
        "#!/bin/sh\n"
        'request="$(cat)"\n'
        "case \"$request\" in\n"
        "  'GET /health '*|*'GET /health '*)\n"
        "    printf 'HTTP/1.1 200 OK\\r\\n\\r\\n{\"ok\":true}' ;;\n"
        "  *)\n"
        f"    printf 'HTTP/1.1 200 OK\\r\\n\\r\\n{{\"ok\":true,\"version\":\"{version}\"}}' ;;\n"
        "esac\n",
        encoding="ascii",
    )
    path.chmod(0o755)
    return path
