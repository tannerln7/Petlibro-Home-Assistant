import { createHash, randomUUID } from 'node:crypto';
import {
  WorkerInvocationNotFoundError,
  WorkerUnavailableError,
} from '#/application/capability-provider';
import type { OperationJournal } from '#/application/operation-journal';
import type { SetupPreflightService } from '#/application/setup-preflight-service';
import {
  BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID,
  BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID,
  BOOTSTRAP_PREPARE_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID,
  type BootstrapCapabilityId,
  type GenericCapabilityRequest,
  type ProtectedConfigurationHandoff,
} from '#/domain/capabilities';
import {
  INSTALLATION_ACTION_ID,
  INSTALLATION_WORKFLOW_CAPABILITY_ID,
  type InstallationCheckpoint,
  type InstallationOperationSnapshot,
  type InstallationStageSnapshot,
  isTerminalOperationStatus,
  type OperationEventDraft,
} from '#/domain/operations';
import type { SetupPreflightRequest } from '#/domain/setup';
import type { InstallationSecretStore } from '#/infrastructure/persistence/installation-secret-store';
import type { WorkerInvocationSnapshot } from '#/infrastructure/worker/worker-contract';

const INSTALLATION_STAGES: readonly Omit<
  InstallationStageSnapshot,
  'status' | 'updatedAt'
>[] = [
  {
    id: 'preflight',
    title: 'Validate prerequisites',
    detail:
      'Bind verified network, broker, and recovery inputs to this installation.',
  },
  {
    id: 'artifacts',
    title: 'Prepare protected artifacts',
    detail:
      'Build and integrity-check the device-bound payload and recovery material.',
  },
  {
    id: 'identity',
    title: 'Capture factory identity',
    detail:
      'Acquire temporary TCP/1883 ownership and observe the intended feeder.',
  },
  {
    id: 'broker-proof',
    title: 'Prove final broker credentials',
    detail: 'Verify backend and captured feeder credentials before OTA.',
  },
  {
    id: 'ota',
    title: 'Guarded OTA transaction',
    detail:
      'Serve and correlate the device-bound payload without blind replay.',
  },
  {
    id: 'integration',
    title: 'Restore and verify integration',
    detail:
      'Restore final broker ownership and verify device and service adoption.',
  },
];

export interface InstallationCapabilityProvider {
  submitInvocation(
    request: GenericCapabilityRequest,
  ): Promise<WorkerInvocationSnapshot>;
  getInvocation(invocationId: string): Promise<WorkerInvocationSnapshot>;
  getConfigurationHandoff(installationId: string): Promise<
    Omit<ProtectedConfigurationHandoff, 'addonOptionsPatch'> & {
      generatedAddonOptions: Readonly<Record<string, unknown>>;
    }
  >;
}

export interface InstallationReadinessObservation {
  readonly ready: boolean;
  readonly summary: string;
  readonly checks: Readonly<Record<string, boolean>>;
}

export interface InstallationReadinessObserver {
  inspect(
    installation: InstallationOperationSnapshot,
    expectedBackendPassword: string,
  ): Promise<InstallationReadinessObservation>;
}

export class InstallationInputError extends Error {}
export class InstallationCheckpointError extends Error {}

export class InstallationCoordinator {
  private initialization: Promise<void> | undefined;
  private transitionQueue: Promise<void> = Promise.resolve();
  private readonly activeInvocations = new Set<string>();

  public constructor(
    private readonly journal: OperationJournal,
    private readonly capabilities: InstallationCapabilityProvider,
    private readonly preflight: SetupPreflightService,
    private readonly secrets: InstallationSecretStore,
    private readonly deviceChangesEnabled: boolean,
    private readonly clock: () => Date = () => new Date(),
    private readonly sleep: (milliseconds: number) => Promise<void> = (
      milliseconds,
    ) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
    private readonly pollMilliseconds = 250,
    private readonly executionTimeoutMilliseconds = 15 * 60_000,
    private readonly readiness?: InstallationReadinessObserver,
  ) {}

  public availability() {
    const unresolved = this.journal
      .listUnresolved()
      .find((operation) => operation.actionId === INSTALLATION_ACTION_ID);
    const installation =
      unresolved ??
      this.journal
        .list(100)
        .find((operation) => operation.actionId === INSTALLATION_ACTION_ID);
    return {
      available: this.deviceChangesEnabled,
      mode: 'device-changing' as const,
      reason: this.deviceChangesEnabled
        ? undefined
        : 'Host-side bootstrap is packaged but device-changing execution is disabled pending controlled Home Assistant and feeder qualification.',
      ...(installation === undefined ? {} : { installation }),
    };
  }

  public async start(
    input: SetupPreflightRequest,
  ): Promise<InstallationOperationSnapshot> {
    await this.ensureInitialized();
    return this.serializeTransition(() => this.startSerialized(input));
  }

