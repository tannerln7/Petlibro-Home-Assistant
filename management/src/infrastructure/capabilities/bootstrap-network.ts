import { randomBytes } from 'node:crypto';
import { createSocket, type Socket as UdpSocket } from 'node:dgram';
import { readFileSync } from 'node:fs';
import {
  createServer as createHttpServer,
  type Server as HttpServer,
} from 'node:http';
import {
  createConnection,
  createServer,
  type Server,
  type Socket,
} from 'node:net';
import {
  buildConnack,
  buildOtaCommand,
  buildPuback,
  buildPublish,
  buildSetMqttServerFrame,
  buildSuback,
  encodeMqttString,
  encodeRemainingLength,
  type MqttConnectIdentity,
  OemProtocolError,
  parseConnect,
  parsePublish,
  parseSubscribe,
} from './oem-protocol';

const MAX_PACKET_BYTES = 256 * 1024;

export interface CapturedFeederIdentity {
  readonly clientId: string;
  readonly username: string;
  readonly password: Uint8Array;
  readonly peerIp: string;
  readonly protocolLevel: 3 | 4;
  readonly softwareVersion?: string;
  readonly otaSubscriptionObserved: boolean;
  readonly connectionGeneration: number;
}

export interface CaptureIdentityOptions {
  readonly listenHost: string;
  readonly listenPort: number;
  readonly feederIp: string;
  readonly timeoutMs: number;
  readonly reconnectGraceMs: number;
  readonly sendProvisioning: boolean;
  readonly report?: (type: string, detail: Record<string, unknown>) => void;
}

/**
 * Owns the temporary TCP/1883 listener for exactly one bounded invocation.
 * It accepts only the configured feeder and never persists or logs credentials.
 */
export async function captureFeederIdentity(
  options: CaptureIdentityOptions,
): Promise<CapturedFeederIdentity> {
  const listener = new TemporaryMqttCapture(options.feederIp, options.report);
  await listener.listen(options.listenHost, options.listenPort);
  let provisioningSent = false;
  try {
    const reconnect = listener.waitForIdentity(options.reconnectGraceMs);
    let identity = await reconnect;
    if (identity === undefined && options.sendProvisioning) {
      await sendProvisioningFrame(
        options.feederIp,
        options.listenHost,
        options.listenPort,
      );
      provisioningSent = true;
      options.report?.('bootstrap.provisioning-sent', {
        destination: options.feederIp,
        endpoint: `${options.listenHost}:${options.listenPort}`,
      });
      identity = await listener.waitForIdentity(options.timeoutMs);
    }
    if (identity === undefined) {
      throw new BootstrapNetworkError(
        'identity_capture_timeout',
        provisioningSent
          ? 'The temporary listener acquired TCP/1883, sent one provisioning command, but did not receive the configured feeder identity.'
          : 'The temporary listener acquired TCP/1883 but did not receive a feeder connection.',
      );
    }
    const ready = await listener.waitForStartupEvidence(options.timeoutMs);
    if (ready === undefined) {
      throw new BootstrapNetworkError(
        'startup_evidence_timeout',
        'The feeder identity connected, but supported firmware startup and OTA subscription evidence were not both observed.',
      );
    }
    return ready;
  } finally {
    await listener.close();
  }
}

export async function probeMqttCredentials(input: {
  readonly host: string;
  readonly port: number;
  readonly clientId: string;
  readonly username: string;
  readonly password: string | Uint8Array;
  readonly protocolLevel?: 3 | 4;
  readonly timeoutMs: number;
}): Promise<void> {
  const packet = buildConnectPacket(input);
  await new Promise<void>((resolve, reject) => {
    const socket = createConnection({ host: input.host, port: input.port });
    const timeout = setTimeout(() => {
      socket.destroy();
      reject(
        new BootstrapNetworkError(
          'broker_probe_timeout',
          'The final broker did not return an MQTT CONNACK before the timeout.',
        ),
      );
    }, input.timeoutMs);
    let buffer = Buffer.alloc(0);
    const finish = (error?: Error) => {
      clearTimeout(timeout);
      socket.destroy();
      error === undefined ? resolve() : reject(error);
    };
    socket.once('connect', () => socket.write(packet));
    socket.on('data', (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      if (buffer.length < 4) return;
      if (buffer[0] !== 0x20 || buffer[1] !== 2 || buffer[2] !== 0) {
        finish(
          new BootstrapNetworkError(
            'broker_protocol_error',
            'The final broker returned an invalid MQTT CONNACK.',
          ),
        );
        return;
      }
      if (buffer[3] !== 0) {
        finish(
          new BootstrapNetworkError(
            'broker_credentials_rejected',
            `The final broker rejected the MQTT credentials (CONNACK ${buffer[3]}).`,
          ),
        );
        return;
      }
      finish();
    });
    socket.once('error', (error) =>
      finish(
        new BootstrapNetworkError(
          'broker_unreachable',
          `The final broker connection failed: ${error.message}`,
        ),
      ),
    );
  });
}

