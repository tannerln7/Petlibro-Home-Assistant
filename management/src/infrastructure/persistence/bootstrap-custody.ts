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
import { dirname, resolve } from 'node:path';

export interface CustodiedIdentity {
  readonly clientId: string;
  readonly username: string;
  readonly passwordBase64: string;
  readonly peerIp: string;
  readonly protocolLevel: 3 | 4;
  readonly softwareVersion: string;
  readonly capturedAt: string;
}

export interface BootstrapCustodyRecord {
  readonly schemaVersion: 1;
  readonly installationId: string;
  readonly feederIp: string;
  readonly homeAssistantIp: string;
  readonly identity?: CustodiedIdentity;
  readonly stateAgentToken?: string;
  readonly payload?: {
    readonly path: string;
    readonly sha256: string;
    readonly md5: string;
    readonly bytes: number;
    readonly preparedAt: string;
  };
  readonly brokerVerification?: {
    readonly brokerHost: string;
    readonly brokerPort: number;
    readonly backendUsername: string;
    /** HMAC over the protected credentials and captured identity. */
    readonly credentialBinding: string;
    readonly verifiedAt: string;
  };
}

/** Private worker-owned custody; records are never returned wholesale over IPC. */
export class BootstrapCustody {
  private readonly directory: string;

  public constructor(dataDirectory: string) {
    this.directory = resolve(dataDirectory, 'bootstrap-custody');
    mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    chmodSync(this.directory, 0o700);
  }

  public initialize(input: {
    installationId: string;
    feederIp: string;
    homeAssistantIp: string;
  }): BootstrapCustodyRecord {
    const existing = this.readOptional(input.installationId);
    if (existing !== undefined) {
      if (
        existing.feederIp !== input.feederIp ||
        existing.homeAssistantIp !== input.homeAssistantIp
      ) {
        throw new Error(
          'The installation ID is already bound to a different feeder or Home Assistant address.',
        );
      }
      return existing;
    }
    const record: BootstrapCustodyRecord = {
      schemaVersion: 1,
      installationId: input.installationId,
      feederIp: input.feederIp,
      homeAssistantIp: input.homeAssistantIp,
    };
    this.write(record);
    return record;
  }

  public storeIdentity(
    installationId: string,
    identity: Omit<CustodiedIdentity, 'capturedAt'>,
    capturedAt: string,
  ): CustodiedIdentity {
    const current = this.read(installationId);
    if (current.identity !== undefined) {
      if (
        current.identity.clientId !== identity.clientId ||
        current.identity.username !== identity.username ||
        current.identity.passwordBase64 !== identity.passwordBase64
      ) {
        throw new Error(
          'The captured feeder identity does not match the identity already bound to this installation.',
        );
      }
      return current.identity;
    }
    const stored = { ...identity, capturedAt };
    this.write({ ...current, identity: stored });
    return stored;
  }

  public storePreparedPayload(
    installationId: string,
    payload: NonNullable<BootstrapCustodyRecord['payload']>,
    stateAgentToken: string,
  ): void {
    if (!/^[0-9a-f]{64}$/.test(stateAgentToken)) {
      throw new TypeError('State Agent token has an invalid shape.');
    }
    const current = this.read(installationId);
    this.write({
      ...current,
      stateAgentToken,
      payload,
    });
  }

  public identity(installationId: string): CustodiedIdentity {
    const identity = this.read(installationId).identity;
    if (identity === undefined) {
      throw new Error('No feeder identity is bound to this installation.');
    }
    return identity;
  }

  public storeBrokerVerification(
    installationId: string,
    verification: NonNullable<BootstrapCustodyRecord['brokerVerification']>,
  ): void {
    const current = this.read(installationId);
    if (
      current.identity === undefined ||
      current.stateAgentToken === undefined
    ) {
      throw new Error(
        'Broker verification cannot be stored before payload and identity custody are complete.',
      );
    }
    this.write({ ...current, brokerVerification: verification });
  }

  public read(installationId: string): BootstrapCustodyRecord {
    const record = this.readOptional(installationId);
    if (record === undefined) {
      throw new Error('The bootstrap custody record does not exist.');
    }
    return record;
  }

  private readOptional(
    installationId: string,
  ): BootstrapCustodyRecord | undefined {
    const path = this.path(installationId);
    try {
      const parsed = JSON.parse(readFileSync(path, 'utf8')) as unknown;
      return parseRecord(parsed, installationId);
    } catch (error) {
      if (
        error instanceof Error &&
        'code' in error &&
        error.code === 'ENOENT'
      ) {
        return undefined;
      }
      throw error;
    }
  }

  private write(record: BootstrapCustodyRecord): void {
    const path = this.path(record.installationId);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    const temporary = `${path}.new`;
    rmSync(temporary, { force: true });
    const descriptor = openSync(temporary, 'wx', 0o600);
    try {
      writeFileSync(descriptor, `${JSON.stringify(record)}\n`, 'utf8');
    } finally {
      // writeFileSync does not own descriptors opened by openSync.
      fsyncSync(descriptor);
      closeSync(descriptor);
    }
    chmodSync(temporary, 0o600);
    renameSync(temporary, path);
  }

  private path(installationId: string): string {
    if (!/^[0-9a-f-]{36}$/.test(installationId)) {
      throw new TypeError('installation ID must be a canonical UUID');
    }
    return resolve(this.directory, `${installationId}.json`);
  }
}

function parseRecord(
  value: unknown,
  installationId: string,
): BootstrapCustodyRecord {
  if (
    typeof value !== 'object' ||
    value === null ||
    Array.isArray(value) ||
    !('schemaVersion' in value) ||
    value.schemaVersion !== 1 ||
    !('installationId' in value) ||
    value.installationId !== installationId ||
    !('feederIp' in value) ||
    typeof value.feederIp !== 'string' ||
    !('homeAssistantIp' in value) ||
    typeof value.homeAssistantIp !== 'string'
  ) {
    throw new Error('The bootstrap custody record is malformed.');
  }
  return value as BootstrapCustodyRecord;
}
