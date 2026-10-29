# Minimal PLAF203 QEMU firmware

Development-only Buildroot external tree: pinned source-built Linux/DTB/modules,
BusyBox init/utilities, DHCP, local
logging, key-only Dropbear and a native **non-actuating** health runtime.
No OEM application, cloud/claim bootstrap, Majestic, camera capture, physical
motor control, web UI or management API is included.
The [architecture](../../docs/plaf203-replacement-firmware-architecture.md) and
[gap register](../../docs/plaf203-replacement-firmware-research-gaps.md) own the
product direction; this document owns the current build/test commands.

The separate [disposable OEM reference profile](oem-reference.md) reuses the same
QEMU model with a verified full OEM flash copy and no networking. It is not this
replacement image or its security/lifecycle qualification.

### Source checkpoint provenance

The source history consolidates emulation tooling separately from the integrated
Buildroot/kernel/runtime/feeding profile. It does not reconstruct historical
intermediate implementations. E14–E17 receipts retain their original source,
artifact and repository baselines; earlier hashes may intentionally differ from
later source. The final feeding receipt identifies the current functional build
inputs. Documentation and ignore-rule integration does not change those inputs.
The ignored local emulator checkout is explained in [emulation/](../../emulation/README.md).

## Native runtime and health ownership

[`runtime/`](runtime/) is the permanent C11 service, built with the same pinned
GCC/musl toolchain through a conventional Buildroot local package. It has no
network/library dependency beyond libc and Linux interfaces. The pure health
core accepts injected clocks, subsystem observations and watchdog operations;
the Linux adapter owns process locking, signals and private status publication.
BusyBox init provides respawn through a one-second restart floor, not another
supervisor framework. An essential fault stays latched in the live owner; it
does **not** exit into an automatic health-recovery loop. Restart is currently
permitted with no physical actuator enabled. Startup now reconciles the device
operation journal before essential readiness. Unfinished effect-bearing work
becomes indeterminate and prevents new operations; restart never resumes it.

The three essential observations are the journal-backed synthetic feeding owner,
synthetic sensors and actual `/state/schema`/state-ready/journal-integrity checks.
No physical motor command, GPIO,
ADC threshold, electrical polarity or actuator deadline is implemented. Each
completed observation carries a strictly increasing sequence and owner
generation. Repeated/old successful callbacks cannot refresh current health;
old fault/uncertain evidence still latches a fault. Networking and media are
explicitly **not monitored** yet; optional fault flags can degrade health but
cannot establish essential readiness or keep a failed control owner healthy.
Wall-clock validity is unqualified; all health timing is monotonic.

Lifecycle: `initializing` → `running`, or `degraded` for optional faults/stale
essential observations; essential initialization/observation errors, clock
regression, watchdog/status errors and prolonged staleness latch `faulted`.
Essential staleness inhibits feeds immediately at two seconds and faults at
four seconds. These are **software diagnostic budgets**, not qualified motor
or physical watchdog safety limits. Deadlines are checked before accepting new
observations, so a paused loop cannot erase its stall on resumption. `TERM`/`INT`
quiesce adapters, stop feed eligibility and publish `shutdown`. The loop checks
signals every 200 ms, subject to local kernel I/O latency; this is not a
hard-real-time shutdown guarantee.

`/run/plaf203/` is root-only mode 0700. The exclusive `flock` lives for the
process lifetime. `status.json` is atomically replaced, mode 0600, once per
second or lifecycle transition; it includes generation, monotonic timestamp,
PID, subsystem sequences, masks, watchdog backend/feed count and explicit
`actuation_enabled:false`. This is a **snapshot**, not an API or proof a dead
process is still healthy: consumers must check timestamp freshness and owner
lifetime. No persistent status write churn occurs. Memory/address-space is
limited to 8 MiB, descriptors to 32, core dumps disabled; transition-only logs
and fixed-size status avoid unbounded runtime logging/state growth.
Status schema 1 uses subsystem bits control=1, sensors=2, storage=4. Latched
fault bits are initialization=1, stale essential progress=2, subsystem error=4,
clock regression=8, watchdog setup/feed=16, uncertain evidence=32 and status
publication=64; definitions live in `runtime/health.h`. Optional mask bits are
reserved for future observers; zero does not claim network/media health.

## Durable feeding operations (synthetic only)

