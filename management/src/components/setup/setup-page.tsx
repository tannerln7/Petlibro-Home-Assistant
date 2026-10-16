import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import {
  Check,
  CircleAlert,
  CircleDashed,
  KeyRound,
  LockKeyhole,
  Router,
} from 'lucide-react';
import { type FormEvent, useState } from 'react';
import { PageHeader } from '#/components/layout/application-shell';
import {
  AvailabilityBadge,
  CapabilityGap,
  humanize,
  StatusBadge,
} from '#/components/shared/presentation';
import { Badge } from '#/components/ui/badge';
import { Button } from '#/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '#/components/ui/card';
import {
  INSTALLATION_ACTION_ID,
  type InstallationOperationSnapshot,
} from '#/domain/operations';
import { fetchOperations } from '#/infrastructure/http/operations-client';
import {
  downloadProtectedConfiguration,
  fetchInstallationAvailability,
  runSetupPreflight,
  satisfyInstallationCheckpoint,
  startInstallation,
} from '#/infrastructure/http/setup-client';
import { fetchSystemStatus } from '#/infrastructure/http/system-status-client';

const setupStages = [
  ['Preflight', 'Management and local network readiness'],
  ['Artifacts', 'Protected recovery material and payload integrity'],
  ['Identity capture', 'Temporary broker and feeder identity evidence'],
  ['Broker proof', 'Backend and captured feeder credential proof'],
  ['Guarded install', 'Correlated OTA, payload transfer, and restart'],
  ['Verification', 'Broker restoration and runtime adoption'],
] as const;

