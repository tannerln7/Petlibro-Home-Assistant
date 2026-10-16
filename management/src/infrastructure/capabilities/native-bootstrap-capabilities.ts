import { createHmac, timingSafeEqual } from 'node:crypto';
import { statSync } from 'node:fs';
import { resolve } from 'node:path';
import {
  BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID,
  BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID,
  BOOTSTRAP_PREPARE_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID,
  type BootstrapCapabilityId,
} from '#/domain/capabilities';
import { BootstrapCustody } from '#/infrastructure/persistence/bootstrap-custody';
import {
  BootstrapNetworkError,
  captureFeederIdentity,
  executeGuardedOta,
  probeMqttCredentials,
  redirectFeederMqtt,
  waitForMqttHeartbeat,
} from './bootstrap-network';
import { prepareBootstrapPayload } from './bootstrap-payload';
import { inspectStateAgentHealth } from './state-agent-inspector';

export interface BootstrapExecutionContext {
  readonly report: (
    type: string,
    severity: 'info' | 'warning' | 'error',
    payload: Readonly<Record<string, unknown>>,
  ) => void;
}

export class NativeBootstrapCapabilities {
  private readonly custody: BootstrapCustody;
  private readonly artifactDirectory: string;
  private readonly assetDirectory: string;
  private readonly payloadDirectory: string;

  public constructor(
    dataDirectory: string,
    private readonly deviceChangesEnabled: boolean,
    environment: NodeJS.ProcessEnv = process.env,
  ) {
    this.custody = new BootstrapCustody(dataDirectory);
    this.artifactDirectory = resolve(
      environment.PETLIBRO_BOOTSTRAP_ARTIFACT_DIR ??
        '/opt/petlibro-bootstrap-artifacts',
    );
    this.assetDirectory = resolve(
      environment.PETLIBRO_BOOTSTRAP_ASSET_DIR ??
        '/opt/petlibro-management-worker/assets',
    );
    this.payloadDirectory = resolve(dataDirectory, 'bootstrap-payloads');
  }

  public async execute(
    capabilityId: BootstrapCapabilityId,
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ): Promise<unknown> {
    switch (capabilityId) {
      case BOOTSTRAP_PREPARE_CAPABILITY_ID:
        return this.prepare(input, context);
      case BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID:
        return this.capture(input, context);
      case BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID:
        return this.verifyBroker(input, context);
      case BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID:
        return this.executeOta(input, context);
      case BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID:
        return this.verifyIntegration(input, context);
    }
  }

  /**
   * Returns the deliberately requested secret handoff over private worker IPC.
   * Callers must not journal, log, cache, or include this value in diagnostics.
   */
  public configurationHandoff(installationId: string) {
    const record = this.custody.read(installationId);
    const identity = this.custody.identity(installationId);
    if (record.stateAgentToken === undefined) {
      throw new BootstrapCapabilityError(
        'configuration_handoff_unavailable',
        'The protected installation configuration is not complete.',
      );
    }
    return {
      installationId,
      feederBrokerAccount: {
        clientId: identity.clientId,
        username: identity.username,
        password: decodeUtf8MqttPassword(
          Buffer.from(identity.passwordBase64, 'base64'),
        ),
        publishTopic: `dl/PLAF203/${identity.clientId}/device/+/post`,
        subscribeTopic: `dl/PLAF203/${identity.clientId}/device/+/sub`,
      },
      generatedAddonOptions: {
        petlibro_state_agent_url: 'http://{ip}:8765',
        petlibro_state_agent_token: record.stateAgentToken,
        state_agent_updates: {
          enabled: true,
          manifest_url:
            'https://raw.githubusercontent.com/tannerln7/Petlibro-Home-Assistant/state-agent-releases/state-agent/latest.json',
          check_on_connect: true,
          check_interval_hours: 24,
        },
      },
    };
  }

