"""Build the self-contained shell payload executed from the inactive OTA slot."""

from __future__ import annotations

import base64
import binascii
import io
import os
import shutil
import stat
import struct
import tarfile
import tempfile
from pathlib import Path


INSTALLER = r'''#!/bin/sh
set -eu

ROOT="${PLAF203_BOOTSTRAP_TEST_ROOT:-/user}"
DATA="$ROOT/data"
STATE_HOME="$DATA/local-state-agent"
BOOTSTRAP_HOME="$DATA/plaf203-bootstrap"
STATE_BACKUP="$BOOTSTRAP_HOME/preinstall-state"
OTA1="$ROOT/ota1/AF203_FW"
OTA2="$ROOT/ota2/AF203_FW"
INDEX="$DATA/ota/index.bin"
JOURNAL="$BOOTSTRAP_HOME/status"
STAGE="$DATA/.plaf203-bootstrap-stage"
PHASE=preflight
FAIL_REASON=

mkdir -p "$BOOTSTRAP_HOME"
printf '%s\n' starting > "$JOURNAL"
sync

if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    EXECUTION_SLOT="${PLAF203_BOOTSTRAP_TEST_SLOT:-ota2}"
else
    case "$0" in
        /user/ota1/AF203_FW) EXECUTION_SLOT=ota1 ;;
        /user/ota2/AF203_FW) EXECUTION_SLOT=ota2 ;;
        *)
            printf '%s\n' wrong_execution_slot > "$JOURNAL"
            sync
            exit 70
            ;;
    esac
fi

case "$EXECUTION_SLOT" in
    ota1)
        DONOR="$OTA2"
        TARGET="$OTA1"
        SAFE_FW="$OTA2"
        SAFE_INDEX=1
        ;;
    ota2)
        DONOR="$OTA1"
        TARGET="$OTA2"
        SAFE_FW="$OTA1"
        SAFE_INDEX=0
        ;;
    *)
        printf '%s\n' wrong_execution_slot > "$JOURNAL"
        sync
        exit 70
        ;;
esac

if [ ! -x "$DONOR" ]; then
    printf '%s\n' donor_missing > "$JOURNAL"
    sync
    exit 71
fi

donor_magic="$(dd if="$DONOR" bs=1 count=4 2>/dev/null | od -An -tx1 | tr -d ' \n')"
if [ "$donor_magic" != "7f454c46" ]; then
    printf '%s\n' donor_not_elf > "$JOURNAL"
    sync
    exit 72
fi

write_index() {
    case "$1" in
        0) printf '\000\000\000\000' > "$INDEX.new" ;;
        1) printf '\001\000\000\000' > "$INDEX.new" ;;
        *) return 1 ;;
    esac
    mv "$INDEX.new" "$INDEX"
}

select_safe_donor() {
    cat > "$ROOT/app_start.sh.new" << SAFE_START
#!/bin/sh
# Boot the known-good OEM donor and retain any already-installed maintenance.
$SAFE_FW &
if [ -f /user/data/enable_state_agent ] && [ -f /user/data/local-state-agent/app_start_snippet.sh ]; then
    /bin/sh /user/data/local-state-agent/app_start_snippet.sh
fi
if [ -f /user/data/enable_ssh ] && [ -x /user/data/dropbear/dropbear ] && [ -x /user/data/dropbear/dropbearkey ] && [ -s /user/data/dropbear/authorized_keys ]; then
    mkdir -p /dev/pts /root/.ssh
    grep -q ' /dev/pts ' /proc/mounts 2>/dev/null || mount -t devpts devpts /dev/pts 2>/dev/null || true
    chmod 700 /root /root/.ssh
    cp /user/data/dropbear/authorized_keys /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    if [ ! -s /user/data/dropbear/dropbear_ed25519_host_key ]; then
        /user/data/dropbear/dropbearkey -t ed25519 -f /user/data/dropbear/dropbear_ed25519_host_key
        chmod 600 /user/data/dropbear/dropbear_ed25519_host_key
    fi
    /user/data/dropbear/dropbear -r /user/data/dropbear/dropbear_ed25519_host_key -p 2222 -s -P /tmp/dropbear.pid
fi
SAFE_START
    chmod 755 "$ROOT/app_start.sh.new"
    mv "$ROOT/app_start.sh.new" "$ROOT/app_start.sh"
    mkdir -p "$DATA/ota"
    write_index "$SAFE_INDEX"
    sync
}

fail_safe() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    reason="${FAIL_REASON:-install_failed_${PHASE}_${result}}"
    printf '%s\n' "$reason" > "$JOURNAL"
    select_safe_donor
    sync
    if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
        exit "${result:-1}"
    fi
    reboot
    sleep 30
    exit "${result:-1}"
}

# From this point forward, every failure selects the untouched OEM donor and a
# startup script that cannot execute the temporary payload again.
trap fail_safe EXIT HUP INT TERM

if [ -z "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    for tool in /usr/bin/flock /usr/bin/nc /usr/bin/sv; do
        if [ ! -x "$tool" ]; then
            FAIL_REASON="required_tool_missing_$(basename "$tool")"
            exit 79
        fi
    done
fi

rm -rf "$STAGE"
mkdir -p "$STAGE"
archive_line="$(awk '/^__PLAF203_BOOTSTRAP_ARCHIVE_BELOW__$/{print NR + 1; exit}' "$0")"
if [ -z "$archive_line" ]; then
    printf '%s\n' embedded_archive_missing > "$JOURNAL"
    sync
    exit 73
fi
tail -n "+$archive_line" "$0" | gzip -dc | tar -xf - -C "$STAGE"

for required in \
    local-state-agent/plaf203-state-agent \
    local-state-agent/plaf203-update-fs \
    local-state-agent/app_start_snippet.sh \
    local-state-agent/token \
    dropbear/dropbearmulti \
    dropbear/LICENSE.dropbear \
    dropbear/BUILD_INFO.dropbear.json \
    authorized_keys \
    app_start.sh; do
    if [ ! -s "$STAGE/$required" ]; then
        printf '%s\n' "archive_missing_$required" > "$JOURNAL"
        sync
        exit 74
    fi
done

# JFFS2 is compressed and log-structured: the donor's uncompressed byte
# count cannot predict free flash after the OTA swap or during restoration.
# The independent restore shell below unlinks the temporary payload first;
# the actual write and byte-for-byte verification remain failure-checked.

# Preserve a fresh copy of feeder-local settings and schedules before changing
# startup or sidecar services. mkdir atomically claims an attempt number; an
# interrupted directory remains visibly incomplete and is never reused.
PHASE=preserving_feeder_state
valid_state_snapshot() {
    snapshot="$1"
    [ -f "$snapshot/attr/state.bin" ] || return 1
    [ "$(wc -c < "$snapshot/attr/state.bin" | tr -d ' ')" = 236 ] || return 1
    [ -f "$snapshot/feed_plan/index.bin" ] || return 1
    [ -f "$snapshot/feed_plan/plan.bin" ] || return 1
    plan_count="$(od -An -tu1 -N1 "$snapshot/feed_plan/index.bin" 2>/dev/null | tr -d ' ')"
    case "$plan_count" in
        ''|*[!0-9]*) return 1 ;;
    esac
    plan_bytes="$(wc -c < "$snapshot/feed_plan/plan.bin" | tr -d ' ')"
    [ "$plan_bytes" = "$((plan_count * 47))" ]
}

mkdir -p "$STATE_BACKUP"
snapshot_number=1
while :; do
    snapshot_name="$(printf 'snapshot-%06d' "$snapshot_number")"
    SNAPSHOT="$STATE_BACKUP/$snapshot_name"
    if mkdir "$SNAPSHOT" 2>/dev/null; then
        break
    fi
    snapshot_number=$((snapshot_number + 1))
    if [ "$snapshot_number" -gt 999999 ]; then
        FAIL_REASON=invalid_preinstall_state_backup
        exit 77
    fi
done
printf '%s\n' "capturing_$snapshot_name" > "$JOURNAL"
if ! cp -Rp "$DATA/attr" "$SNAPSHOT/attr" || \
   ! cp -Rp "$DATA/feed_plan" "$SNAPSHOT/feed_plan" || \
   ! valid_state_snapshot "$SNAPSHOT"; then
    FAIL_REASON=invalid_preinstall_state_backup
    exit 77
fi
chmod -R go-rwx "$SNAPSHOT"
touch "$SNAPSHOT/complete"
chmod go-rwx "$SNAPSHOT/complete"
sync

# OEM OTA rewrites app_start.sh to the inactive slot. Replace it with a minimal
# donor startup and durably select the untouched donor before any mutation. If
# the payload is in OTA1, this temporarily selects OTA2 until OTA1 is restored.
PHASE=selecting_safe_donor
if [ ! -f "$BOOTSTRAP_HOME/app_start.oem" ]; then
    cp "$ROOT/app_start.sh" "$BOOTSTRAP_HOME/app_start.oem"
    chmod 700 "$BOOTSTRAP_HOME/app_start.oem"
fi
select_safe_donor

PHASE=installing_services
printf '%s\n' installing_services > "$JOURNAL"
rm -rf "$STATE_HOME.new"
mv "$STAGE/local-state-agent" "$STATE_HOME.new"
chmod 700 "$STATE_HOME.new/plaf203-state-agent" \
    "$STATE_HOME.new/plaf203-update-fs" \
    "$STATE_HOME.new/app_start_snippet.sh"
chmod 600 "$STATE_HOME.new/token"
find "$STATE_HOME.new/runit" -type f -exec chmod 700 {} \;

if [ -d "$STATE_HOME" ]; then
    rm -rf "$STATE_HOME.pre-bootstrap"
    mv "$STATE_HOME" "$STATE_HOME.pre-bootstrap"
fi
mv "$STATE_HOME.new" "$STATE_HOME"
touch "$DATA/enable_state_agent"

rm -rf "$DATA/dropbear.new"
mv "$STAGE/dropbear" "$DATA/dropbear.new"
chmod 700 "$DATA/dropbear.new/dropbearmulti"
chmod 600 "$DATA/dropbear.new/LICENSE.dropbear" \
    "$DATA/dropbear.new/BUILD_INFO.dropbear.json"
ln -sf dropbearmulti "$DATA/dropbear.new/dropbear"
ln -sf dropbearmulti "$DATA/dropbear.new/dropbearkey"
cp "$STAGE/authorized_keys" "$DATA/dropbear.new/authorized_keys"
chmod 600 "$DATA/dropbear.new/authorized_keys"
if [ -d "$DATA/dropbear" ]; then
    rm -rf "$DATA/dropbear.pre-bootstrap"
    mv "$DATA/dropbear" "$DATA/dropbear.pre-bootstrap"
fi
mv "$DATA/dropbear.new" "$DATA/dropbear"
touch "$DATA/enable_ssh"

PHASE=restoring_payload_slot
printf '%s\n' "restoring_$EXECUTION_SLOT" > "$JOURNAL"

# Move execution away from the OTA script before unlinking it. The donor boot
# target and maintenance-capable fallback were selected and synced above.
# This avoids allocating a third full firmware file (TARGET.restore).
export ROOT DATA STATE_HOME INDEX JOURNAL STAGE DONOR TARGET
exec /bin/sh -c '
set -eu
PHASE=restoring_payload_slot
FAIL_REASON=
restore_failure() {
    result=$?
    trap - EXIT HUP INT TERM
    set +e
    printf "%s\n" "${FAIL_REASON:-install_failed_${PHASE}_${result}}" > "$JOURNAL"
    sync
    if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
        exit "${result:-1}"
    fi
    reboot
    sleep 30
    exit "${result:-1}"
}
trap restore_failure EXIT HUP INT TERM

# Free the temporary OTA script before allocating its firmware replacement.
# If this fails partway through, the selected donor stays intact and bootable.
if ! rm -f "$TARGET"; then
    FAIL_REASON=payload_slot_remove_failed
    exit 76
fi
sync
if ! cp "$DONOR" "$TARGET"; then
    FAIL_REASON=payload_slot_restore_copy_failed
    rm -f "$TARGET"
    exit 77
fi
if ! cmp "$DONOR" "$TARGET"; then
    FAIL_REASON=payload_slot_restore_verify_failed
    rm -f "$TARGET"
    exit 78
fi
chmod 755 "$TARGET"
sync

# Switch to OTA1 only after the restored executable has been verified.
PHASE=selecting_production_ota1
cp "$STAGE/app_start.sh" "$ROOT/app_start.sh.new"
chmod 755 "$ROOT/app_start.sh.new"
mv "$ROOT/app_start.sh.new" "$ROOT/app_start.sh"
mkdir -p "$DATA/ota"
printf "\000\000\000\000" > "$INDEX.new"
mv "$INDEX.new" "$INDEX"
sync

rm -rf "$STATE_HOME.pre-bootstrap" "$DATA/dropbear.pre-bootstrap"
rm -rf "$STAGE"
printf "%s\n" installed_rebooting_to_ota1 > "$JOURNAL"
sync
trap - EXIT HUP INT TERM
if [ -n "${PLAF203_BOOTSTRAP_TEST_ROOT:-}" ]; then
    exit 0
fi
reboot
exit 0
'
__PLAF203_BOOTSTRAP_ARCHIVE_BELOW__
'''.encode("ascii")


