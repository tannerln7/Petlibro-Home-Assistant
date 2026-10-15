import { parseOperationEventHistorySnapshot } from '#/domain/operation-event-history';

export interface OperationEventHistoryClientFilter {
  readonly severity?: string;
  readonly source?: string;
  readonly search?: string;
}

export async function fetchOperationEventHistory(
  filter: OperationEventHistoryClientFilter,
  signal?: AbortSignal,
) {
  const response = await fetch(
    operationEventHistoryUrl(filter),
    signal === undefined ? {} : { signal },
  );
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    throw new Error(
      'The durable operation event history could not be queried.',
    );
  }
  return parseOperationEventHistorySnapshot(value);
}

export function operationEventHistoryUrl(
  filter: OperationEventHistoryClientFilter,
  download = false,
): string {
  const parameters = new URLSearchParams({ limit: '200' });
  if (filter.severity) parameters.set('severity', filter.severity);
  if (filter.source) parameters.set('source', filter.source);
  if (filter.search) parameters.set('search', filter.search);
  if (download) parameters.set('download', '1');
  return `${import.meta.env.BASE_URL}api/v1/operation-events?${parameters}`;
}
