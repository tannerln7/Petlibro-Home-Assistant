import type { DeviceOverviewSnapshot } from '#/domain/device-overview';
import type { OperationEventHistorySnapshot } from '#/domain/operation-event-history';
import type { SystemStatusSnapshot } from '#/domain/system-status';

export interface DeviceOverviewReader {
  getSnapshot(): Promise<DeviceOverviewSnapshot>;
}

export interface SystemStatusReader {
  getSnapshot(): Promise<SystemStatusSnapshot>;
}

export interface OperationEventHistoryReader {
  snapshot(filter: { readonly limit: number }): OperationEventHistorySnapshot;
}

export interface DiagnosticReport {
  readonly schemaVersion: 1;
  readonly generatedAt: string;
  readonly system: SystemStatusSnapshot;
  readonly runtime: DeviceOverviewSnapshot;
  readonly operationEvidence: OperationEventHistorySnapshot;
  readonly limitations: readonly string[];
}

export class DiagnosticReportService {
  public constructor(
    private readonly systemStatus: SystemStatusReader,
    private readonly deviceOverview: DeviceOverviewReader,
    private readonly operationEventHistory: OperationEventHistoryReader,
    private readonly clock: () => Date = () => new Date(),
  ) {}

  public async generate(): Promise<DiagnosticReport> {
    const [system, runtime] = await Promise.all([
      this.systemStatus.getSnapshot(),
      this.deviceOverview.getSnapshot(),
    ]);
    return {
      schemaVersion: 1,
      generatedAt: this.clock().toISOString(),
      system,
      runtime,
      operationEvidence: this.operationEventHistory.snapshot({ limit: 200 }),
      limitations: [
        'This report contains bounded observations, not a live packet capture or media trace.',
        'Home Assistant and Supervisor current health are unavailable without a supported least-privilege API integration.',
        'Raw operational-service logs are ephemeral and unavailable until a supported on-demand source adapter is connected.',
        'Historical MQTT and controller observations are not proof of current device state.',
      ],
    };
  }
}
