import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { CircleHelp, Download, ServerCog, Stethoscope } from 'lucide-react';
import { PageHeader } from '#/components/layout/application-shell';
import {
  AvailabilityBadge,
  ObservationNote,
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
import { INSTALLATION_ACTION_ID } from '#/domain/operations';
import { fetchDeviceOverview } from '#/infrastructure/http/device-overview-client';
import {
  fetchOperations,
  fetchStateAgentInspectionAction,
} from '#/infrastructure/http/operations-client';

export function DiagnosticsPage() {
  const statusQuery = useQuery({
    queryKey: ['device-overview'],
    queryFn: ({ signal }) => fetchDeviceOverview(signal),
    refetchInterval: 30_000,
  });
  const actionQuery = useQuery({
    queryKey: ['actions', 'state-agent-inspection'],
    queryFn: ({ signal }) => fetchStateAgentInspectionAction(signal),
  });
  const operationsQuery = useQuery({
    queryKey: ['operations'],
    queryFn: ({ signal }) => fetchOperations(signal),
    refetchInterval: 5_000,
  });
  const installation = operationsQuery.data?.find(
    (operation) => operation.actionId === INSTALLATION_ACTION_ID,
  );
  const firstService = statusQuery.data?.services[0];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Source-aware checks"
        title="Diagnostics"
        description="Separate observed facts, query failures, and diagnostic interpretation for each subsystem."
      >
        <Button asChild variant="outline">
          <a
            href={`${import.meta.env.BASE_URL}api/v1/diagnostics/report?download=1`}
            download
          >
            <Download aria-hidden="true" /> Export report
          </a>
        </Button>
        {actionQuery.data?.available ? (
          <Button asChild>
            <Link to="/operations">
              <Stethoscope aria-hidden="true" /> Run available check
            </Link>
          </Button>
        ) : (
          <Button disabled>
            <Stethoscope aria-hidden="true" /> Run available check
          </Button>
        )}
      </PageHeader>

      <div className="diagnostics-layout">
        <Card className="diagnostic-sources-card">
          <CardHeader>
            <CardTitle>Subsystem sources</CardTitle>
            <CardDescription>
              Adapter availability, not a fused health score.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {statusQuery.isError ? (
              <p className="operation-inline-error" role="alert">
                {statusQuery.error.message}
              </p>
            ) : (
              <ul className="diagnostic-source-list">
                {(statusQuery.data?.services ?? []).map((integration) => (
                  <li key={integration.id}>
                    <span>
                      <strong>{integration.name}</strong>
                      <small>{integration.detail}</small>
                    </span>
                    <AvailabilityBadge
                      availability={integration.availability}
                    />
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card className="diagnostic-detail-card">
          <CardHeader>
            <div className="card-heading-row">
              <span className="card-icon" aria-hidden="true">
                <ServerCog />
              </span>
              <div>
                <CardTitle>State Agent inspection</CardTitle>
                <CardDescription>
                  Authenticated, read-only health check
                </CardDescription>
              </div>
              {actionQuery.data?.available ? (
                <Badge className="status-ready">Runnable</Badge>
              ) : (
                <Badge variant="secondary">Unavailable</Badge>
              )}
            </div>
          </CardHeader>
          <CardContent>
            <p className="card-copy">
              {actionQuery.error?.message ??
                actionQuery.data?.unavailableReason ??
                actionQuery.data?.description ??
                'Checking the worker capability boundary.'}
            </p>
            <div className="interpretation-box">
              <CircleHelp aria-hidden="true" />
              <div>
                <strong>Scope limitation</strong>
                <p>
                  A successful result confirms the authenticated State Agent
                  response and state decoding only. It does not establish
                  broker, feeder connectivity, or controller readiness.
                </p>
              </div>
            </div>
          </CardContent>
        </Card>
      </div>

      {installation === undefined ? null : (
        <Card>
          <CardHeader>
            <div className="card-heading-row">
              <div>
                <CardTitle>Latest installation evidence</CardTitle>
                <CardDescription>
                  {installation.operationId} · {installation.stage}
                </CardDescription>
              </div>
              <StatusBadge status={installation.status} />
            </div>
          </CardHeader>
          <CardContent>
            <p className="card-copy">
              {installation.failure?.message ??
                installation.checkpoint?.instructions ??
                installation.evidence.at(-1)?.summary ??
                'The installation has no durable evidence yet.'}
            </p>
            <div className="action-card-footer">
              <span>
                {installation.status === 'indeterminate'
                  ? 'Automatic device-changing retry is blocked; inspect evidence before recovery.'
                  : 'Raw logs are supplemental; durable transaction evidence remains in the operation journal.'}
              </span>
              <Button asChild variant="outline">
                <Link to="/operations">Open operation</Link>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {statusQuery.data === undefined || firstService === undefined ? null : (
        <Card>
          <CardHeader>
            <CardTitle>Runtime observation contract</CardTitle>
            <CardDescription>
              Source, freshness, verification level, and scope remain visible.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ObservationNote
              source={firstService.observation.source}
              observedAt={
                firstService.observation.observedAt ??
                firstService.observation.collectedAt
              }
              scope={firstService.observation.scope}
            />
            {statusQuery.data.warnings.length === 0 ? null : (
              <ul className="diagnostic-warning-list">
                {statusQuery.data.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
