# Disposable OEM QEMU reference

Bounded research alongside the [replacement image](README.md), not another
firmware implementation. **Execution of disposable OEM firmware inside this
isolated emulator was specifically authorized.** Never execute recovered ARM
binaries on the host or treat this profile as physical-device deployment.

## Inputs and observed execution

The reconstruction inventory's `primary_oem_flash_backup` selects the owner's
personally acquired August 18 original reference. The runner requires the exact
33,554,432-byte `plaf203-flash-32m.bin`, SHA-256
`cb812b7619bfe77cea4d35b2a7e0fd80df76fb673335673e1e3af16657dac1d7`.
It makes an exclusive, independent regular-file copy and rechecks the preserved
source after execution. It never constructs a synthetic replacement image.

Reuses QEMU v10.2.0 `698104725efad4b29079d857dfdebbd804e34c99` plus
OpenIPC qemu-bench `b011673b31884683a534a3f3e1a2b080d5a99f19`.
Current qualified binary SHA-256:
`a2d54f7b32c4cd50522bec44d73c6aab590fbd77359b8355acde38e1a72a796c`.
This adds only the NOR `0xdc` erase case through the tracked
[patch and source lock](qemu/source-lock.json). The earlier `5c6541e2…` binary
is retained in historical receipts, not the current runner pin. A changed binary
requires review. Existing build/container prerequisites remain in [README.md](README.md).

[Sanitized execution receipts](oem-reference-qualification-20261029.json) record:

- Unmodified full flash executes captured first-stage code/U-Boot 2016.11,
  detects 32 MiB NOR, loads its valid environment and original Linux 4.9.37.
- Original JFFS2 root mounts from `mtdblock3`; no direct `-kernel`, `-append`,
  `-dtb` or `-initrd`, and no patched firmware, environment or DTB bytes.
- Nonce-correlated serial commands confirm BusyBox PID 1, JFFS2 root and a
  running `AF203_FW` executable with authoritative SHA-256
  `da353ba37a7d8c1c0790827e5ec267a180519affb5666ae4efd93e5362e00b71`.
  Command echo cannot pass the probe. This does **not** qualify normal feeding,
  media, networking or sustained application health.
- Initial short-run receipts recorded QMP timeouts. The runner subsequently
  fixed UART backpressure by draining UART while waiting for QMP; repeated QMP
  checks then succeeded. Those earlier failures are not evidence of a hung OEM
  application. Native watchdog resets remain independently confirmed.
- The [stability/storage checkpoint](oem-reference-finalization-20261029.json)
  preserves two pre-erase-fix 930-second runs: `app-disabled/reset` and
  `app-pty/none`, each with 31/31 terminal probes and 61/61 QMP checks. Application
  hashes stayed original in the latter. These establish responsiveness, **not**
  filesystem correctness; later interactive writes exposed the missing erase.
- The fixed binary passes direct NOR MMIO/backing tests and filesystem
  churn/readback/cold-restart qualification described below. `watchdog=none`
  is an explicit emulator policy, never native watchdog or feeder-health proof.

No replacement kernel/userspace is substituted. The model's synthetic mask-ROM boot
stub and virtual peripherals remain emulator components—not recovered silicon
ROM or physical DDR-training validation.

## Launch and reset

Run from the replacement worktree with the qualified container/build. Preferred
interactive profile (no periodic serial command injection):

```sh
bash scripts/run_plaf203_oem_qemu.sh \
  --profile app-pty --watchdog-action none --interactive --seconds 3600
```

Wait for the normal init sequence to complete before typing. The boot-time
diagnostic shell is transient in `app-*` profiles. **Ctrl-]** stops/reaps the
whole disposable VM and restores the host terminal; Ctrl-C is sent to the
guest. This is not a clean guest shutdown: run guest `sync` first if preserving
the working image for inspection. A fresh launch always starts from a new copy.
Interactive output can include OEM secrets: use a private terminal, not shared
logs. `--interactive` rejects `--probe` and `--storage-check`, so automated
commands cannot splice into partially entered manual input. A real PTY test
verified split command entry across QMP polls, then Ctrl-] cleanup.

