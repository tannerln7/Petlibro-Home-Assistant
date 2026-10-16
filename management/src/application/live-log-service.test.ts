// @vitest-environment node

import { describe, expect, it } from 'vitest';
import type { LiveLogRecord } from '#/domain/live-logs';
import type { RawLogSourceAdapter } from '#/infrastructure/logging/supervisor-log-adapter';
import { LiveLogService } from './live-log-service';

describe('LiveLogService', () => {
  it('shares one on-demand collector, filters transport severity, and releases it', async () => {
    const adapter = new ControllableAdapter();
    const service = new LiveLogService(adapter);
    const debug: LiveLogRecord[] = [];
    const trace: LiveLogRecord[] = [];
    const unsubscribeDebug = service.subscribe(
      'debug-viewer',
      'debug',
      (record) => debug.push(record),
      () => undefined,
    );
    const unsubscribeTrace = service.subscribe(
      'trace-viewer',
      'trace',
      (record) => trace.push(record),
      () => undefined,
    );

    expect(adapter.starts).toBe(1);
    adapter.emit('trace', 'trace line');
    adapter.emit('debug', 'debug line');
    expect(debug.map((record) => record.message)).toEqual(['debug line']);
    expect(trace.map((record) => record.message)).toEqual([
      'trace line',
      'debug line',
    ]);

    unsubscribeDebug();
    expect(adapter.signal?.aborted).toBe(false);
    unsubscribeTrace();
    expect(adapter.signal?.aborted).toBe(true);
    expect(service.activeSubscriberCount()).toBe(0);
  });
});

class ControllableAdapter implements RawLogSourceAdapter {
  public starts = 0;
  public signal: AbortSignal | undefined;
  private output:
    | ((record: Omit<LiveLogRecord, 'sequence'>) => void)
    | undefined;

  public stream(
    emit: (record: Omit<LiveLogRecord, 'sequence'>) => void,
    signal: AbortSignal,
  ): Promise<void> {
    this.starts += 1;
    this.signal = signal;
    this.output = emit;
    return new Promise((resolve) =>
      signal.addEventListener('abort', () => resolve(), { once: true }),
    );
  }

  public emit(severity: LiveLogRecord['severity'], message: string): void {
    this.output?.({
      source: 'addon-runtime',
      severity,
      receivedAt: '2026-10-15T00:00:00.000Z',
      message,
    });
  }
}
