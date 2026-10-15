import { createFileRoute } from '@tanstack/react-router';
import type { SetupPreflightRequest } from '#/domain/setup';
import { getApplicationServices } from '#/server/application-services';

const MAX_REQUEST_BYTES = 16 * 1024;

export const Route = createFileRoute('/api/v1/setup/preflight')({
  server: {
    handlers: {
      POST: async ({ request }) => {
        try {
          const contentLength = Number(request.headers.get('content-length'));
          if (
            Number.isFinite(contentLength) &&
            contentLength > MAX_REQUEST_BYTES
          ) {
            return invalid('The preflight request is too large.');
          }
          const body = await request.text();
          if (Buffer.byteLength(body) > MAX_REQUEST_BYTES) {
            return invalid('The preflight request is too large.');
          }
          const input = parseInput(JSON.parse(body) as unknown);
          return Response.json(
            await getApplicationServices().setupPreflight.run(input),
            { headers: { 'Cache-Control': 'no-store' } },
          );
        } catch (error) {
          if (error instanceof SyntaxError || error instanceof TypeError) {
            return invalid(
              error instanceof Error
                ? error.message
                : 'The preflight request is invalid.',
            );
          }
          return Response.json(
            {
              error: 'preflight_failed',
              message: 'The setup preflight could not be completed.',
            },
            { status: 500, headers: { 'Cache-Control': 'no-store' } },
          );
        }
      },
    },
  },
});

function parseInput(value: unknown): SetupPreflightRequest {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new TypeError('The preflight request must be an object.');
  }
  const record = value as Record<string, unknown>;
  const fields = [
    'feederIp',
    'setupHostIp',
    'homeAssistantIp',
    'brokerHost',
    'brokerPort',
    'backendUsername',
    'backendPassword',
    'sshPublicKey',
  ] as const;
  const unknown = Object.keys(record).filter(
    (field) => !fields.includes(field as (typeof fields)[number]),
  );
  if (unknown.length > 0) {
    throw new TypeError('The preflight request contains unknown fields.');
  }
  for (const field of fields.filter((item) => item !== 'brokerPort')) {
    if (typeof record[field] !== 'string') {
      throw new TypeError('The preflight request has invalid field types.');
    }
  }
  if (!Number.isSafeInteger(record.brokerPort)) {
    throw new TypeError('The preflight request has an invalid broker port.');
  }
  return record as unknown as SetupPreflightRequest;
}

function invalid(message: string): Response {
  return Response.json(
    { error: 'invalid_preflight_request', message },
    { status: 400, headers: { 'Cache-Control': 'no-store' } },
  );
}
