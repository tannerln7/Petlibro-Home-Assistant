// @vitest-environment node

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { createConnection, createServer, type Socket } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import {
  type CapturedFeederIdentity,
  executeGuardedOta,
} from './bootstrap-network';
import {
  buildPublish,
  encodeMqttString,
  encodeRemainingLength,
  parsePublish,
} from './oem-protocol';

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('guarded OTA synthetic transaction', () => {
  it('correlates MQTT publication, artifact transfer, terminal report, and reconnect', async () => {
    const result = await runTransaction(true);

    expect(result).toMatchObject({
      payloadDownloaded: true,
      publishAcknowledged: true,
      terminalReport: 'accepted',
      postOtaStartupObserved: true,
    });
  });

  it('classifies a missing artifact transfer after publication as indeterminate', async () => {
    await expect(runTransaction(false)).rejects.toMatchObject({
      code: 'artifact_download_timeout',
      outcome: 'indeterminate',
    });
  });

  it('rejects a changed firmware version in the fresh OTA session before publication', async () => {
    await expect(
      runTransaction(false, '3.1.48', '3.1.49'),
    ).rejects.toMatchObject({
      code: 'firmware_identity_mismatch',
      outcome: 'failed',
    });
  });
});

async function runTransaction(
  downloadArtifact: boolean,
  expectedVersion = '3.1.47',
  currentVersion = expectedVersion,
) {
  const directory = mkdtempSync(join(tmpdir(), 'petlibro-ota-fixture-'));
  directories.push(directory);
  const payload = Buffer.from('synthetic device-bound payload');
  const payloadPath = join(directory, 'payload.bin');
  writeFileSync(payloadPath, payload);
  const listenPort = await freePort();
  const artifactPort = await freePort();
  const identity: CapturedFeederIdentity = {
    clientId: 'SERIAL123',
    username: 'factory-user',
    password: new TextEncoder().encode('factory-secret'),
    peerIp: '127.0.0.1',
    protocolLevel: 4,
    softwareVersion: expectedVersion,
    otaSubscriptionObserved: true,
    connectionGeneration: 1,
  };
  const transaction = executeGuardedOta({
    listenHost: '127.0.0.1',
    listenPort,
    artifactPort,
    feederIp: '127.0.0.1',
    payloadPath,
    payloadMd5: '0'.repeat(32),
    identity,
    timeoutMs: 250,
  });
  const feeder = exerciseFeeder(listenPort, downloadArtifact, currentVersion);
  try {
    const [result] = await Promise.all([transaction, feeder]);
    return result;
  } catch (error) {
    await feeder.catch(() => undefined);
    throw error;
  }
}

async function exerciseFeeder(
  listenPort: number,
  downloadArtifact: boolean,
  currentVersion: string,
): Promise<void> {
  let socket = await connectWithRetry(listenPort);
  socket.write(connectPacket());
  socket.write(startupPacket(currentVersion));
  socket.write(subscribePacket(7));
  const command = parsePublish(await waitForPacket(socket, 3));
  const value = JSON.parse(new TextDecoder().decode(command.payload)) as {
    msgId: string;
    url: string;
  };
  socket.write(Uint8Array.from([0x40, 0x02, 0x00, 0x01]));
  if (downloadArtifact) {
    const response = await fetch(value.url);
    expect(response.status).toBe(200);
    expect(Buffer.from(await response.arrayBuffer()).toString()).toBe(
      'synthetic device-bound payload',
    );
  }
  socket.write(
    buildPublish(
      'dl/PLAF203/SERIAL123/device/ota/post',
      new TextEncoder().encode(
        JSON.stringify({ cmd: 'OTA_INFORM', msgId: value.msgId, state: 1 }),
      ),
      8,
      1,
    ),
  );
  await delay(10);
  socket.destroy();

  socket = await connectWithRetry(listenPort);
  socket.write(connectPacket());
  socket.write(startupPacket('3.1.48'));
  await delay(20);
  socket.destroy();
}

async function connectWithRetry(port: number): Promise<Socket> {
  let lastError: unknown;
  for (let attempt = 0; attempt < 50; attempt += 1) {
    try {
      return await connect(port);
    } catch (error) {
      lastError = error;
      await delay(5);
    }
  }
  throw lastError;
}

async function connect(port: number): Promise<Socket> {
  const socket = createConnection({ host: '127.0.0.1', port });
  await new Promise<void>((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  return socket;
}

async function waitForPacket(
  socket: Socket,
  type: number,
): Promise<Uint8Array> {
  return new Promise((resolve, reject) => {
    let buffer = Buffer.alloc(0);
    const timeout = setTimeout(
      () => finish(new Error('MQTT packet timeout')),
      500,
    );
    const finish = (error?: Error, packet?: Uint8Array) => {
      clearTimeout(timeout);
      socket.off('data', onData);
      socket.off('error', onError);
      if (error !== undefined) reject(error);
      else resolve(packet as Uint8Array);
    };
    const onError = (error: Error) => finish(error);
    const onData = (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      for (;;) {
        const extracted = extractPacket(buffer);
        if (extracted === undefined) return;
        buffer = Buffer.from(extracted.remainder);
        if ((extracted.packet[0] as number) >> 4 === type) {
          finish(undefined, extracted.packet);
          return;
        }
      }
    };
    socket.on('data', onData);
    socket.once('error', onError);
  });
}

function extractPacket(
  buffer: Buffer,
): { readonly packet: Uint8Array; readonly remainder: Buffer } | undefined {
  if (buffer.length < 2) return undefined;
  let multiplier = 1;
  let remaining = 0;
  let offset = 1;
  for (;;) {
    if (offset >= buffer.length) return undefined;
    const byte = buffer[offset] as number;
    remaining += (byte & 0x7f) * multiplier;
    offset += 1;
    if ((byte & 0x80) === 0) break;
    multiplier *= 128;
  }
  const total = offset + remaining;
  if (buffer.length < total) return undefined;
  return {
    packet: buffer.subarray(0, total),
    remainder: buffer.subarray(total),
  };
}

async function freePort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (address === null || typeof address === 'string') {
    throw new Error('Could not allocate a loopback port.');
  }
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return address.port;
}

function connectPacket(): Uint8Array {
  const variable = Uint8Array.from([
    ...encodeMqttString('MQTT'),
    4,
    0xc2,
    0,
    60,
  ]);
  const payload = Uint8Array.from([
    ...encodeMqttString('SERIAL123'),
    ...encodeMqttString('factory-user'),
    ...encodeMqttString(new TextEncoder().encode('factory-secret')),
  ]);
  return Uint8Array.from([
    0x10,
    ...encodeRemainingLength(variable.length + payload.length),
    ...variable,
    ...payload,
  ]);
}

function subscribePacket(packetId: number): Uint8Array {
  const topic = encodeMqttString('dl/PLAF203/SERIAL123/device/ota/sub');
  const body = Uint8Array.from([0, packetId, ...topic, 1]);
  return Uint8Array.from([
    0x82,
    ...encodeRemainingLength(body.length),
    ...body,
  ]);
}

function startupPacket(version: string): Uint8Array {
  return buildPublish(
    'dl/PLAF203/SERIAL123/device/event/post',
    new TextEncoder().encode(
      JSON.stringify({
        cmd: 'DEVICE_START_EVENT',
        msgId: `start-${version}`,
        softwareVersion: version,
      }),
    ),
  );
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}
