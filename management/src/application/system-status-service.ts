import type {
  IntegrationObservation,
  SystemStatusSnapshot,
} from '#/domain/system-status';
import type { WorkerHealth } from '#/infrastructure/worker/worker-contract';

export type Clock = () => Date;

export interface WorkerHealthReader {
  health(): Promise<WorkerHealth>;
}

export class SystemStatusService {
  public constructor(
    private readonly version: string,
    private readonly worker?: WorkerHealthReader,
    private readonly clock: Clock = () => new Date(),
  ) {}

  public async getSnapshot(): Promise<SystemStatusSnapshot> {
    const observedAt = this.clock().toISOString();

    return {
      apiVersion: 'v1',
      generatedAt: observedAt,
      management: {
        name: 'Petlibro Management',
        version: this.version,
        state: 'ready',
        detail: 'The browser application and TypeScript API are available.',
        observation: {
          source: 'management-server',
          authority: 'management-application',
          observedAt,
          scope:
            'This process only; it does not imply feeder or runtime health.',
        },
      },
      integrations: await this.integrationBoundaries(observedAt),
    };
  }

  private async integrationBoundaries(
    observedAt: string,
  ): Promise<readonly IntegrationObservation[]> {
    return [
      await this.workerObservation(observedAt),
      {
        id: 'routine-controller',
        name: 'Routine controller',
        availability: 'not-configured',
        detail:
          'AppDaemon remains independent; a read-only adapter is not implemented yet.',
        observation: this.configurationObservation(observedAt),
      },
      {
        id: 'camera-runtime',
        name: 'Camera runtime',
        availability: 'not-configured',
        detail:
          'go2rtc remains independent; camera status is not projected yet.',
        observation: this.configurationObservation(observedAt),
      },
    ];
  }

  private async workerObservation(
    observedAt: string,
  ): Promise<IntegrationObservation> {
    if (this.worker === undefined) {
      return {
        id: 'capability-worker',
        name: 'Device operations worker',
        availability: 'not-configured',
        detail: 'The worker adapter is not configured.',
        observation: this.configurationObservation(observedAt),
      };
    }
    try {
      const health = await this.worker.health();
      return {
        id: 'capability-worker',
        name: 'Device operations worker',
        availability: 'ready',
        detail: `Worker ${health.workerVersion} accepted protocol version ${health.protocolVersion}.`,
        observation: {
          source: 'capability-worker',
          authority: 'management-application',
          observedAt,
          scope:
            'Worker process readiness only; it does not imply State Agent or feeder health.',
        },
      };
    } catch {
      return {
        id: 'capability-worker',
        name: 'Device operations worker',
        availability: 'unavailable',
        detail: 'The configured private worker socket is unavailable.',
        observation: {
          source: 'management-server',
          authority: 'management-application',
          observedAt,
          scope:
            'Worker transport availability only; no capability or device outcome was observed.',
        },
      };
    }
  }

  private configurationObservation(observedAt: string) {
    return {
      source: 'management-configuration' as const,
      authority: 'management-application' as const,
      observedAt,
      scope:
        'Adapter availability only; no external service state was observed.',
    };
  }
}
