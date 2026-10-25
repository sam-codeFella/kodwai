"""SQLite warehouse. All SQL lives here, so moving to Postgres means swapping this module.

Idempotency is enforced by the database, not by pipeline bookkeeping:
``orders`` has PRIMARY KEY (partner, order_id) and is loaded with an upsert whose
UPDATE branch only fires when a business column actually differs.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable, Optional, Sequence

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    order_id       TEXT    NOT NULL,
    partner        TEXT    NOT NULL,
    customer_email TEXT    NOT NULL,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT    NOT NULL,
    placed_at      TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL,
    source_file    TEXT    NOT NULL,
    run_id         TEXT    NOT NULL,
    PRIMARY KEY (partner, order_id)
);

CREATE TABLE IF NOT EXISTS rejects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL,
    partner     TEXT NOT NULL,
    source_file TEXT NOT NULL,
    line_no     INTEGER NOT NULL,
    raw_row     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    rejected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejects_source ON rejects (source_file);

-- One row per successfully loaded file; used to spot byte-identical re-deliveries.
CREATE TABLE IF NOT EXISTS file_loads (
    source_file TEXT PRIMARY KEY,
    partner     TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL,
    run_id      TEXT NOT NULL,
    loaded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_file_loads_hash ON file_loads (partner, sha256);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    run_id      TEXT PRIMARY KEY,
    run_date    TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    duration_s  REAL,
    status      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partner_run_stats (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL,
    partner         TEXT NOT NULL,
    source_file     TEXT,
    status          TEXT NOT NULL,   -- ok | failed | missing | skipped_duplicate
    rows_in         INTEGER NOT NULL DEFAULT 0,
    rows_out        INTEGER NOT NULL DEFAULT 0,
    rows_rejected   INTEGER NOT NULL DEFAULT 0,
    rows_changed    INTEGER NOT NULL DEFAULT 0,
    rows_unchanged  INTEGER NOT NULL DEFAULT 0,
    extra_columns   TEXT,
    error           TEXT,
    duration_s      REAL
);
"""

# Rows of a file are first staged, then merged in one statement that keeps only the LAST
# row per key. Without this, a file repeating a key with different content would flip the
# stored row back and forth on every run (final data stable, run_id/ingested_at churning).
# The staging table is a disk-backed TEMP table, so memory stays O(batch) for huge files.
_STAGE_DDL = """
CREATE TEMP TABLE stage (
    seq            INTEGER PRIMARY KEY,
    order_id       TEXT    NOT NULL,
    partner        TEXT    NOT NULL,
    customer_email TEXT    NOT NULL,
    amount_minor   INTEGER NOT NULL,
    currency       TEXT    NOT NULL,
    placed_at      TEXT    NOT NULL,
    ingested_at    TEXT    NOT NULL,
    source_file    TEXT    NOT NULL,
    run_id         TEXT    NOT NULL
)
"""

_STAGE_INSERT = """
INSERT INTO stage (order_id, partner, customer_email, amount_minor, currency,
                   placed_at, ingested_at, source_file, run_id)
VALUES (:order_id, :partner, :customer_email, :amount_minor, :currency,
        :placed_at, :ingested_at, :source_file, :run_id)
"""

# The WHERE makes a re-load of identical data a true no-op (ingested_at / run_id keep
# their first-load values). The source_file guard stops a late-arriving *older* file
# from overwriting data already loaded from a *newer* one: file names embed the
# business date (see file_glob), so they order chronologically within a partner.
_MERGE = """
INSERT INTO orders (order_id, partner, customer_email, amount_minor, currency,
                    placed_at, ingested_at, source_file, run_id)
SELECT order_id, partner, customer_email, amount_minor, currency,
       placed_at, ingested_at, source_file, run_id
FROM stage
WHERE seq IN (SELECT max(seq) FROM stage GROUP BY partner, order_id)
ON CONFLICT (partner, order_id) DO UPDATE SET
    customer_email = excluded.customer_email,
    amount_minor   = excluded.amount_minor,
    currency       = excluded.currency,
    placed_at      = excluded.placed_at,
    ingested_at    = excluded.ingested_at,
    source_file    = excluded.source_file,
    run_id         = excluded.run_id
WHERE excluded.source_file >= orders.source_file
  AND (   orders.customer_email IS NOT excluded.customer_email
       OR orders.amount_minor   IS NOT excluded.amount_minor
       OR orders.currency       IS NOT excluded.currency
       OR orders.placed_at      IS NOT excluded.placed_at)
"""


