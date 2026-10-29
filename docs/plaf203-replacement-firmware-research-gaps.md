# PLAF203 replacement firmware — living research-gap register

**Updated:** 2026-10-29 · **Architecture:**
[development centerline](plaf203-replacement-firmware-architecture.md).

This is the implementation-knowledge register, not a request to reverse-engineer
every OEM function. Detailed binary/hardware evidence belongs in the reconstruction
repository. This document records its sufficiency, significance and dependencies,
distinguishing missing knowledge from independent design and qualification work.

## Baseline, statuses and maintenance

- Firmware branch: `feature/replacement-firmware-foundation`; architecture
  `1d2c01a3886779c85f693fe56a9f44123062d03d`, based on management `9edd22d`.
  The other worktree's unresolved merge is excluded, not silently resolved here.
- OEM saved baseline: reconstruction `adfe4c7`, application SHA-256
  `da353ba37a7d8c1c0790827e5ec267a180519affb5666ae4efd93e5362e00b71`,
  90 programs, 87 executable-function exports, all 14 direct runtime providers.
  The 2026-10-28 E8 investigation freshly verified that baseline, applied a
  bounded boot/radio/watchdog group, and established the next saved/exported
  checkpoint at reconstruction `b29b645`. E9 extends reconstruction on
  `bbd4f113738f4e43f572babc3cbd047fb88122e5` with verified two-stage bootloader,
  single-copy `bootargs`, appended DTB (`gpio3` omission), four-stage pinmux,
  RTC fallback, watchdog parameter, and audio-codec recovery plus routine saved
  Ghidra annotations across `/application/AF203_FW`,
  `/vendor/platform/sysconfig.ko`, and `/vendor/kernel/gk7205v200_acodec.ko`.
  Program count/provider coverage did not change.
- Camera reference: standalone go2rtc `petlibro` at `6ca2e665`, not automatically
  equivalent to the management base's vendored snapshot. Local edits are preserved.
- **Access and emulation active:** authoritative Ghidra, dedicated `ssh petlibro`
  read-only observation, and local `gk7205v200` QEMU emulation (E10) are working.
  No flashing, bootenv mutation, or disruptive physical experiment has been performed.

Priorities: **P0** unblocks minimal Linux/QEMU/network/SSH or constrains safe
deployment; **P1** unblocks essential feeding; **P2** follows those foundations.
Priority does not authorize an experiment.

Statuses distinguish **Resolved for scope** (sufficient for the named use, not
blanket qualification), **Partial** (useful evidence exists, named acceptance
evidence is missing), **Open** (acceptance not established), **Access deferred**
(owner-deferred observation), and **Design deferred** (boundary/requirements
known, independent choice belongs to later work rather than more OEM analysis).

Retain IDs and resolved entries. Each update identifies source revision,
program-qualified address or observation provenance, confidence and intended
implementation use. Separate instructions, inference, physical observation and
synthetic results. Record corrections, dependencies and acceptance changes.
Never close a gap because a tool succeeded, a source names the SoC, or a guessed
value makes a test pass. Consolidate existing evidence instead of rediscovering it.

### Evidence routes

- **[E0 — saved analysis reconciliation](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/research/analysis-reconciliation.md)**:
  recovery integrity, metadata, corrected findings and limits.
- **[E1 — pinned source findings](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/06d0c3d0e3acf846e1a6ed7ae3cbedbd5e47b4d0/docs/research/replacement-platform-source-baseline.md)**
  and **[source hashes/settings](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/06d0c3d0e3acf846e1a6ed7ae3cbedbd5e47b4d0/agents/codex/outputs/replacement-platform-research/source-baseline.json)**:
  OpenIPC `9a84dce`, QEMU fork `b011673`, kernel branch resolved to `bb0266a`.
- **[E2 — boot/runtime contract](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/drivers/boot-and-runtime-contract.md)**
  and **[hardware interfaces](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/drivers/kernel-and-hardware-interfaces.md)**.
- **[E3 — feeding/deadline contract](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/drivers/feeder-event-and-deadline-contract.md)**
  and **[host-only models/tests](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/implementation/README.md)**:
  independent requirements, not measured physical limits.
- **[E4 — subsystem evidence](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/subsystems/feeder-media-network-protocols.md)**
  and **[BLE/ISP contracts](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/subsystems/ble-provisioning-and-isp-contracts.md)**.
- **[E5 — original-library recovery](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/research/device-library-reconciliation.md)**:
  matching codec, modules/runtime, provider links, loader and radio blobs.
- **[E6 — management implementation](../management/README.md)**,
  [existing authority](architecture.md), [State Agent](../state-agent/README.md),
  [installer](../installer/README.md), [camera contract](mqtt-camera-contract.md).
