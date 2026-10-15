// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { SetupPreflightService } from './setup-preflight-service';

describe('SetupPreflightService', () => {
  it('verifies safe inputs, broker authentication, and free listener ownership', async () => {
    const service = new SetupPreflightService(
      async () => ({
        kind: 'authenticated',
        observedAt: '2026-10-14T12:00:00.000Z',
      }),
      async () => 'available',
      () => new Date('2026-10-14T12:00:01.000Z'),
    );

    const result = await service.run(input());

    expect(result.readyForBootstrapSession).toBe(true);
    expect(result.checks).toSatisfy((checks: typeof result.checks) =>
      checks.every((check) => check.status === 'passed'),
    );
    expect(JSON.stringify(result)).not.toContain('broker-secret');
    expect(JSON.stringify(result)).not.toContain('backend-user');
  });

  it('requires an explicit same-host broker handoff instead of claiming readiness', async () => {
    const service = new SetupPreflightService(
      async () => ({
        kind: 'authenticated',
        observedAt: '2026-10-14T12:00:00.000Z',
      }),
      async () => 'occupied',
    );

    const result = await service.run(input());

    expect(result.readyForBootstrapSession).toBe(false);
    expect(result.checks).toContainEqual(
      expect.objectContaining({
        id: 'temporary-mqtt-port',
        status: 'blocked',
        detail: expect.stringContaining('operator-controlled handoff'),
      }),
    );
  });

  it('does not make network probes when static safety validation fails', async () => {
    let probes = 0;
    const service = new SetupPreflightService(
      async () => {
        probes += 1;
        return { kind: 'unreachable', observedAt: new Date().toISOString() };
      },
      async () => {
        probes += 1;
        return 'available';
      },
    );

    const result = await service.run({
      ...input(),
      feederIp: 'not-an-address',
      brokerPort: 1884,
      sshPublicKey: 'invalid',
    });

    expect(result.readyForBootstrapSession).toBe(false);
    expect(probes).toBe(0);
  });
});

function input() {
  return {
    feederIp: '192.0.2.20',
    setupHostIp: '192.0.2.10',
    homeAssistantIp: '192.0.2.11',
    brokerHost: '192.0.2.10',
    brokerPort: 1883,
    backendUsername: 'backend-user',
    backendPassword: 'broker-secret',
    sshPublicKey: ed25519Key(),
  };
}

function ed25519Key(): string {
  const algorithm = Buffer.from('ssh-ed25519', 'ascii');
  const key = Buffer.alloc(32, 0x42);
  const blob = Buffer.concat([field(algorithm), field(key)]);
  return `ssh-ed25519 ${blob.toString('base64')} test-key`;
}

function field(value: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(value.length);
  return Buffer.concat([length, value]);
}
