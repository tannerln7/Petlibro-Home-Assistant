import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity,
  CheckCircle2,
  CircleAlert,
  Play,
  Radio,
  ServerCog,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { PageHeader } from '#/components/layout/application-shell';
import { OperationList } from '#/components/shared/operation-list';
import {
  EmptyState,
  formatTime,
  humanize,
  SectionHeading,
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
import { Skeleton } from '#/components/ui/skeleton';
import {
  INSTALLATION_ACTION_ID,
  isTerminalOperationStatus,
  type OperationSnapshot,
  operationTitle,
  STATE_AGENT_INSPECTION_ACTION_ID,
} from '#/domain/operations';
import {
  fetchOperationDetail,
  fetchOperations,
  fetchStateAgentInspectionAction,
  operationEventsWebSocketUrl,
  startStateAgentInspection,
} from '#/infrastructure/http/operations-client';

export function OperationsPage() {
  const queryClient = useQueryClient();
  const [selectedOperationId, setSelectedOperationId] = useState<string>();
  const actionQuery = useQuery({
    queryKey: ['actions', 'state-agent-inspection'],
    queryFn: ({ signal }) => fetchStateAgentInspectionAction(signal),
  });
  const operationsQuery = useQuery({
    queryKey: ['operations'],
    queryFn: ({ signal }) => fetchOperations(signal),
    refetchInterval: 2_000,
  });
  const startMutation = useMutation({
    mutationFn: startStateAgentInspection,
    onSuccess: async (operation) => {
      setSelectedOperationId(operation.operationId);
      await queryClient.invalidateQueries({ queryKey: ['operations'] });
      await queryClient.invalidateQueries({
        queryKey: ['operation', operation.operationId],
      });
    },
  });

  const selectedOperation =
    operationsQuery.data?.find(
      (operation) => operation.operationId === selectedOperationId,
    ) ?? operationsQuery.data?.[0];

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Durable work"
        title="Operations"
        description="Capability work continues independently of this page. Snapshots and evidence are retained by the management service."
      >
        <Button
          onClick={() => startMutation.mutate()}
          disabled={
            actionQuery.data?.available !== true || startMutation.isPending
          }
        >
          <Play aria-hidden="true" />
          {startMutation.isPending ? 'Accepting…' : 'Run inspection'}
        </Button>
      </PageHeader>

      <ActionAvailabilityCard
        pending={actionQuery.isPending}
        error={actionQuery.error}
        action={actionQuery.data}
        mutationError={startMutation.error}
      />

      {operationsQuery.isPending ? (
        <Skeleton className="h-72 rounded-xl" />
      ) : operationsQuery.isError ? (
        <Card className="status-error" role="alert">
          <CardHeader>
            <CardTitle>Operation journal unavailable</CardTitle>
            <CardDescription>{operationsQuery.error.message}</CardDescription>
          </CardHeader>
        </Card>
      ) : selectedOperation === undefined ? (
        <EmptyOperations />
      ) : (
        <>
          <section aria-labelledby="operation-journal-title">
            <SectionHeading
              eyebrow="Operation journal"
              title="Recent work"
              aside={<span className="section-note">Most recent 20</span>}
            />
            <Card id="operation-journal-title" className="operation-list-card">
              <CardContent>
                <OperationList
                  operations={operationsQuery.data}
                  selectedId={selectedOperation.operationId}
                  onSelect={setSelectedOperationId}
                />
              </CardContent>
            </Card>
          </section>
          <OperationView operation={selectedOperation} />
        </>
      )}
    </div>
  );
}

