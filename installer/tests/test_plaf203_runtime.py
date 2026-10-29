"""Compile and execute the actual native health policy, not script-string tests."""
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "firmware/plaf203/runtime"


class NativeRuntimeTests(unittest.TestCase):
    def test_native_health_policy_and_strict_build(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            flags = ["cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                     "-Wformat=2", "-Wshadow", "-I", str(SOURCE)]
            builds = ((["test_health.c", "health.c"], "selftest"),
                      (["main.c", "health.c", "control.c", "feeding.c", "journal.c", "synthetic.c"], "runtime"),
                      (["test_feeding.c", "feeding.c", "journal.c", "synthetic.c"], "feeding-test"),
                      (["client.c"], "client"))
            for sources, name in builds:
                subprocess.run(flags + [str(SOURCE / source) for source in sources] + [
                                        "-o", str(target / name)], check=True, capture_output=True)
            result = subprocess.run([str(target / "selftest")], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.strip(), "PLAF203_RUNTIME_SELFTEST_OK")
            # Invalid command must fail before acquiring a runtime lock or HAL.
            result = subprocess.run([str(target / "runtime"), "--actuate"], capture_output=True)
            self.assertEqual(result.returncode, 64)
            result = subprocess.run([str(target / "feeding-test")], check=True, capture_output=True, text=True, timeout=60)
            self.assertIn("PLAF203_FEEDING_SELFTEST_OK", result.stdout)


if __name__ == "__main__":
    unittest.main()
