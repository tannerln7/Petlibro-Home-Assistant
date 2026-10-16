import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { SetupPage } from './setup-page';

const { runPreflight } = vi.hoisted(() => ({ runPreflight: vi.fn() }));

vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: React.ReactNode }) => (
    <a href="/operations">{children}</a>
  ),
}));

vi.mock('#/infrastructure/http/system-status-client', () => ({
  fetchSystemStatus: async () => ({
    apiVersion: 'v1',
    generatedAt: '2026-10-14T12:00:00.000Z',
    management: {
      name: 'Petlibro Management',
      version: 'test',
      state: 'ready',
      detail: 'Ready.',
      observation: {
        source: 'management-server',
        authority: 'management-application',
        observedAt: '2026-10-14T12:00:00.000Z',
        scope: 'process',
      },
    },
    integrations: [],
  }),
}));

vi.mock('#/infrastructure/http/setup-client', () => ({
  runSetupPreflight: runPreflight,
  fetchInstallationAvailability: async () => ({
    available: false,
    reason: 'Qualification required.',
  }),
  startInstallation: vi.fn(),
  satisfyInstallationCheckpoint: vi.fn(),
  downloadProtectedConfiguration: vi.fn(),
}));

vi.mock('#/infrastructure/http/operations-client', () => ({
  fetchOperations: async () => [],
}));

describe('SetupPage', () => {
  it('runs a real preflight form and presents an explicit broker handoff block', async () => {
    runPreflight.mockResolvedValue({
      generatedAt: '2026-10-14T12:00:00.000Z',
      readyForBootstrapSession: false,
      checks: [
        {
          id: 'temporary-mqtt-port',
          title: 'Temporary MQTT listener',
          status: 'blocked',
          detail: 'An explicit operator-controlled handoff is required.',
        },
      ],
      scope: 'No device-changing operation was attempted.',
    });
    renderPage();

    for (const [label, value] of [
      ['Feeder IPv4', '192.0.2.20'],
      ['Setup host IPv4', '192.0.2.10'],
      ['Home Assistant IPv4', '192.0.2.11'],
      ['Final broker IPv4', '192.0.2.10'],
      ['Final broker port', '1883'],
      ['Backend broker username', 'backend'],
      ['Backend broker password', 'secret'],
      ['SSH recovery public key', 'ssh-ed25519 AAAA test'],
    ] as const) {
      fireEvent.change(screen.getByLabelText(label), {
        target: { value },
      });
    }
    fireEvent.click(screen.getByRole('button', { name: 'Run safe preflight' }));

    expect(
      await screen.findByText(
        'An explicit operator-controlled handoff is required.',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('Checkpoint required')).toBeInTheDocument();
    await waitFor(() => expect(runPreflight).toHaveBeenCalled());
    expect(screen.getByLabelText('Backend broker password')).toHaveValue(
      'secret',
    );
    expect(runPreflight).toHaveBeenCalledWith(
      expect.objectContaining({ brokerPort: 1883, backendPassword: 'secret' }),
      expect.anything(),
    );

    fireEvent.change(screen.getByLabelText('Final broker IPv4'), {
      target: { value: '192.0.2.12' },
    });
    expect(
      screen.queryByText(
        'An explicit operator-controlled handoff is required.',
      ),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Start durable installation' }),
    ).not.toBeInTheDocument();
  });
});

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <SetupPage />
    </QueryClientProvider>,
  );
}
