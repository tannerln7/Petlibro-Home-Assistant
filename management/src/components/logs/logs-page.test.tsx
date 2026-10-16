import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LogsPage } from './logs-page';

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: React.ReactNode }) => (
    <a href="/operations">{children}</a>
  ),
}));

vi.mock('#/infrastructure/http/operation-event-history-client', () => ({
  operationEventHistoryUrl: () => '/api/v1/operation-events?download=1',
  fetchOperationEventHistory: async () => ({
    generatedAt: '2026-10-14T12:00:00.000Z',
    limit: 200,
    truncated: false,
    scope: 'Durable semantic operation evidence only.',
    records: [
      {
        id: 1,
        occurredAt: '2026-10-14T12:00:00.000Z',
        ingestedAt: '2026-10-14T12:00:01.000Z',
        source: 'capability-worker',
        severity: 'error',
        category: 'operation',
        eventType: 'capability.failed',
        message: 'State Agent authentication failed.',
        operationId: 'operation-one',
        fields: { token: '[redacted]' },
      },
    ],
  }),
}));

describe('LogsPage', () => {
  it('keeps durable operation evidence separate from opt-in live logs', async () => {
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <QueryClientProvider client={queryClient}>
        <LogsPage />
      </QueryClientProvider>,
    );

    expect(
      await screen.findByText('State Agent authentication failed.'),
    ).toBeInTheDocument();
    expect(screen.getByText('capability-worker')).toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: 'Export evidence' }),
    ).toHaveAttribute('href', '/api/v1/operation-events?download=1');
    expect(screen.getByText('Live raw-log scope')).toBeVisible();
    expect(screen.queryByText(/token/i)).not.toBeInTheDocument();
  });
});
