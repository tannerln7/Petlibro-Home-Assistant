import type { StateAgentHealthResult } from '#/domain/operations';

const MAX_RESPONSE_BYTES = 64 * 1024;

export class StateAgentInspectionError extends Error {
  public readonly code: string = 'state_agent_error';
}

export class StateAgentUnauthorizedError extends StateAgentInspectionError {
  public override readonly code = 'state_agent_unauthorized';
}

export class StateAgentUnavailableError extends StateAgentInspectionError {
  public override readonly code = 'state_agent_unavailable';
}

export class StateAgentTimeoutError extends StateAgentInspectionError {
  public override readonly code = 'state_agent_timeout';
}

export class StateAgentBadResponseError extends StateAgentInspectionError {
  public override readonly code = 'state_agent_bad_response';
}

export async function inspectStateAgentHealth(
  baseUrl: string,
  token: string,
  timeoutSeconds: number,
): Promise<StateAgentHealthResult> {
  const endpoint = validatedEndpoint(baseUrl);
  if (token.length === 0 || token.length > 4096) {
    throw new TypeError('state-agent token must be a non-empty bounded string');
  }
  if (
    !Number.isFinite(timeoutSeconds) ||
    timeoutSeconds < 0.1 ||
    timeoutSeconds > 30
  ) {
    throw new TypeError(
      'state-agent timeout must be between 0.1 and 30 seconds',
    );
  }

  let response: Response;
  try {
    response = await fetch(`${endpoint}/health`, {
      method: 'GET',
      headers: {
        Accept: 'application/json',
        Authorization: `Bearer ${token}`,
        Connection: 'close',
      },
      redirect: 'manual',
      signal: AbortSignal.timeout(timeoutSeconds * 1_000),
    });
  } catch (error) {
    if (isTimeout(error)) {
      throw new StateAgentTimeoutError('State Agent request timed out.');
    }
    throw new StateAgentUnavailableError('State Agent is unavailable.');
  }

  if (response.status === 401 || response.status === 403) {
    await response.body?.cancel();
    throw new StateAgentUnauthorizedError(
      'State Agent rejected authentication.',
    );
  }
  if (!response.ok) {
    await response.body?.cancel();
    throw new StateAgentUnavailableError(
      `State Agent returned HTTP status ${response.status}.`,
    );
  }

  const body = await boundedBody(response);
  let value: unknown;
  try {
    value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(body));
  } catch {
    throw new StateAgentBadResponseError('State Agent returned invalid JSON.');
  }
  return parseHealth(value);
}

function validatedEndpoint(value: string): string {
  let endpoint: URL;
  try {
    endpoint = new URL(value.replace(/\/+$/, ''));
  } catch {
    throw new TypeError('state-agent URL must be an HTTP(S) URL with a host');
  }
  if (
    (endpoint.protocol !== 'http:' && endpoint.protocol !== 'https:') ||
    endpoint.hostname.length === 0
  ) {
    throw new TypeError('state-agent URL must be an HTTP(S) URL with a host');
  }
  if (
    endpoint.username !== '' ||
    endpoint.password !== '' ||
    endpoint.search !== '' ||
    endpoint.hash !== ''
  ) {
    throw new TypeError(
      'state-agent URL must not contain credentials, query, or fragment',
    );
  }
  return endpoint.toString().replace(/\/$/, '');
}

async function boundedBody(response: Response): Promise<Uint8Array> {
  if (response.body === null) {
    return new Uint8Array();
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) {
        break;
      }
      length += chunk.value.byteLength;
      if (length > MAX_RESPONSE_BYTES) {
        throw new StateAgentBadResponseError(
          'State Agent response exceeds the size limit.',
        );
      }
      chunks.push(chunk.value);
    }
  } finally {
    reader.releaseLock();
  }
  const result = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    result.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return result;
}

function parseHealth(value: unknown): StateAgentHealthResult {
  const document = object(value, 'health response');
  const agent = object(document.agent, 'health response agent');
  const stateDecode = object(
    document.state_decode,
    'health response state_decode',
  );
  const healthy = boolean(document.ok, 'health response ok');
  const stateDecodeOk = boolean(stateDecode.ok, 'state_decode ok');
  if (healthy !== stateDecodeOk) {
    throw new StateAgentBadResponseError(
      'State Agent health and state-decode status disagree.',
    );
  }
  return {
    healthy,
    agent: {
      name: string(agent.name, 'agent name'),
      version: string(agent.version, 'agent version'),
      uptimeMs: integer(agent.uptime_ms, 'agent uptime_ms'),
    },
    stateDecode: {
      ok: stateDecodeOk,
      expectedBytes: integer(
        stateDecode.expected_size,
        'state_decode expected_size',
      ),
      actualBytes: integer(stateDecode.actual_size, 'state_decode actual_size'),
    },
  };
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new StateAgentBadResponseError(`${name} must be an object.`);
  }
  return value as Record<string, unknown>;
}

function string(value: unknown, name: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new StateAgentBadResponseError(`${name} must be a non-empty string.`);
  }
  return value;
}

function boolean(value: unknown, name: string): boolean {
  if (typeof value !== 'boolean') {
    throw new StateAgentBadResponseError(`${name} must be a boolean.`);
  }
  return value;
}

function integer(value: unknown, name: string): number {
  if (!Number.isSafeInteger(value) || (value as number) < 0) {
    throw new StateAgentBadResponseError(
      `${name} must be a non-negative integer.`,
    );
  }
  return value as number;
}

function isTimeout(error: unknown): boolean {
  return (
    error instanceof DOMException &&
    (error.name === 'TimeoutError' || error.name === 'AbortError')
  );
}
