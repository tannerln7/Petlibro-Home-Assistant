#!/bin/sh
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
