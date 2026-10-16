export type LiveLogSeverity = 'trace' | 'debug' | 'info' | 'warning' | 'error';
export type LiveLogSource = 'addon-runtime';

export interface LiveLogRecord {
  readonly sequence: number;
  readonly source: LiveLogSource;
  readonly severity: LiveLogSeverity;
  readonly sourceTimestamp?: string;
  readonly receivedAt: string;
  readonly message: string;
}

export interface LiveLogSourceStatus {
  readonly source: LiveLogSource;
  readonly availability: 'available' | 'unavailable' | 'streaming' | 'error';
  readonly detail: string;
}

export const LIVE_LOG_SOURCES: readonly LiveLogSource[] = ['addon-runtime'];

export function severityPermits(
  record: LiveLogSeverity,
  requested: LiveLogSeverity,
): boolean {
  const order: readonly LiveLogSeverity[] = [
    'trace',
    'debug',
    'info',
    'warning',
    'error',
  ];
  return order.indexOf(record) >= order.indexOf(requested);
}

export function parseLiveLogRecord(value: unknown): LiveLogRecord {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('The live log record is malformed.');
  }
  const record = value as Record<string, unknown>;
  if (
    !Number.isSafeInteger(record.sequence) ||
    record.source !== 'addon-runtime' ||
    !['trace', 'debug', 'info', 'warning', 'error'].includes(
      String(record.severity),
    ) ||
    typeof record.receivedAt !== 'string' ||
    typeof record.message !== 'string'
  ) {
    throw new Error('The live log record is malformed.');
  }
  return {
    sequence: record.sequence as number,
    source: 'addon-runtime',
    severity: record.severity as LiveLogSeverity,
    receivedAt: record.receivedAt,
    message: record.message,
    ...(typeof record.sourceTimestamp === 'string'
      ? { sourceTimestamp: record.sourceTimestamp }
      : {}),
  };
}
