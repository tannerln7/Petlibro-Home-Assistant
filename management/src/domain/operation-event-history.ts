import type {
  OperationEventSeverity,
  OperationEventSource,
} from '#/domain/operations';

export interface OperationEventHistoryRecord {
  readonly id: number;
  readonly occurredAt: string;
  readonly ingestedAt: string;
  readonly source: OperationEventSource;
  readonly severity: OperationEventSeverity;
  readonly eventType: string;
  readonly message: string;
  readonly operationId: string;
  readonly fields: Readonly<Record<string, unknown>>;
}

export interface OperationEventHistorySnapshot {
  readonly generatedAt: string;
  readonly records: readonly OperationEventHistoryRecord[];
  readonly limit: number;
  readonly truncated: boolean;
  readonly scope: string;
}

export function parseOperationEventHistorySnapshot(
  value: unknown,
): OperationEventHistorySnapshot {
  const record = object(value, 'operation event history response');
  if (
    typeof record.generatedAt !== 'string' ||
    !Array.isArray(record.records) ||
    !Number.isSafeInteger(record.limit) ||
    typeof record.truncated !== 'boolean' ||
    typeof record.scope !== 'string'
  ) {
    throw new Error('The operation event history response is malformed.');
  }
  return {
    generatedAt: record.generatedAt,
    records: record.records.map(parseRecord),
    limit: record.limit as number,
    truncated: record.truncated,
    scope: record.scope,
  };
}

function parseRecord(value: unknown): OperationEventHistoryRecord {
  const record = object(value, 'operation event history record');
  const source = record.source;
  const severity = record.severity;
  if (
    !Number.isSafeInteger(record.id) ||
    typeof record.occurredAt !== 'string' ||
    typeof record.ingestedAt !== 'string' ||
    (source !== 'management-application' &&
      source !== 'capability-worker' &&
      source !== 'python-worker') ||
    (severity !== 'info' && severity !== 'warning' && severity !== 'error') ||
    typeof record.eventType !== 'string' ||
    typeof record.message !== 'string' ||
    typeof record.operationId !== 'string'
  ) {
    throw new Error('An operation event history record is malformed.');
  }
  return {
    id: record.id as number,
    occurredAt: record.occurredAt,
    ingestedAt: record.ingestedAt,
    source,
    severity,
    eventType: record.eventType,
    message: record.message,
    operationId: record.operationId,
    fields: object(record.fields, 'log fields'),
  };
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}
