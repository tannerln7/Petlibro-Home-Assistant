import { createFileRoute } from '@tanstack/react-router';
import { InstallationInputError } from '#/application/installation-coordinator';
import { parseSetupPreflightRequest } from '#/domain/setup';
import { getApplicationServices } from '#/server/application-services';
import { requireJsonMutation } from '#/server/request-security';

const MAX_REQUEST_BYTES = 16 * 1024;

export const Route = createFileRoute('/api/v1/installations')({
  server: {
    handlers: {
      GET: async () =>
        Response.json(getApplicationServices().installations.availability(), {
          headers: { 'Cache-Control': 'no-store' },
        }),
      POST: async ({ request }) => {
        const rejected = requireJsonMutation(request);
        if (rejected !== undefined) return rejected;
        try {
          const body = await request.text();
          if (Buffer.byteLength(body) > MAX_REQUEST_BYTES) {
            throw new TypeError('The installation request is too large.');
          }
          const input = parseSetupPreflightRequest(JSON.parse(body) as unknown);
          const installation =
            await getApplicationServices().installations.start(input);
          return Response.json(installation, {
            status: 202,
            headers: { 'Cache-Control': 'no-store' },
          });
        } catch (error) {
          if (
            error instanceof SyntaxError ||
            error instanceof TypeError ||
            error instanceof InstallationInputError
          ) {
            return Response.json(
              {
                error: 'installation_not_accepted',
                message:
                  error instanceof Error
                    ? error.message
                    : 'The installation request is invalid.',
              },
              { status: 400, headers: { 'Cache-Control': 'no-store' } },
            );
          }
          return Response.json(
            {
              error: 'installation_start_failed',
              message: 'The installation could not be accepted safely.',
            },
            { status: 500, headers: { 'Cache-Control': 'no-store' } },
          );
        }
      },
    },
  },
});