`PLAF203_RECONSTRUCTION_ROOT`, `PLAF203_CACHE_ROOT` and `PLAF203_BUILD_IMAGE`
override host locations/image. Defaults use the sibling reconstruction checkout,
existing research cache and `plaf203-qemu-bringup:review`. The host launcher mounts
only inventory, preservation collection, scripts and QEMU build read-only;
experiment storage is writable. It retains all original isolation/resource limits.

| Profile | Changes/bypasses relative to original flash | Intended use |
| --- | --- | --- |
| `native` | None; default watchdog action is reset | Authentic startup/reset comparison, not a sustained reference |
| `app-disabled` | Append `init=/bin/sh` to the disposable environment, recompute CRC, transiently bind a script differing only in the application launch line, then `exec /sbin/init` | Normal OEM init/modules/services without the application; stable fallback |
| `app-pty` | Same staging, but launch exactly one original OTA2 application through `script -qf -c 'exec /user/ota2/AF203_FW </dev/null'` | Preferred reference with `--watchdog-action none`; original application bytes unchanged |
| `shell` | Append `init=/bin/sh`; do not re-enter normal init | Minimal recovery/debug fallback; not the 930-second normal-init qualification |

The fixed application launch path belongs to this identified backup. A guard
requires exactly one matching original launch line; an unfamiliar startup script
must be reviewed instead of silently replaced. PTY output goes to
`/mnt/af203.log` on an 8 MiB tmpfs. Unlike the historical four-file `svlogd`
configuration, this bounded reference uses one file; extended noisy sessions
can fill it. It is useful diagnostic logging, not a production logging service.
The actual watchdog module is application-started; suppressing application startup
also removes that startup dependency rather than qualifying watchdog servicing.

Reset means a **new invocation**, creating a private `oem-*` directory and fresh
flash copy. Never use a previous experiment's mutated backing as original input.
Normal OEM JFFS2 writes change the working flash. Failed experiments also remain
private. `serial.log`, `qemu-debug.log`, `host.log`, UART/QMP sockets,
`command.json` and `receipt.json` reside there; `/experiments` maps to `$private`.
These raw files may contain credentials; do not publish or print them.

`--probe` sends one read-only serial command around ten seconds after start:
PID 1's executable link, root filesystem type and application executable hash.
No settings, keys or command-line arguments are read. Omit it for passive boot.
`--seconds` accepts 10–3600 (default 20); `--seconds 55` reproduces the reset
investigation. `--watchdog-action none` explicitly alters the reset policy.
A three-second QMP diagnostic allowance and bounded cleanup can extend wall time
past the run budget. Combined log budget defaults to 8 MiB, sampled every
100 ms; the container's per-file hard limit is 128 MiB (enough for flash copy).
CPU/memory/PID limits are supplied by the documented container invocation.

Except for `--storage-check`, exit zero means the experiment completed, **not**
that all guest assertions passed. Inspect the receipt's individual fields;
missing evidence or an unqualified binary is an error, not PASS or a silent skip.

## Isolation, limitations and intended use

- Device-level `hisi-fmc.flash-file` and JEDEC `0xc22019` select NOR; the
  machine-level >16 MiB NAND heuristic is not used.
- `-nic none` and pre-execution QMP `info network` reject any backend/peer.
  A disconnected FEMAC remains modeled. The runner additionally requires a
  loopback-only container namespace. No port forwarding, TAP/bridge, external
  networking, synthetic services or device passthrough is configured.
- The non-root container has no capabilities, writable source mounts or Docker
  socket. Only experiment storage and temporary storage are writable. UART
  root-shell access is private Unix-socket access, not exposed TCP/SSH.