  private prepare(
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ) {
    const installationId = string(input, 'installationId');
    const feederIp = string(input, 'feederIp');
    const homeAssistantIp = string(input, 'homeAssistantIp');
    const sshPublicKey = string(input, 'sshPublicKey');
    this.custody.initialize({ installationId, feederIp, homeAssistantIp });
    requireDirectory(this.artifactDirectory, 'packaged bootstrap artifacts');
    requireDirectory(this.assetDirectory, 'bootstrap payload assets');
    const prepared = prepareBootstrapPayload({
      installationId,
      artifactDirectory: this.artifactDirectory,
      assetDirectory: this.assetDirectory,
      custodyDirectory: this.payloadDirectory,
      homeAssistantIp,
      sshPublicKey,
    });
    const preparedAt = new Date().toISOString();
    this.custody.storePreparedPayload(
      installationId,
      {
        path: prepared.path,
        sha256: prepared.sha256,
        md5: prepared.md5,
        bytes: prepared.bytes,
        preparedAt,
      },
      prepared.stateAgentToken,
    );
    context.report('bootstrap.artifact-prepared', 'info', {
      sha256: prepared.sha256,
      bytes: prepared.bytes,
      deviceBound: true,
    });
    return {
      artifactReference: `bootstrap-payload:${installationId}`,
      sha256: prepared.sha256,
      bytes: prepared.bytes,
      deviceBound: true,
      recoveryPublicKeyIncluded: true,
      stateAgentTokenCustodied: true,
    };
  }

  private async capture(
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ) {
    this.requireDeviceChangesEnabled();
    const installationId = string(input, 'installationId');
    const record = this.custody.read(installationId);
    if (record.payload === undefined) {
      throw new BootstrapCapabilityError(
        'payload_not_prepared',
        'A verified device-bound payload must exist before identity capture.',
      );
    }
    const identity = await captureFeederIdentity({
      listenHost: string(input, 'setupHostIp'),
      listenPort: number(input, 'listenPort'),
      feederIp: record.feederIp,
      timeoutMs: boundedNumber(input, 'timeoutSeconds', 5, 600) * 1_000,
      reconnectGraceMs: 10_000,
      sendProvisioning: boolean(input, 'sendProvisioning'),
      report: (type, payload) => context.report(type, 'info', payload),
    });
    if (identity.softwareVersion !== '3.1.48') {
      throw new BootstrapCapabilityError(
        'unsupported_firmware',
        `Observed firmware ${identity.softwareVersion ?? 'unknown'}; only 3.1.48 is qualified for automatic bootstrap.`,
      );
    }
    decodeUtf8MqttPassword(identity.password);
    this.custody.storeIdentity(
      installationId,
      {
        clientId: identity.clientId,
        username: identity.username,
        passwordBase64: Buffer.from(identity.password).toString('base64'),
        peerIp: identity.peerIp,
        protocolLevel: identity.protocolLevel,
        softwareVersion: identity.softwareVersion,
      },
      new Date().toISOString(),
    );
    return {
      clientId: identity.clientId,
      peerIp: identity.peerIp,
      protocolLevel: identity.protocolLevel,
      softwareVersion: identity.softwareVersion,
      otaSubscriptionObserved: identity.otaSubscriptionObserved,
      connectionGeneration: identity.connectionGeneration,
      credentialsCustodied: true,
    };
  }

  private async verifyBroker(
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ) {
    const installationId = string(input, 'installationId');
    const identity = this.custody.identity(installationId);
    const host = string(input, 'brokerHost');
    const port = number(input, 'brokerPort');
    const timeoutMs = boundedNumber(input, 'timeoutSeconds', 1, 30) * 1_000;
    await probeMqttCredentials({
      host,
      port,
      clientId: `petlibro-management-${installationId.slice(0, 8)}`,
      username: string(input, 'backendUsername'),
      password: string(input, 'backendPassword'),
      timeoutMs,
    });
    context.report('bootstrap.backend-credentials-verified', 'info', {
      host,
      port,
    });
    await probeMqttCredentials({
      host,
      port,
      clientId: identity.clientId,
      username: identity.username,
      password: Buffer.from(identity.passwordBase64, 'base64'),
      protocolLevel: identity.protocolLevel,
      timeoutMs,
    });
    context.report('bootstrap.feeder-credentials-verified', 'info', {
      host,
      port,
      clientId: identity.clientId,
    });
    const record = this.custody.read(installationId);
    this.custody.storeBrokerVerification(installationId, {
      brokerHost: host,
      brokerPort: port,
      backendUsername: string(input, 'backendUsername'),
      credentialBinding: brokerCredentialBinding(
        record,
        host,
        port,
        string(input, 'backendUsername'),
        string(input, 'backendPassword'),
      ),
      verifiedAt: new Date().toISOString(),
    });
    return {
      backendCredentialsAccepted: true,
      feederCredentialsAccepted: true,
      brokerHost: host,
      brokerPort: port,
      verifiedAt: new Date().toISOString(),
    };
  }

