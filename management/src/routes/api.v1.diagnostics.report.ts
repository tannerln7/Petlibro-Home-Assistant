import { createFileRoute } from '@tanstack/react-router';
import { getApplicationServices } from '#/server/application-services';

export const Route = createFileRoute('/api/v1/diagnostics/report')({
  server: {
    handlers: {
      GET: async ({ request }) => {
        const report = await getApplicationServices().diagnostics.generate();
        const download =
          new URL(request.url).searchParams.get('download') === '1';
        return Response.json(report, {
          headers: {
            'Cache-Control': 'no-store',
            ...(download
              ? {
                  'Content-Disposition':
                    'attachment; filename="petlibro-diagnostic-report.json"',
                }
              : {}),
          },
        });
      },
    },
  },
});
