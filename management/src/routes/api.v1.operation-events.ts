import { createFileRoute } from '@tanstack/react-router';
import type {
  OperationEventSeverity,
  OperationEventSource,
} from '#/domain/operations';
import { getApplicationServices } from '#/server/application-services';

const SEVERITIES = new Set<OperationEventSeverity>([
  'info',
  'warning',
  'error',
]);
const SOURCES = new Set<OperationEventSource>([
  'management-application',
  'capability-worker',
  'python-worker',
]);

export const Route = createFileRoute('/api/v1/operation-events')({
  server: {
    handlers: {
      GET: ({ request }) => {
        const url = new URL(request.url);
        const parsed = filter(url.searchParams);
        if (parsed instanceof Response) return parsed;
        const snapshot =
          getApplicationServices().operationEventHistory.snapshot(parsed);
        const download = url.searchParams.get('download') === '1';
        return Response.json(snapshot, {
          headers: {
            'Cache-Control': 'no-store',
            ...(download
              ? {
                  'Content-Disposition':
                    'attachment; filename="petlibro-operation-events.json"',
                }
              : {}),
          },
        });
      },
    },
  },
});

function filter(parameters: URLSearchParams) {
  const limit = Number(parameters.get('limit') ?? '200');
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500) {
    return invalid('limit must be between 1 and 500');
  }
  const severity = parameters.get('severity');
  if (
    severity !== null &&
    !SEVERITIES.has(severity as OperationEventSeverity)
  ) {
    return invalid('severity is unsupported');
  }
  const source = parameters.get('source');
  if (source !== null && !SOURCES.has(source as OperationEventSource)) {
    return invalid('source is unsupported');
  }
  const rawSearch = parameters.get('search')?.trim();
  if (rawSearch !== undefined && rawSearch.length > 100) {
    return invalid('search must not exceed 100 characters');
  }
  return {
    limit,
    ...(severity === null
      ? {}
      : { severity: severity as OperationEventSeverity }),
    ...(source === null ? {} : { source: source as OperationEventSource }),
    ...(rawSearch === undefined || rawSearch === ''
      ? {}
      : { search: rawSearch }),
  };
}

function invalid(message: string): Response {
  return Response.json(
    { error: 'invalid_operation_event_filter', message },
    { status: 400, headers: { 'Cache-Control': 'no-store' } },
  );
}