The existing service owns the [feeding state machine](runtime/feeding.c),
[journal](runtime/journal.c), [synthetic HAL](runtime/synthetic.c), and root-only
local control socket. There is no second actuator process or message broker.
Manual callers and synthetic scheduled-origin requests use the same serialized
admission path. This is **not** offline scheduling, physical motor support, food
mass accounting or the authenticated network API.
The C owner functions are single-thread-affine: future workers/callbacks submit
value events to that owner, never call its mutation methods concurrently. Stop
requests can repeat after errors; a future backend must tolerate repeated stop
submission without treating it as stopped-state evidence.

### Identity, state and observation contracts

[operations.h](runtime/operations.h) defines versioned request, outcome and event
contracts. Identity is `(journal_epoch, monotonically increasing operation_id)`;
IDs are uint64 values, decimal strings in JSON. Read `highwater`, submit exactly
`highwater + 1`, and keep the same request/correlation when retrying. Multiple
callers may race; only one can win that ID. A changed request for a retained ID
returns `conflict`. The last **16** detailed outcomes are retained. An evicted ID
at/below highwater returns `retired`, never becomes a fresh operation. There is
no expiry based on wall time and no implicit sequence reset. A future API may
add allocation coordination, but must preserve this no-replay fence and bind
requests to the journal epoch. Correlation is a bounded 1–31 character label;
origin, synthetic units, wall-time validity and wall-time value are immutable
request fields. Correlation alone is not a globally unique execution identity.

Lifecycle: `admitted` → `intent` → `actuating` → `stopping` → `succeeded`.
Each synthetic unit requires a fresh completion observation and **separate fresh
stopped observation**. More units return to `intent`, not an out-of-band motor
path. A jam requests stop through `recovering`; only a fresh stopped observation
permits a bounded retry. Exhaustion yields `failed`, requiring intervention.
Cancellation before actuation becomes `cancelled`; cancellation during motion
first persists `cancelling`, requests stop, then waits for stopped observation.
A successful stop call alone never confirms cancellation/completion.

Owner epoch, operation generation and deadline generation qualify callbacks.
Rearming/cancelling changes the deadline identity; stale successful callbacks,
duplicate samples, invalid/future acquisition times and cancelled expirations
cannot advance work. Old failed/uncertain evidence still faults the current
owner, rather than disappearing under a generation filter. Clock regression,
missing progress, unconfirmed stop and uncertain observations inhibit further
starts. The synthetic profile allows 1 retry per unit (compile-time validation
permits 0–3), 1–16 units, a 1000 ms phase deadline and 800 ms sample freshness;
synthetic completion takes 400 ms. These are **test policy**, not electrical,
mechanical, portion-size or physical safety limits. Time is injected monotonic
milliseconds; recorded wall validity/time is metadata only and cannot establish
schedule eligibility. No old monotonic deadline is restored across owner epochs.

### Journal layout and durability

On the existing JFFS2 `/state` mount:

* `feeding.required`: 32-byte versioned, CRC-protected epoch marker, mode 0600.
* `operations/`: mode 0700; exclusive `owner.lock` and `snapshot` mode 0600.
* `snapshot`: exactly **4096 bytes**, explicit little-endian fields, version,
  revision, highwater, 16 bounded records, reserved-zero validation and CRC32.
* `pending`: at most one 4096-byte temporary snapshot; never replayed/promoted on
  recovery. Normal steady payload is **4128 bytes**, peak payload **8224 bytes**,
  excluding JFFS2/inode/lock overhead and obsolete flash nodes awaiting GC.

For each transition: validate the existing marker/snapshot → exclusive temporary
write → `fsync(file)` → atomic rename → `fsync(directory)` → exact readback →
publish/acknowledge the new revision. Failure poisons the live journal and
inhibits further work; no older backup is silently substituted. High-rate sensor
polls/status updates cause no journal writes. A fresh journal first durably
creates its required marker and directory. Interrupted initialization with no
trustworthy snapshot fails closed and needs explicit operator recovery; it is
not automatically initialized again. Missing required files, wrong schema,
bad checksum, invalid fields, noncanonical bytes and unsafe permissions fail
closed. CRC detects accidental corruption, not malicious rewriting or rollback
to an older valid snapshot. Do not restore old state while retaining a claim
of current deduplication; trusted anti-rollback storage is not implemented.

The actuation fence is committed **before** the sole HAL start path. A delayed
durability fence is rechecked against the live monotonic clock before start.
Runtime health also samples time after subsystem work, so a slow callback cannot
earn a watchdog feed using a pre-I/O timestamp. Kernel/filesystem calls can still
block; these checks do not create hard-real-time I/O guarantees.

