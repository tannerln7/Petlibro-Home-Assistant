export const STATE_AGENT_INSPECTION_ACTION_ID =
  'state-agent.inspect-health' as const;
export const STATE_AGENT_INSPECTION_CAPABILITY_ID =
  'state-agent.inspect-health.v1' as const;

export type OperationStatus =
  | 'accepted'
  | 'running'
  | 'awaiting-user'
  | 'verifying'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'indeterminate';

export type OperationEventSeverity = 'info' | 'warning' | 'error';
export type OperationEventSource =
  | 'management-application'
  | 'capability-worker'
  | 'python-worker';

export interface StateAgentHealthResult {
  readonly healthy: boolean;
  readonly agent: {
    readonly name: string;
    readonly version: string;
    readonly uptimeMs: number;
  };
  readonly stateDecode: {
    readonly ok: boolean;
    readonly expectedBytes: number;
    readonly actualBytes: number;
  };
}

export interface OperationFailure {
  readonly code: string;
  readonly message: string;
}

export interface OperationSnapshot {
  readonly operationId: string;
  readonly actionId: typeof STATE_AGENT_INSPECTION_ACTION_ID;
  readonly capabilityId: typeof STATE_AGENT_INSPECTION_CAPABILITY_ID;
  readonly invocationId: string;
  /** The capability worker process that accepted and owned the invocation. */
  readonly workerInstanceId?: string;
  /** The later worker process, if any, that reconciled it after a restart. */
  readonly reconciledByWorkerInstanceId?: string;
  readonly target: {
    readonly kind: 'state-agent';
    readonly label: string;
  };
  readonly status: OperationStatus;
  readonly stage: string;
  readonly createdAt: string;
  readonly updatedAt: string;
  readonly startedAt?: string;
  readonly completedAt?: string;
  readonly lastWorkerEventSequence: number;
  readonly result?: StateAgentHealthResult;
  readonly failure?: OperationFailure;
}

export interface OperationEvent {
  readonly sequence: number;
  readonly sourceEventKey: string;
  readonly operationId: string;
  readonly type: string;
  readonly source: OperationEventSource;
  readonly severity: OperationEventSeverity;
  readonly occurredAt: string;
  readonly ingestedAt: string;
  readonly payload: Readonly<Record<string, unknown>>;
}

export type OperationEventDraft = Omit<
  OperationEvent,
  'sequence' | 'ingestedAt'
>;

export interface OperationDetail {
  readonly operation: OperationSnapshot;
  readonly events: readonly OperationEvent[];
  readonly nextEventCursor: number;
}

export interface StateAgentInspectionAction {
  readonly id: typeof STATE_AGENT_INSPECTION_ACTION_ID;
  readonly title: 'Inspect State Agent health';
  readonly description: string;
  readonly available: boolean;
  readonly unavailableReason?: string;
  readonly mode: 'read-only';
}

export function isTerminalOperationStatus(status: OperationStatus): boolean {
  return (
    status === 'succeeded' ||
    status === 'failed' ||
    status === 'cancelled' ||
    status === 'interrupted' ||
    status === 'indeterminate'
  );
}

