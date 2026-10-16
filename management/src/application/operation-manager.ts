import { randomUUID } from 'node:crypto';
import type {
  CapabilityProvider,
  StateAgentInspectionCapabilityRequest,
} from '#/application/capability-provider';
import {
  WorkerInvocationNotFoundError,
  WorkerProtocolError,
  WorkerRejectedError,
  WorkerUnavailableError,
} from '#/application/capability-provider';
import type { OperationJournal } from '#/application/operation-journal';
import {
  isTerminalOperationStatus,
  type OperationDetail,
  type OperationEventDraft,
  type OperationSnapshot,
  parseStateAgentHealthResult,
  STATE_AGENT_INSPECTION_ACTION_ID,
  STATE_AGENT_INSPECTION_CAPABILITY_ID,
  type StateAgentInspectionAction,
  type StateAgentInspectionOperationSnapshot,
} from '#/domain/operations';
import type { WorkerInvocationSnapshot } from '#/infrastructure/worker/worker-contract';

export interface StateAgentInspectionConfiguration {
  readonly baseUrl?: string;
  readonly token?: string;
  readonly timeoutSeconds: number;
}

export type Clock = () => Date;
export type Sleep = (milliseconds: number) => Promise<void>;

export class ActionUnavailableError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = 'ActionUnavailableError';
  }
}

export class OperationNotFoundError extends Error {
  public constructor(operationId: string) {
    super(`Operation ${operationId} was not found.`);
    this.name = 'OperationNotFoundError';
  }
}

export class OperationManager {
  private readonly activeInvocations = new Set<string>();
  private initialization: Promise<void> | undefined;

  public constructor(
    private readonly journal: OperationJournal,
    private readonly capabilities: CapabilityProvider,
    private readonly configuration: StateAgentInspectionConfiguration,
    private readonly clock: Clock = () => new Date(),
    private readonly sleep: Sleep = (milliseconds) =>
      new Promise((resolve) => setTimeout(resolve, milliseconds)),
    private readonly pollMilliseconds = 150,
    private readonly executionTimeoutMilliseconds = 35_000,
  ) {}

  public getStateAgentInspectionAction(): StateAgentInspectionAction {
    const unavailableReason = this.configurationUnavailableReason();
    return {
      id: STATE_AGENT_INSPECTION_ACTION_ID,
      title: 'Inspect State Agent health',
      description:
        'Request an authenticated, read-only health observation through the private device-operations worker.',
      available: unavailableReason === undefined,
      ...(unavailableReason === undefined ? {} : { unavailableReason }),
      mode: 'read-only',
    };
  }

  public async startStateAgentInspection(): Promise<OperationSnapshot> {
    await this.ensureInitialized();
    const unavailableReason = this.configurationUnavailableReason();
    if (unavailableReason !== undefined) {
      throw new ActionUnavailableError(unavailableReason);
    }

    const now = this.now();
    const operationId = randomUUID();
    const invocationId = randomUUID();
    const snapshot: OperationSnapshot = {
      operationId,
      invocationId,
      actionId: STATE_AGENT_INSPECTION_ACTION_ID,
      capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
      target: {
        kind: 'state-agent',
        label: this.targetLabel(),
      },
      status: 'accepted',
      stage: 'queued',
      createdAt: now,
      updatedAt: now,
      lastWorkerEventSequence: 0,
    };
    this.journal.create(snapshot, [
      this.applicationEvent(
        snapshot,
        'operation.accepted',
        'info',
        { actionId: STATE_AGENT_INSPECTION_ACTION_ID, stage: 'queued' },
        'accepted',
      ),
    ]);
    void this.executeNewInvocation(snapshot);
    return snapshot;
  }

  public async getOperation(operationId: string): Promise<OperationSnapshot> {
    await this.ensureInitialized();
    const operation = this.journal.get(operationId);
    if (operation === undefined) {
      throw new OperationNotFoundError(operationId);
    }
    return operation;
  }

  public async getOperationDetail(
    operationId: string,
    afterSequence = 0,
  ): Promise<OperationDetail> {
    const operation = await this.getOperation(operationId);
    const events = this.journal.eventsAfter(operationId, afterSequence, 500);
    return {
      operation,
      events,
      nextEventCursor: events.at(-1)?.sequence ?? afterSequence,
    };
  }

  public async listOperations(
    limit = 20,
  ): Promise<readonly OperationSnapshot[]> {
    await this.ensureInitialized();
    return this.journal.list(Math.min(Math.max(limit, 1), 100));
  }

