import { createHash, randomUUID } from 'node:crypto';
import { chmodSync, mkdirSync, rmSync } from 'node:fs';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { dirname, resolve } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import {
  inspectStateAgentHealth,
  StateAgentInspectionError,
} from '#/infrastructure/capabilities/state-agent-inspector';

const CAPABILITY_ID = 'state-agent.inspect-health.v1';
const PROTOCOL_VERSION = 1;
const WORKER_VERSION = '0.2.0-native';
const MAX_REQUEST_BYTES = 64 * 1024;
const MAX_EVENTS = 128;
const INVOCATION_PATH = /^\/v1\/invocations\/([0-9a-f-]{36})$/;

type Status = 'accepted' | 'running' | 'succeeded' | 'failed' | 'interrupted';
type Severity = 'info' | 'warning' | 'error';

interface WorkerEvent {
  readonly sequence: number;
  readonly type: string;
  readonly occurredAt: string;
  readonly severity: Severity;
  readonly payload: Readonly<Record<string, unknown>>;
}

interface InvocationSnapshot {
  readonly protocolVersion: 1;
  /** The worker process that accepted and owned this invocation. */
  readonly workerInstanceId: string;
  /** The later worker process, if any, that classified an incomplete invocation. */
  readonly reconciledByWorkerInstanceId?: string;
  readonly invocationId: string;
  readonly operationId: string;
  readonly capabilityId: typeof CAPABILITY_ID;
  readonly status: Status;
  readonly stage: string;
  readonly acceptedAt: string;
  readonly updatedAt: string;
  readonly events: readonly WorkerEvent[];
  readonly result?: unknown;
  readonly error?: { readonly code: string; readonly message: string };
}

interface CapabilityInput {
  readonly baseUrl: string;
  readonly token: string;
  readonly timeoutSeconds: number;
}

interface InvocationRequest {
  readonly protocolVersion: 1;
  readonly invocationId: string;
  readonly operationId: string;
  readonly capabilityId: typeof CAPABILITY_ID;
  readonly input: CapabilityInput;
}

interface Row {
  readonly fingerprint: string;
  readonly snapshot_json: string;
}

type Inspector = typeof inspectStateAgentHealth;

export class DurableInvocationManager {
  public readonly instanceId = randomUUID();
  private readonly database: DatabaseSync;

  public constructor(
    databasePath: string,
    private readonly inspector: Inspector = inspectStateAgentHealth,
    private readonly clock: () => Date = () => new Date(),
  ) {
    if (databasePath !== ':memory:') {
      mkdirSync(dirname(databasePath), { recursive: true, mode: 0o700 });
    }
    this.database = new DatabaseSync(databasePath, { timeout: 5_000 });
    this.database.exec(`
      PRAGMA journal_mode = WAL;
      CREATE TABLE IF NOT EXISTS capability_invocations (
        invocation_id TEXT PRIMARY KEY,
        fingerprint TEXT NOT NULL,
        status TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL
      ) STRICT;
    `);
    this.reconcileInterruptedInvocations();
  }

  public capabilities() {
    return [
      {
        id: CAPABILITY_ID,
        contractVersion: 1,
        mode: 'read-only',
        cancellation: 'not-supported-after-start',
        replayAfterRestart: 'never-automatic',
      },
    ];
  }

