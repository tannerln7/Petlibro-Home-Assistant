import { Link } from '@tanstack/react-router';
import { ArrowRight, Clock3 } from 'lucide-react';
import type { OperationSnapshot } from '#/domain/operations';
import { operationTitle } from '#/domain/operations';
import { formatDateTime, humanize, StatusBadge } from './presentation';

export function OperationList({
  operations,
  selectedId,
  onSelect,
  limit,
}: {
  readonly operations: readonly OperationSnapshot[];
  readonly selectedId?: string;
  readonly onSelect?: (operationId: string) => void;
  readonly limit?: number;
}) {
  const visible = limit === undefined ? operations : operations.slice(0, limit);
  return (
    <ol className="operation-list">
      {visible.map((operation) => (
        <li key={operation.operationId}>
          {onSelect === undefined ? (
            <Link className="operation-list-item" to="/operations">
              <OperationListContent operation={operation} />
              <ArrowRight aria-hidden="true" />
            </Link>
          ) : (
            <button
              className="operation-list-item"
              data-selected={selectedId === operation.operationId}
              type="button"
              onClick={() => onSelect(operation.operationId)}
            >
              <OperationListContent operation={operation} />
            </button>
          )}
        </li>
      ))}
    </ol>
  );
}

function OperationListContent({
  operation,
}: {
  readonly operation: OperationSnapshot;
}) {
  return (
    <>
      <span className="operation-list-leading" aria-hidden="true">
        <Clock3 />
      </span>
      <span className="operation-list-copy">
        <strong>{operationTitle(operation)}</strong>
        <small>
          {humanize(operation.stage)} · {formatDateTime(operation.updatedAt)}
        </small>
        <code>{operation.operationId}</code>
      </span>
      <StatusBadge status={operation.status} />
    </>
  );
}
