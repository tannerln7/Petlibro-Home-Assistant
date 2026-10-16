// @vitest-environment node

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import type { BootstrapCapabilityId } from '#/domain/capabilities';
import {
  INSTALLATION_ACTION_ID,
  type InstallationOperationSnapshot,
} from '#/domain/operations';
import type { SetupPreflightRequest } from '#/domain/setup';
import { InstallationSecretStore } from '#/infrastructure/persistence/installation-secret-store';
import { SqliteOperationJournal } from '#/infrastructure/persistence/sqlite-operation-journal';
import type { WorkerInvocationSnapshot } from '#/infrastructure/worker/worker-contract';
import {
  type InstallationCapabilityProvider,
  InstallationCheckpointError,
  InstallationCoordinator,
} from './installation-coordinator';
import { SetupPreflightService } from './setup-preflight-service';

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('durable installation coordination', () => {
  it('advances one operation through verified checkpoints and capability invocations', async () => {
    let readinessChecks = 0;
    const fixture = createFixture(undefined, undefined, {
      inspect: async () => {
        readinessChecks += 1;
        return {
          ready: readinessChecks >= 3,
          summary:
            readinessChecks >= 3
              ? 'Synthetic runtime is ready.'
              : 'Synthetic runtime is not ready yet.',
          checks: { controllerReady: readinessChecks >= 3 },
        };
      },
    });
    const started = await fixture.coordinator.start(setupInput());
    expect(started.actionId).toBe(INSTALLATION_ACTION_ID);

    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    expect(operation.checkpoint?.kind).toBe('backup-recovery-key');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    expect(operation.checkpoint?.kind).toBe('stop-final-broker');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    expect(operation.checkpoint?.kind).toBe('start-final-broker');
    const handoff = await fixture.coordinator.configurationHandoff(
      operation.operationId,
    );
    expect(handoff.feederBrokerAccount.password).toBe('factory-secret');
    expect(handoff.addonOptionsPatch).toMatchObject({
      mqtt_host: '192.0.2.10',
      mqtt_password: 'broker-secret',
      petlibro_state_agent_token: 'agent-secret',
    });
    expect(
      JSON.stringify(fixture.journal.queryEvents({ limit: 100 })),
    ).not.toContain('factory-secret');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    expect(operation.checkpoint?.kind).toBe('stop-final-broker');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    expect(operation.checkpoint?.kind).toBe('start-final-broker');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    expect(operation.checkpoint?.kind).toBe('apply-addon-options');
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    expect(operation.checkpoint?.kind).toBe('verify-operational-readiness');
    const firstReadinessCheckpoint = operation.checkpoint
      ?.checkpointId as string;
    await expect(
      fixture.coordinator.respondToCheckpoint(
        operation.operationId,
        firstReadinessCheckpoint,
      ),
    ).rejects.toThrow('Synthetic runtime is not ready yet.');
    operation = fixture.journal.get(
      operation.operationId,
    ) as InstallationOperationSnapshot;
    expect(operation.status).toBe('awaiting-user');
    expect(operation.checkpoint?.checkpointId).toBe(firstReadinessCheckpoint);
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      firstReadinessCheckpoint,
    );
    operation = await waitForStatus(
      fixture.journal,
      operation.operationId,
      'succeeded',
    );

    expect(
      operation.stages.every((stage) => stage.status === 'completed'),
    ).toBe(true);
    expect(
      operation.evidence.some((item) => item.kind.includes('execute-ota')),
    ).toBe(true);
    const integrationEvidence = operation.evidence.find((item) =>
      item.kind.includes('verify-integration'),
    );
    expect(integrationEvidence?.summary).toContain(
      'fresh non-retained feeder heartbeat',
    );
    expect(integrationEvidence?.summary).toContain(
      'Controller readiness is checked separately',
    );
    expect(JSON.stringify(operation)).not.toContain('broker-secret');
    expect(
      JSON.stringify(fixture.journal.queryEvents({ limit: 100 })),
    ).not.toContain('broker-secret');
    expect(readinessChecks).toBe(3);
    fixture.journal.close();
  });

  it('survives restart while awaiting an operator and treats a repeated response idempotently', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const awaiting = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    const checkpointId = awaiting.checkpoint?.checkpointId as string;
    const restarted = new InstallationCoordinator(
      fixture.journal,
      fixture.worker,
      fixture.preflight,
      fixture.secrets,
      true,
      undefined,
      async () => Promise.resolve(),
      1,
      1_000,
      readyObserver,
    );
    await restarted.ensureInitialized();
    const advanced = await restarted.respondToCheckpoint(
      awaiting.operationId,
      checkpointId,
    );
    const repeated = await restarted.respondToCheckpoint(
      awaiting.operationId,
      checkpointId,
    );

    expect(repeated.operationId).toBe(advanced.operationId);
    expect(repeated.satisfiedCheckpointIds).toContain(checkpointId);
    fixture.journal.close();
  });

  it('does not accept operator confirmation as proof that TCP/1883 was released', async () => {
    const fixture = createFixture(['occupied', 'occupied', 'available']);
    const started = await fixture.coordinator.start(setupInput());
    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    const checkpointId = operation.checkpoint?.checkpointId as string;

    await expect(
      fixture.coordinator.respondToCheckpoint(
        operation.operationId,
        checkpointId,
      ),
    ).rejects.toBeInstanceOf(InstallationCheckpointError);
    const stillWaiting = fixture.journal.get(operation.operationId);
    expect(stillWaiting?.status).toBe('awaiting-user');

    await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      checkpointId,
    );
    const progressed = await waitForCheckpoint(
      fixture.journal,
      operation.operationId,
    );
    expect(progressed.checkpoint?.kind).toBe('start-final-broker');
    fixture.journal.close();
  });

  it('serializes concurrent checkpoint responses and dispatches the next capability once', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const backup = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    const stopped = await fixture.coordinator.respondToCheckpoint(
      backup.operationId,
      backup.checkpoint?.checkpointId as string,
    );
    const checkpointId = stopped.checkpoint?.checkpointId as string;
    await Promise.all([
      fixture.coordinator.respondToCheckpoint(
        stopped.operationId,
        checkpointId,
      ),
      fixture.coordinator.respondToCheckpoint(
        stopped.operationId,
        checkpointId,
      ),
    ]);
    await waitForCheckpoint(fixture.journal, stopped.operationId);
    expect(fixture.worker.submissionsFor('bootstrap.capture-identity.v1')).toBe(
      1,
    );
    fixture.journal.close();
  });

  it('rejects a competing installation while one remains active', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    await waitForCheckpoint(fixture.journal, started.operationId);
    await expect(fixture.coordinator.start(setupInput())).rejects.toThrow(
      /still active or has an unresolved outcome/,
    );
    fixture.journal.close();
  });

  it('keeps an older unresolved installation visible even when bounded history omits it', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const unresolved = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    fixture.journal.list = () => [];
    fixture.journal.listUnresolved = () => [unresolved];

    expect(fixture.coordinator.availability().installation?.operationId).toBe(
      unresolved.operationId,
    );
    fixture.journal.close();
  });

  it('records unavailable protected input as a durable failure during detached continuation', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const awaiting = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    fixture.journal.update(
      {
        ...awaiting,
        status: 'running',
        stage: 'artifacts',
        checkpoint: undefined,
        currentInvocation: undefined,
      },
      [],
    );
    rmSync(
      join(
        fixture.directory,
        'installation-secrets',
        `${started.operationId}.json`,
      ),
    );

    await fixture.restartedCoordinator().ensureInitialized();
    const failed = await waitForStatus(
      fixture.journal,
      started.operationId,
      'failed',
    );

    expect(failed.failure).toMatchObject({
      code: 'protected_installation_input_unavailable',
      outcome: 'failed',
    });
    fixture.journal.close();
  });

  it('preserves uncertainty when protected input is lost after OTA completion', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const awaiting = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    fixture.journal.update(
      {
        ...awaiting,
        status: 'running',
        stage: 'integration',
        checkpoint: undefined,
        currentInvocation: undefined,
        stages: awaiting.stages.map((stage) =>
          stage.id === 'ota'
            ? { ...stage, status: 'completed' as const }
            : stage,
        ),
      },
      [],
    );
    rmSync(
      join(
        fixture.directory,
        'installation-secrets',
        `${started.operationId}.json`,
      ),
    );

    await fixture.restartedCoordinator().ensureInitialized();
    const indeterminate = await waitForStatus(
      fixture.journal,
      started.operationId,
      'indeterminate',
    );

    expect(indeterminate.failure).toMatchObject({
      code: 'protected_installation_input_unavailable',
      outcome: 'indeterminate',
    });
    fixture.journal.close();
  });

  it('offers an explicit safe retry when local artifact preparation is interrupted', async () => {
    const fixture = createFixture(
      undefined,
      undefined,
      undefined,
      new Set(['bootstrap.prepare-artifacts.v1']),
    );
    const started = await fixture.coordinator.start(setupInput());
    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    expect(operation.checkpoint?.kind).toBe('retry-safe-capability');
    fixture.worker.succeedCapability('bootstrap.prepare-artifacts.v1');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);

    expect(operation.checkpoint?.kind).toBe('backup-recovery-key');
    expect(
      fixture.worker.submissionsFor('bootstrap.prepare-artifacts.v1'),
    ).toBe(2);
    fixture.journal.close();
  });

  it('continues a checkpoint response persisted before its advancement step', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const awaiting = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    const checkpoint = awaiting.checkpoint;
    if (checkpoint === undefined) throw new Error('checkpoint missing');
    fixture.journal.update(
      {
        ...awaiting,
        checkpoint: {
          ...checkpoint,
          responseRecordedAt: new Date().toISOString(),
        },
        satisfiedCheckpointIds: [
          ...awaiting.satisfiedCheckpointIds,
          checkpoint.checkpointId,
        ],
      },
      [],
    );
    const restarted = fixture.restartedCoordinator();
    await restarted.ensureInitialized();
    const continued = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    expect(continued.checkpoint?.kind).toBe('stop-final-broker');
    fixture.journal.close();
  });

  it('advances a terminal worker result persisted before workflow progression', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const awaiting = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    if (awaiting.currentInvocation === undefined) {
      throw new Error('prepare invocation missing');
    }
    fixture.journal.update(
      {
        ...awaiting,
        status: 'running',
        checkpoint: undefined,
        currentInvocation: {
          ...awaiting.currentInvocation,
          status: 'succeeded',
          stage: 'completed',
        },
      },
      [],
    );
    const restarted = fixture.restartedCoordinator();
    await restarted.ensureInitialized();
    const continued = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    expect(continued.checkpoint?.kind).toBe('backup-recovery-key');
    fixture.journal.close();
  });

  it('dispatches the stage capability after restart instead of re-advancing the prior invocation', async () => {
    const fixture = createFixture();
    const started = await fixture.coordinator.start(setupInput());
    const backup = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    const stopBroker = await fixture.coordinator.respondToCheckpoint(
      backup.operationId,
      backup.checkpoint?.checkpointId as string,
    );
    const checkpointId = stopBroker.checkpoint?.checkpointId as string;
    fixture.journal.update(
      {
        ...stopBroker,
        status: 'running',
        stage: 'identity',
        checkpoint: undefined,
        satisfiedCheckpointIds: [
          ...stopBroker.satisfiedCheckpointIds,
          checkpointId,
        ],
      },
      [],
    );
    const restarted = fixture.restartedCoordinator();
    await restarted.ensureInitialized();
    const continued = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    expect(continued.checkpoint?.kind).toBe('start-final-broker');
    expect(fixture.worker.submissionsFor('bootstrap.capture-identity.v1')).toBe(
      1,
    );
    fixture.journal.close();
  });

  it('keeps a safe broker-proof failure in the same installation with a retry checkpoint', async () => {
    const fixture = createFixture(
      ['occupied', 'available'],
      new Set(['bootstrap.verify-final-broker.v1']),
    );
    const started = await fixture.coordinator.start(setupInput());
    const persistedStatuses: string[] = [];
    const update = fixture.journal.update.bind(fixture.journal);
    fixture.journal.update = (snapshot, events) => {
      persistedStatuses.push(snapshot.status);
      update(snapshot, events);
    };
    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    expect(operation.status).toBe('awaiting-user');
    expect(operation.stage).toBe('broker-proof');
    expect(operation.checkpoint?.kind).toBe('start-final-broker');
    expect(operation.failure).toBeUndefined();
    expect(persistedStatuses).not.toContain('failed');
    fixture.journal.close();
  });

  it('offers an explicit safe retry when final-broker proof is interrupted', async () => {
    const fixture = createFixture(
      ['occupied', 'available'],
      undefined,
      undefined,
      new Set(['bootstrap.verify-final-broker.v1']),
    );
    const started = await fixture.coordinator.start(setupInput());
    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);

    expect(operation.status).toBe('awaiting-user');
    expect(operation.stage).toBe('broker-proof');
    expect(operation.checkpoint?.kind).toBe('start-final-broker');
    expect(operation.failure).toBeUndefined();
    fixture.worker.succeedCapability('bootstrap.verify-final-broker.v1');

    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);

    expect(operation.checkpoint?.kind).toBe('stop-final-broker');
    expect(
      fixture.worker.submissionsFor('bootstrap.verify-final-broker.v1'),
    ).toBe(2);
    fixture.journal.close();
  });

  it('restarts a newly authorized same-capability retry with a fresh invocation identity', async () => {
    const fixture = createFixture(
      ['occupied', 'available'],
      new Set(['bootstrap.verify-final-broker.v1']),
    );
    const started = await fixture.coordinator.start(setupInput());
    let operation = await waitForCheckpoint(
      fixture.journal,
      started.operationId,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    operation = await fixture.coordinator.respondToCheckpoint(
      operation.operationId,
      operation.checkpoint?.checkpointId as string,
    );
    operation = await waitForCheckpoint(fixture.journal, operation.operationId);
    const priorInvocationId = operation.currentInvocation?.invocationId;
    const retryCheckpointId = operation.checkpoint?.checkpointId as string;
    fixture.worker.succeedCapability('bootstrap.verify-final-broker.v1');

    const update = fixture.journal.update.bind(fixture.journal);
    let injected = false;
    fixture.journal.update = (snapshot, events) => {
      update(snapshot, events);
      if (
        !injected &&
        snapshot.actionId === INSTALLATION_ACTION_ID &&
        snapshot.status === 'running' &&
        snapshot.stage === 'broker-proof' &&
        snapshot.currentInvocation === undefined
      ) {
        injected = true;
        throw new Error(
          'synthetic process interruption after stage activation',
        );
      }
    };
    await expect(
      fixture.coordinator.respondToCheckpoint(
        operation.operationId,
        retryCheckpointId,
      ),
    ).rejects.toThrow('synthetic process interruption');
    fixture.journal.update = update;

    const restarted = fixture.restartedCoordinator();
    await restarted.ensureInitialized();
    const continued = await waitForCheckpoint(
      fixture.journal,
      operation.operationId,
    );
    expect(injected).toBe(true);
    expect(continued.currentInvocation?.invocationId).not.toBe(
      priorInvocationId,
    );
    expect(
      fixture.worker.submissionsFor('bootstrap.verify-final-broker.v1'),
    ).toBe(2);
    expect(continued.checkpoint?.kind).toBe('stop-final-broker');
    fixture.journal.close();
  });
});

