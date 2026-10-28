# PLAF203 replacement firmware — living research-gap register

**Updated:** 2026-10-28 · **Architecture:**
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
  These are committed verification records, **not fresh live Ghidra inspection**.
- Camera reference: standalone go2rtc `petlibro` at `6ca2e665`, not automatically
  equivalent to the management base's vendored snapshot. Local edits are preserved.
- **Owner-directed hold:** new OEM/Ghidra analysis and SSH validation are deferred
  to the next turn. No device observation command authenticated; no Ghidra
  change/save/export occurred. No emulation, flashing or firmware implementation
  was performed in this initial pass.

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

E0–E5 may require authorized access to private evidence. Old revision-specific
prose is not a current absence claim: runtime providers and codec are recovered.

## Priority and dependency view

| ID | Subsystem / question scope | Priority | Status |
| --- | --- | --- | --- |
| SRC-01 | Concrete upstream source/configuration starting point | P0 | Resolved for scope |
| BUILD-01 | Minimal profile and complete immutable input closure | P0 | Partial |
| PLAT-01 | Actual RAM/flash/partition inventory | P0 | Access deferred |
| BOOT-01 | OEM-independent boot/init dependencies | P0 | Partial |
| REC-01 | Complete off-device backup and meaningful restoration | P0 | Access deferred |
| EMU-01 | Pinned GK7205V200 machine and exact boot artifacts | P0 | Partial |
| EMU-02 | Emulated network, secure SSH and reboot/persistence | P0 | Open |
| WIFI-01 | Physical radio bus/driver/firmware/initialization | P0 | Access deferred |
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
  Thirteen files match pinned Git objects. The toolchain digest is publisher
  metadata, not an independently hashed local download.
- **Impact/dependencies:** Enables BUILD-01/EMU-01 source selection. No physical
  compatibility, reproducible output or boot is established by this entry.
- **Acceptance/status:** **Resolved for scope**: exact retrievable revisions,
  paths/settings recorded. Reopen on changed/unavailable inputs.
- **Architecture:** OpenIPC has concrete starting inputs; no evidence yet
  requires changing platform or making kernel modernization a prerequisite.

### BUILD-01 — minimal profile and immutable input closure

- **Question/current evidence:** Which exact package/config/input set is our
  reproducible minimal platform? E1 identifies stock `lite`, musl and a Linux
  4.9.37 config, but it also selects camera/web UI/motors and an 8 MiB image
  setting. Branch archives and rolling media assets remain mutable inputs.
- **Impact/dependencies:** P0, **Partial**; SRC-01 → BUILD-01 → BOOT-01/EMU-01.
  Physical allocation additionally depends on PLAT-01/REC-01.
- **Next/accept:** Identify retained dependency closure and artifact hashes,
  verify acquisition, then qualify a clean build and repeatability. A later
  implementation assignment owns the PLAF203 package profile, not speculative
  configuration committed merely to close this entry.
- **Architecture:** A firmware Git SHA alone is not a full input lock. Do not
  call stock `lite` minimal, assume its flash size, or reuse OEM uClibc libraries
  as an unqualified musl ABI drop-in.

### PLAT-01 — actual board and storage map

- **Question/current evidence:** What are detected RAM, flash type/geometry,
  erase semantics, partition offsets, active slot and filesystems? E2/E5 provide
  leads, not a complete live map. Captured loader 64/45/19 MiB differs from
  QEMU 64/32/32; upstream image-size selection is not a board observation.
- **Impact/dependencies:** P0, **Access deferred**; constrains BOOT-01, REC-01,
  physical image layout and rollback, not portable application tests.
- **Next/accept:** Allowlisted read-only `/proc`/sysfs and startup metadata;
  validate coverage/overlap and provenance. No casual MMIO or hardware probing.
  Keep identity/raw captures outside Git. Owner will supply dedicated SSH access.
- **Architecture:** No physical RAM budget, NAND/NOR choice, partition plan or
  A/B fit is approved without the corresponding evidence.

### BOOT-01 — Linux boot without AF203_FW

- **Question/current evidence:** Which scripts/drivers/actions are essential for
  Linux/network/SSH rather than media or feeding? E2 separates application
  startup from boot evidence. E1 has devtmpfs/MTD and module/network/SSH init
  sources; captured media loaders are not the whole OEM boot chain.
