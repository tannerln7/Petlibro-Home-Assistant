# PLAF203 replacement firmware — architecture centerline

**Baseline:** 2026-10-28 · **Status:** initial architecture; no replacement firmware implemented or hardware-qualified by this document.

This is the maintained development centerline for a complete, local-first PLAF203
replacement. It preserves product intent, ownership boundaries, rationale and
revision criteria; it is not an immutable specification, a flashing procedure,
an implementation backlog or a comprehensive research-gap register. The
[living research-gap register](plaf203-replacement-firmware-research-gaps.md)
tracks implementation-knowledge sufficiency and qualification separately.

## 1. Decision authority and scope

Use the following classifications throughout development:

| Class | Meaning and change rule |
| --- | --- |
| **Firm requirement (FR)** | Product behavior or safety constraint. An apparent conflict must be explained and aligned with the owner before relaxing it. |
| **Current decision (AD)** | Selected architectural direction, based on present evidence. Revise deliberately when a better-supported approach emerges. Not a claim of completed implementation. |
| **Preferred candidate (PC)** | Favored technology or implementation that still needs target qualification. Selection is not approval to deploy. |
| **Assumption (AS)** | A working belief, not a verified dependency. Do not hide it inside an implementation contract. |
| **Deferred decision (DD)** | Deliberately open detail with a defined responsibility boundary. Do not invent a final value to fill a template. |
| **Review trigger (RT)** | Evidence or experience requiring reconsideration of a decision; not an automatic change to product requirements. |

Verified device evidence and reproducible implementation results outrank a design
assumption. They do not silently waive a product requirement. Record meaningful
changes here with the evidence, rationale, rejected alternative and effect on
compatibility, safety, testing and recovery. Implementation languages, processes
and layouts are means, not product requirements.

### Repository responsibilities and starting point

