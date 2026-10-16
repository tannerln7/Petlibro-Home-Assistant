import {
  type OperationDetail,
  parseOperationEvent,
  parseOperationSnapshot,
  STATE_AGENT_INSPECTION_ACTION_ID,
  type StateAgentInspectionAction,
} from '#/domain/operations';

export async function fetchStateAgentInspectionAction(
  signal?: AbortSignal,
): Promise<StateAgentInspectionAction> {
  const response = await fetch(
    apiUrl('api/v1/actions/state-agent-inspection'),
    requestWithSignal(signal),
  );
  return parseAction(await responseValue(response));
}

export async function startStateAgentInspection(): Promise<
  ReturnType<typeof parseOperationSnapshot>
> {
  const response = await fetch(
    apiUrl('api/v1/actions/state-agent-inspection'),
    {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
      },
      body: '{}',
    },
  );
  return parseOperationSnapshot(await responseValue(response));
}

export async function fetchOperations(signal?: AbortSignal) {
  const response = await fetch(
    apiUrl('api/v1/operations?limit=20'),
    requestWithSignal(signal),
  );
  const value = objectValue(
    await responseValue(response),
    'operations response',
  );
  if (!Array.isArray(value.operations)) {
    throw new Error('The operations response does not contain a list.');
  }
  return value.operations.map(parseOperationSnapshot);
}

export async function fetchOperationDetail(
  operationId: string,
  signal?: AbortSignal,
): Promise<OperationDetail> {
  const response = await fetch(
    apiUrl(`api/v1/operations/${encodeURIComponent(operationId)}?after=0`),
    requestWithSignal(signal),
  );
  const value = objectValue(
    await responseValue(response),
    'operation response',
  );
  if (!Array.isArray(value.events)) {
    throw new Error('The operation response does not contain events.');
  }
  return {
    operation: parseOperationSnapshot(value.operation),
    events: value.events.map(parseOperationEvent),
    nextEventCursor: nonNegativeInteger(value.nextEventCursor, 'event cursor'),
  };
}

export function operationEventsWebSocketUrl(
  operationId: string,
  afterSequence: number,
): string {
  const base = new URL(import.meta.env.BASE_URL, window.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  base.pathname = `${base.pathname.replace(/\/$/, '')}/api/v1/operations/${encodeURIComponent(operationId)}/events`;
  base.search = `after=${afterSequence}`;
  return base.toString();
}

async function responseValue(response: Response): Promise<unknown> {
  const value = (await response.json()) as unknown;
  if (!response.ok) {
    const message =
      typeof value === 'object' &&
      value !== null &&
      'message' in value &&
      typeof value.message === 'string'
        ? value.message
        : `The management API returned HTTP ${response.status}.`;
    throw new Error(message);
  }
  return value;
}

function parseAction(value: unknown): StateAgentInspectionAction {
  const record = objectValue(value, 'action');
  if (
    record.id !== STATE_AGENT_INSPECTION_ACTION_ID ||
    record.title !== 'Inspect State Agent health' ||
    record.mode !== 'read-only' ||
    typeof record.available !== 'boolean' ||
    typeof record.description !== 'string'
  ) {
    throw new Error('The action response has an unsupported contract.');
  }
  return {
    id: STATE_AGENT_INSPECTION_ACTION_ID,
    title: 'Inspect State Agent health',
    description: record.description,
    available: record.available,
    mode: 'read-only',
    ...(typeof record.unavailableReason === 'string'
      ? { unavailableReason: record.unavailableReason }
      : {}),
  };
}

function apiUrl(path: string): string {
  return `${import.meta.env.BASE_URL}${path}`;
}

function requestWithSignal(signal: AbortSignal | undefined): RequestInit {
  return signal === undefined ? {} : { signal };
}

function objectValue(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function nonNegativeInteger(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new Error(`${name} must be a non-negative integer.`);
  }
  return value as number;
}