- **Impact/dependencies:** P0, **Partial**; needs BUILD-01, and PLAT-01 for the
  physical profile; feeds EMU-01/BSP-01. Version-string equality is not module ABI.
- **Next/accept:** Read-only boot/init/mount/module parameters; inspect only
  unresolved cross-program dependencies when Ghidra resumes. Qualify exact
  AF203-independent boot without motor writes or an unowned watchdog, separately
  in emulation and on hardware with authorization.
- **Architecture:** Kernel/U-Boot remain replaceable. Neither wholesale OEM init
  reuse nor bootloader replacement is justified merely by a matching SoC name.

### REC-01 — backup and restoration feasibility

- **Question/current evidence:** Can wireless transition preserve a complete
  restorable OEM image and identity/calibration, and what survives failed boot?
  E6's slot restoration is valuable, not whole-flash backup/rescue proof.
- **Impact/dependencies:** P0, **Access deferred**; needs PLAT-01/BOOT-01;
  constrains destructive installation, data migration and rollback design.
- **Next/accept:** Define read-only acquisition/coverage first, obtain device-bound
  off-device manifests and independently checked hashes; validate restoration
  later under explicit authority. Include bootenv and ECC/bad-block information
  where applicable. No flash/bootenv writes or reboot are authorized now.
- **Architecture:** Wireless normal installation stays firm; do not promise
  wireless recovery from every bootloader failure or choose A/B before sizing.

### EMU-01 — actual model and boot compatibility

- **Question/current evidence:** Does the pinned machine boot the exact chosen
  kernel/rootfs or full-image chain? E1 verifies registration, QEMU v10.2.0 setup,
  imx307/model-memory defaults, but no local build/boot. Quick-start claims versus
  empty boot-tested table cells are not independent proof.
- **Impact/dependencies:** P0, **Partial**; needs SRC-01/BUILD-01. PLAT-01 is
  necessary before labeling synthetic defaults a PLAF203 board configuration.
- **Next/accept:** Review/build pinned emulator, record base/patch/compiler and
  artifact hashes, boot isolated exact artifacts and capture kernel/init/effective
  arguments and unsupported devices. No physical storage/device passthrough.
- **Architecture:** QEMU is foundational. Generic `virt` remains a separate
  portable profile, not SoC or physical flash qualification.

### EMU-02 — network, authenticated SSH and lifecycle

- **Question/current evidence:** Can the guest network, authenticate SSH, persist
  intended state and reboot/recover repeatably? E1 has FEMAC/config/init sources,
  not tested DHCP/SSH; upstream defaults are not the product security policy.
- **Impact/dependencies:** P0, **Open**; needs EMU-01/BUILD-01/TRUST-01.
- **Next/accept:** Bounded virtual networking and explicit exposure, injected
  protected test keys, successful authorized and rejected unauthorized access,
  service restart, persistent/volatile state and reboot evidence. Record synthetic
  interfaces. This does not qualify physical Wi-Fi.
- **Architecture:** First executable qualification milestone, not a claim that
  this repository already contains a replacement image.

### WIFI-01 — physical networking and Linux facilities

- **Question/current evidence:** What bus/modalias/driver/firmware/init is actually
  used? E5 has `rtl8723fu` blobs; E1 selects MT7601U in stock `lite`. Neither
  proves the installed radio. Named OEM station functions do not identify boot
  driver ownership.
- **Impact/dependencies:** P0, **Access deferred**; needs PLAT-01/BOOT-01;
  physical network/SSH/provisioning depend on it.
- **Next/accept:** Read-only sysfs driver/modalias and module/firmware/init evidence;
  compare exact upstream support and qualify association/reconnect later. No link
  resets, scans, credential changes or module reloads during observation.
- **Architecture:** Favor standard Linux networking, not package selection by
  filename or wholesale reuse of OEM shell orchestration.

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
  (`InitializeBoardBsp`), roles and reset/writes. This is not measured electrical
  behavior or current pinmux/resource ownership.
- **Impact/dependencies:** P1, **Partial**; needs BOOT-01/PLAT-01; feeds
  FEED-01/ADC-01/POWER-01.
- **Next/accept:** Resume targeted cross-program inspection and compare board
  resources; establish checked HAL contracts. Safe physical levels/failure
  behavior require an explicitly authorized observation/experiment plan.
