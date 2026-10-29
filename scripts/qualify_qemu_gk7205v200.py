#!/usr/bin/env python3
"""Reproducible OpenIPC/QEMU GK7205V200 platform qualification harness for PLAF203.

Uses a separately built GK7205V200 QEMU machine model, qualifies baseline
OpenIPC kernel/rootfs boot, applies and verifies a synthetic QEMU DTB gpio3 fixup
and 32 MiB MX25L25635F NOR configuration, and validates virtual networking,
key-authenticated Dropbear SSH, unauthorized access rejection, GPIO3 sysfs
access (GPIO 25/26), guest timer rate, and SC_SYSRES reboot.

All large build artifacts, upstream source checkouts, and generated images
live outside the Git repository under /srv/cache/plaf203-bringup-research/.
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import select
import socket
import struct
import subprocess
import tempfile
import time
import zlib
from pathlib import Path
from typing import Any


DEFAULT_CACHE_ROOT = Path("/srv/cache/plaf203-bringup-research")
FDT_MAGIC = b"\xd0\x0d\xfe\xed"
OPENIPC_INPUT_HASHES = {
    "uImage.gk7205v200": "da61bc25b6268c444ca5b97bea0b851a60e85a68d3aaf21dea7b5a626fb66e6f",
    "rootfs.squashfs.gk7205v200": "2fadbc59b3e9c3ffe3409d671910cb330e2576ff839c5a7c016d4ba9c318145c",
}


def stop_guest(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def qualification_failures(summary: dict[str, Any]) -> list[str]:
    """A report is evidence, not success unless required guest assertions pass."""
    base = summary["baseline_boot"]
    guest = summary["qualified_plaf203_boot"]
    expected_gpio = {f"120b{i}000.gpio_chip" for i in range(8)}
    checks = {
        "baseline login": base["reached_login"],
        "patched login": guest["reached_login"],
        "GK machine": any(line.startswith("gk7205v200 ") for line in summary["registered_machines"]),
        "eight GPIO banks": set(guest["gpio_banks_registered"]) == expected_gpio,
        "no GPIO8 failure": not guest["gpio8_probe_failed"],
        "32 MiB NOR": "Chipsize 32 Mbytes" in guest["nor_chip_line"],
        "no CMA warning": guest["cma_warn_count"] == 0,
        "authorized SSH": guest["authorized_ssh_exit_code"] == 0,
        "unauthorized key denied by SSH": guest["unauthorized_key_exit_code"] == 255
            and "Permission denied (publickey)" in guest["unauthorized_ssh_stderr"],
        "password not offered": guest["password_auth_exit_code"] == 255
            and "Permission denied (publickey)" in guest["password_ssh_stderr"],
        "timer rate": 0.9 <= guest["guest_clock_ratio_5s"] <= 1.1,
        "reset": guest["hardware_reboot_verified"],
    }
    return [name for name, passed in checks.items() if not passed]

GPIO3_DTS_NODE = """\t\tgpio_chip@120b3000 {
\t\t\tcompatible = "arm,pl061", "arm,primecell";
\t\t\treg = <0x120b3000 0x1000>;
\t\t\tinterrupts = <0x00 0x13 0x04>;
\t\t\tclocks = <0x04 0x1f>;
\t\t\tclock-names = "apb_pclk";
\t\t\t#gpio-cells = <0x02>;
\t\t\tstatus = "okay";
\t\t};

