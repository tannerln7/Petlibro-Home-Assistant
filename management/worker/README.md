# Petlibro Management capability worker

The worker is an independently supervised native-TypeScript service for
allowlisted technical capabilities. It is not a browser API or application
workflow coordinator. The standalone Python installer remains an independent,
supported reference implementation and is never imported or executed here.

The initial active capability, `state-agent.inspect-health.v1`, performs one
authenticated, read-only State Agent `/health` request. Native byte-exact OEM
provisioning/MQTT/OTA protocol primitives also have parity tests, but no
device-changing bootstrap action is exposed until the complete temporary
broker/OTA resource-owning session has been ported and qualified.

## Private transport

The worker serves versioned HTTP/JSON on a Unix domain socket. Its parent
directory is mode `0700` and the socket is mode `0600`; it opens no TCP
listener.

| Method and route | Meaning |
| --- | --- |
| `GET /v1/health` | Worker instance, implementation version and protocol compatibility |
| `GET /v1/capabilities` | Exact allowlisted capability IDs and execution characteristics |
| `PUT /v1/invocations/{uuid}` | Idempotently accept one typed capability invocation |
| `GET /v1/invocations/{uuid}` | Reconcile invocation state, result, error and replayable events |

There is no arbitrary command, shell, import, URL, or path execution endpoint.
The State Agent bearer token is used only for the outgoing request and is not
stored in invocation snapshots, SQLite, events, errors, or browser responses.
Redirects are rejected and responses are bounded to 64 KiB.

## Lifecycle and durability

Invocation acceptance, snapshots, terminal evidence, and a bounded event
replay are stored in `/data/management/capability-worker.sqlite`. Reusing an
invocation ID with the same request is idempotent; changing its request is a
conflict. If the worker restarts with an accepted/running invocation, it
durably records an interrupted failure and does not replay the request. The
TypeScript application coordinator reconciles that result into its separate
operation journal.

`workerInstanceId` identifies the process that originally accepted and owned
the invocation. Restart reconciliation preserves that value and records the
new process separately as `reconciledByWorkerInstanceId`, including both IDs in
the interruption evidence. These additive protocol-v1 fields preserve execution
provenance without treating reconciliation as ownership transfer.

This worker process owns technical execution only. The application coordinator
owns user-facing operations, checkpoints, scheduling, and browser delivery.
Future device-changing capabilities must add capability-specific durable
transaction evidence and safe interruption rules before they are allowlisted.

Build and run from `management/`:

```bash
npm run build:worker
PETLIBRO_CAPABILITY_WORKER_SOCKET=.data/worker.sock \
PETLIBRO_MANAGEMENT_DATA_DIR=.data \
npm run worker
```

The production add-on runs this bundle under its own s6 service. The socket is
never proxied to the browser.