export async function executeGuardedOta(input: {
  readonly listenHost: string;
  readonly listenPort: number;
  readonly artifactPort: number;
  readonly feederIp: string;
  readonly payloadPath: string;
  readonly payloadMd5: string;
  readonly identity: CapturedFeederIdentity;
  readonly timeoutMs: number;
  readonly report?: (type: string, detail: Record<string, unknown>) => void;
}): Promise<{
  readonly messageId: string;
  readonly payloadDownloaded: true;
  readonly publishAcknowledged: true;
  readonly terminalReport: 'accepted';
  readonly postOtaStartupObserved: true;
}> {
  const listener = new TemporaryMqttCapture(
    input.feederIp,
    input.report,
    input.identity,
  );
  const route = `/${randomBytes(18).toString('base64url')}`;
  const artifact = new RestrictedArtifactServer(
    input.listenHost,
    input.artifactPort,
    route,
    input.feederIp,
    input.payloadPath,
  );
  await listener.listen(input.listenHost, input.listenPort);
  try {
    await artifact.listen();
    let identity = await listener.waitForIdentity(10_000);
    if (identity === undefined) {
      await sendProvisioningFrame(
        input.feederIp,
        input.listenHost,
        input.listenPort,
      );
      input.report?.('bootstrap.provisioning-sent', {
        endpoint: `${input.listenHost}:${input.listenPort}`,
      });
      identity = await listener.waitForIdentity(input.timeoutMs);
    }
    if (identity === undefined) {
      throw new BootstrapNetworkError(
        'ota_reconnect_timeout',
        'The feeder did not reconnect to the reacquired temporary listener.',
      );
    }
    const ready = await listener.waitForStartupEvidence(input.timeoutMs);
    if (ready === undefined) {
      throw new BootstrapNetworkError(
        'ota_subscription_timeout',
        'The feeder did not establish supported startup and OTA subscription evidence.',
      );
    }
    if (ready.softwareVersion !== input.identity.softwareVersion) {
      throw new BootstrapNetworkError(
        'firmware_identity_mismatch',
        `The current feeder session reported firmware ${ready.softwareVersion ?? 'unknown'}, but this installation is bound to ${input.identity.softwareVersion ?? 'unknown'}. OTA was not published.`,
      );
    }
    const messageId = `bootstrap-${randomBytes(8).toString('hex')}`;
    listener.publishOta(
      buildOtaCommand({
        messageId,
        artifactUrl: `http://${input.listenHost}:${input.artifactPort}${route}`,
        artifactMd5: input.payloadMd5,
        targetVersion: ready.softwareVersion ?? '3.1.48',
      }),
      messageId,
    );
    try {
      await Promise.all([
        listener.waitForOtaCompletion(input.timeoutMs),
        artifact.waitForDownload(input.timeoutMs),
      ]);
    } catch (error) {
      throw classifyPostPublicationFailure(error);
    }
    return {
      messageId,
      payloadDownloaded: true,
      publishAcknowledged: true,
      terminalReport: 'accepted',
      postOtaStartupObserved: true,
    };
  } finally {
    await Promise.all([listener.close(), artifact.close()]);
  }
}

