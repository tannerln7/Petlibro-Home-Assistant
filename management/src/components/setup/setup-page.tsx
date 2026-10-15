import { useMutation, useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import {
  Check,
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
import { runSetupPreflight } from '#/infrastructure/http/setup-client';
import { fetchSystemStatus } from '#/infrastructure/http/system-status-client';

const setupStages = [
  ['Preflight', 'Management and local network readiness'],
  ['Target & broker', 'Feeder target and final broker configuration'],
  ['Recovery', 'Protected recovery key and bootstrap artifacts'],
  ['Identity capture', 'Temporary broker and feeder identity evidence'],
  ['Guarded install', 'Credential proof, OTA, and restart'],
  ['Verification', 'Broker handoff and runtime adoption'],
] as const;

export function SetupPage() {
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
  const worker = statusQuery.data?.integrations.find(
    (item) => item.id === 'capability-worker',
  );
  const preflight = useMutation({
    mutationFn: runSetupPreflight,
    onSettled: () =>
      setForm((current) => ({ ...current, backendPassword: '' })),
  });
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    preflight.mutate({
      ...form,
      brokerPort: Number(form.brokerPort),
    });
  };
  const update = (field: keyof typeof form, value: string) =>
    setForm((current) => ({ ...current, [field]: value }));

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Guided installation"
        title="Setup"
        description="Prepare a safe, checkpointed installation without hiding device-changing boundaries or claiming unverified broker handoff behavior."
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
              Conceptual groups; execution ordering remains capability-owned.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ol className="setup-stage-list">
              {setupStages.map(([title, detail], index) => (
                <li key={title} data-state={index === 0 ? 'current' : 'locked'}>
                  <span aria-hidden="true">
                    {index === 0 ? <CircleDashed /> : <LockKeyhole />}
                  </span>
                  <div>
                    <strong>{title}</strong>
                    <small>{detail}</small>
                  </div>
                </li>
              ))}
            </ol>
          </CardContent>
        </Card>

        <div className="setup-primary">
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
                    The broker password is used for this request and is not
                    returned in evidence or retained by this workflow.
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
                        ? 'Ready to begin'
                        : 'Needs attention'}
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
                </div>
              )}
            </CardContent>
          </Card>
          <CapabilityGap
            compact
            icon={Router}
            title="Device-changing stages remain locked"
            description="Native protocol primitives are parity-tested, but artifact custody, temporary MQTT/OTA session ownership, broker handoff checkpoints, and post-install adoption are not yet exposed. Continue to use the independent Python CLI for installation."
          />
          <Card>
            <CardHeader>
              <CardTitle>Foundation readiness</CardTitle>
              <CardDescription>
                These checks do not inspect the feeder or broker.
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
                    <strong>Device operations worker</strong>
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
