import { createFileRoute } from '@tanstack/react-router';
import { InstallationCheckpointError } from '#/application/installation-coordinator';
import { getApplicationServices } from '#/server/application-services';
import { requireJsonMutation } from '#/server/request-security';

export const Route = createFileRoute(
  '/api/v1/installations/$operationId/checkpoints/$checkpointId',
)({
  server: {
    handlers: {
      POST: async ({ params, request }) => {
        const rejected = requireJsonMutation(request);
        if (rejected !== undefined) return rejected;
        try {
          const operation =
            await getApplicationServices().installations.respondToCheckpoint(
              params.operationId,
              params.checkpointId,
            );
          return Response.json(operation, {
            headers: { 'Cache-Control': 'no-store' },
          });
        } catch (error) {
          if (error instanceof InstallationCheckpointError) {
            return Response.json(
              { error: 'checkpoint_not_satisfied', message: error.message },
              { status: 409, headers: { 'Cache-Control': 'no-store' } },
            );
          }
          return Response.json(
            {
              error: 'checkpoint_failed',
              message: 'The checkpoint could not be verified.',
            },
            { status: 500, headers: { 'Cache-Control': 'no-store' } },
          );
        }
      },
    },
  },
});