- **[E7 — standalone camera source/tests](https://github.com/tannerln7/go2rtc/tree/6ca2e665b1bf483462562165e1cf011526a259f8)**:
  session, transport and media behavior, not a proprietary-transport requirement.
- **[E8 — live platform contracts and saved checkpoint](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/b29b6455e06a5f15b5e06aea41f08a8cd3d91ae8/docs/research/replacement-platform-device-contracts.md)**
  and **[allowlisted observations](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/b29b6455e06a5f15b5e06aea41f08a8cd3d91ae8/agents/codex/outputs/replacement-platform-research/device-observations.json)**:
  live NOR/boot/init/radio/RTC facts; six function annotations, one name/return
  recovery, unchanged bytes/types/boundaries/links, 90-program archive restore
  comparison. Raw/private captures remain outside tracked source.
- **[E9 — bootloader, DTB, pinmux, RTC and codec reconstruction](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/bbd4f113738f4e43f572babc3cbd047fb88122e5/docs/research/replacement-platform-device-contracts.md)**,
  **[boot/runtime contract](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/bbd4f113738f4e43f572babc3cbd047fb88122e5/docs/drivers/boot-and-runtime-contract.md)**,
  **[hardware interfaces](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/bbd4f113738f4e43f572babc3cbd047fb88122e5/docs/drivers/kernel-and-hardware-interfaces.md)**,
  and **[allowlisted observations](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/bbd4f113738f4e43f572babc3cbd047fb88122e5/provenance/replacement-platform-research/device-observations.json)**:
  two-stage Goke bootloader (`mtd0` 3,908-byte `regbin` + SPL + U-Boot `2016.11`),
  single-copy 256 KiB `bootargs` environment (`0x00` upper-half padding and
  compiled-in default-env `bootcmd` omission), 12,436-byte appended OEM DTB
  (`gpio3` omission), four-stage pinmux chain, live `/dev/rtc0` vs
  `/user/data/rtc/rtc_time.bin` fallback, `gk7205v200_wdt.ko` `perm=0`,
  `gk7205v200_acodec.ko`, claimed live `mtd3`/32 MiB flash backup digests
  (**withdrawn; see REC-01**), and routine
  saved Ghidra annotations across `/application/AF203_FW`,
  `/vendor/platform/sysconfig.ko`, and `/vendor/kernel/gk7205v200_acodec.ko`.
- **E10 — reproducible OpenIPC/QEMU `gk7205v200` qualification**
  ([Dockerfile.qemu-bringup](../docker/Dockerfile.qemu-bringup),
  [qualify_qemu_gk7205v200.py](../scripts/qualify_qemu_gk7205v200.py),
  [test_qualify_qemu_gk7205v200.py](../installer/tests/test_qualify_qemu_gk7205v200.py)):
  compiled QEMU `v10.2.0` (`698104725efad4b29079d857dfdebbd804e34c99`) +
  `OpenIPC/qemu-bench` (`b011673b31884683a534a3f3e1a2b080d5a99f19`), baseline vs
  PLAF203-adapted `uImage.gk7205v200` + `rootfs.squashfs.gk7205v200`
  (`BUILD_ID=nightly-20261024-856f22e`, `BUILD_SHA=856f22ef7e046f59a3f409e157eb1e06d42fd03c`),
  DTB `gpio_chip@120b3000` (`gpio3`) fixup, 32 MiB `mx25l25635f` (`0xc22019`) SPI NOR
  partition map, `goke_mem` `mem=64M osmem=45M mmz_allocator=gk`, SLIRP networking,
  key-only Dropbear SSH + unauthorized rejection, `gpio25`/`gpio26` sysfs access,
  `120e0000.rtc`, `1.0004x` timer rate, and `SC_SYSRES` hardware reboot.

**E11 — independent review and minimal platform implementation:**
[build/qualification instructions](../firmware/plaf203/README.md) and the
[sanitized qualification receipt](../firmware/plaf203/qualification-20261028.json).
E9's added OEM findings and the independent corrections are working-tree
continuations in reconstruction `docs/research/replacement-platform-device-contracts.md`
sections 7–8, not contents of the older `bbd4f11` permalinks above. Do not cite
those permalinks as proof that the continuation was already committed.

**E12 — isolated original OEM full-flash execution (2026-10-29):**
[OEM reference profile](../firmware/plaf203/oem-reference.md),
[runner](../scripts/run_plaf203_oem_qemu.py) and
[sanitized execution receipts](../firmware/plaf203/oem-reference-qualification-20261029.json).
Unmodified full flash reaches BusyBox/JFFS2 and a running application with the
authoritative SHA-256. Longer runs reset through the modeled watchdog; ignoring
the reset does not establish sustained health. No feeder access, source-image
mutation, QEMU patch, network connectivity or new peripheral model was involved.

**E13 — OEM terminal/storage finalization:**
[profile and commands](../firmware/plaf203/oem-reference.md),
[source lock/patch](../firmware/plaf203/qemu/source-lock.json) and
[preserved stability/storage evidence](../firmware/plaf203/oem-reference-finalization-20261029.json).
Two historical 930-second runs pass 31/31 terminal and 61/61 QMP checks each.
UART draining during QMP fixes the earlier harness backpressure defect; physical
UART pinmux and actual watchdog resets remain separate. The minimal NOR `0xdc`
fix passes 12 direct MMIO/backing checks; original-app filesystem churn/readback
and same-flash cold restart now pass, with erase activity observed. A reliable
interactive entry point rejects simultaneous automated serial writers. This
qualifies a bounded reference, not full OEM hardware operation or restoration.

**E14 — independent native userspace builds (2026-10-29):**
[source lock](../firmware/plaf203/sources.lock.json),
[build/qualification procedure](../firmware/plaf203/README.md), and
[reproducibility receipt](../firmware/plaf203/reproducibility-20261029.json).
Two independent source/SDK/build trees using only shared verified downloads and
the same disposable enrollment/entropy inputs produced byte-identical rootfs
images and build receipts, inside the same immutable offline container image.
All 19 selected package downloads match the verified Buildroot release's hashes;
the receipt describes 34 selected package entries. This closes the fresh-build
userspace subquestion, not kernel/bootloader source closure or production trust.
The new image also passes the established NOR/U-Boot, three-cold-boot/software
reboot, JFFS2 state/SSH identity and negative security checks. Qualification uses
the relocated emulator read-only inside an offline, unprivileged container.

**E15 — source-built Linux/DTB/modules (2026-10-29):**
[source/kernel checkpoint](../firmware/plaf203/kernel-qualification-20261029.json),
[configuration and QEMU DTS](../firmware/plaf203/board/), and
[build procedure](../firmware/plaf203/README.md#source-built-kernel-and-qemu-dtb).
Two offline kernel builds produce identical kernel/DTB/uImage/ELF/configuration,
ABI symbols, 11 modules, rootfs and receipts. Complete NOR/security suites pass
with the previous E14 userspace and the integrated image; `tun` loads/unloads in
all three integrated cold boots. The retained prebuilt kernel/DTB is no longer a
build dependency. Exact reconstruction of that old binary is not established;
the accepted equivalent is pinned Linux 4.9.37 `bb0266a` plus its release config.

**E16 — native runtime / health foundation (2026-10-29):**
[implementation and policy](../firmware/plaf203/README.md#native-runtime-and-health-ownership),
[source](../firmware/plaf203/runtime/), and
[build/qualification checkpoint](../firmware/plaf203/runtime-qualification-20261029.json).
The permanent C service is packaged and supervised by BusyBox init. Deterministic
host/ARM tests cover initialization, stale/failed essentials, optional-fault
isolation, previous-generation work, fault latching, watchdog feed inhibition
and shutdown. Three NOR cold boots additionally test actual process ownership,
stalls, crash respawn and orderly shutdown; existing security/persistence gates
pass. Missing/malformed SSH enrollment does not block native initialization.
Warm-reboot SSH reconnection required a stable synthetic Ethernet MAC; the old
E15 reboot check only observed the serial startup marker. A separate modeled
watchdog reset passes, while the standard SP805 module only loads/unloads without
binding to a device. This closes the **non-actuating health-owner** subquestion,
not Linux/physical watchdog coverage or feeder safety. No physical experiments
or QEMU model changes were made; WDT-01/FEED-01 remain Partial.

**E17 — durable synthetic feeding operations (2026-10-29):**
[native contracts/journal design](../firmware/plaf203/README.md#durable-feeding-operations-synthetic-only),
[implementation](../firmware/plaf203/runtime/feeding.c), and
[qualification checkpoint](../firmware/plaf203/feeding-qualification-20261029.json).
The single owner persists admission, intent, effect fences, progress and terminal
outcomes. Epoch/highwater fencing prevents evicted IDs from executing again;
startup aborts unstarted admissions and marks effect-bearing unfinished work
indeterminate without replay. There is no automatic uncertainty clearance.
The host/ARM self-test uses deterministic synthetic hardware and process-kill/
storage-failure injection, not physical power cuts. The QEMU regression exercises
the actual JFFS2 journal, not only a host filesystem model. FEED-01/STATE-01 remain
Partial until the hardware and physical durability/recovery boundaries qualify.

E0–E5 and E8–E9 may require authorized access to private evidence. Old revision-specific
prose is not a current absence claim: runtime providers and codec are recovered.

## Priority and dependency view

| ID | Subsystem / question scope | Priority | Status |
| --- | --- | --- | --- |
| SRC-01 | Concrete upstream source/configuration starting point | P0 | Resolved for scope |
| BUILD-01 | Minimal profile and complete immutable input closure | P0 | Partial |
| PLAT-01 | Actual RAM/flash/partition inventory | P0 | Partial |
| BOOT-01 | OEM-independent boot/init dependencies | P0 | Partial |
| REC-01 | Complete off-device backup and meaningful restoration | P0 | Partial |
| KABI-01 | Kernel configuration and module source/ABI closure | P0 | Partial |
| EMU-01 | Pinned GK7205V200 machine and exact boot artifacts | P0 | Partial |
| EMU-02 | Emulated network, secure SSH and reboot/persistence | P0 | Partial |
| WIFI-01 | Physical radio bus/driver/firmware/initialization | P0 | Partial |
| TRUST-01 | Secure first access, custody and update trust | P0 | Partial |
| BSP-01 | Pinmux, GPIO/expander and safe initialization | P1 | Partial |
| FEED-01 | Portion evidence, sequencing and dual bowls | P1 | Partial |
| ADC-01 | Valid acquisition and current/battery calibration | P1 | Partial |
| WDT-01 | Watchdog lifecycle and essential-health policy | P1 | Partial |
| POWER-01 | DC/battery and restart/shutdown safety | P1 | Partial |
| STATE-01 | Durable schemas, corruption and interrupted operations | P1 | Partial |
| SCHED-01 | Offline time, eligibility and reboot policy | P1 | Partial |
| API-01 | Native commands/events and operation semantics | P1 | Design deferred |
| BLE-01 | Bluetooth transport and onboarding/recovery role | P2 | Partial |
| MEDIA-01 | Sensor/ISP/codec compatibility and isolation | P2 | Partial |
| HA-01 | Native adapter, parity and single-writer cutover | P2 | Design deferred |

Advance two lanes: **source locking → emulated boot → emulated network/SSH**;
and **read-only board/boot inventory → backup/recovery constraints → physical
qualification**. Board flash knowledge is not required for an isolated synthetic
profile; synthetic success does not authorize deployment. Do not block basic
Linux bring-up on perfect media analysis.

## Platform and first bring-up

### SRC-01 — upstream source/configuration discovery

- **Question/use:** Is there a concrete GK7205V200 build/emulator source surface
  from which to begin qualification, rather than only an advertised product name?
- **Resolution:** E1 verifies the actual defconfig/kernel config, Buildroot
  2024.02.10, musl toolchain prefix, kernel ref and real QEMU machine registration.
  Thirteen files match pinned Git objects. The independent review downloaded
  the SDK and verified the publisher SHA-256
  `f5062be797d0b2714b9311437168cde2bc72f28273d8d9797e6a897774775924`.
- **Impact/dependencies:** Enables BUILD-01/EMU-01 source selection. No physical
  compatibility, reproducible output or boot is established by this entry.
- **Acceptance/status:** **Resolved for scope**: exact retrievable revisions,
  paths/settings recorded. Reopen on changed/unavailable inputs.
- **Architecture:** OpenIPC has concrete starting inputs; no evidence yet
  requires changing platform or making kernel modernization a prerequisite.

### BUILD-01 — minimal profile and immutable input closure

**Independent implementation update (E11):** a conventional Buildroot external
tree now builds the minimal userspace from source using the hash-verified
OpenIPC GCC13.3 musl SDK. It replaces the stock cloud/claim/media/service set
with BusyBox init, DHCP, local logging, persistent entropy/identity and key-only
Dropbear. E14 extends the earlier cached rebuild result to two independently
extracted source/SDK/build trees: both rootfs and metadata are byte-identical,
with the same verified downloads, enrollment/seed and container ID. E15 now
builds the kernel, source DTB and modules too; only captured OEM SPL/U-Boot remains
a retained boot-chain binary input. Package-security review, physical board
configuration and frozen/reconstructible container inputs keep
BUILD-01 **Partial**. Do not reopen stock-lite patching as the product foundation.

- **Question/current evidence:** Which exact package/config/input set is our
  reproducible minimal platform? E1 identifies stock `lite`, musl and a Linux
  4.9.37 config, but it also selects camera/web UI/motors and an 8 MiB image
  setting. E10 establishes a reproducible Debian Bookworm build container
  ([Dockerfile.qemu-bringup](../docker/Dockerfile.qemu-bringup)) and hashes the
  pinned OpenIPC `gk7205v200_lite` artifacts (`BUILD_ID=nightly-20261024-856f22e`,
  `BUILD_SHA=856f22ef7e046f59a3f409e157eb1e06d42fd03c`, `uImage.gk7205v200`
  SHA-256 `da61bc25b6268c444ca5b97bea0b851a60e85a68d3aaf21dea7b5a626fb66e6f`,
  `rootfs.squashfs.gk7205v200` SHA-256
  `2fadbc59b3e9c3ffe3409d671910cb330e2576ff839c5a7c016d4ba9c318145c`) plus the
  PLAF203-patched DTB `uImage` (`1dd7f35bbec84fd8ae988c1cc89ab2eafd135c556a5ba129b6051ad8e64dfbbc`).
  Branch archives and rolling media assets in upstream Buildroot remain mutable inputs.
- **Impact/dependencies:** P0, **Partial**; SRC-01 → BUILD-01 → BOOT-01/EMU-01.
  Physical allocation additionally depends on PLAT-01/REC-01.
- **Next/accept:** Userspace (E14) and kernel/DTB/module (E15) reproducibility are
  verified for the named container/input sets. Freeze/reconstruct the container,
  review dependency security and qualify the intended physical board/driver set.
  Physical RTL8733BU/board qualification follows KABI-01/WIFI-01, not a
  speculative physical GPIO3 requirement inferred from the emulator.
- **Architecture:** A firmware Git SHA alone is not a full input lock. Do not
  call stock `lite` minimal, assume its flash size, or reuse OEM uClibc libraries
  as an unqualified musl ABI drop-in.

### PLAT-01 — actual board and storage map

- **Question/current evidence:** What are detected RAM, flash type/geometry,
  erase semantics, partition offsets, active slot and filesystems? E8 resolves
  the contiguous 32 MiB exposed NOR map: boot 512 KiB, bootargs 512 KiB, kernel
  4 MiB, rootfs 27 MiB; 64 KiB erase/1-byte write; active JFFS2 root. E9
  identifies `mtd0` as a two-stage image. Independent review fixes the later
  offsets/padding claims: gzip begins at `0x4570`, consumes 235,674 bytes,
  expands to 521,048 bytes; the last nonzero extent is 253,654 bytes followed
  by zero padding. The prefix contains SPL/register initialization, not a
  separately established 3,908-byte table boundary. `mtd1` has a single
  256 KiB CRC window, with zero upper-half padding, not a redundant copy.
  OEM software observations corroborate a 64 MiB DDR configuration (`mem=45M`
  Linux at `0x40000000..0x42cfffff` + 19 MiB MMZ at `0x42D00000..0x43ffffff`),
  while physical DDR package markings remain uninspected. E10 verifies 32 MiB
  Macronix `MX25L25635F` (`0xc22019`) NOR emulation with exact PLAF203
  `mtdparts` and `goke_mem` `mem=64M osmem=45M mmz_allocator=gk` (`OS 48M`
  4 MiB-aligned, `16M` MMZ at `0x43000000`) on QEMU `gk7205v200`.
- **Impact/dependencies:** P0, **Partial**; constrains BOOT-01, REC-01,
  physical image layout and rollback, not portable application tests.
- **Next/accept:** Retain the verified 32 MiB NOR and `mtd0`/`mtd1` layout;
  finalize replacement partition/overlay sizing (`SquashFS` + `JFFS2` `rootfs_data`
  vs A/B) and verify physical RAM/MMZ budgets before hardware deployment.
- **Architecture:** NOR layout, single-copy `0x40000` `bootargs` geometry, and
  64/45/19 software memory split are verified; replacement partition layout and
  A/B feasibility remain deliberate design choices.

### BOOT-01 — Linux boot without AF203_FW

- **Question/current evidence:** Which scripts/drivers/actions are essential for
  Linux/network/SSH rather than media or feeding? E2/E8 separate application
  startup from boot evidence. E9 extracts the 12,436-byte appended DTB
  (`ARM_APPENDED_DTB` at `mtd2` offset `0x345000`, `compatible = "goke,gk7205v200"`)
  and proves that both the OEM DTB and upstream `OpenIPC/firmware`
  `gk7205v200` DTB omit `gpio_chip@120b3000` (`gpio3`, GPIO 24–31), while
  upstream OpenIPC also declares `gpio_chip@120b8000` (`gpio8`, failing AMBA
  probe with `-19` in this emulator, not proven nonexistent hardware).
  E9 also reconstructs the four-stage pinmux
  chain (`mtd0` `regbin` → `/etc/init.d/rcS` → `sysconfig.ko` `pin_mux` →
  `AF203_FW` `InitializeBoardBsp`). E10 proves that patching the OpenIPC
  `gk7205v200` DTB to add `gpio_chip@120b3000` and disable `gpio_chip@120b8000`
  boots cleanly on QEMU `gk7205v200` without `AF203_FW`, registering all 8
  `pl061_gpio` banks (`gpiochip0..56`) and exporting `gpio25`/`gpio26` via
  sysfs. E10 also identifies two OpenIPC boot constraints: `gk7205v200.generic.config`
  compiles in `CONFIG_CMDLINE="init=/init"` with `CONFIG_CMDLINE_EXTEND=y`
  (overriding any `-append init=...`), and `/init` skips mounting a writable
  overlay when `root=/dev/ram0`.
- **Impact/dependencies:** P0, **Partial**; needs BUILD-01, and PLAT-01 for the
  physical profile; feeds EMU-01/BSP-01. Version-string equality is not module ABI.
- **Next/accept:** Qualify board pinmux/GPIO initialization from verified OEM
  call sites. The QEMU GPIO25/26 exercise is not a physical board requirement.
  Linux/network/SSH must work without `AF203_FW` or unowned watchdog arming;
  qualify on hardware only under separate authorization.
- **Architecture:** Kernel/U-Boot remain replaceable. Replacement kernels must
  qualify `ARM_APPENDED_DTB` and the actual board banks under stock U-Boot
  (captured stock `bootm 0x41000000`, with no separate FDT address; the
  development fixture deliberately stages its kernel at `0x42000000`).

### REC-01 — backup and restoration feasibility

- **Question/current evidence:** Can wireless transition preserve a complete
  restorable OEM image and identity/calibration, and what survives failed boot?
  E8 acquired `mtd0`–`mtd2` (5 MiB). E9 explains `mtd1` (`bootargs`): U-Boot
  `2016.11` uses `CONFIG_ENV_OFFSET=0x80000`, `CONFIG_ENV_SIZE=0x40000` (256 KiB
  CRC window), and `0x040000..0x07ffff` is `0x00` padding rather than a
  redundant environment copy. Critically, the compiled-in U-Boot default
  environment (decompressed U-Boot `+0x5308a`) has eight generic entries and
  omits both `bootcmd` and `bootargs`. The prior `mtd0 +0x02a09d`, `mem=96M`
  and proven-UART-halt claims are contradicted/unsupported for the verified
  capture and withdrawn. Missing defaults are a recovery risk, not a physical
  failure-injection result or proof that all alternate boot paths fail.
  **Acquisition correction (2026-10-29):** Gemini explicitly withdrew E9's new
  `mtd3`/`full_flash_32MB.bin` claim: the files never existed; the digests and
  associated JFFS2 node count are not evidence. The protected October 28 pass
  acquired only `mtd0`–`mtd2`. **Primary original OEM reference:** the owner
  personally acquired and verified the complete August 18 backup near project
  start, before extensive modification/reverse engineering. It is the earliest
  verified complete backup available and closest available original OEM state;
  its age does not demote its authority for original-firmware comparisons.
  Originals remain at the acquisition location recorded in the reconstruction
  repository's input-custody documentation; a second
  independently verified preservation copy now exists at reconstruction
  `inputs/original/oem-flash-backup-aug18/` (0700/0600, local-only/Git-ignored,
  including original metadata). No acquisition year is inferred from metadata.
  The 32 MiB full image exactly equals all four partition captures; the first
  three match E8. Reconstruction's maintained `inputs/README.md` (primary OEM
  custody), `provenance/replacement-platform-research/device-observations.json`
  (`primary_oem_flash_backup`) and device-contract findings §8 own the full
  identities, offsets and correction history. Both copies remain on the
  development machine; the owner's additional separate-storage copy is planned,
  not confirmed. Integrity and acquisition provenance are substantially
  established, not untouched factory bytes or safe restoration. Coherent
  writable-filesystem restoration, current-state preservation, identity/
  calibration recovery, physical rescue access and a real recovery procedure
  remain open.
- **Impact/dependencies:** P0, **Partial**; needs PLAT-01/BOOT-01;
  constrains destructive installation, data migration and rollback design.
- **Next/accept:** Keep `mtd0` and `mtd1` untouched during initial replacement
  testing whenever possible (staging a fit-in-`mtd2` `uImage` + `mtd3` rootfs
  under the existing `bootcmd` and `mtdparts`), verify `fw_setenv` / `0x40000`
  size parity in simulation before any bootenv write, and validate quiescent
  backup/restoration only under explicit hardware authority.
- **Architecture:** Single-copy `mtd1` without a compiled-in fallback `bootcmd`
  makes `bootargs` mutation high-risk; prefer preserving stock `mtd0`/`mtd1`
  boot contracts for initial wireless transition.

### KABI-01 — kernel configuration and module source/ABI closure

**E15 source-build closure:** Linux 4.9.37 `bb0266a` is built with the pinned SDK
and imported release configuration. The effective configuration, symbols, ELF,
11 modules and reproducible digests are preserved. Required emulated platform
drivers are built in; the old rootfs boots without module files. Integrated
`tun` load/unload verifies the newly built module path, not physical-radio/media
compatibility. Source `drivers/goke/cma/cma.c` aligns `osmem` through
`GOKE_ZONE_ALIGN` before computing the remaining MMZ pool, explaining the observed
45→48 MiB rounding. This does not choose the physical feeder's memory budget.

**Independent refinement:** the retained kernel's early-boot probes accept
`osmem=40M/44M/45M/48M` as 40/44/48/48 MiB OS memory, consistent with 4 MiB
granularity. The current emulation profile is 48/16, not OEM's 45/19. This
narrows the discrepancy to kernel memory policy. E15 now explains that policy
for the accepted source-built equivalent; exact retained-binary provenance and
a deliberate physical/media memory budget remain open. Neither userspace-only
results nor matching kernel release strings establish OEM module ABI parity.

- **Question/current evidence:** Which exact kernel configuration/source and
  symbol interfaces support the selected physical drivers? E8 observes Linux
  4.9.37 and acquires five unstripped `/user` modules (`rtl8733bu.ko` vermagic
  `4.9.37 mod_unload ARMv7 p2v8`, no `__versions` section). E9 extracts the OEM
  kernel's 12,436-byte `ARM_APPENDED_DTB` (`mtd2` `0x345000`) and confirms both
  OEM and upstream OpenIPC `gk7205v200` kernels rely on `ARM_APPENDED_DTB` and
  omit `gpio_chip@120b3000` (`gpio3`); the omission alone is not a proved
  physical-board defect or a requirement to deploy the emulator's workaround.
  E10 verifies upstream OpenIPC's `Linux-4.9.37-gk7205v200` kernel behavior in
  QEMU: the tested combination is `mmz_allocator=gk` plus `mem=64M osmem=45M`
  (bare `mem=45M` is ignored and `mmz_allocator=hisi` triggers two `mm/cma.c`
  `WARN_ON`s), and `CONFIG_CMDLINE="init=/init"` with `CONFIG_CMDLINE_EXTEND=y`
  always appends `init=/init`.
- **Impact/dependencies:** P0, **Partial**; BUILD-01/BOOT-01/WIFI-01 and later
  media depend on a qualified kernel/module set. Does not block portable ARM
  tests or require every OEM module in the minimal image.
- **Next/accept:** The kernel/QEMU DTB/module build is accepted for E15 scope.
  Build and qualify the physical `rtl8733bu` (`86485bef`) and watchdog driver
  against this kernel; verify symbols and behavior, not just vermagic. Qualify a
  physical-board DTS independently of the QEMU GPIO3/GPIO8 accommodations.
- **Architecture:** Build drivers for the selected kernel rather than mixing OEM
  `.ko` binaries into an OpenIPC kernel without ABI verification.

### EMU-01 — actual model and boot compatibility

**E12 initial scope:** the exact original full-flash boot chain now reaches the original
kernel, rootfs, shell and hash-verified application. Repeat short-run execution
is established, not physical boot, normal feeder behavior or stable long-lived
OEM emulation. E13 supersedes the initial QMP-stall/FMC limitations: the runner
now drains UART during QMP waits, and a tracked NOR-only erase fix is tested.
Normal init without the application and one PTY application with emulator-only
watchdog suppression each passed 930 seconds of repeated responsiveness checks.
The fixed binary additionally passes storage integrity and restart tests. Missing
physical peripherals and native watchdog behavior remain limits, not reasons to
expand this reference into another development project.

- **Question/current evidence:** Does the pinned machine boot the exact chosen
  kernel/rootfs or full-image chain? E10 builds upstream QEMU `v10.2.0`
  (`698104725efad4b29079d857dfdebbd804e34c99`) with `OpenIPC/qemu-bench`
  (`b011673b31884683a534a3f3e1a2b080d5a99f19`) in
  [Dockerfile.qemu-bringup](../docker/Dockerfile.qemu-bringup) and executes
  automated qualification via
  [qualify_qemu_gk7205v200.py](../scripts/qualify_qemu_gk7205v200.py):
  1. Both `gk7205v200` (`Goke GK7205V200 (Cortex-A7, ~Hi3516EV200)`) and
     `hi3516ev200` machines register and boot OpenIPC `uImage.gk7205v200` +
     `rootfs.squashfs.gk7205v200` (`BUILD_ID=nightly-20261024-856f22e`) to
     `openipc-gk7205v200 login:`.
  2. Baseline boot (`26.41s`) exposes two upstream mismatches: `qemu-bench`'s
     default `mem=32M mmz_allocator=hisi mmz=anonymous,0,0x42000000,32M` causes
     two `mm/cma.c` `WARN_ON`s in `goke_mem`, and the stock DTB omits `gpio3`
     (`120b3000`) while probing nonexistent `gpio8` (`120b8000`, AMBA `-19`).
  3. With the PLAF203 DTB fixup (`uImage-plaf203-dtb.gk7205v200`),
     `mem=64M osmem=45M mmz_allocator=gk`, and 32 MiB Macronix `MX25L25635F`
     (`-global driver=hisi-fmc,property=flash-jedec,value=0xc22019` + PLAF203
     `mtdparts`), the guest boots with **zero** `mm/cma.c` warnings (`dram=64`,
     `osmem=48`, `mmz=anonymous,0,0x43000000,16M`, `MemTotal: 43148 kB`),
     detects `mx25l25635f (Chipsize 32 Mbytes, Blocksize 64KiB)` with all four
     PLAF203 MTD partitions, registers all 8 `pl061_gpio` banks (`gpiochip0..56`,
     including `120b3000.gpio_chip` `gpio3`), exports/reads `gpio25`/`gpio26` via
     sysfs, registers `120e0000.rtc`, and runs at `1.0004x` real-time clock ratio.
  4. Modeled vs unmodeled boundaries (`-d unimp,guest_errors`): `hisi-fmc` logs
     harmless `NOR unhandled SPI command 0xe9` (`EX4B`), `dwc3` USB gadget fails
     probe, and PLAF203 feeder peripherals (`AW9523B` on I2C2 `0x5b`, `SAR_ADC`
     `0x120e0000`, USB `RTL8733BU`) are not modeled in upstream `qemu-bench`.
- **Impact/dependencies:** P0, **Partial**; core SoC/kernel/DTB/GPIO3/NOR/RTC/timer
  emulation and flash-root boot are qualified within E11/E12 scope. Synthetic
  feeder harnesses and physical compatibility remain unqualified.
- **Next/accept:** Retain E11's replacement flash lifecycle regressions; use E12
  only for bounded OEM comparisons. Add synthetic HAL backends when required by
  independent feeder logic, not to reproduce all OEM peripherals first.
- **Architecture:** `gk7205v200` in `qemu-bench` is a verified SoC kernel/driver/DTB
  qualification target, while feeder actuation/sensing still requires explicit
  HAL test backends and physical qualification.

### EMU-02 — network, authenticated SSH and lifecycle

E12/E13's OEM reference intentionally has **no network backend**, SSH qualification
or reusable production credentials. Its watchdog resets/control-channel stalls
do not invalidate E11's distinct replacement-image lifecycle results. E13's
same-flash cold-restart proof applies to its unique test payload/deletion and
does not replace installation/rollback or physical recovery qualification. Do not
transfer either profile's security or persistence claims to the other.

**Independent E11 qualification:** the [minimal profile](../firmware/plaf203/README.md)
now boots from a new 32 MiB NOR image through the captured SPL/U-Boot and
synthetic CRC-valid environment, with no `-kernel`, `-append` or `-initrd`.
Three cold boots and software reset retain JFFS2 state and pinned SSH host
identity. Guest probes verify read-only rootfs, DHCP, port-22-only TCP listeners,
authorized-key success, and rejection of unauthorized keys/password methods.
Invalid state schema and missing/malformed enrollment fail closed. Corrupting
only the synthetic environment CRC reaches the captured U-Boot's warning and
monitor boundary. These are **qualified emulation results**, not physical
recovery, flash endurance, production enrollment or rollback qualification;
EMU-02 remains **Partial** for those broader requirements. The development
layout (512K boot / 512K env / 4M kernel / 16M rootfs / 11M state) is not an
installer decision. Machine-level flash-file's >16M NAND heuristic and the
model's SquashFS write protection require care in later update/failure tests.

- **Question/current evidence:** Can the guest network, authenticate SSH, persist
  intended state and reboot/recover repeatably? E10 qualifies virtual networking,
  key-only SSH, unauthorized rejection, and hardware reboot on `gk7205v200` via
  [qualify_qemu_gk7205v200.py](../scripts/qualify_qemu_gk7205v200.py):
  1. `femac` (`10040000.ethernet`) acquires `10.0.2.15/24` and default route
     `10.0.2.2` via `udhcpc` over QEMU user-mode SLIRP (`hostfwd=tcp:127.0.0.1:<port>-:22`).
  2. Stock OpenIPC's `S50dropbear` defaults to `-R -B` (blank-password root login),
      sets root's shell to `/usr/sbin/openipc-claim`, and on a read-only
     `root=/dev/ram0` SquashFS boot `S01seedrng` fails (`can't create directory
     '/var/lib/seedrng': Read-only file system`), leaving the Linux 4.9.37 CRNG
     unseeded so `getrandom()` blocks indefinitely.
  3. Configuring `/etc/default/seedrng` with a writable `/tmp/seedrng/seed.credit`
     (256-bit seed), pre-generating `/tmp/dropbear_ed25519_host_key`, replacing
     `-B` with key-only `-s -g`, and provisioning `/etc/dropbear/authorized_keys`
     (`0600`) yields: unauthorized `ed25519` key rejected (`exit 255`), password
     auth rejected (`exit 255`), and authorized `ed25519` root SSH command
     execution succeeding (`exit 0`).
  4. Issuing `reboot -f` over SSH triggers `hisi-sysctl: SC_SYSRES reset
     triggered (val=0xdeadbeef)` and immediate guest reboot (`Restarting system` →
     `Booting Linux on physical CPU 0x0`).
- **Impact/dependencies:** P0, **Partial**; E11 qualifies the minimal image,
  genuine NOR boot, persistent state and SSH identity across cold/software
  resets, and fail-closed state/enrollment cases. Physical behavior and
  production trust/update/recovery remain separate gates.
- **Next/accept:** Extend bounded fault coverage to interrupted enrollment,
  power-loss/state writes and update recovery after accounting for the model's
  SquashFS write protection. Close production entropy/enrollment policy before
  calling this a deployable device image.
- **Architecture:** Stock OpenIPC's blank-password (`-B`) and `openipc-claim`
  defaults must be replaced in the PLAF203 image profile; headless RAMdisk or
  read-only root boots must also ensure early CRNG seeding before `dropbear`.

### WIFI-01 — physical networking and Linux facilities

- **Question/current evidence:** What bus/modalias/driver/firmware/init is actually
  used? E8 resolves USB `0bda:b733` bound to rtl8733bu, matching recovered module
  alias and embedded firmware arrays (`cfg80211` dependency). OpenIPC's
  `rtl8733bu` package has an exact USB-ID source match at Realtek commit
  `86485bef`. E9 recovers the SoC-side USB PHY/clock enable in
  `/vendor/platform/sysconfig.ko:0001023c` (`usb_pin_mux`, writing `0xb7` to
  `reg_crg_Base + 0x140`, i.e., `0x12010140`) alongside the board-level
  `AW9523B` port 0 pin 5 HIGH write in `/application/AF203_FW:0005657c`
  (`InitializeBoardBsp`).
- **Impact/dependencies:** P0, **Partial**; needs BOOT-01/KABI-01/BSP-01;
  physical network/SSH/provisioning depend on it.
- **Next/accept:** Ensure replacement boot/init performs both the SoC USB CRG
  initialization (`0x12010140 = 0xb7` or equivalent kernel USB driver init) and
  `AW9523B` output 5 HIGH before enumerating USB `0bda:b733`, build `rtl8733bu`
  against the target kernel, and qualify association/reconnect on hardware under
  separate authorization.
- **Architecture:** Favor standard Linux networking (`wpa_supplicant`/`udhcpc`),
  with explicit board power/clock prerequisites documented rather than hidden
  inside `sysconfig.ko` or `AF203_FW`.

### TRUST-01 — first access and software trust

- **Question/current evidence:** How are authenticated management/SSH, identity
  custody and independent update trust established? E6 provides key-only bootstrap
  and signed agent-update invariants, not a whole-image updater. E1 includes
  Dropbear, not a qualified replacement enrollment/security profile.
- **Impact/dependencies:** P0, **Partial**; EMU-02 needs test identity policy;
  hardware enrollment depends on WIFI-01/REC-01; native API follows.
- **Next/accept:** Define credential/threat lifecycle, protected first enrollment,
  rejection tests, rotation/revocation/recovery and immutable signed-artifact
  compatibility policy. Select mechanisms against footprint/offline-clock needs;
  do not weaken authentication to make a boot test pass.
- **Architecture:** Host browser-auth boundaries do not authenticate a separate
  device. SSH, API authentication and image signing have different purposes.

## Essential control and persistent operation

### BSP-01 — safe pinmux and shared hardware access

- **Question/current evidence:** What independent GPIO/AW9523B/bus initialization
  and ownership is required? E0/E2 recover `/application/AF203_FW:0005657c`
  (`InitializeBoardBsp`), roles and reset/writes. Independent native Ghidra
  review verifies `/vendor/platform/sysconfig.ko:00011980` (`sysconfig_init`)
  and `000113c0` (`pinmux`): demo-board amp unmute selects `0x1000` at
  `0x120c0000`, whereas `/application/AF203_FW:00056164` restores LSADC mux
  `0x1001` at `0x120c0000/+4`. `InitializeLocalKeyGpios:00055f00` configures
  bank 1 bit 4 (GPIO12) using `0x120c0010 = 0x1002`, plus GPIO59/69/70.
  `InitializeDetectionGpios:0005606c` configures input GPIO52/4/13/16,
  including `0x120c0014 = 0x1802`. The prior `pin_mux:00010538`,
  `gk7205v200_pin_cfg`, GPIO25/26 button/mute, `0x1100`, I2C0 and
  `system(devmem)` chain was not supported by these matching binaries and is
  withdrawn. Maintained OEM evidence places the expander on I2C2.
  E10's GPIO25/26 exports demonstrate emulation only. Disabling bank 8 fixes
  a QEMU probe failure but omits a bank used by real OEM keys.
- **Impact/dependencies:** P1, **Partial**; needs BOOT-01/PLAT-01; feeds
  FEED-01/ADC-01/POWER-01.
- **Next/accept:** Implement verified board pinmux/I2C2/USB ownership without
  replaying demo-board module side effects; separately qualify safe
  physical output levels under an explicitly authorized hardware test plan.
- **Architecture:** Reuse verified pad/GPIO/AW9523B mappings, qualify actual
  board banks independently of QEMU, serialize shared access, and avoid `system("devmem ...")`
   inside application code.

### FEED-01 — portions, sequencing and two bowls

- **Question/current evidence:** Which transitions establish a portion, dual-bowl
  sequence, jam limits and safe stop? E3/E4 cover callbacks/state machine,
  retained nonconsecutive current hits and nominal delays. The host model uses
  synthetic portions/timings, not measured food or electrical limits.
- **Impact/dependencies:** P1, **Partial**; needs BSP-01/ADC-01/POWER-01;
  schedule/API requests go to this single owner, not directly to hardware.
- **Next/accept:** Build the non-actuating state machine and durable journal;
  investigate binary contracts only when an implementation dependency needs it.
  Correlate controlled physical evidence later. Establish counting, bounded motion,
  stale-work rejection, jam/cancel and uncertainty behavior; do not adopt nominal
  OEM delays as proven safety limits.
- **Architecture:** Output-write success, stopped observation, mechanism cycle
  and guaranteed food mass are different facts.

**E16 implementation boundary:** The permanent native health owner and injected
non-actuating control/sensor interfaces now exist. Generation/sequence rejection,
stale-loop detection, fault latching and restart/shutdown are executable tests,
not physical feeding qualification. Next implement a durable operation journal
and synthetic single-owner feeder state machine against these boundaries;
physical motion remains disabled pending BSP/ADC/power/watchdog qualification.
**E17 supersedes the implementation backlog above:** The native single owner
now has durable manual/synthetic scheduled-origin admission, per-deadline tokens,
bounded jam retries, cancellation with stopped-observation verification, and
explicit failed/indeterminate results. [Contracts and durability rationale](../firmware/plaf203/README.md#durable-feeding-operations-synthetic-only)
and the [feeding checkpoint](../firmware/plaf203/feeding-qualification-20261029.json)
own implementation evidence. This closes synthetic operation semantics only;
physical portion/dual-bowl/ADC/output behavior remains unqualified.

### ADC-01 — acquisition and sensing calibration

- **Question/current evidence:** How do shared ownership, errors, range/freshness
  and conversions become reliable inputs? E2/E3 restore ioctl/release contracts
  and unsigned-read-failure/zero-on-setup-error paths. Calibration is unmeasured.
- **Impact/dependencies:** P1, **Partial**; needs BSP-01; feeds FEED-01/POWER-01.
  The matching original ADC module is present, not missing.
- **Next/accept:** Verify only outstanding conversion boundaries in Ghidra, then
  qualify acquisition validity/range/freshness and electrical calibration
  separately. Read failure must never become a valid zero/default sample.
- **Architecture:** One serialized acquisition owner; OEM thresholds are evidence,
  not automatically independently safe replacement limits.

### WDT-01 — watchdog ownership and fault response

**E15 historical configuration:** the upstream input and that checkpoint have
`CONFIG_WATCHDOG=n`; it was not disabled to pass emulation. E16 adds an explicit
capability fragment (framework/sysfs/nowayout and standard SP805 module) and the
[native non-actuating owner](../firmware/plaf203/README.md#native-runtime-and-health-ownership).
The [runtime checkpoint](../firmware/plaf203/runtime-qualification-20261029.json)
records host/ARM health tests and NOR regressions. Default feeds remain simulated.
No reset suppression or QEMU model change is introduced in replacement tests.
Linux watchdog-device/reset coverage remains **unqualified**, not resolved by a
passing synthetic health policy.

- **Question/current evidence:** Who owns health from module init through shutdown,
  and what reset response is safe? E0/E2/E8 establish enable-before-open,
  close-not-disarm, service-mode transition (mode 1 kernel thread vs mode 2
  exclusive userspace owner), and OEM audio coupling. E9 verifies live
  `/sys/module/gk7205v200_wdt/parameters/` (`default_margin`, `nodeamon`, with
  `perm=0`, matching the OEM `insmod gk7205v200_wdt.ko default_margin=30 nodeamon=1`
  invocation). No actual hardware timeout/reset was triggered on the live feeder.
- **Impact/dependencies:** P1, **Partial**, startup risk also relevant to BOOT-01;
  needs selected kernel/driver and BSP-01/FEED-01.
- **Correction / next / accept:** The former SP805-at-`0x12050000` assertion is
  unsupported by the pinned source. `gk7205v200.dtsi` describes `goke,wdg` at
  `0x12030000`; its `arm,sp805` timer label at `0x12001000` is a different device.
  QEMU models CMSDK ID `0x001bb824`, not the SP805 AMBA ID `0x00141805`. The
  standard module's loadability does not resolve this binding mismatch. Preserve
  the boundary rather than adding fake identity overrides. Qualify a supported
  driver/binding/clock/reset/nowayout combination under separate hardware
  authority before relying on `/dev/watchdog`. Never arm a physical watchdog
  before the essential-health owner and reset/recovery policy are qualified.
- **Architecture:** Essential health must be independent of media. Blind petting
  or an automatic restart loop is not a safety policy.

### POWER-01 — DC/battery and restart/shutdown

- **Question/current evidence:** What is powered/retained across brownout, battery
  transitions and reset? E0 disproves older direct-reboot/suspend claims; flags,
  media teardown and software motor decisions are known. Electrical retention,
  wake/reset and calibrated limits are not established.
- **Impact/dependencies:** P1, **Partial**; needs ADC-01/BSP-01/PLAT-01;
  constrains recovery, feeding and offline scheduling.
- **Next/accept:** Reconcile outstanding software paths, then propose bounded
  physical tests for approval. No power disconnect, reboot, low-battery experiment
  or GPIO action is authorized now.
- **Architecture:** Keep software policy separate from electrical proof; a
  condition wait is not Linux suspend and reset does not prove a safe motor.

### STATE-01 — durability and interrupted physical operations

- **Question/current evidence:** Which state must survive corruption/write failure,
  schema change or interrupted dispensing? E0/E3 provide layouts, startup bound
  defects and tested shape/control models, not OEM multi-file atomicity or
  exactly-once physical execution. OEM packed storage is not a replacement mandate.
- **Impact/dependencies:** P1, **Partial**; needs PLAT-01/REC-01/FEED-01;
  informs SCHED-01/API-01 and rollback compatibility.
- **E17 implementation:** Versioned CRC-protected 4 KiB snapshot, 32-byte
  required marker, durable epoch/highwater, 16 retained outcomes, file sync →
  rename → directory sync → readback. Fault injection covers every commit barrier,
  effect/result windows, cancellation/recovery, corrupt schemas and storage errors.
  No incomplete effect-bearing operation resumes or becomes success on restart.
- **Next/accept:** Physical power-loss, old-kernel flush/error propagation,
  rollback/restore identity and operator reconciliation remain open. A trusted
  old snapshot is not detectable rollback without an independent anchor. No
  claim of exactly-once physical feeding or proven flash endurance follows from
  process-kill or emulated NOR tests. Shape-valid bytes alone cannot authorize feeding.
- **Architecture:** Human-readable config remains preferred; choose journaling
  for consistency/flash-wear requirements, not a format ideology.

### SCHED-01 — offline time and schedule execution

- **Question/current evidence:** What establishes valid time after power loss and
  how are DST/time jumps/missed occurrences/reboot handled? E3/E4 identify exact-
  second scheduling hazards; monotonic deadlines and durable occurrence identity
  are design requirements. E8 observes RTC `120e0000.rtc`, `hctosys=1`. E9
  verifies live `/dev/rtc0` (`crw------- 254, 0`) and `/proc/driver/rtc`
  alongside the 16-byte persistent fallback `/user/data/rtc/rtc_time.bin`
  (`RtcSavedWallClock`, managed by `/application/AF203_FW:0003f260`
  `RtcInitAndSyncSystemTime`, `0003eedc` `RtcWriteHardwareTime`, `0003f030`
  `RtcSaveTimeFile`, and `0003f0fc` `RtcPeriodicSyncWorker`). When `HI_RTC_RD_TIME`
  returns `year <= 0x7DF` (`2015`, unbacked power-on reset default), `AF203_FW`
  restores `/user/data/rtc/rtc_time.bin` (if `year > 2015`) into both system
  clock and `/dev/rtc0`, then re-saves `/user/data/rtc/rtc_time.bin` every 60 s.
  A recent timestamp in `rtc_time.bin` proves software persistence while running,
  not hardware RTC battery-backed retention across total power loss. E10 verifies
  `goke_rtc 120e0000.rtc` probe and `1.0004x` real-time guest timer rate on QEMU
  `gk7205v200`.
- **Impact/dependencies:** P1, **Partial**; needs STATE-01/FEED-01/POWER-01.
- **Next/accept:** Implement a monotonic-floor wall-clock persistence seed plus
  explicit degraded-time status when neither NTP nor verified RTC validity is
  established after power loss; test reboot/offline/time-jump cases in QEMU.
- **Architecture:** Offline feeding is firm; uncertain calendar time needs a
  defined degraded policy rather than an assumed battery-backed RTC.

### API-01 — native commands and autonomous events

- **Question/current evidence:** What schemas/revisions/ordering/correlation expose
  actual device outcomes? Architecture/E3/E6 distinguish intent, acceptance,
  execution, verification and indeterminate results. Exhaustive OEM MQTT recovery
  is not a prerequisite for the independent contract.
- **Impact/dependencies:** P1, **Design deferred**; needs FEED-01/STATE-01/TRUST-01.
  Minimal Linux/network/SSH does not require the complete control API.
- **Next/accept:** Define both sides, conflicting key reuse, epoch/gap handling,
  reconnect snapshot/event watermarks, backpressure and safe reconciliation;
  prove with synthetic and later integration tests, not message counts.
- **Architecture:** HTTP/JSON + WebSocket remains a candidate. Duplicate-message
  protection is not an exactly-once physical guarantee.

## Later capabilities and integration

### BLE-01 — transport and provisioning role

- **Question/current evidence:** Which Bluetooth transport/driver/firmware is
  available and is BLE preferable to AP/another secure recovery path? E0/E4
  recover GATT/frame/mailbox behavior, not the complete HCI hardware/init chain.
  The status worker clears readiness on status change; it is not a producer.
  E8 recovers blue_star.sh loading bluetooth.ko/rtk_btusb.ko and using hciconfig:
  a USB initialization lead, not a UART assumption. No HCI class entry was
  observed; absence of Bluetooth hardware is not established.
- **Impact/dependencies:** P2, **Partial**; needs WIFI-01/BOOT-01 observations and
  TRUST-01 for a secure role decision.
- **Next/accept:** Establish actual transport/init dependencies, compare secure
  onboarding options and measured target cost before implementation.
- **Architecture:** OEM BLE interoperability is not itself a product requirement.

### MEDIA-01 — GC2053/ISP, hardware video and audio

- **Question/current evidence:** Which exact sensor/module/ABI/calibration/codec
  lifecycle permits standard media without feeding interference? E0/E4/E5 preserve
  callbacks, the 0x300 ISP-default envelope, 19,984 calibration-envelope bytes,
  matching codec/SDK/module inputs. E7 tests session/keyframe/timestamp behavior,
  not replacement capture. E9 recovers the internal Goke audio codec register/ioctl
  contract across `/vendor/kernel/gk7205v200_acodec.ko` (`0x100c0000` `acodec_Step`,
  `000105a4` `acodec_DriverIoctl`, `0001164c` `acodec_SoftReset`) and
  `/application/AF203_FW` (`00019b74` `AudioCodecSetInputVolume`, `0002d0d4`
  `AudioCodecConfigureGoke`, `00019fc0` `AudioSetOutputEnableAndGain`),
  connecting internal `/dev/acodec` configuration with the board-level mute switch
  on `GPIO3_2` (GPIO 26) and external speaker amplifier enable on `AW9523B` port 0
  pin 4. `GK_API_*` video/audio wrappers still lack verified source-built providers.
- **Impact/dependencies:** P2, **Partial**; needs BUILD-01/BOOT-01/BSP-01/POWER-01
  and qualified budgets. Not a prerequisite for minimal SSH.
- **Next/accept:** Qualify Divinus / OpenHisilicon against the recovered GC2053
  and `/dev/acodec` + `AW9523B` pin 4 / `GPIO3_2` contracts after the minimal
  Linux/network/SSH and feeding foundations are complete.
- **Architecture:** Divinus remains preferred; OpenHisilicon may support another
  layer rather than be mutually exclusive. No need to recreate TUTK or assume
  software encoding. Matching codec inputs are recovered, not missing.

### HA-01 — native adaptation and verified cutover

- **Question/current evidence:** Which adapter/parity proof preserves HA identity,
  availability and operation semantics without competing integration writers?
  E6 provides projections/jobs/worker/AppDaemon authority, not a finished native
  adapter; E7 remains a separate media owner.
- **Impact/dependencies:** P2, **Design deferred**; needs API-01/TRUST-01/STATE-01,
  plus REC-01 for deployment/rollback. No frontend redesign is implied.
- **Next/accept:** Define native sources behind existing contracts; compare entity/
  operation behavior, then qualify identity binding, quiesced legacy writers,
  state reconciliation and fallback. Preserve one-feeder deployment scope.
- **Architecture:** Removing device MQTT does not force removing HA-side MQTT.
  Autonomous device events do not need a browser job to occur.

## Next bounded investigation and change log

Next: implement the first authenticated native API adapter over the E17 owner,
preserving admission/result/uncertainty/revision semantics and adding secure
event/state reconciliation. Do not enable physical outputs. The durable journal
and synthetic state machine now exist; standard driver binding remains limited as recorded
in WDT-01. Separately qualify the physical-board driver/DTS set (KABI-01 /
WIFI-01 / BSP-01) under appropriate authority. E14 closes independent userspace
builds; E15 closes the source-built equivalent kernel/QEMU DTB/modules. Keep the accepted
SPI-NOR lifecycle tests as regression gates. Qualify a physical-board DTB rather
than deploying the emulator's GPIO-bank workaround; decide the Linux/MMZ budget
deliberately before media bring-up. Qualify backup consistency and restoration
separately; the claimed newer acquisition was withdrawn, while the primary
owner-acquired original OEM backup and its second local preservation copy have
verified identities/assembly. Separate-storage redundancy is planned. Production
entropy/enrollment, Wi-Fi, installation/rollback
and physical hardware remain separate gates requiring explicit authorization.

Routine OEM groups: exact identity/current-state checks → investigation → bounded
supported annotations → affected readback/basic integrity → explicit save →
research/register update. Record newer saved work versus the last exported
checkpoint. Full exports, regeneration, exhaustive comparisons and validated
archives belong at deliberate milestones or warranted risk, not every session;
follow the [reconstruction workflow](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/bbd4f113738f4e43f572babc3cbd047fb88122e5/DEVELOPMENT.md#analysis-preservation-and-milestone-cadence).
Native MCP remains the interactive analysis path; unattended existing exporters
handle milestone batches serially. No separate synchronization framework.

| Date | Change / evidence | Remaining boundary |
| --- | --- | --- |
| 2026-10-29 | E13 preserves successful 930-second responsiveness receipts, fixes UART/QMP multiplexing, tracks/tests the NOR `0xdc` fix, and qualifies original-app storage readback, erase activity and cold-restart persistence. Interactive/manual input is isolated from automated probes. | Emulator watchdog suppression is explicit; no full feeder/media/network/physical or exhaustive GC/recovery qualification. Return to source-built replacement work. |
| 2026-10-29 | E14 locks and records native userspace inputs; two fresh source/SDK/build trees produce identical rootfs and receipts offline. The rebuilt image passes the existing NOR lifecycle/security suite. Private-key mode checks and bounded/root-owned negative fixtures correct host qualification assumptions. | BUILD-01 remains Partial for kernel/bootloader source, container reconstruction and dependency security. No actuation, physical testing or production provisioning. |
| 2026-10-29 | E15 builds pinned Linux 4.9.37, source QEMU DTB and 11 modules through Buildroot. Two independent offline kernel builds match, and full NOR/security suites pass with previous and integrated userspace. | No retained kernel/DTB build dependency. OEM bootloader, physical board/radio, watchdog ownership, dependency security and container reconstruction remain gates. |
| 2026-10-29 | E12 boots disposable original full flash without component substitution; shell verifies BusyBox/JFFS2 and original running application hash. Reuses existing QEMU/container and preserves isolated runnable reference. | Watchdog resets, QMP responsiveness and missing peripherals limit sustained use. No network/feeding/media/physical qualification or emulator patch. |
| 2026-10-29 | Owner clarified the August 18 collection's primary OEM provenance; all five original images and metadata copied independently to reconstruction's private, Git-ignored `inputs/` location and verified against original hashes/partition boundaries. REC-01 references that inventory rather than duplicating it. | Additional separate-storage copy is planned, not confirmed. Filesystem consistency, current-state preservation and real rescue/restoration remain open. |
| 2026-10-29 | Gemini withdrew the nonexistent newer `mtd3`/full-image claim. Independently hashed the older August 18 files and verified that the 32 MiB image exactly equals all four partition files; REC-01 now uses that real baseline. | Acquisition coherence, current-state parity and restoration remain unproved. No physical operation or Ghidra mutation. |
| 2026-10-28 | Independent E11 review fixes the conflicting OEM container/default-environment claims, unsupported GPIO25/26/I2C0 assumptions and Ghidra codec/detection annotations; builds minimal userspace and qualifies actual NOR/SPL/U-Boot/kernel/rootfs boot, persistence, SSH policy, software reset and negative cases. | Kernel source/fresh-cache closure, production trust/update/recovery and physical board qualification remain open. The acquisition custody question was resolved by the October 29 withdrawal/correction above. Earlier E9/E10 rows are historical reports, qualified/superseded where stated. |
| 2026-10-28 | Initial 20 entries; SRC-01 closed only for source-selection scope using E1. Existing contracts reused rather than reopened. | OEM/Ghidra and SSH deferred by owner; no build/boot, firmware implementation or physical qualification. |
| 2026-10-28 | Access resumed; E8 closes live map/radio/RTC-interface subquestions, narrows boot/recovery and adds KABI-01 (21 retained entries). Bounded saved Ghidra findings and a deliberate exported/restore-verified checkpoint are preserved. | Independent boot, complete backup/restoration, physical DDR/driver/radio/time/safety qualification and actual QEMU execution remain open. Routine saves no longer require full synchronization. |
| 2026-10-28 | Reconciled E9 (two-stage `mtd0` bootloader, single-copy `0x40000` `mtd1` + missing-`bootcmd` default-env risk, 12,436-byte appended OEM DTB `gpio3` defect, four-stage pinmux chain, `/dev/rtc0` vs `rtc_time.bin` fallback, `gk7205v200_wdt.ko` `perm=0`, `gk7205v200_acodec.ko`, live `mtd3`/32 MiB flash backup digests, and saved Ghidra annotations) and executed E10 (reproducible QEMU `v10.2.0` + `OpenIPC/qemu-bench` `gk7205v200` build, DTB `gpio3` patch, 32 MiB `mx25l25635f` NOR, `goke_mem` `mem=64M osmem=45M mmz_allocator=gk`, SLIRP networking, key-only Dropbear SSH + unauthorized rejection, `gpio25`/`gpio26` sysfs export, `120e0000.rtc`, `1.0004x` timer rate, and `SC_SYSRES` reboot). Narrowed EMU-02 from Open to Partial. | Clean source-built PLAF203 Buildroot profile, persistent `mtdblock3`/`rootfs_data` NOR flash boot in QEMU, quiescent backup/restoration verification, and hardware qualification remain open. |