export async function waitForMqttHeartbeat(input: {
  readonly host: string;
  readonly port: number;
  readonly username: string;
  readonly password: string;
  readonly feederClientId: string;
  readonly timeoutMs: number;
  readonly onSubscribed: () => Promise<void>;
  /** MQTT keepalive interval; defaults to ten seconds. */
  readonly keepAliveSeconds?: number;
}): Promise<void> {
  if (!input.feederClientId || /[/+#\0]/.test(input.feederClientId)) {
    throw new TypeError(
      'feeder client ID cannot form an exact heartbeat topic',
    );
  }
  const topic = `dl/PLAF203/${input.feederClientId}/device/heart/post`;
  const keepAliveSeconds = input.keepAliveSeconds ?? 10;
  if (
    !Number.isSafeInteger(keepAliveSeconds) ||
    keepAliveSeconds < 1 ||
    keepAliveSeconds > 65_535
  ) {
    throw new TypeError('MQTT keepalive must be between 1 and 65535 seconds');
  }
  const deadline = Date.now() + input.timeoutMs;
  await runHeartbeatObserverPhase(
    input,
    topic,
    'subscription',
    deadline,
    keepAliveSeconds,
  );
  await input.onSubscribed();
  // Establish a new clean MQTT session after the redirect callback completes.
  // This prevents heartbeat bytes queued with the first SUBACK (or while the
  // callback was running) from being treated as post-redirect evidence.
  await runHeartbeatObserverPhase(
    input,
    topic,
    'heartbeat',
    deadline,
    keepAliveSeconds,
  );
}

async function runHeartbeatObserverPhase(
  input: {
    readonly host: string;
    readonly port: number;
    readonly username: string;
    readonly password: string;
  },
  topic: string,
  target: 'subscription' | 'heartbeat',
  deadline: number,
  keepAliveSeconds: number,
): Promise<void> {
  const remaining = deadline - Date.now();
  if (remaining <= 0) {
    throw new BootstrapNetworkError(
      'heartbeat_timeout',
      'Timed out waiting for a fresh feeder heartbeat after final-endpoint restoration.',
    );
  }
  const connect = buildConnectPacket({
    clientId: `petlibro-management-${randomBytes(8).toString('hex')}`,
    username: input.username,
    password: input.password,
    keepAliveSeconds,
  });
  await new Promise<void>((resolve, reject) => {
    const socket = createConnection({ host: input.host, port: input.port });
    let buffer = Buffer.alloc(0);
    let stage: 'connack' | 'suback' | 'heartbeat' = 'connack';
    let settled = false;
    let keepAlive: ReturnType<typeof setInterval> | undefined;
    const timeout = setTimeout(
      () =>
        finish(
          new BootstrapNetworkError(
            'heartbeat_timeout',
            'Timed out waiting for a fresh feeder heartbeat.',
          ),
        ),
      remaining,
    );
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      clearTimeout(timeout);
      if (keepAlive !== undefined) clearInterval(keepAlive);
      socket.destroy();
      error === undefined ? resolve() : reject(error);
    };
    socket.once('connect', () => socket.write(connect));
    socket.once('error', (error) =>
      finish(
        new BootstrapNetworkError(
          'heartbeat_broker_unreachable',
          error.message,
        ),
      ),
    );
    socket.once('close', () => {
      if (!settled) {
        finish(
          new BootstrapNetworkError(
            'heartbeat_broker_disconnected',
            'The final broker disconnected the heartbeat observer before fresh evidence arrived.',
          ),
        );
      }
    });
    socket.on('data', (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      try {
        for (;;) {
          const extracted = extractPacket(buffer);
          if (extracted === undefined) break;
          buffer = Buffer.from(extracted.remainder);
          const packet = extracted.packet;
          if (stage === 'connack') {
            if (
              packet.length !== 4 ||
              packet[0] !== 0x20 ||
              packet[1] !== 2 ||
              packet[2] !== 0 ||
              packet[3] !== 0
            ) {
              finish(
                new BootstrapNetworkError(
                  'heartbeat_broker_rejected',
                  'The final broker rejected backend credentials.',
                ),
              );
              return;
            }
            keepAlive = setInterval(
              () => {
                if (!settled && !socket.destroyed) {
                  socket.write(Uint8Array.from([0xc0, 0x00]));
                }
              },
              Math.max(1, Math.floor((keepAliveSeconds * 1_000) / 2)),
            );
            socket.write(buildSubscribePacket(topic));
            stage = 'suback';
          } else if (
            packet.length === 2 &&
            packet[0] === 0xd0 &&
            packet[1] === 0
          ) {
            // PINGRESP only proves that this observer session remains live.
            // It is not feeder readiness evidence.
          } else if (stage === 'suback') {
            if (!Buffer.from(packet).equals(Buffer.from([0x90, 3, 0, 1, 0]))) {
              finish(
                new BootstrapNetworkError(
                  'heartbeat_subscription_rejected',
                  'The final broker rejected the exact feeder heartbeat subscription.',
                ),
              );
              return;
            }
            if (target === 'subscription') {
              finish();
              return;
            }
            stage = 'heartbeat';
          } else if ((packet[0] as number) >> 4 === 3) {
            const publication = parsePublish(packet);
            if (publication.qos === 1 && publication.packetId !== undefined) {
              socket.write(buildPuback(publication.packetId));
            }
            if (publication.topic !== topic || publication.retain) continue;
            const value = JSON.parse(
              new TextDecoder().decode(publication.payload),
            ) as unknown;
            if (
              typeof value === 'object' &&
              value !== null &&
              'cmd' in value &&
              value.cmd === 'HEARTBEAT'
            ) {
              finish();
              return;
            }
          }
        }
      } catch (error) {
        finish(
          error instanceof Error
            ? error
            : new Error('MQTT heartbeat parse failed'),
        );
      }
    });
  });
}

