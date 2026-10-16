// @vitest-environment node

import { describe, expect, it } from 'vitest';
import {
  normalizeSupervisorLine,
  StreamingLogSanitizer,
  sanitizeLogMessage,
} from './supervisor-log-adapter';

describe('Supervisor log normalization', () => {
  it('preserves source timestamps and normalizes severity', () => {
    expect(
      normalizeSupervisorLine(
        '2026-10-15T02:00:00Z WARNING connection delayed',
        new Date('2026-10-15T02:00:01Z'),
      ),
    ).toEqual({
      source: 'addon-runtime',
      severity: 'warning',
      sourceTimestamp: '2026-10-15T02:00:00.000Z',
      receivedAt: '2026-10-15T02:00:01.000Z',
      message: '2026-10-15T02:00:00Z WARNING connection delayed',
    });
  });

  it('redacts credential-shaped values before browser delivery', () => {
    const sanitized = sanitizeLogMessage(
      'authorization=Bearer abc.def token=secret mqtt_password=hunter2 url=?token=again',
    );
    expect(sanitized).not.toContain('abc.def');
    expect(sanitized).not.toContain('hunter2');
    expect(sanitized).not.toContain('again');
    expect(sanitized).toContain('[REDACTED');
  });

  it('redacts nested structured credentials and URL user information', () => {
    const structured = sanitizeLogMessage(
      JSON.stringify({
        event: 'configuration',
        nested: { petlibro_state_agent_token: 'a'.repeat(64) },
        endpoint: 'mqtt://backend:broker-secret@192.0.2.10:1883',
      }),
    );
    expect(structured).not.toContain('a'.repeat(64));
    expect(structured).not.toContain('broker-secret');
    expect(structured).toContain('[REDACTED');
  });

  it('redacts a private key body split across framed log records', () => {
    const sanitizer = new StreamingLogSanitizer();
    const records = [
      'ERROR -----BEGIN OPENSSH PRIVATE KEY-----',
      'b3BlbnNzaC1rZXktdjEAAAAA synthetic-secret-body',
      '-----END OPENSSH PRIVATE KEY-----',
      'INFO ordinary output resumes',
    ].map((line) => sanitizer.sanitize(line));

    expect(records.join('\n')).not.toContain('synthetic-secret-body');
    expect(records.slice(0, 3)).toEqual([
      '[REDACTED SENSITIVE RECORD]',
      '[REDACTED PRIVATE KEY MATERIAL]',
      '[REDACTED PRIVATE KEY MATERIAL]',
    ]);
    expect(records[3]).toBe('INFO ordinary output resumes');
  });
});
