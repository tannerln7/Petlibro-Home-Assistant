# Petlibro Management application

This directory contains the initial browser and TypeScript server foundation for
the planned Petlibro Management application. It is independently runnable and
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
  -> accepted State Agent inspection action
  -> durable TypeScript OperationManager + SQLite journal
  -> versioned HTTP/JSON over a private Unix socket
  -> independent native-TypeScript capability worker
  -> authenticated, read-only State Agent /health request
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
  -> explicit same-host handoff block without service reconfiguration

Events / Diagnostics
  -> bounded query over durable SQLite operation evidence
  -> recursive secret-field redaction + filters + JSON export
  -> source-backed diagnostic report with explicit limitations

Live raw logs (not yet connected)
  -> future on-demand source subscriptions over the browser transport
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
or interface for every planned subsystem. The operation journal, worker IPC and
event delivery exist because the State Agent capability exercises them; there is
no speculative general workflow or plugin framework.

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
- durable operation snapshots and globally sequenced events in SQLite;
- restart reconciliation without blind capability replay;
- HTTP operation snapshot/history and reconnectable WebSocket notifications;
- source-aware Operations UI with selectable durable history, success, failure,
  interruption, result evidence, and cursor-based live updates;
- operation-backed recent activity on Overview and History, plus the real
  State Agent inspection capability on Actions and Diagnostics;
- bounded, filterable, recursively redacted durable operation-event queries and
  downloadable JSON exports, plus a source-backed diagnostic report export;
- root and configurable-prefix build configuration;
- add-on image packaging and independent s6 supervision;
- Supervisor-source-restricted Ingress gateway and opt-in CIDR-restricted
  direct gateway over the same HTTP/WebSocket backend;
- unit, contract, representative UI, responsive browser, and prefixed-path
  runtime tests.

Intentionally not implemented:

- complete native installer transaction orchestration beyond the tested OEM
  protocol primitives and State Agent health probe;
- artifact/payload custody, temporary MQTT identity capture, source-restricted
  artifact serving, correlated OTA/recovery transactions, final-broker
  handoff, post-install adoption, and capability-specific cancellation;
- operator checkpoints or full action catalog/schema forms;
- maintenance writes or signed State Agent update submission; existing
  AppDaemon update and persistent-state authorities remain unchanged;
- on-demand operational-service stdout/stderr adapters and the browser-retained
  multi-source raw-log explorer; Events & Logs currently presents durable
  management/capability evidence in a separately identified section only;
- direct Supervisor/Home Assistant API access, service control, or option writes;
- routine feeder controls, setup execution, or fabricated demo data;
- a current Home Assistant health heartbeat (only historical birth events are
  available from AppDaemon without broader API permissions);
- application authentication or TLS;
- live Home Assistant Supervisor qualification for this uncommitted native-worker
  change set (the preceding management release was exercised through Ingress).

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