- `sensor=none`; no fabricated feeder feedback. AW9523B, motor/mechanics, ADC,
  USB radio and camera/ISP operation remain unqualified. I2C errors occur;
  WLAN module startup output does not establish an accessible radio.
- Watchdog writes to read-only offset `0x4` remain a model limitation. The
  previously unhandled FMC command `0xdc` is now fixed and tested; this does not
  qualify every NOR command, protection feature or physical flash behavior.

Useful now: interactive OEM Linux, hash-verified application/logs, init/service
sequencing, tested filesystem operations and controlled missing-peripheral
failures. It is not a healthy physical feeder/media/cloud or recovery oracle.
Keep this reference optional;
do not block independent firmware development on perfect OEM hardware emulation.

## Rebuild the model without losing its integration

Use the pinned qemu-bench and QEMU revisions above in `CACHE_ROOT/qemu-bench`
and its `qemu-src` child. The offline builder is:

```sh
bash scripts/build_plaf203_emulator.sh /srv/cache/plaf203-bringup-research
```

For **fresh clean clones only**, add `--initialize`. It executes the pinned
upstream `qemu/setup.sh` integration portion, retaining its model copies and
Kconfig/Meson changes, then applies the tracked NOR-only patch and builds ARM.
Existing configured caches skip integration; unknown FMC changes cause failure,
not overwrite. A clean initial checkout can be prepared with ordinary pinned
clones before the offline build:

```sh
CACHE_ROOT=/srv/cache/plaf203-emulator-clean
mkdir -p "$CACHE_ROOT"
git clone https://github.com/OpenIPC/qemu-bench.git "$CACHE_ROOT/qemu-bench"
git -C "$CACHE_ROOT/qemu-bench" checkout b011673b31884683a534a3f3e1a2b080d5a99f19
git clone --branch v10.2.0 https://gitlab.com/qemu-project/qemu.git "$CACHE_ROOT/qemu-bench/qemu-src"
bash scripts/build_plaf203_emulator.sh "$CACHE_ROOT" --initialize
```

The builder uses the existing container at `/work/qemu-src`, matching Meson's
configured path, and validates source/patch hashes. Its cached rebuild reproduced
the pinned binary. Fresh independent-cache binary reproducibility and fully
frozen distro dependencies are **not** claimed. Do not simply rerun upstream
setup on the working cache: its copy step would remove a local FMC edit.

## Storage and automated checks

```sh
bash scripts/run_plaf203_oem_qemu.sh \
  --profile app-pty --watchdog-action none --probe --seconds 180 --storage-check
```

This writes only a uniquely named test directory in the disposable guest. It
retains a 128 KiB random payload, runs 24 × 256 KiB random write/sync/cache-drop/
readback/delete cycles (6 MiB), and verifies hashes and sizes. It then stops the
synced VM, starts a new process using the **same** mutated flash, reapplies only
the transient init wrapper, and verifies payload identity/deletion persistence.
It is a synced cold restart, not a clean-unmount, kernel-reboot or power-failure
qualification. Storage failures produce nonzero exit status.

The recorded run observed 0→1 changes in 61 rootfs sectors during the churn
window. Both NOR programming paths only clear bits, so this is evidence of
erase activity under JFFS2. Together with successful churn/readback/restart and
absence of previous GC diagnostics, it narrows the earlier failure. It does
**not** prove every GC victim/reclamation path or physical recovery behavior.

For direct controller tests, run `scripts/test_plaf203_fmc.py` in the same
network-none container, with `--qemu` pointing to the qualified binary and
`--work-root` to private writable storage. It boots no OEM code: synthetic
32 MiB images exercise `0xdc` with register and four-byte big-endian I/O-buffer
addresses below/above 16 MiB, alignment, chip-end bounds, model WPS locks/unlock,
full-sector readback and exact whole-backing comparisons; legacy `0xd8` is retained.
The controller read path is used because the XIP aperture does not cover the
whole chip. This tests the model's addressing/protection, not a complete physical
SPI protocol. The NAND handler is unchanged.
