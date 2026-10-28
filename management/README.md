# Petlibro Management application

This directory contains the browser, TypeScript server, and native capability
worker for Petlibro Management. It is independently runnable and
packaged into the Home Assistant add-on. Existing AppDaemon and go2rtc services
remain the production operational authorities.

The application implements cohesive vertical backend paths and presents their real
state across the management route inventory:

```text
Overview page
  -> TanStack Query browser client
  -> GET /api/v1/system/status
  -> HTTP route adapter
  -> SystemStatusService
  -> source-aware management status contract

Operations page
  -> accepted State Agent inspection or guided-installation action
  -> durable TypeScript OperationManager + SQLite journal
  -> versioned HTTP/JSON over a private Unix socket
  -> independent native-TypeScript capability worker
  -> allowlisted read-only or qualification-gated bootstrap capability
  -> structured result/evidence
  -> HTTP snapshot/history + reconnectable browser WebSocket notification

Overview / Devices / Diagnostics
  -> GET /api/v1/devices/overview
  -> DeviceOverviewService
  -> Supervisor-owned options + AppDaemon discovery/controller observations
  -> go2rtc physical-session status + bounded MQTT authentication probe
  -> source-, freshness-, and verification-aware device projection

Setup
  -> POST /api/v1/setup/preflight
  -> bounded input validation + SSH recovery-key shape validation
  -> final-broker MQTT authentication + transient TCP/1883 ownership check
  -> one durable installation across operator-mediated broker handoffs
  -> protected payload/identity/configuration custody
  -> broker-bound guarded OTA and source-aware operational verification

Events / Diagnostics
  -> bounded query over durable SQLite operation evidence
  -> recursive secret-field redaction + filters + JSON export
  -> source-backed diagnostic report with explicit limitations

Live raw logs
  -> on-demand Supervisor combined add-on stream over WebSocket
  -> backend normalization/redaction + bounded browser-session retention
  -> no automatic copy into the durable operation journal
```

The system endpoint reports management and worker transport readiness. The
device overview separately reports operational services and correlated devices;
neither management nor worker readiness is presented as feeder, broker,
controller, State Agent, or camera health.

## Technology baseline

- Node.js 24, pinned through the repository's `mise.toml`;
- npm with a committed lockfile;
- strict TypeScript and React 19;
- TanStack Start in SPA mode, Router, and Query;
- Vite, Tailwind CSS 4, shadcn/ui source components, and Lucide icons;
- Nitro's Node server preset for the current standalone production build;
- Node 24's built-in SQLite API for the durable operation journal;
- Biome for formatting and linting;
- Vitest, jsdom, and Testing Library for deterministic tests.

The packaged Nitro runtime listens on a private Unix socket behind Nginx. The
native capability worker uses a separate mode-`0600` Unix socket and a separate
durable invocation database.

Operation snapshots are created only for explicit management intent: the
guided installation and operator-requested State Agent inspection currently
create them. Polling, service observations, MQTT telemetry, routine feeder
activity, and raw logs do not. Installation admission queries all authoritative
unresolved snapshots without a display/history limit; the normal operation list
remains bounded for presentation.

The browser log explorer retains up to 10,000 records but renders at most the
latest 500 matching rows at once. Regular expressions use a deliberately
conservative subset (no lookaround, backreferences, counted quantifiers,
repeated groups, or multiple unbounded quantifiers) and inspect at most the
first 4 KiB of each already transport-bounded record. Export still includes all
matching retained records. These limits keep investigation reversible without
allowing an adversarial expression or DOM volume to monopolize the UI thread.

## Source organization

```text
src/
  application/       # Framework-independent application services
  components/        # React layout, page, shared, and shadcn UI components
  domain/             # Shared semantic contracts and boundary validation
  infrastructure/    # Browser/transport adapters
  routes/             # Thin TanStack page and HTTP route adapters
  server/             # Explicit server composition root
  test/               # Shared test setup
worker/               # Independently bundled TypeScript capability process
server/routes/        # Nitro WebSocket delivery route
```

These are responsibility boundaries rather than a requirement to add a folder
or interface for every planned subsystem. The operation journal, worker IPC,
installation coordinator, and event delivery support concrete capabilities;
there is no speculative general workflow or plugin framework.

