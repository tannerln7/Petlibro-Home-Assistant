// @vitest-environment node

import { randomUUID } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import type { StateAgentHealthResult } from '#/domain/operations';
import { DurableInvocationManager } from './run';

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('native capability invocation ownership', () => {
  it('deduplicates an invocation and never persists secret input', async () => {
    const directory = temporaryDirectory();
    const database = join(directory, 'worker.sqlite');
    let calls = 0;
    const manager = new DurableInvocationManager(database, async () => {
      calls += 1;
      return result();
    });
    const request = invocationRequest();

    const first = manager.submit(request.invocationId, request);
    const second = manager.submit(request.invocationId, request);
    const terminal = await waitForTerminal(manager, request.invocationId);

    expect(first.created).toBe(true);
    expect(second.created).toBe(false);
    expect(terminal.status).toBe('succeeded');
    expect(calls).toBe(1);
    expect(JSON.stringify(terminal)).not.toContain(request.input.token);
    manager.close();
  });

  it('rejects rebinding an invocation ID to different input', async () => {
    const manager = new DurableInvocationManager(':memory:', async () => result());
    const request = invocationRequest();
    manager.submit(request.invocationId, request);
    expect(() =>
      manager.submit(request.invocationId, {
        ...request,
        input: { ...request.input, token: 'different-secret' },
      }),
    ).toThrow('already bound');
    await waitForTerminal(manager, request.invocationId);
    manager.close();
  });

  it('durably classifies an in-flight invocation after worker restart without replay', async () => {
    const directory = temporaryDirectory();
    const database = join(directory, 'worker.sqlite');
    const manager = new DurableInvocationManager(
      database,
      () => new Promise<StateAgentHealthResult>(() => {}),
    );
    const request = invocationRequest();
    manager.submit(request.invocationId, request);
    await waitForStatus(manager, request.invocationId, 'running');
    const originalWorkerInstanceId = manager.instanceId;
    manager.close();

    const recovered = new DurableInvocationManager(database, async () => {
      throw new Error('must not replay');
    });
    const snapshot = recovered.get(request.invocationId);
    expect(snapshot).toMatchObject({
      status: 'interrupted',
      stage: 'interrupted',
      error: { code: 'capability_worker_restarted' },
    });
    expect(snapshot.workerInstanceId).toBe(originalWorkerInstanceId);
    expect(snapshot.reconciledByWorkerInstanceId).toBe(recovered.instanceId);
    expect(snapshot.reconciledByWorkerInstanceId).not.toBe(
      originalWorkerInstanceId,
    );
    expect(snapshot.events.at(-1)?.payload).toMatchObject({
      originalWorkerInstanceId,
      reconciledByWorkerInstanceId: recovered.instanceId,
    });
    recovered.close();
  });
});

function invocationRequest() {
  return {
    protocolVersion: 1,
    invocationId: randomUUID(),
    operationId: randomUUID(),
    capabilityId: 'state-agent.inspect-health.v1',
    input: {
      baseUrl: 'http://192.0.2.20:8080',
      token: 'synthetic-secret-token',
      timeoutSeconds: 1,
    },
  } as const;
}

function result(): StateAgentHealthResult {
  return {
    healthy: true,
    agent: { name: 'agent', version: '1', uptimeMs: 1 },
    stateDecode: { ok: true, expectedBytes: 236, actualBytes: 236 },
  };
}

async function waitForTerminal(
  manager: DurableInvocationManager,
  invocationId: string,
) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    const snapshot = manager.get(invocationId);
    if (snapshot.status === 'succeeded' || snapshot.status === 'failed') {
      return snapshot;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error('invocation did not finish');
}

async function waitForStatus(
  manager: DurableInvocationManager,
  invocationId: string,
  status: string,
) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    if (manager.get(invocationId).status === status) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error(`invocation did not reach ${status}`);
}

function temporaryDirectory(): string {
  const directory = mkdtempSync(join(tmpdir(), 'petlibro-native-worker-'));
  directories.push(directory);
  return directory;
}
