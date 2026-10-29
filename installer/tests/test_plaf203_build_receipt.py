import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("plaf203_build_receipt", ROOT / "scripts/plaf203_build_receipt.py")
receipt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receipt)


class BuildReceiptTests(unittest.TestCase):
    def test_offline_missing_archive_fails_without_starting_build_container(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docker = root / "docker"
            docker.write_text('#!/bin/sh\n[ "$1 $2" = "image inspect" ] || exit 99\necho sha256:test\n')
            docker.chmod(0o755)
            result = subprocess.run(["bash", str(ROOT / "scripts/build_plaf203_qemu.sh"), str(root / "cache")],
                                    env={**os.environ, "PATH": f"{root}:{os.environ['PATH']}", "PLAF203_OFFLINE": "1"},
                                    text=True, capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Offline input missing: buildroot-2024.02.10.tar.xz", result.stderr)
            self.assertFalse((root / "cache/downloads/buildroot-2024.02.10.tar.xz.part").exists())

    def test_invalid_offline_policy_rejected(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/build_plaf203_qemu.sh"), "/not-used"],
                                env={**os.environ, "PLAF203_OFFLINE": "maybe"},
                                text=True, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PLAF203_OFFLINE must be 0 or 1", result.stderr)

    def test_source_tree_validation_does_not_overwrite_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "Makefile").write_bytes(b"original\n")
            (source / "nested").mkdir()
            # Buildroot contains symlink targets ending in '/', which must not
            # be normalized before comparison with archive link metadata.
            (source / "link").symlink_to("nested/", target_is_directory=True)
            archive = root / "source.tar.xz"
            with tarfile.open(archive, "w:xz") as tar:
                tar.add(source, arcname="buildroot-1")
            receipt.verify_tree(archive, source)
            (source / "local.mk").write_text("untracked build override")
            with self.assertRaisesRegex(ValueError, "Unexpected Buildroot input"):
                receipt.verify_tree(archive, source)
            (source / "local.mk").unlink()
            (source / "Makefile").write_bytes(b"local change\n")
            with self.assertRaisesRegex(ValueError, "Modified Buildroot input"):
                receipt.verify_tree(archive, source)
            self.assertEqual((source / "Makefile").read_bytes(), b"local change\n")
            (source / "Makefile").unlink()
            (source / "Makefile").symlink_to(archive)
            with self.assertRaisesRegex(ValueError, "nonregular"):
                receipt.verify_tree(archive, source)

    def test_archive_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bad.tar"
            with tarfile.open(archive, "w") as tar:
                member = tarfile.TarInfo("source/../bad")
                member.size = 1
                tar.addfile(member, io.BytesIO(b"x"))
            with self.assertRaisesRegex(ValueError, "Unsafe archive"):
                receipt.verify_tree(archive, Path(directory))

    def test_downloads_must_match_upstream_hashes_not_cache_hash_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "package/demo").mkdir(parents=True)
            cache = root / "dl/demo"
            cache.mkdir(parents=True)
            source = cache / "demo.tar.gz"
            source.write_bytes(b"source")
            digest = receipt.sha256(source)
            hashes = root / "package/demo/demo.hash"
            hashes.write_text(f"sha256  {digest}  demo.tar.gz\n")
            info = {"demo": {"dl_dir": "demo", "downloads": [{"source": source.name}]}}
            self.assertEqual(receipt.downloads(root, info), {"demo/demo.tar.gz": digest})
            source.write_bytes(b"tampered")
            (cache / "fake.hash").write_text(f"sha256 {receipt.sha256(source)} demo.tar.gz\n")
            with self.assertRaisesRegex(ValueError, "Unverified package"):
                receipt.downloads(root, info)
            info["demo"]["dl_dir"] = "../outside"
            with self.assertRaisesRegex(ValueError, "Unsafe package"):
                receipt.downloads(root, info)

    def test_receipt_is_deterministic_and_omits_secret_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            profile = repo / "firmware/plaf203"
            for name in ("configs", "overlay", "qemu"):
                (profile / name).mkdir(parents=True)
            (repo / "scripts").mkdir()
            files = ["post-build.sh", "S50sshd", "Config.in", "external.mk", "external.desc", "configs/test"]
            for name in files:
                (profile / name).write_text("configuration")
            (profile / "sources.lock.json").write_text('{"schema":1}')
            for name in ("build_plaf203_qemu.sh", "plaf203_build_receipt.py"):
                (repo / "scripts" / name).write_text("script")
            output = repo / "output"
            for name in ("images", "build/busybox-1", "target/etc/plaf203"):
                (output / name).mkdir(parents=True)
            for name in (".config", "build/busybox-1/.config", "images/rootfs.squashfs"):
                (output / name).write_text("build output")
            for name in ("enrollment.pub", "test.seed"):
                (output / "target/etc/plaf203" / name).write_text("DO-NOT-RECORD-ME")
            with patch.object(receipt.subprocess, "check_output", return_value="gcc 13.3\n"), \
                    patch.object(receipt, "downloads", return_value={}):
                first = receipt.receipt(repo, repo, output, {}, "sha256:image", 123)
                self.assertEqual(first, receipt.receipt(repo, repo, output, {}, "sha256:image", 123))
            serialized = json.dumps(first)
            self.assertNotIn("DO-NOT-RECORD-ME", serialized)
            self.assertNotIn(directory, serialized)
            self.assertTrue(first["development_enrollment"])
            self.assertTrue(first["test_entropy_seed"])
            source = profile / "runtime"
            tree = output / "build/plaf203-runtime-1"
            source.mkdir(); tree.mkdir()
            (source / "main.c").write_text("source")
            (tree / "main.c").write_text("source")
            (output / "target/usr/sbin").mkdir(parents=True)
            (output / "target/usr/libexec").mkdir(parents=True)
            (output / "target/usr/sbin/plaf203-runtime").write_text("binary")
            (output / "target/usr/libexec/plaf203-runtime-selftest").write_text("test binary")
            (output / "target/usr/libexec/plaf203-feeding-selftest").write_text("feeding test binary")
            (output / "target/usr/bin").mkdir(parents=True)
            (output / "target/usr/bin/plaf203-ctl").write_text("control client binary")
            info = {"plaf203-runtime": {"source_dir": "build/plaf203-runtime-1"}}
            with patch.object(receipt.subprocess, "check_output", return_value="gcc 13.3\n"), \
                    patch.object(receipt, "downloads", return_value={}):
                result = receipt.receipt(repo, repo, output, info, "sha256:image", 123)
                self.assertEqual(result["runtime"]["source_sha256"]["main.c"], receipt.sha256(source / "main.c"))
                (tree / "main.c").write_text("stale copied source")
                with self.assertRaisesRegex(ValueError, "Stale local"):
                    receipt.receipt(repo, repo, output, info, "sha256:image", 123)
                (tree / "main.c").write_text("source")
                (tree / "extra.c").write_text("untracked source")
                with self.assertRaisesRegex(ValueError, "Unexpected local"):
                    receipt.receipt(repo, repo, output, info, "sha256:image", 123)

    def test_local_package_requires_explicit_source_mapping(self):
        info = {"plaf203-runtime": {"dl_dir": "plaf203-runtime", "downloads": [
            {"source": "plaf203-runtime-1.tar.gz", "uris": ["local+/work/runtime"]}]}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(receipt.downloads(root, info, local_sources={
                "plaf203-runtime": Path("/work/runtime")}), {})
            with self.assertRaisesRegex(ValueError, "Unexpected local"):
                receipt.downloads(root, info, local_sources={"plaf203-runtime": root})
            with self.assertRaises(FileNotFoundError):
                receipt.downloads(root, info)


if __name__ == "__main__":
    unittest.main()