| Last trustworthy state at restart | Recovery | New actuation |
| --- | --- | --- |
| No admission committed | No operation exists | Only a fresh admitted request |
| `admitted` | Persist `aborted / not_started` | No automatic replay |
| `intent`, `actuating`, `stopping`, `recovering`, `cancelling` | Persist `indeterminate / owner_lost` | Blocked |
| Valid terminal outcome | Preserve it; duplicates return the recorded result | Never repeat that ID |
| Corrupt/missing journal or failed recovery write | Faulted / storage failure | Blocked |

Even a crash after synthetic completion, or after the last physical-effect
possibility but before trustworthy terminal persistence, stays indeterminate.
If a terminal record became durable before the caller received its reply, a
retry discovers that record; lost acknowledgement does not trigger execution.
There is deliberately no automatic clear/retry of indeterminate or failed
history. Operator reconciliation and authenticated recovery authorization belong
to a subsequent slice; deleting the journal is not a supported recovery command.

One no-retry success uses `2 + 3*units` whole-snapshot commits: **20 KiB** logical
writes for one unit, **32 KiB** for two, one file sync and directory sync per
commit. Cancellation/retries add bounded transitions. These are application
bytes/barriers, **not measured NOR erase wear**: compression, metadata, GC and
the flash translation/model determine actual amplification. The pinned JFFS2
`file.c:jffs2_fsync` and `dir.c` provide file/directory sync, but this old kernel
does not propagate every internal write-buffer flush result. Readback adds
consistency checking, not proof against physical power loss or controller caches.
Hardware power-cut/durability qualification remains required.

### Local development control and tests

`plaf203-ctl` is a small client of the root-only 0600 Unix `SOCK_SEQPACKET`
socket `/run/plaf203/control.sock`. Only the runtime opens the journal and owns
synthetic motor effects. It accepts bounded `V1` packets; max four requests per
loop and a 10 ms first-packet wait bound silent clients. There is no TCP listener,
remote authentication or HTTP service. The CLI exits nonzero on transport errors;
inspect JSON `result` for semantic rejection (a delivered rejection is not a
transport failure). Commands in the isolated development guest:

```sh
plaf203-ctl STATUS
# Use the next ID from highwater, not these literals after earlier operations.
plaf203-ctl SUBMIT 1 manual 1 example-manual
plaf203-ctl QUERY 1
plaf203-ctl SUBMIT 2 scheduled 2 synthetic-occurrence
plaf203-ctl CANCEL 2
```

`SIMULATE normal|hold|jam-once|jam|stale|failed|uncertain|stop-unconfirmed`
selects a synthetic mode only while idle; it cannot change active operation
behavior. These modes do not bypass journal/health gates. The internal C request
supports wall-time metadata and explicit journal epoch; the trusted local CLI
uses the live journal epoch with unknown wall time. Future asynchronous API
adapters must preserve caller-provided epoch/revision/token preconditions rather
than copying this interactive CLI convenience. `op_event` is the latest committed
transition, not a separately durable replayable event stream.

`/usr/libexec/plaf203-feeding-selftest` executes the same host crash/fault suite
inside ARM Linux, using private disposable `/tmp` directories. It never operates
the live journal or real hardware. The additional `--feeding-checks` (requires
`--runtime-checks`) exercises the live single owner on JFFS2 through three cold
boots, software reboot, concurrent callers, cancellation, process death during
actuation, indeterminate recovery, and corrupted-journal cold boot. Original
SSH/NOR/state/environment regression gates remain enabled. See the
[feeding checkpoint](feeding-qualification-20261029.json) for actual results.
The E16 [runtime-only checkpoint](runtime-qualification-20261029.json) remains
historical evidence; E17 adds durable feeding/journal qualification rather than
rewriting that receipt. Protected build artifacts and receipts for this milestone
are retained under `/srv/dev/artifacts/plaf203-durable-feeding/20261029/`.

### Watchdog capability boundary

The additional [kernel fragment](board/watchdog.config) enables the standard
watchdog framework, sysfs, `CONFIG_WATCHDOG_NOWAYOUT=y` and the **module**
`CONFIG_ARM_SP805_WATCHDOG=m`; the provenance base config remains unchanged.
Nothing automatically loads/opens a physical watchdog. Default runtime feeds
are simulated, testing policy only; a hung/killed default runtime cannot reset
the machine through this backend.

