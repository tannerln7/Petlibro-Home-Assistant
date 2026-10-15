import {
  parseStateAgentHealthResult,
  STATE_AGENT_INSPECTION_CAPABILITY_ID,
  type StateAgentHealthResult,
} from '#/domain/operations';

export type WorkerInvocationStatus =
  | 'accepted'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted';

export interface WorkerEvent {
  readonly sequence: number;
  readonly type: string;
  readonly occurredAt: string;
  readonly severity: 'info' | 'warning' | 'error';
  readonly payload: Readonly<Record<string, unknown>>;
}

export interface WorkerInvocationSnapshot {
  readonly protocolVersion: 1;
  /** The worker process that accepted and owned this invocation. */
  readonly workerInstanceId: string;
  /** The later worker process, if any, that reconciled it after a restart. */
  readonly reconciledByWorkerInstanceId?: string;
  readonly invocationId: string;
  readonly operationId: string;
  readonly capabilityId: typeof STATE_AGENT_INSPECTION_CAPABILITY_ID;
  readonly status: WorkerInvocationStatus;
  readonly stage: string;
  readonly acceptedAt: string;
  readonly updatedAt: string;
  readonly events: readonly WorkerEvent[];
  readonly result?: StateAgentHealthResult;
  readonly error?: {
    readonly code: string;
    readonly message: string;
  };
}

export interface WorkerHealth {
  readonly protocolVersion: 1;
  readonly workerVersion: string;
  readonly workerInstanceId: string;
  readonly status: 'ready';
}

export function parseWorkerHealth(value: unknown): WorkerHealth {
  const record = objectValue(value, 'worker health');
  if (record.protocolVersion !== 1 || record.status !== 'ready') {
    throw new Error('The worker returned an incompatible health response.');
  }
  return {
    protocolVersion: 1,
    workerVersion: stringValue(record.workerVersion, 'worker version'),
    workerInstanceId: stringValue(
      record.workerInstanceId,
      'worker instance ID',
    ),
    status: 'ready',
  };
}

export function parseWorkerInvocationSnapshot(
  value: unknown,
): WorkerInvocationSnapshot {
  const record = objectValue(value, 'worker invocation');
  const status = stringValue(record.status, 'worker invocation status');
  if (
    record.protocolVersion !== 1 ||
    record.capabilityId !== STATE_AGENT_INSPECTION_CAPABILITY_ID ||
    !isWorkerStatus(status) ||
    !Array.isArray(record.events)
  ) {
    throw new Error('The worker returned an incompatible invocation response.');
  }
  const snapshot: WorkerInvocationSnapshot = {
    protocolVersion: 1,
    workerInstanceId: stringValue(
      record.workerInstanceId,
      'worker instance ID',
    ),
    invocationId: stringValue(record.invocationId, 'invocation ID'),
    operationId: stringValue(record.operationId, 'operation ID'),
    capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
    status,
    stage: stringValue(record.stage, 'worker invocation stage'),
    acceptedAt: stringValue(record.acceptedAt, 'worker acceptance time'),
    updatedAt: stringValue(record.updatedAt, 'worker update time'),
    events: record.events.map(parseWorkerEvent),
  };
  const reconciledByWorkerInstanceId = optionalString(
    record.reconciledByWorkerInstanceId,
  );
  const parsed = {
    ...snapshot,
    ...(reconciledByWorkerInstanceId === undefined
      ? {}
      : { reconciledByWorkerInstanceId }),
    ...(record.result === null || record.result === undefined
      ? {}
      : { result: parseStateAgentHealthResult(record.result) }),
    ...(record.error === null || record.error === undefined
      ? {}
      : { error: parseWorkerError(record.error) }),
  };
  if (parsed.status === 'succeeded' && parsed.result === undefined) {
    throw new Error('A successful worker invocation must include its result.');
  }
  if (
    (parsed.status === 'failed' || parsed.status === 'interrupted') &&
    parsed.error === undefined
  ) {
    throw new Error('A failed worker invocation must include its error.');
  }
  return parsed;
}

function parseWorkerEvent(value: unknown): WorkerEvent {
  const record = objectValue(value, 'worker event');
  const severity = stringValue(record.severity, 'worker event severity');
  if (severity !== 'info' && severity !== 'warning' && severity !== 'error') {
    throw new Error('The worker event has an unsupported severity.');
  }
  return {
    sequence: nonNegativeInteger(record.sequence, 'worker event sequence'),
    type: stringValue(record.type, 'worker event type'),
    occurredAt: stringValue(record.occurredAt, 'worker event time'),
    severity,
    payload: objectValue(record.payload, 'worker event payload'),
  };
}

function parseWorkerError(value: unknown) {
  const record = objectValue(value, 'worker error');
  return {
    code: stringValue(record.code, 'worker error code'),
    message: stringValue(record.message, 'worker error message'),
  };
}

function isWorkerStatus(value: string): value is WorkerInvocationStatus {
  return ['accepted', 'running', 'succeeded', 'failed', 'interrupted'].includes(
    value,
  );
}

function objectValue(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function stringValue(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`${name} must be a non-empty string.`);
  }
  return value;
}

function optionalString(value: unknown): string | undefined {
  if (value === undefined) return undefined;
  return stringValue(value, 'optional worker instance ID');
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${name} must be a non-negative integer.`);
  }
  return value as number;
}
