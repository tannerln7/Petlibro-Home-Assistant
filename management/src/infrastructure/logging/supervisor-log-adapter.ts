import type { LiveLogRecord, LiveLogSeverity } from '#/domain/live-logs';

export interface RawLogSourceAdapter {
  stream(
    emit: (record: Omit<LiveLogRecord, 'sequence'>) => void,
    signal: AbortSignal,
  ): Promise<void>;
}

const MAX_LOG_LINE_CHARACTERS = 64 * 1024;

/** Reads the add-on's existing Supervisor log stream without changing producers. */
export class SupervisorLogAdapter implements RawLogSourceAdapter {
  public constructor(
    private readonly supervisorToken: string | undefined,
    private readonly supervisorUrl = 'http://supervisor',
    private readonly clock: () => Date = () => new Date(),
  ) {}

  public async stream(
    emit: (record: Omit<LiveLogRecord, 'sequence'>) => void,
    signal: AbortSignal,
  ): Promise<void> {
    if (!this.supervisorToken) {
      throw new LogSourceUnavailableError(
        'Supervisor log streaming is unavailable because SUPERVISOR_TOKEN was not supplied.',
      );
    }
    const response = await fetch(
      `${this.supervisorUrl.replace(/\/$/, '')}/addons/self/logs/follow`,
      {
        headers: {
          Authorization: `Bearer ${this.supervisorToken}`,
          Accept: 'text/plain',
        },
        signal,
      },
    );
    if (!response.ok || response.body === null) {
      throw new LogSourceUnavailableError(
        `Supervisor rejected add-on log streaming with HTTP ${response.status}.`,
      );
    }
    const reader = response.body
      .pipeThrough(new TextDecoderStream())
      .getReader();
    const sanitizer = new StreamingLogSanitizer();
    let pending = '';
    let discardingOversizedLine = false;
    try {
      for (;;) {
        const chunk = await reader.read();
        if (chunk.done) break;
        let remainder = chunk.value;
        for (;;) {
          const newline = remainder.indexOf('\n');
          if (newline < 0) {
            if (!discardingOversizedLine) {
              pending += remainder;
              if (pending.length > MAX_LOG_LINE_CHARACTERS) {
                pending = '';
                discardingOversizedLine = true;
                emit(oversizedLineRecord(this.clock()));
              }
            }
            break;
          }
          const segment = remainder.slice(0, newline).replace(/\r$/, '');
          remainder = remainder.slice(newline + 1);
          if (discardingOversizedLine) {
            discardingOversizedLine = false;
            pending = '';
            continue;
          }
          const line = pending + segment;
          pending = '';
          if (line.length > MAX_LOG_LINE_CHARACTERS) {
            emit(oversizedLineRecord(this.clock()));
            continue;
          }
          const record = normalizeSupervisorLine(line, this.clock(), sanitizer);
          if (record !== undefined) emit(record);
        }
      }
      if (!discardingOversizedLine && pending !== '') {
        const record = normalizeSupervisorLine(
          pending,
          this.clock(),
          sanitizer,
        );
        if (record !== undefined) emit(record);
      }
    } finally {
      reader.releaseLock();
    }
  }
}

export class LogSourceUnavailableError extends Error {}

export function normalizeSupervisorLine(
  line: string,
  received: Date,
  sanitizer: StreamingLogSanitizer = new StreamingLogSanitizer(),
): Omit<LiveLogRecord, 'sequence'> | undefined {
  const stripped = stripDockerPrefix(line).trimEnd();
  if (stripped === '') return undefined;
  const timestamp = timestampFromLine(stripped);
  return {
    source: 'addon-runtime',
    severity: severityFromLine(stripped),
    receivedAt: received.toISOString(),
    message: sanitizer.sanitize(stripped),
    ...(timestamp === undefined ? {} : { sourceTimestamp: timestamp }),
  };
}

/** Maintains only the bounded state needed to redact known multiline secrets. */
export class StreamingLogSanitizer {
  private privateKeyBlock: string | undefined;