  private async executeOta(
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ) {
    this.requireDeviceChangesEnabled();
    const installationId = string(input, 'installationId');
    const record = this.custody.read(installationId);
    const identity = this.custody.identity(installationId);
    if (record.payload === undefined) {
      throw new BootstrapCapabilityError(
        'payload_not_prepared',
        'The protected device-bound payload is unavailable.',
      );
    }
    if (identity.softwareVersion !== '3.1.48') {
      throw new BootstrapCapabilityError(
        'unsupported_firmware',
        `The protected identity is bound to firmware ${identity.softwareVersion}; only 3.1.48 is qualified for automatic bootstrap.`,
      );
    }
    const verification = record.brokerVerification;
    if (verification === undefined) {
      throw new BootstrapCapabilityError(
        'final_broker_not_verified',
        'Final-broker credentials must be verified for this installation before OTA.',
      );
    }
    const verifiedAt = Date.parse(verification.verifiedAt);
    if (
      !Number.isFinite(verifiedAt) ||
      verifiedAt > Date.now() + 60_000 ||
      Date.now() - verifiedAt > 30 * 60_000
    ) {
      throw new BootstrapCapabilityError(
        'final_broker_verification_expired',
        'Final-broker credential proof is older than 30 minutes and must be repeated before OTA.',
      );
    }
    const expectedBinding = brokerCredentialBinding(
      record,
      string(input, 'brokerHost'),
      number(input, 'brokerPort'),
      string(input, 'backendUsername'),
      string(input, 'backendPassword'),
    );
    if (
      verification.brokerHost !== string(input, 'brokerHost') ||
      verification.brokerPort !== number(input, 'brokerPort') ||
      verification.backendUsername !== string(input, 'backendUsername') ||
      !safeEqual(verification.credentialBinding, expectedBinding)
    ) {
      throw new BootstrapCapabilityError(
        'final_broker_verification_mismatch',
        'The protected broker verification does not match the current installation credentials and destination.',
      );
    }
    const result = await executeGuardedOta({
      listenHost: string(input, 'setupHostIp'),
      listenPort: number(input, 'listenPort'),
      artifactPort: number(input, 'artifactPort'),
      feederIp: record.feederIp,
      payloadPath: record.payload.path,
      payloadMd5: record.payload.md5,
      identity: {
        clientId: identity.clientId,
        username: identity.username,
        password: Buffer.from(identity.passwordBase64, 'base64'),
        peerIp: identity.peerIp,
        protocolLevel: identity.protocolLevel,
        softwareVersion: identity.softwareVersion,
        otaSubscriptionObserved: false,
        connectionGeneration: 0,
      },
      timeoutMs: boundedNumber(input, 'timeoutSeconds', 60, 900) * 1_000,
      report: (type, payload) => context.report(type, 'info', payload),
    });
    return {
      ...result,
      artifactSha256: record.payload.sha256,
      payloadIncludesDonorPreservationProcedure: true,
      replaySafe: false,
    };
  }

  private requireDeviceChangesEnabled(): void {
    if (!this.deviceChangesEnabled) {
      throw new BootstrapCapabilityError(
        'device_changes_not_qualified',
        'Device-changing bootstrap capabilities are packaged but disabled until controlled Home Assistant and feeder qualification is explicitly enabled.',
      );
    }
  }

  private async verifyIntegration(
    input: Readonly<Record<string, unknown>>,
    context: BootstrapExecutionContext,
  ) {
    this.requireDeviceChangesEnabled();
    const installationId = string(input, 'installationId');
    const record = this.custody.read(installationId);
    const identity = this.custody.identity(installationId);
    if (record.stateAgentToken === undefined) {
      throw new BootstrapCapabilityError(
        'state_agent_token_unavailable',
        'The protected State Agent token is unavailable.',
      );
    }
    const brokerHost = string(input, 'brokerHost');
    const brokerPort = number(input, 'brokerPort');
    await waitForMqttHeartbeat({
      host: brokerHost,
      port: brokerPort,
      username: string(input, 'backendUsername'),
      password: string(input, 'backendPassword'),
      feederClientId: identity.clientId,
      timeoutMs: boundedNumber(input, 'timeoutSeconds', 10, 300) * 1_000,
      onSubscribed: async () => {
        await redirectFeederMqtt(record.feederIp, brokerHost, 1883);
        context.report('bootstrap.final-endpoint-restored', 'info', {
          brokerHost,
          brokerPort: 1883,
        });
      },
    });
    context.report('bootstrap.fresh-heartbeat-observed', 'info', {
      clientId: identity.clientId,
      retained: false,
    });
    const stateAgent = await inspectStateAgentHealth(
      `http://${record.feederIp}:8765`,
      record.stateAgentToken,
      5,
    );
    if (!stateAgent.healthy) {
      throw new BootstrapCapabilityError(
        'state_agent_unhealthy',
        'The State Agent responded but did not report a healthy state snapshot.',
      );
    }
    return {
      finalEndpointRestored: true,
      freshHeartbeatObserved: true,
      stateAgentHealthy: true,
      stateAgentVersion: stateAgent.agent.version,
      controllerReadiness: 'observed-separately',
    };
  }
}