export async function redirectFeederMqtt(
  feederIp: string,
  host: string,
  port: number,
): Promise<void> {
  await sendProvisioningFrame(feederIp, host, port);
}

export class BootstrapNetworkError extends Error {
  public constructor(
    public readonly code: string,
    message: string,
    public readonly outcome: 'failed' | 'indeterminate' = 'failed',
  ) {
    super(message);
    this.name = 'BootstrapNetworkError';
  }
}

export function classifyPostPublicationFailure(
  error: unknown,
): BootstrapNetworkError {
  if (
    error instanceof BootstrapNetworkError &&
    (error.outcome === 'indeterminate' || error.code === 'ota_rejected')
  ) {
    return error;
  }
  if (error instanceof BootstrapNetworkError) {
    return new BootstrapNetworkError(
      error.code,
      `${error.message} The OTA command was already published, so the feeder effect is indeterminate and the command must not be replayed automatically.`,
      'indeterminate',
    );
  }
  return new BootstrapNetworkError(
    'ota_outcome_indeterminate',
    `OTA verification failed after command publication: ${error instanceof Error ? error.message : 'unknown error'}. The command must not be replayed automatically.`,
    'indeterminate',
  );
}

export class TemporaryMqttCapture {
  private readonly server: Server;
  private readonly sockets = new Set<Socket>();
  private identity: CapturedFeederIdentity | undefined;
  private readonly waiters = new Set<() => void>();
  private connectionGeneration = 0;
  private activeSocket: Socket | undefined;
  private readonly subscriptions = new Set<string>();
  private otaMessageId: string | undefined;
  private otaGeneration: number | undefined;
  private otaPublishAcknowledged = false;
  private otaTerminalAccepted = false;
  private otaError: string | undefined;
  private postOtaStartupObserved = false;
  private readonly pendingApplicationResponses: Array<{
    readonly topic: string;
    readonly payload: Uint8Array;
    readonly generation: number;
  }> = [];

  public constructor(
    private readonly feederIp: string,
    private readonly report?: (
      type: string,
      detail: Record<string, unknown>,
    ) => void,
    private readonly expectedIdentity?: CapturedFeederIdentity,
  ) {
    this.server = createServer((socket) => this.accept(socket));
  }

