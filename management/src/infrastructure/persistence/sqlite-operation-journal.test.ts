// @vitest-environment node

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  type OperationSnapshot,
  STATE_AGENT_INSPECTION_ACTION_ID,
  STATE_AGENT_INSPECTION_CAPABILITY_ID,
} from '#/domain/operations';
import { SqliteOperationJournal } from './sqlite-operation-journal';

const temporaryDirectories: string[] = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('SqliteOperationJournal', () => {
  it('persists operation snapshots and deduplicates replayed source events', () => {
    const directory = mkdtempSync(join(tmpdir(), 'petlibro-journal-'));
    temporaryDirectories.push(directory);
    const databasePath = join(directory, 'operations.sqlite');
    const first = new SqliteOperationJournal(databasePath);
    const operation = snapshot();
    const event = {
      sourceEventKey: `management:${operation.operationId}:accepted`,
      operationId: operation.operationId,
      type: 'operation.accepted',
      source: 'management-application' as const,
      severity: 'info' as const,
      occurredAt: operation.createdAt,
      payload: { stage: 'queued' },
    };
    first.create(operation, [event]);
    first.update({ ...operation, stage: 'queued-again' }, [event]);
    first.close();

    const reopened = new SqliteOperationJournal(databasePath);
    expect(reopened.get(operation.operationId)?.stage).toBe('queued-again');
    expect(reopened.eventsAfter(operation.operationId, 0, 10)).toHaveLength(1);
    expect(
      reopened.queryEvents({ limit: 10, search: 'accepted' }),
    ).toHaveLength(1);
    expect(reopened.queryEvents({ limit: 10, severity: 'error' })).toHaveLength(
      0,
    );
    expect(reopened.listNonTerminal()).toHaveLength(1);
    reopened.close();
  });

  it('does not prune durable semantic evidence at a global display limit', () => {
    const journal = new SqliteOperationJournal(':memory:');
    const operation = snapshot();
    const events = Array.from({ length: 10_005 }, (_, index) => ({
      sourceEventKey: `worker:${operation.operationId}:${index + 1}`,
      operationId: operation.operationId,
      type: 'capability.evidence',
      source: 'capability-worker' as const,
      severity: 'info' as const,
      occurredAt: operation.createdAt,
      payload: { evidenceSequence: index + 1 },
    }));

    journal.create(operation, events);

    expect(journal.queryEvents({ limit: events.length })).toHaveLength(
      events.length,
    );
    journal.close();
  });

  it('finds unresolved operations without a bounded history window', () => {
    const journal = new SqliteOperationJournal(':memory:');
    const unresolved = snapshot();
    journal.create(unresolved, []);
    for (let index = 0; index < 125; index += 1) {
      const operation = {
        ...snapshot(),
        operationId: `completed-${index.toString().padStart(3, '0')}`,
        status: 'succeeded' as const,
        createdAt: new Date(
          Date.parse(unresolved.createdAt) + index + 1,
        ).toISOString(),
        updatedAt: new Date(
          Date.parse(unresolved.updatedAt) + index + 1,
        ).toISOString(),
      };
      journal.create(operation, []);
    }

    expect(journal.list(100)).not.toContainEqual(unresolved);
    expect(journal.listUnresolved()).toEqual([unresolved]);
    journal.close();
  });
});

function snapshot(): OperationSnapshot {
  return {
    operationId: 'b7a647e6-40e1-4f60-ae67-a4e9af687684',
    invocationId: '60de899f-32c2-4590-a631-b67a80307dcc',
    actionId: STATE_AGENT_INSPECTION_ACTION_ID,
    capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
    target: { kind: 'state-agent', label: '192.0.2.1' },
    status: 'accepted',
    stage: 'queued',
    createdAt: '2026-10-13T12:00:00.000Z',
    updatedAt: '2026-10-13T12:00:00.000Z',
    lastWorkerEventSequence: 0,
  };
}
