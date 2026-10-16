// @vitest-environment node

import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { prepareBootstrapPayload } from './bootstrap-payload';

const directories: string[] = [];

afterEach(() => {
  for (const directory of directories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

describe('native device-bound payload assembly', () => {
  it('validates packaged provenance and emits the reference archive layout', () => {
    const root = mkdtempSync(join(tmpdir(), 'petlibro-payload-'));
    directories.push(root);
    const artifacts = join(root, 'artifacts');
    const agent = join(artifacts, 'state-agent');
    const dropbear = join(artifacts, 'dropbear');
    write(join(agent, 'plaf203-state-agent'), armElf(false));
    write(join(agent, 'plaf203-update-fs'), armElf(false));
    write(join(agent, 'app_start_snippet.sh'), '#!/bin/sh\n');
    write(
      join(agent, 'runit', 'plaf203-state-agent', 'run'),
      '#!/bin/sh\nexec agent --allow-ip 192.0.2.10\n',
    );
    const dropbearBinary = armElf(true);
    write(join(dropbear, 'dropbearmulti'), dropbearBinary);
    write(join(dropbear, 'LICENSE.dropbear'), 'license\n');
    write(
      join(dropbear, 'BUILD_INFO.dropbear.json'),
      JSON.stringify({
        commit: '28216cd9af822732a1549b78621c2ea9a76a0fe5',
        target: 'arm-linux-musleabihf',
        password_auth: false,
        dropbearmulti_sha256: createHash('sha256')
          .update(dropbearBinary)
          .digest('hex'),
      }),
    );
    const assets = resolve(process.cwd(), 'worker/assets');
    const prepared = prepareBootstrapPayload({
      installationId: '11111111-1111-4111-8111-111111111111',
      artifactDirectory: artifacts,
      assetDirectory: assets,
      custodyDirectory: join(root, 'custody'),
      homeAssistantIp: '192.0.2.42',
      sshPublicKey: ed25519Key(),
    });
    expect(prepared.stateAgentToken).toMatch(/^[0-9a-f]{64}$/);
    expect(prepared.sha256).toMatch(/^[0-9a-f]{64}$/);
    const payload = readFileSync(prepared.path);
    const installer = readFileSync(join(assets, 'bootstrap-installer.sh'));
    expect(payload.subarray(0, installer.length)).toEqual(installer);
    const archive = join(root, 'payload.tar.gz');
    writeFileSync(archive, payload.subarray(installer.length));
    const extracted = join(root, 'extracted');
    mkdirSync(extracted);
    execFileSync('tar', ['-xzf', archive, '-C', extracted]);
    expect(
      readFileSync(
        join(
          extracted,
          'local-state-agent',
          'runit',
          'plaf203-state-agent',
          'run',
        ),
        'utf8',
      ),
    ).toContain('--allow-ip 192.0.2.42');
    expect(
      readFileSync(
        join(extracted, 'local-state-agent', 'token'),
        'utf8',
      ).trim(),
    ).toBe(prepared.stateAgentToken);
    expect(readFileSync(join(extracted, 'authorized_keys'), 'utf8')).toContain(
      'ssh-ed25519',
    );
  });
});

function write(path: string, value: string | Buffer): void {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, value);
}

function armElf(hardFloat: boolean): Buffer {
  const value = Buffer.alloc(64);
  Buffer.from([0x7f, 0x45, 0x4c, 0x46]).copy(value);
  value[4] = 1;
  value.writeUInt16LE(40, 18);
  value.writeUInt32LE(hardFloat ? 0x400 : 0, 36);
  return value;
}

function ed25519Key(): string {
  const algorithm = field(Buffer.from('ssh-ed25519'));
  const key = field(Buffer.alloc(32, 0x42));
  return `ssh-ed25519 ${Buffer.concat([algorithm, key]).toString('base64')} test`;
}

function field(value: Buffer): Buffer {
  const length = Buffer.alloc(4);
  length.writeUInt32BE(value.length);
  return Buffer.concat([length, value]);
}
