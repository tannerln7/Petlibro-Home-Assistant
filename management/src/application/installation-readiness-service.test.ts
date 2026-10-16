// @vitest-environment node

import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import type { InstallationOperationSnapshot } from '#/domain/operations';
import { RuntimeFileAdapter } from '#/infrastructure/observations/runtime-file-adapter';
import { InstallationReadinessService } from './installation-readiness-service';

const directories: string[] = [];
const now = new Date('2026-10-15T08:00:00.000Z');

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('installation operational readiness', () => {
  it('requires the requested protected broker credential and fresh controller evidence', async () => {
    const directory = runtimeFiles('2026-10-15T07:59:30.000Z');
    const service = new InstallationReadinessService(
      new RuntimeFileAdapter(directory),
      () => now,
    );

    const result = await service.inspect(installation(), 'broker-secret');

    expect(result.ready).toBe(true);
    expect(result.checks).toMatchObject({
      effectiveConfigurationAdopted: true,
      installationIdentityCaptured: true,
      deviceRegistryFresh: true,
      feederIdentityObservedAtExpectedAddress: true,
      controllerObservationFresh: true,
      controllerReady: true,
    });
  });

  it('does not promote stale observations or mismatched protected configuration', async () => {
    const directory = runtimeFiles('2026-10-15T07:50:00.000Z');
    const service = new InstallationReadinessService(
      new RuntimeFileAdapter(directory),
      () => now,
    );

    const result = await service.inspect(installation(), 'different-secret');

    expect(result.ready).toBe(false);
    expect(result.checks).toMatchObject({
      effectiveConfigurationAdopted: false,
      installationIdentityCaptured: true,
      deviceRegistryFresh: false,
      feederIdentityObservedAtExpectedAddress: false,
      controllerObservationFresh: false,
      controllerReady: false,
    });
  });

  it('does not accept a different feeder that reused the installation IP address', async () => {
    const directory = runtimeFiles('2026-10-15T07:59:30.000Z', 'other-serial');
    const service = new InstallationReadinessService(
      new RuntimeFileAdapter(directory),
      () => now,
    );

    const result = await service.inspect(installation(), 'broker-secret');

    expect(result.ready).toBe(false);
    expect(result.checks).toMatchObject({
      installationIdentityCaptured: true,
      feederIdentityObservedAtExpectedAddress: false,
      controllerObservationFresh: false,
      controllerReady: false,
    });
  });
});

function runtimeFiles(observedAt: string, serial = 'serial'): string {
  const directory = mkdtempSync(join(tmpdir(), 'petlibro-readiness-'));
  directories.push(directory);
  write(directory, 'options.json', {
    mqtt_host: '192.0.2.10',
    mqtt_port: 1883,
    mqtt_username: 'backend-user',
    mqtt_password: 'broker-secret',
    persist_feeder_mqtt: true,
    feeder_mqtt_host: '192.0.2.10',
    feeder_mqtt_port: 1883,
    petlibro_state_agent_url: 'http://{ip}:8765',
    petlibro_state_agent_token: 'configured-token',
  });
  write(directory, 'devices.json', {
    schema_version: 1,
    last_update: observedAt,
    devices: {
      feeder: {
        product: 'PLAF203',
        serial,
        ip_address: '192.0.2.20',
      },
    },
  });
  write(directory, `petlibro_controller_status_PLAF203_${serial}.json`, {
    schema_version: 1,
    product: 'PLAF203',
    serial,
    lifecycle: 'running',
    reconciliation_state: 'READY',
    feeder_mqtt_connected: true,
    state_agent_healthy: true,
    writes_ready: true,
    write_pending: false,
    last_update: observedAt,
    verified_state: null,
  });
  return directory;
}

function write(directory: string, name: string, value: unknown): void {
  writeFileSync(join(directory, name), JSON.stringify(value));
}

function installation(): InstallationOperationSnapshot {
  return {
    operationId: 'installation-1',
    actionId: 'installation.bootstrap',
    capabilityId: 'installation.workflow.v1',
    target: { kind: 'feeder-installation', label: '192.0.2.20' },
    status: 'awaiting-user',
    stage: 'integration',
    createdAt: now.toISOString(),
    updatedAt: now.toISOString(),
    configuration: {
      feederIp: '192.0.2.20',
      setupHostIp: '192.0.2.2',
      homeAssistantIp: '192.0.2.2',
      brokerHost: '192.0.2.10',
      brokerPort: 1883,
      backendUsername: 'backend-user',
      sshPublicKeyFingerprint: 'SHA256:synthetic',
      configurationFingerprint: 'synthetic',
    },
    deviceIdentity: {
      product: 'PLAF203',
      serial: 'serial',
      source: 'factory-mqtt-connect',
      capturedAt: '2026-10-15T07:55:00.000Z',
    },
    stages: [],
    evidence: [],
    satisfiedCheckpointIds: [],
  };
}