\t\tgpio_chip@120b4000 {"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def patch_gk7205v200_dts_for_plaf203(dts_text: str) -> str:
    """Fix dtc string-list escapes, add gpio_chip@120b3000 (gpio3), and disable gpio_chip@120b8000."""
    normalized = dts_text.replace("\\0", '", "')
    if 'gpio3 = "/soc/gpio_chip@120b3000";' not in normalized:
        normalized = normalized.replace(
            '\t\tgpio2 = "/soc/gpio_chip@120b2000";\n',
            '\t\tgpio2 = "/soc/gpio_chip@120b2000";\n\t\tgpio3 = "/soc/gpio_chip@120b3000";\n',
            1,
        )
    if "gpio_chip@120b3000 {" not in normalized:
        if "\t\tgpio_chip@120b4000 {" not in normalized:
            raise ValueError("Expected gpio_chip@120b4000 anchor in GK7205V200 DTS")
        normalized = normalized.replace(
            "\t\tgpio_chip@120b4000 {",
            GPIO3_DTS_NODE,
            1,
        )
    normalized = re.sub(
        r'(gpio_chip@120b8000\s*\{[^}]*?)status = "okay";',
        r'\g<1>status = "disabled";',
        normalized,
        flags=re.DOTALL,
    )
    return normalized


def harden_dropbear_init_script(script_text: str) -> str:
    """Replace stock OpenIPC blank-password (-B) Dropbear flags with key-only (-s -g) flags and pre-generate an ephemeral hostkey in /tmp for read-only SquashFS initrd boots."""
    target_args = "-r /tmp/dropbear_ed25519_host_key -s -g -k -p 22 -K 300"
    key_gen_line = (
        "\t[ -f /tmp/dropbear_ed25519_host_key ] || "
        "dropbearkey -t ed25519 -f /tmp/dropbear_ed25519_host_key >/dev/null 2>&1"
    )
    updated = script_text
    if target_args not in updated:
        if "-R -B -k -p 22 -K 300" not in updated:
            raise ValueError("Unexpected Dropbear DAEMON_ARGS in S50dropbear")
        updated = updated.replace("-R -B -k -p 22 -K 300", target_args, 1)
    if (
        "\tumask 077" in updated
        and "dropbearkey -t ed25519 -f /tmp/dropbear_ed25519_host_key" not in updated
    ):
        updated = updated.replace(
            "\tumask 077",
            f"\tumask 077\n{key_gen_line}",
            1,
        )
    return updated


def rewrite_passwd_root_shell(passwd_text: str, shell: str = "/bin/ash") -> str:
    """Replace root's interactive claim wrapper with a standard shell for automated test fixtures."""
    return re.sub(
        r"^(root:[^:\n]*:[^:\n]*:[^:\n]*:[^:\n]*:[^:\n]*:).*$",
        rf"\g<1>{shell}",
        passwd_text,
        count=1,
        flags=re.MULTILINE,
    )


def parse_unimp_addresses(unimp_log_text: str) -> list[str]:
    """Extract unique sorted unimplemented MMIO base/offset messages from QEMU -d unimp,guest_errors."""
    seen: set[str] = set()
    for line in unimp_log_text.splitlines():
        line_str = line.strip()
        if line_str:
            seen.add(line_str)
    return sorted(seen)


def build_patched_uimage(uimage_orig: Path, uimage_out: Path) -> dict[str, Any]:
    if sha256_file(uimage_orig) != OPENIPC_INPUT_HASHES["uImage.gk7205v200"]:
        raise ValueError("Kernel does not match the qualified OpenIPC release")
    data = uimage_orig.read_bytes()
    dtb_offset = data.rfind(FDT_MAGIC)
    if dtb_offset < 64:
        raise ValueError(f"No appended DTB found in {uimage_orig}")
    zimage_bytes = data[64:dtb_offset]
    orig_dtb = data[dtb_offset:]

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        dtb_in = tmp / "orig.dtb"
        dts_file = tmp / "patched.dts"
        dtb_out = tmp / "patched.dtb"
        zimage_out = tmp / "zImage+dtb"

        dtb_in.write_bytes(orig_dtb)
        raw_dts = subprocess.check_output(
            ["dtc", "-I", "dtb", "-O", "dts", str(dtb_in)],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8")
        patched_dts = patch_gk7205v200_dts_for_plaf203(raw_dts)
        dts_file.write_text(patched_dts, encoding="utf-8")
        subprocess.check_call(
            ["dtc", "-q", "-I", "dts", "-O", "dtb", "-o", str(dtb_out), str(dts_file)]
        )
        patched_dtb_bytes = dtb_out.read_bytes()
        zimage_out.write_bytes(zimage_bytes + patched_dtb_bytes)
        subprocess.check_call(
            [
                "mkimage",
                "-A",
                "arm",
                "-O",
                "linux",
                "-T",
                "kernel",
                "-C",
                "none",
                "-a",
                "0x40008000",
                "-e",
                "0x40008000",
                "-n",
                "Linux-4.9.37-gk7205v200-plaf203",
                "-d",
                str(zimage_out),
                str(uimage_out),
            ],
            stdout=subprocess.DEVNULL,
            env={**os.environ, "SOURCE_DATE_EPOCH": os.environ.get("SOURCE_DATE_EPOCH", "1738368000")},
        )

    return {
        "orig_uimage_bytes": len(data),
        "orig_uimage_sha256": sha256_file(uimage_orig),
        "orig_dtb_offset": dtb_offset,
        "orig_dtb_bytes": len(orig_dtb),
        "orig_dtb_sha256": hashlib.sha256(orig_dtb).hexdigest(),
        "patched_uimage_bytes": uimage_out.stat().st_size,
        "patched_uimage_sha256": sha256_file(uimage_out),
        "patched_dtb_bytes": len(patched_dtb_bytes),
        "patched_dtb_sha256": hashlib.sha256(patched_dtb_bytes).hexdigest(),
    }


def run_serial_boot_until_login(
    cmd: list[str], timeout_sec: float = 120.0, marker: bytes = b"login:"
) -> tuple[subprocess.Popen[bytes], bytes, float]:
    t0 = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    # Also clean up on a later SSH/probe exception, not only the happy path.
    atexit.register(stop_guest, proc)
    buf = read_serial_until(proc, marker, timeout_sec)
    elapsed = time.monotonic() - t0
    return proc, buf, elapsed


def read_serial_until(proc: subprocess.Popen, marker: bytes, timeout_sec: float) -> bytes:
    buf = bytearray()
    deadline = time.monotonic() + timeout_sec
    assert proc.stdout is not None
    while time.monotonic() < deadline:
        ready, _, _ = select.select([proc.stdout], [], [], 0.2)
        if ready:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            buf.extend(chunk)
            if marker in buf:
                break
    return bytes(buf)


def use_source_kernel(image: Path, destination: Path) -> dict[str, Any]:
    """Consume a checked build output verbatim, never patch its compiled DTB."""
    record = json.loads((image.parent / "build-receipt.json").read_text())
    payload = image.read_bytes()
    zimage = image.parent / "zImage"
    dtb = image.parent / "plaf203-qemu.dtb"
    for name, path in (("uImage", image), ("zImage", zimage), ("plaf203-qemu.dtb", dtb)):
        if sha256_file(path) != record["artifacts"][name]:
            raise ValueError(f"Source-built kernel artifact does not match receipt: {name}")
    if len(payload) < 64 or struct.unpack_from(">I", payload)[0] != 0x27051956:
        raise ValueError("Invalid source-built uImage header")
    header = bytearray(payload[:64])
    expected_crc = struct.unpack_from(">I", header, 4)[0]
    struct.pack_into(">I", header, 4, 0)
    if zlib.crc32(header) != expected_crc:
        raise ValueError("Source-built uImage header CRC mismatch")
    size, load, entry, crc = struct.unpack_from(">IIII", payload, 12)
    if (size != len(payload) - 64 or zlib.crc32(payload[64:]) != crc
            or load != 0x40008000 or entry != load or payload[28:32] != bytes([5, 2, 2, 0])):
        raise ValueError("Source-built uImage layout/CRC mismatch")
    if payload[64:] != zimage.read_bytes() + dtb.read_bytes():
        raise ValueError("Source-built uImage must contain exactly zImage plus its compiled DTB")
    zdata, ddata = zimage.read_bytes(), dtb.read_bytes()
    if len(zdata) < 0x30 or struct.unpack_from("<I", zdata, 0x24)[0] != 0x016f2818:
        raise ValueError("Invalid source-built ARM zImage")
    if len(ddata) < 40 or struct.unpack_from(">II", ddata) != (0xd00dfeed, len(ddata)):
        raise ValueError("Invalid source-built DTB header/size")
    destination.write_bytes(payload)
    return {"mode": "source-built; no post-build DTB rewrite", "uimage_sha256": sha256_file(image),
            "dtb_sha256": sha256_file(dtb), "zimage_sha256": sha256_file(zimage),
            "release": record["kernel"]["release"], "sources": record["sources"]["kernel"],
            "build_receipt_sha256": sha256_file(image.parent / "build-receipt.json")}


def check_kernel_diagnostics(log: str) -> dict[str, Any]:
    serious = [line for line in log.splitlines() if re.search(
        r"\b(?:Kernel panic|Oops:|BUG:)|WARNING:.* at |Call Trace:|(?:jffs2|mtd|ubi).*\b(?:error|warning|failed)\b", line, re.I)]
    if serious:
        raise RuntimeError("Unexpected kernel integrity/storage diagnostics: " + "; ".join(serious[:4]))
    return {"serious_errors": serious,
            "probe_errors": [line for line in log.splitlines() if re.search(r"failed|error|warn", line, re.I)],
            "gpio_banks": sorted(set(re.findall(r"pl061_gpio (120b[0-9]000\.gpio_chip):.*registered", log)))}


def qualify_native_runtime(probe) -> dict[str, Any]:
    """Exercise the actual ARM service; all fault injection is guest-only."""
    def status(predicate=lambda s: s["state"] == "running", timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = probe("cat /run/plaf203/status.json", check=False)
            try:
                value = json.loads(result.stdout)
                if result.returncode == 0 and predicate(value):
                    return value
            except (ValueError, KeyError):
                pass
            time.sleep(0.1)
        raise RuntimeError("Native runtime did not reach the expected health state")

    first = status()
    if (first["actuation_enabled"] or first["watchdog"] != "simulated" or
            first["control_backend"] != "synthetic" or first["sensor_backend"] != "synthetic" or
            first["fault_mask"] or first["ready_mask"] != 7 or not first["feed_allowed"]):
        raise RuntimeError("Unexpected native runtime capability/health declaration")
    executed = probe("/usr/libexec/plaf203-runtime-selftest").stdout.strip()
    if executed != "PLAF203_RUNTIME_SELFTEST_OK":
        raise RuntimeError("ARM deterministic health self-test did not pass")
    progressed = status(lambda s: s["feeds"] > first["feeds"] and
                        all(b > a for a, b in zip(first["sequences"], s["sequences"])))
    modes = probe("ls -ld /run/plaf203 /run/plaf203/status.json").stdout.splitlines()
    if [line.split()[0] for line in modes] != ["drwx------", "-rw-------"]:
        raise RuntimeError("Runtime state/status permissions are not private")
    probe("/usr/sbin/plaf203-runtime >/dev/null 2>&1 && exit 1; test $? = 75")
    pid = int(progressed["pid"])
    # A stopped loop must not refresh itself healthy upon resumption.
    probe(f"kill -STOP {pid}; sleep 5; kill -CONT {pid}")
    faulted = status(lambda s: s["state"] == "faulted")
    if faulted["feed_allowed"] or not faulted["fault_mask"] & 2:
        raise RuntimeError("Stalled essential owner did not inhibit feeds and latch a fault")
    time.sleep(1.2)
    if status(lambda s: s["state"] == "faulted")["feeds"] != faulted["feeds"]:
        raise RuntimeError("Faulted runtime continued simulated watchdog feeds")
    probe(f"kill -KILL {pid}")
    restarted = status(lambda s: s["state"] == "running" and s["generation"] != first["generation"])
    # Observe shutdown in the same guest SSH command. A new SSH handshake may
    # outlast the respawn floor and miss the deliberately brief snapshot.
    stopped = probe(f"kill -TERM {int(restarted['pid'])}; i=0; while [ $i -lt 60 ]; do "
                    "if grep -q '\"state\":\"shutdown\"' /run/plaf203/status.json; then "
                    "cat /run/plaf203/status.json; exit 0; fi; i=$((i+1)); sleep 0.05; done; exit 1")
    shutdown = json.loads(stopped.stdout)
    if shutdown["feed_allowed"] or shutdown["watchdog_armed"]:
        raise RuntimeError("Orderly shutdown left watchdog feed policy enabled")
    final = status(lambda s: s["state"] == "running" and s["generation"] != restarted["generation"])
    resource = probe(f"grep -E 'VmRSS:|VmSize:|Threads:' /proc/{int(final['pid'])}/status").stdout
    driver = probe("set -e; modprobe sp805_wdt; test -d /sys/module/sp805_wdt; "
                   "test ! -e /dev/watchdog; test ! -e /dev/watchdog0; "
                   "modprobe -r sp805_wdt; echo PLAF203_SP805_UNBOUND_EXPECTED").stdout.strip()
    return {"selftest": executed, "initial": first, "faulted": faulted,
            "restarted": restarted, "orderly_shutdown": shutdown, "final": final,
            "resources": resource.splitlines(), "exclusive_owner_verified": True,
            "stalled_feed_inhibition_verified": True, "sp805_module": driver,
            "hardware_watchdog_coverage": False}


def qualify_feeding_operations(probe, cycle: int, previous_highwater: str) -> dict[str, Any]:
    """Use the single real ARM owner and its actual JFFS2 journal."""
    def ctl(command):
        return json.loads(probe("plaf203-ctl " + command).stdout)

    def wait_command(operation_id, state):
        return (f"i=0; while [ $i -lt 80 ]; do r=$(plaf203-ctl QUERY {operation_id}); "
                f"if echo \"$r\" | grep -q '\"state\":\"{state}\"'; then "
                "echo \"$r\"; break; fi; i=$((i+1)); sleep 0.05; done; test $i -lt 80")

    initial = ctl("STATUS")
    if (initial["status"]["blocked"] or initial["status"]["highwater"] != previous_highwater or
            initial["status"]["synthetic_starts"] or initial["status"]["commits_this_owner"]):
        raise RuntimeError("Journal continuity failed or startup replayed/wrote completed work")
    selftest = probe("/usr/libexec/plaf203-feeding-selftest").stdout.strip() if cycle == 0 else None
    if cycle == 0 and not selftest.startswith("PLAF203_FEEDING_SELFTEST_OK"):
        raise RuntimeError("ARM feeding/crash self-tests failed")
    operation_id = int(previous_highwater)
    completed = []
    for origin, units in (("manual", 1), ("scheduled", 2)):
        operation_id += 1
        correlation = f"qemu-{origin}-{cycle}"
        output = probe(f"plaf203-ctl SUBMIT {operation_id} {origin} {units} {correlation}; " +
                       wait_command(operation_id, "succeeded")).stdout.splitlines()
        admitted, finished = [json.loads(line) for line in output]
        if (admitted["result"] != "accepted" or finished["operation"]["completed_synthetic_units"] != units or
                finished["operation"]["state"] != "succeeded"):
            raise RuntimeError("Native synthetic operation did not complete with the correct contract")
        duplicate = ctl(f"SUBMIT {operation_id} {origin} {units} {correlation}")
        conflict = ctl(f"SUBMIT {operation_id} {origin} {units} conflicting-correlation")
        if (duplicate["result"] != "duplicate" or conflict["result"] != "conflict" or
                duplicate["status"]["revision"] != finished["status"]["revision"] or
                conflict["status"]["synthetic_starts"] != finished["status"]["synthetic_starts"]):
            raise RuntimeError("Repeated/conflicting operation identity mutated or repeated work")
        completed.append(finished)

    operation_id += 1
    cancel_script = (f"set -e; plaf203-ctl SIMULATE hold; plaf203-ctl SUBMIT {operation_id} manual 1 cancel-{cycle}; "
        "i=0; while [ $i -lt 40 ]; do r=$(plaf203-ctl STATUS); "
        "if echo \"$r\" | grep -q '\"state\":\"actuating\"'; then break; fi; "
        "i=$((i+1)); sleep 0.05; done; test $i -lt 40; "
        f"plaf203-ctl CANCEL {operation_id}; " + wait_command(operation_id, "cancelled"))
    cancellation = [json.loads(line) for line in probe(cancel_script).stdout.splitlines()]
    if cancellation[-1]["operation"]["completed_synthetic_units"] != 0 or cancellation[-2]["result"] != "accepted":
        raise RuntimeError("Cancellation did not wait for verified synthetic stop")
    ctl("SIMULATE normal")
    operation_id += 1
    concurrent = (f"plaf203-ctl SUBMIT {operation_id} manual 1 concurrent-a > /tmp/feed-a & "
                  f"plaf203-ctl SUBMIT {operation_id} manual 1 concurrent-b > /tmp/feed-b & wait; "
                  "cat /tmp/feed-a /tmp/feed-b; " + wait_command(operation_id, "succeeded"))
    simultaneous = [json.loads(line) for line in probe(concurrent).stdout.splitlines()]
    if {r["result"] for r in simultaneous[:2]} != {"accepted", "conflict"}:
        raise RuntimeError("Concurrent callers did not serialize to one operation")
    final = simultaneous[-1]
    if final["status"]["synthetic_starts"] != 5 or final["status"]["retained"] > 16:
        raise RuntimeError("Unexpected synthetic actuation count or unbounded journal retention")
    sizes = probe("wc -c /state/operations/snapshot /state/feeding.required; "
                  "ls -ld /state/operations /state/operations/snapshot /state/feeding.required").stdout
    if "4096 /state/operations/snapshot" not in sizes or "32 /state/feeding.required" not in sizes:
        raise RuntimeError("Journal storage bounds changed")
    return {"initial": initial, "arm_selftest": selftest, "completed": completed,
            "cancellation": cancellation[-1], "concurrent_requests": simultaneous[:2],
            "final": final, "storage": sizes.splitlines()}


def qualify_indeterminate_recovery(probe, runtime_pid: int, operation_id: int) -> dict[str, Any]:
    script = (f"set -e; plaf203-ctl SIMULATE hold; plaf203-ctl SUBMIT {operation_id} manual 1 crash-at-effect; "
        "i=0; while [ $i -lt 40 ]; do r=$(plaf203-ctl STATUS); "
        "if echo \"$r\" | grep -q '\"state\":\"actuating\"'; then echo \"$r\"; break; fi; "
        "i=$((i+1)); sleep 0.05; done; test $i -lt 40; "
        f"kill -KILL {runtime_pid}; "
        "i=0; while [ $i -lt 100 ]; do r=$(plaf203-ctl STATUS 2>/dev/null || true); "
        "if echo \"$r\" | grep -q '\"state\":\"indeterminate\"'; then echo \"$r\"; break; fi; "
        "i=$((i+1)); sleep 0.05; done; test $i -lt 100")
    observed = [json.loads(line) for line in probe(script).stdout.splitlines()]
    recovered = observed[-1]
    if not recovered["status"]["blocked"] or recovered["status"]["synthetic_starts"]:
        raise RuntimeError("Restart did not inhibit/reconcile uncertain work without replay")
    denied = json.loads(probe(f"plaf203-ctl SUBMIT {operation_id + 1} manual 1 must-not-execute").stdout)
    if denied["result"] != "blocked":
        raise RuntimeError("Uncertain operation did not prevent subsequent admission")
    health = json.loads(probe("cat /run/plaf203/status.json").stdout)
    if health["state"] != "faulted" or health["feed_allowed"]:
        raise RuntimeError("Indeterminate recovery did not inhibit essential-health watchdog feeds")
    return {"before_crash": observed[1], "recovered": recovered, "new_request": denied, "health": health}


def run_minimal_flash_qualification(cache_root: Path, rootfs: Path, key: Path,
                                    expected_failure: str | None = None,
                                    bootloader: Path | None = None,
                                    kernel_image: Path | None = None,
                                    runtime_checks: bool = False,
                                    feeding_checks: bool = False) -> dict[str, Any]:
    """Boot source-built userspace from NOR; optionally load kernel through U-Boot.

    Keep failed images/logs in a private experiment directory. Never touch an
    OEM capture, reuse an existing flash image, or connect to a physical device.
    """
    if key.stat().st_mode & 0o077:
        raise ValueError("Test private key must not have group/other permissions (use chmod 600)")
    boot_dir = cache_root / "qemu-bench/qemu-boot"
    qemu = cache_root / "qemu-bench/qemu-src/build/qemu-system-arm"
    work = Path(tempfile.mkdtemp(prefix="minimal-flash-", dir=cache_root))
    kernel = work / "uImage"
    dtb = (use_source_kernel(kernel_image, kernel) if kernel_image else
           build_patched_uimage(boot_dir / "uImage.gk7205v200", kernel))
    flash = work / "nor.bin"
    image = bytearray(b"\xff" * (32 * 1024 * 1024))
    bootargs = ("console=ttyAMA0,115200 root=/dev/mtdblock3 rootfstype=squashfs ro "
                "mem=45M osmem=45M mmz_allocator=gk "
                "mtdparts=sfc:512k(boot),512k(env),4m(kernel),16m(rootfs),11m(state)")
    for source, offset, capacity in ((kernel, 0x100000, 4 << 20), (rootfs, 0x500000, 16 << 20)):
        payload = source.read_bytes()
        if len(payload) > capacity:
            raise ValueError(f"Image exceeds development NOR slot: {source.name}")
        image[offset:offset + len(payload)] = payload
    if bootloader:
        if sha256_file(bootloader) != "a41b42026e22655b2c6f7c874bdfca3686106b3b7a285cd7948ad83869282bb0":
            raise ValueError("Bootloader does not match the verified OEM mtd0 capture")
        image[:0x80000] = bootloader.read_bytes()
        environment = ("bootdelay=0\0baudrate=115200\0"
                       "bootcmd=sf probe 0; sf read 0x42000000 0x100000 0x400000; bootm 0x42000000\0"
                       f"bootargs={bootargs}\0\0").encode().ljust(0x40000 - 4, b"\0")
        image[0x80000:0x100000] = bytes(0x80000)
        image[0x80000:0xc0000] = struct.pack("<I", zlib.crc32(environment)) + environment
    flash.write_bytes(image)
    flash.chmod(0o600)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    cmd = [str(qemu), "-M", "gk7205v200,sensor=none",
           "-global", "driver=hisi-fmc,property=flash-jedec,value=0xc22019",
           "-global", f"driver=hisi-fmc,property=flash-file,value={flash}",
           "-kernel", str(kernel), "-nographic", "-monitor", "none",
           "-nic", f"user,hostfwd=tcp:127.0.0.1:{port}-:22",
           "-append", bootargs]
    if bootloader:
        # Do NOT use machine flash-file: the pinned model guesses NAND for >16M.
        start = cmd.index("-kernel")
        del cmd[start:start + 2]
        cmd = cmd[:cmd.index("-append")]
    ssh = ["ssh", "-F", "/dev/null", "-p", str(port),
           "-o", "IdentityAgent=none", "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
           "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
           "-o", f"UserKnownHostsFile={work / 'known_hosts'}"]

    def probe(command: str, extra: list[str] | None = None, check: bool = True, identity: Path = key):
        result = subprocess.run(ssh + ["-i", str(identity)] + (extra or []) + ["root@127.0.0.1", command],
                                capture_output=True, text=True, timeout=35, check=False)
        if check and result.returncode:
            (work / "failed-probe.stderr").write_text(result.stderr)
            (work / "failed-probe.stdout").write_text(result.stdout)
            raise RuntimeError(f"SSH probe failed ({result.returncode}); inspect {work}/failed-probe.stderr")
        return result

    if expected_failure:
        expected = f"PLAF203_SSH_DISABLED: {expected_failure}".encode()
        proc, serial, _ = run_serial_boot_until_login(cmd, marker=expected)
        (work / "disabled-enrollment.log").write_bytes(serial)
        try:
            if expected not in serial or probe("true", check=False).returncode == 0:
                raise RuntimeError(f"Enrollment failure did not disable SSH; inspect {work}")
            if runtime_checks:
                serial += read_serial_until(proc, b'"state":"running"', 30)
                (work / "disabled-enrollment.log").write_bytes(serial)
                if b'"state":"running"' not in serial:
                    raise RuntimeError("Native runtime depended on SSH enrollment readiness")
        finally:
            stop_guest(proc)
        return {"scope": "NOR-rootfs negative enrollment test", "experiment": str(work),
                "rootfs_sha256": sha256_file(rootfs), "ssh_disabled_reason": expected_failure,
                "runtime_independent_of_enrollment": runtime_checks}

    wrong_key = work / "unauthorized"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(wrong_key)], check=True)
    # Default cache ACLs can broaden newly created key modes. A negative test
    # must reach server authentication, not fail local OpenSSH permission checks.
    wrong_key.chmod(0o600)
    boots = []
    for cycle in range(3):
        proc, serial, seconds = run_serial_boot_until_login(cmd, marker=b"PLAF203_SSH_READY")
        (work / f"boot-{cycle}.log").write_bytes(serial)
        try:
            if b"PLAF203_SSH_READY" not in serial:
                raise RuntimeError(f"Minimal guest startup failed; inspect {work}")
            if bootloader and (b"SPI Nor total size: 32MB" not in serial or b"Starting kernel" not in serial):
                raise RuntimeError("Kernel boot through NOR/U-Boot was not observed")
            deadline = time.monotonic() + 60
            while (readiness := probe("true", check=False)).returncode:
                if time.monotonic() >= deadline:
                    (work / "ssh-readiness.stderr").write_text(readiness.stderr)
                    raise RuntimeError(f"Guest SSH never became ready; inspect {work}/ssh-readiness.stderr")
                time.sleep(1)
            kernel_log = probe("dmesg").stdout
            (work / f"kernel-{cycle}.log").write_text(kernel_log)
            diagnostics = check_kernel_diagnostics(kernel_log)
            if kernel_image and set(diagnostics["gpio_banks"]) != {f"120b{i}000.gpio_chip" for i in range(8)}:
                raise RuntimeError("Source-built QEMU DTB did not register the eight qualified GPIO banks")
            output = probe("set -e; uname -r; cat /proc/mounts; cat /proc/mtd; ip addr show eth0; "
                           "cat /sys/module/goke_mem/parameters/osmem; "
                           "test -s /state/dropbear/host_key; "
                           "awk '$4 == \"0A\" {n++; split($2,a,\":\"); if(a[2] != \"0016\") bad=1} "
                           "END {exit (bad || !n)}' /proc/net/tcp /proc/net/tcp6; "
                           "if touch /etc/plaf203-ro-test 2>/dev/null; then exit 1; fi; "
                           "if [ -f /state/qualification ]; then cat /state/qualification; "
                           "else echo nor-persist-v1 > /state/qualification; fi; sync").stdout
            if not re.search(r"/dev/root / squashfs ro[, ]", output):
                raise RuntimeError("Root filesystem is not read-only SquashFS")
            if not re.search(r"/dev/mtdblock4 /state jffs2 rw[, ]", output):
                raise RuntimeError("Persistent JFFS2 state did not mount")
            if cycle and "nor-persist-v1" not in output:
                raise RuntimeError("NOR state was not retained across a fresh QEMU process")
            if not re.search(r"inet 10\.0\.2\.", output):
                raise RuntimeError("Guest DHCP address absent")
            denied = probe("true", ["-o", "PubkeyAuthentication=no",
                                    "-o", "PreferredAuthentications=password,keyboard-interactive"], False)
            if denied.returncode != 255 or "Permission denied (publickey)" not in denied.stderr:
                raise RuntimeError("Guest offered password authentication")
            denied_key = probe("true", check=False, identity=wrong_key)
            if denied_key.returncode != 255 or "Permission denied (publickey)" not in denied_key.stderr:
                raise RuntimeError("Unauthorized key was not rejected by the guest SSH server")
            module_probe = probe("set -e; if [ -f /lib/modules/$(uname -r)/kernel/drivers/net/tun.ko ]; then "
                                 "modprobe tun; test -d /sys/module/tun; modprobe -r tun; "
                                 "test ! -d /sys/module/tun; echo PLAF203_TUN_MODULE_OK; fi").stdout
            runtime = qualify_native_runtime(probe) if runtime_checks else None
            feeding = qualify_feeding_operations(probe, cycle,
                boots[-1]["feeding"]["final"]["status"]["highwater"] if cycle else "0") if feeding_checks else None
            kernel_log = probe("dmesg").stdout
            (work / f"kernel-{cycle}.log").write_text(kernel_log)
            diagnostics = check_kernel_diagnostics(kernel_log)
            boots.append({"seconds": round(seconds, 2), "probe": output.splitlines(), "kernel_diagnostics": diagnostics,
                          "tun_module_load_unload": "PLAF203_TUN_MODULE_OK" in module_probe,
                          "native_runtime": runtime, "feeding": feeding})
            if cycle == 0:
                reboot = subprocess.Popen(ssh + ["-i", str(key), "root@127.0.0.1", "sync; reboot -f"],
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    reset_serial = read_serial_until(proc, b"PLAF203_SSH_READY", 120)
                    (work / "software-reset.log").write_bytes(reset_serial)
                    if b"Restarting system" not in reset_serial or b"PLAF203_SSH_READY" not in reset_serial:
                        raise RuntimeError("Guest reboot did not return to the SSH service")
                finally:
                    # A reset need not close the old TCP session cleanly. Guest
                    # boot evidence, not SSH's disconnect timing, proves reset.
                    if reboot.poll() is None:
                        reboot.terminate()
                    try:
                        reboot.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        reboot.kill()
                        reboot.wait(timeout=5)
                if runtime_checks:
                    # Separate MODEL test, not a Linux driver qualification.
                    # This is only the disposable GK7205V200 QEMU process above;
                    # never run this command on a physical device.
                    deadline = time.monotonic() + 35
                    while probe("true", check=False).returncode:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("SSH failed to recover after software reset")
                        time.sleep(0.2)
                    if feeding_checks:
                        persisted = json.loads(probe("plaf203-ctl STATUS").stdout)
                        if (persisted["status"]["highwater"] != feeding["final"]["status"]["highwater"] or
                                persisted["status"]["synthetic_starts"] or persisted["status"]["blocked"]):
                            raise RuntimeError("Software reboot lost or replayed the operation journal")
                        boots[-1]["feeding_software_reboot"] = persisted
                    identity = probe("devmem 0x12030fe0 32; devmem 0x12030fe4 32; "
                                     "devmem 0x12030fe8 32; devmem 0x12030fec 32").stdout.split()
                    (work / "modeled-watchdog-identity.json").write_text(json.dumps(identity))
                    if [int(v, 16) for v in identity] != [0x24, 0xb8, 0x1b, 0]:
                        raise RuntimeError("Unexpected modeled watchdog identity; refusing MMIO reset test")
                    probe("sync; devmem 0x12030c00 32 0x1acce551; "
                          "devmem 0x12030000 32 3000000; devmem 0x12030008 32 3; "
                          "devmem 0x12030c00 32 1")
                    reset_serial = read_serial_until(proc, b"PLAF203_SSH_READY", 120)
                    (work / "modeled-watchdog-reset.log").write_bytes(reset_serial)
                    if b"U-Boot" not in reset_serial or b"PLAF203_SSH_READY" not in reset_serial:
                        raise RuntimeError("Modeled watchdog expiration did not reboot through U-Boot/userspace")
                    boots[-1]["modeled_watchdog_reset"] = {
                        "verified": True, "peripheral_id": "0x001bb824", "clock_hz": 3000000,
                        "load": 3000000, "method": "emulation-only MMIO, NOT /dev/watchdog"}
            if cycle == 2:
                if feeding_checks:
                    boots[-1]["feeding_crash"] = qualify_indeterminate_recovery(
                        probe, int(runtime["final"]["pid"]), int(feeding["final"]["status"]["highwater"]) + 1)
                else:
                    probe("printf 'invalid-schema\\n' > /state/schema; sync")
        finally:
            stop_guest(proc)
    indeterminate_cold_boot = None
    corrupt_journal_cold_boot = None
    if feeding_checks:
        proc, serial, _ = run_serial_boot_until_login(cmd, marker=b"PLAF203_SSH_READY")
        (work / "indeterminate-cold-boot.log").write_bytes(serial)
        try:
            deadline = time.monotonic() + 45
            while True:
                result = probe("plaf203-ctl STATUS", check=False)
                if result.returncode == 0:
                    indeterminate_cold_boot = json.loads(result.stdout)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Could not query recovered journal after cold boot")
                time.sleep(0.2)
            status = indeterminate_cold_boot["status"]
            if not status["blocked"] or status["current"]["state"] != "indeterminate" or status["synthetic_starts"]:
                raise RuntimeError("Cold boot lost uncertainty or replayed an operation")
            probe("printf X | dd of=/state/operations/snapshot bs=1 seek=128 count=1 conv=notrunc; sync")
        finally:
            stop_guest(proc)
        proc, serial, _ = run_serial_boot_until_login(cmd, marker=b"PLAF203_SSH_READY")
        (work / "corrupt-journal-cold-boot.log").write_bytes(serial)
        try:
            deadline = time.monotonic() + 45
            while True:
                result = probe("plaf203-ctl STATUS", check=False)
                if result.returncode == 0:
                    corrupt_journal_cold_boot = json.loads(result.stdout)
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Could not inspect corrupt-journal fail-closed status")
                time.sleep(0.2)
            status = corrupt_journal_cold_boot["status"]
            if not status["storage_failed"] or not status["blocked"] or status["synthetic_starts"]:
                raise RuntimeError("Journal corruption was silently accepted/reset")
            rejected = json.loads(probe("plaf203-ctl QUERY 1").stdout)
            if rejected["result"] != "storage_error":
                raise RuntimeError("Corrupt journal produced a trustworthy operation result")
            corrupt_journal_cold_boot["query"] = rejected
            probe("printf 'invalid-schema\\n' > /state/schema; sync")
        finally:
            stop_guest(proc)
    # Corrupt persistent state must fail closed, not replace/erase its contents.
    proc, serial, _ = run_serial_boot_until_login(cmd, marker=b"PLAF203_SSH_DISABLED")
    (work / "invalid-state.log").write_bytes(serial)
    try:
        if b"PLAF203_STATE_FAILED: schema" not in serial or b"PLAF203_SSH_DISABLED" not in serial:
            raise RuntimeError("Invalid persistent state was silently accepted")
        if probe("true", check=False).returncode == 0:
            raise RuntimeError("SSH still accessible with invalid persistent state")
    finally:
        stop_guest(proc)
    environment_rejected = None
    if bootloader:
        # Corrupt only the synthetic image's environment CRC, never the capture.
        with flash.open("r+b") as handle:
            handle.seek(0x80000)
            value = handle.read(1)
            handle.seek(0x80000)
            handle.write(bytes([value[0] ^ 1]))
        proc, serial, _ = run_serial_boot_until_login(cmd, marker=b"goke #")
        (work / "invalid-environment.log").write_bytes(serial)
        try:
            if b"bad crc" not in serial.lower() or b"goke #" not in serial:
                raise RuntimeError("Synthetic environment CRC failure did not reach expected recovery boundary")
            environment_rejected = True
        finally:
            stop_guest(proc)
    return {"scope": "SPI NOR/U-Boot/kernel/rootfs boot" if bootloader else "NOR-rootfs boot; RAM-loaded kernel",
            "bootloader_sha256": sha256_file(bootloader) if bootloader else None,
            "rootfs_sha256": sha256_file(rootfs), "kernel": dtb, "experiment": str(work),
            "cold_boots": boots, "software_reboot_verified": True, "invalid_state_fails_closed": True,
            "synthetic_environment_crc_rejected": environment_rejected,
            "feeding_indeterminate_cold_boot": indeterminate_cold_boot,
            "feeding_corrupt_journal_cold_boot": corrupt_journal_cold_boot,
            "qemu_sha256": sha256_file(qemu)}


def run_enrollment_negatives(cache_root: Path, rootfs: Path, key: Path,
                              bootloader: Path | None = None,
                              kernel_image: Path | None = None,
                              runtime_checks: bool = False) -> list[dict[str, Any]]:
    """Malformed image fixtures vary only enrollment; private outputs stay in cache."""
    work = Path(tempfile.mkdtemp(prefix="enrollment-fixtures-", dir=cache_root))
    tree = work / "rootfs"
    subprocess.run(["unsquashfs", "-d", str(tree), str(rootfs)], check=True,
                   stdout=subprocess.DEVNULL)
    enrollment = tree / "etc/plaf203/enrollment.pub"
    results = []
    for reason in ("no-enrollment", "invalid-enrollment"):
        if reason == "no-enrollment":
            enrollment.unlink(missing_ok=True)
        else:
            enrollment.write_text("not-an-ssh-key\n")
        fixture = work / f"{reason}.squashfs"
        subprocess.run(["mksquashfs", str(tree), str(fixture), "-comp", "xz", "-noappend", "-no-xattrs",
                        "-processors", "2", "-all-root"],
                       check=True, stdout=subprocess.DEVNULL)
        results.append(run_minimal_flash_qualification(cache_root, fixture, key, reason, bootloader, kernel_image, runtime_checks))
    return results


def run_qualification(cache_root: Path) -> dict[str, Any]:
    qemu_bench = cache_root / "qemu-bench"
    boot_dir = qemu_bench / "qemu-boot"
    qemu_bin = qemu_bench / "qemu-src" / "build" / "qemu-system-arm"
    uimage_orig = boot_dir / "uImage.gk7205v200"
    rootfs_orig = boot_dir / "rootfs.squashfs.gk7205v200"
    for name, expected in OPENIPC_INPUT_HASHES.items():
        if sha256_file(boot_dir / name) != expected:
            raise ValueError(f"Unqualified OpenIPC input: {name}")

    version_out = subprocess.check_output([str(qemu_bin), "--version"], text=True).strip()
    machines_out = subprocess.check_output([str(qemu_bin), "-machine", "help"], text=True)
    gk_machines = [
        line.strip()
        for line in machines_out.splitlines()
        if any(tok in line for tok in ("gk7205v200", "hi3516ev200"))
    ]

    # 1. Baseline boot of unmodified OpenIPC uImage + rootfs
    baseline_cmd = [
        str(qemu_bin),
        "-M",
        "gk7205v200,sensor=none",
        "-kernel",
        str(uimage_orig),
        "-initrd",
        str(rootfs_orig),
        "-nographic",
        "-serial",
        "stdio",
        "-monitor",
        "none",
        "-nic",
        "user,restrict=on",
        "-append",
        "console=ttyAMA0,115200 earlyprintk root=/dev/ram0 rootfstype=squashfs "
        "mtdparts=sfc:256k(boot),64k(env),3072k(kernel),10240k(rootfs),-(rootfs_data)",
    ]
    base_proc, base_serial_bytes, base_boot_sec = run_serial_boot_until_login(baseline_cmd)
    base_proc.kill()
    base_proc.wait()
    base_serial = base_serial_bytes.decode("utf-8", errors="replace")
    base_gpio_banks = re.findall(r"pl061_gpio ([0-9a-f]+\.gpio_chip): PL061 GPIO chip", base_serial)

    # 2. Build patched DTB uImage + ephemeral key-authenticated SSH SquashFS + 32 MiB NOR
    uimage_patched = boot_dir / "uImage-plaf203-dtb.gk7205v200"
    rootfs_patched = boot_dir / "rootfs-plaf203-ssh-test.squashfs.gk7205v200"
    nor32_path = boot_dir / "nor-plaf203-32m.bin"
    unimp_log_path = boot_dir / "qemu-gk7205v200-unimp.log"
    qual_serial_path = boot_dir / "qemu-gk7205v200-ssh-qual.log"

    dtb_info = build_patched_uimage(uimage_orig, uimage_patched)

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        auth_key = tmp / "id_ed25519_auth"
        unauth_key = tmp / "id_ed25519_unauth"
        subprocess.check_call(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(auth_key),
                "-C",
                "plaf203-qemu-test-auth",
            ],
            stdout=subprocess.DEVNULL,
        )
        subprocess.check_call(
            [
                "ssh-keygen",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(unauth_key),
                "-C",
                "plaf203-qemu-test-unauth",
            ],
            stdout=subprocess.DEVNULL,
        )
        auth_pub = auth_key.with_suffix(".pub").read_text(encoding="utf-8").strip()

        sq_dir = tmp / "sq"
        subprocess.check_call(
            ["unsquashfs", "-d", str(sq_dir), str(rootfs_orig)],
            stdout=subprocess.DEVNULL,
        )
        os_release_text = (sq_dir / "usr/lib/os-release").read_text(encoding="utf-8")

        passwd_path = sq_dir / "etc/passwd"
        passwd_path.write_text(
            rewrite_passwd_root_shell(passwd_path.read_text(encoding="utf-8"), "/bin/ash"),
            encoding="utf-8",
        )

        dropbear_dir = sq_dir / "etc/dropbear"
        dropbear_dir.mkdir(parents=True, exist_ok=True)
        dropbear_dir.chmod(0o700)
        ak = dropbear_dir / "authorized_keys"
        ak.write_text(auth_pub + "\n", encoding="utf-8")
        ak.chmod(0o600)
        seed_credit = dropbear_dir / "seed.credit"
        seed_credit.write_bytes(os.urandom(32))
        seed_credit.chmod(0o400)
        (sq_dir / "root").chmod(0o700)

        default_dir = sq_dir / "etc/default"
        default_dir.mkdir(parents=True, exist_ok=True)
        (default_dir / "seedrng").write_text(
            "mkdir -p /tmp/seedrng\n"
            "chmod 0700 /tmp/seedrng\n"
            "if [ ! -f /tmp/seedrng/seed.credit ] && [ -f /etc/dropbear/seed.credit ]; then\n"
            "\tcp /etc/dropbear/seed.credit /tmp/seedrng/seed.credit\n"
            "\tchmod 0400 /tmp/seedrng/seed.credit\n"
            "fi\n"
            'SEEDRNG_ARGS="-d /tmp/seedrng"\n',
            encoding="utf-8",
        )

        s50_path = sq_dir / "etc/init.d/S50dropbear"
        s50_path.write_text(
            harden_dropbear_init_script(s50_path.read_text(encoding="utf-8")),
            encoding="utf-8",
        )

        for unneeded in ("S49ntpd", "S95majestic"):
            init_file = sq_dir / "etc/init.d" / unneeded
            if init_file.exists():
                init_file.unlink()

        if rootfs_patched.exists():
            rootfs_patched.unlink()
        subprocess.check_call(
            ["mksquashfs", str(sq_dir), str(rootfs_patched), "-comp", "xz", "-noappend"],
            stdout=subprocess.DEVNULL,
        )
        rem = rootfs_patched.stat().st_size % 4096
        if rem:
            with rootfs_patched.open("ab") as handle:
                handle.write(b"\x00" * (4096 - rem))

        # Sparse zero-filled storage is not erased NOR.
        nor32_path.write_bytes(b"\xff" * (32 * 1024 * 1024))

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        ssh_port = sock.getsockname()[1]
        sock.close()

        qual_cmd = [
            str(qemu_bin),
            "-M",
            "gk7205v200,sensor=none",
            "-global",
            "driver=hisi-fmc,property=flash-jedec,value=0xc22019",
            "-global",
            f"driver=hisi-fmc,property=flash-file,value={nor32_path}",
            "-kernel",
            str(uimage_patched),
            "-initrd",
            str(rootfs_patched),
            "-nographic",
            "-serial",
            "stdio",
            "-monitor",
            "none",
            "-nic",
            f"user,hostfwd=tcp:127.0.0.1:{ssh_port}-:22",
            "-d",
            "unimp,guest_errors",
            "-D",
            str(unimp_log_path),
            "-append",
            "console=ttyAMA0,115200 earlyprintk panic=20 root=/dev/ram0 rootfstype=squashfs "
            "mem=64M osmem=45M mmz_allocator=gk "
            "mtdparts=sfc:512K(boot),512K(bootargs),4M(kernel),27M(rootfs)",
        ]
        proc, qual_serial_bytes, qual_boot_sec = run_serial_boot_until_login(qual_cmd)
        qual_serial_path.write_bytes(qual_serial_bytes)
        qual_serial = qual_serial_bytes.decode("utf-8", errors="replace")
        if "login:" not in qual_serial:
            proc.kill()
            proc.wait()
            raise RuntimeError("Patched qualification guest did not reach login prompt")

        ssh_base = [
            "ssh",
            "-F", "/dev/null",
            "-o", "IdentityAgent=none",
            "-p",
            str(ssh_port),
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "ConnectTimeout=10",
        ]

        unauth_res = subprocess.run(
            ssh_base
            + [
                "-i",
                str(unauth_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "root@127.0.0.1",
                "true",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        nopass_res = subprocess.run(
            ssh_base
            + [
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "BatchMode=yes",
                "root@127.0.0.1",
                "true",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )

        guest_probe = """set -e
uname -a
echo "---IPCINFO---"
ipcinfo -v
ipcinfo -c
echo "---GOKE_MEM---"
for f in dram osmem allocator mmz; do
  echo "$f=$(cat /sys/module/goke_mem/parameters/$f)"
done
echo "---MEMINFO---"
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo "---MTD---"
cat /proc/mtd
echo "---NET---"
ip -4 -o addr show dev eth0
ip route
echo "---GPIOCHIPS---"
for c in /sys/class/gpio/gpiochip*; do
  echo "$(basename "$c"):base=$(cat "$c/base"):ngpio=$(cat "$c/ngpio"):label=$(cat "$c/label")"
done
echo "---GPIO3_PINS---"
for pin in 25 26; do
  echo "$pin" > /sys/class/gpio/export
  echo in > "/sys/class/gpio/gpio${pin}/direction"
  echo "gpio${pin}:dir=$(cat "/sys/class/gpio/gpio${pin}/direction"):val=$(cat "/sys/class/gpio/gpio${pin}/value")"
  echo "$pin" > /sys/class/gpio/unexport
done
echo "---RTC---"
cat /sys/class/rtc/rtc0/name /sys/class/rtc/rtc0/date /sys/class/rtc/rtc0/time
"""
        auth_res = subprocess.run(
            ssh_base
            + [
                "-i",
                str(auth_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "root@127.0.0.1",
                guest_probe,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=45,
        )

        u0 = float(
            subprocess.check_output(
                ssh_base
                + [
                    "-i",
                    str(auth_key),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "root@127.0.0.1",
                    'cut -d" " -f1 /proc/uptime',
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
            ).strip()
        )
        h0 = time.monotonic()
        time.sleep(5.0)
        u1 = float(
            subprocess.check_output(
                ssh_base
                + [
                    "-i",
                    str(auth_key),
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "BatchMode=yes",
                    "root@127.0.0.1",
                    'cut -d" " -f1 /proc/uptime',
                ],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=30,
            ).strip()
        )
        h1 = time.monotonic()
        clock_ratio = (u1 - u0) / (h1 - h0)

        subprocess.Popen(
            ssh_base
            + [
                "-i",
                str(auth_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "root@127.0.0.1",
                "reboot -f",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        reb_buf = bytearray()
        reb_deadline = time.monotonic() + 15.0
        reboot_seen = False
        assert proc.stdout is not None
        while time.monotonic() < reb_deadline:
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if ready:
                chunk = os.read(proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                reb_buf.extend(chunk)
                if (
                    b"Restarting system" in reb_buf
                    and b"Booting Linux on physical CPU 0x0" in reb_buf
                ):
                    reboot_seen = True
                    break

        proc.kill()
        proc.wait()

    owner_uid = os.environ.get("OWNER_UID")
    owner_gid = os.environ.get("OWNER_GID")
    if owner_uid is not None and owner_gid is not None:
        uid, gid = int(owner_uid), int(owner_gid)
        for path in (
            uimage_patched,
            rootfs_patched,
            nor32_path,
            unimp_log_path,
            qual_serial_path,
        ):
            if path.exists():
                os.chown(path, uid, gid)

    qual_gpio_banks = re.findall(r"pl061_gpio ([0-9a-f]+\.gpio_chip): PL061 GPIO chip", qual_serial)
    unimp_lines = (
        parse_unimp_addresses(unimp_log_path.read_text(encoding="utf-8", errors="replace"))
        if unimp_log_path.exists()
        else []
    )

    return {
        "qemu_version": version_out.splitlines()[0],
        "registered_machines": gk_machines,
        "rootfs_orig_bytes": rootfs_orig.stat().st_size,
        "rootfs_orig_sha256": sha256_file(rootfs_orig),
        "os_release": os_release_text.strip().splitlines(),
        "dtb_patch": dtb_info,
        "baseline_boot": {
            "boot_seconds": round(base_boot_sec, 2),
            "reached_login": "openipc-gk7205v200 login:" in base_serial,
            "cma_warn_count": base_serial.count("WARNING: CPU: 0 PID: 1 at mm/cma.c"),
            "gpio_banks_registered": base_gpio_banks,
            "gpio8_probe_failed": "amba_device_add() failed (-19) for /soc/gpio_chip@120b8000"
            in base_serial,
            "nor_chip_detected": "w25q64fv(spi)/w25q64jv_iq (Chipsize 8 Mbytes, Blocksize 64KiB)"
            in base_serial,
        },
        "qualified_plaf203_boot": {
            "boot_seconds": round(qual_boot_sec, 2),
            "reached_login": "openipc-gk7205v200 login:" in qual_serial,
            "cma_warn_count": qual_serial.count("WARNING: CPU: 0 PID: 1 at mm/cma.c"),
            "gpio_banks_registered": qual_gpio_banks,
            "gpio8_probe_failed": "amba_device_add() failed (-19) for /soc/gpio_chip@120b8000"
            in qual_serial,
            "nor_chip_line": next(
                (line for line in qual_serial.splitlines() if "Chipsize" in line), ""
            ),
            "unauthorized_key_exit_code": unauth_res.returncode,
            "unauthorized_ssh_stderr": unauth_res.stderr,
            "password_auth_exit_code": nopass_res.returncode,
            "password_ssh_stderr": nopass_res.stderr,
            "authorized_ssh_exit_code": auth_res.returncode,
            "authorized_ssh_output": auth_res.stdout.strip().splitlines(),
            "guest_clock_ratio_5s": round(clock_ratio, 4),
            "hardware_reboot_verified": reboot_seen,
            "qemu_unimp_and_guest_errors": unimp_lines,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=DEFAULT_CACHE_ROOT,
        help="Path to /srv/cache/plaf203-bringup-research",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Optional path to write JSON qualification summary",
    )
    parser.add_argument("--minimal-rootfs", type=Path, help="Source-built SquashFS to boot from NOR")
    parser.add_argument("--test-key", type=Path, help="Disposable private key matching development enrollment")
    parser.add_argument("--expect-ssh-disabled", choices=("no-enrollment", "invalid-enrollment"))
    parser.add_argument("--enrollment-negative-tests", action="store_true")
    parser.add_argument("--bootloader", type=Path, help="Verified OEM mtd0 capture; enables actual NOR/U-Boot boot")
    parser.add_argument("--kernel-image", type=Path, help="Source-built uImage beside its zImage, DTB and build-receipt.json")
    parser.add_argument("--runtime-checks", action="store_true", help="Require and fault-test the native ARM runtime")
    parser.add_argument("--feeding-checks", action="store_true", help="Require durable feeding/journal lifecycle qualification")
    args = parser.parse_args()
    runner_sha256 = sha256_file(Path(__file__))
    if args.feeding_checks and not args.runtime_checks:
        parser.error("--feeding-checks requires --runtime-checks")

    if args.minimal_rootfs:
        if not args.test_key:
            parser.error("--minimal-rootfs requires --test-key")
        summary = run_minimal_flash_qualification(args.cache_root, args.minimal_rootfs, args.test_key,
                                                  args.expect_ssh_disabled, args.bootloader, args.kernel_image, args.runtime_checks, args.feeding_checks)
        if args.enrollment_negative_tests:
            summary["enrollment_negative_tests"] = run_enrollment_negatives(
                args.cache_root, args.minimal_rootfs, args.test_key, args.bootloader, args.kernel_image, args.runtime_checks)
    else:
        if args.expect_ssh_disabled or args.enrollment_negative_tests or args.bootloader or args.test_key or args.kernel_image or args.runtime_checks or args.feeding_checks:
            parser.error("Flash-profile options require --minimal-rootfs")
        summary = run_qualification(args.cache_root)
    summary["qualifier_sha256"] = runner_sha256
    rendered = json.dumps(summary, indent=2)
    if args.output_json is not None:
        args.output_json.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    failures = [] if args.minimal_rootfs else qualification_failures(summary)
    if failures:
        raise SystemExit("Qualification failed: " + ", ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
