import { createFileRoute } from '@tanstack/react-router';
import { getApplicationServices } from '#/server/application-services';
import { requireJsonMutation } from '#/server/request-security';

export const Route = createFileRoute('/api/v1/actions/state-agent-inspection')({
  server: {
    handlers: {
      GET: () =>
        noStoreJson(
          getApplicationServices().operations.getStateAgentInspectionAction(),
        ),
      POST: async ({ request }) => {
        const rejected = requireJsonMutation(request);
        if (rejected !== undefined) return rejected;
        try {
          const operation =
            await getApplicationServices().operations.startStateAgentInspection();
          return noStoreJson(operation, 202);
        } catch (error) {
          return applicationError(error);
        }
      },
    },
  },
});

function applicationError(error: unknown): Response {
  if (error instanceof Error && error.name === 'ActionUnavailableError') {
    return noStoreJson(
      { error: 'action_unavailable', message: error.message },
      409,
    );
  }
  return noStoreJson(
    {
      error: 'internal_error',
      message: 'The operation could not be accepted.',
    },
    500,
  );
}

function noStoreJson(value: unknown, status = 200): Response {
  return Response.json(value, {
    status,
    headers: { 'Cache-Control': 'no-store' },
  });
}