  public async eventsAfter(operationId: string, afterSequence: number) {
    await this.getOperation(operationId);
    return this.journal.eventsAfter(operationId, afterSequence, 500);
  }

  public async ensureInitialized(): Promise<void> {
    this.initialization ??= this.reconcilePersistedOperations();
    await this.initialization;
  }

  private async reconcilePersistedOperations(): Promise<void> {
    for (const operation of this.journal.listNonTerminal()) {
      void this.resumeInvocation(operation);
    }
  }

  private async executeNewInvocation(
    operation: StateAgentInspectionOperationSnapshot,
  ): Promise<void> {
    if (!this.acquireInvocation(operation.invocationId)) {
      return;
    }
    try {
      const request: StateAgentInspectionCapabilityRequest = {
        invocationId: operation.invocationId,
        operationId: operation.operationId,
        baseUrl: this.configuration.baseUrl as string,
        token: this.configuration.token as string,
        timeoutSeconds: this.configuration.timeoutSeconds,
      };
      let workerSnapshot: WorkerInvocationSnapshot;
      try {
        workerSnapshot =
          await this.capabilities.submitStateAgentInspection(request);
      } catch (error) {
        if (error instanceof WorkerRejectedError) {
          this.recordTerminalFailure(
            operation,
            'capability_rejected',
            error.message,
          );
          return;
        }
        await this.reconcileAfterSubmitFailure(operation, error);
        return;
      }
      await this.followInvocation(
        this.ingestWorkerSnapshot(operation, workerSnapshot),
      );
    } catch (error) {
      const persisted = this.journal.get(operation.operationId);
      this.recordInterruption(
        persisted?.actionId === STATE_AGENT_INSPECTION_ACTION_ID
          ? persisted
          : operation,
        reconciliationFailure(error),
      );
    } finally {
      this.activeInvocations.delete(operation.invocationId);
    }
  }

  private async resumeInvocation(operation: OperationSnapshot): Promise<void> {
    if (operation.actionId !== STATE_AGENT_INSPECTION_ACTION_ID) {
      return;
    }
    if (!this.acquireInvocation(operation.invocationId)) {
      return;
    }
    try {
      await this.followInvocation(operation, true);
    } catch (error) {
      const persisted = this.journal.get(operation.operationId);
      this.recordInterruption(
        persisted?.actionId === STATE_AGENT_INSPECTION_ACTION_ID
          ? persisted
          : operation,
        reconciliationFailure(error),
      );
    } finally {
      this.activeInvocations.delete(operation.invocationId);
    }
  }

  private async reconcileAfterSubmitFailure(
    operation: StateAgentInspectionOperationSnapshot,
    _submitError: unknown,
  ): Promise<void> {
    // The worker may have accepted the invocation before the response was
    // lost. Reconcile by invocation ID until the coordination deadline; never
    // replay an ambiguous device request merely because IPC was interrupted.
    await this.followInvocation(operation, true);
  }

  private async followInvocation(
    operation: StateAgentInspectionOperationSnapshot,
    queryImmediately = false,
  ): Promise<void> {
    let current = operation;
    const deadline = Date.now() + this.executionTimeoutMilliseconds;
    let delayBeforeQuery = !queryImmediately;
    while (!isTerminalOperationStatus(current.status)) {
      if (Date.now() >= deadline) {
        this.recordInterruption(
          current,
          'The capability did not reach a terminal state before the coordination timeout. Its read-only outcome is unknown.',
        );
        return;
      }
      if (delayBeforeQuery) {
        await this.sleep(this.pollMilliseconds);
      }
      delayBeforeQuery = true;
      try {
        const workerSnapshot = await this.capabilities.getInvocation(
          current.invocationId,
        );
        current = this.ingestWorkerSnapshot(current, workerSnapshot);
      } catch (error) {
        if (error instanceof WorkerUnavailableError) {
          continue;
        }
        this.recordInterruption(current, reconciliationFailure(error));
        return;
      }
    }
  }