- **Architecture:** Reuse verified mappings, serialize shared access, and avoid
  binding application logic directly to one sysfs/driver implementation.

### FEED-01 — portions, sequencing and two bowls

- **Question/current evidence:** Which transitions establish a portion, dual-bowl
  sequence, jam limits and safe stop? E3/E4 cover callbacks/state machine,
  retained nonconsecutive current hits and nominal delays. The host model uses
  synthetic portions/timings, not measured food or electrical limits.
- **Impact/dependencies:** P1, **Partial**; needs BSP-01/ADC-01/POWER-01;
  schedule/API requests go to this single owner, not directly to hardware.
- **Next/accept:** Complete only missing binary contracts after the hold; correlate
  controlled physical evidence later. Establish counting, bounded motion,
  stale-work rejection, jam/cancel and uncertainty behavior; do not adopt nominal
  OEM delays as proven safety limits.
- **Architecture:** Output-write success, stopped observation, mechanism cycle
  and guaranteed food mass are different facts.

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

- **Question/current evidence:** Who owns health from module init through shutdown,
  and what reset response is safe? E0/E2 establish enable-before-open,
  close-not-disarm, option meanings and OEM audio coupling. Do not rediscover
  these facts; the replacement health/fault policy is still unqualified.
- **Impact/dependencies:** P1, **Partial**, startup risk also relevant to BOOT-01;
  needs selected kernel/driver and BSP-01/FEED-01.
- **Next/accept:** Resume outstanding lifecycle paths; qualify acquisition failure,
  health loss, reset and outputs under separate hardware authority. Never enable
  the physical watchdog merely to test a guess.
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
- **Next/accept:** Specify validated independent schemas/commit protocol including
  the actuation-versus-recording crash window; fault-inject, then qualify actual
  filesystem/power-loss behavior. Shape-valid bytes alone cannot authorize feeding.
- **Architecture:** Human-readable config remains preferred; choose journaling
  for consistency/flash-wear requirements, not a format ideology.

### SCHED-01 — offline time and schedule execution

- **Question/current evidence:** What establishes valid time after power loss and
  how are DST/time jumps/missed occurrences/reboot handled? E3/E4 identify exact-
  second scheduling hazards; monotonic deadlines and durable occurrence identity
  are design requirements. E1's fake-hwclock/ntpd is not proof of a reliable RTC.
- **Impact/dependencies:** P1, **Partial**; needs STATE-01/FEED-01/POWER-01.
- **Next/accept:** Establish clock/retention facilities read-only later, agree
  invalid-time/catch-up policy, then test offline/reboot/time-change cases.
  Do not silently dispense overdue meals or introduce cloud dependence.
- **Architecture:** Offline feeding is firm; uncertain calendar time needs a
  defined degraded policy rather than an assumed RTC.

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
  not replacement capture. `GK_API_*` wrappers still lack verified providers.
- **Impact/dependencies:** P2, **Partial**; needs BUILD-01/BOOT-01/BSP-01/POWER-01
  and qualified budgets. Not a prerequisite for minimal SSH.
- **Next/accept:** Resume bounded sensor/ISP/audio ioctl/shutdown cross-analysis;
  compare exact upstream interfaces, then qualify capture/encode/playback,
  stop/restart and concurrent feeding. Retain proprietary-layout uncertainties.
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

Next turn: restore native Ghidra and dedicated read-only SSH access. Obtain a
sanitized PLAT-01/BOOT-01/WIFI-01 fact set without MMIO, service changes or resets;
verify saved program identities before choosing new binary questions. In parallel,
qualify EMU-01/EMU-02 against locked source/artifact identities. Keep execution
proof separate from the source-discovery closure already recorded here.

For resumed OEM groups: identity/checkpoint → investigation → supported bounded
annotations → save/readback/preservation → affected canonical exports/derived
evidence → tracker update with immutable links. Until then, this register must
not claim new Ghidra improvements or a new saved-project checkpoint.

| Date | Change / evidence | Remaining boundary |
| --- | --- | --- |
| 2026-10-28 | Initial 20 entries; SRC-01 closed only for source-selection scope using E1. Existing contracts reused rather than reopened. | OEM/Ghidra and SSH deferred by owner; no build/boot, firmware implementation or physical qualification. |
