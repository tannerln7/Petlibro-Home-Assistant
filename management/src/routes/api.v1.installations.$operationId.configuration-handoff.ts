import { createFileRoute } from '@tanstack/react-router';
import { InstallationCheckpointError } from '#/application/installation-coordinator';
import { getApplicationServices } from '#/server/application-services';
import { requireJsonMutation } from '#/server/request-security';

export const Route = createFileRoute(
  '/api/v1/installations/$operationId/configuration-handoff',
)({
  server: {
    handlers: {
      POST: async ({ params, request }) => {
        const rejected = requireJsonMutation(request);
        if (rejected !== undefined) return rejected;
        try {
          const handoff =
            await getApplicationServices().installations.configurationHandoff(
              params.operationId,
            );
          return Response.json(handoff, {
            headers: {
              'Cache-Control': 'no-store, private',
              Pragma: 'no-cache',
            },
          });
        } catch (error) {
          if (error instanceof InstallationCheckpointError) {
            return Response.json(
              {
                error: 'configuration_handoff_unavailable',
                message: error.message,
              },
              { status: 409, headers: { 'Cache-Control': 'no-store' } },
            );
          }
          return Response.json(
            {
              error: 'configuration_handoff_failed',
              message: 'Protected configuration could not be retrieved.',
            },
            { status: 500, headers: { 'Cache-Control': 'no-store' } },
          );
        }
      },
    },
  },
});
