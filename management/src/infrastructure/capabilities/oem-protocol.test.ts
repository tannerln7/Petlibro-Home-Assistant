// @vitest-environment node

import { describe, expect, it } from 'vitest';
import {
  buildOtaCommand,
  buildSetMqttServerFrame,
  encodeMqttString,
  encodeRemainingLength,
  OemProtocolError,
  parseConnect,
  parsePublish,
  parseSubscribe,
} from './oem-protocol';

describe('native OEM protocol capability', () => {
  it('matches the reference command 0x02 provisioning frame', () => {
    const frame = buildSetMqttServerFrame('192.0.2.10', 1883, '123456789', 7);
    const endpoint = bytes('192.0.2.10:1883');
    expect(frame.slice(0, 5)).toEqual(Uint8Array.from([0x44, 0x4c, 2, 0, 7]));
    expect(frame[5]).toBe(endpoint.length + 11);
    expect(frame[6]).toBe(endpoint.length);
    expect(frame.slice(7, 7 + endpoint.length)).toEqual(endpoint);
    expect(frame.slice(-11)).toEqual(
      Uint8Array.from([9, ...bytes('123456789'), 0xff]),
    );
  });

  it('preserves reference provisioning constraints', () => {
    expect(() => buildSetMqttServerFrame('host:bad', 1883, '1')).toThrow(
      "without ':'",
    );
    expect(() => buildSetMqttServerFrame('host', 999, '1')).toThrow(
      'four decimal',
    );
    expect(() => buildSetMqttServerFrame('host', 1883, 'member')).toThrow(
      'decimal digits',
    );
  });

  it.each([
    ['MQTT', 4] as const,
    ['MQIsdp', 3] as const,
  ])('parses authenticated MQTT %s CONNECT identity', (name, level) => {
    const flags = 0xc2;
    const variable = Uint8Array.from([
      ...encodeMqttString(name),
      level,
      flags,
      0,
      60,
    ]);
    const payload = Uint8Array.from([
      ...encodeMqttString('SERIAL123'),
      ...encodeMqttString('product-key'),
      ...encodeMqttString(bytes('secret')),
    ]);
    const packet = Uint8Array.from([
      0x10,
      ...encodeRemainingLength(variable.length + payload.length),
      ...variable,
      ...payload,
    ]);
    expect(parseConnect(packet)).toEqual({
      clientId: 'SERIAL123',
      username: 'product-key',
      password: bytes('secret'),
      keepAlive: 60,
      protocolLevel: level,
    });
  });

  it('rejects unsupported MQTT v5 CONNECT', () => {
    const body = Uint8Array.from([
      ...encodeMqttString('MQTT'),
      5,
      2,
      0,
      60,
      0,
      0,
    ]);
    expect(() =>
      parseConnect(
        Uint8Array.from([0x10, ...encodeRemainingLength(body.length), ...body]),
      ),
    ).toThrow('3.1 and 3.1.1');
  });

  it('parses the reference subscribe and publish fixtures', () => {
    const topic = 'dl/PLAF203/SERIAL/device/ota/sub';
    const subscribeBody = Uint8Array.from([
      0,
      9,
      ...encodeMqttString(topic),
      1,
    ]);
    const subscription = parseSubscribe(
      Uint8Array.from([
        0x82,
        ...encodeRemainingLength(subscribeBody.length),
        ...subscribeBody,
      ]),
    );
    expect(subscription).toEqual({
      packetId: 9,
      topics: [{ topic, qos: 1 }],
    });

    const publicationBody = Uint8Array.from([
      ...encodeMqttString('post/topic'),
      0,
      12,
      ...bytes('{"ok":true}'),
    ]);
    expect(
      parsePublish(
        Uint8Array.from([
          0x32,
          ...encodeRemainingLength(publicationBody.length),
          ...publicationBody,
        ]),
      ),
    ).toMatchObject({
      topic: 'post/topic',
      packetId: 12,
      qos: 1,
      payload: bytes('{"ok":true}'),
    });
  });

  it('builds compact, byte-exact reference OTA JSON', () => {
    const command = buildOtaCommand({
      messageId: 'bootstrap-1',
      artifactUrl: 'http://192.0.2.10:18080/token',
      artifactMd5: '0'.repeat(32),
      targetVersion: '3.99.99',
    });
    expect(new TextDecoder().decode(command)).toBe(
      `{"cmd":"OTA_UPGRADE","msgId":"bootstrap-1","ts":0,"upgradeType":2,"url":"http://192.0.2.10:18080/token","targetSoftwareVersion":"3.99.99","md5":"${'0'.repeat(32)}"}`,
    );
  });

  it('rejects malformed lengths instead of partially parsing packets', () => {
    expect(() => parsePublish(Uint8Array.from([0x30, 2, 0]))).toThrow(
      OemProtocolError,
    );
  });
});

function bytes(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}