class Warehouse:
    def __init__(self, path: str):
        # isolation_level=None: we issue BEGIN/COMMIT ourselves, one transaction per file.
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA temp_store=FILE")  # staged rows spill to disk, never accumulate in RAM
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- transactions -------------------------------------------------------
    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    # -- orders / rejects ---------------------------------------------------
    def begin_stage(self) -> None:
        self.conn.execute("DROP TABLE IF EXISTS temp.stage")
        self.conn.execute(_STAGE_DDL)

    def stage_orders(self, rows: Sequence[dict]) -> None:
        self.conn.executemany(_STAGE_INSERT, rows)

    def merge_stage(self) -> int:
        """Upsert the staged rows (last row per key wins); returns rows inserted or actually changed."""
        before = self.conn.total_changes
        self.conn.execute(_MERGE)
        changed = self.conn.total_changes - before
        self.conn.execute("DROP TABLE temp.stage")
        return changed

    def replace_rejects_for(self, source_file: str) -> None:
        """Rejects mirror the current state of the file, so re-runs never pile up duplicates."""
        self.conn.execute("DELETE FROM rejects WHERE source_file = ?", (source_file,))

    def insert_rejects(self, rows: Iterable[tuple]) -> None:
        self.conn.executemany(
            "INSERT INTO rejects (run_id, partner, source_file, line_no, raw_row, reason, rejected_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    # -- file ledger --------------------------------------------------------
    def find_identical_file(self, partner: str, sha256: str, source_file: str) -> Optional[str]:
        """A *different* file name already loaded with byte-identical content (re-delivery)."""
        row = self.conn.execute(
            "SELECT source_file FROM file_loads WHERE partner = ? AND sha256 = ? AND source_file <> ?"
            " ORDER BY source_file LIMIT 1",
            (partner, sha256, source_file),
        ).fetchone()
        return row[0] if row else None

    def record_file_load(self, source_file, partner, sha256, size_bytes, run_id, loaded_at) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO file_loads (source_file, partner, sha256, size_bytes, run_id, loaded_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (source_file, partner, sha256, size_bytes, run_id, loaded_at),
        )

    # -- run metrics --------------------------------------------------------
    def start_run(self, run_id: str, run_date: str, started_at: str) -> None:
        self.conn.execute(
            "INSERT INTO pipeline_runs (run_id, run_date, started_at, status) VALUES (?, ?, ?, 'running')",
            (run_id, run_date, started_at),
        )

    def finish_run(self, run_id: str, finished_at: str, duration_s: float, status: str) -> None:
        self.conn.execute(
            "UPDATE pipeline_runs SET finished_at = ?, duration_s = ?, status = ? WHERE run_id = ?",
            (finished_at, duration_s, status, run_id),
        )

    def record_partner_stats(self, s: dict) -> None:
        self.conn.execute(
            "INSERT INTO partner_run_stats (run_id, partner, source_file, status, rows_in, rows_out,"
            " rows_rejected, rows_changed, rows_unchanged, extra_columns, error, duration_s)"
            " VALUES (:run_id, :partner, :source_file, :status, :rows_in, :rows_out, :rows_rejected,"
            " :rows_changed, :rows_unchanged, :extra_columns, :error, :duration_s)",
            s,
        )
