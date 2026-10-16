import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import {
  BookOpenText,
  Download,
  Filter,
  RefreshCw,
  Search,
} from 'lucide-react';
import { useState } from 'react';
import { PageHeader } from '#/components/layout/application-shell';
import { Badge } from '#/components/ui/badge';
import { Button } from '#/components/ui/button';
import { Card, CardContent } from '#/components/ui/card';
import type { OperationEventHistoryRecord } from '#/domain/operation-event-history';
import {
  fetchOperationEventHistory,
  type OperationEventHistoryClientFilter,
  operationEventHistoryUrl,
} from '#/infrastructure/http/operation-event-history-client';
import { LiveLogExplorer } from './live-log-explorer';

export function LogsPage() {
  const [severity, setSeverity] = useState('');
  const [source, setSource] = useState('');
  const [search, setSearch] = useState('');
  const filter: OperationEventHistoryClientFilter = {
    ...(severity === '' ? {} : { severity }),
    ...(source === '' ? {} : { source }),
    ...(search.trim() === '' ? {} : { search: search.trim() }),
  };
  const eventHistory = useQuery({
    queryKey: ['operation-event-history', filter],
    queryFn: ({ signal }) => fetchOperationEventHistory(filter, signal),
    refetchInterval: 5_000,
  });

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="Durable observability"
        title="Events & logs"
        description="Review persistent operation evidence. Raw service logs will use separate, ephemeral on-demand streams rather than this durable journal."
      >
        <Button asChild variant="outline">
          <a href={operationEventHistoryUrl(filter, true)} download>
            <Download aria-hidden="true" /> Export evidence
          </a>
        </Button>
      </PageHeader>

      <LiveLogExplorer />

      <Card className="log-viewer-shell">
        <CardContent>
          <div
            className="log-toolbar"
            role="toolbar"
            aria-label="Operation event filters"
          >
            <label>
              <Search aria-hidden="true" />
              <span className="sr-only">Search operation events</span>
              <input
                type="search"
                value={search}
                maxLength={100}
                placeholder="Search event, field, or operation"
                onChange={(event) => setSearch(event.target.value)}
              />
            </label>
            <label>
              <Filter aria-hidden="true" />
              <span className="sr-only">Filter severity</span>
              <select
                value={severity}
                onChange={(event) => setSeverity(event.target.value)}
              >
                <option value="">All severities</option>
                <option value="info">Info</option>
                <option value="warning">Warning</option>
                <option value="error">Error</option>
              </select>
            </label>
            <label>
              <span className="sr-only">Filter source</span>
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
              >
                <option value="">All sources</option>
                <option value="management-application">Application</option>
                <option value="capability-worker">Capability worker</option>
              </select>
            </label>
            <Button
              size="sm"
              variant="outline"
              onClick={() => void eventHistory.refetch()}
            >
              <RefreshCw aria-hidden="true" /> Refresh
            </Button>
          </div>
          {eventHistory.isError ? (
            <div className="log-empty" role="alert">
              <BookOpenText aria-hidden="true" />
              <strong>Event history query failed</strong>
              <p>{eventHistory.error.message}</p>
            </div>
          ) : eventHistory.data?.records.length ? (
            <ol className="log-records" aria-live="polite">
              {eventHistory.data.records.map((record) => (
                <EventLine key={record.id} record={record} />
              ))}
            </ol>
          ) : (
            <div className="log-empty">
              <BookOpenText aria-hidden="true" />
              <strong>
                {eventHistory.isPending
                  ? 'Loading operation events'
                  : 'No matching records'}
              </strong>
              <p>
                Durable capability and operation evidence appears here after an
                operation is accepted.
              </p>
              <Badge variant="secondary">0 records</Badge>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="truth-callout">
        <BookOpenText aria-hidden="true" />
        <div>
          <strong>Persistent evidence scope</strong>
          <p>
            {eventHistory.data?.scope ??
              'Only durable management operation and capability evidence is queried here.'}{' '}
            Secret-like fields are redacted before export. Query result size is
            bounded without pruning the underlying authoritative evidence.
          </p>
        </div>
      </div>

      <div className="truth-callout">
        <BookOpenText aria-hidden="true" />
        <div>
          <strong>Live raw-log scope</strong>
          <p>
            The initial adapter streams the add-on&apos;s existing combined
            Supervisor output only while a browser subscribes. Source-specific
            AppDaemon and go2rtc adapters remain deferred; this viewer labels
            combined output honestly as addon-runtime rather than guessing its
            producer. Capture history is browser-owned and bounded.
          </p>
        </div>
      </div>
    </div>
  );
}

function EventLine({
  record,
}: {
  readonly record: OperationEventHistoryRecord;
}) {
  return (
    <li data-severity={record.severity}>
      <time dateTime={record.occurredAt}>
        {new Date(record.occurredAt).toLocaleTimeString()}
      </time>
      <Badge variant={record.severity === 'error' ? 'destructive' : 'outline'}>
        {record.severity}
      </Badge>
      <span className="log-source">{record.source}</span>
      <div>
        <strong>{record.message}</strong>
        <small>{record.eventType}</small>
      </div>
      <Button asChild size="sm" variant="ghost">
        <Link to="/operations" search={{ operation: record.operationId }}>
          Operation
        </Link>
      </Button>
    </li>
  );
}
