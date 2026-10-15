import type {
  OperationEventQuery,
  OperationJournal,
} from '#/application/operation-journal';
import type {
  OperationEventHistoryRecord,
  OperationEventHistorySnapshot,
} from '#/domain/operation-event-history';
import type {
  OperationEvent,
  OperationEventSeverity,
  OperationEventSource,
} from '#/domain/operations';

const SECRET_KEY =
  /(?:authorization|cookie|credential|password|private.?key|secret|token)/i;
const MAX_STRING_LENGTH = 512;
const MAX_COLLECTION_ITEMS = 50;
const MAX_DEPTH = 5;

export interface OperationEventHistoryFilter {
  readonly limit: number;
  readonly severity?: OperationEventSeverity;
  readonly source?: OperationEventSource;
  readonly search?: string;
}

export class OperationEventHistoryService {
  public constructor(
    private readonly journal: OperationJournal,
    private readonly clock: () => Date = () => new Date(),
  ) {}

  public snapshot(
    filter: OperationEventHistoryFilter,
  ): OperationEventHistorySnapshot {
    const query: OperationEventQuery = {
      limit: filter.limit + 1,
      ...(filter.severity === undefined ? {} : { severity: filter.severity }),
      ...(filter.source === undefined ? {} : { source: filter.source }),
      ...(filter.search === undefined ? {} : { search: filter.search }),
    };
    const events = this.journal.queryEvents(query);
    return {
      generatedAt: this.clock().toISOString(),
      records: events.slice(0, filter.limit).map(toHistoryRecord),
      limit: filter.limit,
      truncated: events.length > filter.limit,
      scope:
        'Durable semantic operation and capability evidence. This is not a raw operational-log archive.',
    };
  }
}

function toHistoryRecord(event: OperationEvent): OperationEventHistoryRecord {
  const fields = redact(event.payload, 0);
  return {
    id: event.sequence,
    occurredAt: event.occurredAt,
    ingestedAt: event.ingestedAt,
    source: event.source,
    severity: event.severity,
    eventType: event.type,
    message: message(event.type, fields),
    operationId: event.operationId,
    fields,
  };
}

function message(
  type: string,
  fields: Readonly<Record<string, unknown>>,
): string {
  const candidate = fields.message;
  if (typeof candidate === 'string' && candidate.length > 0) {
    return candidate;
  }
  return type.replaceAll('.', ' ').replaceAll('-', ' ');
}

function redact(
  value: Readonly<Record<string, unknown>>,
  depth: number,
): Readonly<Record<string, unknown>> {
  if (depth >= MAX_DEPTH) {
    return { bounded: '[depth limit]' };
  }
  return Object.fromEntries(
    Object.entries(value)
      .slice(0, MAX_COLLECTION_ITEMS)
      .map(([key, item]) => [
        key,
        SECRET_KEY.test(key) ? '[redacted]' : bounded(item, depth + 1),
      ]),
  );
}

function bounded(value: unknown, depth: number): unknown {
  if (typeof value === 'string') {
    return value.length <= MAX_STRING_LENGTH
      ? value
      : `${value.slice(0, MAX_STRING_LENGTH)}…`;
  }
  if (Array.isArray(value)) {
    return value
      .slice(0, MAX_COLLECTION_ITEMS)
      .map((item) => bounded(item, depth + 1));
  }
  if (typeof value === 'object' && value !== null) {
    return redact(value as Record<string, unknown>, depth);
  }
  return value;
}
