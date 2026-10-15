import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import type {
  OperationEventQuery,
  OperationJournal,
} from '#/application/operation-journal';
import {
  isTerminalOperationStatus,
  type OperationEvent,
  type OperationEventDraft,
  type OperationSnapshot,
  parseOperationSnapshot,
} from '#/domain/operations';

interface SnapshotRow {
  readonly snapshot_json: string;
}

interface SchemaVersionRow {
  readonly user_version: number;
}

interface EventRow {
  readonly sequence: number;
  readonly source_event_key: string;
  readonly operation_id: string;
  readonly type: string;
  readonly source: OperationEvent['source'];
  readonly severity: OperationEvent['severity'];
  readonly occurred_at: string;
  readonly ingested_at: string;
  readonly payload_json: string;
}

export class SqliteOperationJournal implements OperationJournal {
  private readonly database: DatabaseSync;

  public constructor(databasePath: string) {
    if (databasePath !== ':memory:') {
      mkdirSync(dirname(databasePath), { recursive: true, mode: 0o700 });
    }
    this.database = new DatabaseSync(databasePath, { timeout: 5_000 });
    const schemaVersion = this.database
      .prepare('PRAGMA user_version')
      .get() as unknown as SchemaVersionRow;
    if (schemaVersion.user_version > 1) {
      throw new Error(
        `Management database schema ${schemaVersion.user_version} is newer than this application supports.`,
      );
    }
    this.database.exec(`
      PRAGMA journal_mode = WAL;
      PRAGMA foreign_keys = ON;
      CREATE TABLE IF NOT EXISTS operations (
        operation_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        snapshot_json TEXT NOT NULL
      ) STRICT;
      CREATE TABLE IF NOT EXISTS operation_events (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        source_event_key TEXT NOT NULL UNIQUE,
        operation_id TEXT NOT NULL REFERENCES operations(operation_id) ON DELETE CASCADE,
        type TEXT NOT NULL,
        source TEXT NOT NULL,
        severity TEXT NOT NULL,
        occurred_at TEXT NOT NULL,
        ingested_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
      ) STRICT;
      CREATE INDEX IF NOT EXISTS operation_events_operation_sequence
        ON operation_events(operation_id, sequence);
      CREATE INDEX IF NOT EXISTS operations_updated
        ON operations(updated_at DESC);
      PRAGMA user_version = 1;
    `);
  }

  public create(
    snapshot: OperationSnapshot,
    events: readonly OperationEventDraft[],
  ): void {
    this.transaction(() => {
      this.database
        .prepare(
          `INSERT INTO operations
            (operation_id, status, created_at, updated_at, snapshot_json)
           VALUES (?, ?, ?, ?, ?)`,
        )
        .run(
          snapshot.operationId,
          snapshot.status,
          snapshot.createdAt,
          snapshot.updatedAt,
          JSON.stringify(snapshot),
        );
      this.insertEvents(events);
    });
  }

  public update(
    snapshot: OperationSnapshot,
    events: readonly OperationEventDraft[],
  ): void {
    this.transaction(() => {
      const result = this.database
        .prepare(
          `UPDATE operations
           SET status = ?, updated_at = ?, snapshot_json = ?
           WHERE operation_id = ?`,
        )
        .run(
          snapshot.status,
          snapshot.updatedAt,
          JSON.stringify(snapshot),
          snapshot.operationId,
        );
      if (result.changes !== 1) {
        throw new Error(`Operation ${snapshot.operationId} does not exist.`);
      }
      this.insertEvents(events);
    });
  }

  public get(operationId: string): OperationSnapshot | undefined {
    const row = this.database
      .prepare('SELECT snapshot_json FROM operations WHERE operation_id = ?')
      .get(operationId) as SnapshotRow | undefined;
    return row === undefined
      ? undefined
      : parseOperationSnapshot(JSON.parse(row.snapshot_json));
  }

  public list(limit: number): readonly OperationSnapshot[] {
    const rows = this.database
      .prepare(
        `SELECT snapshot_json FROM operations
         ORDER BY created_at DESC LIMIT ?`,
      )
      .all(limit) as unknown as SnapshotRow[];
    return rows.map((row) =>
      parseOperationSnapshot(JSON.parse(row.snapshot_json)),
    );
  }

  public listNonTerminal(): readonly OperationSnapshot[] {
    return this.list(1_000).filter(
      (snapshot) => !isTerminalOperationStatus(snapshot.status),
    );
  }

  public eventsAfter(
    operationId: string,
    afterSequence: number,
    limit: number,
  ): readonly OperationEvent[] {
    const rows = this.database
      .prepare(
        `SELECT sequence, source_event_key, operation_id, type, source,
                severity, occurred_at, ingested_at, payload_json
         FROM operation_events
         WHERE operation_id = ? AND sequence > ?
         ORDER BY sequence ASC LIMIT ?`,
      )
      .all(operationId, afterSequence, limit) as unknown as EventRow[];
    return rows.map(eventFromRow);
  }

  public queryEvents(query: OperationEventQuery): readonly OperationEvent[] {
    const conditions: string[] = [];
    const parameters: Array<string | number> = [];
    if (query.severity !== undefined) {
      conditions.push('severity = ?');
      parameters.push(query.severity);
    }
    if (query.source !== undefined) {
      conditions.push('source = ?');
      parameters.push(query.source);
    }
    if (query.search !== undefined) {
      conditions.push(
        `(type LIKE ? ESCAPE '\\' OR payload_json LIKE ? ESCAPE '\\'
          OR operation_id LIKE ? ESCAPE '\\')`,
      );
      const pattern = `%${escapeLike(query.search)}%`;
      parameters.push(pattern, pattern, pattern);
    }
    const where =
      conditions.length === 0 ? '' : `WHERE ${conditions.join(' AND ')}`;
    const rows = this.database
      .prepare(
        `SELECT sequence, source_event_key, operation_id, type, source,
                severity, occurred_at, ingested_at, payload_json
         FROM operation_events ${where}
         ORDER BY sequence DESC LIMIT ?`,
      )
      .all(...parameters, query.limit) as unknown as EventRow[];
    return rows.map(eventFromRow);
  }

  public close(): void {
    this.database.close();
  }

  private insertEvents(events: readonly OperationEventDraft[]): void {
    const insert = this.database.prepare(
      `INSERT OR IGNORE INTO operation_events
        (source_event_key, operation_id, type, source, severity,
         occurred_at, ingested_at, payload_json)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    );
    for (const event of events) {
      insert.run(
        event.sourceEventKey,
        event.operationId,
        event.type,
        event.source,
        event.severity,
        event.occurredAt,
        new Date().toISOString(),
        JSON.stringify(event.payload),
      );
    }
  }

  private transaction(work: () => void): void {
    this.database.exec('BEGIN IMMEDIATE');
    try {
      work();
      this.database.exec('COMMIT');
    } catch (error) {
      this.database.exec('ROLLBACK');
      throw error;
    }
  }
}

function eventFromRow(row: EventRow): OperationEvent {
  return {
    sequence: row.sequence,
    sourceEventKey: row.source_event_key,
    operationId: row.operation_id,
    type: row.type,
    source: row.source,
    severity: row.severity,
    occurredAt: row.occurred_at,
    ingestedAt: row.ingested_at,
    payload: JSON.parse(row.payload_json) as Record<string, unknown>,
  };
}

function escapeLike(value: string): string {
  return value
    .replaceAll('\\', '\\\\')
    .replaceAll('%', '\\%')
    .replaceAll('_', '\\_');
}