export function SetupPage() {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({
    feederIp: '',
    setupHostIp: '',
    homeAssistantIp: '',
    brokerHost: '',
    brokerPort: '1883',
    backendUsername: '',
    backendPassword: '',
    sshPublicKey: '',
  });
  const statusQuery = useQuery({
    queryKey: ['system-status'],
    queryFn: ({ signal }) => fetchSystemStatus(signal),
  });
  const availability = useQuery({
    queryKey: ['installation-availability'],
    queryFn: ({ signal }) => fetchInstallationAvailability(signal),
    refetchInterval: 2_000,
  });
  const operations = useQuery({
    queryKey: ['operations'],
    queryFn: ({ signal }) => fetchOperations(signal),
    refetchInterval: 2_000,
  });
  const currentInstallation =
    availability.data?.installation ??
    operations.data?.find(
      (operation): operation is InstallationOperationSnapshot =>
        operation.actionId === INSTALLATION_ACTION_ID,
    );
  const worker = statusQuery.data?.integrations.find(
    (item) => item.id === 'capability-worker',
  );
  const preflight = useMutation({ mutationFn: runSetupPreflight });
  const installation = useMutation({
    mutationFn: startInstallation,
    onSuccess: async () => {
      setForm((current) => ({ ...current, backendPassword: '' }));
      await queryClient.invalidateQueries({ queryKey: ['operations'] });
      await queryClient.invalidateQueries({
        queryKey: ['installation-availability'],
      });
    },
  });
  const checkpoint = useMutation({
    mutationFn: satisfyInstallationCheckpoint,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['operations'] });
      await queryClient.invalidateQueries({
        queryKey: ['installation-availability'],
      });
    },
  });
  const protectedConfiguration = useMutation({
    mutationFn: downloadProtectedConfiguration,
  });
  const request = () => ({ ...form, brokerPort: Number(form.brokerPort) });
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    preflight.mutate(request());
  };
  const update = (field: keyof typeof form, value: string) => {
    // A successful preflight is evidence about one exact request. Editing any
    // field invalidates that presentation even though the server also reruns
    // preflight before accepting an installation.
    preflight.reset();
    installation.reset();
    setForm((current) => ({ ...current, [field]: value }));
  };
  const canStart =
    availability.data?.available === true &&
    preflight.data !== undefined &&
    preflight.data.checks.every(
      (check) =>
        check.status === 'passed' ||
        check.status === 'warning' ||
        (check.id === 'temporary-mqtt-port' && check.status === 'blocked'),
    );

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Guided installation"
        title="Setup"
        description="Run one durable installation through verified technical stages and explicit broker-handoff checkpoints."
      >
        <Button asChild variant="outline">
          <Link to="/operations">View operations</Link>
        </Button>
      </PageHeader>

      <div className="setup-layout">
        <Card className="setup-stage-card">
          <CardHeader>
            <CardTitle>Installation stages</CardTitle>
            <CardDescription>
              Progress is restored from the durable operation journal.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ol className="setup-stage-list">
              {(currentInstallation?.stages ?? fallbackStages()).map(
                (stage) => (
                  <li key={stage.id} data-state={stageState(stage.status)}>
                    <span aria-hidden="true">{stageIcon(stage.status)}</span>
                    <div>
                      <strong>{stage.title}</strong>
                      <small>{stage.detail}</small>
                    </div>
                  </li>
                ),
              )}
            </ol>
          </CardContent>
        </Card>

        <div className="setup-primary">
          {currentInstallation === undefined ? null : (
            <InstallationProgressCard
              operation={currentInstallation}
              pending={checkpoint.isPending}
              error={checkpoint.error}
              handoffPending={protectedConfiguration.isPending}
              handoffError={protectedConfiguration.error}
              onDownloadHandoff={() =>
                protectedConfiguration.mutate(currentInstallation.operationId)
              }
              onContinue={(checkpointId) =>
                checkpoint.mutate({
                  operationId: currentInstallation.operationId,
                  checkpointId,
                })
              }
            />
          )}

          <Card>
            <CardHeader>
              <CardTitle>Installation preflight</CardTitle>
              <CardDescription>
                Validate recovery input, final-broker authentication, and
                current TCP/1883 ownership without changing the feeder.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form className="setup-preflight-form" onSubmit={submit}>
                <div className="setup-field-grid">
                  <SetupField
                    label="Feeder IPv4"
                    value={form.feederIp}
                    onChange={(value) => update('feederIp', value)}
                  />
                  <SetupField
                    label="Setup host IPv4"
                    value={form.setupHostIp}
                    onChange={(value) => update('setupHostIp', value)}
                  />
                  <SetupField
                    label="Home Assistant IPv4"
                    value={form.homeAssistantIp}
                    onChange={(value) => update('homeAssistantIp', value)}
                  />
                  <SetupField
                    label="Final broker IPv4"
                    value={form.brokerHost}
                    onChange={(value) => update('brokerHost', value)}
                  />
                  <SetupField
                    label="Final broker port"
                    value={form.brokerPort}
                    inputMode="numeric"
                    onChange={(value) => update('brokerPort', value)}
                  />
                  <SetupField
                    label="Backend broker username"
                    value={form.backendUsername}
                    autoComplete="username"
                    onChange={(value) => update('backendUsername', value)}
                  />
                  <SetupField
                    label="Backend broker password"
                    value={form.backendPassword}
                    type="password"
                    autoComplete="new-password"
                    onChange={(value) => update('backendPassword', value)}
                  />
                </div>
                <label className="setup-wide-field">
                  <span>SSH recovery public key</span>
                  <textarea
                    required
                    rows={3}
                    value={form.sshPublicKey}
                    placeholder="ssh-ed25519 AAAA… plaf203-recovery"
                    onChange={(event) =>
                      update('sshPublicKey', event.target.value)
                    }
                  />
                </label>
                <div className="setup-form-actions">
                  <Button type="submit" disabled={preflight.isPending}>
                    <Router aria-hidden="true" />
                    {preflight.isPending ? 'Checking…' : 'Run safe preflight'}
                  </Button>
                  <small>
                    Preflight does not retain the broker password. Starting a
                    workflow stores it in protected server custody. Secrets are
                    returned only through an explicit no-store configuration
                    download and never enter operation events or diagnostics.
                  </small>
                </div>
              </form>
              {preflight.isError ? (
                <p className="operation-inline-error" role="alert">
                  {preflight.error.message}
                </p>
              ) : null}
              {preflight.data === undefined ? null : (
                <div className="setup-preflight-result" aria-live="polite">
                  <div className="card-heading-row">
                    <strong>Preflight result</strong>
                    <Badge
                      className={
                        preflight.data.readyForBootstrapSession
                          ? 'status-ready'
                          : 'status-warning'
                      }
                    >
                      {preflight.data.readyForBootstrapSession
                        ? 'Listener currently available'
                        : 'Checkpoint required'}
                    </Badge>
                  </div>
                  <ul className="check-list">
                    {preflight.data.checks.map((check) => (
                      <li key={check.id}>
                        <Check aria-hidden="true" />
                        <span>
                          <strong>{check.title}</strong>
                          <small>{check.detail}</small>
                        </span>
                        <Badge
                          className={
                            check.status === 'passed'
                              ? 'status-ready'
                              : 'status-warning'
                          }
                        >
                          {check.status}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                  <div className="setup-form-actions">
                    <Button
                      type="button"
                      disabled={!canStart || installation.isPending}
                      onClick={() => installation.mutate(request())}
                    >
                      <Router aria-hidden="true" />
                      {installation.isPending
                        ? 'Starting…'
                        : 'Start durable installation'}
                    </Button>
                    <small>
                      The server reruns preflight and binds the result to these
                      exact non-secret inputs before accepting the operation.
                    </small>
                  </div>
                  {installation.isError ? (
                    <p className="operation-inline-error" role="alert">
                      {installation.error.message}
                    </p>
                  ) : null}
                </div>
              )}
            </CardContent>
          </Card>

          {availability.data?.available === true ? null : (
            <CapabilityGap
              compact
              icon={Router}
              title="Device-changing execution is qualification-gated"
              description={
                availability.data?.reason ??
                'Installation availability has not been confirmed.'
              }
            />
          )}

          <Card>
            <CardHeader>
              <CardTitle>Runtime readiness</CardTitle>
              <CardDescription>
                Process health is not feeder or broker readiness.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ul className="check-list">
                <li>
                  <Check aria-hidden="true" />
                  <span>
                    <strong>Management API</strong>
                    <small>
                      {statusQuery.isSuccess
                        ? 'Available in this browser session'
                        : 'Status not confirmed'}
                    </small>
                  </span>
                  <Badge
                    className={
                      statusQuery.isSuccess ? 'status-ready' : 'status-warning'
                    }
                  >
                    {statusQuery.isSuccess ? 'Ready' : 'Unknown'}
                  </Badge>
                </li>
                <li>
                  <KeyRound aria-hidden="true" />
                  <span>
                    <strong>Capability worker</strong>
                    <small>
                      {worker?.detail ?? 'Worker status has not been observed.'}
                    </small>
                  </span>
                  {worker === undefined ? (
                    <Badge variant="secondary">Unknown</Badge>
                  ) : (
                    <AvailabilityBadge availability={worker.availability} />
                  )}
                </li>
              </ul>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

function InstallationProgressCard({
  operation,
  pending,
  error,
  handoffPending,
  handoffError,
  onContinue,
  onDownloadHandoff,
}: {
  readonly operation: InstallationOperationSnapshot;
  readonly pending: boolean;
  readonly error: Error | null;
  readonly handoffPending: boolean;
  readonly handoffError: Error | null;
  readonly onContinue: (checkpointId: string) => void;
  readonly onDownloadHandoff: () => void;
}) {
  const handoffAvailable =
    operation.checkpoint?.kind === 'start-final-broker' ||
    operation.checkpoint?.kind === 'apply-addon-options';
  return (
    <Card className="operation-action-card">
      <CardHeader>
        <div className="card-heading-row">
          <span className="card-icon" aria-hidden="true">
            <Router />
          </span>
          <div>
            <CardTitle>
              Installation {operation.operationId.slice(0, 8)}
            </CardTitle>
            <CardDescription>
              {operation.target.label} · {humanize(operation.stage)}
            </CardDescription>
          </div>
          <StatusBadge status={operation.status} />
        </div>
      </CardHeader>
      <CardContent>
        {operation.checkpoint === undefined ? (
          <p className="guardrail-copy">
            The backend owns the active stage. You can leave this page without
            cancelling it.
          </p>
        ) : (
          <div className="operation-evidence">
            <div>
              <CircleAlert aria-hidden="true" />
              <strong>{operation.checkpoint.title}</strong>
            </div>
            <p>{operation.checkpoint.instructions}</p>
            <small>{operation.checkpoint.consequence}</small>
            <div className="setup-form-actions">
              {handoffAvailable ? (
                <Button
                  variant="outline"
                  disabled={handoffPending}
                  onClick={onDownloadHandoff}
                >
                  <LockKeyhole aria-hidden="true" />
                  {handoffPending
                    ? 'Preparing private file…'
                    : 'Download protected configuration'}
                </Button>
              ) : null}
              <Button
                disabled={pending}
                onClick={() =>
                  onContinue(operation.checkpoint?.checkpointId as string)
                }
              >
                {pending
                  ? 'Verifying…'
                  : 'I completed this step — verify and continue'}
              </Button>
              <Button asChild variant="outline">
                <Link to="/operations">Execution evidence</Link>
              </Button>
            </div>
          </div>
        )}
        {operation.failure === undefined ? null : (
          <p className="operation-inline-error" role="alert">
            {operation.failure.message}
          </p>
        )}
        {error === null ? null : (
          <p className="operation-inline-error" role="alert">
            {error.message}
          </p>
        )}
        {handoffError === null ? null : (
          <p className="operation-inline-error" role="alert">
            {handoffError.message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function fallbackStages() {
  return setupStages.map(([title, detail], index) => ({
    id: title,
    title,
    detail,
    status: index === 0 ? ('active' as const) : ('pending' as const),
  }));
}

function stageState(status: string): string {
  if (status === 'active' || status === 'awaiting-user') return 'current';
  return status === 'pending' ? 'locked' : status;
}

function stageIcon(status: string) {
  if (status === 'completed') return <Check />;
  if (status === 'active' || status === 'awaiting-user')
    return <CircleDashed />;
  if (status === 'failed' || status === 'indeterminate') return <CircleAlert />;
  return <LockKeyhole />;
}

function SetupField({
  label,
  value,
  onChange,
  type = 'text',
  inputMode,
  autoComplete,
}: {
  readonly label: string;
  readonly value: string;
  readonly onChange: (value: string) => void;
  readonly type?: 'text' | 'password';
  readonly inputMode?: 'numeric';
  readonly autoComplete?: string;
}) {
  return (
    <label>
      <span>{label}</span>
      <input
        required
        type={type}
        inputMode={inputMode}
        autoComplete={autoComplete}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}