The opt-in Linux backend (`plaf203-runtime --watchdog /dev/watchdog`, after
stopping the normal owner) requires sysfs nowayout, SP805 identity and an exact
10-second timeout. Failed setup faults, never silently falls back. It starts
only after essential initialization/observations pass. No magic close/disarm
is sent: fault, death or shutdown can leave reset armed. `watchdog_armed` means
successful backend setup, **not** proof of electrical safety; status separately
reports an opened backend and whether a reset may remain armed after an attempt.
This is not an automatic watchdog handoff/restart policy for physical deployment.

The current QEMU machine maps a **CMSDK** watchdog at `0x12030000`, 3 MHz,
peripheral ID `0x001bb824`. The pinned DT describes `goke,wdg` there, not an AMBA
SP805. Linux `sp805_wdt` binds ID `0x00141805`. The misleading `arm,sp805` string
on `dual_timer1@12001000` is not the watchdog. The module can be built and
loaded/unloaded, but exposes **no `/dev/watchdog`** in this configuration.
No fake ID override, vendor driver rewrite or QEMU model change is used.
The old gap-register suggestion of SP805 at `0x12050000` was incorrect for this
source tree. Physical driver/binding/clock/reset qualification remains open.

### Runtime qualification

Run host tests with `python3 -m unittest installer.tests.test_plaf203_runtime`.
`make -C runtime test` is also supported in a disposable source copy, not in
the tracked source directory (build receipts deliberately reject stale/extra
local source inputs). The ARM image includes
`/usr/libexec/plaf203-runtime-selftest`, executing the same deterministic core
tests without hardware access. Build receipts hash actual copied runtime
sources, package recipe, init files, kernel fragment and installed executables.
After edits in a reused Buildroot tree, run `plaf203-runtime-dirclean` before
rebuilding; a stale local-source copy causes receipt verification to fail.

Add `--runtime-checks` to the existing isolated NOR qualification command below.
It requires the service on each of three cold boots and checks sequence/health
progress, private modes, second-owner rejection, `SIGSTOP` stall/feed inhibition,
`SIGKILL` respawn with a new generation, orderly `SIGTERM` shutdown and respawn,
and module load/unload without claiming a watchdog device exists. The first
cycle also separately verifies the **modeled** watchdog expiration/reboot using
emulation-only MMIO after checking its peripheral ID. This does not test the
Linux watchdog backend or physical watchdog coverage. Never run these MMIO
commands on the feeder. Existing software reboot, NOR persistence, SSH identity,
authentication rejection, invalid enrollment, corrupt state and environment CRC
tests remain enabled. See [runtime checkpoint](runtime-qualification-20261029.json)
for actual results/hashes and remaining limits; historical E14/E15 receipts are
unchanged.

## Inputs and scope

- Buildroot **2024.02.10** release tarball SHA-256:
  `b193867d91ed468925a76828bd35ba64d8b4bd1ec238e35db8722fdd406926c2`.
- OpenIPC GK7205V200 GCC **13.3.0** musl SDK archive SHA-256:
  `f5062be797d0b2714b9311437168cde2bc72f28273d8d9797e6a897774775924`.
