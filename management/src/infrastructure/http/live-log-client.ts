import type { LiveLogSeverity } from '#/domain/live-logs';

export function liveLogWebSocketUrl(level: LiveLogSeverity): string {
  const base = new URL(import.meta.env.BASE_URL, window.location.href);
  base.protocol = base.protocol === 'https:' ? 'wss:' : 'ws:';
  base.pathname = `${base.pathname.replace(/\/$/, '')}/api/v1/logs/live`;
  base.search = `level=${encodeURIComponent(level)}`;
  return base.toString();
}
