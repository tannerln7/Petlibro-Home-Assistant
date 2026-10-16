import { request as httpRequest } from 'node:http';
import type {
  CapabilityProvider,
  StateAgentInspectionCapabilityRequest,
} from '#/application/capability-provider';
import {
  WorkerInvocationNotFoundError,
  WorkerProtocolError,
  WorkerRejectedError,
  WorkerUnavailableError,
} from '#/application/capability-provider';
import type {
  GenericCapabilityRequest,
  ProtectedConfigurationHandoff,
} from '#/domain/capabilities';
import { STATE_AGENT_INSPECTION_CAPABILITY_ID } from '#/domain/operations';
import {
  parseWorkerHealth,
  parseWorkerInvocationSnapshot,
  type WorkerHealth,
  type WorkerInvocationSnapshot,
} from './worker-contract';

const MAX_RESPONSE_BYTES = 1024 * 1024;

interface WorkerResponse {
  readonly status: number;
  readonly value: unknown;
}

export class CapabilityWorkerClient implements CapabilityProvider {
  public constructor(
    private readonly socketPath: string,
    private readonly timeoutMilliseconds = 2_000,
  ) {}

  public async health(): Promise<WorkerHealth> {
    const response = await this.request('GET', '/v1/health');
    this.requireSuccess(response);
    try {
      return parseWorkerHealth(response.value);
    } catch (error) {
      throw new WorkerProtocolError(errorMessage(error));
    }
  }

  public async submitStateAgentInspection(
    request: StateAgentInspectionCapabilityRequest,
  ): Promise<WorkerInvocationSnapshot> {
    const response = await this.request(
      'PUT',
      `/v1/invocations/${encodeURIComponent(request.invocationId)}`,
      {
        protocolVersion: 1,
        invocationId: request.invocationId,
        operationId: request.operationId,
        capabilityId: STATE_AGENT_INSPECTION_CAPABILITY_ID,
        input: {
          baseUrl: request.baseUrl,
          token: request.token,
          timeoutSeconds: request.timeoutSeconds,
        },
      },
    );
    this.requireSuccess(response);
    return this.parseInvocation(response.value);
  }

  public async submitInvocation(
    request: GenericCapabilityRequest,
  ): Promise<WorkerInvocationSnapshot> {
    const response = await this.request(
      'PUT',
      `/v1/invocations/${encodeURIComponent(request.invocationId)}`,
      {
        protocolVersion: 1,
        invocationId: request.invocationId,
        operationId: request.operationId,
        capabilityId: request.capabilityId,
        input: request.input,
      },
    );
    this.requireSuccess(response);
    return this.parseInvocation(response.value);
  }

  public async getInvocation(
    invocationId: string,
  ): Promise<WorkerInvocationSnapshot> {
    const response = await this.request(
      'GET',
      `/v1/invocations/${encodeURIComponent(invocationId)}`,
    );
    if (response.status === 404) {
      throw new WorkerInvocationNotFoundError();
    }
    this.requireSuccess(response);
    return this.parseInvocation(response.value);
  }

  public async getConfigurationHandoff(installationId: string): Promise<
    Omit<ProtectedConfigurationHandoff, 'addonOptionsPatch'> & {
      generatedAddonOptions: Readonly<Record<string, unknown>>;
    }
  > {
    const response = await this.request(
      'GET',
      `/v1/installations/${encodeURIComponent(installationId)}/configuration-handoff`,
    );
    this.requireSuccess(response);
    const value = response.value;
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      throw new WorkerProtocolError(
        'Worker returned an invalid protected handoff.',
      );
    }
    return value as Omit<ProtectedConfigurationHandoff, 'addonOptionsPatch'> & {
      generatedAddonOptions: Readonly<Record<string, unknown>>;
    };
  }

  private async request(
    method: 'GET' | 'PUT',
    path: string,
    document?: unknown,
  ): Promise<WorkerResponse> {
    const body = document === undefined ? undefined : JSON.stringify(document);
    return new Promise((resolve, reject) => {
      const request = httpRequest(
        {
          socketPath: this.socketPath,
          path,
          method,
          signal: AbortSignal.timeout(this.timeoutMilliseconds),
          headers:
            body === undefined
              ? { Accept: 'application/json', Connection: 'close' }
              : {
                  Accept: 'application/json',
                  Connection: 'close',
                  'Content-Type': 'application/json',
                  'Content-Length': Buffer.byteLength(body),
                },
        },
        (response) => {
          const chunks: Buffer[] = [];
          let length = 0;
          response.on('data', (chunk: Buffer) => {
            length += chunk.length;
            if (length > MAX_RESPONSE_BYTES) {
              response.destroy(
                new WorkerProtocolError(
                  'Worker response exceeds the size limit.',
                ),
              );
              return;
            }
            chunks.push(chunk);
          });
          response.on('end', () => {
            try {
              const value = JSON.parse(Buffer.concat(chunks).toString('utf8'));
              resolve({ status: response.statusCode ?? 500, value });
            } catch {
              reject(new WorkerProtocolError('Worker returned invalid JSON.'));
            }
          });
          response.on('error', reject);
        },
      );
      request.on('error', (error) => {
        reject(
          error instanceof WorkerProtocolError
            ? error
            : new WorkerUnavailableError(),
        );
      });
      if (body !== undefined) {
        request.write(body);
      }
      request.end();
    });
  }

  private requireSuccess(response: WorkerResponse): void {
    if (response.status >= 200 && response.status < 300) {
      return;
    }
    const message = workerErrorMessage(response.value);
    if (response.status === 409 || response.status === 400) {
      throw new WorkerRejectedError(message);
    }
    throw new WorkerProtocolError(
      `Worker returned HTTP status ${response.status}: ${message}`,
    );
  }

  private parseInvocation(value: unknown): WorkerInvocationSnapshot {
    try {
      return parseWorkerInvocationSnapshot(value);
    } catch (error) {
      throw new WorkerProtocolError(errorMessage(error));
    }
  }
}

function workerErrorMessage(value: unknown): string {
  if (
    typeof value === 'object' &&
    value !== null &&
    'message' in value &&
    typeof value.message === 'string'
  ) {
    return value.message;
  }
  return 'The worker rejected the request.';
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : 'Worker protocol error.';
}