  private async startSerialized(
    input: SetupPreflightRequest,
  ): Promise<InstallationOperationSnapshot> {
    if (!this.deviceChangesEnabled) {
      throw new InstallationInputError(this.availability().reason as string);
    }
    const conflicting = this.journal
      .listUnresolved()
      .find((operation) => operation.actionId === INSTALLATION_ACTION_ID);
    if (conflicting !== undefined) {
      throw new InstallationInputError(
        `Installation ${conflicting.operationId} is still active or has an unresolved outcome. Continue or resolve that installation before starting another.`,
      );
    }
    const preflight = await this.preflight.run(input);
    const blocking = preflight.checks.filter(
      (check) =>
        check.id !== 'temporary-mqtt-port' &&
        check.status !== 'passed' &&
        check.status !== 'warning',
    );
    if (blocking.length > 0) {
      throw new InstallationInputError(
        blocking.map((check) => check.detail).join(' '),
      );
    }
    const temporary = preflight.checks.find(
      (check) => check.id === 'temporary-mqtt-port',
    );
    if (temporary?.status === 'failed') {
      throw new InstallationInputError(temporary.detail);
    }
    const operationId = randomUUID();
    this.secrets.create(operationId, input);
    const now = this.now();
    const snapshot: InstallationOperationSnapshot = {
      operationId,
      actionId: INSTALLATION_ACTION_ID,
      capabilityId: INSTALLATION_WORKFLOW_CAPABILITY_ID,
      target: { kind: 'feeder-installation', label: input.feederIp },
      status: 'running',
      stage: 'artifacts',
      createdAt: now,
      updatedAt: now,
      startedAt: now,
      configuration: {
        feederIp: input.feederIp,
        setupHostIp: input.setupHostIp,
        homeAssistantIp: input.homeAssistantIp,
        brokerHost: input.brokerHost,
        brokerPort: input.brokerPort,
        backendUsername: input.backendUsername,
        sshPublicKeyFingerprint: sshFingerprint(input.sshPublicKey),
        configurationFingerprint: configurationFingerprint(input),
      },
      stages: INSTALLATION_STAGES.map((stage) => ({
        ...stage,
        status:
          stage.id === 'preflight'
            ? 'completed'
            : stage.id === 'artifacts'
              ? 'active'
              : 'pending',
        ...(stage.id === 'preflight' ? { updatedAt: now } : {}),
      })),
      evidence: preflight.checks.map((check) => ({
        kind: `preflight.${check.id}`,
        summary: `${check.status}: ${check.detail}`,
        observedAt: preflight.generatedAt,
        source: 'management-application',
      })),
      satisfiedCheckpointIds: [],
    };
    this.journal.create(snapshot, [
      this.event(
        snapshot,
        'installation.accepted',
        'info',
        {
          configurationFingerprint:
            snapshot.configuration.configurationFingerprint,
          stage: snapshot.stage,
        },
        'accepted',
      ),
    ]);
    void this.beginCapability(snapshot, BOOTSTRAP_PREPARE_CAPABILITY_ID);
    return snapshot;
  }

  public async respondToCheckpoint(
    operationId: string,
    checkpointId: string,
  ): Promise<InstallationOperationSnapshot> {
    await this.ensureInitialized();
    return this.serializeTransition(() =>
      this.respondToCheckpointSerialized(operationId, checkpointId),
    );
  }

  private async respondToCheckpointSerialized(
    operationId: string,
    checkpointId: string,
  ): Promise<InstallationOperationSnapshot> {
    const operation = this.installation(operationId);
    const checkpoint = operation.checkpoint;
    if (operation.satisfiedCheckpointIds.includes(checkpointId)) {
      return operation;
    }
    if (checkpoint === undefined || operation.status !== 'awaiting-user') {
      throw new InstallationCheckpointError(
        'The installation is not awaiting an operator checkpoint.',
      );
    }
    if (checkpoint.checkpointId !== checkpointId) {
      throw new InstallationCheckpointError(
        'The checkpoint response does not match the active checkpoint.',
      );
    }
    if (checkpoint.responseRecordedAt !== undefined) return operation;

    if (checkpoint.kind === 'stop-final-broker') {
      const port = await this.preflight.verifyTemporaryListenerAvailable(
        operation.configuration.setupHostIp,
      );
      if (port !== 'available') {
        this.journal.update(operation, [
          this.event(
            operation,
            'installation.checkpoint-condition-failed',
            'warning',
            {
              checkpointId,
              condition: 'tcp-1883-available',
              observation: port,
            },
            `checkpoint:${checkpointId}:condition:${Date.now()}`,
          ),
        ]);
        throw new InstallationCheckpointError(
          port === 'occupied'
            ? 'TCP/1883 is still occupied. Stop the final broker, then retry this checkpoint.'
            : 'The setup host cannot currently bind TCP/1883.',
        );
      }
    }
    if (checkpoint.kind === 'verify-operational-readiness') {
      const observation = await this.observeReadiness(operation);
      if (!observation.ready) {
        this.journal.update(operation, [
          this.event(
            operation,
            'installation.integration-not-ready',
            'warning',
            { summary: observation.summary, checks: observation.checks },
            `checkpoint:${checkpointId}:condition:${Date.now()}`,
          ),
        ]);
        throw new InstallationCheckpointError(observation.summary);
      }
    }

    const now = this.now();
    const acknowledged: InstallationOperationSnapshot = {
      ...operation,
      updatedAt: now,
      checkpoint: { ...checkpoint, responseRecordedAt: now },
      satisfiedCheckpointIds: [
        ...operation.satisfiedCheckpointIds,
        checkpoint.checkpointId,
      ],
    };
    this.journal.update(acknowledged, [
      this.event(
        acknowledged,
        'installation.checkpoint-satisfied',
        'info',
        { checkpointId, kind: checkpoint.kind },
        `checkpoint:${checkpointId}:satisfied`,
      ),
    ]);
    return this.advanceFromCheckpoint(acknowledged);
  }