APP_START_TEMPLATE = r'''#!/bin/sh

# Installed by the PLAF203 local bootstrap. Keep production startup minimal.
/bin/sh /user/data/local-state-agent/app_start_snippet.sh

if [ -f /user/data/enable_ssh ] && \
   [ -x /user/data/dropbear/dropbear ] && \
   [ -x /user/data/dropbear/dropbearkey ] && \
   [ -s /user/data/dropbear/authorized_keys ]; then
    mkdir -p /dev/pts /root/.ssh /user/data/dropbear
    grep -q ' /dev/pts ' /proc/mounts 2>/dev/null || \
        mount -t devpts devpts /dev/pts 2>/dev/null || true
    chmod 700 /root /root/.ssh
    cp /user/data/dropbear/authorized_keys /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
    if [ ! -s /user/data/dropbear/dropbear_ed25519_host_key ]; then
        /user/data/dropbear/dropbearkey -t ed25519 \
            -f /user/data/dropbear/dropbear_ed25519_host_key
        chmod 600 /user/data/dropbear/dropbear_ed25519_host_key
    fi
    dropbear_running=false
    if [ -r /tmp/dropbear.pid ]; then
        dropbear_pid="$(cat /tmp/dropbear.pid 2>/dev/null || true)"
        case "$dropbear_pid" in
            ''|*[!0-9]*) ;;
            *)
                if [ -r "/proc/$dropbear_pid/cmdline" ]; then
                    dropbear_cmdline="$(tr '\000' ' ' < "/proc/$dropbear_pid/cmdline")"
                    case "$dropbear_cmdline" in
                        /user/data/dropbear/dropbear\ *' -p 2222 '*)
                            dropbear_running=true
                            ;;
                    esac
                fi
                ;;
        esac
    fi
    if [ "$dropbear_running" != true ]; then
        rm -f /tmp/dropbear.pid
        /user/data/dropbear/dropbear \
            -r /user/data/dropbear/dropbear_ed25519_host_key \
            -p 2222 -s -P /tmp/dropbear.pid
    fi
fi

/user/ota1/AF203_FW &
'''