- Linux **4.9.37**, [OpenIPC source revision](https://github.com/OpenIPC/linux/tree/bb0266a361ebc03dc258aed20119793eab4835f1)
  `bb0266a361ebc03dc258aed20119793eab4835f1`; archive SHA-256
  `cc8eafe42f6019c180b40c57bbec038df98485401379952ef2a19ce343df96cf`.
  See the source-kernel qualification below. The former retained release image
  `da61bc25…` remains a comparison/legacy-test input, **not a build dependency**.
- QEMU v10.2.0 `698104725efad4b29079d857dfdebbd804e34c99` plus OpenIPC
  qemu-bench `b011673b31884683a534a3f3e1a2b080d5a99f19`. Qualification mounts the
  relocated `emulation/qemu/build` read-only at the runner's existing
  `qemu-bench/qemu-src/build` cache-layout path. Only the legacy retained-kernel
  comparison needs `qemu-bench/qemu-boot/uImage.gk7205v200`. Rebuild the emulator through
  `bash scripts/build_plaf203_emulator.sh CACHE_ROOT`; the
  [OEM reference guide](oem-reference.md#rebuild-the-model-without-losing-its-integration)
  explains clean initialization and the maintained NOR-only `0xdc` patch.
  Do not blindly replay upstream setup on an existing patched cache or fetch
  rolling head. `scripts/build_plaf203_qemu.sh` below builds Linux/userspace, not QEMU.

The [source lock](sources.lock.json) owns the Buildroot/SDK/kernel archive
identities and default reproducible epoch. Selected package versions and hashes
come from that pinned Buildroot release, not rolling OpenIPC media packages.
The SDK tarball is downloaded and hash-checked, then relocated in the disposable
cache. Buildroot package source hashes remain enforced. The Debian base digest
is pinned, but APT package versions are not; this is not full supply-chain closure
or a production security qualification. Upgrade/review dated dependencies before
deployment. Physical board/MMZ/driver qualification remains separate from the
source-built QEMU configuration.
Follow [Buildroot's external-tree/overlay customization model](https://buildroot.org/downloads/manual/manual.html#outside-br-custom),
not a second firmware build system.

## Build

Host prerequisites are Docker, Python 3.12+, jq, curl, tar, OpenSSH and ACL utilities.
Run from this worktree. Build outputs/checkouts remain outside Git. Choose a
private development cache; do not put the only copy of important data there.

```sh
cache=/srv/cache/plaf203-bringup-research
docker build --network=host --secret id=resolver,src=/etc/resolv.conf \
  -f docker/Dockerfile.qemu-bringup -t plaf203-qemu-bringup:review .
mkdir -p "$cache/minimal-test-private"
chmod 700 "$cache/minimal-test-private"
# Use a NEW disposable key, never a personal/feeder key. Do not overwrite one.
ssh-keygen -q -t ed25519 -N '' -f "$cache/minimal-test-private/auth"
(umask 077; head -c 32 /dev/urandom > "$cache/minimal-test-private/seed")
chmod 0600 "$cache/minimal-test-private/auth" "$cache/minimal-test-private/seed"
PLAF203_ENROLLMENT_PUBKEY="$cache/minimal-test-private/auth.pub" \
PLAF203_TEST_SEED="$cache/minimal-test-private/seed" \
  bash scripts/build_plaf203_qemu.sh "$cache"
```

The optional resolver mount supplies ordinary DNS configuration readable to
APT's `_apt` sandbox on hosts with a mode-0640 Docker resolver; it does not
disable the sandbox. Image name can be selected using `PLAF203_BUILD_IMAGE`.
The builder uses `BR2_REPRODUCIBLE=y` and `SOURCE_DATE_EPOCH` (default
1738368000). Different enrollment/entropy inputs intentionally produce different
images. The build container is resolved to its immutable local image ID before
execution. Reused Buildroot source files are compared with the verified release
archive; changed/missing files fail without overwriting local edits.

Every successful build writes `minimal-output/images/build-receipt.json` with
the image ID, resolved compiler version, configurations, profile/helper hashes,
selected package versions/licenses, verified package download digests, and rootfs
digest. It records only the **presence** of development enrollment/test entropy,
never their contents. A failed container build invalidates the previous receipt.
It also records the effective kernel configuration, module hashes, kernel image
tool/DTC versions and output hashes. This is not a source-built bootloader or a
production firmware release.

### Offline and independent-cache builds

`PLAF203_OFFLINE=1` forbids downloading the three locked archives and runs the
build container with `--network none`. Prime package downloads through an online
build (or Buildroot's `make source`) first. Copy only download inputs for a fresh
build; do not copy an extracted SDK, `minimal-output/`, or compiled host tools:

```sh
fresh="$(mktemp -d /srv/cache/plaf203-clean.XXXXXX)"
mkdir -p "$fresh/downloads" "$fresh/buildroot"
cp "$cache/downloads/"{buildroot-2024.02.10.tar.xz,toolchain.goke-gk7205v200.tgz,linux-bb0266a361ebc03dc258aed20119793eab4835f1.tar.gz} "$fresh/downloads/"
tar -xf "$fresh/downloads/buildroot-2024.02.10.tar.xz" -C "$fresh/buildroot"
cp -a "$cache/buildroot/buildroot-2024.02.10/dl" "$fresh/buildroot/buildroot-2024.02.10/"
PLAF203_OFFLINE=1 \
PLAF203_ENROLLMENT_PUBKEY="$cache/minimal-test-private/auth.pub" \
PLAF203_TEST_SEED="$cache/minimal-test-private/seed" \
  bash scripts/build_plaf203_qemu.sh "$fresh"
sha256sum "$fresh/minimal-output/images/rootfs.squashfs"
```

Package archive hashes are checked against the verified Buildroot `.hash`
records again when recording a completed build, including with reused caches.
Keep both receipts when comparing independent builds. Matching images with the
same container and inputs do not prove reproducibility across different host
environments or independently rebuilt containers. APT snapshot/version closure,
dependency security review and physical-board configuration closure remain
separate requirements. See the [Buildroot offline-build procedure](https://buildroot.org/downloads/manual/manual.html).

The earlier [userspace-only reproducibility receipt](reproducibility-20261029.json) records
two independent source/SDK/build trees with byte-identical rootfs and receipts.
Only verified downloads were reused. The rootfs is 1,331,200 bytes, SHA-256
`12f02aa2db366af2a912f72065ad31adccf1c4b8f432cec756fcc57f3ae76e88`.
The enrollment and seed were newly generated disposable inputs, not device or
personal credentials. Seeded/enrolled images and writable NOR remain private.

### Source-built kernel and QEMU DTB

The [kernel checkpoint](kernel-qualification-20261029.json) supersedes the earlier
retained-kernel dependency, not the historical receipts. The selected revision
was the OpenIPC kernel branch tip on the retained release date. Its configuration
is copied byte-for-byte from
[OpenIPC firmware release source](https://github.com/OpenIPC/firmware/blob/856f22ef7e046f59a3f409e157eb1e06d42fd03c/br-ext-chip-goke/board/gk7205v200/gk7205v200.generic.config)
to [board/linux.config](board/linux.config). The retained image does not establish
its exact source/configuration identity; this is a verified equivalent baseline,
not a claim of bit-for-bit reconstruction of that binary.

Buildroot builds the kernel, source DTB and modules with the existing GCC13.3
musl SDK. Its pinned Linux compatibility hooks handle host-DTC/GCC compatibility;
no local driver rewrite or additional kernel source patch was required.
Buildroot does not automatically invalidate every package stamp when recipes or
sources change. Use a fresh disposable build tree for acceptance; after changing
kernel inputs in a reused tree, explicitly use Buildroot's `linux-dirclean`
before rebuilding. Preserve any local source edits first; the builder does not
silently delete them. Do not edit generated kernel sources as maintained code.
[board/plaf203-qemu.dts](board/plaf203-qemu.dts) includes the pinned vendor board
source and adds only the already-qualified GPIO3 node/alias and disabled GPIO8.
Decompilation against the retained DTB confirms those are the only semantic
changes. This file is **not a physical-board DTB**. The driver derives GPIO
numbers from aliases, not node order.

[post-image.sh](post-image.sh) packages exactly one `zImage + plaf203-qemu.dtb`
in a CRC-checked legacy uImage with header compression `none`, load/entry
`0x40008000` (the zImage itself uses XZ).
This deliberately avoids the vendor `uImage` rule's automatic default-DTB append.
The established U-Boot staging address, bootargs, NOR partitions, `/init`, state
and SSH policy are unchanged. No prebuilt kernel or DTB is read by this build.

`minimal-output/images/` contains `zImage`, `plaf203-qemu.dtb`, `zImage+dtb`,
`uImage`, `kernel.config`, `Module.symvers`, `vmlinux` and the rootfs/receipt.
Buildroot installs 11 source-built modules in the rootfs; essential emulated NOR,
FEMAC, GPIO and RTC operation is built in. The integrated test additionally loads
and unloads `tun`; this does not qualify physical USB/radio/media drivers or OEM
module ABI compatibility. The matching ELF/config/symbols support later debugging
and driver builds without retrieving an unrelated kernel build directory.

Two separate offline kernel builds with no shared kernel objects produced
identical kernel, DTB, uImage, ELF, configuration, ABI symbols, all modules, rootfs
and receipts. Qualified userspace/host-tool outputs were reused from E14; this
is independent **kernel** build reproducibility, not another fresh userspace or
container reconstruction. Kernel and module release remains `4.9.37`.

The effective configuration retains stack-protector-strong and strict devmem.
The upstream base configuration and historical E15 checkpoint have
`CONFIG_WATCHDOG` disabled; it was not disabled to pass tests. The later runtime
milestone adds the capability fragment described above. A qualified hardware
watchdog binding/reset policy remains required before feeder operation.
No emulator watchdog-reset suppression is used here.
Legacy compiler warnings and Linux 4.9 security/support debt remain review gates.

For a source-built NOR regression, add
`--kernel-image "$cache/minimal-output/images/uImage"` to the command below.
The harness validates the receipt, component hashes, header/payload CRCs, load
addresses and exact single-DTB layout, then uses that image **without DTB edits**.
Without the option, the former retained-kernel patching test remains available.
The source-built test requires no `qemu-boot/uImage.gk7205v200` file.

Without `PLAF203_ENROLLMENT_PUBKEY`, the image **does not enable SSH**. Post-build
removes stale enrollment/seed from a previous target tree before applying inputs.
Only the public key enters the image. The optional random seed is a private,
per-experiment QEMU bootstrap input, **not a reusable product entropy seed**.
Never publish these seeded development images or their writable NOR captures.

## Actual NOR-rootfs qualification

The isolated QEMU Ethernet profile sets a fixed locally administered development
MAC in `overlay/etc/network/interfaces` before DHCP. The vendor DT contains a
zero MAC, so Linux otherwise generates a new identity on each boot. Extended
testing found that warm reboot reached `PLAF203_SSH_READY` but lost forwarded
SSH reachability; selecting the stable MAC restored reconnection. Earlier E15
software-reboot evidence checked the serial startup marker, not this reconnect.
This shared synthetic MAC is only for unbridged, isolated emulation, **not a
physical-device identity or production network policy**. The runtime has no
network-readiness prerequisite; the pinned DHCP client backgrounds without a
lease. Do not introduce blocking optional-service initialization as a prerequisite
to essential control initialization.

The harness refuses a private test key with group/other mode bits; inherited
cache ACLs can broaden new files despite a restrictive umask. Keep the containing
test directory private and verify key mode `0600`. Generated unauthorized keys
are explicitly restricted too, so rejection tests reach the guest rather than
failing OpenSSH's local permission check. For non-root containers, supply a
disposable `/etc/passwd` entry matching the numeric UID; `ssh-keygen` requires
it. No host passwords or production account data are needed. SSH-readiness
timeouts preserve the last client error privately in `ssh-readiness.stderr`.
Negative-image compression is bounded to two workers and uses `-all-root`, so
non-root qualification preserves target ownership without exhausting the
container process limit. These are host fixture rules, not relaxed guest checks.

```sh
run="$(mktemp -d "$cache/native-qualification.XXXXXX")"
setfacl -b -k "$run" # discard inherited cache ACL defaults, only on this new dir
printf 'root:x:0:0:root:/root:/bin/sh\nqualifier:x:%s:%s:Qualifier:/tmp:/bin/sh\n' \
  "$(id -u)" "$(id -g)" > "$run/passwd"
reconstruction="${PLAF203_RECONSTRUCTION_ROOT:-/srv/dev/repos/plaf203-firmware-reconstruction}"
qemu_build="${PLAF203_QEMU_BUILD_DIR:-$PWD/emulation/qemu/build}"
test -x "$qemu_build/qemu-system-arm"
docker run --rm --network none --user "$(id -u):$(id -g)" \
  --cap-drop ALL --security-opt no-new-privileges --read-only --ulimit core=0 \
  --pids-limit 64 --memory 512m --cpus 2 --tmpfs /tmp:rw,nosuid,nodev,size=32m \
  -v "$run:/qualified-cache:rw" -v "$run/passwd:/etc/passwd:ro" \
  -v "$PWD/scripts:/work/scripts:ro" \
  -v "$qemu_build:/qualified-cache/qemu-bench/qemu-src/build:ro" \
  -v "$cache/minimal-output/images:/images:ro" \
  -v "$cache/minimal-test-private/auth:/test-key:ro" \
  -v "$reconstruction/inputs/original/oem-flash-backup-aug18/mtd0.bin:/verified-boot.bin:ro" \
  plaf203-qemu-bringup:review \
  python3 /work/scripts/qualify_qemu_gk7205v200.py --cache-root /qualified-cache \
    --kernel-image /images/uImage --minimal-rootfs /images/rootfs.squashfs \
    --test-key /test-key --bootloader /verified-boot.bin \
    --runtime-checks --feeding-checks --enrollment-negative-tests --output-json /qualified-cache/result.json
```

Use the matching disposable private key if building in a second cache; it is
not copied into the firmware build output. The command needs host ACL utilities.

The synthetic 32 MiB layout is **512K boot / 512K env / 4M kernel / 16M rootfs /
11M state**, using erased-FF NOR, read-only SquashFS root and JFFS2 state. This
single-slot layout is an **emulation test fixture**, not an OEM installation,
rollback, calibration-preservation or production partition decision.

The bootloader input must be the verified OEM 512 KiB `mtd0` capture, SHA-256
`a41b42026e22655b2c6f7c874bdfca3686106b3b7a285cd7948ad83869282bb0`;
adjust the read-only mount to its protected acquisition location. The harness
copies it into a new private NOR image and creates a **synthetic**, CRC-valid
256 KiB environment. It never modifies the capture or an existing experiment.

With `--bootloader` there is **no `-kernel`, `-append` or `-initrd`**: the model
boots SPL/U-Boot, which probes NOR and loads/CRC-checks the kernel at `0x42000000`;
Linux mounts rootfs/state through the FMC/NOR driver. Omitting `--bootloader`
retains the RAM-loaded-kernel/NOR-rootfs diagnostic mode; do not conflate them.
Use **device-level** FMC `flash-file` as implemented by the harness. The pinned
model's **machine-level** `flash-file` guesses NAND when the file exceeds 16 MiB,
sets NAND boot straps, and fails to read this NOR environment. The model also
protects detected SquashFS blocks against flash writes; OTA/erase/failure tests
will need separate qualification of that emulator behavior.

Both the retained and source-built kernels append `init=/init`; our small
`/init` delegates to normal BusyBox init,
not OpenIPC's camera/overlay bootstrap. Observed memory remains 48 MiB OS / 16 MiB
MMZ, not the OEM 45/19 split. GPIO bank 8 remains unmodeled; no feeder actuation,
Wi-Fi, ADC, camera or physical flash behavior is qualified.

The harness asserts three fresh-process cold boots, one software reset, retained state and SSH host
identity, DHCP, read-only root, public-key authentication, unauthorized-key and
password-method rejection. It then corrupts the **synthetic** schema and checks
that SSH stays disabled, and boots missing/malformed enrollment fixtures. In
bootloader mode it corrupts the **synthetic** environment CRC and verifies the
captured U-Boot's warning/default-environment/monitor boundary. That is emulation
evidence, not a physical failure-injection or restoration result.
Experiment directories/logs are retained privately in the cache, including on
failure. No live feeder connection occurs. Published summary records should
contain only hashes, versions, observations and sanitized results—not private keys
or full NOR images. The stock OpenIPC baseline remains available by omitting
`--minimal-rootfs`; its reports now fail the command when required checks fail.

## State and trust boundaries

`S20state` mounts only the development state partition and refuses unknown
schemas/nonempty uninitialized state. It never erases or reformats a failed
partition. `seedrng` rotates persistent entropy; the image seed is copied only
when creating fresh state. `S50sshd` requires successful state startup and valid
development enrollment, persists/checksums its host key and authorized key, and
uses `-s -g -j -k` (no password authentication or forwarding). Missing/corrupt
identity fails closed instead of silently rotating identity. Root password login
and getty are disabled. Interrupted enrollment may require offline recovery;
automatic repair, production enrollment/revocation, wear/power-loss testing,
signed OTA/rollback and physical boot/installation remain follow-up work.

The [qualification receipt](qualification-20261028.json) records accepted image
and source hashes, guest observations and validation scope. Final experiment
NOR files intentionally contain injected faults; they are **not deployment
artifacts**. Each harness run constructs a new image from the verified inputs.
The pinned original OpenIPC kernel/rootfs inputs are also preserved outside the
disposable cache under `/srv/dev/artifacts/plaf203-openipc-856f22e/` with hashes;
the kernel/rootfs release must match the recorded digests, not a rolling download.

The [2026-10-29 continuation receipt](reproducibility-20261029.json) supersedes
only the earlier fresh-build limitation: two independent userspace builds are
verified, and the new image passes the same NOR lifecycle/security checks with
the qualified `a2d54f7b…` emulator. The tested binary is the relocated
`emulation/qemu/build/qemu-system-arm`, mounted read-only at the runner's existing
cache-layout path inside the offline container. Current script defaults still
use that cache layout; do not mistake a generic MCP launch for this qualification.
Full sanitized build/qualification receipts are also retained at
`/srv/dev/artifacts/plaf203-native-userspace/20261029/`. Private build images,
test credentials and experiment NOR files remain outside Git.
