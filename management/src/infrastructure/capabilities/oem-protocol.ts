const PROVISIONING_MAGIC = Uint8Array.from([0x44, 0x4c]);
const SET_MQTT_SERVER = 0x02;

export class OemProtocolError extends Error {}

export interface MqttConnectIdentity {
  readonly clientId: string;
  readonly username: string | null;
  readonly password: Uint8Array | null;
  readonly keepAlive: number;
  readonly protocolLevel: 3 | 4;
}

export interface MqttSubscription {
  readonly packetId: number;
  readonly topics: readonly { readonly topic: string; readonly qos: number }[];
}

export interface MqttPublication {
  readonly topic: string;
  readonly packetId?: number;
  readonly qos: number;
  readonly retain: boolean;
  readonly duplicate: boolean;
  readonly payload: Uint8Array;
}

export function buildSetMqttServerFrame(
  host: string,
  port: number,
  memberId: string,
  sequence = 1,
): Uint8Array {
  if (host.length === 0 || host.includes(':')) {
    throw new TypeError("host must be non-empty and without ':'");
  }
  if (!Number.isSafeInteger(port) || port < 1000 || port > 9999) {
    throw new TypeError('port must contain exactly four decimal digits');
  }
  if (!/^\d+$/.test(memberId)) {
    throw new TypeError('member ID must contain decimal digits');
  }
  if (!Number.isSafeInteger(sequence) || sequence < 0 || sequence > 0xff) {
    throw new TypeError('sequence must fit in one byte');
  }
  const endpoint = ascii(`${host}:${port}`, 'endpoint');
  const member = ascii(memberId, 'member ID');
  if (endpoint.length > 99) {
    throw new TypeError("endpoint exceeds the firmware's 99-byte safe limit");
  }
  if (member.length > 15) {
    throw new TypeError('member ID is too long');
  }
  const payloadLength = endpoint.length + member.length + 2;
  if (payloadLength > 0xff) {
    throw new TypeError('provisioning payload is too large');
  }
  return Uint8Array.from([
    ...PROVISIONING_MAGIC,
    SET_MQTT_SERVER,
    0,
    sequence,
    payloadLength,
    endpoint.length,
    ...endpoint,
    member.length,
    ...member,
    0xff,
  ]);
}

export function encodeRemainingLength(length: number): Uint8Array {
  if (!Number.isSafeInteger(length) || length < 0 || length > 268_435_455) {
    throw new TypeError('MQTT remaining length is out of range');
  }
  const encoded: number[] = [];
  let remaining = length;
  do {
    let digit = remaining % 128;
    remaining = Math.floor(remaining / 128);
    if (remaining > 0) {
      digit |= 0x80;
    }
    encoded.push(digit);
  } while (remaining > 0);
  return Uint8Array.from(encoded);
}

export function encodeMqttString(value: string | Uint8Array): Uint8Array {
  const encoded =
    typeof value === 'string' ? new TextEncoder().encode(value) : value;
  if (encoded.length > 0xffff) {
    throw new TypeError('MQTT string is too long');
  }
  return Uint8Array.from([
    encoded.length >> 8,
    encoded.length & 0xff,
    ...encoded,
  ]);
}

