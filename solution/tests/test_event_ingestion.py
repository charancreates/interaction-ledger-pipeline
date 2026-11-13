"""Tests for Stage 5: Event Ingestion, Deduplication, Append-Only Ledger, and Orphan Handling."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import pytest

from solution.database import get_connection, init_db, transaction
from solution.event_ingestion import EventIngestor
from solution.lead_ingestion import LeadIngestor
from solution.schemas import SchemaRegistry


@pytest.fixture
def test_env(tmp_path: Path):
    conn = get_connection(":memory:")
    init_db(conn)
    registry = SchemaRegistry()
    lead_ingestor = LeadIngestor(conn, registry)
    event_ingestor = EventIngestor(conn, registry)
    return conn, registry, lead_ingestor, event_ingestor, tmp_path


def test_event_ingestion_and_deduplication(test_env):
    conn, _, _, event_ingestor, tmp_path = test_env

    events_file = tmp_path / "events_0001.jsonl"
    lines = [
        # Valid event 1 (UTC)
        json.dumps({
            "event_id": "evt_001",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-05T10:00:00Z",
            "phone": "9876543210",
            "payload": {}
        }),
        # Blank line (ignored)
        "",
        # Duplicate of event 1
        json.dumps({
            "event_id": "evt_001",
            "event_type": "call.initiated",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-05T10:00:00Z",
            "phone": "9876543210",
            "payload": {}
        }),
        # Malformed line (truncated JSON)
        '{"event_id": "evt_002", "event_type": "call.',
        # Valid event 2 (naive IST timestamp)
        json.dumps({
            "event_id": "evt_003",
            "event_type": "call.connected",
            "workflow_id": "motor_renewal",
            "call_id": "call_001",
            "occurred_at": "2026-10-05 15:30:00",
            "phone": "+91 98765 43210",
            "payload": {}
        }),
    ]
    events_file.write_text("\n".join(lines), encoding="utf-8")

    with transaction(conn):
        res = event_ingestor.ingest_event_file(events_file, "drop_1", "events/events_0001.jsonl")

    assert res["status"] == "success"
    assert res["events_total"] == 4  # 4 non-empty lines
    assert res["malformed_events"] == 1
    assert res["duplicate_events_dropped"] == 1

    # Check ledger records in database
    cur = conn.execute("SELECT event_id, occurred_at_utc FROM interaction_ledger ORDER BY event_id;")
    rows = cur.fetchall()
    assert len(rows) == 2
    assert rows[0]["event_id"] == "evt_001"
    assert rows[1]["event_id"] == "evt_003"
    # Naive 15:30:00 IST is 10:00:00 UTC
    assert rows[1]["occurred_at_utc"] == "2026-10-05T10:00:00+00:00"


def test_orphan_resolution_events_before_leads(test_env):
    conn, _, lead_ingestor, event_ingestor, tmp_path = test_env

    # Step 1: Ingest events in drop_1 BEFORE the lead exists
    events_file = tmp_path / "events_0001.jsonl"
    event_data = {
        "event_id": "evt_100",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_100",
        "occurred_at": "2026-10-05T10:00:00Z",
        "phone": "9876543210",
        "payload": {}
    }
    events_file.write_text(json.dumps(event_data) + "\n", encoding="utf-8")

    with transaction(conn):
        event_ingestor.ingest_event_file(events_file, "drop_1", "events/events_0001.jsonl")

    # Verify event is an orphan
    assert event_ingestor.get_current_orphan_count() == 1

    # Step 2: Ingest lead in drop_2
    lead_file = tmp_path / "batch_001.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Rahul Sharma", "9876543210", "MOT/9999"])

    with transaction(conn):
        lead_ingestor.ingest_lead_file(lead_file, "drop_2", "leads/motor_renewal/batch_001.csv", "motor_renewal")

    # Verify orphan is now resolved automatically!
    assert event_ingestor.get_current_orphan_count() == 0


def test_out_of_order_override_linking(test_env):
    conn, _, lead_ingestor, event_ingestor, tmp_path = test_env

    # 1. Ingest lead first
    lead_file = tmp_path / "batch_001.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Rahul Sharma", "9876543210", "MOT/9999"])

    with transaction(conn):
        lead_ingestor.ingest_lead_file(lead_file, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")

    # 2. Ingest disposition.override FIRST (has call_id, NO phone)
    override_file = tmp_path / "events_override.jsonl"
    override_event = {
        "event_id": "evt_ovr_1",
        "event_type": "disposition.override",
        "workflow_id": "motor_renewal",
        "call_id": "call_555",
        "occurred_at": "2026-10-06T12:00:00Z",
        "payload": {
            "disposition_code": "renewed",
            "overridden_by": "supervisor_1",
            "reason": "qa_review"
        }
    }
    override_file.write_text(json.dumps(override_event) + "\n", encoding="utf-8")

    with transaction(conn):
        event_ingestor.ingest_event_file(override_file, "drop_1", "events/events_override.jsonl")

    # Override has no phone yet, so it is an orphan
    assert event_ingestor.get_current_orphan_count() == 1

    # 3. Later event for call_555 arrives with phone number
    call_file = tmp_path / "events_call.jsonl"
    init_event = {
        "event_id": "evt_init_1",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_555",
        "occurred_at": "2026-10-06T11:00:00Z",
        "phone": "9876543210",
        "payload": {}
    }
    call_file.write_text(json.dumps(init_event) + "\n", encoding="utf-8")

    with transaction(conn):
        event_ingestor.ingest_event_file(call_file, "drop_1", "events/events_call.jsonl")

    # Now both events (override and initiated) are linked to the lead!
    assert event_ingestor.get_current_orphan_count() == 0


def test_unknown_disposition_handling(test_env):
    conn, _, _, event_ingestor, tmp_path = test_env

    events_file = tmp_path / "events_unknown.jsonl"
    # Event with an unknown disposition code not in dispositions.csv
    event = {
        "event_id": "evt_unk_1",
        "event_type": "disposition.set",
        "workflow_id": "motor_renewal",
        "call_id": "call_777",
        "occurred_at": "2026-10-05T10:00:00Z",
        "phone": "9876543210",
        "payload": {"disposition_code": "completely_unknown_outcome"}
    }
    events_file.write_text(json.dumps(event) + "\n", encoding="utf-8")

    with transaction(conn):
        res = event_ingestor.ingest_event_file(events_file, "drop_1", "events/events_unknown.jsonl")

    assert res["unknown_disposition_events"] == 1

    # Verified stored in ledger with is_valid_disposition = 0
    cur = conn.execute("SELECT is_valid_disposition FROM interaction_ledger WHERE event_id = 'evt_unk_1';")
    assert cur.fetchone()[0] == 0


def test_conflicting_phone_in_call_association(test_env):
    conn, _, _, event_ingestor, tmp_path = test_env

    events_file = tmp_path / "events_conflict.jsonl"
    e1 = {
        "event_id": "evt_c1",
        "event_type": "call.initiated",
        "workflow_id": "motor_renewal",
        "call_id": "call_conflict",
        "occurred_at": "2026-10-05T10:00:00Z",
        "phone": "9876543210",
        "payload": {}
    }
    e2 = {
        "event_id": "evt_c2",
        "event_type": "call.connected",
        "workflow_id": "motor_renewal",
        "call_id": "call_conflict",
        "occurred_at": "2026-10-05T10:05:00Z",
        "phone": "9123456789",  # Conflicting phone for same call_id!
        "payload": {}
    }
    events_file.write_text(f"{json.dumps(e1)}\n{json.dumps(e2)}\n", encoding="utf-8")

    with transaction(conn):
        event_ingestor.ingest_event_file(events_file, "drop_1", "events/events_conflict.jsonl")

    # Initial phone association must be preserved, not overwritten
    from solution.crypto import hash_phone
    expected_hash = hash_phone("+919876543210")
    cur = conn.execute("SELECT phone_hash FROM call_lead_association WHERE call_id = 'call_conflict';")
    assert cur.fetchone()["phone_hash"] == expected_hash

