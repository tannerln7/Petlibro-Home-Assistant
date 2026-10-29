# PLAF203 emulation sources and local builds

The maintained [QEMU recipe and lock](../firmware/plaf203/qemu/) pin QEMU
10.2.0, OpenIPC qemu-bench integration and the NOR-only erase4 patch.
Follow the [existing build procedure](../firmware/plaf203/oem-reference.md#rebuild-the-model-without-losing-its-integration)
and [replacement firmware qualification](../firmware/plaf203/README.md).

`emulation/qemu/` is an **ignored local checkout/build**, not a Git submodule or
vendored source dependency. Do not add it as a gitlink: that would omit its
generated/uncommitted model integration. The source-checkpoint audit reproduced
the pinned integration without building and compared all 263 pending source
files with the qualified local checkout; the sole additional patch is tracked
under `firmware/plaf203/qemu/`. Rebuild from those pins, not rolling upstream.

A fresh clone does not contain an emulator executable. Build in an external
cache using the linked procedure, then select that build directory through
`PLAF203_QEMU_BUILD_DIR` in the documented replacement qualification command.
The OEM launcher uses its documented `PLAF203_CACHE_ROOT` cache layout.
Do not move or replace an existing checkout merely to match an example path.

Keep private firmware, writable flash copies, enrollment keys, entropy inputs,
raw logs and generated images in protected storage outside Git. No physical
hardware passthrough or feeder deployment is part of these profiles.
