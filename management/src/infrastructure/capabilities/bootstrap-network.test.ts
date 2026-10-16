// @vitest-environment node

import { createConnection, createServer, type Socket } from 'node:net';
import { describe, expect, it } from 'vitest';
import {
  BootstrapNetworkError,
  buildBootstrapApplicationResponse,
  classifyPostPublicationFailure,
  TemporaryMqttCapture,
  waitForMqttHeartbeat,
} from './bootstrap-network';
import { bootstrapFailure } from './native-bootstrap-capabilities';
import {
  buildPublish,
  encodeMqttString,
  encodeRemainingLength,
} from './oem-protocol';

describe('bootstrap MQTT application responses', () => {
  const eventTopic = 'dl/PLAF203/serial/device/event/post';

  it('matches the supported correlated startup and grain response shapes', () => {
    const startup = buildBootstrapApplicationResponse(
      eventTopic,
      { cmd: 'DEVICE_START_EVENT', msgId: 'boot-1' },
      123,
    );
    expect(startup?.topic).toBe('dl/PLAF203/serial/device/event/sub');
    expect(JSON.parse(new TextDecoder().decode(startup?.payload))).toEqual({
      cmd: 'DEVICE_START_EVENT',
      ts: 123,
      msgId: 'boot-1',
      code: 0,
    });
    const grain = buildBootstrapApplicationResponse(
      eventTopic,
      { cmd: 'GRAIN_OUTPUT_EVENT', msgId: 'feed-1', execStep: '2' },
      456,
    );
    expect(JSON.parse(new TextDecoder().decode(grain?.payload))).toMatchObject({
      cmd: 'GRAIN_OUTPUT_EVENT',
      msgId: 'feed-1',
      execStep: '2',
      code: 0,
    });
  });

  it('returns a non-calibrating NTP response and never fabricates a feeding plan', () => {
    const ntp = buildBootstrapApplicationResponse(
      'dl/PLAF203/serial/device/ntp/post',
      { cmd: 'NTP' },
      789,
    );
    expect(JSON.parse(new TextDecoder().decode(ntp?.payload))).toMatchObject({
      cmd: 'NTP',
      calibrationTag: false,
      timezoneOffsetSeconds: 0,
    });
    expect(
      buildBootstrapApplicationResponse(
        eventTopic,
        { cmd: 'GET_FEEDING_PLAN_EVENT', msgId: 'plans-1' },
        789,
      ),
    ).toBeUndefined();
  });

  it('preserves an ambiguous OTA effect in the worker failure taxonomy', () => {
    expect(
      bootstrapFailure(
        new BootstrapNetworkError(
          'ota_outcome_indeterminate',
          'confirmation was lost',
          'indeterminate',
        ),
      ),
    ).toEqual({
      code: 'ota_outcome_indeterminate',
      message: 'confirmation was lost',
      outcome: 'indeterminate',
    });
  });

  it('promotes artifact and verification failures after OTA publication to indeterminate', () => {
    expect(
      classifyPostPublicationFailure(
        new BootstrapNetworkError(
          'artifact_download_timeout',
          'payload was not observed',
        ),
      ),
    ).toMatchObject({
      code: 'artifact_download_timeout',
      outcome: 'indeterminate',
    });
    expect(
      classifyPostPublicationFailure(
        new BootstrapNetworkError('ota_rejected', 'device rejected OTA'),
      ),
    ).toMatchObject({ code: 'ota_rejected', outcome: 'failed' });
  });

  it('requires startup and OTA subscription evidence from each connection generation', async () => {
    const events: string[] = [];
    const listener = new TemporaryMqttCapture('127.0.0.1', (type) =>
      events.push(type),
    );
    await listener.listen('127.0.0.1', 0);
    let socket: Socket | undefined;
    try {
      socket = await connectFeeder(listener.listeningPort());
      socket.write(startupPacket('3.1.47'));
      socket.write(subscribePacket(1));
      const first = await listener.waitForStartupEvidence(200);
      expect(first).toMatchObject({
        connectionGeneration: 1,
        softwareVersion: '3.1.47',
        otaSubscriptionObserved: true,
      });

      socket = await connectFeeder(listener.listeningPort());
      expect(await listener.waitForStartupEvidence(20)).toBeUndefined();
      expect(() =>
        listener.publishOta(new TextEncoder().encode('{}'), 'ota-before-sub'),
      ).toThrow('exactly one active OTA subscription');

      socket.write(startupPacket('3.1.48'));
      expect(await listener.waitForStartupEvidence(20)).toBeUndefined();
      socket.write(subscribePacket(2));
      const second = await listener.waitForStartupEvidence(200);
      expect(second).toMatchObject({
        connectionGeneration: 2,
        softwareVersion: '3.1.48',
        otaSubscriptionObserved: true,
      });

      listener.publishOta(new TextEncoder().encode('{}'), 'ota-2');
      socket = await connectFeeder(listener.listeningPort());
      socket.write(Uint8Array.from([0x40, 0x02, 0x00, 0x01]));
      await new Promise((resolve) => setTimeout(resolve, 10));
      expect(events).not.toContain('bootstrap.ota-puback-observed');
    } finally {
      socket?.destroy();
      await listener.close();
    }
  });

  it('classifies durable reporting failure after an OTA send attempt as indeterminate', async () => {
    const events: string[] = [];
    const listener = new TemporaryMqttCapture('127.0.0.1', (type) => {
      events.push(type);
      if (type === 'bootstrap.ota-published') {
        throw new Error('synthetic durable event failure');
      }
    });
    await listener.listen('127.0.0.1', 0);
    const socket = await connectFeeder(listener.listeningPort());
    try {
      socket.write(startupPacket('3.1.48'));
      socket.write(subscribePacket(1));
      await listener.waitForStartupEvidence(200);
      expect(() =>
        listener.publishOta(new TextEncoder().encode('{}'), 'ota-report-fail'),
      ).toThrow(expect.objectContaining({ outcome: 'indeterminate' }));
      expect(events).toContain('bootstrap.ota-publication-starting');
      expect(events).toContain('bootstrap.ota-published');
    } finally {
      socket.destroy();
      await listener.close();
    }
  });

  it('cannot satisfy final heartbeat verification with bytes queued before redirect completion', async () => {
    const topic = 'dl/PLAF203/SERIAL123/device/heart/post';
    const heartbeat = buildPublish(
      topic,
      new TextEncoder().encode(JSON.stringify({ cmd: 'HEARTBEAT' })),
    );
    let connections = 0;
    const broker = createServer((socket) => {
      connections += 1;
      let connected = false;
      socket.on('data', (packet: Buffer) => {
        const type = (packet[0] as number) >> 4;
        if (!connected && type === 1) {
          connected = true;
          socket.write(Uint8Array.from([0x20, 0x02, 0x00, 0x00]));
        } else if (type === 8) {
          socket.write(
            Buffer.concat([
              Buffer.from([0x90, 0x03, 0x00, 0x01, 0x00]),
              Buffer.from(heartbeat),
            ]),
          );
        }
      });
    });
    await new Promise<void>((resolve, reject) => {
      broker.once('error', reject);
      broker.listen(0, '127.0.0.1', resolve);
    });
    const address = broker.address();
    if (address === null || typeof address === 'string') {
      throw new Error('fixture broker did not bind');
    }
    let redirectCompleted = false;
    try {
      await waitForMqttHeartbeat({
        host: '127.0.0.1',
        port: address.port,
        username: 'backend',
        password: 'secret',
        feederClientId: 'SERIAL123',
        timeoutMs: 1_000,
        onSubscribed: async () => {
          await new Promise((resolve) => setTimeout(resolve, 25));
          redirectCompleted = true;
        },
      });
      expect(redirectCompleted).toBe(true);
      expect(connections).toBe(2);
    } finally {
      await new Promise<void>((resolve) => broker.close(() => resolve()));
    }
  });
});

async function connectFeeder(port: number): Promise<Socket> {
  const socket = createConnection({ host: '127.0.0.1', port });
  await new Promise<void>((resolve, reject) => {
    socket.once('connect', resolve);
    socket.once('error', reject);
  });
  socket.write(connectPacket());
  await new Promise((resolve) => setTimeout(resolve, 5));
  return socket;
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