function createFixture(
  portResults = ['occupied', 'available', 'available'],
  failures: ReadonlySet<BootstrapCapabilityId> = new Set(),
  readiness = readyObserver,
  interruptions: ReadonlySet<BootstrapCapabilityId> = new Set(),
) {
  const directory = mkdtempSync(join(tmpdir(), 'petlibro-installation-'));
  directories.push(directory);
  const journal = new SqliteOperationJournal(
    join(directory, 'management.sqlite'),
  );
  const worker = new ImmediateWorker(failures, interruptions);
  let portIndex = 0;
  const preflight = new SetupPreflightService(
    async () => ({
      kind: 'authenticated',
      observedAt: new Date().toISOString(),
    }),
    async () =>
      (portResults[Math.min(portIndex++, portResults.length - 1)] ??
        'available') as 'available' | 'occupied' | 'address-unavailable',
  );
  const secrets = new InstallationSecretStore(directory);
  return {
    directory,
    journal,
    worker,
    preflight,
    secrets,
    coordinator: new InstallationCoordinator(
      journal,
      worker,
      preflight,
      secrets,
      true,
      undefined,
      async () => Promise.resolve(),
      1,
      1_000,
      readiness,
    ),
    restartedCoordinator: () =>
      new InstallationCoordinator(
        journal,
        worker,
        preflight,
        secrets,
        true,
        undefined,
        async () => Promise.resolve(),
        1,
        1_000,
        readiness,
      ),
  };
}

