import { describe, expect, it } from 'vitest';
import { SystemStatusService } from './system-status-service';

describe('SystemStatusService', () => {
  it('reports only management-owned readiness and unconfigured boundaries', async () => {
    const observedAt = new Date('2026-10-13T12:00:00.000Z');
    const service = new SystemStatusService(
      '0.1.0-test',
      undefined,
      () => observedAt,
    );

    const snapshot = await service.getSnapshot();

    expect(snapshot).toMatchObject({
      apiVersion: 'v1',
      generatedAt: observedAt.toISOString(),
      management: {
        version: '0.1.0-test',
        state: 'ready',
        observation: {
          source: 'management-server',
          authority: 'management-application',
        },
      },
    });
    expect(snapshot.integrations).toHaveLength(3);
    expect(snapshot.integrations).toSatisfy(
      (integrations: typeof snapshot.integrations) =>
        integrations.every(
          (integration) =>
            integration.availability === 'not-configured' &&
            integration.observation.source === 'management-configuration',
        ),
    );
  });

  it('reports worker readiness without extending it to device health', async () => {
    const service = new SystemStatusService('0.1.0-test', {
      health: async () => ({
        protocolVersion: 1,
        workerVersion: '0.1.0-test',
        workerInstanceId: 'worker-test',
        status: 'ready',
      }),
    });

    const snapshot = await service.getSnapshot();
    const worker = snapshot.integrations.find(
      (integration) => integration.id === 'capability-worker',
    );

    expect(worker?.availability).toBe('ready');
    expect(worker?.observation.source).toBe('capability-worker');
    expect(worker?.observation.scope).toContain(
      'does not imply State Agent or feeder health',
    );
  });
});
