import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("kernel_qualification", ROOT / "scripts/qualify_qemu_gk7205v200.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SourceKernelTests(unittest.TestCase):
    def fixture(self, root):
        zimage = bytearray(64)
        struct.pack_into("<I", zimage, 0x24, 0x016f2818)
        dtb = bytearray(40)
        struct.pack_into(">II", dtb, 0, 0xd00dfeed, 40)
        (root / "zImage").write_bytes(zimage)
        (root / "plaf203-qemu.dtb").write_bytes(dtb)
        self.image(root, zimage + dtb)

    def image(self, root, payload, load=0x40008000):
        header = bytearray(64)
        struct.pack_into(">IIIIIII", header, 0, 0x27051956, 0, 1738368000,
                         len(payload), load, load, zlib.crc32(payload))
        header[28:32] = bytes([5, 2, 2, 0])
        struct.pack_into(">I", header, 4, zlib.crc32(header))
        (root / "uImage").write_bytes(header + payload)
        self.record(root)

    def record(self, root):
        data = {"artifacts": {p: hashlib.sha256((root / p).read_bytes()).hexdigest()
                              for p in ("zImage", "plaf203-qemu.dtb", "uImage")},
                "kernel": {"release": "4.9.37"}, "sources": {"kernel": {"revision": "fixture"}}}
        (root / "build-receipt.json").write_text(json.dumps(data))

    def test_source_image_is_copied_without_dtb_rewrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            result = mod.use_source_kernel(root / "uImage", root / "copy")
            self.assertEqual((root / "uImage").read_bytes(), (root / "copy").read_bytes())
            self.assertEqual(result["release"], "4.9.37")
            self.assertIn("no post-build DTB rewrite", result["mode"])

    def test_tampered_artifacts_and_headers_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            original = (root / "uImage").read_bytes()
            data = bytearray(original)
            data[12] ^= 1
            (root / "uImage").write_bytes(data)
            with self.assertRaisesRegex(ValueError, "does not match receipt"):
                mod.use_source_kernel(root / "uImage", root / "copy")
            self.record(root)
            with self.assertRaisesRegex(ValueError, "header CRC"):
                mod.use_source_kernel(root / "uImage", root / "copy")
            self.image(root, original[64:], load=0x42000000)
            with self.assertRaisesRegex(ValueError, "layout/CRC"):
                mod.use_source_kernel(root / "uImage", root / "copy")

    def test_extra_appended_dtb_is_rejected_even_with_valid_crcs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.image(root, (root / "zImage").read_bytes() + (root / "plaf203-qemu.dtb").read_bytes() * 2)
            with self.assertRaisesRegex(ValueError, "exactly zImage"):
                mod.use_source_kernel(root / "uImage", root / "copy")

    def test_serious_kernel_diagnostics_cannot_be_a_passing_boot(self):
        for line in ("Kernel panic - not syncing", "Oops: kernel", "BUG: test",
                     "WARNING: CPU: 0 at mm/cma.c", "jffs2: error during GC", "mtd: warning erase failure"):
            with self.subTest(line=line), self.assertRaises(RuntimeError):
                mod.check_kernel_diagnostics(line)
        result = mod.check_kernel_diagnostics("ramoops: attached 0x20000@0x41f00000, ecc: 0/0\n"
                                             "dwc3: failed to initialize core\n"
                                             "pl061_gpio 120b3000.gpio_chip: PL061 GPIO chip registered")
        self.assertEqual(result["serious_errors"], [])
        self.assertEqual(result["gpio_banks"], ["120b3000.gpio_chip"])
        self.assertEqual(len(result["probe_errors"]), 1)

    def test_source_lock_configuration_and_archive_references_agree(self):
        profile = ROOT / "firmware/plaf203"
        lock = json.loads((profile / "sources.lock.json").read_text())["kernel"]
        self.assertEqual(hashlib.sha256((profile / "board/linux.config").read_bytes()).hexdigest(),
                         lock["configuration_sha256"])
        self.assertIn(lock["file"], (profile / "configs/plaf203_qemu_defconfig").read_text())
        self.assertIn(f'{lock["sha256"]}  {lock["file"]}', (profile / "patches/linux/linux.hash").read_text())


if __name__ == "__main__":
    unittest.main()