Device-changing bootstrap is fail-closed unless the add-on option
`management_bootstrap_device_changes` is explicitly enabled. That option is a
qualification gate, not evidence of real-feeder or same-host broker-handoff
qualification. The coordinator uses durable operator checkpoints because the
add-on does not request broad Supervisor service-control permissions.

## Development

From the repository root:

```bash
mise install
cd management
npm ci
npm run dev
```

The development server defaults to `http://localhost:3000`. The shell still runs
without the worker, add-on image, feeder, AppDaemon, go2rtc, Mosquitto, or State
Agent. In that state, worker and action availability are explicitly unavailable.

To exercise the capability locally, use two terminals from `management/` and
provide a real protected token only through the environment:

```bash
# Terminal 1
PETLIBRO_CAPABILITY_WORKER_SOCKET=.data/worker.sock npm run build:worker
PETLIBRO_CAPABILITY_WORKER_SOCKET=.data/worker.sock npm run worker

# Terminal 2
export PETLIBRO_MANAGEMENT_DATA_DIR=.data
export PETLIBRO_RUNTIME_DATA_DIR=/path/to/synthetic-or-mounted-addon-data
export PETLIBRO_CAPABILITY_WORKER_SOCKET=.data/worker.sock
export PETLIBRO_STATE_AGENT_URL=http://FEEDER_IP:8765
export PETLIBRO_STATE_AGENT_TOKEN='replace-with-protected-token'
npm run dev
```

Do not copy `.env.example` to a tracked file containing real values. The worker
socket and SQLite database are under ignored `.data/` by default.

Available checks:

```bash
npm run format:check
npm run lint
npm run check
npm run typecheck
npm test
npm run test:worker
npm run build
```

`npm run format` updates files in place. `npm run generate-routes` regenerates
`src/routeTree.gen.ts`; the Start/Vite plugin also maintains that generated file
during development and builds.

## Production build

```bash
npm run build
npm start
```

The Node build is emitted under `.output/`. `PETLIBRO_MANAGEMENT_VERSION` may be
set at runtime to expose packaged version metadata through the status endpoint.

Set `PETLIBRO_MANAGEMENT_BASE_PATH` at build time to prepare route and asset URLs
for a prefixed browser path, for example:

```bash
PETLIBRO_MANAGEMENT_BASE_PATH=/petlibro/ npm run build
```

Root-path and runtime-variable prefix support is implemented. Browser WebSocket
reconnect uses a durable event cursor and HTTP snapshot/history as the recovery
source. Container tests cover direct and simulated Ingress HTTP, deep links,
assets, API calls, and WebSocket upgrades. Installation in a real Home Assistant
Supervisor has also exercised the Ingress route successfully.

## Implemented and deferred boundaries

Implemented now:

- responsive desktop and narrow/Ingress application shell, route-aware
  navigation, light/dark browser preference, and active-operation indicator;
- purpose-built Overview, Devices, Setup, Actions, Operations, Diagnostics,
  Logs, History, and Settings layouts;
- honest capability-gap and first-run states for pages whose source adapters or
  operation contracts have not been migrated;
- reusable service availability, observation provenance, action availability,
  operation status/list, execution evidence, and empty-state presentation;
- accessible loading, API-error, not-found, and reduced-motion behavior;
- source-aware status contract and response validation;
- coherent read-only device overview contract backed by Supervisor options,
  AppDaemon discovery/controller status, go2rtc runtime status, and a bounded
  MQTT CONNECT/CONNACK authentication probe;
- correlated Devices view with masked camera UID, discovered address, feeder
  MQTT activity, reconciliation state, State Agent-verified revision/plan
  summary, physical camera session state, resolution, and health counters;
- thin versioned HTTP adapter and explicit application-service composition;
- private versioned native-TypeScript worker with durable typed invocation
  deduplication and explicit no-replay restart reconciliation;
- real authenticated State Agent health inspection with bounded safe results;
- byte-exact native OEM provisioning, MQTT packet, and OTA command primitives
  covered against the standalone installer's reference vectors;