  public async listen(host: string, port: number): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      const onError = (error: NodeJS.ErrnoException) => {
        this.server.off('listening', onListening);
        reject(
          new BootstrapNetworkError(
            error.code === 'EADDRINUSE'
              ? 'temporary_listener_occupied'
              : 'temporary_listener_unavailable',
            error.code === 'EADDRINUSE'
              ? `TCP/${port} is still owned by another process.`
              : `The temporary listener could not bind ${host}:${port}: ${error.message}`,
          ),
        );
      };
      const onListening = () => {
        this.server.off('error', onError);
        resolve();
      };
      this.server.once('error', onError);
      this.server.once('listening', onListening);
      this.server.listen({ host, port, exclusive: true });
    });
    this.report?.('bootstrap.listener-acquired', { host, port });
  }

  public listeningPort(): number {
    const address = this.server.address();
    if (address === null || typeof address === 'string') {
      throw new Error('The temporary listener is not bound to a TCP port.');
    }
    return address.port;
  }

  public async waitForIdentity(
    timeoutMs: number,
  ): Promise<CapturedFeederIdentity | undefined> {
    if (this.identity !== undefined) return this.identity;
    await new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(done);
        resolve();
      }, timeoutMs);
      const done = () => {
        clearTimeout(timeout);
        this.waiters.delete(done);
        resolve();
      };
      this.waiters.add(done);
    });
    return this.identity;
  }

  public async waitForStartupEvidence(
    timeoutMs: number,
  ): Promise<CapturedFeederIdentity | undefined> {
    if (
      this.identity?.softwareVersion !== undefined &&
      this.identity.otaSubscriptionObserved
    ) {
      return this.identity;
    }
    await new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(done);
        resolve();
      }, timeoutMs);
      const done = () => {
        if (
          this.identity?.softwareVersion === undefined ||
          !this.identity.otaSubscriptionObserved
        ) {
          return;
        }
        clearTimeout(timeout);
        this.waiters.delete(done);
        resolve();
      };
      this.waiters.add(done);
    });
    return this.identity?.softwareVersion !== undefined &&
      this.identity.otaSubscriptionObserved
      ? this.identity
      : undefined;
  }

  public async close(): Promise<void> {
    for (const socket of this.sockets) socket.destroy();
    if (!this.server.listening) return;
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }

  public publishOta(payload: Uint8Array, messageId: string): void {
    const socket = this.activeSocket;
    const topics = [...this.subscriptions].filter((topic) =>
      topic.endsWith('/device/ota/sub'),
    );
    if (
      socket === undefined ||
      topics.length !== 1 ||
      this.identity === undefined
    ) {
      throw new BootstrapNetworkError(
        'ota_subscription_unavailable',
        'The feeder does not have exactly one active OTA subscription.',
      );
    }
    this.otaMessageId = messageId;
    this.otaGeneration = this.identity.connectionGeneration;
    this.otaPublishAcknowledged = false;
    this.otaTerminalAccepted = false;
    this.postOtaStartupObserved = false;
    this.otaError = undefined;
    this.report?.('bootstrap.ota-publication-starting', {
      messageId,
      connectionGeneration: this.otaGeneration,
    });
    try {
      socket.write(buildPublish(topics[0] as string, payload, 1, 1));
      this.report?.('bootstrap.ota-published', {
        messageId,
        connectionGeneration: this.otaGeneration,
      });
    } catch (error) {
      throw new BootstrapNetworkError(
        'ota_publish_indeterminate',
        `The OTA command write or durable publication report failed after publication began: ${error instanceof Error ? error.message : 'unknown error'}. The command must not be replayed automatically.`,
        'indeterminate',
      );
    }
  }

  public async waitForOtaCompletion(timeoutMs: number): Promise<void> {
    const complete = () =>
      this.otaPublishAcknowledged &&
      this.otaTerminalAccepted &&
      this.postOtaStartupObserved;
    if (!complete()) {
      await new Promise<void>((resolve) => {
        const timeout = setTimeout(() => {
          this.waiters.delete(done);
          resolve();
        }, timeoutMs);
        const done = () => {
          if (!complete() && this.otaError === undefined) return;
          clearTimeout(timeout);
          this.waiters.delete(done);
          resolve();
        };
        this.waiters.add(done);
      });
    }
    if (this.otaError !== undefined) {
      throw new BootstrapNetworkError('ota_rejected', this.otaError);
    }
    if (!complete()) {
      throw new BootstrapNetworkError(
        'ota_outcome_indeterminate',
        'Timed out before PUBACK, terminal OTA acceptance, and post-OTA startup were all observed. The command must not be replayed automatically.',
        'indeterminate',
      );
    }
  }

  private accept(socket: Socket): void {
    const peerIp = normalizeIp(socket.remoteAddress);
    if (peerIp !== this.feederIp) {
      this.report?.('bootstrap.connection-rejected', { peerIp });
      socket.destroy();
      return;
    }
    this.sockets.add(socket);
    socket.setTimeout(30_000, () => socket.destroy());
    let buffer = Buffer.alloc(0);
    let connectedIdentity: MqttConnectIdentity | undefined;
    socket.on('data', (chunk: Buffer) => {
      buffer = Buffer.concat([buffer, chunk]);
      try {
        for (;;) {
          const extracted = extractPacket(buffer);
          if (extracted === undefined) break;
          buffer = Buffer.from(extracted.remainder);
          const packet = extracted.packet;
          const type = (packet[0] as number) >> 4;
          if (connectedIdentity === undefined) {
            if (type !== 1) throw new OemProtocolError('CONNECT must be first');
            connectedIdentity = parseConnect(packet);
            this.recordIdentity(connectedIdentity, peerIp);
            if (
              this.activeSocket !== undefined &&
              this.activeSocket !== socket
            ) {
              this.activeSocket.destroy();
            }
            this.subscriptions.clear();
            this.activeSocket = socket;
            socket.write(buildConnack());
            continue;
          }
          if (type === 3) {
            const publication = parsePublish(packet);
            if (publication.qos === 1 && publication.packetId !== undefined) {
              socket.write(buildPuback(publication.packetId));
            }
            this.recordPublication(publication.topic, publication.payload);
          } else if (type === 8) {
            const subscription = parseSubscribe(packet);
            for (const { topic } of subscription.topics) {
              this.subscriptions.add(topic);
            }
            socket.write(
              buildSuback(
                subscription.packetId,
                subscription.topics.map(({ qos }) => Math.min(qos, 1)),
              ),
            );
            this.flushApplicationResponses(socket);
            if (
              subscription.topics.some(({ topic }) =>
                topic.endsWith('/device/ota/sub'),
              )
            ) {
              this.identity = this.identity && {
                ...this.identity,
                otaSubscriptionObserved: true,
              };
              this.notifyWaiters();
            }
          } else if (type === 12) {
            socket.write(Uint8Array.from([0xd0, 0]));
          } else if (type === 4 && packet.length === 4) {
            const packetId =
              ((packet[2] as number) << 8) | (packet[3] as number);
            if (
              packetId === 1 &&
              this.otaGeneration === this.identity?.connectionGeneration
            ) {
              this.otaPublishAcknowledged = true;
              this.report?.('bootstrap.ota-puback-observed', {
                connectionGeneration: this.otaGeneration,
              });
              this.notifyWaiters();
            }
          } else if (type === 14) {
            socket.end();
          }
        }
      } catch (error) {
        this.report?.('bootstrap.session-error', {
          message: error instanceof Error ? error.message : 'MQTT parse error',
        });
        socket.destroy();
      }
    });
    const remove = () => {
      this.sockets.delete(socket);
      if (this.activeSocket === socket) {
        this.activeSocket = undefined;
        this.subscriptions.clear();
      }
    };
    socket.once('close', remove);
    socket.once('error', remove);
  }

  private recordIdentity(identity: MqttConnectIdentity, peerIp: string): void {
    if (identity.username === null || identity.password === null) {
      throw new OemProtocolError('feeder CONNECT omitted credentials');
    }
    const candidate = {
      clientId: identity.clientId,
      username: identity.username,
      password: identity.password,
      peerIp,
      protocolLevel: identity.protocolLevel,
    };
    if (
      this.expectedIdentity !== undefined &&
      (candidate.clientId !== this.expectedIdentity.clientId ||
        candidate.username !== this.expectedIdentity.username ||
        !Buffer.from(candidate.password).equals(
          this.expectedIdentity.password,
        ) ||
        candidate.protocolLevel !== this.expectedIdentity.protocolLevel)
    ) {
      throw new OemProtocolError(
        'the feeder identity does not match the identity bound to this installation',
      );
    }
    if (
      this.identity !== undefined &&
      (this.identity.clientId !== candidate.clientId ||
        this.identity.username !== candidate.username ||
        !Buffer.from(this.identity.password).equals(candidate.password))
    ) {
      throw new OemProtocolError(
        'a second MQTT identity attempted to use the bootstrap listener',
      );
    }
    this.connectionGeneration += 1;
    this.pendingApplicationResponses.splice(
      0,
      this.pendingApplicationResponses.length,
    );
    this.identity = {
      ...candidate,
      otaSubscriptionObserved: false,
      connectionGeneration: this.connectionGeneration,
    };
    this.report?.('bootstrap.identity-captured', {
      clientId: candidate.clientId,
      peerIp,
      protocolLevel: candidate.protocolLevel,
      connectionGeneration: this.connectionGeneration,
    });
    this.notifyWaiters();
  }

  private recordPublication(topic: string, payload: Uint8Array): void {
    let value: unknown;
    try {
      value = JSON.parse(new TextDecoder().decode(payload));
    } catch {
      return;
    }
    const response = buildBootstrapApplicationResponse(
      topic,
      value,
      Date.now(),
    );
    if (response !== undefined && this.identity !== undefined) {
      if (
        this.subscriptions.has(response.topic) &&
        this.activeSocket !== undefined
      ) {
        this.activeSocket.write(
          buildPublish(response.topic, response.payload, 1, 0),
        );
      } else {
        this.pendingApplicationResponses.push({
          ...response,
          generation: this.identity.connectionGeneration,
        });
      }
    }
    if (
      topic.endsWith('/device/event/post') &&
      typeof value === 'object' &&
      value !== null &&
      'cmd' in value &&
      value.cmd === 'DEVICE_START_EVENT' &&
      'softwareVersion' in value &&
      typeof value.softwareVersion === 'string' &&
      value.softwareVersion.startsWith('3') &&
      this.identity !== undefined
    ) {
      this.identity = {
        ...this.identity,
        softwareVersion: value.softwareVersion,
      };
      if (
        this.otaGeneration !== undefined &&
        this.identity.connectionGeneration > this.otaGeneration
      ) {
        this.postOtaStartupObserved = true;
        this.report?.('bootstrap.post-ota-startup-observed', {
          connectionGeneration: this.identity.connectionGeneration,
          softwareVersion: value.softwareVersion,
        });
      }
      this.report?.('bootstrap.device-start-observed', {
        softwareVersion: value.softwareVersion,
      });
      this.notifyWaiters();
    }
    if (
      topic.endsWith('/device/ota/post') &&
      typeof value === 'object' &&
      value !== null &&
      'msgId' in value &&
      value.msgId === this.otaMessageId &&
      'cmd' in value
    ) {
      if (value.cmd === 'OTA_INFORM' && 'state' in value && value.state === 1) {
        this.otaTerminalAccepted = true;
        this.report?.('bootstrap.ota-terminal-accepted', {
          messageId: this.otaMessageId,
        });
      } else if (
        (value.cmd === 'OTA_INFORM' && 'state' in value && value.state === 2) ||
        (value.cmd === 'OTA_UPGRADE' &&
          'code' in value &&
          value.code !== 0 &&
          value.code !== null)
      ) {
        this.otaError =
          'errorMsg' in value && typeof value.errorMsg === 'string'
            ? value.errorMsg
            : 'The OEM firmware reported OTA failure.';
      }
      this.notifyWaiters();
    }
  }

  private notifyWaiters(): void {
    for (const waiter of [...this.waiters]) waiter();
  }

  private flushApplicationResponses(socket: Socket): void {
    const generation = this.identity?.connectionGeneration;
    if (generation === undefined) return;
    const ready = this.pendingApplicationResponses.filter(
      (pending) =>
        pending.generation === generation &&
        this.subscriptions.has(pending.topic),
    );
    const retained = this.pendingApplicationResponses.filter(
      (pending) => !ready.includes(pending),
    );
    this.pendingApplicationResponses.splice(
      0,
      this.pendingApplicationResponses.length,
      ...retained,
    );
    for (const pending of ready) {
      socket.write(buildPublish(pending.topic, pending.payload, 1, 0));
    }
  }
}

