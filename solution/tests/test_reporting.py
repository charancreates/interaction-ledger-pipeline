"""Comprehensive tests for Stage 6: Derived Facts & Reporting Engine."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import pytest

from solution.database import get_connection, init_db, transaction
from solution.event_ingestion import EventIngestor
from solution.lead_ingestion import LeadIngestor
from solution.reporting import ReportGenerator
from solution.schemas import SchemaRegistry


@pytest.fixture
def reporting_env(tmp_path: Path):
    conn = get_connection(":memory:")
    init_db(conn)
    registry = SchemaRegistry()
    lead_ingestor = LeadIngestor(conn, registry)
    event_ingestor = EventIngestor(conn, registry)
    report_gen = ReportGenerator(conn, registry)
    return conn, registry, lead_ingestor, event_ingestor, report_gen, tmp_path


def test_reporting_end_to_end(reporting_env):
    conn, _, lead_ingestor, event_ingestor, report_gen, tmp_path = reporting_env

    # 1. Ingest Leads
    lead_file = tmp_path / "batch_001.csv"
    with open(lead_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount"])
        # Lead A: MOT/101 - Phone 9876543210
        writer.writerow(["Aarav Sharma", "9876543210", "MOT/101", "10000"])
        # Lead B: MOT/102 - Phone 9876543211
        writer.writerow(["Diya Patel", "9876543211", "MOT/102", "12000"])
        # Lead C: MOT/103 - Phone 9876543212
        writer.writerow(["Kavya Nair", "9876543212", "MOT/103", "15000"])
        # Lead D: MOT/104 - Phone 9876543213
        writer.writerow(["Rohan Iyer", "9876543213", "MOT/104", "8000"])

    with transaction(conn):
        lead_ingestor.ingest_lead_file(lead_file, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")

    # 2. Ingest Events
    events_file = tmp_path / "events.jsonl"
    events = [
        # Call 1 for Lead A: Connected, renewed (terminal, success)
        {
            "event_id": "e01", "event_type": "call.initiated", "workflow_id": "motor_renewal",
            "call_id": "call_a1", "occurred_at": "2026-10-05T09:00:00Z", "phone": "9876543210", "payload": {}
        },
        {
            "event_id": "e02", "event_type": "call.connected", "workflow_id": "motor_renewal",
            "call_id": "call_a1", "occurred_at": "2026-10-05T09:00:10Z", "phone": "9876543210", "payload": {}
        },
        {
            "event_id": "e03", "event_type": "disposition.set", "workflow_id": "motor_renewal",
            "call_id": "call_a1", "occurred_at": "2026-10-05T09:02:00Z", "phone": "9876543210",
            "payload": {"disposition_code": "renewed"}
        },

        # Call 1 for Lead B: AI set terminal 'not_interested', but supervisor OVERRODE to non-terminal 'callback_requested'
        {
            "event_id": "e04", "event_type": "call.initiated", "workflow_id": "motor_renewal",
            "call_id": "call_b1", "occurred_at": "2026-10-05T10:00:00Z", "phone": "9876543211", "payload": {}
        },
        {
            "event_id": "e05", "event_type": "call.connected", "workflow_id": "motor_renewal",
            "call_id": "call_b1", "occurred_at": "2026-10-05T10:00:15Z", "phone": "9876543211", "payload": {}
        },
        {
            "event_id": "e06", "event_type": "disposition.set", "workflow_id": "motor_renewal",
            "call_id": "call_b1", "occurred_at": "2026-10-05T10:02:00Z", "phone": "9876543211",
            "payload": {"disposition_code": "not_interested"}
        },
        {
            "event_id": "e07", "event_type": "disposition.override", "workflow_id": "motor_renewal",
            "call_id": "call_b1", "occurred_at": "2026-10-05T11:00:00Z",
            "payload": {"disposition_code": "callback_requested", "overridden_by": "sup_1", "reason": "qa_review"}
        },

        # Call 1 for Lead C: Telephony failed
        {
            "event_id": "e08", "event_type": "call.initiated", "workflow_id": "motor_renewal",
            "call_id": "call_c1", "occurred_at": "2026-10-06T10:00:00Z", "phone": "9876543212", "payload": {}
        },
        {
            "event_id": "e09", "event_type": "call.telephony_failed", "workflow_id": "motor_renewal",
            "call_id": "call_c1", "occurred_at": "2026-10-06T10:00:05Z", "phone": "9876543212",
            "payload": {"reason": "sip_503"}
        },

        # Lead D: 0 calls
    ]
    events_file.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

    with transaction(conn):
        event_ingestor.ingest_event_file(events_file, "drop_1", "events/events.jsonl")

    # 3. Generate Reports
    reports_dir = tmp_path / "reports"
    files = report_gen.generate_all_reports(reports_dir)

    # Verify all 5 files generated
    assert (reports_dir / "batch_summary.csv").exists()
    assert (reports_dir / "daily_overview.csv").exists()
    assert (reports_dir / "lead_report.csv").exists()
    assert (reports_dir / "rejections.csv").exists()
    assert (reports_dir / "data_quality.json").exists()

    # --- Verify batch_summary.csv ---
    with open(reports_dir / "batch_summary.csv", newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        row = reader[0]
        assert row["workflow_id"] == "motor_renewal"
        assert row["batch_id"] == "batch_001"
        assert int(row["total_leads"]) == 4
        assert int(row["completed"]) == 1               # Lead A
        assert int(row["in_progress_dialed"]) == 1      # Lead B (override to callback_requested)
        assert int(row["in_progress_telephony_failed"]) == 1  # Lead C
        assert int(row["yet_to_reach_out"]) == 1        # Lead D

    # --- Verify daily_overview.csv ---
    with open(reports_dir / "daily_overview.csv", newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 2  # Oct 05 and Oct 06
        # Oct 05: 2 calls (call_a1, call_b1), both connected, 1 success lead (Lead A)
        d1 = reader[0]
        assert d1["date"] == "2026-10-05"
        assert int(d1["total_calls"]) == 2
        assert int(d1["connected_calls"]) == 2
        assert d1["connectivity_rate"] == "1.0000"
        assert int(d1["success_leads"]) == 1

        # Oct 06: 1 call (call_c1), 0 connected, 0 success
        d2 = reader[1]
        assert d2["date"] == "2026-10-06"
        assert int(d2["total_calls"]) == 1
        assert int(d2["connected_calls"]) == 0
        assert d2["connectivity_rate"] == "0.0000"
        assert int(d2["success_leads"]) == 0

    # --- Verify lead_report.csv ---
    with open(reports_dir / "lead_report.csv", newline="", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 4
        # Sorted by workflow_id, external_ref: MOT/101, MOT/102, MOT/103, MOT/104
        l_a = reader[0]
        assert l_a["external_ref"] == "MOT/101"
        assert l_a["bucket"] == "completed"
        assert l_a["current_disposition"] == "renewed"
        assert l_a["ever_success"] == "true"
        assert l_a["total_calls"] == "1"
        assert l_a["connected_calls"] == "1"

        l_b = reader[1]
        assert l_b["external_ref"] == "MOT/102"
        # Overridden to callback_requested -> not terminal -> in_progress_dialed
        assert l_b["bucket"] == "in_progress_dialed"
        assert l_b["current_disposition"] == "callback_requested"
        assert l_b["ever_success"] == "false"

        l_c = reader[2]
        assert l_c["external_ref"] == "MOT/103"
        assert l_c["bucket"] == "in_progress_telephony_failed"
        assert l_c["current_disposition"] == ""
        assert l_c["ever_success"] == "false"

        l_d = reader[3]
        assert l_d["external_ref"] == "MOT/104"
        assert l_d["bucket"] == "yet_to_reach_out"
        assert l_d["last_call_at"] == ""
        assert l_d["current_disposition"] == ""

    # --- Verify data_quality.json ---
    with open(reports_dir / "data_quality.json", encoding="utf-8") as f:
        dq = json.load(f)
        assert dq["lead_rows_total"] == 4
        assert dq["lead_rows_rejected"] == 0
        assert dq["orphan_events"] == 0
        assert dq["events_total"] == len(events)