  public async ensureInitialized(): Promise<void> {
    this.initialization ??= this.reconcilePersisted();
    await this.initialization;
  }

  public async configurationHandoff(
    operationId: string,
  ): Promise<ProtectedConfigurationHandoff> {
    await this.ensureInitialized();
    const operation = this.installation(operationId);
    const identityCaptured = operation.stages.some(
      (stage) => stage.id === 'identity' && stage.status === 'completed',
    );
    if (!identityCaptured) {
      throw new InstallationCheckpointError(
        'Protected configuration is unavailable until feeder identity capture completes.',
      );
    }
    const protectedInput = this.secrets.read(operationId);
    const workerHandoff =
      await this.capabilities.getConfigurationHandoff(operationId);
    return {
      installationId: operationId,
      feederBrokerAccount: workerHandoff.feederBrokerAccount,
      addonOptionsPatch: {
        mqtt_host: protectedInput.brokerHost,
        mqtt_port: protectedInput.brokerPort,
        mqtt_username: protectedInput.backendUsername,
        mqtt_password: protectedInput.backendPassword,
        persist_feeder_mqtt: true,
        feeder_mqtt_host: protectedInput.brokerHost,
        feeder_mqtt_port: 1883,
        ...workerHandoff.generatedAddonOptions,
      },
    };
  }

  private async reconcilePersisted(): Promise<void> {
    for (const operation of this.journal.listNonTerminal()) {
      if (operation.actionId !== INSTALLATION_ACTION_ID) continue;
      if (operation.status === 'awaiting-user') {
        if (operation.checkpoint?.responseRecordedAt !== undefined) {
          await this.advanceFromCheckpoint(operation);
        }
        continue;
      }
      if (operation.status === 'running') {
        const expected = capabilityForStage(operation.stage);
        if (expected === undefined) {
          this.interrupt(
            operation,
            'The persisted installation stage has no safe automatic continuation.',
          );
        } else if (operation.currentInvocation?.capabilityId === expected) {
          void this.followCapability(operation, true);
        } else {
          void this.beginCapability(operation, expected);
        }
      }
    }
  }

  private async advanceFromCheckpoint(
    operation: InstallationOperationSnapshot,
  ): Promise<InstallationOperationSnapshot> {
    const checkpoint = operation.checkpoint as InstallationCheckpoint;
    if (checkpoint.kind === 'backup-recovery-key') {
      const completed = {
        ...operation,
        stages: updateStage(
          operation.stages,
          'artifacts',
          'completed',
          'Recovery-key custody was confirmed by the operator.',
          this.now(),
        ),
      };
      return this.setCheckpoint(
        completed,
        'identity',
        'stop-final-broker',
        'Release TCP/1883 for identity capture',
        'Stop the Home Assistant MQTT broker using its supported controls. This can interrupt unrelated MQTT clients. Return only after it reports stopped.',
        'The management worker will verify and exclusively acquire TCP/1883 before sending at most one feeder provisioning datagram.',
      );
    }
    if (
      checkpoint.kind === 'retry-safe-capability' &&
      operation.stage === 'artifacts'
    ) {
      const running = this.activateStage(operation, 'artifacts');
      void this.beginCapability(running, BOOTSTRAP_PREPARE_CAPABILITY_ID);
      return running;
    }
    if (
      checkpoint.kind === 'stop-final-broker' &&
      operation.stage === 'identity'
    ) {
      const running = this.activateStage(operation, 'identity');
      void this.beginCapability(
        running,
        BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID,
      );
      return running;
    }
    if (
      checkpoint.kind === 'start-final-broker' &&
      operation.stage === 'broker-proof'
    ) {
      const running = this.activateStage(operation, 'broker-proof');
      void this.beginCapability(running, BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID);
      return running;
    }
    if (checkpoint.kind === 'stop-final-broker' && operation.stage === 'ota') {
      const running = this.activateStage(operation, 'ota');
      void this.beginCapability(running, BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID);
      return running;
    }
    if (
      checkpoint.kind === 'start-final-broker' &&
      operation.stage === 'integration'
    ) {
      return this.setCheckpoint(
        operation,
        'integration',
        'apply-addon-options',
        'Apply generated add-on options',
        'Apply the protected generated options through Home Assistant add-on configuration, then restart this add-on if Home Assistant requires it.',
        'Confirmation records intent only. Verification may resend OEM provisioning for the final broker endpoint, then independently observes runtime adoption.',
      );
    }
    if (checkpoint.kind === 'apply-addon-options') {
      const running = this.activateStage(operation, 'integration');
      void this.beginCapability(
        running,
        BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID,
      );
      return running;
    }
    if (checkpoint.kind === 'verify-operational-readiness') {
      const now = this.now();
      const terminal: InstallationOperationSnapshot = {
        ...operation,
        status: 'succeeded',
        stage: 'completed',
        updatedAt: now,
        completedAt: now,
        checkpoint: undefined,
        stages: updateStage(
          operation.stages,
          'integration',
          'completed',
          'Bootstrap, runtime configuration, broker, and controller readiness were independently observed.',
          now,
        ),
      };
      this.journal.update(terminal, [
        this.event(
          terminal,
          'installation.succeeded',
          'info',
          { verification: 'end-to-end-operational-readiness' },
          'terminal',
        ),
      ]);
      return terminal;
    }
    throw new InstallationCheckpointError(
      'The checkpoint cannot advance from this stage.',
    );
  }

