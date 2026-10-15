export interface SetupPreflightRequest {
  readonly feederIp: string;
  readonly setupHostIp: string;
  readonly homeAssistantIp: string;
  readonly brokerHost: string;
  readonly brokerPort: number;
  readonly backendUsername: string;
  readonly backendPassword: string;
  readonly sshPublicKey: string;
}

export type SetupCheckStatus = 'passed' | 'warning' | 'blocked' | 'failed';

export interface SetupPreflightCheck {
  readonly id: string;
  readonly title: string;
  readonly status: SetupCheckStatus;
  readonly detail: string;
}

export interface SetupPreflightResult {
  readonly generatedAt: string;
  readonly readyForBootstrapSession: boolean;
  readonly checks: readonly SetupPreflightCheck[];
  readonly scope: string;
}

export function parseSetupPreflightResult(
  value: unknown,
): SetupPreflightResult {
  const record = object(value, 'preflight result');
  if (
    typeof record.generatedAt !== 'string' ||
    typeof record.readyForBootstrapSession !== 'boolean' ||
    !Array.isArray(record.checks) ||
    typeof record.scope !== 'string'
  ) {
    throw new Error('The setup preflight result is malformed.');
  }
  return {
    generatedAt: record.generatedAt,
    readyForBootstrapSession: record.readyForBootstrapSession,
    checks: record.checks.map(parseCheck),
    scope: record.scope,
  };
}

function parseCheck(value: unknown): SetupPreflightCheck {
  const record = object(value, 'preflight check');
  const status = record.status;
  if (
    typeof record.id !== 'string' ||
    typeof record.title !== 'string' ||
    (status !== 'passed' &&
      status !== 'warning' &&
      status !== 'blocked' &&
      status !== 'failed') ||
    typeof record.detail !== 'string'
  ) {
    throw new Error('A setup preflight check is malformed.');
  }
  return {
    id: record.id,
    title: record.title,
    status,
    detail: record.detail,
  };
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}