def _copy_required(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(source)
    shutil.copyfile(source, destination)


def _read_ssh_field(blob: bytes, offset: int) -> tuple[bytes, int]:
    if offset + 4 > len(blob):
        raise ValueError("SSH public key blob is truncated")
    length = struct.unpack_from("!I", blob, offset)[0]
    offset += 4
    end = offset + length
    if end > len(blob):
        raise ValueError("SSH public key blob is truncated")
    return blob[offset:end], end


def validate_public_key(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("an SSH public key is required for feeder recovery access")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("SSH public key must contain exactly one line")
    fields = normalized.split()
    if len(fields) < 2 or fields[0] not in {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
    }:
        raise ValueError("SSH public key must be one supported OpenSSH public-key line")
    try:
        blob = base64.b64decode(fields[1], validate=True)
    except (binascii.Error, ValueError) as err:
        raise ValueError("SSH public key contains invalid base64") from err

    algorithm, offset = _read_ssh_field(blob, 0)
    if algorithm != fields[0].encode("ascii"):
        raise ValueError("SSH public key type does not match its encoded key blob")
    if fields[0] == "ssh-ed25519":
        key_bytes, offset = _read_ssh_field(blob, offset)
        if len(key_bytes) != 32:
            raise ValueError("Ed25519 SSH public key must contain 32 key bytes")
    elif fields[0] == "ssh-rsa":
        exponent, offset = _read_ssh_field(blob, offset)
        modulus, offset = _read_ssh_field(blob, offset)
        if not exponent or not modulus:
            raise ValueError("RSA SSH public key is incomplete")
    else:
        curve, offset = _read_ssh_field(blob, offset)
        point, offset = _read_ssh_field(blob, offset)
        if curve != b"nistp256" or len(point) != 65 or point[:1] != b"\x04":
            raise ValueError("ECDSA SSH public key is not a valid nistp256 key")
    if offset != len(blob):
        raise ValueError("SSH public key blob contains trailing data")
    return normalized


def _validate_dropbear_bundle(path: Path) -> None:
    candidate = path / "dropbearmulti"
    license_file = path / "LICENSE.dropbear"
    build_info = path / "BUILD_INFO.dropbear.json"
    if not candidate.is_file():
        raise FileNotFoundError(candidate)
    if not license_file.is_file():
        raise FileNotFoundError(license_file)
    if not build_info.is_file():
        raise FileNotFoundError(build_info)
    header = candidate.read_bytes()[:40]
    if (
        len(header) < 40
        or header[:4] != b"\x7fELF"
        or header[4] != 1
        or int.from_bytes(header[18:20], "little") != 40
        or not (int.from_bytes(header[36:40], "little") & 0x400)
    ):
        raise ValueError(f"{candidate} is not an ELF32 ARM hard-float executable")


def build_payload(
    *,
    output: Path,
    state_agent_dir: Path,
    home_assistant_ip: str,
    state_agent_token: str,
    ssh_public_key: str,
    dropbear_dir: Path,
) -> Path:
    """Create an OTA payload without persisting intermediate secret files."""

    key = validate_public_key(ssh_public_key)
    _validate_dropbear_bundle(dropbear_dir)
    if len(state_agent_token) != 64 or any(
        character not in "0123456789abcdef" for character in state_agent_token
    ):
        raise ValueError("state-agent token must be exactly 64 lowercase hexadecimal characters")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".payload-build-", dir=output.parent
    ) as raw_temp:
        root = Path(raw_temp)
        agent = root / "local-state-agent"
        agent.mkdir()
        for name in (
            "plaf203-state-agent",
            "plaf203-update-fs",
            "app_start_snippet.sh",
        ):
            _copy_required(state_agent_dir / name, agent / name)
        shutil.copytree(state_agent_dir / "runit", agent / "runit")
        run_path = agent / "runit" / "plaf203-state-agent" / "run"
        run_text = run_path.read_text(encoding="utf-8")
        template_allowlist = "--allow-ip 192.0.2.10"
        configured_allowlist = f"--allow-ip {home_assistant_ip}"
        if template_allowlist not in run_text:
            raise ValueError("State Agent run template does not contain the expected allowlist")
        run_text = run_text.replace(template_allowlist, configured_allowlist)
        if configured_allowlist not in run_text or (
            configured_allowlist != template_allowlist and template_allowlist in run_text
        ):
            raise ValueError("failed to specialize State Agent allowlist")
        run_path.write_text(run_text, encoding="utf-8")
        (agent / "token").write_text(state_agent_token + "\n", encoding="ascii")
        (root / "app_start.sh").write_text(APP_START_TEMPLATE, encoding="ascii")
        (root / "authorized_keys").write_text(key + "\n", encoding="ascii")

        target = root / "dropbear"
        target.mkdir()
        for name in ("dropbearmulti", "LICENSE.dropbear", "BUILD_INFO.dropbear.json"):
            _copy_required(dropbear_dir / name, target / name)

        for path in root.rglob("*"):
            if path.is_file():
                path.chmod(0o600)

        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz", compresslevel=9) as tar:
            for path in sorted(root.rglob("*")):
                info = tar.gettarinfo(str(path), arcname=str(path.relative_to(root)))
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                if path.is_file():
                    with path.open("rb") as source:
                        tar.addfile(info, source)
                else:
                    tar.addfile(info)

    temporary = output.with_name(output.name + ".new")
    with temporary.open("wb") as stream:
        stream.write(INSTALLER)
        stream.write(archive.getvalue())
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    temporary.replace(output)
    return output