| Repository | Ownership and inspected baseline |
| --- | --- |
| [Petlibro-Home-Assistant](https://github.com/tannerln7/Petlibro-Home-Assistant) | Replacement development, management integration, independent source, build configuration and future implementation planning. This branch starts at management foundation `9edd22d65508ccef934c4c749d212c9cac36d2c1`. |
| [plaf203-firmware-reconstruction](https://github.com/tannerln7/plaf203-firmware-reconstruction) | OEM evidence, Ghidra, captures, hardware research and analysis corrections. Inspected `main` at `adfe4c7e71678aec933c19f9f94230b6a4d71426`; no evidence files are copied into this development repository. |
| [Patched go2rtc](https://github.com/tannerln7/go2rtc) | Existing camera transport/session/media reference. Actual local branch is lowercase `petlibro`, inspected at `6ca2e665b1bf483462562165e1cf011526a259f8`; the requested `Petlibro` spelling is not the checked-out ref. |

The dedicated worktree is `/srv/dev/repos/Petlibro-firmware-replacement/`, branch
`feature/replacement-firmware-foundation`. These are development locations, not
paths to embed in firmware. The management base matched
`origin/feature/management-app-foundation` when created. It diverges from
`develop` and current `origin/main`; neither is silently merged into this branch.

The other management worktree had an unfinished merge from `77e2aea`, including
conflicts in `addon/CHANGELOG.md`, `addon/config.yaml` and `docs/development.md`,
and pending removal of vendored go2rtc in favor of source acquisition. Those
changes are **not** part of this committed base and were left untouched. Its
resolution needs deliberate integration later, not firmware-specific workarounds.
The main development worktree also had an untracked replacement-research map;
it remains separate, not an imported specification. The go2rtc worktree has local
documentation/configuration edits; references here distinguish its committed
source from those edits. The base's vendored `addon/go2rtc/` is a separate
snapshot; its last path-changing commit is `e107570b`, not proof of equivalence
to the standalone fork.

### Maintained sources, not a second management design

- [Existing operational architecture](architecture.md): OEM installation,
  AppDaemon, State Agent, broker and camera authority.
- [Management coverage map](petlibro-management-architecture-coverage-map.md):
  responsibility and state ownership.
- [Management implementation guide](petlibro-management-implementation-guide.md)
  and [migration sequence](petlibro-management-migration-implementation-sequence.md):
  host-side conventions, contracts and qualification boundaries.
- [Management README](../management/README.md) and its colocated source/tests:
  what the foundation actually implements. Older planning statements such as
  “the application does not yet exist” are not current implementation status.
- [Development guide](development.md), [installer](../installer/README.md),
  [State Agent](../state-agent/README.md), and
  [camera development](camera-development.md): established component workflows.

For OEM facts, start with the pinned
[analysis reconciliation](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/adfe4c7e71678aec933c19f9f94230b6a4d71426/docs/research/analysis-reconciliation.md)
and its linked exports/contracts. That checkpoint covers 90 programs and all 14
direct application runtime providers. This is analysis coverage, **not** a
complete board-support package or physical safety qualification. Some older
replacement-guide preamble still describes revision 15/live analysis as
unverified; it does not supersede the reconciled manifest and preservation record.
Original application identity remains SHA-256
`da353ba37a7d8c1c0790827e5ec267a180519affb5666ae4efd93e5362e00b71`.
Keep address-level research and raw/private material in the evidence repository;
reference sanitized conclusions and immutable revisions here.

## 2. Product intent and non-goals

**FR:** Build a purpose-built embedded Linux system, not a reproduction of
`AF203_FW`, its process topology, packed files, cloud MQTT dialect or proprietary
camera protocol. The finished product must provide reliable local feeder
operation, offline schedules, local management and Home Assistant integration,
with useful native camera/audio support. Vendor cloud availability must not be
required for startup, feeding, provisioning after replacement, or operation.

Priorities are predictable hardware behavior; testable and observable software;
maintainable responsibility boundaries; authenticated management and trustworthy
updates; reproducible builds; and bounded CPU, RAM, storage and dependency cost.
Construct permanent, working slices of this intended architecture, not a large
temporary OEM-filesystem implementation to discard later.

Offline operation does not mean guessing calendar time after power loss, ignoring
sensor uncertainty or dispensing without limits. Define degraded behavior
explicitly. Physical evidence bounds what can be promised: a completed motor
sequence is not automatically a measured quantity of food delivered.

Non-goals include cloud emulation, arbitrary remote shell/register APIs, broad
support for unqualified feeder models, importing raw firmware into application
source, or rewriting the management frontend. Existing one-feeder-per-add-on
guided deployment remains the supported integration scope unless separately
expanded and qualified. This assignment creates no firmware services, source
skeletons, QEMU setup, installer, OTA implementation or gap register.

## 3. Target system and authority

**AD:** Modular Linux software with a small number of justified lifecycle units.
These are responsibility boundaries, not a process, class or RPC per row.

```text
Home Assistant entities / management browser
                    |
       existing host coordination and operations
                    |
       native device capability/controller adapter
          | authenticated commands and snapshots
          | asynchronous device-originated events
          v
Device API -> device-local owners -> hardware interfaces -> peripherals
                 |       |
       local schedules   persistent device state / operation evidence

Independent media capture -> standard streams -> go2rtc / authorized clients
Platform: boot, networking, supervision, health, update and recovery facilities
```

The firmware owns physical actuation, local schedules, effective device
configuration and device-originated operation outcomes. The host owns accepted
management intent, user workflows/checkpoints and its historical records. The
browser owns presentation only. A native adapter translates boundaries; it
does not move physical control or the schedule execution clock into Node or HA.

| Responsibility | Authority and isolation rule |
| --- | --- |
| Hardware interfaces | Own checked GPIO/expander/ADC/driver access, initialization and acquisition validity. Share hardware through explicit serialization, not competing register writers. |
| Feeding control | One authoritative motor/feeding state-machine owner admits manual, scheduled and remote requests, enforces exclusivity and determines evidence-supported outcomes. |
| Sensor/fault handling | Supplies timestamped, valid/error/stale observations and fault evidence; never independently drives a delayed motor action. |
| Local scheduler | Owns calendar eligibility and durable occurrence identity; submits feed requests to the same feeding owner. No second dispensing path. |
| State/configuration | Owns validated, versioned effective settings and durable transitions. Storage mechanisms do not decide physical truth. |
| Device API | Authenticates/authorizes, validates, delegates, publishes snapshots/events; no direct driver bypass. |
| Networking/provisioning | Owns link and credential lifecycle; loss of network does not stop local feeding or recreate schedules. |
| Camera/audio | Owns capture, encoding, playback and media lifecycles; failure/resource pressure must not block essential control. |
| Updates/recovery | Owns device-side verification/activation evidence and safe transitions; host capability owns its installation transaction, not device truth. |
| System health/diagnostics | Observes meaningful progress, faults and resource health; cannot turn liveness or a successful write into proof of safety. |

An initial cohesive feeding/sensor/scheduler/state service is compatible with
this model if its ownership and tests remain explicit. Isolate media and network
blocking paths from safety/control work; split further only for actual fault,
privilege, timing or resource reasons. Prefer narrow local interfaces over a
generic plugin framework or internal message-broker infrastructure.

## 4. Platform, boot and reproducible builds

**AD:** Use a clean, minimal OpenIPC/Buildroot-based Linux image with PLAF203
configuration and components. Do not build the permanent service suite inside
the storage-constrained OEM rootfs. Temporary inspection/bootstrap facilities
are a transition mechanism, not the replacement platform.

[OpenIPC firmware](https://github.com/OpenIPC/firmware) identifies Buildroot as its
base. **AS:** its GK7205V200 family support and conventional camera-board
configuration are a reasonable starting point, not verified compatibility with
this feeder's pinmux, flash, Wi-Fi, power transitions or peripherals. Qualify the
actual board and avoid selecting a similarly named vendor binary by filename.

**Evidence refinement (2026-10-28):** the [read-only platform investigation](https://github.com/tannerln7/plaf203-firmware-reconstruction/blob/b29b6455e06a5f15b5e06aea41f08a8cd3d91ae8/docs/research/replacement-platform-device-contracts.md)
observes a contiguous 32 MiB NOR partition window, current JFFS2 root and 45 MiB
Linux allocation; total physical DDR and replacement layout remain unqualified.
Actual WLAN is USB RTL8733BU (`0bda:b733`), with a matching OpenIPC source
candidate—not stock lite's MT7601U or an identity inferred from rtl8723fu blobs.
Kernel/module ABI, radio power/reset and real reconnect behavior need qualification.
The OEM init loader/application can alter sensor selection and reboot; bulk OEM
startup is not a minimal platform recipe. Watchdog load can arm hardware before
userspace ownership. Preserve explicit safe initialization/health ownership rather
than importing these side effects into generic Linux/network bring-up. These
facts refine constraints; they do not choose an A/B scheme, waive recovery, require
OEM architecture or justify replacing the selected OpenIPC direction.

Initial platform coverage must include bootable Linux, board support/drivers,
network/Wi-Fi management, secure key-based SSH, lightweight init/supervision,
essential utilities, bounded logs, diagnostics and validated configuration.
Readiness is subsystem-specific; networking/media failures cannot make the
feeding owner appear healthy or gate its startup indefinitely.

**AD:** Kernel, rootfs/application image and U-Boot are independently versioned
components with an explicit compatibility set. Retaining a compatible existing
kernel or bootloader initially is reasonable, but neither is preserved simply
for historical compatibility. A newer maintained kernel is desirable where
practical; it is not an unconditional prerequisite. SDK modules, media ABI,
Wi-Fi and board support may constrain kernel choice. **RT:** security/support
limits or incompatible drivers require reviewing the selected combination,
not copying old modules into a different kernel and declaring success.

Builds should pin upstream revisions, patches, Buildroot configuration,
toolchains/sysroots, package inputs and checksums, CPU/ABI settings and emulator
version. Host environments are reproducible and mise-compatible; target build
requirements come from the selected platform, not from a developer's active
host compiler. Record artifact identity, component versions, licenses/provenance
and compatibility in release metadata. Measure reproducibility rather than
assuming that one successful build is reproducible. Keep host signing secrets
and device-specific credentials out of source and generic images.

**PC:** predominantly read-only firmware contents, with explicitly mounted
durable data and volatile runtime files. **DD:** exact filesystem formats,
mountpoints, flash allocation, partitioning, init implementation and package
profile. Choose them from verified storage geometry, RAM and recovery constraints,
not an assumed capacity for A/B images. Firmware updates must not accidentally
replace device identity, configuration, calibration or operation history.

## 5. QEMU is a first-class development target

**FR:** Reproducible ARM system-emulation development/testing begins with the
platform foundation, not after physical deployment. Support kernel/rootfs boot,
virtual networking and SSH, supervision, application/API lifecycle, persistent
state and reboot tests, and installation/update/failure-recovery simulation.

There are two complementary test profiles, not two unrelated implementations:

1. **PC: SoC-specific profile.** The upstream
   [OpenIPC QEMU fork](https://github.com/OpenIPC/qemu-bench), previously named
   `qemu-hisilicon`, advertises a `gk7205v200` model. Its existence makes this a
   concrete qualification candidate, not proof that the PLAF203 kernel, flash,
   board peripherals or media pipeline are faithfully modeled. Pin a revision
   and establish what actually boots and which interfaces are synthetic.
2. **AD: portable ARM integration profile.** A supported generic QEMU board can
   exercise the same application packages/contracts with synthetic hardware
   adapters and an appropriate kernel/configuration. Upstream
   [QEMU ARM documentation](https://www.qemu.org/docs/master/system/target-arm.html)
   recommends `virt` for generic Linux rather than reproducing a real board.
   CPU compatibility alone does not make an OEM/SoC kernel boot on that board.

Keep application behavior shared, with explicit hardware backends rather than
an emulator-only feeder implementation. Inject sensor acquisition, motor-command
receipts, clocks, power loss and faults at real boundaries. Bound virtual CPU/RAM
and test load where meaningful. Generic-board tests must not be labeled
GK7205V200 boot qualification. Failure of a particular SoC emulator does not
remove the QEMU requirement or automatically require writing a new emulator;
reconsider the test split and unsupported claims with the owner.

QEMU results do not qualify flash durability, Wi-Fi/RF, electrical motor stops,
sensor thresholds, real timing, battery behavior, watchdog reset or camera/audio
hardware. Even a faithful-looking boot model needs independent physical evidence.
No emulator was built or run in this planning assignment.

## 6. Feeding, timing and failure semantics

**FR:** Essential feeding remains autonomous when HA, the browser, network,
broker or media services are unavailable. Scheduling and actuation cannot wait
on network I/O, camera initialization or cloud authentication.

**AD:** A single feeding owner accepts events/commands, serializes state
transitions and issues hardware actions through checked interfaces. Carry
owner-lifetime/operation identity and versioned deadline identity so a cancelled,
rearmed or previous-operation timer cannot resume a new motor operation.
Cancellation requests are honored only at defined safe points. Stale command
results cannot complete a newer operation, but relevant fault/uncertain-actuator
evidence must not simply disappear because its generation is old.

Ground hardware contracts in the reconciled OEM evidence without copying unsafe
OEM behavior. Notable design consequences include shared ADC/expander resource
ownership, explicit read errors and sample freshness, bounded motor activity,
deterministic jam/recovery limits, and persistence bounds checking. Stop-command
success is not proof the motor physically stopped. OEM GPIO polarities, timings
and current thresholds require board qualification before becoming safety limits.
The existing host-only feeder simulation in the evidence repository is a useful
contract/test reference, not hardware-qualified firmware to copy wholesale.

**AD:** Use monotonic timing for control deadlines and wall/calendar time for
schedule eligibility. Define durable occurrence identity, plan revisions,
timezone/DST behavior, clock validity, time jumps, reboot catch-up and missed-feed
policy. **DD:** the exact policy and time-retention source; no reliable RTC or
offline correct wall clock is assumed. Offline schedules must work with valid
local time; uncertain time must produce a defined, observable degraded state,
not invented timestamps, duplicate meals or an undocumented network dependency.

Record enough operation intent/progress/outcome durably to reconcile after a
crash. If power is lost between physical actuation and recording the result,
the result can be **indeterminate**. Do not automatically repeat or resume a
possibly completed feed. Define recovery and operator/local-action policy from
available evidence. Exactly-once network delivery or a deduplication key cannot
guarantee exactly-once physical dispensing across every failure boundary.

**FR:** Watchdog behavior must reflect essential-system health, independent of
audio/media activity. **DD:** exact ownership, health criteria and fault response
for the selected driver/platform. The recovered OEM driver can enable hardware
before userspace opens it and closing its fd does not disarm it. A blind petting
loop, automatic service restart or watchdog reset alone is not proof of safe
actuator state. Qualify startup, acquisition failure, shutdown, crash and reset
behavior; never weaken watchdog safeguards merely to simplify bring-up.

## 7. Persistent state and configuration

**AD:** Prefer human-readable, versioned configuration/state where practical.
Validate schemas, bounds, units, ranges and cross-record relationships before
activation; separate desired changes, durable committed state and effective
runtime state. Preserve useful local schedules/history without requiring OEM
packed-file compatibility. OEM import, if offered, is a validated migration,
not continued dependence on OEM binary layouts.

Separate immutable firmware, protected durable identity/credentials, ordinary
durable configuration/operation records, and volatile sockets/caches/logs.
Choose a small transaction/journal mechanism from actual consistency needs;
neither a database for every setting nor ad hoc JSON for a physical-operation
journal is mandated. Atomic rename alone is not a complete power-loss protocol:
qualify filesystem guarantees, synchronization, multi-record commits, corruption
detection, schema migrations and rollback compatibility. Preserve last-known-good
state, expose corruption and bound retention/write amplification. High-rate
telemetry should not cause continuous flash writes.

The device remains the authority for applied schedules and settings. The host
may retain requested values, acknowledged revisions and historical observations;
these do not override a fresh device snapshot. Factory reset, key retirement,
backup restore and firmware rollback need explicit data/version policies rather
than silently erasing the persistent mount.

## 8. Native device API and security

**FR:** An authenticated, versioned native API with bidirectional event-driven
communication is part of the first implemented control slice. Do not recreate
OEM MQTT/cloud messages unless a demonstrated compatibility requirement warrants
an adapter. **PC:** HTTP/JSON request/response plus WebSocket device events;
revisit framing/library choices for target footprint and lifecycle behavior.

Commands and queries must distinguish rejection, acceptance, execution, observed
completion, verification, known failure and indeterminate outcome. Use stable
device identity, request/operation correlation and bounded duplicate protection;
reject reuse of an operation key with conflicting intent. State-changing requests
need explicit concurrency/precondition semantics. A timeout is not permission to
retry physical work blindly: query the owning operation and reconcile first.

The device independently originates scheduled-feed, dispensing progress/result,
sensor fault, jam/recovery, state, connectivity/service and significant
update/recovery events. The browser or HA must not poll to create those events.
A conceptual envelope includes schema/type, device identity, owner/source,
boot/session epoch, sequence within a defined ordering scope, operation/request
correlation when applicable, time/clock validity and a typed payload. Do not
pretend independent services share one total order without an actual sequencer.

Snapshots expose authoritative revision/watermark context so reconnecting clients
can detect gaps and reconcile without a subscription race. Bound replay and
retention; explicitly signal unavailable history or changed epochs. Recover
critical facts through state/operation queries, not only an open WebSocket.
Durable significant outcomes and ephemeral logs/telemetry have different delivery
guarantees. Slow clients or event backpressure cannot block motor safety work.

**AD:** Device trust is explicit: per-device authentication, scoped authorization,
protected secret storage, verified peer identity, credential rotation/revocation,
and least-privilege listeners. Prefer encrypted control/event transport; choose
TLS/credential enrollment with the actual footprint, offline clock and recovery
constraints in view. **DD:** exact server/library, certificate versus pinned-key
or token scheme, secure first enrollment and discovery. No shared factory secret,
plaintext credential URL, unauthenticated control fallback or generic command
execution endpoint is acceptable. SSH uses secure authentication and is a
recovery/administration facility, not the normal device API.

The existing management app delegates browser authentication/TLS to its deployment
boundary; that does **not** authenticate a separately networked device. Preserve
Ingress/direct-access protections and explicitly authorize native device actions.
API authentication, software-update signing and SSH recovery are distinct trust
purposes; do not casually reuse keys or an OEM account across them.

## 9. Networking, Bluetooth and provisioning

**AD:** Networking/Wi-Fi is platform functionality built on maintained Linux
facilities, with clear credential custody, restart/adoption evidence and offline
behavior. Do not reproduce OEM shell/process management as application logic.
Network changes need observable outcomes and a recovery path when connectivity
is lost; the feeding owner continues independently.

**AS:** Bluetooth may provide useful onboarding/recovery access. Hardware,
driver/firmware requirements and an appropriate Linux stack are not yet qualified.
**DD:** BLE, temporary Wi-Fi AP onboarding, another local mechanism, or a supported
combination. Evaluate authenticated ownership establishment, credential exposure,
Wi-Fi station/AP concurrency, headless usability, resource cost and recovery.
OEM BLE compatibility is not itself a requirement. Do not expose an unsecured
permanent provisioning listener or assume that the existing claimed-device
bootstrap is the final replacement provisioning design.

## 10. Native camera and audio

**AD:** Native sensor/audio capture and hardware encoding with standard streaming,
not the OEM proprietary LAN transport. Keep capture/encoding on the feeder and
stream distribution/playback in the appropriate media service; do not carry
video through the management event journal or rebuild software encoding without
a demonstrated need.

**PC:** [Divinus](https://github.com/OpenIPC/divinus) is the first media candidate;
its upstream hardware table lists the GK7205V200 family. **PC:**
[OpenHisilicon](https://github.com/OpenIPC/openhisilicon) is an important
alternative/supporting SDK and driver path. These can occupy different layers
rather than being mutually exclusive end-to-end products. Neither listing proves
PLAF203 sensor initialization, calibration, codec behavior or distribution rights
for every vendor component. **DD:** final media stack, sensor/ISP setup, driver
set, audio implementation and detailed audio/talkback feature profile.

GC2053 and matching codec/module evidence inform qualification; do not infer
all board/calibration contracts from a standard SoC family. Evaluate kernel/SDK
ABI and resource requirements together. Retaining narrowly justified compatible
vendor pieces may be an interim choice with explicit provenance/support limits;
it is not permission to ship unreviewed recovered binaries by default.

Use existing go2rtc behavior as a lifecycle reference: demand-driven sessions,
clean stop/release, ordering, keyframe/parameter-set readiness, timestamps,
backpressure, restart and quality changes. Do not reproduce TUTK quirks merely
for parity when a standard stream removes their cause. go2rtc may remain the
host-side standard-stream gateway; management consumes status and authorized
stream references, not transport internals. Media resource exhaustion or crash
must leave feeding/control safe and observable. Stream access also needs an
explicit authentication/exposure policy, not a permanent public camera URL.

## 11. Wireless installation, recovery and updates

**FR:** Normal installation from an already claimed stock feeder must be wireless;
UART/external programming is not a normal production prerequisite. Existing OEM
provisioning/root-access/bootstrap mechanisms are a starting point, not proof
that a complete firmware transition is already safe or implemented. The existing
State Agent/bootstrap's OTA-slot preservation is not a complete flash backup.

**FR:** Before destructive modification, acquire and verify a complete off-device
OEM flash backup and the information required for meaningful restoration.
Preserve board-specific identity, calibration and other essential data. Establish
flash type/geometry, partition boundaries and names, boot/environment contents,
read/write semantics and, where applicable, ECC/bad-block handling. Bind backup
manifests and checksums to the device and acquisition method; verify off-device
retrieval and custody, not only a feeder-local checksum. Protect private contents
and recovery keys; backups/dumps do not belong in tracked application source.

Investigate bootloader capability, boot selection, rescue access, rollback and
restoration early enough to shape image/data layout. **DD:** A/B, rescue image,
staged rootfs switch or another mechanism; flash constraints may rule out a
preferred scheme. Keeping OEM U-Boot is not a requirement. Replacement is allowed
when justified and qualified, with a separate risk/authority decision for
bootloader writes. A verified backup improves recovery but cannot guarantee
rescue from a broken bootloader or every wireless failure. UART/external flashing
may be necessary for exceptional and early-development recovery.

**AD:** Design now for authenticated management, signature-verified update inputs,
device/component compatibility checks, power/space preconditions, interruption
evidence, boot health confirmation, and state-schema compatibility with recovery.
Distinguish upload, validation, staging, activation and verified boot. A transport
ACK or successful filesystem write is not a successful update. **DD:** exact OTA
format, signing/enrollment/rotation mechanism, rollback/anti-rollback policy and
installer sequence. Do not copy the agent-only update protocol as if it already
solves whole-image, kernel or bootloader recovery.

The host coordinator owns user approval, backups/artifact custody, checkpoints
and overall installation evidence; its capability worker owns technical sessions
and guarded transfer. Device-side facilities own verification and activation.
No browser-held job or disconnected socket determines whether flashing finished.
This document authorizes no flashing or installer implementation.

## 12. Native Home Assistant and management integration

**FR:** Preserve established entities, stable identifiers and user-facing behavior
where practical. Keep the operational OEM AppDaemon/MQTT/State Agent path intact
during development. Removing device-side OEM MQTT does **not** require removing
MQTT discovery/entities between HA and its integration controller.

**AD:** Add native device capability/controller adapters behind existing domain
contracts and source-aware device projections. Reuse the action/operation model,
worker boundary, custody, event delivery and diagnostics. The current
`DeviceOverviewService` reads AppDaemon/go2rtc observations; a native source must
be deliberately added/selected, not disguised as an OEM State Agent file or
treated as an already pluggable implementation. Existing generic capability
requests are not an excuse to skip validated native schemas.

The host may own a long-running installation job and a shorter capability
invocation while the firmware owns its dispensing operation. Preserve those
distinct IDs/lifetimes and evidence when reporting an outcome. Management's
SQLite journal is not the device schedule database. Routine autonomous feeds
originate on the device and become observations/history; they do not require a
browser management job to exist.

Keep the frontend's concepts stable: availability, settings/plans, operation
progress/results, source/freshness, camera status and diagnostics. Add capability
differences through explicit contracts rather than a parallel frontend or fake
controls. Preserve factory-associated identities only where verified and safe;
an IP address alone is not stable identity or authority.

Cutover is per supported deployment/device: establish identity and trust, compare
contracts and required HA behavior, quiesce/disable the legacy device writers,
verify the selected native path, reconcile state and restore availability.
Do not run AppDaemon/OEM/native controllers as competing integration writers.
The firmware still serializes local buttons, scheduled requests and authorized
remote commands through its single motor owner. Safe rollback to OEM depends on
compatible state and the qualified recovery path, not merely re-enabling an old
MQTT topic. A future host-controller TypeScript migration is a separate decision
from replacing device firmware; neither entails a frontend rewrite.

## 13. Languages, dependencies and validation direction

**DD:** Implementation language and exact process/library choices. C and modern
C++ are natural control/HAL candidates, not mandates inherited from the OEM.
Other languages are acceptable when measured runtime/flash/RAM cost, target ABI,
toolchain availability, dependency/update burden and failure behavior justify
them. The management stack's TypeScript/Node choices are host-side decisions,
not a reason to place Node on the feeder. Prefer conventional Linux components
and simple interfaces over custom infrastructure or excessive process splitting.

Tests develop against the final boundaries, with deterministic clocks and
synthetic hardware where needed. Required evidence classes are distinct:

| Level | What it establishes; what it does not |
| --- | --- |
| Host/unit and contract tests | Parsing, state transitions, scheduling, fault policy, revisions and protocol behavior; not board behavior. |
| ARM/QEMU integration | Selected image boot, services, authenticated API/events, network/reboot/persistence and injected failures; only modeled hardware behavior. |
| Controlled physical qualification | Actual electrical sequencing, limits, watchdog, flash/power loss, Wi-Fi, sensor/media and recovery behavior, with separate authorization. |
| HA/management integration | Entity/identity/availability parity, source authority, reconnect and single-writer cutover; not inferred from a successful API call alone. |

Installation/update testing needs safe interruption points and recovery outcomes
before a physical release. Every test result identifies image/component hashes,
target profile, synthetic versus real interfaces and unsupported claims. No test
plan or existing OEM test result is represented as replacement qualification.

## 14. Decision rationale and reconsideration

The detailed sections above define intent. This compact index preserves the
consequential alternatives and review triggers, not a research task register.

| Class / choice | Why this direction; important alternative | Review trigger |
| --- | --- | --- |
| FR: local-first autonomous feeding | Network/HA faults must not remove core function; host-only scheduling is insufficient. | Explain any conflict between time/power evidence and promised offline behavior; agree on degraded policy, not silent removal. |
| AD: clean OpenIPC/Buildroot platform | Reproducible minimal Linux rather than permanent development inside OEM constraints. | Board incompatibility, support/security limits, licensing or a demonstrably better-maintained platform. |
| AD: modular owners, few justified services | Avoid an OEM-style monolith without microservice fragmentation. | Measured blocking, privilege, resource or fault-isolation requirements. |
| FR/PC: QEMU / GK model | Emulator-first repeatability, with a concrete SoC-model candidate and portable ARM profile. | Actual boot/peripheral coverage differs from upstream claims; revise scope/claims, not pretend generic emulation is physical proof. |
| PC: read-mostly firmware + durable data | Limit accidental mutation and flash wear; a writable root is possible if better justified. | Verified filesystem, storage, update and crash-consistency constraints. |
| PC: HTTP/JSON + WebSocket | Simple queries plus autonomous events; other transports remain acceptable. | Measured footprint, security, reconnect or interoperability shortcomings. |
| PC: Divinus; OpenHisilicon alternative/support | Reuse hardware media work rather than OEM TUTK or software encoding. | Sensor/audio/ABI/maintenance/resource qualification or a better-supported upstream option. |
| DD: kernel/U-Boot, image layout and rollback | Preserve independent replaceability; do not promise A/B or mainline compatibility without evidence. | Qualified flash geometry, boot/rescue behavior, kernel/media dependency and security support. |
| DD: languages, onboarding, persistence engine | Choose for actual target constraints, not OEM or host language fashion. | Resource measurements, toolchain/driver support, security and maintainability evidence. |

When a trigger occurs, propose the alternative and its consequences, including
what remains reusable and what product requirements are affected. Seek alignment
for material changes; do not blindly preserve this baseline or silently overturn
it. Update the owning management documents only when their actual contracts
change, and the evidence repository only in separately authorized research work.

### Revision record

| Date | Change | Rationale / validation status |
| --- | --- | --- |
| 2026-10-28 | Initial centerline on a separate management-based feature worktree; linked from maintained navigation. | Preserves agreed product intent, distinguishes evidence from decisions, and records existing-worktree/base limitations. Documentation-only; no firmware, emulator or hardware qualification claimed. |
| 2026-10-28 | Added living research-gap register and pinned upstream-source evidence. | Confirms concrete source/configuration starting points, not a boot result; stock camera profile and model defaults are not a qualified PLAF203 image. No major architectural decision changed; new OEM/Ghidra and SSH work deferred by owner. |
| 2026-10-28 | Incorporated resumed live platform evidence and kernel/module-ABI dependency; adopted routine-save versus milestone-export research cadence through the linked register. | NOR map, Linux allocation and RTL8733BU identity are observed; independent boot, physical DDR/driver/time qualification and full restoration are not. No major product requirement changed, implementation begun or device mutation authorized. |

External upstream references in this document were consulted on 2026-10-28.
They are candidate/support evidence, not pinned build inputs or a claim that an
upstream advertised capability has been verified on the PLAF203. Pin and
qualify the chosen revisions during implementation.