export function buildBootstrapApplicationResponse(
  topic: string,
  value: unknown,
  nowMs: number,
): { readonly topic: string; readonly payload: Uint8Array } | undefined {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return undefined;
  }
  const record = value as Record<string, unknown>;
  const command = record.cmd;
  if (topic.endsWith('/device/ntp/post') && command === 'NTP') {
    return jsonResponse(topic, {
      cmd: 'NTP',
      ts: nowMs,
      code: 0,
      calibrationTag: false,
      timezoneOffsetSeconds: 0,
      nextDSTOffsetSeconds: 0,
      nextDSTTransitionTs: 0,
      secondNextDSTOffsetSeconds: 0,
      secondNextDSTTransitionTs: 0,
      timezone: 0,
    });
  }
  if (!topic.endsWith('/device/event/post')) return undefined;
  const messageId = record.msgId;
  if (typeof messageId !== 'string' || messageId.length === 0) return undefined;
  if (command === 'ATTR_PUSH_EVENT' || command === 'DEVICE_START_EVENT') {
    return jsonResponse(topic, {
      cmd: command,
      ts: nowMs,
      msgId: messageId,
      code: 0,
    });
  }
  if (
    command === 'GRAIN_OUTPUT_EVENT' &&
    typeof record.execStep === 'string' &&
    record.execStep.length > 0
  ) {
    return jsonResponse(topic, {
      cmd: command,
      ts: nowMs,
      msgId: messageId,
      code: 0,
      execStep: record.execStep,
    });
  }
  // GET_FEEDING_PLAN_EVENT deliberately receives no application response:
  // the bootstrap listener does not own authoritative complete schedule state.
  return undefined;
}