class ImmediateWorker implements InstallationCapabilityProvider {
  private readonly snapshots = new Map<string, WorkerInvocationSnapshot>();
  private readonly submitted: BootstrapCapabilityId[] = [];
  private readonly failedCapabilities: Set<BootstrapCapabilityId>;
  private readonly interruptedCapabilities: Set<BootstrapCapabilityId>;

  public constructor(
    failures: ReadonlySet<BootstrapCapabilityId>,
    interruptions: ReadonlySet<BootstrapCapabilityId>,
  ) {
    this.failedCapabilities = new Set(failures);
    this.interruptedCapabilities = new Set(interruptions);
  }

  public async submitInvocation(request: {
    invocationId: string;
    operationId: string;
    capabilityId: BootstrapCapabilityId;
  }): Promise<WorkerInvocationSnapshot> {
    this.submitted.push(request.capabilityId);
    const now = new Date().toISOString();
    const interrupted = this.interruptedCapabilities.has(request.capabilityId);
    const failed = this.failedCapabilities.has(request.capabilityId);
    const snapshot: WorkerInvocationSnapshot = {
      protocolVersion: 1,
      workerInstanceId: 'synthetic-worker',
      invocationId: request.invocationId,
      operationId: request.operationId,
      capabilityId: request.capabilityId,
      status: interrupted ? 'interrupted' : failed ? 'failed' : 'succeeded',
      stage: interrupted ? 'interrupted' : failed ? 'failed' : 'completed',
      acceptedAt: now,
      updatedAt: now,
      events: [
        {
          sequence: 1,
          type: interrupted
            ? 'capability.interrupted'
            : failed
              ? 'capability.failed'
              : 'capability.succeeded',
          occurredAt: now,
          severity: 'info',
          payload: { capabilityId: request.capabilityId },
        },
      ],
      ...(interrupted || failed
        ? {
            error: {
              code: interrupted
                ? 'capability_worker_restarted'
                : 'synthetic_failure',
              message: interrupted
                ? 'Synthetic worker interruption.'
                : 'Synthetic recoverable failure.',
              outcome: 'failed' as const,
            },
          }
        : {
            result:
              request.capabilityId === 'bootstrap.capture-identity.v1'
                ? { clientId: 'serial', verified: true }
                : { verified: true },
          }),
    };
    this.snapshots.set(request.invocationId, snapshot);
    return snapshot;
  }

