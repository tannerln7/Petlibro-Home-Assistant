#!/usr/bin/env python3
"""Exercise the real FMC NOR MMIO handler on synthetic, disposable flash."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time

from qualify_qemu_gk7205v200 import sha256_file, stop_guest


CTRL = 0x10000000
WINDOW = 0x14000000
SECTOR = 65536
SIZE = 32 << 20


class Qtest:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(5)
        self.sock.connect(str(path))
        self.stream = self.sock.makefile("rwb")

    def execute(self, command):
        self.stream.write(command.encode() + b"\n"); self.stream.flush()
        while True:
            result = self.stream.readline().decode().strip()
            if result.startswith("IRQ "):
                continue
            if not result.startswith("OK"):
                raise AssertionError(f"qtest failed: {command.split()[0]}")
            return result

    def reg(self, offset, value):
        self.execute(f"writel {CTRL + offset:#x} {value:#x}")

    def op(self, opcode, address, address_enable=True, length=4):
        self.reg(0x24, opcode)
        self.reg(0x2c, address if address_enable else 0x90000)
        # Deliberately poison the unused address source.
        buf = (0x90000 if address_enable else address).to_bytes(length, "big")
        for i, byte in enumerate(buf):
            self.execute(f"writeb {WINDOW + i:#x} {byte:#x}")
        self.reg(0x38, length)
        self.reg(0x3c, 0xc1 if address_enable else 0x81)

    def read(self, address, length):
        # The XIP aperture does not cover the entire 32 MiB chip. Read through
        # the controller's 256-byte register I/O buffer, including high addresses.
        data = bytearray()
        for offset in range(0, length, 256):
            size = min(256, length - offset)
            self.reg(0x24, 0x03)
            self.reg(0x2c, address + offset)
            self.reg(0x38, size)
            self.reg(0x3c, 0xc5)
            result = self.execute(f"read {WINDOW:#x} {size}")
            data.extend(bytes.fromhex(result.split()[1].removeprefix("0x")))
        return data

    def close(self):
        self.stream.close(); self.sock.close()


def exercise(qemu: Path, work_root: Path) -> dict:
    work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    work = Path(tempfile.mkdtemp(prefix="fmc-qtest-", dir=work_root))
    checks = []
    for locked in (False, True):
        image = work / ("locked.bin" if locked else "nor.bin")
        expected = bytearray().join(bytes([i % 251]) * SECTOR for i in range(SIZE // SECTOR))
        image.write_bytes(expected)
        path = work / "qtest.sock"
        cmd = [str(qemu), "-M", "gk7205v200,sensor=none", "-S", "-nic", "none",
               "-display", "none", "-monitor", "none", "-serial", "null",
               "-global", "driver=hisi-fmc,property=flash-jedec,value=0xc22019",
               "-global", f"driver=hisi-fmc,property=flash-file,value={image}",
               "-qtest", f"unix:{path},server=on,wait=off", "-qtest-log", str(work / "qtest.log")]
        if locked:
            cmd += ["-global", "driver=hisi-fmc,property=nor-wps-locked,value=on"]
        proc = None; client = None
        try:
            with (work / "host.log").open("wb") as log:
                proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log)
                deadline = time.monotonic() + 10
                while not path.exists():
                    if proc.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError("QEMU qtest startup failed; inspect private log")
                    time.sleep(0.02)
                client = Qtest(path)
                cases = [(0xdc, 0x23456, True), (0xdc, 0x1023456, True),
                         (0xdc, 0x34567, False), (0xdc, 0x1034567, False)]
                if not locked:
                    cases += [(0xd8, 0x44567, True), (0xdc, SIZE - 1, False), (0xdc, SIZE + 123, True)]
                for opcode, address, addr_en in cases:
                    client.op(opcode, address, addr_en)
                    base = address & ~(SECTOR - 1)
                    if not locked and base < SIZE:
                        expected[base:base + SECTOR] = b"\xff" * SECTOR
                    assert image.read_bytes() == expected, "backing mismatch or unintended sector change"
                    if base < SIZE:
                        assert client.read(base, SECTOR) == expected[base:base + SECTOR], "MMIO readback mismatch"
                    checks.append({"opcode": hex(opcode), "address": hex(address),
                                   "address_enable": addr_en, "locked": locked, "pass": True})
                if locked:
                    client.op(0x39, 0x1045678)  # Individually unlock one high block.
                    client.op(0xdc, 0x1045678)
                    base = 0x1040000
                    expected[base:base + SECTOR] = b"\xff" * SECTOR
                    assert image.read_bytes() == expected
                    assert client.read(base, SECTOR) == b"\xff" * SECTOR
                    client.op(0xdc, 0x23456)  # A different low block remains locked.
                    assert image.read_bytes() == expected
                    checks.append({"individual_unlock_above_16m": True, "other_blocks_still_protected": True, "pass": True})
        finally:
            if client:
                client.close()
            if proc:
                stop_guest(proc)
            path.unlink(missing_ok=True)
    result = {"qemu_sha256": sha256_file(qemu), "synthetic_flash_bytes": SIZE,
              "sector_bytes": SECTOR, "checks": checks,
              "scope": "real NOR MMIO and backing-file readback; not physical flash or all protection modes"}
    (work / "receipt.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(exercise(args.qemu, args.work_root), indent=2))
