#!/bin/sh

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
