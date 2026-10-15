import { isIP } from 'node:net';
import type {
  SetupPreflightCheck,
  SetupPreflightRequest,
  SetupPreflightResult,
} from '#/domain/setup';
import { validateSshPublicKey } from '#/infrastructure/capabilities/ssh-public-key';
import {
  type BrokerProbe,
  mqttConnectProbe,
} from '#/infrastructure/observations/mqtt-broker-adapter';

export type PortProbeResult = 'available' | 'occupied' | 'address-unavailable';
export type PortProbe = (
  host: string,
  port: number,
) => Promise<PortProbeResult>;

export class SetupPreflightService {
  public constructor(
    private readonly brokerProbe: BrokerProbe = mqttConnectProbe,
    private readonly portProbe: PortProbe = probePort,
    private readonly clock: () => Date = () => new Date(),
  ) {}

  public async run(
    input: SetupPreflightRequest,
  ): Promise<SetupPreflightResult> {
    const checks: SetupPreflightCheck[] = [];
    const validAddresses = this.validateConfiguration(input, checks);
    if (validAddresses) {
      const [broker, port] = await Promise.all([
        this.brokerProbe({
          host: input.brokerHost,
          port: input.brokerPort,
          username: input.backendUsername,
          password: input.backendPassword,
          timeoutMs: 2_000,
        }),
        this.portProbe(input.setupHostIp, 1883),
      ]);
      checks.push(brokerCheck(broker.kind));
      checks.push(portCheck(port, input.setupHostIp === input.brokerHost));
    }
    return {
      generatedAt: this.clock().toISOString(),
      readyForBootstrapSession: checks.every(
        (check) => check.status === 'passed' || check.status === 'warning',
      ),
      checks,
      scope:
        'Read-only configuration, final-broker authentication, recovery-key, and current TCP/1883 ownership checks. No provisioning, credential capture, payload serving, OTA, or broker reconfiguration was attempted.',
    };
  }

  private validateConfiguration(
    input: SetupPreflightRequest,
    checks: SetupPreflightCheck[],
  ): boolean {
    const addressFields = [
      ['Feeder address', input.feederIp],
      ['Setup host address', input.setupHostIp],
      ['Home Assistant address', input.homeAssistantIp],
      ['Final broker address', input.brokerHost],
    ] as const;
    const invalid = addressFields.filter(([, value]) => !usableIpv4(value));
    checks.push({
      id: 'network-addresses',
      title: 'Network addresses',
      status: invalid.length === 0 ? 'passed' : 'failed',
      detail:
        invalid.length === 0
          ? 'All installation endpoints are concrete usable IPv4 addresses.'
          : `${invalid.map(([name]) => name).join(', ')} must be a usable IPv4 address.`,
    });

    const credentialsValid =
      input.backendUsername.length > 0 &&
      input.backendUsername.length <= 1024 &&
      input.backendPassword.length > 0 &&
      input.backendPassword.length <= 4096;
    checks.push({
      id: 'backend-credentials',
      title: 'Backend broker credentials',
      status: credentialsValid ? 'passed' : 'failed',
      detail: credentialsValid
        ? 'Bounded backend credentials were supplied for an authentication probe.'
        : 'A bounded non-empty backend username and password are required.',
    });

    let keyValid = false;
    try {
      validateSshPublicKey(input.sshPublicKey);
      keyValid = true;
    } catch (error) {
      checks.push({
        id: 'recovery-key',
        title: 'SSH recovery key',
        status: 'failed',
        detail: error instanceof Error ? error.message : 'The key is invalid.',
      });
    }
    if (keyValid) {
      checks.push({
        id: 'recovery-key',
        title: 'SSH recovery key',
        status: 'passed',
        detail: 'The public key has a supported OpenSSH type and valid shape.',
      });
    }

    const portValid = input.brokerPort === 1883;
    checks.push({
      id: 'broker-port',
      title: 'Feeder-facing broker port',
      status: portValid ? 'passed' : 'failed',
      detail: portValid
        ? 'TCP/1883 matches the supported AF203_FW behavior.'
        : 'The feeder-facing final broker must use TCP/1883.',
    });
    return invalid.length === 0 && credentialsValid && keyValid && portValid;
  }
}

function usableIpv4(value: string): boolean {
  if (isIP(value) !== 4 || value === '0.0.0.0') return false;
  const first = Number(value.split('.')[0]);
  return first < 224;
}

function brokerCheck(kind: string): SetupPreflightCheck {
  if (kind === 'authenticated') {
    return {
      id: 'final-broker-authentication',
      title: 'Final broker authentication',
      status: 'passed',
      detail: 'The final broker accepted the configured backend credentials.',
    };
  }
  return {
    id: 'final-broker-authentication',
    title: 'Final broker authentication',
    status: 'failed',
    detail:
      kind === 'rejected'
        ? 'The final broker rejected the backend credentials.'
        : 'The final broker could not be authenticated through a valid MQTT CONNACK.',
  };
}

function portCheck(
  result: PortProbeResult,
  sameHostAsBroker: boolean,
): SetupPreflightCheck {
  if (result === 'available') {
    return {
      id: 'temporary-mqtt-port',
      title: 'Temporary MQTT listener',
      status: 'passed',
      detail: 'The setup host can currently bind TCP/1883.',
    };
  }
  if (result === 'occupied') {
    return {
      id: 'temporary-mqtt-port',
      title: 'Temporary MQTT listener',
      status: 'blocked',
      detail: sameHostAsBroker
        ? 'TCP/1883 is owned by the final broker. A supported, explicit operator-controlled handoff is required before temporary identity capture.'
        : 'TCP/1883 is already owned by another process on the setup host.',
    };
  }
  return {
    id: 'temporary-mqtt-port',
    title: 'Temporary MQTT listener',
    status: 'failed',
    detail: 'The setup host address is not bindable in this add-on runtime.',
  };
}

async function probePort(host: string, port: number): Promise<PortProbeResult> {
  const { createServer } = await import('node:net');
  return new Promise((resolve) => {
    const server = createServer();
    server.once('error', (error: NodeJS.ErrnoException) => {
      resolve(error.code === 'EADDRINUSE' ? 'occupied' : 'address-unavailable');
    });
    server.listen({ host, port, exclusive: true }, () => {
      server.close(() => resolve('available'));
    });
  });
}