  public sanitize(value: string): string {
    if (this.privateKeyBlock !== undefined) {
      if (value.toUpperCase().includes(this.privateKeyBlock)) {
        this.privateKeyBlock = undefined;
      }
      return '[REDACTED PRIVATE KEY MATERIAL]';
    }
    const begin = value.match(/-----BEGIN ([A-Z0-9 ]*PRIVATE KEY)-----/i);
    if (begin?.[1] !== undefined) {
      const end = `-----END ${begin[1].toUpperCase()}-----`;
      if (!value.toUpperCase().includes(end)) this.privateKeyBlock = end;
      return sanitizeLogMessage(value);
    }
    return sanitizeLogMessage(value);
  }
}

export function sanitizeLogMessage(value: string): string {
  const structured = sanitizeStructuredRecord(value);
  const sanitized = structured
    .replace(
      /-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*/gi,
      '[REDACTED PRIVATE KEY]',
    )
    .replace(/\b([a-z][a-z0-9+.-]*:\/\/)[^\s/:]+:[^\s/@]+@/gi, '$1[REDACTED]@')
    .replace(
      /\b(authorization|proxy-authorization|bearer|password|passwd|token|secret|credential|mqtt_password|petlibro_state_agent_token)\b\s*[:=]\s*(?:bearer\s+)?(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      '$1=[REDACTED]',
    )
    .replace(
      /([?&](?:token|password|passwd|secret|credential|key)=)[^&\s]+/gi,
      '$1[REDACTED]',
    )
    .replace(/\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=/-]+/gi, '$1 [REDACTED]');
  // Unknown producer formats are not allowed through merely because a generic
  // substitution failed to understand a secret-bearing record.
  return /\b(password|passwd|token|secret|authorization|credential|private[_ -]?key)\b/i.test(
    sanitized.replaceAll('[REDACTED]', ''),
  )
    ? '[REDACTED SENSITIVE RECORD]'
    : sanitized;
}

function sanitizeStructuredRecord(value: string): string {
  const trimmed = value.trim();
  if (!trimmed.startsWith('{') || !trimmed.endsWith('}')) return value;
  try {
    return JSON.stringify(redactStructured(JSON.parse(trimmed) as unknown));
  } catch {
    return value;
  }
}

function redactStructured(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(redactStructured);
  if (typeof value !== 'object' || value === null) return value;
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>).map(([key, item]) => [
      key,
      /password|passwd|token|secret|authorization|credential|private.?key/i.test(
        key,
      )
        ? '[REDACTED]'
        : redactStructured(item),
    ]),
  );
}

function oversizedLineRecord(received: Date): Omit<LiveLogRecord, 'sequence'> {
  return {
    source: 'addon-runtime',
    severity: 'warning',
    receivedAt: received.toISOString(),
    message:
      'A source log line exceeded the 64 KiB transport limit and was dropped before browser delivery.',
  };
}

function severityFromLine(value: string): LiveLogSeverity {
  if (/\b(trace)\b/i.test(value)) return 'trace';
  if (/\b(debug)\b/i.test(value)) return 'debug';
  if (/\b(error|fatal|critical)\b/i.test(value)) return 'error';
  if (/\b(warn|warning)\b/i.test(value)) return 'warning';
  return 'info';
}

function timestampFromLine(value: string): string | undefined {
  const match = value.match(
    /(?:^|\s)(\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?)(?:\s|$)/,
  );
  if (match?.[1] === undefined) return undefined;
  const parsed = new Date(match[1].replace(' ', 'T'));
  return Number.isNaN(parsed.valueOf()) ? undefined : parsed.toISOString();
}

function stripDockerPrefix(value: string): string {
  // Supervisor can expose Docker's 8-byte multiplex header for non-TTY logs.
  if (
    value.length >= 8 &&
    [0, 1, 2].includes(value.charCodeAt(0)) &&
    value.charCodeAt(1) === 0 &&
    value.charCodeAt(2) === 0 &&
    value.charCodeAt(3) === 0
  ) {
    return value.slice(8);
  }
  return value;
}
