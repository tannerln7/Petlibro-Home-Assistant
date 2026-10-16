import { Download, Pause, Play, Radio, Search } from 'lucide-react';
import { useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { Badge } from '#/components/ui/badge';
import { Button } from '#/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '#/components/ui/card';
import {
  type LiveLogRecord,
  type LiveLogSeverity,
  type LiveLogSourceStatus,
  parseLiveLogRecord,
  severityPermits,
} from '#/domain/live-logs';
import { liveLogWebSocketUrl } from '#/infrastructure/http/live-log-client';

const MAX_RENDERED_RECORDS = 500;
const MAX_REGEX_RECORD_CHARACTERS = 4_096;

export function LiveLogExplorer() {
  const [enabled, setEnabled] = useState(false);
  const [transportLevel, setTransportLevel] =
    useState<LiveLogSeverity>('debug');
  const [filterLevel, setFilterLevel] = useState<LiveLogSeverity>('debug');
  const [search, setSearch] = useState('');
  const [regularExpression, setRegularExpression] = useState(false);
  const [timeframeMinutes, setTimeframeMinutes] = useState(0);
  const [retention, setRetention] = useState(2_000);
  const [records, setRecords] = useState<readonly LiveLogRecord[]>([]);
  const [status, setStatus] = useState<LiveLogSourceStatus>();
  const [transportDropped, setTransportDropped] = useState(0);
  const [retentionDropped, setRetentionDropped] = useState(0);
  const [pausedRecords, setPausedRecords] =
    useState<readonly LiveLogRecord[]>();
  const deferredSearch = useDeferredValue(search);
  const retentionRef = useRef(retention);
  const recordsRef = useRef<readonly LiveLogRecord[]>([]);
  const followRef = useRef<HTMLOListElement>(null);

  useEffect(() => {
    if (!enabled) return;
    const socket = new WebSocket(liveLogWebSocketUrl(transportLevel));
    socket.addEventListener('message', (message) => {
      try {
        const value = JSON.parse(String(message.data)) as Record<
          string,
          unknown
        >;
        if (value.type === 'logs.status' && Array.isArray(value.sources)) {
          const source = value.sources[0];
          if (isSourceStatus(source)) setStatus(source);
          return;
        }
        if (value.type !== 'logs.records' || !Array.isArray(value.records))
          return;
        const incoming = value.records.map(parseLiveLogRecord);
        const dropped = Number(value.dropped ?? 0);
        if (Number.isSafeInteger(dropped) && dropped > 0) {
          setTransportDropped((current) => current + dropped);
        }
        const merged = [...recordsRef.current, ...incoming].sort(
          compareRecords,
        );
        const excess = Math.max(0, merged.length - retentionRef.current);
        const retained = excess === 0 ? merged : merged.slice(excess);
        recordsRef.current = retained;
        setRecords(retained);
        if (excess > 0) {
          setRetentionDropped((count) => count + excess);
        }
      } catch {
        setStatus({
          source: 'addon-runtime',
          availability: 'error',
          detail: 'The browser rejected a malformed log transport record.',
        });
      }
    });
    socket.addEventListener('close', () => {
      setStatus((current) =>
        current?.availability === 'error'
          ? current
          : {
              source: 'addon-runtime',
              availability: 'unavailable',
              detail: 'The live log WebSocket is disconnected.',
            },
      );
    });
    return () => socket.close();
  }, [enabled, transportLevel]);

  useEffect(() => {
    retentionRef.current = retention;
    const current = recordsRef.current;
    if (current.length > retention) {
      const excess = current.length - retention;
      const retained = current.slice(excess);
      recordsRef.current = retained;
      setRecords(retained);
      setRetentionDropped((count) => count + excess);
    }
    setPausedRecords((current) =>
      current === undefined || current.length <= retention
        ? current
        : current.slice(current.length - retention),
    );
  }, [retention]);

  const { visible, regexError } = useMemo(() => {
    let expression: RegExp | undefined;
    let expressionError: string | undefined;
    if (regularExpression && deferredSearch !== '') {
      const compiled = compileSafeLogRegex(deferredSearch);
      expression = compiled.expression;
      expressionError = compiled.error;
    }
    const filtered = (pausedRecords ?? records).filter((record) => {
      if (
        timeframeMinutes > 0 &&
        Date.parse(record.sourceTimestamp ?? record.receivedAt) <
          Date.now() - timeframeMinutes * 60_000
      ) {
        return false;
      }
      if (!severityPermits(record.severity, filterLevel)) return false;
      if (deferredSearch === '') return true;
      if (regularExpression && expressionError !== undefined) return false;
      return expression === undefined
        ? record.message
            .toLocaleLowerCase()
            .includes(deferredSearch.toLocaleLowerCase())
        : expression.test(record.message.slice(0, MAX_REGEX_RECORD_CHARACTERS));
    });
    return { visible: filtered, regexError: expressionError };
  }, [
    filterLevel,
    pausedRecords,
    records,
    regularExpression,
    deferredSearch,
    timeframeMinutes,
  ]);
  const rendered = visible.slice(-MAX_RENDERED_RECORDS);

  useEffect(() => {
    if (enabled && pausedRecords === undefined && visible.length > 0) {
      followRef.current?.lastElementChild?.scrollIntoView?.({
        block: 'nearest',
      });
    }
  }, [enabled, pausedRecords, visible.length]);

  const togglePause = () =>
    setPausedRecords((current) =>
      current === undefined ? [...records] : undefined,
    );
  const exportVisible = () => {
    const text = visible.map(formatExportLine).join('\n');
    const url = URL.createObjectURL(
      new Blob([`${text}\n`], { type: 'text/plain' }),
    );
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `petlibro-live-logs-${new Date().toISOString()}.txt`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Card className="log-viewer-shell">
      <CardHeader>
        <div className="card-heading-row">
          <span className="card-icon" aria-hidden="true">
            <Radio />
          </span>
          <div>
            <CardTitle>Live add-on runtime</CardTitle>
            <p className="card-copy">
              Ephemeral Supervisor output is collected only while this viewer is
              active and retained only in this browser session.
            </p>
          </div>
          <Badge
            className={
              status?.availability === 'streaming'
                ? 'status-ready'
                : 'status-warning'
            }
          >
            {enabled ? (status?.availability ?? 'connecting') : 'stopped'}
          </Badge>
        </div>
      </CardHeader>
      <CardContent>
        <div
          className="log-toolbar"
          role="toolbar"
          aria-label="Live log controls"
        >
          <Button
            size="sm"
            variant={enabled ? 'destructive' : 'default'}
            onClick={() => setEnabled((value) => !value)}
          >
            <Radio aria-hidden="true" />{' '}
            {enabled ? 'Stop capture' : 'Start capture'}
          </Button>
          <label>
            <span className="sr-only">Transport verbosity</span>
            <select
              value={transportLevel}
              onChange={(event) =>
                setTransportLevel(event.target.value as LiveLogSeverity)
              }
            >
              <option value="debug">Through DEBUG</option>
              <option value="trace">Through TRACE</option>
              <option value="info">INFO and above</option>
            </select>
          </label>
          <label>
            <span className="sr-only">Display severity</span>
            <select
              value={filterLevel}
              onChange={(event) =>
                setFilterLevel(event.target.value as LiveLogSeverity)
              }
            >
              <option value="trace">Show TRACE+</option>
              <option value="debug">Show DEBUG+</option>
              <option value="info">Show INFO+</option>
              <option value="warning">Show WARNING+</option>
              <option value="error">Show ERROR</option>
            </select>
          </label>
          <label>
            <Search aria-hidden="true" />
            <span className="sr-only">Filter live logs</span>
            <input
              type="search"
              maxLength={200}
              value={search}
              placeholder="Filter captured lines"
              onChange={(event) => setSearch(event.target.value)}
            />
          </label>
          <label className="log-checkbox">
            <input
              type="checkbox"
              checked={regularExpression}
              onChange={(event) => setRegularExpression(event.target.checked)}
            />{' '}
            Regex
          </label>
          <label>
            <span className="sr-only">Time range</span>
            <select
              value={timeframeMinutes}
              onChange={(event) =>
                setTimeframeMinutes(Number(event.target.value))
              }
            >
              <option value="0">All captured time</option>
              <option value="5">Last 5 minutes</option>
              <option value="15">Last 15 minutes</option>
              <option value="60">Last hour</option>
            </select>
          </label>
          <label>
            <span className="sr-only">Retained line count</span>
            <select
              value={retention}
              onChange={(event) => setRetention(Number(event.target.value))}
            >
              <option value="1000">1,000 lines</option>
              <option value="2000">2,000 lines</option>
              <option value="5000">5,000 lines</option>
              <option value="10000">10,000 lines</option>
            </select>
          </label>
          <Button
            size="sm"
            variant="outline"
            onClick={togglePause}
            disabled={!enabled}
          >
            {pausedRecords === undefined ? (
              <Pause aria-hidden="true" />
            ) : (
              <Play aria-hidden="true" />
            )}
            {pausedRecords === undefined ? 'Pause view' : 'Resume view'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={exportVisible}
            disabled={visible.length === 0}
          >
            <Download aria-hidden="true" /> Export visible
          </Button>
        </div>
        {status?.detail === undefined ? null : (
          <p className="guardrail-copy">{status.detail}</p>
        )}
        {regexError === undefined ? null : (
          <p className="operation-inline-error" role="alert">
            Invalid regular expression: {regexError}
          </p>
        )}
        {transportDropped + retentionDropped === 0 ? null : (
          <output className="operation-inline-error">
            {transportDropped} transport records dropped; {retentionDropped}{' '}
            records truncated by browser retention.
          </output>
        )}
        {visible.length <= MAX_RENDERED_RECORDS ? null : (
          <output className="guardrail-copy">
            Showing the latest {MAX_RENDERED_RECORDS.toLocaleString()} of{' '}
            {visible.length.toLocaleString()} matching records. Export includes
            every matching retained record.
          </output>
        )}
        <ol
          className="log-records live-log-records"
          ref={followRef}
          aria-live={pausedRecords === undefined ? 'polite' : 'off'}
        >
          {rendered.map((record) => (
            <li key={record.sequence} data-severity={record.severity}>
              <time dateTime={record.sourceTimestamp ?? record.receivedAt}>
                {new Date(
                  record.sourceTimestamp ?? record.receivedAt,
                ).toLocaleTimeString()}
              </time>
              <Badge
                variant={
                  record.severity === 'error' ? 'destructive' : 'outline'
                }
              >
                {record.severity}
              </Badge>
              <span className="log-source">{record.source}</span>
              <div>
                <strong>{record.message}</strong>
                <small>
                  {record.sourceTimestamp === undefined
                    ? 'receipt time · source timestamp unavailable'
                    : 'source timestamp'}
                </small>
              </div>
            </li>
          ))}
        </ol>
        {visible.length === 0 ? (
          <div className="log-empty">
            <Radio aria-hidden="true" />
            <strong>
              {enabled ? 'Waiting for matching records' : 'Capture is stopped'}
            </strong>
            <p>
              Starting capture requests DEBUG output by default and does not
              alter producer log levels.
            </p>
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

export function compileSafeLogRegex(pattern: string): {
  readonly expression?: RegExp;
  readonly error?: string;
} {
  if (pattern.length > 120) {
    return { error: 'Regular expressions are limited to 120 characters.' };
  }
  if (/\\[1-9]|\\k<|\(\?/.test(pattern)) {
    return {
      error: 'Backreferences and lookaround constructs are not supported.',
    };
  }
  if (/\{/.test(pattern) || /\([^)]*\)\s*[*+]/.test(pattern)) {
    return {
      error: 'Counted or repeated-group quantifiers are not supported.',
    };
  }
  const unboundedQuantifiers = pattern.match(/(?<!\\)[*+]/g)?.length ?? 0;
  if (unboundedQuantifiers > 1) {
    return {
      error: 'Use at most one unbounded quantifier in a log expression.',
    };
  }
  try {
    return { expression: new RegExp(pattern, 'i') };
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : 'Invalid expression',
    };
  }
}

function compareRecords(left: LiveLogRecord, right: LiveLogRecord): number {
  const leftTime = Date.parse(left.sourceTimestamp ?? left.receivedAt);
  const rightTime = Date.parse(right.sourceTimestamp ?? right.receivedAt);
  return leftTime - rightTime || left.sequence - right.sequence;
}

function formatExportLine(record: LiveLogRecord): string {
  return `${record.sourceTimestamp ?? record.receivedAt} [${record.source}] ${record.severity.toUpperCase()} ${record.message}`;
}

function isSourceStatus(value: unknown): value is LiveLogSourceStatus {
  return (
    typeof value === 'object' &&
    value !== null &&
    'source' in value &&
    value.source === 'addon-runtime' &&
    'availability' in value &&
    typeof value.availability === 'string' &&
    'detail' in value &&
    typeof value.detail === 'string'
  );
}
