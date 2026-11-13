"""Database management: SQLite schema, constraints, migrations, and transactional execution."""
from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Iterator

from solution.config import DB_PATH

SCHEMA_SQL = """
-- File ingestion audit log for idempotency and durable metrics tracking
CREATE TABLE IF NOT EXISTS file_ingestion_log (
    drop_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    file_type TEXT NOT NULL, -- 'leads' or 'events'
    file_hash TEXT NOT NULL,
    rows_total INTEGER NOT NULL DEFAULT 0,
    rows_rejected INTEGER NOT NULL DEFAULT 0,
    duplicates_merged INTEGER NOT NULL DEFAULT 0,
    field_warnings INTEGER NOT NULL DEFAULT 0,
    malformed_lines INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped INTEGER NOT NULL DEFAULT 0,
    unknown_dispositions INTEGER NOT NULL DEFAULT 0,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (drop_id, relative_path)
);

-- Master leads table: encrypted PII at rest, HMAC phone identity
CREATE TABLE IF NOT EXISTS leads (
    workflow_id TEXT NOT NULL,
    phone_hash TEXT NOT NULL,                  -- HMAC-SHA256 of canonical phone
    batch_id TEXT NOT NULL,                     -- Permanent first-seen batch stem
    phone_masked TEXT NOT NULL,                 -- +91******XXXX for reporting
    external_ref TEXT NOT NULL,                 -- Schema reference field (policy_number / order_id)
    encrypted_pii_json TEXT NOT NULL,           -- AES-256-GCM encrypted PII fields
    attributes_json TEXT NOT NULL,              -- Non-PII fields JSON (merged last-non-null)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, phone_hash)
);

CREATE INDEX IF NOT EXISTS idx_leads_external_ref ON leads (workflow_id, external_ref);
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads (workflow_id, batch_id);

-- Rejected lead rows: no PII stored, exact reason recorded
CREATE TABLE IF NOT EXISTS lead_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    drop_id TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (drop_id, relative_path, row_number)
);

CREATE INDEX IF NOT EXISTS idx_rejections_lookup ON lead_rejections (workflow_id, batch_id, row_number);

-- Decoupled call-to-lead association table: enables out-of-order overrides and ledger immutability
CREATE TABLE IF NOT EXISTS call_lead_association (
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    phone_hash TEXT,                            -- NULL if first event seen is disposition.override
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workflow_id, call_id)
);

CREATE INDEX IF NOT EXISTS idx_call_lead_phone ON call_lead_association (workflow_id, phone_hash);

-- Append-only interaction ledger: strictly immutable event records
CREATE TABLE IF NOT EXISTS interaction_ledger (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    occurred_at_utc TEXT NOT NULL,              -- Canonical UTC ISO-8601
    payload_json TEXT NOT NULL,                 -- Immutable raw payload
    is_valid_disposition INTEGER NOT NULL DEFAULT 1, -- 0 if unknown disposition code per catalogue
    source_drop_id TEXT NOT NULL,
    source_relative_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (workflow_id, call_id) REFERENCES call_lead_association(workflow_id, call_id)
);

CREATE INDEX IF NOT EXISTS idx_ledger_call_id ON interaction_ledger (workflow_id, call_id);
CREATE INDEX IF NOT EXISTS idx_ledger_event_type ON interaction_ledger (event_type);
CREATE INDEX IF NOT EXISTS idx_ledger_occurred_at ON interaction_ledger (occurred_at_utc);
"""


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Creates a configured SQLite connection with WAL mode and foreign keys enabled."""
    target_path = Path(db_path) if db_path else DB_PATH
    if target_path != Path(":memory:"):
        os.makedirs(target_path.parent, exist_ok=True)

    conn = sqlite3.connect(
        str(target_path),
        timeout=10.0,
        isolation_level=None,  # Autocommit mode; we manage explicit transactions
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row

    # Performance and integrity pragmas
    conn.execute("PRAGMA foreign_keys = ON;")
    if target_path != Path(":memory:"):
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 10000;")

    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initializes tables and indexes idempotently."""
    conn.executescript(SCHEMA_SQL)


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Context manager for explicit ACID transaction boundaries."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise

