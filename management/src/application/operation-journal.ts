import type {
  OperationEvent,
  OperationEventDraft,
  OperationSnapshot,
} from '#/domain/operations';

export interface OperationJournal {
  create(
    snapshot: OperationSnapshot,
    events: readonly OperationEventDraft[],
  ): void;
  update(
    snapshot: OperationSnapshot,
    events: readonly OperationEventDraft[],
  ): void;
  get(operationId: string): OperationSnapshot | undefined;
  list(limit: number): readonly OperationSnapshot[];
  listNonTerminal(): readonly OperationSnapshot[];
  eventsAfter(
    operationId: string,
    afterSequence: number,
    limit: number,
  ): readonly OperationEvent[];
  queryEvents(query: OperationEventQuery): readonly OperationEvent[];
}

export interface OperationEventQuery {
  readonly limit: number;
  readonly severity?: OperationEvent['severity'];
  readonly source?: OperationEvent['source'];
  readonly search?: string;
}
