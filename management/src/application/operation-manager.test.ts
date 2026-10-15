// @vitest-environment node

import { describe, expect, it } from 'vitest';
import type {
  CapabilityProvider,
  StateAgentInspectionCapabilityRequest,
} from '#/application/capability-provider';
import {
  WorkerInvocationNotFoundError,
  WorkerUnavailableError,
} from '#/application/capability-provider';
import { OperationManager } from '#/application/operation-manager';
import {
  isTerminalOperationStatus,
  type OperationSnapshot,
  STATE_AGENT_INSPECTION_ACTION_ID,
  STATE_AGENT_INSPECTION_CAPABILITY_ID,
} from '#/domain/operations';
import { SqliteOperationJournal } from '#/infrastructure/persistence/sqlite-operation-journal';
import type {
  WorkerHealth,
  WorkerInvocationSnapshot,
} from '#/infrastructure/worker/worker-contract';

const timestamp = '2026-10-13T12:00:00.000Z';

describe('OperationManager', () => {
  it('persists accepted, worker evidence, and a verified terminal result', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const worker = new SequencedWorker();
    const manager = createManager(journal, worker);

    const accepted = await manager.startStateAgentInspection();
    expect(accepted.status).toBe('accepted');
    const terminal = await waitForTerminal(manager, accepted.operationId);

    expect(terminal.status).toBe('succeeded');
    expect(terminal.result?.healthy).toBe(true);
    const detail = await manager.getOperationDetail(accepted.operationId);
    expect(detail.events.map((event) => event.type)).toEqual([
      'operation.accepted',
      'capability.accepted',
      'capability.started',
      'capability.evidence',
      'capability.succeeded',
      'operation.succeeded',
    ]);
    journal.close();
  });

  it('records communication loss as interrupted without replaying the request', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const worker = new UnavailableWorker();
    const manager = createManager(journal, worker);

    const accepted = await manager.startStateAgentInspection();
    const terminal = await waitForTerminal(manager, accepted.operationId);

    expect(terminal.status).toBe('interrupted');
    expect(terminal.failure?.code).toBe('worker_reconciliation_failed');
    expect(worker.submitCalls).toBe(1);
    journal.close();
  });

  it('reconciles an ambiguous submit after temporary worker unavailability without replay', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const worker = new AmbiguousSubmitWorker();
    const manager = createManager(journal, worker);

    const accepted = await manager.startStateAgentInspection();
    const terminal = await waitForTerminal(manager, accepted.operationId);

    expect(terminal.status).toBe('succeeded');
    expect(worker.submitCalls).toBe(1);
    expect(worker.unavailableGetCalls).toBe(1);
    journal.close();
  });

  it('reconciles a persisted running operation after management restart', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const running = persistedRunningOperation();
    journal.create(running, []);
    const worker = new SequencedWorker(running, true);
    const restartedManager = createManager(journal, worker);

    await restartedManager.ensureInitialized();
    const terminal = await waitForTerminal(
      restartedManager,
      running.operationId,
    );

    expect(terminal.status).toBe('succeeded');
    expect(worker.submitCalls).toBe(0);
    expect(worker.getCalls).toBeGreaterThan(0);
    journal.close();
  });

  it('marks an invocation lost by a restarted worker as interrupted', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const running = persistedRunningOperation();
    journal.create(running, []);
    const restartedManager = createManager(journal, new LostInvocationWorker());

    await restartedManager.ensureInitialized();
    const terminal = await waitForTerminal(
      restartedManager,
      running.operationId,
    );

    expect(terminal.status).toBe('interrupted');
    expect(terminal.failure?.message).toContain('was not replayed');
    journal.close();
  });

  it('preserves invocation ownership and records the reconciling worker', async () => {
    const journal = new SqliteOperationJournal(':memory:');
    const running = persistedRunningOperation();
    journal.create(running, []);
    const worker = new ReconciledInvocationWorker(running);
    const restartedManager = createManager(journal, worker);

    await restartedManager.ensureInitialized();
    const terminal = await waitForTerminal(
      restartedManager,
      running.operationId,
    );

    expect(terminal).toMatchObject({
      status: 'interrupted',
      workerInstanceId: 'worker-instance',
      reconciledByWorkerInstanceId: 'reconciling-worker-instance',
    });
    expect(worker.submitCalls).toBe(0);
    journal.close();
  });
});

class SequencedWorker implements CapabilityProvider {
  public submitCalls = 0;
  public getCalls = 0;
  private operation: OperationSnapshot | undefined;
  private readonly immediatelyTerminal: boolean;

  public constructor(
    operation?: OperationSnapshot,
    immediatelyTerminal = false,
  ) {
    this.operation = operation;
    this.immediatelyTerminal = immediatelyTerminal;
  }

  public async health(): Promise<WorkerHealth> {
    return workerHealth();
  }

  public async submitStateAgentInspection(
    request: StateAgentInspectionCapabilityRequest,
  ): Promise<WorkerInvocationSnapshot> {
    this.submitCalls += 1;
    this.operation = {
      operationId: request.operationId,
      invocationId: request.invocationId,
      actionId: STATE_AGENT_INSPECTION_ACTION_ID,
      capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
      target: { kind: 'state-agent', label: '127.0.0.1' },
      status: 'accepted',
      stage: 'queued',
      createdAt: timestamp,
      updatedAt: timestamp,
      lastWorkerEventSequence: 0,
    };
    return workerSnapshot(this.operation, false);
  }

  public async getInvocation(): Promise<WorkerInvocationSnapshot> {
    this.getCalls += 1;
    if (this.operation === undefined) {
      throw new WorkerInvocationNotFoundError();
    }
    return workerSnapshot(
      this.operation,
      this.immediatelyTerminal || this.getCalls >= 2,
    );
  }
}

