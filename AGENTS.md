# Petlibro-Home-Assistant — Repository Instructions

These instructions supplement the user's global `AGENTS.md`. They describe this repository's domain, architecture, implementation direction, and validation procedures. Do not duplicate global communication, tool-use, permission, or Git-safety rules here.

## Project and source layout

Petlibro Local provides local Home Assistant control and camera streaming for the **Petlibro PLAF203**. Its existing components are functional and their behavior is the reference for migration:

| Location | Responsibility |
| --- | --- |
| `installer/` | Guided no-UART bootstrap: OEM provisioning, temporary MQTT broker, identity capture, guarded OTA, payload preparation, final-broker migration |
| `state-agent/` | Feeder-resident C agent; authenticated, allowlisted state inspection and signed update mechanism |
| `addon/appdaemon/` | Python routine feeder controller: MQTT, HA entities, discovery, telemetry, reconciled state, verified writes, updates |
| `addon/go2rtc/` | Maintained Go camera transport integration, streaming and camera-session status |
| `addon/` | HA add-on packaging, configuration rendering, s6 supervision, image build |
| `docs/` | Architecture, development, protocol, installation, component, and management-planning documents |
| `docker/`, `scripts/` | Local development/runtime helpers, packaging and validation |

The existing add-on is the production operational system; **Petlibro Management** is an implemented expansion whose individual capabilities still have explicit qualification states. Do not describe unqualified bootstrap, recovery, Supervisor control, or future controller migration as production-ready. The initial supported guided deployment is **one feeder per add-on**; do not silently expand that support.

## Maintained references and decision authority

- [`docs/architecture.md`](docs/architecture.md): existing operational topology, authority, and device invariants.
- [`docs/development.md`](docs/development.md): existing build, tests, source conventions and research workflow.
- [`docs/petlibro-management-architecture-coverage-map.md`](docs/petlibro-management-architecture-coverage-map.md): **what** owns behavior, state, lifecycles, and interactions.
- [`docs/petlibro-management-migration-implementation-sequence.md`](docs/petlibro-management-migration-implementation-sequence.md): **when** to define, migrate, integrate and validate work across synchronized tracks.
- [`docs/petlibro-management-implementation-guide.md`](docs/petlibro-management-implementation-guide.md): **how** management subsystems should be shaped; stack, pages, contracts, style, security, and quality requirements.

For initial management-architecture onboarding, read the three management documents in full. For later bounded tasks, consult the affected sections and related source/tests; do not repeatedly ingest all three documents without need. Consult the component-specific README/docs when touching that component.

The planning documents distinguish **established**, **planned**, **candidate**, **requires validation**, and **deferred** decisions. Preserve those distinctions. Actual code, tests, observed device behavior, and proved safety invariants supersede speculative assumptions. Surface material document conflicts instead of quietly choosing an interpretation; update the appropriate maintained source after resolving them.

## Management architecture and ownership

The target is **one integrated HA add-on** with a browser management application, independently supervised existing services, and an independently supervised native-TypeScript capability worker. The standalone Python installer remains supported and is the behavioral reference, but it is not a management runtime dependency.

Keep these logical regions distinct; they are **responsibility boundaries, not mandatory packages/classes/processes**:

- **Presentation:** React UI, navigation, inputs and visual state; no authoritative jobs or feeder state.
- **Application coordination:** actions, user intent, workflows, durable job/checkpoint lifecycles and scheduling; no OEM protocol internals.
- **Device capabilities:** technical execution, protocol constraints, live sockets/sessions, device-changing transaction integrity, and correlated evidence.
- **Runtime integrations:** observation and authorized operations against AppDaemon, go2rtc, State Agent, broker, and Supervisor; existing services retain authority.
- **Domain contracts:** shared types, requirements, results, errors and capability interfaces independent of Python/Node/framework mechanics.
- **Persistence/custody:** durable records, protected artifacts/secrets and retrieval; storage does not become authority over external truth.
- **Observability:** structured source-tagged events, progress, logs, telemetry and history; observations do not confer authority.
- **Platform:** hosting, process supervision, network/authentication boundaries and lifecycle isolation.

**Intent flows down; results/evidence flow up; authoritative state stays with its owner.** Distinguish desired, execution, authoritative, observed, historical and cached state. Projections must preserve source, freshness, scope and validity. A healthy management process is not proof that the feeder, controller, broker, or camera is healthy.

## Management implementation conventions

- **Frontend:** strict TypeScript, React, TanStack Start in SPA mode, TanStack Router and Query, Tailwind CSS, shadcn/ui and Lucide. RJSF/`@rjsf/shadcn` and TanStack Table/Virtual remain candidates where actual action/table requirements justify them. Use the implementation guide for page and interaction goals.
- **Backend:** conventional object-oriented TypeScript where beneficial: cohesive services/classes, explicit dependencies, descriptive methods, typed interfaces at genuine boundaries, and composition. Keep business/domain logic separate from React and web-framework handlers. Avoid generic DI frameworks, empty class/interface forests, and speculative feature scaffolding.
- **Capability migration:** implement management capabilities natively in TypeScript behind stable contracts. Preserve the proven Python installer as an independent CLI and behavioral reference; management must not import or execute it. Stateful resource ownership remains exclusive.
- **Worker and streaming:** the native-TypeScript worker is an independent long-lived s6 service using versioned HTTP/JSON over a mode-`0600` Unix socket. Browser live events and ephemeral logs use WebSockets alongside HTTP snapshots/history. Preserve correlated evidence, restart provenance, no-blind-replay behavior, bounded lossy raw-log transport, and durable critical outcomes.
- **Operations:** distinguish user-facing job, capability invocation, live-resource session, and actual device transaction. Do not blindly replay an ambiguous device-changing operation after restart. Cancellation is a request subject to capability-defined safe interruption points.
- **UI structure:** one action model reusable across pages, source-aware device projections, accessible responsive design, useful empty/error/loading states, and expandable live operation details. Do not implement fake device status or controls that appear functional but are not.

