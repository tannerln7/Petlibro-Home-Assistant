#!/usr/bin/env python3
"""Bounded OEM reference experiment; run only in a network-none container.

All guest output and writable flash stay private. This is not a firmware
installer or the replacement-image qualification. See firmware/plaf203/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
import struct
import zlib

from qualify_qemu_gk7205v200 import sha256_file, stop_guest


OEM_SHA256 = "cb812b7619bfe77cea4d35b2a7e0fd80df76fb673335673e1e3af16657dac1d7"
OEM_BYTES = 32 * 1024 * 1024
QEMU_SHA256 = "a2d54f7b32c4cd50522bec44d73c6aab590fbd77359b8355acde38e1a72a796c"


def verify_image(path: Path) -> None:
    if not path.is_file() or path.stat().st_size != OEM_BYTES or sha256_file(path) != OEM_SHA256:
        raise ValueError("OEM input does not match the primary 32 MiB reference")


def copy_flash(source: Path, destination: Path) -> None:
    verify_image(source)
    # Exclusive creation and streaming copy: never reuse/overwrite a previous run.
    with source.open("rb") as src, destination.open("xb") as dst:
        os.chmod(destination, 0o600)
        shutil.copyfileobj(src, dst)
    verify_image(destination)
    if os.path.samestat(source.stat(), destination.stat()):
        raise ValueError("Flash backing must be independent of preserved input")


def command(qemu: Path, flash: Path, work: Path) -> list[str]:
    return [str(qemu), "-M", "gk7205v200,sensor=none,dump-guest-core=off",
            "-global", "driver=hisi-fmc,property=flash-jedec,value=0xc22019",
            "-global", f"driver=hisi-fmc,property=flash-file,value={flash}",
            "-nic", "none", "-display", "none", "-monitor", "none",
            "-chardev", f"socket,id=uart,path={work / 'uart.sock'},server=on,wait=off,logfile={work / 'serial.log'}",
            "-serial", "chardev:uart",
            "-qmp", f"unix:{work / 'qmp.sock'},server=on,wait=off", "-S",
            "-d", "unimp,guest_errors", "-D", str(work / "qemu-debug.log")]


def milestones(serial: bytes) -> dict[str, bool]:
    # Execution output only, never a scan of flash bytes. Later stages require
    # preceding kernel/rootfs evidence rather than an isolated banner string.
    kernel = b"Linux version 4.9.37" in serial
    root = kernel and b"VFS: Mounted root (jffs2 filesystem)" in serial
    return {
        "uboot_banner": b"U-Boot 2016.11" in serial,
        "nor_32m": b"SPI Nor total size: 32MB" in serial,
        "environment_crc_failure": b"bad CRC" in serial,
        "kernel_handoff": b"Starting kernel" in serial,
        "oem_linux_banner": kernel,
        "kernel_panic": b"Kernel panic" in serial,
        "jffs2_root_mounted": root,
        "init_started": root and (b"Run /sbin/init" in serial or b"init started:" in serial),
    }


def probe_command(nonce: str) -> bytes:
    # No configuration, environment, command-line arguments or credentials.
    # Split printf markers distinguish execution from terminal command echo.
    return ("p1=$(readlink /proc/1/exe); "
            "rt=$(awk '$2==\"/\"{print $3}' /proc/mounts); ap=no; ah=none; ac=0; "
            "for x in /proc/[0-9]*/exe; do n=$(readlink \"$x\"); "
            "case \"$n\" in */AF203_FW*) ap=yes; ac=$((ac+1)); "
            "ah=$(sha256sum \"$x\" | cut -d' ' -f1);; esac; done; "
            f"printf '\\nPLAF203_REF_%s|%s|%s|%s|%s|END\\n' '{nonce}' \"$p1\" \"$rt\" \"$ap\" \"$ah\"; "
            "lc=0; ul=no; wl=no; if [ -f /mnt/af203.log ]; then "
            "lc=$(wc -c < /mnt/af203.log); grep -q uartEnableFlag /mnt/af203.log && ul=yes; "
            "grep -q 'open watchdog success' /mnt/af203.log && wl=yes; fi; "
            "mc=$(wc -l < /proc/modules); "
            f"printf '\\nPLAF203_MORE_%s|%s|%s|%s|%s|%s|END\\n' '{nonce}' \"$ac\" \"$lc\" \"$ul\" \"$wl\" \"$mc\"\n").encode()


def probe_result(serial: bytes, nonce: str) -> dict:
    data = serial.replace(b"\r", b"")
    pattern = rb"\nPLAF203_REF_" + re.escape(nonce.encode()) + rb"\|(/[^|\n]+)\|(\w+)\|(yes|no)\|([0-9a-f]{64}|none)\|END\n"
    match = re.search(pattern, data)
    if not match:
        return {"completed": False}
    result = {"completed": True,
            "pid1_is_busybox": match[1] == b"/bin/busybox",
            "root_is_jffs2": match[2] == b"jffs2",
            "application_present": match[3] == b"yes",
            "application_original_sha256": match[4] == b"da353ba37a7d8c1c0790827e5ec267a180519affb5666ae4efd93e5362e00b71"}
    more = re.search(rb"\nPLAF203_MORE_" + re.escape(nonce.encode()) + rb"\|(\d+)\|\s*(\d+)\|(yes|no)\|(yes|no)\|\s*(\d+)\|END\n", data)
    if more:
        result.update(application_count=int(more[1]), application_log_bytes=int(more[2]),
                      uart_setting_logged=more[3] == b"yes", watchdog_open_logged=more[4] == b"yes",
                      loaded_module_count=int(more[5]))
    return result


class Qmp:
    def __init__(self, path: Path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.buffer = b""
        self.service_io = None
        self.sequence = 0
        self.events = []
        self.receive(time.monotonic() + 5)
        self.execute("qmp_capabilities")

    def receive(self, deadline):
        while b"\n" not in self.buffer:
            if time.monotonic() >= deadline:
                raise TimeoutError("QMP response deadline")
            if self.service_io:
                self.service_io()
            if select.select([self.sock], [], [], 0.02)[0]:
                data = self.sock.recv(65536)
                if not data:
                    raise RuntimeError("QMP disconnected")
                self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def execute(self, name: str, arguments: dict | None = None):
        self.sequence += 1
        request_id = self.sequence
        self.sock.sendall(json.dumps({"execute": name, "arguments": arguments or {}, "id": request_id}).encode() + b"\n")
        deadline = time.monotonic() + 3
        while True:
            result = self.receive(deadline)
            if "error" in result:
                raise RuntimeError("QMP command failed")
            if "return" in result and result.get("id") == request_id:
                return result["return"]
            if result.get("event") in {"RESET", "WATCHDOG", "SHUTDOWN"}:
                self.events.append({"event": result["event"], "data": result.get("data", {})})

    def close(self):
        self.sock.close()


def diagnostic_environment(flash: Path) -> None:
    """Only disposable bootargs change; keep the OEM boot command and padding."""
    image = bytearray(flash.read_bytes())
    original = bytes(image[0x80000:0xc0000])
    if zlib.crc32(original[4:]) != struct.unpack_from("<I", original)[0]:
        raise ValueError("Unexpected original environment CRC")
    entries = original[4:].split(b"\0\0", 1)[0].split(b"\0")
    found = 0
    for i, entry in enumerate(entries):
        if entry.startswith(b"bootargs="):
            entries[i] += b" init=/bin/sh"
            found += 1
    if found != 1:
        raise ValueError("Expected one original bootargs entry")
    payload = (b"\0".join(entries) + b"\0\0").ljust(0x40000 - 4, b"\0")
    if len(payload) != 0x40000 - 4:
        raise ValueError("Environment overflow")
    image[0x80000:0xc0000] = struct.pack("<I", zlib.crc32(payload)) + payload
    flash.write_bytes(image)


def startup_command(profile: str) -> bytes:
    setup = "mount -t proc proc /proc; mount -t sysfs sysfs /sys; "
    if profile == "shell":
        return (setup + "echo PLAF203_DIAGNOSTIC_SHELL\n").encode()
    # A bind-mounted file changes only the application's launch line. Original
    # rcS, module/service setup and all other app_start commands remain intact.
    replacement = ": # application disabled by emulator profile"
    if profile == "app-pty":
        replacement = "script -qf -c 'exec /user/ota2/AF203_FW </dev/null' /dev/null > /mnt/af203.log 2>&1 &"
    replacement = replacement.replace("&", r"\&")
    return (setup + "mount -t tmpfs -o size=8m,nosuid,nodev tmpfs /mnt && "
            "cp /user/app_start.sh /mnt/app_start.original && "
            "[ \"$(grep -cx '/user/ota2/AF203_FW &' /mnt/app_start.original)\" = 1 ] && "
            f"sed \"s@^/user/ota2/AF203_FW &\\$@{replacement}@\" /mnt/app_start.original > /mnt/app_start.modified && "
            "chmod 755 /mnt/app_start.modified && "
            "mount -o bind /mnt/app_start.modified /user/app_start.sh && exec /sbin/init\n").encode()


def storage_command(token: str, restart: bool = False) -> bytes:
    """Only a unique disposable-guest test directory; never raw MTD writes."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("Invalid storage test token")
    path = f"/user/data/.plaf203-qemu-{token}"
    if restart:
        return (f"p={path}; h=$(sha256sum \"$p/persist.bin\" | cut -d' ' -f1); "
                "d=no; [ ! -e \"$p/trash.bin\" ] && d=yes; "
                "rm \"$p/persist.bin\" && rmdir \"$p\"; sync; "
                f"printf '\\nPLAF203_RESTART_%s|%s|%s|END\\n' '{token}' \"$h\" \"$d\"\n").encode()
    return (f"( umask 077; p={path}; ok=yes; i=0; "
            "mkdir \"$p\" && dd if=/dev/urandom of=\"$p/persist.bin\" bs=65536 count=2 2>/dev/null && "
            "sync || ok=no; [ \"$(wc -c < \"$p/persist.bin\")\" -eq 131072 ] || ok=no; "
            "h=$(sha256sum \"$p/persist.bin\" | cut -d' ' -f1); "
            "while [ \"$i\" -lt 24 ] && [ \"$ok\" = yes ]; do "
            "dd if=/dev/urandom of=\"$p/trash.bin\" bs=65536 count=4 2>/dev/null || ok=no; "
            "a=$(sha256sum \"$p/trash.bin\" | cut -d' ' -f1); sync; "
            "echo 3 > /proc/sys/vm/drop_caches; b=$(sha256sum \"$p/trash.bin\" | cut -d' ' -f1); "
            "[ \"$a\" = \"$b\" ] && [ \"$(wc -c < \"$p/trash.bin\")\" -eq 262144 ] || ok=no; "
            "rm \"$p/trash.bin\" && sync || ok=no; i=$((i+1)); done; "
            "sync; echo 3 > /proc/sys/vm/drop_caches; "
            "r=$(sha256sum \"$p/persist.bin\" | cut -d' ' -f1); "
            "[ \"$h\" = \"$r\" ] || ok=no; "
            f"printf '\\nPLAF203_STORAGE_%s|%s|%s|%s|END\\n' '{token}' \"$ok\" \"$i\" \"$h\" )\n").encode()