export class BootstrapCapabilityError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
  ) {
    super(message);
    this.name = 'BootstrapCapabilityError';
  }
}

function string(
  input: Readonly<Record<string, unknown>>,
  field: string,
): string {
  const value = input[field];
  if (typeof value !== 'string' || value.length === 0 || value.length > 4096) {
    throw new TypeError(`${field} must be a bounded non-empty string`);
  }
  return value;
}

function number(
  input: Readonly<Record<string, unknown>>,
  field: string,
): number {
  const value = input[field];
  if (
    !Number.isSafeInteger(value) ||
    (value as number) < 1 ||
    (value as number) > 65535
  ) {
    throw new TypeError(`${field} must be a valid port`);
  }
  return value as number;
}

function boundedNumber(
  input: Readonly<Record<string, unknown>>,
  field: string,
  minimum: number,
  maximum: number,
): number {
  const value = input[field];
  if (typeof value !== 'number' || value < minimum || value > maximum) {
    throw new TypeError(`${field} must be between ${minimum} and ${maximum}`);
  }
  return value;
}

function boolean(
  input: Readonly<Record<string, unknown>>,
  field: string,
): boolean {
  const value = input[field];
  if (typeof value !== 'boolean')
    throw new TypeError(`${field} must be boolean`);
  return value;
}

function requireDirectory(path: string, name: string): void {
  try {
    if (statSync(path).isDirectory()) return;
  } catch {
    // Converted to one stable capability error below.
  }
  throw new BootstrapCapabilityError(
    'bootstrap_artifacts_unavailable',
    `The ${name} are unavailable in this runtime.`,
  );
}

export function bootstrapFailure(error: unknown) {
  if (
    error instanceof BootstrapCapabilityError ||
    error instanceof BootstrapNetworkError
  ) {
    return {
      code: error.code,
      message: error.message,
      outcome:
        error instanceof BootstrapNetworkError &&
        error.outcome === 'indeterminate'
          ? ('indeterminate' as const)
          : ('failed' as const),
    };
  }
  if (error instanceof TypeError) {
    return {
      code: 'invalid_capability_input',
      message: error.message,
      outcome: 'failed' as const,
    };
  }
  return {
    code: 'bootstrap_internal_error',
    message: 'The bootstrap capability failed unexpectedly.',
    outcome: 'failed' as const,
  };
}

function brokerCredentialBinding(
  record: ReturnType<BootstrapCustody['read']>,
  brokerHost: string,
  brokerPort: number,
  backendUsername: string,
  backendPassword: string,
): string {
  if (record.stateAgentToken === undefined || record.identity === undefined) {
    throw new BootstrapCapabilityError(
      'broker_binding_unavailable',
      'Protected payload and feeder identity custody must exist before broker verification.',
    );
  }
  return createHmac('sha256', record.stateAgentToken)
    .update(
      JSON.stringify({
        installationId: record.installationId,
        brokerHost,
        brokerPort,
        backendUsername,
        backendPassword,
        feederClientId: record.identity.clientId,
        feederUsername: record.identity.username,
        feederPasswordBase64: record.identity.passwordBase64,
      }),
    )
    .digest('hex');
}

function safeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, 'hex');
  const rightBytes = Buffer.from(right, 'hex');
  return (
    leftBytes.length === rightBytes.length &&
    timingSafeEqual(leftBytes, rightBytes)
  );
}

function decodeUtf8MqttPassword(value: Uint8Array): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(value);
  } catch {
    throw new BootstrapCapabilityError(
      'unsupported_mqtt_password_encoding',
      'The captured feeder MQTT password is not valid UTF-8 and cannot be represented losslessly in Home Assistant add-on options.',
    );
  }
}
