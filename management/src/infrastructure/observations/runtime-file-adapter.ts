import { readFile } from 'node:fs/promises';
import { join } from 'node:path';

export interface RuntimeOptions {
  readonly mqttHost: string;
  readonly mqttPort: number;
  readonly mqttUsername: string;
  readonly mqttPassword: string;
  readonly persistFeederMqtt: boolean;
  readonly feederMqttHost: string;
  readonly feederMqttPort: number;
  readonly stateAgentUrl: string;
  readonly stateAgentTokenConfigured: boolean;
  readonly deviceDiscovery: boolean;
  readonly cameraQuality: string;
  readonly publishCameraMetadata: boolean;
}

export interface RegistryDevice {
  readonly product: string;
  readonly serial: string;
  readonly uid: string | null;
  readonly ipAddress: string | null;
  readonly streamName: string | null;
  readonly lastMqttSeen: string | null;
  readonly lastIpResolved: string | null;
  readonly manualOverride: boolean;
  readonly ready: Readonly<Record<string, boolean>>;
}

export interface DeviceRegistryStatus {
  readonly lastUpdate: string | null;
  readonly devices: readonly RegistryDevice[];
}

export interface ControllerStatus {
  readonly product: string;
  readonly serial: string;
  readonly lifecycle: string;
  readonly reconciliationState: string;
  readonly feederMqttConnected: boolean;
  readonly stateAgentHealthy: boolean;
  readonly writesReady: boolean;
  readonly writePending: boolean;
  readonly lastFeederActivityAt: string | null;
  readonly lastHomeAssistantBirthAt: string | null;
  readonly lastUpdate: string;
  readonly verifiedState: {
    readonly verifiedAt: string;
    readonly revisions: Readonly<Record<string, string>>;
    readonly planCount: number;
    readonly queuePending: boolean;
    readonly settings: Readonly<Record<string, unknown>>;
  } | null;
}

export interface CameraStatus {
  readonly status: string;
  readonly requestedQuality: string;
  readonly lastUpdate: string;
  readonly actualResolution: {
    readonly width: number;
    readonly height: number;
  } | null;
  readonly health: Readonly<Record<string, number | null>>;
}

export type FileObservation<T> =
  | { readonly kind: 'available'; readonly value: T }
  | { readonly kind: 'missing' }
  | { readonly kind: 'invalid'; readonly detail: string };

export class RuntimeFileAdapter {
  public constructor(private readonly dataDirectory: string) {}

  public async options(): Promise<FileObservation<RuntimeOptions>> {
    const result = await this.readObject('options.json');
    if (result.kind !== 'available') return result;
    const value = result.value;
    const mqttHost = text(value.mqtt_host);
    const mqttPort = integer(value.mqtt_port);
    if (mqttHost === null || mqttPort === null) {
      return invalid('Supervisor options omit a valid MQTT endpoint.');
    }
    return {
      kind: 'available',
      value: {
        mqttHost,
        mqttPort,
        mqttUsername: text(value.mqtt_username) ?? '',
        mqttPassword: text(value.mqtt_password) ?? '',
        persistFeederMqtt: boolean(value.persist_feeder_mqtt, false),
        feederMqttHost: text(value.feeder_mqtt_host) ?? '',
        feederMqttPort: integer(value.feeder_mqtt_port) ?? 1883,
        stateAgentUrl: text(value.petlibro_state_agent_url) ?? '',
        stateAgentTokenConfigured:
          (text(value.petlibro_state_agent_token) ?? '').length > 0,
        deviceDiscovery: boolean(value.device_discovery, true),
        cameraQuality: text(value.camera_quality) ?? 'hd',
        publishCameraMetadata: boolean(value.publish_camera_metadata, true),
      },
    };
  }

  public async registry(): Promise<FileObservation<DeviceRegistryStatus>> {
    const result = await this.readObject('devices.json');
    if (result.kind !== 'available') return result;
    if (result.value.schema_version !== 1 || !isRecord(result.value.devices)) {
      return invalid('AppDaemon device registry uses an unsupported schema.');
    }
    const devices: RegistryDevice[] = [];
    for (const candidate of Object.values(result.value.devices)) {
      if (!isRecord(candidate)) continue;
      const product = text(candidate.product);
      const serial = text(candidate.serial);
      if (product === null || serial === null) continue;
      const ready = isRecord(candidate.ready)
        ? Object.fromEntries(
            Object.entries(candidate.ready).filter(
              (entry): entry is [string, boolean] =>
                typeof entry[1] === 'boolean',
            ),
          )
        : {};
      devices.push({
        product,
        serial,
        uid: text(candidate.uid),
        ipAddress: text(candidate.ip_address),
        streamName: text(candidate.stream_name),
        lastMqttSeen: timestamp(candidate.last_mqtt_seen),
        lastIpResolved: timestamp(candidate.last_ip_resolved),
        manualOverride: candidate.manual_override === true,
        ready,
      });
    }
    return {
      kind: 'available',
      value: { lastUpdate: timestamp(result.value.last_update), devices },
    };
  }