def erased_sectors(before: bytes, after: bytes) -> list[int]:
    # Both NOR programming paths use &=; a 0->1 transition requires erase.
    # Limit inference to rootfs; this does not identify every GC phase/victim.
    result = []
    for offset in range(0x500000, OEM_BYTES, 65536):
        a, b = before[offset:offset + 65536], after[offset:offset + 65536]
        if a != b and any((old | new) != old for old, new in zip(a, b)):
            result.append(offset)
    return result


def verify_network(info: str) -> None:
    # A modeled, disconnected FEMAC may remain visible. Reject any peer/backend.
    nic = r"[\w.-]+: index=\d+,type=nic,model=hisi-femac,macaddr=(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}"
    if any(line and not re.fullmatch(nic, line) for line in info.splitlines()):
        raise RuntimeError("QEMU network backend unexpectedly present")


def run(args, resume_work: Path | None = None, storage_token: str | None = None) -> dict:
    if {p.name for p in Path("/sys/class/net").iterdir()} != {"lo"}:
        raise RuntimeError("Run inside a container with --network none (loopback only)")
    inventory = json.loads((args.reconstruction_root / "provenance/replacement-platform-research/device-observations.json").read_text())
    record = inventory["primary_oem_flash_backup"]
    source = args.reconstruction_root / record["repository_copy"] / "plaf203-flash-32m.bin"
    verify_image(source)
    args.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="oem-", dir=args.work_root)) if resume_work is None else resume_work / "restart"
    if resume_work is not None:
        work.mkdir(mode=0o700)
    os.chmod(work, 0o700)
    flash = work / "flash.bin" if resume_work is None else resume_work / "flash.bin"
    if resume_work is None:
        copy_flash(source, flash)
        if args.profile != "native":
            diagnostic_environment(flash)
    elif (flash.resolve().parent.parent != args.work_root.resolve() or
          flash.is_symlink() or flash.stat().st_size != OEM_BYTES or
          os.path.samestat(source.stat(), flash.stat())):
        raise ValueError("Unsafe restart backing")
    qemu = args.cache_root / "qemu-bench/qemu-src/build/qemu-system-arm"
    if sha256_file(qemu) != QEMU_SHA256:
        raise ValueError("QEMU binary differs from the qualified model; review before execution")
    cmd = command(qemu, flash, work)
    if args.watchdog_action == "none":
        cmd += ["-action", "watchdog=none"]
    (work / "command.json").write_text(json.dumps(cmd, indent=2) + "\n")
    proc = None
    qmp = None
    uart = None
    nonce = uuid.uuid4().hex
    probes = []
    controls = []
    pending = []
    setup_sent = False
    operator_exit = False
    next_probe = 10
    next_control = 15
    storage_sent = False
    storage_before = None
    token = storage_token or uuid.uuid4().hex
    terminal_settings = None
    control_responsive = True
    reason = "time_budget"
    started = time.monotonic()
    runner_sha256 = sha256_file(Path(__file__))
    try:
        with (work / "host.log").open("wb") as host:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=host, stderr=host)
            deadline = time.monotonic() + 10
            while not (work / "qmp.sock").exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("QEMU failed before isolation check; see private host.log")
                time.sleep(0.05)
            qmp = Qmp(work / "qmp.sock")
            network = qmp.execute("human-monitor-command", {"command-line": "info network"})
            (work / "network.txt").write_text(network)
            verify_network(network)
            uart = socket.socket(socket.AF_UNIX)
            uart.connect(str(work / "uart.sock"))
            uart.setblocking(False)
            def service_uart():
                nonlocal operator_exit
                for _ in range(16):
                    if not select.select([uart], [], [], 0)[0]:
                        break
                    data = uart.recv(65536)
                    if not data:
                        break
                    if args.interactive:
                        os.write(1, data)
                if args.interactive and select.select([0], [], [], 0)[0]:
                    data = os.read(0, 4096)
                    if not data or b"\x1d" in data:
                        operator_exit = True  # Ctrl-] stops the disposable VM.
                    else:
                        uart.sendall(data)
            qmp.service_io = service_uart
            if args.interactive:
                import termios
                import tty
                terminal_settings = termios.tcgetattr(0)
                tty.setraw(0)
            qmp.execute("cont")
            while proc.poll() is None and time.monotonic() - started < args.seconds:
                if sum(p.stat().st_size for p in work.glob("*.log")) >= args.log_limit_mib * 1024 * 1024:
                    reason = "log_budget"
                    break
                service_uart()
                if operator_exit:
                    reason = "operator_exit"
                    break
                elapsed = time.monotonic() - started
                serial_now = (work / "serial.log").read_bytes() if args.profile != "native" and not setup_sent else b""
                if args.profile != "native" and not setup_sent and b"VFS: Mounted root" in serial_now and elapsed > 4:
                    uart.sendall(startup_command(args.profile))
                    setup_sent = True
                if args.probe and elapsed >= next_probe:
                    nonce = uuid.uuid4().hex
                    uart.sendall(probe_command(nonce))
                    pending.append((round(elapsed, 2), nonce))
                    next_probe = elapsed + 30
                if args.storage_check and not storage_sent and elapsed > 20:
                    storage_before = flash.read_bytes()
                    uart.sendall(storage_command(token, restart=resume_work is not None))
                    storage_sent = True
                if elapsed >= next_control:
                    try:
                        status = qmp.execute("query-status")
                        controls.append({"seconds": round(elapsed, 2), "responsive": True, "running": status.get("running")})
                    except (TimeoutError, OSError):
                        control_responsive = False
                        controls.append({"seconds": round(elapsed, 2), "responsive": False})
                    next_control = time.monotonic() - started + 15
                time.sleep(0.1)
            if proc.poll() is not None:
                reason = "guest_or_qemu_exit"
            else:
                try:
                    qmp.execute("query-status")  # Drain queued reset/watchdog events.
                except (TimeoutError, OSError):
                    # A hung model/guest is an observation, not a lost receipt.
                    control_responsive = False
    finally:
        try:
            if qmp:
                qmp.close()
            if uart:
                uart.close()
        finally:
            try:
                if proc:
                    stop_guest(proc)
            finally:
                if terminal_settings is not None:
                    termios.tcsetattr(0, termios.TCSADRAIN, terminal_settings)
                # Always check preserved input, including failed experiments.
                verify_image(source)
    serial = (work / "serial.log").read_bytes()
    probes = [{"seconds": at, **probe_result(serial, token)} for at, token in pending]
    probe = next((p for p in probes if p.get("completed")), {"completed": False}) if args.probe else {"requested": False}
    reached = milestones(serial)
    reached["init_started"] |= probe.get("pid1_is_busybox", False) and probe.get("root_is_jffs2", False)
    result = {
        "mode": "unmodified-full-flash" if args.profile == "native" and args.watchdog_action == "reset" else f"diagnostic-{args.profile}-watchdog-{args.watchdog_action}",
        "profile": args.profile,
        "runner_sha256": runner_sha256,
        "watchdog_action": args.watchdog_action, "source_sha256": OEM_SHA256,
        "source_bytes": OEM_BYTES, "source_unchanged": True,
        "qemu_sha256": sha256_file(qemu),
        "disposable_flash_sha256_after": sha256_file(flash),
        "network": {"namespace_interfaces": ["lo"], "qemu_backends": "none; checked through QMP before execution"},
        "termination": reason, "seconds": round(time.monotonic() - started, 2),
        "milestones": reached, "private_experiment": str(work),
        "qmp_events": qmp.events if qmp else [],
        "qmp_responsive_at_end": control_responsive,
        "boot_sequences": serial.count(b"U-Boot 2016.11"),
        "console_probe": probe,
        "terminal_probes": probes, "qmp_checks": controls,
        "diagnostics": {"jffs2_gc_errors": serial.count(b"No free space left for GC") + serial.count(b"Newly-erased block contained word"),
                        "unsupported_erase4": (work / "qemu-debug.log").read_bytes().count(b"NOR unhandled SPI command 0xdc")},
        "qualification": "early OEM userspace/application observed; not hardware or sustained-operation qualification"
            if probe.get("application_original_sha256") else "boot experiment only; inspect individual milestones",
    }
    if args.storage_check:
        data = serial.replace(b"\r", b"")
        if resume_work is None:
            match = re.search(rb"\nPLAF203_STORAGE_" + token.encode() + rb"\|(yes|no)\|(\d+)\|([0-9a-f]{64})\|END\n", data)
            result["storage"] = {"completed": bool(match), "integrity_pass": bool(match and match[1] == b"yes"),
                                 "cycles": int(match[2]) if match else 0,
                                 "churn_bytes": int(match[2]) * 262144 if match else 0,
                                 "payload_sha256": match[3].decode() if match else None,
                                 "rootfs_sectors_with_erase_evidence": erased_sectors(storage_before, flash.read_bytes()) if storage_before else []}
        else:
            match = re.search(rb"\nPLAF203_RESTART_" + token.encode() + rb"\|([0-9a-f]{64})\|(yes|no)\|END\n", data)
            result["storage_restart"] = {"completed": bool(match), "payload_sha256": match[1].decode() if match else None,
                                         "deleted_file_still_absent": bool(match and match[2] == b"yes"),
                                         "same_disposable_flash": True}
    if args.storage_check and resume_work is None and result["storage"]["integrity_pass"]:
        from copy import copy
        resumed_args = copy(args)
        resumed_args.seconds = 65
        result["restart"] = run(resumed_args, resume_work=work, storage_token=token)
        result["storage"]["persisted_after_cold_restart"] = (
            result["restart"]["storage_restart"]["completed"] and
            result["restart"]["storage_restart"]["payload_sha256"] == result["storage"]["payload_sha256"])
    (work / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def storage_failures(result: dict) -> list[str]:
    storage = result.get("storage", {})
    restart = result.get("restart", {})
    checks = {
        "churn/readback": storage.get("integrity_pass") and storage.get("cycles") == 24,
        "erase evidence": bool(storage.get("rootfs_sectors_with_erase_evidence")),
        "same-flash persistence": storage.get("persisted_after_cold_restart"),
        "deleted file absent after restart": restart.get("storage_restart", {}).get("deleted_file_still_absent"),
        "no storage diagnostics": all(not r.get("diagnostics", {}).get(k, 1)
                                      for r in (result, restart) for k in ("jffs2_gc_errors", "unsupported_erase4")),
    }
    return [name for name, passed in checks.items() if not passed]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reconstruction-root", type=Path, required=True)
    parser.add_argument("--cache-root", type=Path, default=Path("/srv/cache/plaf203-bringup-research"))
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--log-limit-mib", type=int, choices=range(1, 65), default=8)
    parser.add_argument("--probe", action="store_true", help="Read-only serial shell probe after boot; never reads configuration or credentials")
    parser.add_argument("--watchdog-action", choices=("reset", "none"), default="reset",
                        help="none is an explicitly modified emulator policy, not native watchdog behavior")
    parser.add_argument("--profile", choices=("native", "app-disabled", "app-pty", "shell"), default="native")
    parser.add_argument("--interactive", action="store_true", help="Human-only raw terminal; use docker -it, output may contain secrets")
    parser.add_argument("--storage-check", action="store_true", help="Disposable-guest filesystem churn/readback and same-flash cold-restart test")
    args = parser.parse_args()
    if not 10 <= args.seconds <= 3600:
        parser.error("--seconds must be between 10 and 3600")
    if args.interactive and not (os.isatty(0) and os.isatty(1)):
        parser.error("--interactive requires a real terminal (docker -it)")
    if args.interactive and (args.probe or args.storage_check):
        parser.error("Interactive input cannot be combined with automatic serial probes/storage commands")
    if args.storage_check and (args.profile not in {"app-disabled", "app-pty"} or args.seconds < 90):
        parser.error("Storage qualification needs app-disabled/app-pty and at least 90 seconds")
    result = run(args)
    print(json.dumps(result, indent=2))
    if args.storage_check and storage_failures(result):
        raise SystemExit("Storage qualification failed: " + ", ".join(storage_failures(result)))


if __name__ == "__main__":
    main()
