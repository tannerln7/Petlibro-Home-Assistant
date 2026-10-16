import type {
  InstallationReadinessObservation,
  InstallationReadinessObserver,
} from '#/application/installation-coordinator';
import type { InstallationOperationSnapshot } from '#/domain/operations';
import type { RuntimeFileAdapter } from '#/infrastructure/observations/runtime-file-adapter';

/**
 * Correlates Supervisor-rendered options with AppDaemon-owned runtime status.
 * It observes existing authorities and never writes configuration or feeder state.
 */
export class InstallationReadinessService
  implements InstallationReadinessObserver
{
  public constructor(
    private readonly files: RuntimeFileAdapter,
    private readonly clock: () => Date = () => new Date(),
    private readonly staleAfterMilliseconds = 2 * 60_000,
  ) {}

  public async inspect(
    installation: InstallationOperationSnapshot,
    expectedBackendPassword: string,
  ): Promise<InstallationReadinessObservation> {
    const [options, registry] = await Promise.all([
      this.files.options(),
      this.files.registry(),
    ]);
    const configured =
      options.kind === 'available' &&
      options.value.mqttHost === installation.configuration.brokerHost &&
      options.value.mqttPort === installation.configuration.brokerPort &&
      options.value.mqttUsername ===
        installation.configuration.backendUsername &&
      options.value.mqttPassword === expectedBackendPassword &&
      options.value.persistFeederMqtt &&
      options.value.feederMqttHost === installation.configuration.brokerHost &&
      options.value.feederMqttPort === 1883 &&
      options.value.stateAgentUrl === 'http://{ip}:8765' &&
      options.value.stateAgentTokenConfigured;
    const registryFresh =
      registry.kind === 'available' && this.fresh(registry.value.lastUpdate);
    const capturedIdentity = installation.deviceIdentity;
    const device =
      registryFresh && capturedIdentity !== undefined
        ? registry.value.devices.find(
            (candidate) =>
              candidate.product === capturedIdentity.product &&
              candidate.serial === capturedIdentity.serial &&
              candidate.ipAddress === installation.configuration.feederIp,
          )
        : undefined;
    const controller =
      device === undefined
        ? ({ kind: 'missing' } as const)
        : await this.files.controller(device.product, device.serial);
    const controllerFresh =
      controller.kind === 'available' &&
      this.fresh(controller.value.lastUpdate);
    const controllerReady =
      controller.kind === 'available' &&
      controllerFresh &&
      controller.value.lifecycle === 'running' &&
      controller.value.reconciliationState === 'READY' &&
      controller.value.feederMqttConnected &&
      controller.value.stateAgentHealthy &&
      controller.value.writesReady;
    const checks = {
      effectiveConfigurationAdopted: configured,
      installationIdentityCaptured: capturedIdentity !== undefined,
      deviceRegistryFresh: registryFresh,
      feederIdentityObservedAtExpectedAddress:
        registryFresh && device !== undefined,
      controllerObservationFresh: controllerFresh,
      controllerReady,
    };
    const missing = Object.entries(checks)
      .filter(([, ready]) => !ready)
      .map(([name]) => name);
    return {
      ready: missing.length === 0,
      checks,
      summary:
        missing.length === 0
          ? 'Effective add-on configuration, feeder discovery, and AppDaemon READY reconciliation were observed.'
          : `Still waiting for: ${missing.join(', ')}.`,
    };
  }

  private fresh(timestamp: string | null): boolean {
    if (timestamp === null) return false;
    const observed = Date.parse(timestamp);
    const now = this.clock().getTime();
    return (
      Number.isFinite(observed) &&
      observed <= now + 30_000 &&
      observed >= now - this.staleAfterMilliseconds
    );
  }
}