  public submit(
    invocationId: string,
    value: unknown,
  ): { readonly snapshot: InvocationSnapshot; readonly created: boolean } {
    const request = validateRequest(invocationId, value);
    const fingerprint = hashRequest(request);
    const existing = this.row(invocationId);
    if (existing !== undefined) {
      if (existing.fingerprint !== fingerprint) {
        throw new InvocationConflictError(
          'invocation ID is already bound to a different request',
        );
      }
      return { snapshot: parseStored(existing), created: false };
    }

    const timestamp = this.now();
    const snapshot: InvocationSnapshot = {
      protocolVersion: 1,
      workerInstanceId: this.instanceId,
      invocationId,
      operationId: request.operationId,
      capabilityId: CAPABILITY_ID,
      status: 'accepted',
      stage: 'queued',
      acceptedAt: timestamp,
      updatedAt: timestamp,
      events: [
        {
          sequence: 1,
          type: 'capability.accepted',
          occurredAt: timestamp,
          severity: 'info',
          payload: { capabilityId: CAPABILITY_ID, stage: 'queued' },
        },
      ],
    };
    this.database
      .prepare(
        `INSERT INTO capability_invocations
          (invocation_id, fingerprint, status, updated_at, snapshot_json)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        invocationId,
        fingerprint,
        snapshot.status,
        snapshot.updatedAt,
        JSON.stringify(snapshot),
      );

    queueMicrotask(() => void this.execute(snapshot, request.input));
    return { snapshot, created: true };
  }

  public get(invocationId: string): InvocationSnapshot {
    const row = this.row(invocationId);
    if (row === undefined) {
      throw new InvocationNotFoundError();
    }
    return parseStored(row);
  }

  public close(): void {
    this.database.close();
  }

  private async execute(
    accepted: InvocationSnapshot,
    input: CapabilityInput,
  ): Promise<void> {
    let current = this.transition(
      accepted,
      'running',
      'requesting-state-agent-health',
      'capability.started',
      'info',
      { stage: 'requesting-state-agent-health' },
    );
    try {
      const result = await this.inspector(
        input.baseUrl,
        input.token,
        input.timeoutSeconds,
      );
      current = this.transition(
        current,
        'running',
        'health-observed',
        'capability.evidence',
        result.healthy ? 'info' : 'warning',
        { kind: 'state-agent-health', observation: result },
        { result },
      );
      this.transition(
        current,
        'succeeded',
        'completed',
        'capability.succeeded',
        'info',
        { stage: 'completed' },
      );
    } catch (error) {
      const failure = safeFailure(error);
      this.transition(
        current,
        'failed',
        'failed',
        'capability.failed',
        'error',
        failure,
        { error: failure },
      );
    }
  }

  private transition(
    snapshot: InvocationSnapshot,
    status: Status,
    stage: string,
    type: string,
    severity: Severity,
    payload: Readonly<Record<string, unknown>>,
    additional: Pick<
      InvocationSnapshot,
      'result' | 'error' | 'reconciledByWorkerInstanceId'
    > = {},
  ): InvocationSnapshot {
    const timestamp = this.now();
    const nextSequence = (snapshot.events.at(-1)?.sequence ?? 0) + 1;
    const events = [
      ...snapshot.events,
      { sequence: nextSequence, type, occurredAt: timestamp, severity, payload },
    ].slice(-MAX_EVENTS);
    const updated: InvocationSnapshot = {
      ...snapshot,
      status,
      stage,
      updatedAt: timestamp,
      events,
      ...additional,
    };
    this.persist(updated);
    return updated;
  }

  private reconcileInterruptedInvocations(): void {
    const rows = this.database
      .prepare(
        `SELECT fingerprint, snapshot_json FROM capability_invocations
         WHERE status IN ('accepted', 'running')`,
      )
      .all() as unknown as Row[];
    for (const row of rows) {
      const snapshot = parseStored(row);
      const failure = {
        code: 'capability_worker_restarted',
        message:
          'The capability worker restarted before recording a terminal result. The invocation was not replayed.',
      };
      this.transition(
        snapshot,
        'interrupted',
        'interrupted',
        'capability.interrupted',
        'warning',
        {
          ...failure,
          originalWorkerInstanceId: snapshot.workerInstanceId,
          reconciledByWorkerInstanceId: this.instanceId,
        },
        {
          error: failure,
          reconciledByWorkerInstanceId: this.instanceId,
        },
      );
    }
  }

  private persist(snapshot: InvocationSnapshot): void {
    this.database
      .prepare(
        `UPDATE capability_invocations
         SET status = ?, updated_at = ?, snapshot_json = ?
         WHERE invocation_id = ?`,
      )
      .run(
        snapshot.status,
        snapshot.updatedAt,
        JSON.stringify(snapshot),
        snapshot.invocationId,
      );
  }

  private row(invocationId: string): Row | undefined {
    return this.database
      .prepare(
        `SELECT fingerprint, snapshot_json FROM capability_invocations
         WHERE invocation_id = ?`,
      )
      .get(invocationId) as Row | undefined;
  }

  private now(): string {
    return this.clock().toISOString();
  }
}

class InvocationConflictError extends Error {}
class InvocationNotFoundError extends Error {}

function validateRequest(
  invocationId: string,
  value: unknown,
): InvocationRequest {
  canonicalUuid(invocationId, 'invocation ID');
  const request = object(value, 'request');
  exactKeys(request, [
    'protocolVersion',
    'invocationId',
    'operationId',
    'capabilityId',
    'input',
  ]);
  if (
    request.protocolVersion !== 1 ||
    request.invocationId !== invocationId ||
    request.capabilityId !== CAPABILITY_ID
  ) {
    throw new TypeError('unsupported or mismatched capability request');
  }
  canonicalUuid(request.operationId, 'operation ID');
  const input = object(request.input, 'capability input');
  exactKeys(input, ['baseUrl', 'token', 'timeoutSeconds']);
  if (
    typeof input.baseUrl !== 'string' ||
    typeof input.token !== 'string' ||
    typeof input.timeoutSeconds !== 'number'
  ) {
    throw new TypeError('capability input fields have invalid types');
  }
  return {
    protocolVersion: 1,
    invocationId,
    operationId: request.operationId as string,
    capabilityId: CAPABILITY_ID,
    input: {
      baseUrl: input.baseUrl,
      token: input.token,
      timeoutSeconds: input.timeoutSeconds,
    },
  };
}

function canonicalUuid(value: unknown, name: string): asserts value is string {
  if (
    typeof value !== 'string' ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/.test(
      value,
    )
  ) {
    throw new TypeError(`${name} must be a canonical UUID`);
  }
}

function exactKeys(
  value: Record<string, unknown>,
  allowed: readonly string[],
): void {
  const unknown = Object.keys(value).filter((key) => !allowed.includes(key));
  if (unknown.length > 0) {
    throw new TypeError(`unknown request fields: ${unknown.sort().join(', ')}`);
  }
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError(`${name} must be an object`);
  }
  return value as Record<string, unknown>;
}

function hashRequest(request: InvocationRequest): string {
  return createHash('sha256').update(stableJson(request)).digest('hex');
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(',')}]`;
  }
  if (typeof value === 'object' && value !== null) {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`;
  }
  return JSON.stringify(value);
}

function parseStored(row: Row): InvocationSnapshot {
  return JSON.parse(row.snapshot_json) as InvocationSnapshot;
}

function safeFailure(error: unknown) {
  if (error instanceof StateAgentInspectionError) {
    return { code: error.code, message: error.message };
  }
  if (error instanceof TypeError) {
    return { code: 'invalid_capability_input', message: error.message };
  }
  return {
    code: 'capability_internal_error',
    message: 'The capability failed unexpectedly.',
  };
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  if (request.headers['content-type']?.split(';', 1)[0] !== 'application/json') {
    throw new TypeError('content type must be application/json');
  }
  const declaredLength = Number(request.headers['content-length']);
  if (
    !Number.isSafeInteger(declaredLength) ||
    declaredLength < 2 ||
    declaredLength > MAX_REQUEST_BYTES
  ) {
    throw new TypeError('request body size is invalid');
  }
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of request) {
    const buffer = Buffer.from(chunk);
    length += buffer.length;
    if (length > MAX_REQUEST_BYTES) {
      throw new TypeError('request body size is invalid');
    }
    chunks.push(buffer);
  }
  if (length !== declaredLength) {
    throw new TypeError('request body length does not match');
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new TypeError('request body must be valid JSON');
  }
}

function json(response: ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  response.writeHead(status, {
    'Content-Type': 'application/json',
    'Content-Length': Buffer.byteLength(body),
    'Cache-Control': 'no-store',
    Connection: 'close',
  });
  response.end(body);
}

export function createWorkerServer(manager: DurableInvocationManager) {
  return createServer(async (request, response) => {
    const path = new URL(request.url ?? '/', 'http://worker.invalid').pathname;
    if (request.method === 'GET' && path === '/v1/health') {
      json(response, 200, {
        protocolVersion: PROTOCOL_VERSION,
        workerVersion: WORKER_VERSION,
        workerInstanceId: manager.instanceId,
        status: 'ready',
      });
      return;
    }
    if (request.method === 'GET' && path === '/v1/capabilities') {
      json(response, 200, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: manager.capabilities(),
      });
      return;
    }
    const match = INVOCATION_PATH.exec(path);
    if (match === null || (request.method !== 'GET' && request.method !== 'PUT')) {
      json(response, 404, { error: 'not_found' });
      return;
    }
    const invocationId = match[1] as string;
    try {
      if (request.method === 'GET') {
        json(response, 200, manager.get(invocationId));
        return;
      }
      const result = manager.submit(invocationId, await readJson(request));
      json(response, result.created ? 202 : 200, result.snapshot);
    } catch (error) {
      if (error instanceof InvocationNotFoundError) {
        json(response, 404, { error: 'invocation_not_found' });
      } else if (error instanceof InvocationConflictError) {
        json(response, 409, {
          error: 'invocation_conflict',
          message: error.message,
        });
      } else if (error instanceof TypeError) {
        json(response, 400, { error: 'invalid_request', message: error.message });
      } else {
        json(response, 500, { error: 'internal_error' });
      }
    }
  });
}

async function main(): Promise<void> {
  const socketPath = resolve(
    process.env.PETLIBRO_CAPABILITY_WORKER_SOCKET ??
      process.env.PETLIBRO_WORKER_SOCKET ??
      '.data/worker.sock',
  );
  const dataDirectory = resolve(
    process.env.PETLIBRO_MANAGEMENT_DATA_DIR ?? '.data',
  );
  mkdirSync(dirname(socketPath), { recursive: true, mode: 0o700 });
  rmSync(socketPath, { force: true });
  const manager = new DurableInvocationManager(
    resolve(dataDirectory, 'capability-worker.sqlite'),
  );
  const server = createWorkerServer(manager);
  const shutdown = () => {
    server.close(() => {
      manager.close();
      rmSync(socketPath, { force: true });
      process.exit(0);
    });
  };
  process.once('SIGINT', shutdown);
  process.once('SIGTERM', shutdown);
  server.listen(socketPath, () => chmodSync(socketPath, 0o600));
}

if (import.meta.url === `file://${process.argv[1]}`) {
  await main();
}
