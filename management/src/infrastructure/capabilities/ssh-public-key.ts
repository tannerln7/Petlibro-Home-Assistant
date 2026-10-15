const SUPPORTED_ALGORITHMS = new Set([
  'ssh-ed25519',
  'ssh-rsa',
  'ecdsa-sha2-nistp256',
]);

export function validateSshPublicKey(value: string): string {
  const normalized = value.trim();
  if (normalized.length === 0) {
    throw new TypeError(
      'an SSH public key is required for feeder recovery access',
    );
  }
  if (normalized.includes('\n') || normalized.includes('\r')) {
    throw new TypeError('SSH public key must contain exactly one line');
  }
  const fields = normalized.split(/\s+/);
  const algorithm = fields[0];
  const encoded = fields[1];
  if (
    fields.length < 2 ||
    algorithm === undefined ||
    encoded === undefined ||
    !SUPPORTED_ALGORITHMS.has(algorithm)
  ) {
    throw new TypeError(
      'SSH public key must be one supported OpenSSH public-key line',
    );
  }
  let blob: Buffer;
  try {
    blob = Buffer.from(encoded, 'base64');
    if (blob.length === 0 || blob.toString('base64') !== encoded) {
      throw new Error('invalid');
    }
  } catch {
    throw new TypeError('SSH public key contains invalid base64');
  }
  const type = readField(blob, 0);
  if (!type.value.equals(Buffer.from(algorithm, 'ascii'))) {
    throw new TypeError(
      'SSH public key type does not match its encoded key blob',
    );
  }
  let offset = type.offset;
  if (algorithm === 'ssh-ed25519') {
    const key = readField(blob, offset);
    offset = key.offset;
    if (key.value.length !== 32) {
      throw new TypeError('Ed25519 SSH public key must contain 32 key bytes');
    }
  } else if (algorithm === 'ssh-rsa') {
    const exponent = readField(blob, offset);
    const modulus = readField(blob, exponent.offset);
    offset = modulus.offset;
    if (exponent.value.length === 0 || modulus.value.length === 0) {
      throw new TypeError('RSA SSH public key is incomplete');
    }
  } else {
    const curve = readField(blob, offset);
    const point = readField(blob, curve.offset);
    offset = point.offset;
    if (
      curve.value.toString('ascii') !== 'nistp256' ||
      point.value.length !== 65 ||
      point.value[0] !== 0x04
    ) {
      throw new TypeError('ECDSA SSH public key is not a valid nistp256 key');
    }
  }
  if (offset !== blob.length) {
    throw new TypeError('SSH public key blob contains trailing data');
  }
  return normalized;
}

function readField(blob: Buffer, offset: number) {
  if (offset + 4 > blob.length) {
    throw new TypeError('SSH public key blob is truncated');
  }
  const length = blob.readUInt32BE(offset);
  const end = offset + 4 + length;
  if (end > blob.length) {
    throw new TypeError('SSH public key blob is truncated');
  }
  return { value: blob.subarray(offset + 4, end), offset: end };
}
