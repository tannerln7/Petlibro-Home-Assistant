// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { requireJsonMutation } from './request-security';

describe('management mutation request boundary', () => {
  it('accepts JSON with parameters and rejects simple form-compatible content', () => {
    expect(
      requireJsonMutation(
        new Request('http://management.invalid/api', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json; charset=utf-8' },
          body: '{}',
        }),
      ),
    ).toBeUndefined();
    expect(
      requireJsonMutation(
        new Request('http://management.invalid/api', {
          method: 'POST',
          headers: { 'Content-Type': 'text/plain' },
          body: '{}',
        }),
      )?.status,
    ).toBe(415);
    expect(
      requireJsonMutation(
        new Request('http://management.invalid/api', { method: 'POST' }),
      )?.status,
    ).toBe(415);
  });
});
