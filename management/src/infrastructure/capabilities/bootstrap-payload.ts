import { createHash, randomBytes } from 'node:crypto';
import {
  chmodSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { basename, join, relative, resolve } from 'node:path';
import { gzipSync } from 'node:zlib';
import { validateSshPublicKey } from './ssh-public-key';

const TAR_BLOCK = 512;

export interface PreparedBootstrapPayload {
  readonly path: string;
  readonly sha256: string;
  readonly md5: string;
  readonly bytes: number;
  readonly stateAgentToken: string;
}

/** Builds the device-bound payload entirely in Node from packaged binaries. */
export function prepareBootstrapPayload(input: {
  readonly installationId: string;
  readonly artifactDirectory: string;
  readonly assetDirectory: string;
  readonly custodyDirectory: string;
  readonly homeAssistantIp: string;
  readonly sshPublicKey: string;
}): PreparedBootstrapPayload {
  const key = validateSshPublicKey(input.sshPublicKey);
  const artifactDirectory = resolve(input.artifactDirectory);
  const assetDirectory = resolve(input.assetDirectory);
  const outputDirectory = resolve(input.custodyDirectory, input.installationId);
  mkdirSync(outputDirectory, { recursive: true, mode: 0o700 });
  chmodSync(outputDirectory, 0o700);

  const agentRoot = resolve(artifactDirectory, 'state-agent');
  const dropbearRoot = resolve(artifactDirectory, 'dropbear');
  validateArmElf(resolve(agentRoot, 'plaf203-state-agent'));
  validateArmElf(resolve(agentRoot, 'plaf203-update-fs'));
  validateDropbear(dropbearRoot);

  const token = randomBytes(32).toString('hex');
  const entries: TarEntry[] = [];
  addDirectory(entries, 'local-state-agent');
  addFileFromDisk(
    entries,
    resolve(agentRoot, 'plaf203-state-agent'),
    'local-state-agent/plaf203-state-agent',
  );
  addFileFromDisk(
    entries,
    resolve(agentRoot, 'plaf203-update-fs'),
    'local-state-agent/plaf203-update-fs',
  );
  addFileFromDisk(
    entries,
    resolve(agentRoot, 'app_start_snippet.sh'),
    'local-state-agent/app_start_snippet.sh',
  );
  addTree(entries, resolve(agentRoot, 'runit'), 'local-state-agent/runit');
  const runEntry = entries.find(
    ({ path }) => path === 'local-state-agent/runit/plaf203-state-agent/run',
  );
  if (runEntry?.data === undefined) {
    throw new Error('The packaged State Agent run template is missing.');
  }
  const template = runEntry.data.toString('utf8');
  if (!template.includes('--allow-ip 192.0.2.10')) {
    throw new Error(
      'The packaged State Agent run template lacks the expected allowlist placeholder.',
    );
  }
  runEntry.data = Buffer.from(
    template.replace(
      '--allow-ip 192.0.2.10',
      `--allow-ip ${input.homeAssistantIp}`,
    ),
  );
  entries.push(fileEntry('local-state-agent/token', `${token}\n`));
  entries.push(
    fileEntry(
      'app_start.sh',
      readFileSync(resolve(assetDirectory, 'app-start.sh')),
    ),
  );
  entries.push(fileEntry('authorized_keys', `${key}\n`));
  addDirectory(entries, 'dropbear');
  for (const name of [
    'dropbearmulti',
    'LICENSE.dropbear',
    'BUILD_INFO.dropbear.json',
  ]) {
    addFileFromDisk(entries, resolve(dropbearRoot, name), `dropbear/${name}`);
  }

  const archive = gzipSync(buildTar(entries), { level: 9 });
  const installer = readFileSync(
    resolve(assetDirectory, 'bootstrap-installer.sh'),
  );
  const payload = Buffer.concat([installer, archive]);
  const output = resolve(outputDirectory, 'AF203_FW.bootstrap');
  const temporary = `${output}.new`;
  rmSync(temporary, { force: true });
  writeFileSync(temporary, payload, { mode: 0o700, flag: 'wx' });
  chmodSync(temporary, 0o700);
  renameSync(temporary, output);
  return {
    path: output,
    sha256: digest('sha256', payload),
    md5: digest('md5', payload),
    bytes: payload.length,
    stateAgentToken: token,
  };
}

interface TarEntry {
  readonly path: string;
  data?: Buffer;
  readonly mode: number;
  readonly type: '0' | '5';
}

function buildTar(entries: readonly TarEntry[]): Buffer {
  const blocks: Buffer[] = [];
  for (const entry of [...entries].sort((left, right) =>
    left.path.localeCompare(right.path),
  )) {
    const data = entry.data ?? Buffer.alloc(0);
    const header = Buffer.alloc(TAR_BLOCK);
    writeTarText(header, 0, 100, entry.path);
    writeTarOctal(header, 100, 8, entry.mode);
    writeTarOctal(header, 108, 8, 0);
    writeTarOctal(header, 116, 8, 0);
    writeTarOctal(header, 124, 12, data.length);
    writeTarOctal(header, 136, 12, 0);
    header.fill(0x20, 148, 156);
    header[156] = entry.type.charCodeAt(0);
    writeTarText(header, 257, 6, 'ustar');
    // The POSIX ustar version field is exactly two bytes and has no NUL terminator.
    header.write('00', 263, 2, 'ascii');
    writeTarText(header, 265, 32, 'root');
    writeTarText(header, 297, 32, 'root');
    writeTarOctal(header, 148, 8, checksum(header));
    blocks.push(header, data);
    const padding = (TAR_BLOCK - (data.length % TAR_BLOCK)) % TAR_BLOCK;
    if (padding > 0) blocks.push(Buffer.alloc(padding));
  }
  blocks.push(Buffer.alloc(TAR_BLOCK * 2));
  return Buffer.concat(blocks);
}

function addTree(entries: TarEntry[], root: string, archiveRoot: string): void {
  if (!statSync(root).isDirectory())
    throw new Error(`${root} is not a directory.`);
  addDirectory(entries, archiveRoot);
  for (const name of readdirSync(root).sort()) {
    const source = resolve(root, name);
    const target = join(archiveRoot, name);
    const stat = statSync(source);
    if (stat.isDirectory()) addTree(entries, source, target);
    else if (stat.isFile()) addFileFromDisk(entries, source, target);
    else
      throw new Error(`Unsupported artifact type: ${relative(root, source)}`);
  }
}

function addDirectory(entries: TarEntry[], path: string): void {
  entries.push({
    path: path.endsWith('/') ? path : `${path}/`,
    mode: 0o755,
    type: '5',
  });
}

function addFileFromDisk(
  entries: TarEntry[],
  source: string,
  target: string,
): void {
  if (!statSync(source).isFile()) throw new Error(`${source} is not a file.`);
  entries.push(fileEntry(target, readFileSync(source)));
}

function fileEntry(path: string, value: string | Buffer): TarEntry {
  return {
    path,
    data: typeof value === 'string' ? Buffer.from(value, 'utf8') : value,
    mode: 0o600,
    type: '0',
  };
}

function validateArmElf(path: string): void {
  const header = readFileSync(path).subarray(0, 40);
  if (
    header.length < 40 ||
    !header.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) ||
    header[4] !== 1 ||
    header.readUInt16LE(18) !== 40
  ) {
    throw new Error(`${basename(path)} is not a supported ELF32 ARM artifact.`);
  }
}