Establish contracts from both sides before independently implementing producers and consumers. For sizeable assignments, identify the owning region, inputs/outputs, resource/state owner, dependencies, exclusions, tests, and completion evidence. Implement concrete working slices rather than placeholder infrastructure.

## Existing-device and service invariants

- **Persistent feeder state:** OEM persistent files are truth, read through the authenticated State Agent; AppDaemon owns reconciliation and the verified write path. Preserve fresh preflight, command correlation, post-write revision/value verification, complete feeding-plan preservation, and existing HA MQTT identifiers/topics. MQTT PUBACK or retained HA state is **not** proof of a persistent write. The management UI must not introduce a second routine writer.
- **Live dispensing:** retain the existing solicited `motorState` and ordered grain-event logic; avoid treating file-backed motor state or retained historical feed observations as live actuator truth.
- **State Agent:** fixed allowlisted files/routes, exact shape validation, snapshot-consistent state/revisions, source-IP plus bearer authentication, and tightly constrained signature-verified updates; no generic remote shell/path API. Preserve update probation/rollback and transaction locking.
- **Camera:** go2rtc owns transport, physical viewing session and media status. Preserve the camera adapter/transport/producer boundaries and established ACK, ordering, SPS/keyframe, timestamp and stop semantics. Management observes status; it must not duplicate camera transport or proxy video through the event journal.
- **Installer:** stock PLAF203 bootstrap uses OEM UDP provisioning and a source-restricted temporary MQTT listener. Feeder-facing MQTT is **TCP 1883** on supported firmware. Keep OTA message/session-generation correlation, restricted artifact delivery, OEM firmware preservation, mandatory SSH recovery, final-broker credential proof **before** OTA, and final endpoint restoration/heartbeat observation. Avoid destructive or falsely successful schedule responses. Treat same-host Mosquitto/temporary-broker handoff as **unresolved until validated**, not as a solved implementation detail.
- **Runtime isolation:** AppDaemon remains the authoritative routine controller and go2rtc remains independently supervised. The management UI must remain available before successful feeder setup and when operational services are unhealthy. Future AppDaemon-to-TypeScript replacement requires a deliberate single-writer cutover, not competing controllers.
- **Configuration:** HA add-on options are the current authority; `addon/render_config.py` validates `/data/options.json` and atomically renders runtime config. When changing existing options, update `addon/config.yaml`, renderer, templates, Compose configuration, docs, and tests consistently. Supervisor permissions, apply/restart semantics and integration mechanisms must be verified before use.
- **Security/deployment:** add-on uses `host_network: true`. HA Ingress is the target default browser entry, but the same app must support appropriately authenticated direct/reverse-proxied access. Do not expose unauthenticated management APIs, worker RPC, or device-control listeners. Keep recovery private keys, factory MQTT accounts, State Agent tokens, signing material, raw captures, feeder dumps and installer output out of Git and user-visible logs.

## Worktrees, provenance and migration discipline

Management development may occur in a worktree separate from `develop`; the original working tree can contain additional uncommitted fixes not present in the committed branch. Work only in the active authorized worktree. Before extracting or porting existing behavior, identify the exact source revision and reconcile relevant unpublished changes deliberately; never assume a feature branch contains them.

Keep existing production components intact during initial scaffolding. Port cohesive capabilities behind tested contracts instead of restructuring files by language alone. Use sanitized byte-exact protocol fixtures and behavioral/parity tests. Changing source behavior is different from mechanical extraction; record and validate either explicitly.

## Development and validation

Use `docs/development.md` for prerequisites and the canonical current commands. From the repository root:

```bash
./scripts/validate.sh
```

Focused existing checks (choose according to impact):

```bash
python3 -m pytest addon/tests addon/appdaemon/tests -q
python3 -m pytest installer/tests -q
python3 -m pytest state-agent/tests -q
make -C state-agent clean all
make -C state-agent clean arm-release
(cd addon/go2rtc && go test ./pkg/petlibro ./cmd/petlibro-resolve)
(cd addon/go2rtc && go vet ./pkg/petlibro ./cmd/petlibro-resolve)
docker compose --env-file docker/.env.example -f docker/docker-compose.yml config --quiet
```

For add-on packaging, use `./scripts/build-local.sh` when the affected scope warrants it. Use the management app's declared scripts/lockfile after it is scaffolded; do not invent commands before they exist. Deterministic tests and synthetic fixtures are the routine baseline; live feeder actions require explicit task scope and controlled conditions. Record untested Ingress, Supervisor, broker-handoff and hardware behavior as unverified.

Update the owning architecture/implementation document when contracts, lifecycle/authority boundaries or durable decisions change. Keep research captures and device-specific raw evidence outside tracked source; commit only sanitized fixtures and maintainable conclusions.