export function parseConnect(packet: Uint8Array): MqttConnectIdentity {
  const { packetType, flags, body } = mqttPacket(packet);
  if (packetType !== 1 || flags !== 0) {
    throw new OemProtocolError('packet is not an MQTT CONNECT');
  }
  let offset = 0;
  const protocol = readBytes(body, offset);
  offset = protocol.offset;
  const protocolName = decodeUtf8(protocol.value, 'protocol name');
  if (offset + 4 > body.length) {
    throw new OemProtocolError('CONNECT variable header is truncated');
  }
  const protocolLevel = body[offset] as number;
  const connectFlags = body[offset + 1] as number;
  const keepAlive =
    ((body[offset + 2] as number) << 8) | (body[offset + 3] as number);
  offset += 4;
  if (
    !(
      (protocolName === 'MQIsdp' && protocolLevel === 3) ||
      (protocolName === 'MQTT' && protocolLevel === 4)
    )
  ) {
    throw new OemProtocolError('only MQTT 3.1 and 3.1.1 are supported');
  }
  if ((connectFlags & 0x01) !== 0) {
    throw new OemProtocolError('CONNECT reserved flag is set');
  }
  const client = readBytes(body, offset);
  offset = client.offset;
  if ((connectFlags & 0x04) !== 0) {
    const willTopic = readBytes(body, offset);
    offset = willTopic.offset;
    const willPayload = readBytes(body, offset);
    offset = willPayload.offset;
  }
  let username: string | null = null;
  if ((connectFlags & 0x80) !== 0) {
    const field = readBytes(body, offset);
    username = decodeUtf8(field.value, 'username');
    offset = field.offset;
  }
  let password: Uint8Array | null = null;
  if ((connectFlags & 0x40) !== 0) {
    const field = readBytes(body, offset);
    password = field.value;
    offset = field.offset;
  }
  if (offset !== body.length) {
    throw new OemProtocolError('CONNECT payload contains trailing bytes');
  }
  return {
    clientId: decodeUtf8(client.value, 'client ID'),
    username,
    password,
    keepAlive,
    protocolLevel: protocolLevel as 3 | 4,
  };
}

export function parseSubscribe(packet: Uint8Array): MqttSubscription {
  const { packetType, flags, body } = mqttPacket(packet);
  if (packetType !== 8 || flags !== 2 || body.length < 5) {
    throw new OemProtocolError('packet is not a valid MQTT SUBSCRIBE');
  }
  const packetId = ((body[0] as number) << 8) | (body[1] as number);
  if (packetId === 0) {
    throw new OemProtocolError('SUBSCRIBE packet ID must not be zero');
  }
  let offset = 2;
  const topics: Array<{ topic: string; qos: number }> = [];
  while (offset < body.length) {
    const field = readBytes(body, offset);
    offset = field.offset;
    if (offset >= body.length) {
      throw new OemProtocolError('SUBSCRIBE QoS is missing');
    }
    const qos = body[offset] as number;
    offset += 1;
    if (qos > 2) {
      throw new OemProtocolError('SUBSCRIBE QoS is invalid');
    }
    topics.push({ topic: decodeUtf8(field.value, 'topic'), qos });
  }
  return { packetId, topics };
}

export function parsePublish(packet: Uint8Array): MqttPublication {
  const { packetType, flags, body } = mqttPacket(packet);
  if (packetType !== 3) {
    throw new OemProtocolError('packet is not an MQTT PUBLISH');
  }
  const qos = (flags >> 1) & 0x03;
  if (qos === 3) {
    throw new OemProtocolError('PUBLISH QoS is invalid');
  }
  const topicField = readBytes(body, 0);
  let offset = topicField.offset;
  let packetId: number | undefined;
  if (qos > 0) {
    if (offset + 2 > body.length) {
      throw new OemProtocolError('PUBLISH packet ID is truncated');
    }
    packetId = ((body[offset] as number) << 8) | (body[offset + 1] as number);
    offset += 2;
    if (packetId === 0) {
      throw new OemProtocolError('PUBLISH packet ID must not be zero');
    }
  }
  return {
    topic: decodeUtf8(topicField.value, 'topic'),
    ...(packetId === undefined ? {} : { packetId }),
    qos,
    retain: (flags & 0x01) !== 0,
    duplicate: (flags & 0x08) !== 0,
    payload: body.slice(offset),
  };
}

export function buildConnack(returnCode = 0): Uint8Array {
  if (!Number.isSafeInteger(returnCode) || returnCode < 0 || returnCode > 5) {
    throw new TypeError('CONNACK return code is invalid');
  }
  return Uint8Array.from([0x20, 0x02, 0x00, returnCode]);
}

export function buildSuback(
  packetId: number,
  grantedQos: readonly number[],
): Uint8Array {
  const id = mqttPacketId(packetId);
  if (grantedQos.some((qos) => ![0, 1, 2, 0x80].includes(qos))) {
    throw new TypeError('SUBACK granted QoS is invalid');
  }
  const body = Uint8Array.from([...id, ...grantedQos]);
  return Uint8Array.from([
    0x90,
    ...encodeRemainingLength(body.length),
    ...body,
  ]);
}

