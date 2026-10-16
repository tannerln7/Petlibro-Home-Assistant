import {
  chmodSync,
  closeSync,
  fsyncSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { resolve } from 'node:path';
import type { SetupPreflightRequest } from '#/domain/setup';

export class InstallationSecretStore {
  private readonly directory: string;

  public constructor(dataDirectory: string) {
    this.directory = resolve(dataDirectory, 'installation-secrets');
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    chmodSync(this.directory, 0o700);
  }

  public create(operationId: string, input: SetupPreflightRequest): void {
    const path = this.path(operationId);
    const temporary = `${path}.new`;
    rmSync(temporary, { force: true });
    const descriptor = openSync(temporary, 'wx', 0o600);
    try {
      writeFileSync(
        descriptor,
        `${JSON.stringify({ schemaVersion: 1, operationId, input })}\n`,
        'utf8',
      );
      fsyncSync(descriptor);
    } finally {
      closeSync(descriptor);
    }
    chmodSync(temporary, 0o600);
    renameSync(temporary, path);
  }

  public read(operationId: string): SetupPreflightRequest {
    const value = JSON.parse(readFileSync(this.path(operationId), 'utf8')) as {
      schemaVersion?: unknown;
      operationId?: unknown;
      input?: unknown;
    };
    if (
      value.schemaVersion !== 1 ||
      value.operationId !== operationId ||
      typeof value.input !== 'object' ||
      value.input === null
    ) {
      throw new Error('The protected installation input is malformed.');
    }
    return value.input as SetupPreflightRequest;
  }

  private path(operationId: string): string {
    if (!/^[0-9a-f-]{36}$/.test(operationId)) {
      throw new TypeError('operation ID must be a canonical UUID');
    }
    return resolve(this.directory, `${operationId}.json`);
  }
}
