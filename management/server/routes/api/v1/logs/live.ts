import { randomUUID } from 'node:crypto';
import { defineWebSocketHandler } from 'nitro';
import type {
  LiveLogRecord,
  LiveLogSeverity,
  LiveLogSourceStatus,
} from '#/domain/live-logs';
import { getApplicationServices } from '#/server/application-services';

const MAX_QUEUE = 1_000;
const MAX_BATCH = 100;
const MAX_SOCKET_BUFFER = 512 * 1024;

interface LiveLogPeerContext {
  readonly id: string;
  readonly level: LiveLogSeverity;
  readonly records: LiveLogRecord[];
  readonly statuses: LiveLogSourceStatus[];
  dropped: number;
  timer?: ReturnType<typeof setInterval>;
  unsubscribe?: () => void;
}

export default defineWebSocketHandler({
  upgrade(request) {
    const url = new URL(request.url);
    const level = url.searchParams.get('level') ?? 'debug';
    if (!isSeverity(level)) {
      throw new Response('Invalid log verbosity', { status: 400 });
    }
    return {
      context: {
        id: randomUUID(),
        level,
        records: [],
        statuses: [],
        dropped: 0,
      } satisfies LiveLogPeerContext,
    };
  },
  open(peer) {
    const context = peer.context as unknown as LiveLogPeerContext;
    const service = getApplicationServices().liveLogs;
    context.unsubscribe = service.subscribe(
      context.id,
      context.level,
      (record) => {
        context.records.push(record);
        if (context.records.length > MAX_QUEUE) {
          const excess = context.records.length - MAX_QUEUE;
          context.records.splice(0, excess);
          context.dropped += excess;
        }
      },
      (status) => {
        context.statuses.splice(0, context.statuses.length, status);
      },
    );
    context.timer = setInterval(() => {
      if (peer.bufferedAmount > MAX_SOCKET_BUFFER) {
        if (context.records.length > MAX_QUEUE / 2) {
          const excess = context.records.length - MAX_QUEUE / 2;
          context.records.splice(0, excess);
          context.dropped += excess;
        }
        return;
      }
      if (context.statuses.length > 0) {
        peer.send({ type: 'logs.status', sources: context.statuses.splice(0) });
      }
      if (context.records.length > 0 || context.dropped > 0) {
        peer.send({
          type: 'logs.records',
          records: context.records.splice(0, MAX_BATCH),
          dropped: context.dropped,
        });
        context.dropped = 0;
      }
    }, 100);
  },
  close(peer) {
    dispose(peer.context as unknown as LiveLogPeerContext);
  },
  error(peer) {
    dispose(peer.context as unknown as LiveLogPeerContext);
  },
});

function dispose(context: LiveLogPeerContext): void {
  if (context.timer !== undefined) clearInterval(context.timer);
  context.unsubscribe?.();
}

function isSeverity(value: string): value is LiveLogSeverity {
  return ['trace', 'debug', 'info', 'warning', 'error'].includes(value);
}
