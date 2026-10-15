// @vitest-environment node

import { describe, expect, it } from 'vitest';
import { validateSshPublicKey } from './ssh-public-key';

describe('native recovery public-key validation', () => {
  it('accepts a structurally valid Ed25519 OpenSSH line', () => {
    const key = ed25519Key();
    expect(validateSshPublicKey(`  ${key}  `)).toBe(key);
  });

  it('rejects unsupported, mismatched, truncated, and multiline keys', () => {
    expect(() => validateSshPublicKey('ssh-dss AAAA')).toThrow('supported');
    expect(() =>
      validateSshPublicKey(`ssh-rsa ${ed25519Blob().toString('base64')}`),
    ).toThrow('does not match');
    expect(() => validateSshPublicKey('ssh-ed25519 AAAA')).toThrow('truncated');
    expect(() => validateSshPublicKey(`${ed25519Key()}\nsecond`)).toThrow(
      'exactly one line',
    );
  });
});

function ed25519Key(): string {
  return `ssh-ed25519 ${ed25519Blob().toString('base64')} test-key`;
}

function ed25519Blob(): Buffer {
  return Buffer.concat([
    field(Buffer.from('ssh-ed25519', 'ascii')),
    field(Buffer.alloc(32, 0x42)),
  ]);
}

function field(value: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(value.length);
  return Buffer.concat([length, value]);
}
