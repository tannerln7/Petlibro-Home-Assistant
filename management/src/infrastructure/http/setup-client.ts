import type { ProtectedConfigurationHandoff } from '#/domain/capabilities';
import {
  INSTALLATION_ACTION_ID,
  type InstallationOperationSnapshot,
  parseOperationSnapshot,
} from '#/domain/operations';
import {
  parseSetupPreflightResult,
  type SetupPreflightRequest,
} from '#/domain/setup';

export async function runSetupPreflight(input: SetupPreflightRequest) {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/setup/preflight`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(input),
    },
  );
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    const message =
      typeof value === 'object' &&
      value !== null &&
      'message' in value &&
      typeof value.message === 'string'
        ? value.message
        : 'Setup preflight failed.';
    throw new Error(message);
  }
  return parseSetupPreflightResult(value);
}

export async function fetchInstallationAvailability(signal?: AbortSignal) {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/installations`,
    signal === undefined ? undefined : { signal },
  );
  const value = (await response.json()) as unknown;
  if (
    !response.ok ||
    typeof value !== 'object' ||
    value === null ||
    !('available' in value) ||
    typeof value.available !== 'boolean'
  ) {
    throw new Error('Installation availability could not be determined.');
  }
  const installation =
    'installation' in value && value.installation !== undefined
      ? parseOperationSnapshot(value.installation)
      : undefined;
  if (
    installation !== undefined &&
    installation.actionId !== INSTALLATION_ACTION_ID
  ) {
    throw new Error(
      'Installation availability returned another operation type.',
    );
  }
  return {
    available: value.available,
    reason:
      'reason' in value && typeof value.reason === 'string'
        ? value.reason
        : undefined,
    ...(installation === undefined ? {} : { installation }),
  };
}

export async function startInstallation(
  input: SetupPreflightRequest,
): Promise<InstallationOperationSnapshot> {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/installations`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(input),
    },
  );
  return installationFromResponse(response);
}

export async function satisfyInstallationCheckpoint(input: {
  operationId: string;
  checkpointId: string;
}): Promise<InstallationOperationSnapshot> {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/installations/${encodeURIComponent(input.operationId)}/checkpoints/${encodeURIComponent(input.checkpointId)}`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: '{}',
    },
  );
  return installationFromResponse(response);
}

export async function downloadProtectedConfiguration(
  operationId: string,
): Promise<void> {
  const response = await fetch(
    `${import.meta.env.BASE_URL}api/v1/installations/${encodeURIComponent(operationId)}/configuration-handoff`,
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: '{}',
    },
  );
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    throw new Error(
      errorMessage(value, 'Protected configuration could not be retrieved.'),
    );
  }
  const handoff = value as ProtectedConfigurationHandoff;
  if (
    typeof handoff !== 'object' ||
    handoff === null ||
    handoff.installationId !== operationId ||
    typeof handoff.feederBrokerAccount?.password !== 'string' ||
    typeof handoff.addonOptionsPatch !== 'object'
  ) {
    throw new Error('The server returned an invalid protected handoff.');
  }
  const url = URL.createObjectURL(
    new Blob([`${JSON.stringify(handoff, null, 2)}\n`], {
      type: 'application/json',
    }),
  );
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `petlibro-installation-${operationId}.private.json`;
  anchor.click();
  URL.revokeObjectURL(url);
}

async function installationFromResponse(
  response: Response,
): Promise<InstallationOperationSnapshot> {
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    throw new Error(errorMessage(value, 'The installation request failed.'));
  }
  const operation = parseOperationSnapshot(value);
  if (operation.actionId !== INSTALLATION_ACTION_ID) {
    throw new Error('The server returned a different operation contract.');
  }
  return operation;
}

function errorMessage(value: unknown, fallback: string): string {
  return typeof value === 'object' &&
    value !== null &&
    'message' in value &&
    typeof value.message === 'string'
    ? value.message
    : fallback;
}
