# Contributing

Contributions should preserve the project's production boundaries and leave a
clear, testable reason for every tracked file.

## Set up

Fork or clone the repository, create a Python virtual environment, install
`addon/appdaemon/requirements-dev.txt`, and install the toolchains needed for
the component you plan to change. The complete prerequisite and command list is
in the [development guide](docs/development.md).

Work on a focused branch and inspect existing code, tests, and component
documentation before changing behavior. Do not commit generated runtime files,
local feeder configuration, compiled binaries, or secrets.

## Where changes belong

- Home Assistant package/runtime integration: `addon/`
- PLAF203 controller and MQTT behavior: `addon/appdaemon/`
- Add-on consumption of the pinned go2rtc source: `addon/go2rtc-source.lock`,
  `addon/Dockerfile`, and `scripts/fetch-go2rtc.sh`
- Camera transport and go2rtc internals: the standalone
  [`tannerln7/go2rtc`](https://github.com/tannerln7/go2rtc) fork
- Feeder state decoder/API and signed updater: `state-agent/`
- Stock-feeder installation/bootstrap: `installer/`
- Local maintenance/build tools: `scripts/` or `docker/`
- Cross-component architecture and user guides: `docs/`
- Component-specific details: the component's own README

Keep tests with their owning component. Introduce shared production code only
when multiple runtime components actually share the responsibility.

## Expected workflow

1. Make the smallest complete change that solves the issue.
2. Preserve public behavior unless the change intentionally updates it.
3. Add focused tests for new behavior and regressions.
4. Update configuration examples and docs in the same change.
5. Run focused checks while iterating.
6. Run `./scripts/validate.sh` before submitting.
7. Inspect the final diff for unrelated files, generated output, or secrets.

If your environment lacks a release toolchain, run every available focused
check and state exactly what was not validated.

## Testing expectations

- Controller changes need deterministic Python tests, including stale and
  failure paths for asynchronous operations.
- Camera protocol changes need focused Go parser/transport tests.
- State Agent changes need host integration tests against synthetic file roots;
  update changes also need interrupted transaction/rollback coverage.
- Installer changes need payload/transaction tests and must not require a live
  feeder for routine validation. Keep downloads, toolchains, compiler caches,
  generated keys, credentials, and payloads under ignored `build/bootstrap/`.
- Packaging/configuration changes need renderer and Compose/image checks.

Do not use sleep-based race tests when explicit generations, callbacks, clocks,
or injected dependencies can make ordering deterministic.

## Documentation expectations

The root README is the user entry point. Keep exhaustive details in the owning
component README or an appropriate file under `docs/`, and link rather than
copying sections. Use consistent names: **Home Assistant add-on**, **PLAF203
controller**, **State Agent**, and **installer/bootstrap**.

Update documentation when commands, paths, configuration, runtime ownership,
security boundaries, or supported behavior changes. Remove obsolete
instructions instead of preserving them as undocumented alternatives.

## Reverse engineering and protocol evidence

Raw captures, firmware binaries, device dumps, Ghidra projects, decompiler
exports, arbitrary-code proof payloads, live logs, and exploratory scripts do
not belong in the production repository. Keep them in an access-controlled,
untracked research workspace.

Useful discoveries should reach the repository as:

- sanitized synthetic fixtures;
- regression tests;
- protocol constants/types in the owning implementation; and
- concise maintenance documentation explaining the verified conclusion.

Never submit factory credentials, account/member IDs, camera secrets, IP/MAC
addresses tied to a real user, State Agent tokens, SSH keys, or signing keys.
If evidence cannot be sanitized without losing its purpose, describe how an
authorized maintainer can reproduce it instead of attaching it.

## Security-sensitive changes

Changes involving OEM OTA, broker credential capture, persistent feeder writes,
State Agent authentication/update, firmware slots, or key management require
explicit negative-path tests and documentation of recovery behavior. Never
weaken source-IP restrictions, signature checks, input validation, rollback, or
least-privilege broker ACLs merely to simplify setup.

Only test against devices and networks you own or are authorized to modify.