export function parseOperationSnapshot(value: unknown): OperationSnapshot {
  const record = objectValue(value, 'operation');
  const target = objectValue(record.target, 'operation target');
  const status = stringValue(record.status, 'operation status');
  if (!isOperationStatus(status)) {
    throw new Error('The operation response has an unsupported status.');
  }
  if (
    record.actionId !== STATE_AGENT_INSPECTION_ACTION_ID ||
    record.capabilityId !== STATE_AGENT_INSPECTION_CAPABILITY_ID ||
    target.kind !== 'state-agent'
  ) {
    throw new Error('The operation response has an unsupported contract.');
  }

  const snapshot: OperationSnapshot = {
    operationId: stringValue(record.operationId, 'operation ID'),
    actionId: STATE_AGENT_INSPECTION_ACTION_ID,
    capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
    invocationId: stringValue(record.invocationId, 'invocation ID'),
    target: {
      kind: 'state-agent',
      label: stringValue(target.label, 'target label'),
    },
    status,
    stage: stringValue(record.stage, 'operation stage'),
    createdAt: stringValue(record.createdAt, 'operation creation time'),
    updatedAt: stringValue(record.updatedAt, 'operation update time'),
    lastWorkerEventSequence: nonNegativeInteger(
      record.lastWorkerEventSequence,
      'last worker event sequence',
    ),
  };
  const workerInstanceId = optionalString(record.workerInstanceId);
  const reconciledByWorkerInstanceId = optionalString(
    record.reconciledByWorkerInstanceId,
  );
  const startedAt = optionalString(record.startedAt);
  const completedAt = optionalString(record.completedAt);

  return {
    ...snapshot,
    ...(workerInstanceId === undefined ? {} : { workerInstanceId }),
    ...(reconciledByWorkerInstanceId === undefined
      ? {}
      : { reconciledByWorkerInstanceId }),
    ...(startedAt === undefined ? {} : { startedAt }),
    ...(completedAt === undefined ? {} : { completedAt }),
    ...(record.result === undefined
      ? {}
      : { result: parseStateAgentHealthResult(record.result) }),
    ...(record.failure === undefined
      ? {}
      : { failure: parseFailure(record.failure) }),
  };
}

export function parseOperationEvent(value: unknown): OperationEvent {
  const record = objectValue(value, 'operation event');
  const source = stringValue(record.source, 'event source');
  const severity = stringValue(record.severity, 'event severity');
  if (
    source !== 'management-application' &&
    source !== 'capability-worker' &&
    source !== 'python-worker'
  ) {
    throw new Error('The operation event has an unsupported source.');
  }
  if (severity !== 'info' && severity !== 'warning' && severity !== 'error') {
    throw new Error('The operation event has an unsupported severity.');
  }
  return {
    sequence: nonNegativeInteger(record.sequence, 'event sequence'),
    sourceEventKey: stringValue(record.sourceEventKey, 'source event key'),
    operationId: stringValue(record.operationId, 'event operation ID'),
    type: stringValue(record.type, 'event type'),
    source,
    severity,
    occurredAt: stringValue(record.occurredAt, 'event occurrence time'),
    ingestedAt: stringValue(record.ingestedAt, 'event ingestion time'),
    payload: objectValue(record.payload, 'event payload'),
  };
}

export function parseStateAgentHealthResult(
  value: unknown,
): StateAgentHealthResult {
  const record = objectValue(value, 'State Agent health result');
  const agent = objectValue(record.agent, 'State Agent identity');
  const stateDecode = objectValue(record.stateDecode, 'state decode result');
  return {
    healthy: booleanValue(record.healthy, 'health result'),
    agent: {
      name: stringValue(agent.name, 'agent name'),
      version: stringValue(agent.version, 'agent version'),
      uptimeMs: nonNegativeInteger(agent.uptimeMs, 'agent uptime'),
    },
    stateDecode: {
      ok: booleanValue(stateDecode.ok, 'state decode status'),
      expectedBytes: nonNegativeInteger(
        stateDecode.expectedBytes,
        'expected state size',
      ),
      actualBytes: nonNegativeInteger(
        stateDecode.actualBytes,
        'actual state size',
      ),
    },
  };
}

function parseFailure(value: unknown): OperationFailure {
  const record = objectValue(value, 'operation failure');
  return {
    code: stringValue(record.code, 'failure code'),
    message: stringValue(record.message, 'failure message'),
  };
}

function isOperationStatus(value: string): value is OperationStatus {
  return [
    'accepted',
    'running',
    'awaiting-user',
    'verifying',
    'succeeded',
    'failed',
    'cancelled',
    'interrupted',
    'indeterminate',
  ].includes(value);
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
  return value === undefined ? undefined : stringValue(value, 'optional value');
}

function booleanValue(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') {
    throw new Error(`${name} must be a boolean.`);
  }
  return value;
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${name} must be a non-negative integer.`);
  }
  return value as number;
}
