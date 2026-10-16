// @vitest-environment node

import { describe, expect, it } from 'vitest';
import type {
  OperationEventQuery,
  OperationJournal,
} from '#/application/operation-journal';
import type {
  OperationEvent,
  OperationEventDraft,
  OperationSnapshot,
} from '#/domain/operations';
import { OperationEventHistoryService } from './operation-event-history-service';

describe('OperationEventHistoryService', () => {
  it('returns bounded durable evidence and redacts nested secret fields', () => {
    const journal = new FakeJournal([
      {
        sequence: 9,
        sourceEventKey: 'worker:one:1',
        operationId: 'operation-one',
        type: 'capability.failed',
        source: 'capability-worker',
        severity: 'error',
        occurredAt: '2026-10-14T12:00:00.000Z',
        ingestedAt: '2026-10-14T12:00:01.000Z',
        payload: {
          message: 'Authentication failed safely.',
          token: 'must-not-leak',
          nested: { mqttPassword: 'also-secret', safe: 'visible' },
          long: 'x'.repeat(1_000),
        },
      },
      {
        sequence: 8,
        sourceEventKey: 'management:one:accepted',
        operationId: 'operation-one',
        type: 'operation.accepted',
        source: 'management-application',
        severity: 'info',
        occurredAt: '2026-10-14T11:59:59.000Z',
        ingestedAt: '2026-10-14T12:00:00.000Z',
        payload: {},
      },
    ]);
    const service = new OperationEventHistoryService(
      journal,
      () => new Date('2026-10-14T12:01:00.000Z'),
    );

    const snapshot = service.snapshot({ limit: 1, severity: 'error' });

    expect(snapshot.truncated).toBe(true);
    expect(snapshot.records).toHaveLength(1);
    expect(snapshot.records[0]).toMatchObject({
      source: 'capability-worker',
      message: 'Authentication failed safely.',
      fields: {
        token: '[redacted]',
        nested: { mqttPassword: '[redacted]', safe: 'visible' },
      },
    });
    expect(JSON.stringify(snapshot)).not.toContain('must-not-leak');
    expect(JSON.stringify(snapshot)).not.toContain('also-secret');
    expect(journal.lastQuery).toEqual({ limit: 2, severity: 'error' });
  });
});

class FakeJournal implements OperationJournal {
  public lastQuery?: OperationEventQuery;

  public constructor(private readonly events: readonly OperationEvent[]) {}

  public queryEvents(query: OperationEventQuery): readonly OperationEvent[] {
    this.lastQuery = query;
    return this.events;
  }

  public create(
    _snapshot: OperationSnapshot,
    _events: readonly OperationEventDraft[],
  ): void {}
  public update(
    _snapshot: OperationSnapshot,
    _events: readonly OperationEventDraft[],
  ): void {}
  public get(_operationId: string): OperationSnapshot | undefined {
    return undefined;
  }
  public list(_limit: number): readonly OperationSnapshot[] {
    return [];
  }
  public listNonTerminal(): readonly OperationSnapshot[] {
    return [];
  }
  public listUnresolved(): readonly OperationSnapshot[] {
    return [];
  }
  public eventsAfter(
    _operationId: string,
    _afterSequence: number,
    _limit: number,
  ): readonly OperationEvent[] {
    return [];
  }
}