function validateDropbear(directory: string): void {
  const binary = resolve(directory, 'dropbearmulti');
  validateArmElf(binary);
  const header = readFileSync(binary).subarray(0, 40);
  if ((header.readUInt32LE(36) & 0x400) === 0) {
    throw new Error('dropbearmulti is not an ARM hard-float artifact.');
  }
  const infoPath = resolve(directory, 'BUILD_INFO.dropbear.json');
  const info = JSON.parse(readFileSync(infoPath, 'utf8')) as Record<
    string,
    unknown
  >;
  if (
    info.commit !== '28216cd9af822732a1549b78621c2ea9a76a0fe5' ||
    info.target !== 'arm-linux-musleabihf' ||
    info.password_auth !== false ||
    info.dropbearmulti_sha256 !== digest('sha256', readFileSync(binary))
  ) {
    throw new Error(
      'The packaged Dropbear provenance or integrity is invalid.',
    );
  }
  if (!statSync(resolve(directory, 'LICENSE.dropbear')).isFile()) {
    throw new Error('The packaged Dropbear license is missing.');
  }
}

function writeTarText(
  target: Buffer,
  offset: number,
  length: number,
  value: string,
): void {
  const data = Buffer.from(value, 'utf8');
  if (data.length >= length)
    throw new Error(`Tar path or field is too long: ${value}`);
  data.copy(target, offset);
}

function writeTarOctal(
  target: Buffer,
  offset: number,
  length: number,
  value: number,
): void {
  const octal = value.toString(8).padStart(length - 2, '0');
  writeTarText(target, offset, length, `${octal} `);
}

function checksum(header: Buffer): number {
  let total = 0;
  for (const byte of header) total += byte;
  return total;
}

function digest(algorithm: 'sha256' | 'md5', value: Buffer): string {
  return createHash(algorithm).update(value).digest('hex');
}