  private ingestWorkerSnapshot(
    operation: StateAgentInspectionOperationSnapshot,
    worker: WorkerInvocationSnapshot,
  ): StateAgentInspectionOperationSnapshot {
    if (
      worker.invocationId !== operation.invocationId ||
      worker.operationId !== operation.operationId
    ) {
      throw new WorkerProtocolError(
        'Worker invocation correlation does not match the operation.',
      );
    }
    const workerEvents = worker.events.filter(
      (event) => event.sequence > operation.lastWorkerEventSequence,
    );
    const lastWorkerSequence = Math.max(
      operation.lastWorkerEventSequence,
      ...workerEvents.map((event) => event.sequence),
    );
    const status = worker.status;
    const terminal =
      status === 'succeeded' || status === 'failed' || status === 'interrupted';
    const updated: StateAgentInspectionOperationSnapshot = {
      ...operation,
      workerInstanceId: worker.workerInstanceId,
      ...(worker.reconciledByWorkerInstanceId === undefined
        ? {}
        : {
            reconciledByWorkerInstanceId: worker.reconciledByWorkerInstanceId,
          }),
      status,
      stage: worker.stage,
      updatedAt: worker.updatedAt,
      startedAt: operation.startedAt ?? worker.acceptedAt,
      lastWorkerEventSequence: lastWorkerSequence,
      ...(terminal ? { completedAt: worker.updatedAt } : {}),
      ...(worker.result === undefined
        ? {}
        : { result: parseStateAgentHealthResult(worker.result) }),
      ...(worker.error === undefined ? {} : { failure: worker.error }),
    };
    const events: OperationEventDraft[] = workerEvents.map((event) => ({
      sourceEventKey: `worker:${worker.invocationId}:${event.sequence}`,
      operationId: operation.operationId,
      type: event.type,
      source: 'capability-worker',
      severity: event.severity,
      occurredAt: event.occurredAt,
      payload: event.payload,
    }));
    if (terminal) {
      events.push(
        this.applicationEvent(
          updated,
          status === 'succeeded'
            ? 'operation.succeeded'
            : status === 'interrupted'
              ? 'operation.interrupted'
              : 'operation.failed',
          status === 'succeeded'
            ? 'info'
            : status === 'interrupted'
              ? 'warning'
              : 'error',
          status === 'succeeded'
            ? { stage: updated.stage }
            : { failure: updated.failure },
          'terminal',
        ),
      );
    }
    this.journal.update(updated, events);
    return updated;
  }

  private recordTerminalFailure(
    operation: StateAgentInspectionOperationSnapshot,
    code: string,
    message: string,
  ): void {
    const now = this.now();
    const updated: OperationSnapshot = {
      ...operation,
      status: 'failed',
      stage: 'rejected',
      updatedAt: now,
      completedAt: now,
      failure: { code, message },
    };
    this.journal.update(updated, [
      this.applicationEvent(
        updated,
        'operation.failed',
        'error',
        { failure: updated.failure },
        'terminal',
      ),
    ]);
  }

  private recordInterruption(
    operation: StateAgentInspectionOperationSnapshot,
    message: string,
  ): void {
    if (isTerminalOperationStatus(operation.status)) {
      return;
    }
    const now = this.now();
    const updated: OperationSnapshot = {
      ...operation,
      status: 'interrupted',
      stage: 'worker-reconciliation-required',
      updatedAt: now,
      completedAt: now,
      failure: { code: 'worker_reconciliation_failed', message },
    };
    this.journal.update(updated, [
      this.applicationEvent(
        updated,
        'operation.interrupted',
        'warning',
        { failure: updated.failure },
        'terminal',
      ),
    ]);
  }

  private applicationEvent(
    operation: OperationSnapshot,
    type: string,
    severity: OperationEventDraft['severity'],
    payload: Record<string, unknown>,
    key: string,
  ): OperationEventDraft {
    return {
      sourceEventKey: `management:${operation.operationId}:${key}`,
      operationId: operation.operationId,
      type,
      source: 'management-application',
      severity,
      occurredAt: this.now(),
      payload,
    };
  }

  private acquireInvocation(invocationId: string): boolean {
    if (this.activeInvocations.has(invocationId)) {
      return false;
    }
    this.activeInvocations.add(invocationId);
    return true;
  }

  private configurationUnavailableReason(): string | undefined {
    if (!this.configuration.baseUrl || !this.configuration.token) {
      return 'Configure PETLIBRO_STATE_AGENT_URL and PETLIBRO_STATE_AGENT_TOKEN on the management server.';
    }
    return undefined;
  }

  private targetLabel(): string {
    try {
      return new URL(this.configuration.baseUrl as string).hostname;
    } catch {
      return 'Configured State Agent';
    }
  }

  private now(): string {
    return this.clock().toISOString();
  }
}

function reconciliationFailure(error: unknown): string {
  if (error instanceof WorkerInvocationNotFoundError) {
    return 'The worker restarted or lost the invocation. The read-only inspection was not replayed.';
  }
  if (error instanceof WorkerUnavailableError) {
    return 'The worker is unavailable. The read-only inspection outcome could not be reconciled.';
  }
  if (error instanceof Error) {
    return `Worker communication failed: ${error.message}`;
  }
  return 'Worker communication failed and the read-only outcome could not be reconciled.';
}
