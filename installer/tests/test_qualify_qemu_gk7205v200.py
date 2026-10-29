from __future__ import annotations

import importlib.util
import unittest
from copy import deepcopy
import os
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
from pathlib import Path


def _load_qualify_module():
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "qualify_qemu_gk7205v200.py"
    spec = importlib.util.spec_from_file_location("qualify_qemu_gk7205v200", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class QualifyQemuGk7205v200Tests(unittest.TestCase):
    def test_enrollment_fixtures_bound_compression_and_preserve_root_ownership(self) -> None:
        mod = _load_qualify_module()
        def command(args, **kwargs):
            if args[0] == "unsquashfs":
                (Path(args[2]) / "etc/plaf203").mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(mod.subprocess, "run", side_effect=command) as run, \
                patch.object(mod, "run_minimal_flash_qualification", return_value={}) as boot:
            mod.run_enrollment_negatives(Path(directory), Path("rootfs"), Path("key"))
            commands = [c.args[0] for c in run.call_args_list if c.args[0][0] == "mksquashfs"]
            self.assertEqual(len(commands), 2)
            for args in commands:
                self.assertEqual(args[args.index("-processors") + 1], "2")
                self.assertIn("-all-root", args)
            self.assertEqual([c.args[3] for c in boot.call_args_list], ["no-enrollment", "invalid-enrollment"])

    def test_insecure_test_private_key_rejected_before_guest_start(self) -> None:
        mod = _load_qualify_module()
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "test-key"
            key.write_text("disposable test fixture")
            key.chmod(0o644)
            with patch.object(mod, "build_patched_uimage") as build, \
                    self.assertRaisesRegex(ValueError, "Test private key"):
                mod.run_minimal_flash_qualification(Path(directory), Path("unused"), key)
            build.assert_not_called()

    def test_serial_marker_can_span_reads(self) -> None:
        mod = _load_qualify_module()
        with subprocess.Popen([sys.executable, "-c", "import sys,time; "
                               "sys.stdout.write('disabled: '); sys.stdout.flush(); "
                               "time.sleep(0.03); print('reason',flush=True)"],
                              stdout=subprocess.PIPE) as proc:
            data = mod.read_serial_until(proc, b"disabled: reason", 2)
        self.assertIn(b"disabled: reason", data)

    def test_bootloader_option_cannot_silently_run_ram_profile(self) -> None:
        mod = _load_qualify_module()
        with patch.object(sys, "argv", ["qualify", "--bootloader", "not-used"]), \
                patch.object(mod, "run_qualification") as run, \
                patch("sys.stderr"):
            with self.assertRaises(SystemExit) as result:
                mod.main()
        self.assertEqual(result.exception.code, 2)
        run.assert_not_called()

    def test_post_build_removes_stale_enrollment_and_stock_seed_service(self) -> None:
        profile = Path(__file__).resolve().parents[2] / "firmware/plaf203"
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            shutil.copytree(profile / "overlay", target)
            (target / "etc/plaf203").mkdir()
            (target / "etc/inittab").write_text("::sysinit:/etc/init.d/rcS\n")
            for name in ("enrollment.pub", "test.seed"):
                (target / "etc/plaf203" / name).write_text("stale")
            for name in ("S01seedrng", "S50dropbear"):
                (target / "etc/init.d" / name).write_text("old policy")
            subprocess.run(["sh", str(profile / "post-build.sh"), str(target)], check=True,
                           env={"PATH": os.environ["PATH"], "BR2_EXTERNAL_PLAF203_PATH": str(profile)})
            self.assertEqual(list((target / "etc/plaf203").iterdir()), [])
            self.assertFalse((target / "etc/init.d/S01seedrng").exists())
            self.assertFalse((target / "etc/init.d/S50dropbear").exists())
            self.assertTrue((target / "etc/init.d/S50sshd").exists())
            self.assertEqual((target / "etc/inittab").read_text().count(
                "::respawn:/usr/libexec/plaf203-runtime-supervise"), 1)

    def test_failed_guest_assertions_fail_qualification(self) -> None:
        mod = _load_qualify_module()
        report = {"registered_machines": ["gk7205v200 Goke"],
                  "baseline_boot": {"reached_login": True}, "qualified_plaf203_boot": {
                      "reached_login": True, "gpio_banks_registered": [f"120b{i}000.gpio_chip" for i in range(8)],
                      "gpio8_probe_failed": False, "nor_chip_line": "Chipsize 32 Mbytes",
                      "cma_warn_count": 0, "authorized_ssh_exit_code": 0,
                      "unauthorized_key_exit_code": 255, "password_auth_exit_code": 255,
                      "unauthorized_ssh_stderr": "Permission denied (publickey).",
                      "password_ssh_stderr": "Permission denied (publickey).",
                      "guest_clock_ratio_5s": 1.0, "hardware_reboot_verified": True}}
        self.assertEqual(mod.qualification_failures(report), [])
        for field, value in (("hardware_reboot_verified", False), ("guest_clock_ratio_5s", 3.0),
                             ("unauthorized_ssh_stderr", "Connection refused"),
                             ("password_ssh_stderr", "Permission denied (publickey,password)."),
                             ("gpio_banks_registered", []), ("authorized_ssh_exit_code", 255)):
            broken = deepcopy(report)
            broken["qualified_plaf203_boot"][field] = value
            with self.subTest(field=field):
                self.assertTrue(mod.qualification_failures(broken))

    def test_patch_gk7205v200_dts_adds_gpio3_and_disables_gpio8(self) -> None:
        mod = _load_qualify_module()
        sample_dts = (
            "/dts-v1/;\n"
            "/ {\n"
            "\taliases {\n"
            '\t\tgpio2 = "/soc/gpio_chip@120b2000";\n'
            '\t\tgpio4 = "/soc/gpio_chip@120b4000";\n'
            '\t\tgpio8 = "/soc/gpio_chip@120b8000";\n'
            "\t};\n"
            "\tsoc {\n"
            "\t\tgpio_chip@120b2000 {\n"
            '\t\t\tcompatible = "arm,pl061\\0arm,primecell";\n'
            "\t\t\treg = <0x120b2000 0x1000>;\n"
            '\t\t\tstatus = "okay";\n'
            "\t\t};\n\n"
            "\t\tgpio_chip@120b4000 {\n"
            '\t\t\tcompatible = "arm,pl061\\0arm,primecell";\n'
            "\t\t\treg = <0x120b4000 0x1000>;\n"
            '\t\t\tstatus = "okay";\n'
            "\t\t};\n\n"
            "\t\tgpio_chip@120b8000 {\n"
            '\t\t\tcompatible = "arm,pl061\\0arm,primecell";\n'
            "\t\t\treg = <0x120b8000 0x1000>;\n"
            '\t\t\tstatus = "okay";\n'
            "\t\t};\n"
            "\t};\n"
            "};\n"
        )
        patched = mod.patch_gk7205v200_dts_for_plaf203(sample_dts)
        self.assertNotIn("\\0", patched)
        self.assertIn('gpio3 = "/soc/gpio_chip@120b3000";', patched)
        self.assertIn("gpio_chip@120b3000 {", patched)
        self.assertIn("reg = <0x120b3000 0x1000>;", patched)
        self.assertIn("interrupts = <0x00 0x13 0x04>;", patched)
        self.assertRegex(
            patched,
            r'gpio_chip@120b8000\s*\{[^}]*status = "disabled";',
        )
        # Idempotence check
        self.assertEqual(patched, mod.patch_gk7205v200_dts_for_plaf203(patched))

    def test_harden_dropbear_replaces_blank_password_flag_with_key_only(self) -> None:
        mod = _load_qualify_module()
        stock = (
            "#!/bin/sh\n"
            'DAEMON_ARGS="-R -B -k -p 22 -K 300"\n'
            "start() {\n"
            "\tumask 077\n"
            "}\n"
        )
        hardened = mod.harden_dropbear_init_script(stock)
        self.assertIn(
            'DAEMON_ARGS="-r /tmp/dropbear_ed25519_host_key -s -g -k -p 22 -K 300"',
            hardened,
        )
        self.assertIn(
            "dropbearkey -t ed25519 -f /tmp/dropbear_ed25519_host_key",
            hardened,
        )
        self.assertNotIn("-B", hardened)
        self.assertEqual(hardened, mod.harden_dropbear_init_script(hardened))

    def test_rewrite_passwd_root_shell_targets_only_root_entry(self) -> None:
        mod = _load_qualify_module()
        passwd = (
            "root:x:0:0:root:/root:/usr/sbin/openipc-claim\n"
            "daemon:x:1:1:daemon:/usr/sbin:/bin/false\n"
        )
        rewritten = mod.rewrite_passwd_root_shell(passwd, "/bin/ash")
        self.assertEqual(
            rewritten,
            "root:x:0:0:root:/root:/bin/ash\n"
            "daemon:x:1:1:daemon:/usr/sbin:/bin/false\n",
        )

    def test_parse_unimp_addresses_deduplicates_and_sorts(self) -> None:
        mod = _load_qualify_module()
        raw = "  unimp: b\nunimp: a\nunimp: b  \n\n"
        self.assertEqual(mod.parse_unimp_addresses(raw), ["unimp: a", "unimp: b"])


if __name__ == "__main__":
    unittest.main()
