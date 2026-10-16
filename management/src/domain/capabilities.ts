export const BOOTSTRAP_PREPARE_CAPABILITY_ID =
  'bootstrap.prepare-artifacts.v1' as const;
export const BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID =
  'bootstrap.capture-identity.v1' as const;
export const BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID =
  'bootstrap.verify-final-broker.v1' as const;
export const BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID =
  'bootstrap.execute-ota.v1' as const;
export const BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID =
  'bootstrap.verify-integration.v1' as const;

export type BootstrapCapabilityId =
  | typeof BOOTSTRAP_PREPARE_CAPABILITY_ID
  | typeof BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID
  | typeof BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID
  | typeof BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID
  | typeof BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID;

export const BOOTSTRAP_CAPABILITY_IDS: readonly BootstrapCapabilityId[] = [
  BOOTSTRAP_PREPARE_CAPABILITY_ID,
  BOOTSTRAP_CAPTURE_IDENTITY_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_BROKER_CAPABILITY_ID,
  BOOTSTRAP_EXECUTE_OTA_CAPABILITY_ID,
  BOOTSTRAP_VERIFY_INTEGRATION_CAPABILITY_ID,
];

export interface GenericCapabilityRequest {
  readonly invocationId: string;
  readonly operationId: string;
  readonly capabilityId: BootstrapCapabilityId;
  readonly input: Readonly<Record<string, unknown>>;
}

export interface ProtectedConfigurationHandoff {
  readonly installationId: string;
  readonly feederBrokerAccount: {
    readonly clientId: string;
    readonly username: string;
    readonly password: string;
    readonly publishTopic: string;
    readonly subscribeTopic: string;
  };
  readonly addonOptionsPatch: Readonly<Record<string, unknown>>;
}