function ActionAvailabilityCard({
  pending,
  error,
  action,
  mutationError,
}: {
  readonly pending: boolean;
  readonly error: Error | null;
  readonly action:
    | Awaited<ReturnType<typeof fetchStateAgentInspectionAction>>
    | undefined;
  readonly mutationError: Error | null;
}) {
  return (
    <Card className="operation-action-card">
      <CardHeader>
        <div className="card-heading-row">
          <span className="card-icon" aria-hidden="true">
            <ServerCog />
          </span>
          <div>
            <CardTitle>Inspect State Agent health</CardTitle>
            <CardDescription>
              Authenticated read-only capability · no feeder writes
            </CardDescription>
          </div>
          {pending ? (
            <Badge variant="outline">Checking availability</Badge>
          ) : action?.available === true ? (
            <Badge className="status-ready">Available</Badge>
          ) : (
            <Badge variant="secondary">Unavailable</Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        <p className="guardrail-copy">
          {error?.message ??
            action?.unavailableReason ??
            action?.description ??
            'Checking the configured worker and capability boundary.'}
        </p>
        {mutationError === null ? null : (
          <p className="operation-inline-error" role="alert">
            {mutationError.message}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function EmptyOperations() {
  return (
    <Card className="empty-operation-card">
      <CardContent>
        <EmptyState
          title="No recorded operations"
          description="Configure the worker and State Agent server settings to exercise the first read-only capability path."
        />
      </CardContent>
    </Card>
  );
}

function OperationView({
  operation,
}: {
  readonly operation: OperationSnapshot;
}) {
  const detailQuery = useQuery({
    queryKey: ['operation', operation.operationId],
    queryFn: ({ signal }) =>
      fetchOperationDetail(operation.operationId, signal),
    refetchInterval: isTerminalOperationStatus(operation.status)
      ? false
      : 2_000,
  });
  useOperationEvents(
    detailQuery.data?.operation.operationId,
    detailQuery.data?.operation.status,
    detailQuery.data?.nextEventCursor,
  );

  const detail = detailQuery.data;
  const current = detail?.operation ?? operation;

  return (
    <section
      className="operation-layout"
      aria-labelledby="latest-operation-title"
    >
      <Card className="operation-summary-card">
        <CardHeader>
          <div className="card-heading-row">
            <span className="card-icon" aria-hidden="true">
              <Activity />
            </span>
            <div>
              <CardTitle id="latest-operation-title">
                {operationTitle(current)}
              </CardTitle>
              <CardDescription>{current.target.label}</CardDescription>
            </div>
            <StatusBadge status={current.status} />
          </div>
        </CardHeader>
        <CardContent>
          <dl className="operation-metadata">
            <div>
              <dt>Stage</dt>
              <dd>{humanize(current.stage)}</dd>
            </div>
            <div>
              <dt>Updated</dt>
              <dd>{formatTime(current.updatedAt)}</dd>
            </div>
            <div>
              <dt>Operation</dt>
              <dd className="monospace-value">{current.operationId}</dd>
            </div>
          </dl>

          {current.actionId !== STATE_AGENT_INSPECTION_ACTION_ID ||
          current.result === undefined ? null : (
            <div className="operation-evidence">
              <div>
                {current.result.healthy ? (
                  <CheckCircle2 aria-hidden="true" />
                ) : (
                  <CircleAlert aria-hidden="true" />
                )}
                <strong>
                  State Agent reported{' '}
                  {current.result.healthy ? 'healthy' : 'degraded'}
                </strong>
              </div>
              <p>
                {current.result.agent.name} {current.result.agent.version} ·
                state snapshot {current.result.stateDecode.actualBytes}/
                {current.result.stateDecode.expectedBytes} bytes
              </p>
              <small>
                Observed by the capability worker from the authenticated State
                Agent response. This is not feeder connectivity or AppDaemon
                readiness.
              </small>
            </div>
          )}

          {current.actionId !== INSTALLATION_ACTION_ID ? null : (
            <div className="operation-evidence">
              <div>
                <Activity aria-hidden="true" />
                <strong>Durable installation workflow</strong>
              </div>
              <p>
                {
                  current.stages.filter((stage) => stage.status === 'completed')
                    .length
                }
                /{current.stages.length} stages completed
              </p>
              {current.checkpoint === undefined ? null : (
                <small>
                  Awaiting operator: {current.checkpoint.title}. Confirmation
                  does not replace prerequisite verification.
                </small>
              )}
            </div>
          )}

          {current.failure === undefined ? null : (
            <output className="operation-failure">
              <CircleAlert aria-hidden="true" />
              <div>
                <strong>{humanize(current.failure.code)}</strong>
                <p>{current.failure.message}</p>
              </div>
            </output>
          )}
        </CardContent>
      </Card>

      <Card className="operation-events-card">
        <CardHeader>
          <div className="card-heading-row">
            <span className="card-icon" aria-hidden="true">
              <Radio />
            </span>
            <div>
              <CardTitle>Execution evidence</CardTitle>
              <CardDescription>
                Durable journal events; live notifications reconnect by cursor.
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          {detailQuery.isError ? (
            <p className="operation-inline-error">
              {detailQuery.error.message}
            </p>
          ) : (
            <ol className="operation-event-list">
              {(detail?.events ?? []).map((event) => (
                <li key={event.sequence} data-severity={event.severity}>
                  <span aria-hidden="true" />
                  <div>
                    <strong>{humanize(event.type)}</strong>
                    <small>
                      {formatTime(event.occurredAt)} · {event.source}
                    </small>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
    </section>
  );
}

function useOperationEvents(
  operationId: string | undefined,
  status: OperationSnapshot['status'] | undefined,
  nextEventCursor: number | undefined,
) {
  const queryClient = useQueryClient();
  const cursor = useRef(0);
  const cursorOperationId = useRef<string | undefined>(undefined);
  const cursorReady = nextEventCursor !== undefined;

  useEffect(() => {
    if (operationId === undefined || nextEventCursor === undefined) {
      return;
    }
    if (cursorOperationId.current !== operationId) {
      cursorOperationId.current = operationId;
      cursor.current = nextEventCursor;
      return;
    }
    cursor.current = Math.max(cursor.current, nextEventCursor);
  }, [nextEventCursor, operationId]);

  useEffect(() => {
    if (
      operationId === undefined ||
      status === undefined ||
      !cursorReady ||
      isTerminalOperationStatus(status)
    ) {
      return;
    }
    let disposed = false;
    let reconnect: ReturnType<typeof setTimeout> | undefined;
    let socket: WebSocket | undefined;

    const connect = () => {
      if (disposed) {
        return;
      }
      socket = new WebSocket(
        operationEventsWebSocketUrl(operationId, cursor.current),
      );
      socket.addEventListener('message', (message) => {
        try {
          const value = JSON.parse(String(message.data)) as {
            cursor?: unknown;
          };
          if (Number.isSafeInteger(value.cursor)) {
            cursor.current = Math.max(cursor.current, value.cursor as number);
          }
        } catch {
          // The HTTP snapshot remains the recovery source for malformed notices.
        }
        void queryClient.invalidateQueries({
          queryKey: ['operation', operationId],
        });
        void queryClient.invalidateQueries({ queryKey: ['operations'] });
      });
      socket.addEventListener('close', () => {
        if (!disposed) {
          reconnect = setTimeout(connect, 1_000);
        }
      });
    };

    connect();
    return () => {
      disposed = true;
      if (reconnect !== undefined) {
        clearTimeout(reconnect);
      }
      socket?.close();
    };
  }, [cursorReady, operationId, queryClient, status]);
}