- image-built, provenance-checked ARM inputs, device-bound payload assembly,
  protected artifact/credential custody, temporary MQTT identity capture,
  final-broker proof, fresh-session firmware admission, guarded OTA, and
  post-transition verification;
- one durable installation workflow with serialized/idempotent checkpoints,
  restart-driven progression, exclusive active-installation ownership,
  explicit indeterminate outcomes, and no automatic replay of uncertain OTA;
- protected no-store configuration handoff and source-backed completion checks
  requiring effective options, captured product/serial correlation, fresh
  device discovery, and fresh AppDaemon READY reconciliation rather than
  treating a checkpoint click or reused IP address as proof;
- durable operation snapshots and globally sequenced events in SQLite, plus
  complete sequence-checked worker semantic evidence independent of ephemeral
  raw-log retention;
- restart reconciliation without blind capability replay;
- HTTP operation snapshot/history and reconnectable WebSocket notifications;
- source-aware Operations UI with selectable durable history, success, failure,
  interruption, result evidence, and cursor-based live updates;
- operation-backed recent activity on Overview and History, plus the real
  State Agent inspection capability on Actions and Diagnostics;
- bounded, filterable, recursively redacted durable operation-event queries and
  downloadable JSON exports, plus a source-backed diagnostic report export;
- on-demand sanitized Supervisor combined-runtime logs over the existing
  WebSocket boundary, with shared source collection, bounded queues/framing,
  explicit drops, and browser-owned filtering, pause, retention, and export;
- root and configurable-prefix build configuration;
- add-on image packaging and independent s6 supervision;
- Supervisor-source-restricted Ingress gateway and opt-in CIDR-restricted
  direct gateway over the same HTTP/WebSocket backend; mutation routes require
  non-simple JSON requests while deployment access remains the authentication
  boundary;
- unit, contract, representative UI, responsive browser, and prefixed-path
  runtime tests.

Intentionally not implemented:

- automatic Supervisor control of Mosquitto or writes to Supervisor-owned
  options; broker transitions and option application remain verified,
  operator-mediated checkpoints;
- independently launchable destructive recovery or a broad maintenance action
  catalog; safe identity/broker-proof re-entry is part of the continuing
  installation, while ambiguous OTA is never retried automatically;
- distinct AppDaemon and go2rtc raw-log streams where no trustworthy separate
  source surface has yet been connected; the current source is honestly
  identified as Supervisor combined add-on output;
- maintenance writes or signed State Agent update submission; existing
  AppDaemon update and persistent-state authorities remain unchanged;
- direct Supervisor/Home Assistant API access, service control, or option writes;
- routine feeder controls, arbitrary shell/device commands, or fabricated demo
  data;
- a current Home Assistant health heartbeat (only historical birth events are
  available from AppDaemon without broader API permissions);
- qualified multi-interface feeder-facing address selection beyond the current
  explicit Home Assistant host-network address constraint;
- application authentication or TLS;
- live qualification of the current guided bootstrap against Home Assistant
  Supervisor, same-host broker handoff, and a physical feeder. Earlier
  management builds exercised Ingress, but this prerelease remains a controlled
  qualification channel rather than a hardware-qualified release.

Authentication is intentionally outside this application's scope. Ingress is
isolated to the Supervisor proxy. Direct access is disabled by default and must
be restricted to a trusted LAN or an externally authenticated reverse proxy;
the app must never be published directly to the Internet. The worker socket is
never proxied to a browser.

See [worker/README.md](worker/README.md) for the current wire contract,
credential handling, lifecycle semantics, and migration provenance.

See the maintained [architecture coverage map](../docs/petlibro-management-architecture-coverage-map.md),
[migration sequence](../docs/petlibro-management-migration-implementation-sequence.md),
[implementation guide](../docs/petlibro-management-implementation-guide.md),
and proposed [UI concept plan](../docs/petlibro-management-ui-concept-plan.md)
for target responsibilities, visual direction, and subsequent work. The UI
concept is design guidance rather than evidence that its illustrated runtime
states or controls are implemented.

The [PLAF203 replacement-firmware architecture centerline](../docs/plaf203-replacement-firmware-architecture.md)
defines the proposed native-device integration and device-side authority without
replacing these management foundations or claiming a qualified firmware cutover.
