// @vitest-environment node

import { type ChildProcess, spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { createServer, type Server } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';
import { OperationManager } from '#/application/operation-manager';
import {
  isTerminalOperationStatus,
  STATE_AGENT_INSPECTION_ACTION_ID,
} from '#/domain/operations';
import { SqliteOperationJournal } from '#/infrastructure/persistence/sqlite-operation-journal';
import { CapabilityWorkerClient } from '#/infrastructure/worker/capability-worker-client';

const TOKEN = 'synthetic-cross-process-token';
const cleanup: Array<() => Promise<void> | void> = [];

afterEach(async () => {
  for (const dispose of cleanup.splice(0).reverse()) {
    await dispose();
  }
});

describe('capability execution boundary', () => {
  it('executes State Agent health through Node, UDS worker, HTTP, journal, and result projection', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'petlibro-execution-'));
    cleanup.push(() => rmSync(directory, { recursive: true, force: true }));
    const stateAgent = await startStateAgent();
    cleanup.push(() => closeServer(stateAgent.server));
    const socketPath = join(directory, 'worker.sock');
    const worker = await startWorker(socketPath);
    cleanup.push(() => stopWorker(worker));

    const journal = new SqliteOperationJournal(
      join(directory, 'management.sqlite'),
    );
    cleanup.push(() => journal.close());
    const manager = new OperationManager(
      journal,
      new CapabilityWorkerClient(socketPath, 2_000),
      {
        baseUrl: stateAgent.url,
        token: TOKEN,
        timeoutSeconds: 1,
      },
      undefined,
      undefined,
      20,
      5_000,
    );

    const accepted = await manager.startStateAgentInspection();
    const terminal = await waitForTerminal(manager, accepted.operationId);
    const detail = await manager.getOperationDetail(accepted.operationId);

    expect(terminal.status).toBe('succeeded');
    expect(terminal.result).toEqual({
      healthy: true,
      agent: {
        name: 'plaf203-state-agent',
        version: '0.3.0-test',
        uptimeMs: 125,
      },
      stateDecode: { ok: true, expectedBytes: 236, actualBytes: 236 },
    });
    expect(
      detail.events.find((event) => event.type === 'capability.evidence'),
    ).toMatchObject({
      source: 'capability-worker',
      payload: {
        kind: 'state-agent-health',
        observation: terminal.result,
      },
    });
    expect(JSON.stringify(detail)).not.toContain(TOKEN);
    expect(JSON.stringify(detail)).not.toContain('/synthetic');
  }, 10_000);
});

async function startStateAgent(): Promise<{ server: Server; url: string }> {
  const server = createServer((request, response) => {
    if (
      request.url !== '/health' ||
      request.headers.authorization !== `Bearer ${TOKEN}`
    ) {
      response.writeHead(401).end();
      return;
    }
    const body = JSON.stringify({
      ok: true,
      agent: {
        name: 'plaf203-state-agent',
        version: '0.3.0-test',
        uptime_ms: 125,
      },
      device: { root: '/synthetic' },
      state_decode: { ok: true, expected_size: 236, actual_size: 236 },
    });
    response.writeHead(200, {
      'Content-Type': 'application/json',
      'Content-Length': Buffer.byteLength(body),
    });
    response.end(body);
  });
  await new Promise<void>((resolveListen) =>
    server.listen(0, '127.0.0.1', resolveListen),
  );
  const address = server.address();
  if (address === null || typeof address === 'string') {
    throw new Error('synthetic State Agent did not bind to TCP');
  }
  return { server, url: `http://127.0.0.1:${address.port}` };
}

async function startWorker(socketPath: string): Promise<ChildProcess> {
  const workerEntry = fileURLToPath(
    new URL('../../.worker/run.mjs', import.meta.url),
  );
  const child = spawn(process.execPath, [workerEntry], {
    env: {
      ...process.env,
      PETLIBRO_CAPABILITY_WORKER_SOCKET: socketPath,
      PETLIBRO_MANAGEMENT_DATA_DIR: dirname(socketPath),
    },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr?.on('data', (chunk: Buffer) => {
    stderr += chunk.toString('utf8');
  });
  const deadline = Date.now() + 3_000;
  while (!existsSync(socketPath) && child.exitCode === null) {
    if (Date.now() >= deadline) {
      child.kill('SIGTERM');
      throw new Error(`worker socket did not appear: ${stderr}`);
    }
    await delay(20);
  }
  if (child.exitCode !== null) {
    throw new Error(`worker exited before startup: ${stderr}`);
  }
  return child;
}

async function stopWorker(process: ChildProcess): Promise<void> {
  if (process.exitCode !== null) {
    return;
  }
  process.kill('SIGTERM');
  await Promise.race([
    new Promise<void>((resolveExit) =>
      process.once('exit', () => resolveExit()),
    ),
    delay(2_000).then(() => {
      process.kill('SIGKILL');
    }),
  ]);
}

async function closeServer(server: Server): Promise<void> {
  await new Promise<void>((resolveClose, reject) =>
    server.close((error) =>
      error === undefined ? resolveClose() : reject(error),
    ),
  );
}

async function waitForTerminal(manager: OperationManager, operationId: string) {
  const deadline = Date.now() + 5_000;
  while (Date.now() < deadline) {
    const operation = await manager.getOperation(operationId);
    if (isTerminalOperationStatus(operation.status)) {
      if (operation.actionId !== STATE_AGENT_INSPECTION_ACTION_ID) {
        throw new Error('unexpected operation contract');
      }
      return operation;
    }
    await delay(20);
  }
  throw new Error('cross-process operation did not finish');
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}
