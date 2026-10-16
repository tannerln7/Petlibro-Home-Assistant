import { createFileRoute } from '@tanstack/react-router';
import { parseSetupPreflightRequest } from '#/domain/setup';
import { getApplicationServices } from '#/server/application-services';
import { requireJsonMutation } from '#/server/request-security';

const MAX_REQUEST_BYTES = 16 * 1024;

export const Route = createFileRoute('/api/v1/setup/preflight')({
  server: {
    handlers: {
      POST: async ({ request }) => {
        const rejected = requireJsonMutation(request);
        if (rejected !== undefined) return rejected;
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
          const input = parseSetupPreflightRequest(JSON.parse(body) as unknown);
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

function invalid(message: string): Response {
  return Response.json(
    { error: 'invalid_preflight_request', message },
    { status: 400, headers: { 'Cache-Control': 'no-store' } },
  );
}
