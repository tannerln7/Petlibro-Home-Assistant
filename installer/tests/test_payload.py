from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from installer.payload import build_payload, validate_public_key


VALID_PUBLIC_KEY = (
    "ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIAABAgMEBQYHCAkKCwwNDg8QERITFBUWFxgZGhscHR4f "
    "bootstrap@example"
)


class PayloadTests(unittest.TestCase):
    @staticmethod
    def _snapshots(data: Path) -> list[Path]:
        root = data / "plaf203-bootstrap" / "preinstall-state"
        return sorted(path for path in root.iterdir() if path.is_dir())

    def _state_agent_tree(self, root: Path) -> Path:
        agent = root / "agent"
        (agent / "runit" / "plaf203-state-agent").mkdir(parents=True)
        (agent / "runit" / "plaf203-update-supervisor").mkdir(parents=True)
        (agent / "plaf203-state-agent").write_bytes(b"agent-binary")
        (agent / "plaf203-update-fs").write_bytes(b"helper-binary")
        (agent / "app_start_snippet.sh").write_text("#!/bin/sh\n", encoding="ascii")
        (agent / "runit" / "plaf203-state-agent" / "run").write_text(
            "#!/bin/sh\nexec agent --allow-ip 192.0.2.10\n", encoding="ascii"
        )
        (agent / "runit" / "plaf203-update-supervisor" / "run").write_text(
            "#!/bin/sh\nexit 0\n", encoding="ascii"
        )
        (agent / "runit" / "plaf203-update-supervisor" / "supervisor.sh").write_text(
            "#!/bin/sh\nexit 0\n", encoding="ascii"
        )
        return agent

    def _dropbear_tree(self, root: Path) -> Path:
        dropbear = root / "dropbear-build"
        dropbear.mkdir()
        elf32_arm = bytearray(52)
        elf32_arm[:7] = b"\x7fELF\x01\x01\x01"
        elf32_arm[18:20] = (40).to_bytes(2, "little")
        elf32_arm[36:40] = (0x400).to_bytes(4, "little")
        (dropbear / "dropbearmulti").write_bytes(elf32_arm)
        (dropbear / "LICENSE.dropbear").write_text("Dropbear license\n")
        (dropbear / "BUILD_INFO.dropbear.json").write_text("{}\n")
        return dropbear

    def _run_installer(
        self, execution_slot: str, *, from_slot: bool = False
    ) -> tuple[Path, bytes, tempfile.TemporaryDirectory[str]]:
        raw = tempfile.TemporaryDirectory()
        temp = Path(raw.name)
        agent = self._state_agent_tree(temp)
        payload = temp / "payload"
        build_payload(
            output=payload,
            state_agent_dir=agent,
            home_assistant_ip="192.0.2.25",
            state_agent_token="a" * 64,
            ssh_public_key=VALID_PUBLIC_KEY,
            dropbear_dir=self._dropbear_tree(temp),
        )
        feeder = temp / "feeder"
        (feeder / "ota1").mkdir(parents=True)
        (feeder / "ota2").mkdir()
        (feeder / "data" / "ota").mkdir(parents=True)
        (feeder / "data" / "attr").mkdir()
        (feeder / "data" / "attr" / "state.bin").write_bytes(b"S" * 236)
        (feeder / "data" / "feed_plan").mkdir()
        (feeder / "data" / "feed_plan" / "index.bin").write_bytes(b"\x01")
        (feeder / "data" / "feed_plan" / "plan.bin").write_bytes(b"P" * 47)
        donor = b"\x7fELF" + b"stock-oem" * 100
        donor_slot = "ota1" if execution_slot == "ota2" else "ota2"
        (feeder / donor_slot / "AF203_FW").write_bytes(donor)
        (feeder / donor_slot / "AF203_FW").chmod(0o755)
        (feeder / "app_start.sh").write_text(
            f"/user/{execution_slot}/AF203_FW &\n", encoding="ascii"
        )
        executable = payload
        if from_slot:
            executable = feeder / execution_slot / "AF203_FW"
            executable.write_bytes(payload.read_bytes())
            executable.chmod(0o755)
        env = os.environ | {
            "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
            "PLAF203_BOOTSTRAP_TEST_SLOT": execution_slot,
        }
        subprocess.run(["/bin/sh", str(executable)], env=env, check=True, timeout=15)
        return feeder, donor, raw

    def test_installer_restores_ota2_and_selects_ota1(self) -> None:
        feeder, donor, temporary = self._run_installer("ota2")
        with temporary:
            self._assert_successful_install(feeder, donor)

    def test_installer_restores_ota1_and_selects_ota1(self) -> None:
        feeder, donor, temporary = self._run_installer("ota1")
        with temporary:
            self._assert_successful_install(feeder, donor)

    def test_exec_handoff_restores_a_payload_executing_from_either_slot(self) -> None:
        # Exercise the real OTA file layout: the executing shell is the same
        # file the restore handoff must unlink before recreating OEM firmware.
        for slot in ("ota1", "ota2"):
            with self.subTest(slot=slot):
                feeder, donor, temporary = self._run_installer(slot, from_slot=True)
                with temporary:
                    self._assert_successful_install(feeder, donor)
                    self.assertFalse(list(feeder.glob("ota*/AF203_FW.restore")))

    def test_failed_restore_preserves_donor_and_existing_maintenance(self) -> None:
        feeder, donor, temporary = self._run_installer("ota2", from_slot=True)
        with temporary:
            payload = Path(temporary.name) / "payload"
            target = feeder / "ota2" / "AF203_FW"
            target.write_bytes(payload.read_bytes())
            target.chmod(0o755)
            mock_bin = Path(temporary.name) / "mock-bin"
            mock_bin.mkdir()
            copy = mock_bin / "cp"
            copy.write_text(
                '#!/bin/sh\n'
                'case "$2" in */ota2/AF203_FW) exit 1;; esac\n'
                'exec /bin/cp "$@"\n',
                encoding="ascii",
            )
            copy.chmod(0o755)
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota2",
                "PATH": str(mock_bin) + os.pathsep + os.environ["PATH"],
            }
            result = subprocess.run(
                ["/bin/sh", str(target)], env=env, timeout=15, capture_output=True
            )
            self.assertEqual(77, result.returncode, result.stderr)
            self.assertEqual(donor, (feeder / "ota1" / "AF203_FW").read_bytes())
            self.assertFalse(target.exists())
            self.assertEqual(
                b"\x00\x00\x00\x00", (feeder / "data" / "ota" / "index.bin").read_bytes()
            )
            self.assertEqual(
                "payload_slot_restore_copy_failed\n",
                (feeder / "data" / "plaf203-bootstrap" / "status").read_text(),
            )
            startup = (feeder / "app_start.sh").read_text()
            self.assertIn(str(feeder / "ota1" / "AF203_FW"), startup)
            self.assertIn("/user/data/enable_state_agent", startup)
            self.assertIn("/user/data/enable_ssh", startup)

    def test_preflight_failure_keeps_existing_maintenance_hooks(self) -> None:
        feeder, donor, temporary = self._run_installer("ota2", from_slot=True)
        with temporary:
            (feeder / "data" / "attr" / "state.bin").unlink()
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota2",
            }
            result = subprocess.run(
                ["/bin/sh", str(Path(temporary.name) / "payload")],
                env=env, capture_output=True, timeout=15
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual(donor, (feeder / "ota1" / "AF203_FW").read_bytes())
            startup = (feeder / "app_start.sh").read_text()
            self.assertIn("/user/data/enable_state_agent", startup)
            self.assertIn("/user/data/enable_ssh", startup)

    def test_installer_always_enables_key_only_dropbear(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2")
        with temporary:
            data = feeder / "data"
            self.assertTrue((data / "enable_ssh").is_file())
            self.assertEqual(
                VALID_PUBLIC_KEY + "\n",
                (data / "dropbear" / "authorized_keys").read_text(),
            )
            app_start = (feeder / "app_start.sh").read_text()
            self.assertIn("-p 2222 -s", app_start)
            self.assertNotIn("pidof dropbear", app_start)
            self.assertIn("/proc/$dropbear_pid/cmdline", app_start)
            self.assertNotIn("telnet", app_start)
            self.assertTrue((data / "dropbear" / "dropbear").is_symlink())
            self.assertEqual(
                "dropbearmulti", os.readlink(data / "dropbear" / "dropbear")
            )
            self.assertEqual(
                "Dropbear license\n",
                (data / "dropbear" / "LICENSE.dropbear").read_text(),
            )
            self.assertEqual(
                "{}\n",
                (data / "dropbear" / "BUILD_INFO.dropbear.json").read_text(),
            )

    def test_bootstrap_retry_captures_fresh_state_and_preserves_first_snapshot(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2")
        with temporary:
            data = feeder / "data"
            (data / "attr" / "state.bin").write_bytes(b"N" * 236)
            (data / "feed_plan" / "index.bin").write_bytes(b"\x00")
            (data / "feed_plan" / "plan.bin").write_bytes(b"")

            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota2",
            }
            subprocess.run(
                ["/bin/sh", str(Path(temporary.name) / "payload")],
                env=env,
                check=True,
                timeout=15,
            )

            first, second = self._snapshots(data)
            self.assertEqual("snapshot-000001", first.name)
            self.assertEqual("snapshot-000002", second.name)
            self.assertTrue((first / "complete").is_file())
            self.assertTrue((second / "complete").is_file())
            self.assertEqual(b"S" * 236, (first / "attr" / "state.bin").read_bytes())
            self.assertEqual(b"\x01", (first / "feed_plan" / "index.bin").read_bytes())
            self.assertEqual(b"P" * 47, (first / "feed_plan" / "plan.bin").read_bytes())
            self.assertEqual(b"N" * 236, (second / "attr" / "state.bin").read_bytes())
            self.assertEqual(b"\x00", (second / "feed_plan" / "index.bin").read_bytes())
            self.assertEqual(b"", (second / "feed_plan" / "plan.bin").read_bytes())

    def test_retry_preserves_incomplete_attempt_and_uses_new_identity(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2")
        with temporary:
            data = feeder / "data"
            snapshot_root = data / "plaf203-bootstrap" / "preinstall-state"
            interrupted = snapshot_root / "snapshot-000002"
            interrupted.mkdir()
            (interrupted / "capture-interrupted").write_text("unchanged\n")
            (data / "attr" / "state.bin").write_bytes(b"N" * 236)
            (data / "feed_plan" / "index.bin").write_bytes(b"\x00")
            (data / "feed_plan" / "plan.bin").write_bytes(b"")

            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota2",
            }
            subprocess.run(
                ["/bin/sh", str(Path(temporary.name) / "payload")],
                env=env,
                check=True,
                timeout=15,
            )

            first, stale, latest = self._snapshots(data)
            self.assertEqual("snapshot-000001", first.name)
            self.assertEqual("snapshot-000002", stale.name)
            self.assertEqual("snapshot-000003", latest.name)
            self.assertEqual("unchanged\n", (stale / "capture-interrupted").read_text())
            self.assertFalse((stale / "complete").exists())
            self.assertTrue((latest / "complete").is_file())
            self.assertEqual(b"N" * 236, (latest / "attr" / "state.bin").read_bytes())

    def test_retry_rejects_invalid_current_state_without_damaging_first_snapshot(self) -> None:
        feeder, _donor, temporary = self._run_installer("ota2")
        with temporary:
            data = feeder / "data"
            (data / "attr" / "state.bin").unlink()
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota2",
            }

            result = subprocess.run(
                ["/bin/sh", str(Path(temporary.name) / "payload")],
                env=env,
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(0, result.returncode)
            first, failed = self._snapshots(data)
            self.assertTrue((first / "complete").is_file())
            self.assertEqual(b"S" * 236, (first / "attr" / "state.bin").read_bytes())
            self.assertFalse((failed / "complete").exists())
            self.assertIn(
                "invalid_preinstall_state_backup",
                (data / "plaf203-bootstrap" / "status").read_text(),
            )

    def _assert_successful_install(self, feeder: Path, donor: bytes) -> None:
        self.assertEqual(donor, (feeder / "ota1" / "AF203_FW").read_bytes())
        self.assertEqual(donor, (feeder / "ota2" / "AF203_FW").read_bytes())
        self.assertEqual(
            b"\x00\x00\x00\x00", (feeder / "data" / "ota" / "index.bin").read_bytes()
        )
        self.assertEqual(
            "a" * 64,
            (feeder / "data" / "local-state-agent" / "token").read_text().strip(),
        )
        run = (
            feeder
            / "data"
            / "local-state-agent"
            / "runit"
            / "plaf203-state-agent"
            / "run"
        ).read_text()
        self.assertIn("--allow-ip 192.0.2.25", run)
        self.assertNotIn("192.0.2.10", run)
        app_start = (feeder / "app_start.sh").read_text()
        self.assertIn("/user/ota1/AF203_FW &", app_start)
        self.assertNotIn("telnet", app_start)
        self.assertNotIn("bootclock", app_start)
        self.assertNotIn("af203-live-log", app_start)
        self.assertEqual(
            "installed_rebooting_to_ota1\n",
            (feeder / "data" / "plaf203-bootstrap" / "status").read_text(),
        )
        snapshots = self._snapshots(feeder / "data")
        self.assertEqual(["snapshot-000001"], [path.name for path in snapshots])
        state_backup = snapshots[0]
        self.assertTrue((state_backup / "complete").is_file())
        self.assertEqual(
            b"S" * 236, (state_backup / "attr" / "state.bin").read_bytes()
        )
        self.assertEqual(
            b"\x01", (state_backup / "feed_plan" / "index.bin").read_bytes()
        )
        self.assertEqual(
            b"P" * 47, (state_backup / "feed_plan" / "plan.bin").read_bytes()
        )

    def test_state_agent_startup_matches_only_its_runsvdir_tree(self) -> None:
        snippet = (
            Path(__file__).resolve().parents[2]
            / "state-agent"
            / "app_start_snippet.sh"
        ).read_text()
        self.assertNotIn("pidof runsvdir", snippet)
        self.assertIn("runsvdir_for_tree_running", snippet)

    def test_installer_rejects_unknown_test_slot_without_touching_startup(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            agent = self._state_agent_tree(temp)
            payload = temp / "payload"
            build_payload(
                output=payload,
                state_agent_dir=agent,
                home_assistant_ip="192.0.2.25",
                state_agent_token="a" * 64,
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(temp),
            )
            feeder = temp / "feeder"
            (feeder / "ota1").mkdir(parents=True)
            (feeder / "ota2").mkdir()
            (feeder / "data" / "ota").mkdir(parents=True)
            donor = b"\x7fELF" + b"stock-oem" * 100
            (feeder / "ota1" / "AF203_FW").write_bytes(donor)
            (feeder / "ota1" / "AF203_FW").chmod(0o755)
            (feeder / "app_start.sh").write_text("/user/ota1/AF203_FW &\n", encoding="ascii")
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "invalid",
            }
            result = subprocess.run(
                ["/bin/sh", str(payload)],
                env=env,
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(70, result.returncode)
            self.assertEqual(
                "/user/ota1/AF203_FW &\n", (feeder / "app_start.sh").read_text()
            )

    def test_failed_install_selects_the_untouched_donor(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            payload = temp / "payload"
            build_payload(
                output=payload,
                state_agent_dir=self._state_agent_tree(temp),
                home_assistant_ip="192.0.2.25",
                state_agent_token="a" * 64,
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(temp),
            )
            feeder = temp / "feeder"
            (feeder / "ota1").mkdir(parents=True)
            (feeder / "ota2").mkdir()
            (feeder / "data" / "ota").mkdir(parents=True)
            (feeder / "data" / "attr").mkdir()
            (feeder / "data" / "attr" / "state.bin").write_bytes(b"S" * 236)
            (feeder / "data" / "feed_plan").mkdir()
            (feeder / "data" / "feed_plan" / "index.bin").write_bytes(b"\x00")
            (feeder / "data" / "feed_plan" / "plan.bin").write_bytes(b"")
            donor = b"\x7fELF" + b"stock-oem" * 100
            (feeder / "ota2" / "AF203_FW").write_bytes(donor)
            (feeder / "ota2" / "AF203_FW").chmod(0o755)
            (feeder / "app_start.sh").write_text(
                "/user/ota1/AF203_FW &\n", encoding="ascii"
            )
            # A non-directory at the service destination forces a failure after
            # the transaction has committed its donor fallback.
            (feeder / "data" / "local-state-agent").write_text("conflict")
            env = os.environ | {
                "PLAF203_BOOTSTRAP_TEST_ROOT": str(feeder),
                "PLAF203_BOOTSTRAP_TEST_SLOT": "ota1",
            }
            result = subprocess.run(
                ["/bin/sh", str(payload)],
                env=env,
                check=False,
                timeout=15,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                b"\x01\x00\x00\x00", (feeder / "data" / "ota" / "index.bin").read_bytes()
            )
            self.assertIn(
                str(feeder / "ota2" / "AF203_FW"),
                (feeder / "app_start.sh").read_text(),
            )
            self.assertIn(
                "install_failed_installing_services",
                (feeder / "data" / "plaf203-bootstrap" / "status").read_text(),
            )

    def test_ssh_key_is_mandatory(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            with self.assertRaisesRegex(ValueError, "SSH public key is required"):
                build_payload(
                    output=temp / "payload",
                    state_agent_dir=self._state_agent_tree(temp),
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="a" * 64,
                    ssh_public_key="",
                    dropbear_dir=self._dropbear_tree(temp),
                )

    def test_state_agent_template_must_contain_expected_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            temp = Path(raw)
            agent = self._state_agent_tree(temp)
            run = agent / "runit" / "plaf203-state-agent" / "run"
            run.write_text("#!/bin/sh\nexec agent --allow-ip 198.51.100.10\n", encoding="ascii")
            with self.assertRaisesRegex(ValueError, "expected allowlist"):
                build_payload(
                    output=temp / "payload",
                    state_agent_dir=agent,
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="a" * 64,
                    ssh_public_key=VALID_PUBLIC_KEY,
                    dropbear_dir=self._dropbear_tree(temp),
                )

    def test_template_address_is_itself_a_valid_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            state_agent = self._state_agent_tree(root)

            output = root / "AF203_FW.bootstrap"
            build_payload(
                output=output,
                state_agent_dir=state_agent,
                home_assistant_ip="192.0.2.10",
                state_agent_token="a" * 64,
                ssh_public_key=VALID_PUBLIC_KEY,
                dropbear_dir=self._dropbear_tree(root),
            )

            self.assertTrue(output.is_file())

    def test_rejects_malformed_public_key_blob(self) -> None:
        with self.assertRaisesRegex(ValueError, "base64"):
            validate_public_key("ssh-ed25519 not-base64 user@example")

    def test_rejects_public_key_type_mismatch(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            validate_public_key(VALID_PUBLIC_KEY.replace("ssh-ed25519", "ssh-rsa", 1))

    def test_rejects_invalid_state_agent_token_shape(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, "64 lowercase hexadecimal"):
                build_payload(
                    output=root / "payload",
                    state_agent_dir=self._state_agent_tree(root),
                    home_assistant_ip="192.0.2.25",
                    state_agent_token="not-a-token",
                    ssh_public_key=VALID_PUBLIC_KEY,
                    dropbear_dir=self._dropbear_tree(root),
                )


if __name__ == "__main__":
    unittest.main()
