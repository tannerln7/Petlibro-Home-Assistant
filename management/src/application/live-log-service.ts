import {
  type LiveLogRecord,
  type LiveLogSeverity,
  type LiveLogSourceStatus,
  severityPermits,
} from '#/domain/live-logs';
import type { RawLogSourceAdapter } from '#/infrastructure/logging/supervisor-log-adapter';

interface Subscriber {
  readonly level: LiveLogSeverity;
  readonly emit: (record: LiveLogRecord) => void;
  readonly status: (status: LiveLogSourceStatus) => void;
}

export class LiveLogService {
  private readonly subscribers = new Map<string, Subscriber>();
  private controller: AbortController | undefined;
  private sequence = 0;

  public constructor(private readonly adapter: RawLogSourceAdapter) {}

  public subscribe(
    id: string,
    level: LiveLogSeverity,
    emit: Subscriber['emit'],
    status: Subscriber['status'],
  ): () => void {
    this.subscribers.set(id, { level, emit, status });
    if (this.controller === undefined) this.start();
    return () => {
      this.subscribers.delete(id);
      if (this.subscribers.size === 0) {
        this.controller?.abort();
        this.controller = undefined;
      }
    };
  }

  public activeSubscriberCount(): number {
    return this.subscribers.size;
  }

  private start(): void {
    const controller = new AbortController();
    this.controller = controller;
    this.broadcastStatus(
      'streaming',
      'Supervisor add-on logs are streaming on demand.',
    );
    void this.adapter
      .stream((draft) => {
        const record = { ...draft, sequence: ++this.sequence };
        for (const subscriber of this.subscribers.values()) {
          if (severityPermits(record.severity, subscriber.level)) {
            subscriber.emit(record);
          }
        }
      }, controller.signal)
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        this.broadcastStatus(
          'error',
          error instanceof Error ? error.message : 'The log stream failed.',
        );
      })
      .finally(() => {
        if (this.controller === controller) {
          this.controller = undefined;
          if (!controller.signal.aborted && this.subscribers.size > 0) {
            this.broadcastStatus(
              'unavailable',
              'The Supervisor log stream ended. Stop and restart capture to reconnect.',
            );
          }
        }
      });
  }

  private broadcastStatus(
    availability: LiveLogSourceStatus['availability'],
    detail: string,
  ): void {
    for (const subscriber of this.subscribers.values()) {
      subscriber.status({ source: 'addon-runtime', availability, detail });
    }
  }
}
