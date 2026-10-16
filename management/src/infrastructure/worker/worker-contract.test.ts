// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { parseWorkerInvocationSnapshot } from './worker-contract';

describe('worker invocation event continuity', () => {
  it('rejects a snapshot whose durable semantic event sequence has a gap', () => {
    expect(() =>
      parseWorkerInvocationSnapshot({
        protocolVersion: 1,
        workerInstanceId: 'worker',
        invocationId: '11111111-1111-4111-8111-111111111111',
        operationId: '22222222-2222-4222-8222-222222222222',
        capabilityId: 'bootstrap.prepare-artifacts.v1',
        status: 'running',
        stage: 'executing-capability',
        acceptedAt: '2026-10-16T00:00:00.000Z',
        updatedAt: '2026-10-16T00:00:01.000Z',
        events: [
          {
            sequence: 1,
            type: 'capability.accepted',
            occurredAt: '2026-10-16T00:00:00.000Z',
            severity: 'info',
            payload: {},
          },
          {
            sequence: 3,
            type: 'capability.started',
            occurredAt: '2026-10-16T00:00:01.000Z',
            severity: 'info',
            payload: {},
          },
        ],
      }),
    ).toThrow('event sequence is incomplete');
  });
});