class UnavailableWorker implements CapabilityProvider {
  public submitCalls = 0;

  public async health(): Promise<WorkerHealth> {
    throw new WorkerUnavailableError();
  }

  public async submitStateAgentInspection(): Promise<WorkerInvocationSnapshot> {
    this.submitCalls += 1;
    throw new WorkerUnavailableError();
  }

  public async getInvocation(): Promise<WorkerInvocationSnapshot> {
    throw new WorkerUnavailableError();
  }
}

class AmbiguousSubmitWorker extends SequencedWorker {
  public unavailableGetCalls = 0;

  public override async submitStateAgentInspection(
    request: StateAgentInspectionCapabilityRequest,
  ): Promise<WorkerInvocationSnapshot> {
    await super.submitStateAgentInspection(request);
    throw new WorkerUnavailableError();
  }

  public override async getInvocation(): Promise<WorkerInvocationSnapshot> {
    if (this.unavailableGetCalls === 0) {
      this.unavailableGetCalls += 1;
      throw new WorkerUnavailableError();
    }
    return super.getInvocation();
  }
}

class LostInvocationWorker extends UnavailableWorker {
  public override async getInvocation(): Promise<WorkerInvocationSnapshot> {
    throw new WorkerInvocationNotFoundError();
  }
}

class ReconciledInvocationWorker extends SequencedWorker {
  public override async getInvocation(): Promise<WorkerInvocationSnapshot> {
    const operation = persistedRunningOperation();
    return {
      protocolVersion: 1,
      workerInstanceId: 'worker-instance',
      reconciledByWorkerInstanceId: 'reconciling-worker-instance',
      invocationId: operation.invocationId,
      operationId: operation.operationId,
      capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
      status: 'interrupted',
      stage: 'interrupted',
      acceptedAt: timestamp,
      updatedAt: timestamp,
      events: [
        {
          sequence: 3,
          type: 'capability.interrupted',
          occurredAt: timestamp,
          severity: 'warning',
          payload: {
            originalWorkerInstanceId: operation.workerInstanceId,
            reconciledByWorkerInstanceId: 'reconciling-worker-instance',
          },
        },
      ],
      error: {
        code: 'capability_worker_restarted',
        message: 'The invocation was not replayed.',
      },
    };
  }
}

function createManager(
  journal: SqliteOperationJournal,
  worker: CapabilityProvider,
): OperationManager {
  return new OperationManager(
    journal,
    worker,
    {
      baseUrl: 'http://127.0.0.1:8765',
      token: 'synthetic-token',
      timeoutSeconds: 1,
    },
    () => new Date(timestamp),
    async () => new Promise((resolve) => setTimeout(resolve, 1)),
    1,
    25,
  );
}

async function waitForTerminal(
  manager: OperationManager,
  operationId: string,
): Promise<OperationSnapshot> {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    const operation = await manager.getOperation(operationId);
    if (isTerminalOperationStatus(operation.status)) {
      return operation;
    }
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  throw new Error('operation did not reach a terminal state');
}

function persistedRunningOperation(): OperationSnapshot {
  return {
    operationId: '077fb351-30c0-4935-a962-49bf11e8d327',
    invocationId: 'bf3fa744-c49e-49bd-b7af-e4eec2f5fa86',
    workerInstanceId: 'worker-instance',
    actionId: STATE_AGENT_INSPECTION_ACTION_ID,
    capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
    target: { kind: 'state-agent', label: '127.0.0.1' },
    status: 'running',
    stage: 'requesting-state-agent-health',
    createdAt: timestamp,
    updatedAt: timestamp,
    startedAt: timestamp,
    lastWorkerEventSequence: 2,
  };
}

function workerHealth(): WorkerHealth {
  return {
    protocolVersion: 1,
    workerVersion: '0.1.0-test',
    workerInstanceId: 'worker-instance',
    status: 'ready',
  };
}

function workerSnapshot(
  operation: OperationSnapshot,
  terminal: boolean,
): WorkerInvocationSnapshot {
  const events = [
    {
      sequence: 1,
      type: 'capability.accepted',
      occurredAt: timestamp,
      severity: 'info' as const,
      payload: { stage: 'queued' },
    },
    {
      sequence: 2,
      type: 'capability.started',
      occurredAt: timestamp,
      severity: 'info' as const,
      payload: { stage: 'requesting-state-agent-health' },
    },
    ...(terminal
      ? [
          {
            sequence: 3,
            type: 'capability.evidence',
            occurredAt: timestamp,
            severity: 'info' as const,
            payload: { kind: 'state-agent-health' },
          },
          {
            sequence: 4,
            type: 'capability.succeeded',
            occurredAt: timestamp,
            severity: 'info' as const,
            payload: { stage: 'completed' },
          },
        ]
      : []),
  ];
  return {
    protocolVersion: 1,
    workerInstanceId: 'worker-instance',
    invocationId: operation.invocationId,
    operationId: operation.operationId,
    capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
    status: terminal ? 'succeeded' : 'running',
    stage: terminal ? 'completed' : 'requesting-state-agent-health',
    acceptedAt: timestamp,
    updatedAt: timestamp,
    events,
    ...(terminal
      ? {
          result: {
            healthy: true,
            agent: {
              name: 'plaf203-state-agent',
              version: '0.3.0',
              uptimeMs: 5,
            },
            stateDecode: { ok: true, expectedBytes: 236, actualBytes: 236 },
          },
        }
      : {}),
  };
}
