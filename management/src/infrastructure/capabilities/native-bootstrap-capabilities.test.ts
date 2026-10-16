// @vitest-environment node

import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID } from '#/domain/capabilities';
import { BootstrapCustody } from '#/infrastructure/persistence/bootstrap-custody';
import {
  type BootstrapCapabilityError,
  NativeBootstrapCapabilities,
} from './native-bootstrap-capabilities';

const directories: string[] = [];
const installationId = '11111111-1111-4111-8111-111111111111';

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('guarded OTA prerequisites', () => {
  it('fails closed before opening a listener when broker proof is absent', async () => {
    const fixture = preparedCustody();
    const capabilities = new NativeBootstrapCapabilities(
      fixture.directory,
      true,
    );
    await expect(
      capabilities.execute(BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID, otaInput(), {
        report: () => undefined,
      }),
    ).rejects.toMatchObject<Partial<BootstrapCapabilityError>>({
      code: 'final_broker_not_verified',
    });
  });

  it('rejects proof that is not bound to the current credentials', async () => {
    const fixture = preparedCustody();
    fixture.custody.storeBrokerVerification(installationId, {
      brokerHost: '192.0.2.10',
      brokerPort: 1883,
      backendUsername: 'backend',
      credentialBinding: '0'.repeat(64),
      verifiedAt: new Date().toISOString(),
    });
    const capabilities = new NativeBootstrapCapabilities(
      fixture.directory,
      true,
    );
    await expect(
      capabilities.execute(BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID, otaInput(), {
        report: () => undefined,
      }),
    ).rejects.toMatchObject<Partial<BootstrapCapabilityError>>({
      code: 'final_broker_verification_mismatch',
    });
  });

  it('rejects stale broker proof before evaluating an OTA transaction', async () => {
    const fixture = preparedCustody();
    fixture.custody.storeBrokerVerification(installationId, {
      brokerHost: '192.0.2.10',
      brokerPort: 1883,
      backendUsername: 'backend',
      credentialBinding: '0'.repeat(64),
      verifiedAt: '2000-01-01T00:00:00.000Z',
    });
    const capabilities = new NativeBootstrapCapabilities(
      fixture.directory,
      true,
    );
    await expect(
      capabilities.execute(BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID, otaInput(), {
        report: () => undefined,
      }),
    ).rejects.toMatchObject<Partial<BootstrapCapabilityError>>({
      code: 'final_broker_verification_expired',
    });
  });

  it('revalidates the custodied qualified firmware before opening the OTA transaction', async () => {
    const fixture = preparedCustody('3.1.49');
    const capabilities = new NativeBootstrapCapabilities(
      fixture.directory,
      true,
    );
    await expect(
      capabilities.execute(BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID, otaInput(), {
        report: () => undefined,
      }),
    ).rejects.toMatchObject<Partial<BootstrapCapabilityError>>({
      code: 'unsupported_firmware',
    });
  });

  it('rejects a binary MQTT password instead of altering it during configuration handoff', () => {
    const fixture = preparedCustody('3.1.48', Buffer.from([0xff, 0xfe]));
    const capabilities = new NativeBootstrapCapabilities(
      fixture.directory,
      true,
    );
    expect(() => capabilities.configurationHandoff(installationId)).toThrow(
      expect.objectContaining({ code: 'unsupported_mqtt_password_encoding' }),
    );
  });
});

function preparedCustody(
  softwareVersion = '3.1.48',
  password = Buffer.from('factory-password'),
) {
  const directory = mkdtempSync(join(tmpdir(), 'petlibro-custody-'));
  directories.push(directory);
  const custody = new BootstrapCustody(directory);
  custody.initialize({
    installationId,
    feederIp: '192.0.2.20',
    homeAssistantIp: '192.0.2.10',
  });
  custody.storePreparedPayload(
    installationId,
    {
      path: join(directory, 'payload'),
      sha256: '1'.repeat(64),
      md5: '2'.repeat(32),
      bytes: 1,
      preparedAt: new Date().toISOString(),
    },
    '3'.repeat(64),
  );
  custody.storeIdentity(
    installationId,
    {
      clientId: '1234567890',
      username: 'factory-user',
      passwordBase64: password.toString('base64'),
      peerIp: '192.0.2.20',
      protocolLevel: 4,
      softwareVersion,
    },
    new Date().toISOString(),
  );
  return { directory, custody };
}

function otaInput() {
  return {
    installationId,
    setupHostIp: '192.0.2.10',
    listenPort: 1883,
    artifactPort: 18080,
    brokerHost: '192.0.2.10',
    brokerPort: 1883,
    backendUsername: 'backend',
    backendPassword: 'backend-password',
    timeoutSeconds: 60,
  };
}