  private async beginCapability(
    operation: InstallationOperationSnapshot,
    capabilityId: BootstrapCapabilityId,
  ): Promise<void> {
    const invocationId = randomUUID();
    let input: Readonly<Record<string, unknown>>;
    try {
      input = this.capabilityInput(operation, capabilityId);
    } catch (error) {
      this.recordProtectedInputFailure(operation, capabilityId, error);
      return;
    }
    const now = this.now();
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status: 'running',
      updatedAt: now,
      checkpoint: undefined,
      currentInvocation: {
        invocationId,
        capabilityId,
        status: 'accepted',
        stage: 'queued',
        lastWorkerEventSequence: 0,
      },
    };
    this.journal.update(updated, [
      this.event(
        updated,
        'installation.capability-requested',
        'info',
        {
          capabilityId,
          invocationId,
        },
        `invocation:${invocationId}:requested`,
      ),
    ]);
    if (!this.acquireInvocation(invocationId)) return;
    try {
      try {
        await this.capabilities.submitInvocation({
          invocationId,
          operationId: operation.operationId,
          capabilityId,
          input,
        });
      } catch {
        await this.followCapability(updated, true, true);
        return;
      }
      // Always reconcile through the durable worker record. In particular, do
      // not persist a terminal submission response separately from a supported
      // recovery checkpoint.
      await this.followCapability(updated, false, true);
    } finally {
      this.activeInvocations.delete(invocationId);
    }
  }

  private async followCapability(
    operation: InstallationOperationSnapshot,
    immediately = false,
    invocationAlreadyAcquired = false,
  ): Promise<void> {
    const invocation = operation.currentInvocation;
    if (
      invocation === undefined ||
      (!invocationAlreadyAcquired &&
        !this.acquireInvocation(invocation.invocationId))
    ) {
      return;
    }
    try {
      let current = operation;
      let delay = !immediately;
      const deadline = Date.now() + this.executionTimeoutMilliseconds;
      for (;;) {
        if (Date.now() >= deadline) {
          this.interrupt(
            current,
            'Capability reconciliation timed out; no automatic replay was attempted.',
          );
          return;
        }
        if (delay) await this.sleep(this.pollMilliseconds);
        delay = true;
        try {
          const worker = await this.capabilities.getInvocation(
            invocation.invocationId,
          );
          const beforeIngest = current;
          const recoverableOutcome = this.supportsRecovery(worker);
          current = this.ingest(current, worker, !recoverableOutcome);
          if (
            worker.status === 'succeeded' ||
            worker.status === 'failed' ||
            worker.status === 'interrupted'
          ) {
            if (worker.status === 'succeeded') {
              await this.advanceAfterCapability(current, worker);
            } else if (recoverableOutcome) {
              this.offerSupportedRecovery(
                current,
                worker,
                this.workerEventDrafts(beforeIngest, worker),
              );
            }
            return;
          }
        } catch (error) {
          if (error instanceof WorkerUnavailableError) continue;
          this.interrupt(
            current,
            error instanceof WorkerInvocationNotFoundError
              ? 'The worker lost this invocation; it was not replayed.'
              : `Capability reconciliation failed: ${error instanceof Error ? error.message : 'unknown error'}`,
          );
          return;
        }
      }
    } finally {
      if (!invocationAlreadyAcquired) {
        this.activeInvocations.delete(invocation.invocationId);
      }
    }
  }

  private ingest(
    operation: InstallationOperationSnapshot,
    worker: WorkerInvocationSnapshot,
    persist = true,
  ): InstallationOperationSnapshot {
    const invocation = operation.currentInvocation;
    if (
      invocation === undefined ||
      worker.invocationId !== invocation.invocationId ||
      worker.operationId !== operation.operationId ||
      worker.capabilityId !== invocation.capabilityId
    ) {
      throw new Error(
        'Worker invocation correlation does not match the installation.',
      );
    }
    const fresh = this.freshWorkerEvents(operation, worker);
    const lastSequence = Math.max(
      invocation.lastWorkerEventSequence,
      ...fresh.map((event) => event.sequence),
    );
    const terminal = ['succeeded', 'failed', 'interrupted'].includes(
      worker.status,
    );
    const outcomeIndeterminate = worker.error?.outcome === 'indeterminate';
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status:
        worker.status === 'failed'
          ? outcomeIndeterminate
            ? 'indeterminate'
            : 'failed'
          : worker.status === 'interrupted'
            ? 'indeterminate'
            : 'running',
      updatedAt: worker.updatedAt,
      ...(terminal && worker.status !== 'succeeded'
        ? {
            completedAt: worker.updatedAt,
            failure: worker.error ?? {
              code: 'capability_failed',
              message: 'The capability failed without structured detail.',
            },
          }
        : {}),
      currentInvocation: {
        ...invocation,
        status: worker.status,
        stage: worker.stage,
        lastWorkerEventSequence: lastSequence,
        workerInstanceId: worker.workerInstanceId,
        ...(worker.reconciledByWorkerInstanceId === undefined
          ? {}
          : {
              reconciledByWorkerInstanceId: worker.reconciledByWorkerInstanceId,
            }),
      },
      stages:
        worker.status === 'failed' || worker.status === 'interrupted'
          ? updateStage(
              operation.stages,
              operation.stage,
              worker.status === 'failed' && !outcomeIndeterminate
                ? 'failed'
                : 'indeterminate',
              worker.error?.message ?? 'Outcome is indeterminate.',
              worker.updatedAt,
            )
          : operation.stages,
    };
    if (persist) {
      this.journal.update(updated, this.workerEventDrafts(operation, worker));
    }
    return updated;
  }

  private workerEventDrafts(
    operation: InstallationOperationSnapshot,
    worker: WorkerInvocationSnapshot,
  ): readonly OperationEventDraft[] {
    return this.freshWorkerEvents(operation, worker).map((event) => ({
      sourceEventKey: `worker:${worker.invocationId}:${event.sequence}`,
      operationId: operation.operationId,
      type: event.type,
      source: 'capability-worker' as const,
      severity: event.severity,
      occurredAt: event.occurredAt,
      payload: event.payload,
    }));
  }

  private async advanceAfterCapability(
    operation: InstallationOperationSnapshot,
    worker: WorkerInvocationSnapshot,
  ): Promise<void> {
    const capabilityId = worker.capabilityId;
    const evidence = {
      kind: capabilityId,
      summary: safeResultSummary(capabilityId, worker.result),
      observedAt: worker.updatedAt,
      source: 'capability-worker',
    };
    const completed = {
      ...operation,
      ...(capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID
        ? {
            deviceIdentity: capturedDeviceIdentity(
              worker.result,
              worker.updatedAt,
            ),
          }
        : {}),
      evidence: [...operation.evidence, evidence],
      stages: updateStage(
        operation.stages,
        operation.stage,
        'completed',
        evidence.summary,
        worker.updatedAt,
      ),
      currentInvocation: operation.currentInvocation,
    };
    if (capabilityId === BOOTSTRAP_PREPARE_CAPABILITY_ID) {
      this.setCheckpoint(
        completed,
        'artifacts',
        'backup-recovery-key',
        'Confirm recovery key custody',
        'Confirm that the private key corresponding to the displayed recovery-key fingerprint is backed up outside this add-on.',
        'The private key is never uploaded to Petlibro Management. Losing it can prevent SSH recovery.',
      );
    } else if (capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID) {
      this.setCheckpoint(
        completed,
        'broker-proof',
        'start-final-broker',
        'Start the final MQTT broker',
        'Release of the temporary listener has been verified. Start the Home Assistant MQTT broker and create the captured feeder account using the protected credential handoff.',
        'The next stage independently proves both backend and feeder broker credentials before OTA.',
      );
    } else if (capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID) {
      this.setCheckpoint(
        completed,
        'ota',
        'stop-final-broker',
        'Release TCP/1883 for guarded OTA',
        'Stop the final broker again. The worker must reacquire the temporary listener to correlate the OTA transaction.',
        'Stopping a shared broker can interrupt unrelated MQTT clients. The worker will verify port acquisition before continuing.',
      );
    } else if (capabilityId === BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID) {
      this.setCheckpoint(
        completed,
        'integration',
        'start-final-broker',
        'Restore the final MQTT broker',
        'The temporary listener and artifact server have been released. Start the final broker through Home Assistant.',
        'The next stages verify final connectivity and configuration adoption; starting the broker is not itself proof of feeder reconnection.',
      );
    } else if (capabilityId === BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID) {
      const observation = await this.observeReadiness(completed);
      const withEvidence: InstallationOperationSnapshot = {
        ...completed,
        evidence: [
          ...completed.evidence,
          {
            kind: 'operational-readiness',
            summary: observation.summary,
            observedAt: this.now(),
            source: 'runtime-observation-adapters',
          },
        ],
      };
      this.setCheckpoint(
        withEvidence,
        'integration',
        'verify-operational-readiness',
        'Verify add-on operational readiness',
        observation.ready
          ? 'Runtime adoption is currently ready. Continue to record end-to-end completion.'
          : `Bootstrap verification passed, but end-to-end readiness is incomplete: ${observation.summary} Restart or reconfigure the add-on as needed, then retry.`,
        'Confirmation triggers a fresh source-aware runtime check. It does not itself prove AppDaemon or configuration readiness.',
      );
    }
  }

  private setCheckpoint(
    operation: InstallationOperationSnapshot,
    stage: string,
    kind: InstallationCheckpoint['kind'],
    title: string,
    instructions: string,
    consequence: string,
    precedingEvents: readonly OperationEventDraft[] = [],
  ): InstallationOperationSnapshot {
    const now = this.now();
    const checkpoint: InstallationCheckpoint = {
      checkpointId: randomUUID(),
      kind,
      title,
      instructions,
      consequence,
      createdAt: now,
    };
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status: 'awaiting-user',
      stage,
      updatedAt: now,
      checkpoint,
      stages: updateStage(operation.stages, stage, 'awaiting-user', title, now),
    };
    this.journal.update(updated, [
      ...precedingEvents,
      this.event(
        updated,
        'installation.checkpoint-created',
        'warning',
        {
          checkpointId: checkpoint.checkpointId,
          kind,
          stage,
        },
        `checkpoint:${checkpoint.checkpointId}:created`,
      ),
    ]);
    return updated;
  }

  private offerSupportedRecovery(
    operation: InstallationOperationSnapshot,
    worker: WorkerInvocationSnapshot,
    precedingEvents: readonly OperationEventDraft[],
  ): void {
    const retryable = resumable(operation);
    if (worker.capabilityId === BOOTSTRAP_PREPARE_CAPABILITY_ID) {
      this.setCheckpoint(
        retryable,
        'artifacts',
        'retry-safe-capability',
        'Retry protected artifact preparation',
        'The worker restarted during local artifact preparation. Review runtime storage and packaged-artifact availability, then retry this local stage.',
        'This retry may replace an incomplete local payload and token, but it does not contact or modify the feeder.',
        precedingEvents,
      );
    } else if (
      worker.capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID
    ) {
      this.setCheckpoint(
        retryable,
        'identity',
        'stop-final-broker',
        'Recover feeder identity capture',
        'The feeder may already target the temporary endpoint. Stop the final broker and retry only after TCP/1883 is released; the listener first waits for reconnection before sending one provisioning datagram.',
        'This is an explicit device-changing recovery attempt. It does not replay OTA.',
        precedingEvents,
      );
    } else if (worker.capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID) {
      this.setCheckpoint(
        retryable,
        'broker-proof',
        'start-final-broker',
        'Correct and recheck final-broker credentials',
        'Ensure the final broker is running and that both the backend and captured feeder accounts are authorized, then retry the bounded authentication proof.',
        'This recovery performs authentication probes only. OTA remains blocked until proof succeeds.',
        precedingEvents,
      );
    } else if (
      worker.capabilityId === BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID
    ) {
      this.setCheckpoint(
        retryable,
        'integration',
        'apply-addon-options',
        'Correct configuration and recheck integration',
        'Review and apply the protected options, restore the final broker, and restart the add-on if required before retrying final connectivity checks.',
        'This device-changing retry may resend OEM provisioning for the final endpoint, then rechecks heartbeat and State Agent evidence. It never repeats OTA.',
        precedingEvents,
      );
    } else if (
      worker.capabilityId === BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID &&
      (worker.error?.code === 'final_broker_not_verified' ||
        worker.error?.code === 'final_broker_verification_mismatch' ||
        worker.error?.code === 'final_broker_verification_expired')
    ) {
      this.setCheckpoint(
        retryable,
        'broker-proof',
        'start-final-broker',
        'Refresh final-broker proof before OTA',
        'Start the final broker and repeat credential verification. The worker rejected OTA before opening the transaction because its protected prerequisite was absent, mismatched, or stale.',
        'No OTA command was transmitted. Successful proof returns this installation to the guarded OTA handoff.',
        precedingEvents,
      );
    }
  }

  private supportsRecovery(worker: WorkerInvocationSnapshot): boolean {
    if (worker.status === 'interrupted') {
      return (
        worker.capabilityId === BOOTSTRAP_PREPARE_CAPABILITY_ID ||
        worker.capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID
      );
    }
    return (
      worker.status === 'failed' &&
      worker.error?.outcome !== 'indeterminate' &&
      (worker.capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID ||
        worker.capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID ||
        worker.capabilityId === BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID ||
        (worker.capabilityId === BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID &&
          (worker.error?.code === 'final_broker_not_verified' ||
            worker.error?.code === 'final_broker_verification_mismatch' ||
            worker.error?.code === 'final_broker_verification_expired')))
    );
  }

  private activateStage(
    operation: InstallationOperationSnapshot,
    stage: string,
  ): InstallationOperationSnapshot {
    const now = this.now();
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status: 'running',
      stage,
      updatedAt: now,
      checkpoint: undefined,
      currentInvocation: undefined,
      stages: updateStage(
        operation.stages,
        stage,
        'active',
        'Capability execution is active.',
        now,
      ),
    };
    this.journal.update(updated, []);
    return updated;
  }

  private capabilityInput(
    operation: InstallationOperationSnapshot,
    capabilityId: BootstrapCapabilityId,
  ): Readonly<Record<string, unknown>> {
    const input = this.secrets.read(operation.operationId);
    const common = { installationId: operation.operationId };
    if (capabilityId === BOOTSTRAP_PREPARE_CAPABILITY_ID) {
      return {
        ...common,
        feederIp: input.feederIp,
        homeAssistantIp: input.homeAssistantIp,
        sshPublicKey: input.sshPublicKey,
      };
    }
    if (capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID) {
      return {
        ...common,
        setupHostIp: input.setupHostIp,
        listenPort: 1883,
        timeoutSeconds: 180,
        sendProvisioning: true,
      };
    }
    if (capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID) {
      return {
        ...common,
        brokerHost: input.brokerHost,
        brokerPort: input.brokerPort,
        backendUsername: input.backendUsername,
        backendPassword: input.backendPassword,
        timeoutSeconds: 5,
      };
    }
    if (capabilityId === BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID) {
      return {
        ...common,
        setupHostIp: input.setupHostIp,
        listenPort: 1883,
        artifactPort: 18080,
        brokerHost: input.brokerHost,
        brokerPort: input.brokerPort,
        backendUsername: input.backendUsername,
        backendPassword: input.backendPassword,
        timeoutSeconds: 600,
      };
    }
    return {
      ...common,
      feederIp: input.feederIp,
      brokerHost: input.brokerHost,
      brokerPort: input.brokerPort,
      backendUsername: input.backendUsername,
      backendPassword: input.backendPassword,
      timeoutSeconds: 90,
    };
  }

  private interrupt(
    operation: InstallationOperationSnapshot,
    message: string,
  ): void {
    if (isTerminalOperationStatus(operation.status)) return;
    const now = this.now();
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status: 'indeterminate',
      updatedAt: now,
      completedAt: now,
      failure: { code: 'worker_reconciliation_failed', message },
      stages: updateStage(
        operation.stages,
        operation.stage,
        'indeterminate',
        message,
        now,
      ),
    };
    this.journal.update(updated, [
      this.event(
        updated,
        'installation.indeterminate',
        'warning',
        {
          message,
          replayed: false,
        },
        'terminal',
      ),
    ]);
  }

  private recordProtectedInputFailure(
    operation: InstallationOperationSnapshot,
    capabilityId: BootstrapCapabilityId,
    error: unknown,
  ): void {
    const now = this.now();
    const otaCompleted = operation.stages.some(
      (stage) => stage.id === 'ota' && stage.status === 'completed',
    );
    const message = `Protected installation input is unavailable or invalid: ${error instanceof Error ? error.message : 'unknown custody error'}`;
    const status = otaCompleted ? 'indeterminate' : 'failed';
    const updated: InstallationOperationSnapshot = {
      ...operation,
      status,
      updatedAt: now,
      completedAt: now,
      failure: {
        code: 'protected_installation_input_unavailable',
        message,
        outcome: otaCompleted ? 'indeterminate' : 'failed',
      },
      stages: updateStage(
        operation.stages,
        operation.stage,
        otaCompleted ? 'indeterminate' : 'failed',
        message,
        now,
      ),
    };
    this.journal.update(updated, [
      this.event(
        updated,
        otaCompleted ? 'installation.indeterminate' : 'installation.failed',
        'error',
        { capabilityId, code: 'protected_installation_input_unavailable' },
        `capability:${capabilityId}:protected-input-unavailable`,
      ),
    ]);
  }

  private freshWorkerEvents(
    operation: InstallationOperationSnapshot,
    worker: WorkerInvocationSnapshot,
  ): readonly WorkerInvocationSnapshot['events'][number][] {
    const lastSequence =
      operation.currentInvocation?.lastWorkerEventSequence ?? 0;
    const fresh = worker.events.filter(
      (event) => event.sequence > lastSequence,
    );
    let expected = lastSequence + 1;
    for (const event of fresh) {
      if (event.sequence !== expected) {
        throw new Error(
          `Worker event sequence gap: expected ${expected}, received ${event.sequence}.`,
        );
      }
      expected += 1;
    }
    return fresh;
  }

  private installation(operationId: string): InstallationOperationSnapshot {
    const operation = this.journal.get(operationId);
    if (operation?.actionId !== INSTALLATION_ACTION_ID) {
      throw new InstallationCheckpointError('The installation was not found.');
    }
    return operation;
  }

  private event(
    operation: InstallationOperationSnapshot,
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

  private now(): string {
    return this.clock().toISOString();
  }

  private acquireInvocation(invocationId: string): boolean {
    if (this.activeInvocations.has(invocationId)) return false;
    this.activeInvocations.add(invocationId);
    return true;
  }

  private serializeTransition<T>(work: () => Promise<T>): Promise<T> {
    const result = this.transitionQueue.then(work, work);
    this.transitionQueue = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }

  private async observeReadiness(
    operation: InstallationOperationSnapshot,
  ): Promise<InstallationReadinessObservation> {
    if (this.readiness === undefined) {
      return {
        ready: false,
        summary:
          'Operational readiness observation is unavailable in this runtime; bootstrap completion cannot be promoted to end-to-end installation success.',
        checks: { runtimeObserverAvailable: false },
      };
    }
    return this.readiness.inspect(
      operation,
      this.secrets.read(operation.operationId).backendPassword,
    );
  }
}