  public async getInvocation(invocationId: string) {
    const snapshot = this.snapshots.get(invocationId);
    if (snapshot === undefined) throw new Error('missing synthetic invocation');
    return snapshot;
  }

  public async getConfigurationHandoff(installationId: string) {
    return {
      installationId,
      feederBrokerAccount: {
        clientId: 'serial',
        username: 'feeder',
        password: 'factory-secret',
        publishTopic: 'dl/PLAF203/serial/device/+/post',
        subscribeTopic: 'dl/PLAF203/serial/device/+/sub',
      },
      generatedAddonOptions: {
        petlibro_state_agent_url: 'http://{ip}:8765',
        petlibro_state_agent_token: 'agent-secret',
      },
    };
  }

  public submissionsFor(capabilityId: BootstrapCapabilityId): number {
    return this.submitted.filter((item) => item === capabilityId).length;
  }

  public succeedCapability(capabilityId: BootstrapCapabilityId): void {
    this.failedCapabilities.delete(capabilityId);
    this.interruptedCapabilities.delete(capabilityId);
  }
}

const readyObserver = {
  inspect: async () => ({
    ready: true,
    summary: 'Synthetic runtime is ready.',
    checks: { controllerReady: true },
  }),
};

async function waitForCheckpoint(
  journal: SqliteOperationJournal,
  operationId: string,
) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    const operation = journal.get(operationId);
    if (
      operation?.actionId === INSTALLATION_ACTION_ID &&
      operation.status === 'awaiting-user'
    ) {
      return operation;
    }
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error('installation did not reach a checkpoint');
}

async function waitForStatus(
  journal: SqliteOperationJournal,
  operationId: string,
  status: string,
) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    const operation = journal.get(operationId);
    if (
      operation?.actionId === INSTALLATION_ACTION_ID &&
      operation.status === status
    ) {
      return operation;
    }
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error(`installation did not reach ${status}`);
}

function setupInput(): SetupPreflightRequest {
  return {
    feederIp: '192.0.2.20',
    setupHostIp: '192.0.2.10',
    homeAssistantIp: '192.0.2.10',
    brokerHost: '192.0.2.10',
    brokerPort: 1883,
    backendUsername: 'backend',
    backendPassword: 'broker-secret',
    sshPublicKey: ed25519Key(),
  };
}

function ed25519Key(): string {
  const algorithm = field(Buffer.from('ssh-ed25519'));
  const key = field(Buffer.alloc(32, 0x42));
  return `ssh-ed25519 ${Buffer.concat([algorithm, key]).toString('base64')} test`;
}

function field(value: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(value.length);
  return Buffer.concat([length, value]);
}