  public async controller(
    product: string,
    serial: string,
  ): Promise<FileObservation<ControllerStatus>> {
    const result = await this.readObject(
      `petlibro_controller_status_${safe(product)}_${safe(serial)}.json`,
    );
    if (result.kind !== 'available') return result;
    const value = result.value;
    if (
      value.schema_version !== 1 ||
      value.product !== product ||
      value.serial !== serial ||
      typeof value.lifecycle !== 'string' ||
      typeof value.reconciliation_state !== 'string' ||
      typeof value.feeder_mqtt_connected !== 'boolean' ||
      typeof value.state_agent_healthy !== 'boolean' ||
      typeof value.writes_ready !== 'boolean' ||
      typeof value.write_pending !== 'boolean' ||
      timestamp(value.last_update) === null
    ) {
      return invalid('AppDaemon controller status is malformed.');
    }
    const verified = verifiedState(value.verified_state);
    if (value.verified_state !== null && verified === null) {
      return invalid('AppDaemon verified-state summary is malformed.');
    }
    return {
      kind: 'available',
      value: {
        product,
        serial,
        lifecycle: value.lifecycle,
        reconciliationState: value.reconciliation_state,
        feederMqttConnected: value.feeder_mqtt_connected,
        stateAgentHealthy: value.state_agent_healthy,
        writesReady: value.writes_ready,
        writePending: value.write_pending,
        lastFeederActivityAt: timestamp(value.last_feeder_activity_at),
        lastHomeAssistantBirthAt: timestamp(value.last_home_assistant_birth_at),
        lastUpdate: value.last_update as string,
        verifiedState: verified,
      },
    };
  }

  public async camera(
    streamName: string,
  ): Promise<FileObservation<CameraStatus>> {
    const result = await this.readObject(
      `petlibro_camera_status_${safe(streamName)}.json`,
    );
    if (result.kind !== 'available') return result;
    const value = result.value;
    const observedAt = timestamp(value.last_update);
    if (
      value.schema_version !== 2 ||
      typeof value.status !== 'string' ||
      typeof value.requested_quality !== 'string' ||
      observedAt === null ||
      !isRecord(value.health)
    ) {
      return invalid('go2rtc camera runtime status is malformed.');
    }
    const resolution = isRecord(value.actual_resolution)
      ? resolutionValue(value.actual_resolution)
      : null;
    const health = Object.fromEntries(
      Object.entries(value.health).filter(
        (entry): entry is [string, number | null] =>
          entry[1] === null ||
          (typeof entry[1] === 'number' &&
            Number.isInteger(entry[1]) &&
            entry[1] >= 0),
      ),
    );
    return {
      kind: 'available',
      value: {
        status: value.status,
        requestedQuality: value.requested_quality,
        lastUpdate: observedAt,
        actualResolution: resolution,
        health,
      },
    };
  }

  private async readObject(
    relativePath: string,
  ): Promise<FileObservation<Record<string, unknown>>> {
    try {
      const decoded: unknown = JSON.parse(
        await readFile(join(this.dataDirectory, relativePath), 'utf8'),
      );
      return isRecord(decoded)
        ? { kind: 'available', value: decoded }
        : invalid(`${relativePath} does not contain an object.`);
    } catch (error) {
      if (isNodeError(error) && error.code === 'ENOENT')
        return { kind: 'missing' };
      return invalid(`${relativePath} could not be read or decoded.`);
    }
  }
}

function verifiedState(value: unknown): ControllerStatus['verifiedState'] {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    timestamp(value.verified_at) === null ||
    !isRecord(value.revisions) ||
    !Object.values(value.revisions).every((item) => typeof item === 'string') ||
    !Number.isInteger(value.plan_count) ||
    typeof value.queue_pending !== 'boolean' ||
    !isRecord(value.settings)
  )
    return null;
  return {
    verifiedAt: value.verified_at as string,
    revisions: value.revisions as Record<string, string>,
    planCount: value.plan_count as number,
    queuePending: value.queue_pending,
    settings: value.settings,
  };
}

function resolutionValue(value: Record<string, unknown>) {
  return Number.isInteger(value.width) && Number.isInteger(value.height)
    ? { width: value.width as number, height: value.height as number }
    : null;
}

function safe(value: string): string {
  return value.replaceAll(/[^A-Za-z0-9_-]+/g, '_');
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function integer(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
    ? value
    : null;
}

function boolean(value: unknown, fallback: boolean): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function timestamp(value: unknown): string | null {
  return typeof value === 'string' && !Number.isNaN(Date.parse(value))
    ? value
    : null;
}

function invalid<T>(detail: string): FileObservation<T> {
  return { kind: 'invalid', detail };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isNodeError(error: unknown): error is NodeJS.ErrnoException {
  return error instanceof Error && 'code' in error;
}