function capabilityForStage(stage: string): BootstrapCapabilityId | undefined {
  if (stage === 'artifacts') return BOOTSTRAP_PREPARE_CAPABILITY_ID;
  if (stage === 'identity') return BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID;
  if (stage === 'broker-proof') return BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID;
  if (stage === 'ota') return BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID;
  if (stage === 'integration')
    return BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID;
  return undefined;
}

function resumable(
  operation: InstallationOperationSnapshot,
): InstallationOperationSnapshot {
  const { completedAt: _completedAt, failure: _failure, ...active } = operation;
  return active;
}

function updateStage(
  stages: readonly InstallationStageSnapshot[],
  stageId: string,
  status: InstallationStageSnapshot['status'],
  detail: string,
  updatedAt: string,
): readonly InstallationStageSnapshot[] {
  return stages.map((stage) =>
    stage.id === stageId ? { ...stage, status, detail, updatedAt } : stage,
  );
}

function configurationFingerprint(input: SetupPreflightRequest): string {
  return createHash('sha256')
    .update(
      JSON.stringify({
        feederIp: input.feederIp,
        setupHostIp: input.setupHostIp,
        homeAssistantIp: input.homeAssistantIp,
        brokerHost: input.brokerHost,
        brokerPort: input.brokerPort,
        backendUsername: input.backendUsername,
        sshPublicKey: input.sshPublicKey.trim(),
      }),
    )
    .digest('hex');
}