function jsonResponse(
  postTopic: string,
  value: Readonly<Record<string, unknown>>,
) {
  return {
    topic: `${postTopic.slice(0, -4)}sub`,
    payload: new TextEncoder().encode(JSON.stringify(value)),
  };
}

class RestrictedArtifactServer {
  private readonly server: HttpServer;
  private downloaded = false;
  private readonly waiters = new Set<() => void>();

  public constructor(
    private readonly host: string,
    private readonly port: number,
    route: string,
    feederIp: string,
    payloadPath: string,
  ) {
    const payload = readFileSync(payloadPath);
    this.server = createHttpServer((request, response) => {
      const peerIp = normalizeIp(request.socket.remoteAddress);
      if (
        request.method !== 'GET' ||
        request.url !== route ||
        peerIp !== feederIp
      ) {
        response.writeHead(404, { Connection: 'close' }).end();
        return;
      }
      response.writeHead(200, {
        'Content-Type': 'application/octet-stream',
        'Content-Length': payload.length,
        Connection: 'close',
      });
      response.end(payload, () => {
        this.downloaded = true;
        for (const waiter of [...this.waiters]) waiter();
      });
    });
  }

  public async listen(): Promise<void> {
    await new Promise<void>((resolve, reject) => {
      this.server.once('error', reject);
      this.server.listen(this.port, this.host, () => resolve());
    });
  }

