export type ManagementServiceState = 'ready';

export type IntegrationAvailability =
  | 'not-configured'
  | 'ready'
  | 'unavailable';

export interface ObservationMetadata {
  readonly source:
    | 'management-server'
    | 'management-configuration'
    | 'capability-worker'
    | 'python-worker';
  readonly authority: 'management-application';
  readonly observedAt: string;
  readonly scope: string;
}

export interface ManagementServiceObservation {
  readonly name: 'Petlibro Management';
  readonly version: string;
  readonly state: ManagementServiceState;
  readonly detail: string;
  readonly observation: ObservationMetadata;
}

export interface IntegrationObservation {
  readonly id:
    | 'capability-worker'
    | 'python-worker'
    | 'routine-controller'
    | 'camera-runtime';
  readonly name: string;
  readonly availability: IntegrationAvailability;
  readonly detail: string;
  readonly observation: ObservationMetadata;
}

export interface SystemStatusSnapshot {
  readonly apiVersion: 'v1';
  readonly generatedAt: string;
  readonly management: ManagementServiceObservation;
  readonly integrations: readonly IntegrationObservation[];
}

export function parseSystemStatusSnapshot(
  value: unknown,
): SystemStatusSnapshot {
  if (!isRecord(value) || value.apiVersion !== 'v1') {
    throw new Error('The management API returned an unsupported response.');
  }

  if (
    typeof value.generatedAt !== 'string' ||
    !isManagementObservation(value.management) ||
    !Array.isArray(value.integrations) ||
    !value.integrations.every(isIntegrationObservation)
  ) {
    throw new Error('The management API returned an invalid status snapshot.');
  }

  return value as unknown as SystemStatusSnapshot;
}

function isManagementObservation(value: unknown): boolean {
  return (
    isRecord(value) &&
    value.name === 'Petlibro Management' &&
    typeof value.version === 'string' &&
    value.state === 'ready' &&
    typeof value.detail === 'string' &&
    isObservationMetadata(value.observation)
  );
}

function isIntegrationObservation(value: unknown): boolean {
  return (
    isRecord(value) &&
    (value.id === 'capability-worker' ||
      value.id === 'python-worker' ||
      value.id === 'routine-controller' ||
      value.id === 'camera-runtime') &&
    typeof value.name === 'string' &&
    (value.availability === 'not-configured' ||
      value.availability === 'ready' ||
      value.availability === 'unavailable') &&
    typeof value.detail === 'string' &&
    isObservationMetadata(value.observation)
  );
}

function isObservationMetadata(value: unknown): boolean {
  return (
    isRecord(value) &&
    (value.source === 'management-server' ||
      value.source === 'management-configuration' ||
      value.source === 'capability-worker' ||
      value.source === 'python-worker') &&
    value.authority === 'management-application' &&
    typeof value.observedAt === 'string' &&
    typeof value.scope === 'string'
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