function sshFingerprint(value: string): string {
  const fields = value.trim().split(/\s+/);
  const blob = fields[1];
  if (blob === undefined) return 'invalid';
  return `SHA256:${createHash('sha256').update(Buffer.from(blob, 'base64')).digest('base64url')}`;
}

function safeResultSummary(capabilityId: string, result: unknown): string {
  if (capabilityId === BOOTSTRAP_PREPARE_CAPABILITY_ID) {
    return 'Device-bound payload integrity verified and protected recovery material custodied.';
  }
  if (capabilityId === BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID) {
    return 'Factory identity, supported firmware startup, and OTA subscription were observed from the configured feeder.';
  }
  if (capabilityId === BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID) {
    return 'The final broker accepted both backend and captured feeder credentials.';
  }
  if (capabilityId === BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID) {
    return 'The correlated OTA transaction, payload transfer, terminal report, and post-OTA startup were verified.';
  }
  if (capabilityId === BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID) {
    return 'The final endpoint, a fresh non-retained feeder heartbeat, and authenticated State Agent health were verified. Controller readiness is checked separately.';
  }
  return typeof result === 'object' && result !== null
    ? 'Capability returned structured evidence.'
    : 'Capability completed.';
}

function capturedDeviceIdentity(
  result: unknown,
  capturedAt: string,
): NonNullable<InstallationOperationSnapshot['deviceIdentity']> {
  if (
    typeof result !== 'object' ||
    result === null ||
    !('clientId' in result) ||
    typeof result.clientId !== 'string' ||
    result.clientId.length === 0
  ) {
    throw new Error(
      'Identity capture completed without a usable feeder client identity.',
    );
  }
  return {
    product: 'PLAF203',
    serial: result.clientId,
    source: 'factory-mqtt-connect',
    capturedAt,
  };
}
