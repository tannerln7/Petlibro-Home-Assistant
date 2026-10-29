from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import sys
import subprocess
import tempfile
import json
import socket
import threading
import select
import struct
import zlib
import unittest
from unittest.mock import patch


def load_module():
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    spec = importlib.util.spec_from_file_location("run_plaf203_oem_qemu", scripts / "run_plaf203_oem_qemu.py")
    module = importlib.util.module_from_spec(spec)
    # The profile deliberately reuses the existing qualification's helpers.
    with patch.object(sys, "path", [str(scripts), *sys.path]):
        spec.loader.exec_module(module)
    return module


class OemReferenceTests(unittest.TestCase):
    def test_copy_is_exclusive_independent_and_verified(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            dest = Path(directory) / "flash"
            source.write_bytes(b"synthetic evidence")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            with patch.object(m, "OEM_BYTES", source.stat().st_size), patch.object(m, "OEM_SHA256", digest):
                m.copy_flash(source, dest)
                self.assertEqual(dest.read_bytes(), source.read_bytes())
                self.assertNotEqual(dest.stat().st_ino, source.stat().st_ino)
                self.assertEqual(dest.stat().st_mode & 0o777, 0o600)
                with self.assertRaises(FileExistsError):
                    m.copy_flash(source, dest)
                source.write_bytes(b"tampered evidence!")
                with self.assertRaises(ValueError):
                    m.copy_flash(source, Path(directory) / "new")
                self.assertFalse((Path(directory) / "new").exists())

    def test_real_identity_is_required(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "input"
            p.write_bytes(b"not the OEM image")
            with self.assertRaises(ValueError):
                m.verify_image(p)

    def test_launch_uses_nor_and_never_network_or_direct_kernel(self):
        m = load_module()
        cmd = m.command(Path("qemu"), Path("flash.bin"), Path("private"))
        self.assertIn("driver=hisi-fmc,property=flash-jedec,value=0xc22019", cmd)
        self.assertIn("driver=hisi-fmc,property=flash-file,value=flash.bin", cmd)
        self.assertEqual(cmd[cmd.index("-nic") + 1], "none")
        self.assertIn("-S", cmd)  # Isolation checked while CPU is stopped.
        for option in ("-kernel", "-initrd", "-append", "-netdev", "-device"):
            self.assertNotIn(option, cmd)
        self.assertFalse(any("hostfwd" in value or "gk7205v200,flash-file" in value for value in cmd))

    def test_network_inspection_fails_closed(self):
        m = load_module()
        nic = "hisi-femac.0: index=0,type=nic,model=hisi-femac,macaddr=00:00:00:00:00:00\n"
        m.verify_network(nic)
        m.verify_network("")
        for value in (nic.rstrip() + ",peer=net0", "net0: type=user", "net0: type=tap", "unexpected format"):
            with self.assertRaises(RuntimeError):
                m.verify_network(value)

    def test_probe_requires_execution_not_echo_and_correct_identity(self):
        m = load_module()
        nonce = "abcd1234"
        self.assertFalse(m.probe_result(m.probe_command(nonce), nonce)["completed"])
        response = f"\r\nPLAF203_REF_{nonce}|/bin/busybox|jffs2|yes|da353ba37a7d8c1c0790827e5ec267a180519affb5666ae4efd93e5362e00b71|END\r\n".encode()
        result = m.probe_result(response, nonce)
        self.assertTrue(all(result.values()))
        self.assertFalse(m.probe_result(response, "different")["completed"])
        bad = response.replace(b"da353ba3", b"00000000")
        self.assertFalse(m.probe_result(bad, nonce)["application_original_sha256"])

    def test_kernel_banner_does_not_establish_root_or_init(self):
        m = load_module()
        result = m.milestones(b"Linux version 4.9.37\n")
        self.assertTrue(result["oem_linux_banner"])
        self.assertFalse(result["jffs2_root_mounted"])
        self.assertFalse(result["init_started"])
        self.assertFalse(m.milestones(b"VFS: Mounted root (jffs2 filesystem)\n")["jffs2_root_mounted"])

    def test_shared_cleanup_terminates_and_reaps_guest(self):
        m = load_module()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            m.stop_guest(child)
            self.assertIsNotNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_diagnostic_environment_changes_only_bootargs_and_crc(self):
        m = load_module()
        entries = [b"bootcmd=sf read original", b"bootargs=root=/dev/mtdblock3", b"other=unchanged"]
        payload = (b"\0".join(entries) + b"\0\0").ljust(0x40000 - 4, b"\0")
        image = bytearray(b"\xff" * (1 << 20))
        image[0x80000:0xc0000] = struct.pack("<I", zlib.crc32(payload)) + payload
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "disposable"
            p.write_bytes(image)
            m.diagnostic_environment(p)
            result = p.read_bytes()
        self.assertEqual(result[:0x80000], image[:0x80000])
        self.assertEqual(result[0xc0000:], image[0xc0000:])
        env = result[0x80000:0xc0000]
        self.assertEqual(struct.unpack_from("<I", env)[0], zlib.crc32(env[4:]))
        actual = env[4:].split(b"\0\0", 1)[0].split(b"\0")
        self.assertEqual(actual, [entries[0], entries[1] + b" init=/bin/sh", entries[2]])

    def test_startup_sed_preserves_other_lines_and_literal_background(self):
        m = load_module()
        original = "#!/bin/sh\necho preserve\n/user/ota2/AF203_FW &\necho also-preserve\n"
        for profile in ("app-disabled", "app-pty"):
            text = m.startup_command(profile).decode()
            # Exercise the actual shell/sed quoting without mounts or firmware.
            sed = text.split("sed ", 1)[1].split(" /mnt/app_start.original", 1)[0]
            result = subprocess.run(["sh", "-c", f"sed {sed}"], input=original, text=True, capture_output=True, check=True).stdout
            self.assertTrue(result.startswith("#!/bin/sh\necho preserve\n"))
            self.assertTrue(result.endswith("echo also-preserve\n"))
            if profile == "app-pty":
                self.assertIn("script -qf -c 'exec /user/ota2/AF203_FW </dev/null'", result)
                self.assertEqual(result.count("/user/ota2/AF203_FW"), 1)
                self.assertIn("2>&1 &", result)
            else:
                self.assertNotIn("/user/ota2/AF203_FW", result)

    def test_qmp_services_uart_while_waiting_for_response(self):
        m = load_module()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "qmp"
            listener = socket.socket(socket.AF_UNIX)
            listener.bind(str(path)); listener.listen(1)
            incoming, outgoing = socket.socketpair()
            incoming.setblocking(False)
            outgoing.settimeout(5)
            errors = []
            def server():
                try:
                    with listener.accept()[0] as connection:
                        stream = connection.makefile("rwb")
                        stream.write(b'{"QMP":{}}\n'); stream.flush()
                        for index in range(2):
                            request = json.loads(stream.readline())
                            if index == 1:
                                # More than a socket buffer: a blocking client
                                # that ignores UART cannot receive the QMP reply.
                                outgoing.sendall(b"x" * (1 << 20))
                            stream.write(json.dumps({"return": {"running": True}, "id": request["id"]}).encode() + b"\n")
                            stream.flush()
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=server, daemon=True); thread.start()
            qmp = m.Qmp(path)
            received = bytearray()
            def drain():
                while select.select([incoming], [], [], 0)[0]:
                    received.extend(incoming.recv(65536))
            qmp.service_io = drain
            try:
                self.assertTrue(qmp.execute("query-status")["running"])
                drain()
                self.assertEqual(len(received), 1 << 20)
            finally:
                qmp.close(); outgoing.close(); incoming.close(); listener.close()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_interactive_rejects_automatic_terminal_writers(self):
        m = load_module()
        for option in ("--probe", "--storage-check"):
            with patch.object(sys, "argv", ["runner", "--reconstruction-root", "/unused", "--work-root", "/unused", "--interactive", option]), \
                    patch.object(m.os, "isatty", return_value=True), patch.object(m, "run") as run, patch("sys.stderr"):
                with self.assertRaises(SystemExit) as result:
                    m.main()
                self.assertEqual(result.exception.code, 2)
                run.assert_not_called()

    def test_storage_paths_are_bounded_and_injection_is_rejected(self):
        m = load_module()
        for token in ("../", "x;reboot", "a" * 33, ""):
            with self.assertRaises(ValueError):
                m.storage_command(token)
        command = m.storage_command("a" * 32).decode()
        self.assertIn("/user/data/.plaf203-qemu-" + "a" * 32, command)
        self.assertNotIn("/dev/mtd", command)
        self.assertIn("sha256sum", command)
        self.assertIn("drop_caches", command)
        self.assertIn('"$i" -lt 24', command)
        self.assertIn("PLAF203_RESTART_", m.storage_command("a" * 32, restart=True).decode())

    def test_erase_evidence_requires_zero_to_one_transition_in_rootfs(self):
        m = load_module()
        size = 0x520000
        before = bytearray(b"\xff" * size)
        before[0x500100] = 0
        after = bytearray(before)
        after[0x500100] = 0xff
        after[0x510100] = 0  # Ordinary programming, not evidence of erase.
        after[0x100] = 0
        with patch.object(m, "OEM_BYTES", size):
            self.assertEqual(m.erased_sectors(before, after), [0x500000])

    def test_model_lock_matches_patch_and_runner_binary_pin(self):
        m = load_module()
        directory = Path(__file__).resolve().parents[2] / "firmware/plaf203/qemu"
        lock = json.loads((directory / "source-lock.json").read_text())
        patch_bytes = (directory / lock["patch"]).read_bytes()
        self.assertEqual(hashlib.sha256(patch_bytes).hexdigest(), lock["patch_sha256"])
        self.assertEqual(m.QEMU_SHA256, lock["qualified_binary_sha256"])
        self.assertEqual(patch_bytes.count(b"+    case SPI_CMD_SECTOR_ERASE4:"), 1)

    def test_storage_qualification_fails_closed(self):
        m = load_module()
        report = {"storage": {"integrity_pass": True, "cycles": 24,
                              "rootfs_sectors_with_erase_evidence": [0x600000], "persisted_after_cold_restart": True},
                  "diagnostics": {"jffs2_gc_errors": 0, "unsupported_erase4": 0},
                  "restart": {"storage_restart": {"deleted_file_still_absent": True},
                              "diagnostics": {"jffs2_gc_errors": 0, "unsupported_erase4": 0}}}
        self.assertEqual(m.storage_failures(report), [])
        report["restart"]["diagnostics"]["jffs2_gc_errors"] = 1
        self.assertIn("no storage diagnostics", m.storage_failures(report))
        self.assertTrue(m.storage_failures({}))


if __name__ == "__main__":
    unittest.main()
