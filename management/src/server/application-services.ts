import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { DeviceOverviewService } from '#/application/device-overview-service';
import { DiagnosticReportService } from '#/application/diagnostic-report-service';
import { InstallationCoordinator } from '#/application/installation-coordinator';
import { InstallationReadinessService } from '#/application/installation-readiness-service';
import { LiveLogService } from '#/application/live-log-service';
import { OperationEventHistoryService } from '#/application/operation-event-history-service';
import { OperationManager } from '#/application/operation-manager';
import { SetupPreflightService } from '#/application/setup-preflight-service';
import { SystemStatusService } from '#/application/system-status-service';
import { SupervisorLogAdapter } from '#/infrastructure/logging/supervisor-log-adapter';
import { MqttBrokerAdapter } from '#/infrastructure/observations/mqtt-broker-adapter';
import { RuntimeFileAdapter } from '#/infrastructure/observations/runtime-file-adapter';
import { InstallationSecretStore } from '#/infrastructure/persistence/installation-secret-store';
import { SqliteOperationJournal } from '#/infrastructure/persistence/sqlite-operation-journal';
import { CapabilityWorkerClient } from '#/infrastructure/worker/capability-worker-client';

export interface ApplicationServices {
  readonly systemStatus: SystemStatusService;
  readonly deviceOverview: DeviceOverviewService;
  readonly operations: OperationManager;
  readonly operationEventHistory: OperationEventHistoryService;
  readonly diagnostics: DiagnosticReportService;
  readonly setupPreflight: SetupPreflightService;
  readonly installations: InstallationCoordinator;
  readonly liveLogs: LiveLogService;
}

export function createApplicationServices(
  environment: NodeJS.ProcessEnv = process.env,
): ApplicationServices {
  const dataDirectory = resolve(
    environment.PETLIBRO_MANAGEMENT_DATA_DIR ?? '.data',
  );
  const workerSocket = resolve(
    environment.PETLIBRO_CAPABILITY_WORKER_SOCKET ??
      environment.PETLIBRO_WORKER_SOCKET ??
      `${dataDirectory}/worker.sock`,
  );
  const worker = new CapabilityWorkerClient(workerSocket);
  const runtimeDataDirectory = resolve(
    environment.PETLIBRO_RUNTIME_DATA_DIR ?? '.runtime-data',
  );
  const stateAgentConfiguration = {
    ...(environment.PETLIBRO_STATE_AGENT_URL === undefined
      ? {}
      : { baseUrl: environment.PETLIBRO_STATE_AGENT_URL }),
    ...configuredToken(environment),
    timeoutSeconds: configuredTimeout(
      environment.PETLIBRO_STATE_AGENT_TIMEOUT_SECONDS,
    ),
  };
  const journal = new SqliteOperationJournal(
    `${dataDirectory}/management.sqlite`,
  );
  const systemStatus = new SystemStatusService(
    environment.PETLIBRO_MANAGEMENT_VERSION ?? '0.1.0-development',
    worker,
  );
  const runtimeFiles = new RuntimeFileAdapter(runtimeDataDirectory);
  const deviceOverview = new DeviceOverviewService(
    runtimeFiles,
    new MqttBrokerAdapter(),
  );
  const operationEventHistory = new OperationEventHistoryService(journal);
  const setupPreflight = new SetupPreflightService();
  const installations = new InstallationCoordinator(
    journal,
    worker,
    setupPreflight,
    new InstallationSecretStore(dataDirectory),
    environment.PETLIBRO_ENABLE_UNQUALIFIED_BOOTSTRAP === '1',
    undefined,
    undefined,
    250,
    15 * 60_000,
    new InstallationReadinessService(runtimeFiles),
  );
  void installations.ensureInitialized();
  return {
    systemStatus,
    deviceOverview,
    operations: new OperationManager(journal, worker, stateAgentConfiguration),
    operationEventHistory,
    diagnostics: new DiagnosticReportService(
      systemStatus,
      deviceOverview,
      operationEventHistory,
    ),
    setupPreflight,
    installations,
    liveLogs: new LiveLogService(
      new SupervisorLogAdapter(
        environment.SUPERVISOR_TOKEN,
        environment.PETLIBRO_SUPERVISOR_URL ?? 'http://supervisor',
      ),
    ),
  };
}

function configuredToken(environment: NodeJS.ProcessEnv): { token?: string } {
  if (environment.PETLIBRO_STATE_AGENT_TOKEN !== undefined) {
    return { token: environment.PETLIBRO_STATE_AGENT_TOKEN };
  }
  const tokenFile = environment.PETLIBRO_STATE_AGENT_TOKEN_FILE;
  if (tokenFile === undefined || tokenFile === '') {
    return {};
  }
  try {
    const token = readFileSync(tokenFile, 'utf8');
    return token === '' ? {} : { token };
  } catch {
    return {};
  }
}

let applicationServices: ApplicationServices | undefined;

export function getApplicationServices(): ApplicationServices {
  applicationServices ??= createApplicationServices();
  return applicationServices;
}

function configuredTimeout(value: string | undefined): number {
  if (value === undefined) {
    return 5;
  }
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0.1 || parsed > 30) {
    throw new Error(
      'PETLIBRO_STATE_AGENT_TIMEOUT_SECONDS must be between 0.1 and 30.',
    );
  }
  return parsed;
}
