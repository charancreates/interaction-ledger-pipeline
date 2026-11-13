"""Event ingestion engine: JSONL streaming, deduplication, append-only ledger, and orphan handling."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution.crypto import hash_phone
from solution.normalization import normalize_phone, normalize_timestamp_to_utc
from solution.schemas import SchemaRegistry

logger = logging.getLogger(__name__)


def compute_file_hash(file_path: Path) -> str:
    """Calculates SHA-256 hash of a file for change detection."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class EventIngestor:
    """Ingests JSON Lines event files into the append-only ledger."""

    def __init__(self, conn: sqlite3.Connection, schema_registry: SchemaRegistry):
        self.conn = conn
        self.schema_registry = schema_registry

    def ingest_event_file(
        self,
        file_path: Path,
        drop_id: str,
        relative_path: str,
    ) -> dict[str, int]:
        """Ingests a single event JSONL file idempotently within an ACID transaction."""
        file_hash = compute_file_hash(file_path)

        # Idempotency check: if file already ingested with identical content hash, skip safely
        cur = self.conn.execute(
            "SELECT file_hash FROM file_ingestion_log WHERE drop_id = ? AND relative_path = ?;",
            (drop_id, relative_path),
        )
        existing = cur.fetchone()
        if existing and existing["file_hash"] == file_hash:
            return {"status": "skipped", "reason": "already_ingested"}

        now_iso = datetime.now(timezone.utc).isoformat()

        events_total = 0
        malformed_events = 0
        duplicate_events_dropped = 0
        unknown_disposition_events = 0

        # In-memory tracking of seen event IDs in this batch/run to prevent duplicate inserts in the same file
        seen_event_ids_this_file: set[str] = set()

        with open(file_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if not stripped:
                    # Blank line: ignored per specification
                    continue

                # Specification rule: "events_total counts every non-empty line read, including malformed and duplicate lines."
                events_total += 1

                # 1. Malformed line check: valid JSON and has event_id
                try:
                    event = json.loads(stripped)
                except Exception:
                    malformed_events += 1
                    continue

                if not isinstance(event, dict):
                    malformed_events += 1
                    continue

                event_id = event.get("event_id")
                if not event_id or not isinstance(event_id, str) or not event_id.strip():
                    malformed_events += 1
                    continue

                event_id = event_id.strip()
                event_type = event.get("event_type")
                workflow_id = event.get("workflow_id")
                call_id = event.get("call_id")
                occurred_at_raw = event.get("occurred_at")

                if not all([event_type, workflow_id, call_id, occurred_at_raw]):
                    malformed_events += 1
                    continue

                try:
                    occurred_at_utc = normalize_timestamp_to_utc(occurred_at_raw).isoformat()
                except Exception:
                    malformed_events += 1
                    continue

                # 2. Duplicate event check: event_id already seen (in this file or any prior drop)
                if event_id in seen_event_ids_this_file:
                    duplicate_events_dropped += 1
                    continue

                cur = self.conn.execute(
                    "SELECT 1 FROM interaction_ledger WHERE event_id = ?;",
                    (event_id,),
                )
                if cur.fetchone() is not None:
                    duplicate_events_dropped += 1
                    seen_event_ids_this_file.add(event_id)
                    continue

                seen_event_ids_this_file.add(event_id)

                # 3. Call-Lead Association & Phone Resolution
                # Extract phone if present (overrides have no phone)
                raw_phone = event.get("phone")
                phone_h = None
                if raw_phone:
                    try:
                        canonical_phone = normalize_phone(raw_phone)
                        phone_h = hash_phone(canonical_phone)
                    except ValueError:
                        # Present but invalid phone in event -> cannot link to valid lead
                        phone_h = None

                # Query existing call association
                cur = self.conn.execute(
                    "SELECT phone_hash FROM call_lead_association WHERE workflow_id = ? AND call_id = ?;",
                    (workflow_id, call_id),
                )
                assoc_row = cur.fetchone()

                if assoc_row is None:
                    # New call association record
                    self.conn.execute(
                        """
                        INSERT INTO call_lead_association (
                            workflow_id, call_id, phone_hash, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?);
                        """,
                        (workflow_id, call_id, phone_h, now_iso, now_iso),
                    )
                else:
                    existing_phone_h = assoc_row["phone_hash"]
                    if existing_phone_h is None and phone_h is not None:
                        # An earlier event (like override) arrived before call events with phone; update phone_hash
                        self.conn.execute(
                            """
                            UPDATE call_lead_association
                            SET phone_hash = ?, updated_at = ?
                            WHERE workflow_id = ? AND call_id = ?;
                            """,
                            (phone_h, now_iso, workflow_id, call_id),
                        )
                    elif existing_phone_h is not None and phone_h is not None and existing_phone_h != phone_h:
                        # Conflict handling: Never silently overwrite an established phone association!
                        logger.warning(
                            "Call %s in workflow %s received conflicting phone_hash. Keeping existing association.",
                            call_id,
                            workflow_id,
                        )

                # 4. Unknown disposition check
                # Rule: "a disposition.* event whose code is not in dispositions.csv for that workflow -> count it and ignore it."
                is_valid_disp = 1
                payload = event.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}

                if event_type.startswith("disposition."):
                    disp_code = payload.get("disposition_code")
                    if not disp_code:
                        is_valid_disp = 0
                        unknown_disposition_events += 1
                    else:
                        disp_info = self.schema_registry.get_disposition(workflow_id, disp_code)
                        if disp_info is None:
                            is_valid_disp = 0
                            unknown_disposition_events += 1

                # 5. Append event to interaction_ledger (Strictly append-only)
                self.conn.execute(
                    """
                    INSERT INTO interaction_ledger (
                        event_id, event_type, workflow_id, call_id,
                        occurred_at_utc, payload_json, is_valid_disposition,
                        source_drop_id, source_relative_path, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at_utc,
                        json.dumps(payload, ensure_ascii=False),
                        is_valid_disp,
                        drop_id,
                        relative_path,
                        now_iso,
                    ),
                )

        # Record file ingestion audit log in the same transaction
        self.conn.execute(
            """
            INSERT OR REPLACE INTO file_ingestion_log (
                drop_id, relative_path, file_type, file_hash,
                rows_total, rows_rejected, duplicates_merged,
                field_warnings, malformed_lines, duplicate_events_dropped,
                unknown_dispositions, ingested_at
            ) VALUES (?, ?, 'events', ?, ?, 0, 0, 0, ?, ?, ?, ?);
            """,
            (
                drop_id,
                relative_path,
                file_hash,
                events_total,
                malformed_events,
                duplicate_events_dropped,
                unknown_disposition_events,
                now_iso,
            ),
        )

        return {
            "status": "success",
            "events_total": events_total,
            "malformed_events": malformed_events,
            "duplicate_events_dropped": duplicate_events_dropped,
            "unknown_disposition_events": unknown_disposition_events,
        }

    def get_current_orphan_count(self) -> int:
        """Returns the current number of orphan events in the ledger.
        
        An event is an orphan if:
        - Its call association has no phone_hash (phone unknown), OR
        - Its (workflow_id, phone_hash) does not match any valid lead in the leads table.
        """
        cur = self.conn.execute(
            """
            SELECT COUNT(*)
            FROM interaction_ledger e
            JOIN call_lead_association c
              ON e.workflow_id = c.workflow_id AND e.call_id = c.call_id
            LEFT JOIN leads l
              ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
            WHERE c.phone_hash IS NULL OR l.phone_hash IS NULL;
            """
        )
        return cur.fetchone()[0]