  public async waitForDownload(timeoutMs: number): Promise<void> {
    if (this.downloaded) return;
    await new Promise<void>((resolve) => {
      const timeout = setTimeout(() => {
        this.waiters.delete(done);
        resolve();
      }, timeoutMs);
      const done = () => {
        clearTimeout(timeout);
        this.waiters.delete(done);
        resolve();
      };
      this.waiters.add(done);
    });
    if (!this.downloaded) {
      throw new BootstrapNetworkError(
        'artifact_download_timeout',
        'The feeder did not download the authorized payload before the timeout.',
      );
    }
  }

  public async close(): Promise<void> {
    if (!this.server.listening) return;
    await new Promise<void>((resolve) => this.server.close(() => resolve()));
  }
}

async function sendProvisioningFrame(
  feederIp: string,
  host: string,
  port: number,
): Promise<void> {
  const socket: UdpSocket = createSocket('udp4');
  const frame = buildSetMqttServerFrame(host, port, '1');
  try {
    await new Promise<void>((resolve, reject) => {
      socket.send(frame, 17484, feederIp, (error) =>
        error === null ? resolve() : reject(error),
      );
    });
  } finally {
    socket.close();
  }
}

function extractPacket(
  buffer: Buffer,
): { packet: Uint8Array; remainder: Buffer } | undefined {
  if (buffer.length < 2) return undefined;
  let multiplier = 1;
  let remaining = 0;
  let lengthBytes = 0;
  for (let index = 1; index < Math.min(buffer.length, 5); index += 1) {
    const digit = buffer[index] as number;
    remaining += (digit & 0x7f) * multiplier;
    lengthBytes += 1;
    if (remaining > MAX_PACKET_BYTES) {
      throw new OemProtocolError('MQTT packet exceeds bootstrap limit');
    }
    if ((digit & 0x80) === 0) {
      const total = 1 + lengthBytes + remaining;
      if (buffer.length < total) return undefined;
      return {
        packet: buffer.subarray(0, total),
        remainder: buffer.subarray(total),
      };
    }
    multiplier *= 128;
  }
  if (buffer.length >= 5) {
    throw new OemProtocolError('invalid MQTT remaining length');
  }
  return undefined;
}

function buildConnectPacket(input: {
  readonly clientId: string;
  readonly username: string;
  readonly password: string | Uint8Array;
  readonly protocolLevel?: 3 | 4;
  readonly keepAliveSeconds?: number;
}): Uint8Array {
  const level = input.protocolLevel ?? 4;
  const protocol = level === 3 ? 'MQIsdp' : 'MQTT';
  const keepAliveSeconds = input.keepAliveSeconds ?? 10;
  const variable = Uint8Array.from([
    ...encodeMqttString(protocol),
    level,
    0xc2,
    (keepAliveSeconds >> 8) & 0xff,
    keepAliveSeconds & 0xff,
  ]);
  const payload = Uint8Array.from([
    ...encodeMqttString(input.clientId),
    ...encodeMqttString(input.username),
    ...encodeMqttString(input.password),
  ]);
  return Uint8Array.from([
    0x10,
    ...encodeRemainingLength(variable.length + payload.length),
    ...variable,
    ...payload,
  ]);
}

function buildSubscribePacket(topic: string): Uint8Array {
  const body = Uint8Array.from([0, 1, ...encodeMqttString(topic), 0]);
  return Uint8Array.from([
    0x82,
    ...encodeRemainingLength(body.length),
    ...body,
  ]);
}

function normalizeIp(value: string | undefined): string {
  return value?.startsWith('::ffff:') ? value.slice(7) : (value ?? 'unknown');
}