export function buildPuback(packetId: number): Uint8Array {
  return Uint8Array.from([0x40, 0x02, ...mqttPacketId(packetId)]);
}

export function buildPublish(
  topic: string,
  payload: Uint8Array,
  packetId = 1,
  qos: 0 | 1 = 1,
): Uint8Array {
  const topicField = encodeMqttString(topic);
  const body = Uint8Array.from([
    ...topicField,
    ...(qos === 0 ? [] : mqttPacketId(packetId)),
    ...payload,
  ]);
  return Uint8Array.from([
    0x30 | (qos << 1),
    ...encodeRemainingLength(body.length),
    ...body,
  ]);
}

export function buildOtaCommand(input: {
  readonly messageId: string;
  readonly artifactUrl: string;
  readonly artifactMd5: string;
  readonly targetVersion: string;
}): Uint8Array {
  if (!input.targetVersion.startsWith('3') || input.targetVersion.length > 31) {
    throw new TypeError('target version must be in the 3.x.y firmware family');
  }
  if (!/^[0-9a-f]{32}$/.test(input.artifactMd5)) {
    throw new TypeError('artifact MD5 must contain 32 lowercase hex digits');
  }
  let artifactUrl: Uint8Array;
  try {
    artifactUrl = ascii(input.artifactUrl, 'artifact URL');
  } catch {
    throw new TypeError('artifact URL must contain ASCII characters');
  }
  if (artifactUrl.length > 256) {
    throw new TypeError('artifact URL exceeds firmware limit');
  }
  return new TextEncoder().encode(
    JSON.stringify({
      cmd: 'OTA_UPGRADE',
      msgId: input.messageId,
      ts: 0,
      upgradeType: 2,
      url: input.artifactUrl,
      targetSoftwareVersion: input.targetVersion,
      md5: input.artifactMd5,
    }),
  );
}

function mqttPacket(packet: Uint8Array) {
  if (packet.length < 2) {
    throw new OemProtocolError('MQTT packet is truncated');
  }
  const header = packet[0] as number;
  let multiplier = 1;
  let remainingLength = 0;
  let offset = 1;
  for (let index = 0; index < 4; index += 1) {
    if (offset >= packet.length) {
      throw new OemProtocolError('MQTT remaining length is truncated');
    }
    const digit = packet[offset] as number;
    offset += 1;
    remainingLength += (digit & 0x7f) * multiplier;
    if ((digit & 0x80) === 0) {
      if (offset + remainingLength !== packet.length) {
        throw new OemProtocolError('MQTT packet length does not match');
      }
      return {
        packetType: header >> 4,
        flags: header & 0x0f,
        body: packet.slice(offset),
      };
    }
    multiplier *= 128;
  }
  throw new OemProtocolError('MQTT remaining length is invalid');
}

function readBytes(data: Uint8Array, offset: number) {
  if (offset + 2 > data.length) {
    throw new OemProtocolError('MQTT length-prefixed field is truncated');
  }
  const length = ((data[offset] as number) << 8) | (data[offset + 1] as number);
  const end = offset + 2 + length;
  if (end > data.length) {
    throw new OemProtocolError('MQTT length-prefixed value is truncated');
  }
  return { value: data.slice(offset + 2, end), offset: end };
}

function decodeUtf8(value: Uint8Array, name: string): string {
  try {
    return new TextDecoder('utf-8', { fatal: true }).decode(value);
  } catch {
    throw new OemProtocolError(`${name} is not valid UTF-8`);
  }
}

function mqttPacketId(value: number): readonly number[] {
  if (!Number.isSafeInteger(value) || value < 1 || value > 0xffff) {
    throw new TypeError('MQTT packet ID must be between 1 and 65535');
  }
  return [value >> 8, value & 0xff];
}

function ascii(value: string, name: string): Uint8Array {
  if (!/^[\x20-\x7e]+$/.test(value)) {
    throw new TypeError(`${name} must contain printable ASCII`);
  }
  return new TextEncoder().encode(value);
}
