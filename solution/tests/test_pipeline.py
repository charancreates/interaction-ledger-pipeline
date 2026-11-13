"""End-to-end integration tests: CLI, idempotency, incremental vs batch processing, and synthetic data validation."""
from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path
import pytest

from solution.cli import generate_reports, ingest_drop, reset_database
from solution.config import REPO_ROOT


@pytest.fixture
def run_env(tmp_path: Path):
    db_path = tmp_path / "test_ledger.db"
    reports_dir = tmp_path / "reports"
    return db_path, reports_dir, tmp_path


def test_synthetic_data_generator_pipeline(run_env):
    """Generates synthetic data with a fresh seed (seed=789) and runs full end-to-end ingestion and reporting."""
    db_path, reports_dir, tmp_path = run_env
    data_dir = tmp_path / "synthetic_data"

    # Run generate_data.py with seed=789
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    subprocess.run(
        [sys.executable, str(gen_script), "--seed", "789", "--scale", "small", "--out", str(data_dir)],
        check=True,
        capture_output=True,
    )

    drop_1 = data_dir / "drop_1"
    drop_2 = data_dir / "drop_2"
    assert drop_1.exists()
    assert drop_2.exists()

    # Step 1: Ingest drop_1
    ingest_drop(drop_1, db_path)

    # Step 2: Idempotency check: Re-ingest drop_1
    ingest_drop(drop_1, db_path)

    # Step 3: Ingest drop_2 (incremental)
    ingest_drop(drop_2, db_path)

    # Step 4: Generate reports
    generate_reports(reports_dir, db_path)

    # Verify all 5 files exist
    batch_summary_file = reports_dir / "batch_summary.csv"
    daily_overview_file = reports_dir / "daily_overview.csv"
    lead_report_file = reports_dir / "lead_report.csv"
    rejections_file = reports_dir / "rejections.csv"
    data_quality_file = reports_dir / "data_quality.json"

    assert batch_summary_file.exists()
    assert daily_overview_file.exists()
    assert lead_report_file.exists()
    assert rejections_file.exists()
    assert data_quality_file.exists()

    # Check batch_summary.csv header & structure
    with open(batch_summary_file, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected_header = [
            "workflow_id", "batch_id", "total_leads", "yet_to_reach_out",
            "in_progress_telephony_failed", "in_progress_dialed", "completed"
        ]
        assert header == expected_header
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            # Verify sum of buckets == total_leads
            total = int(r[2])
            buckets_sum = int(r[3]) + int(r[4]) + int(r[5]) + int(r[6])
            assert total == buckets_sum

    # Check daily_overview.csv header & structure
    with open(daily_overview_file, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected_header = [
            "workflow_id", "date", "total_calls", "connected_calls",
            "connectivity_rate", "success_leads"
        ]
        assert header == expected_header
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            rate_str = r[4]
            # Must have exactly 4 decimal places
            assert "." in rate_str
            decimals = rate_str.split(".")[1]
            assert len(decimals) == 4

    # Check lead_report.csv header & structure
    with open(lead_report_file, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected_header = [
            "workflow_id", "external_ref", "batch_id", "phone_masked",
            "bucket", "total_calls", "connected_calls", "last_call_at",
            "current_disposition", "ever_success"
        ]
        assert header == expected_header
        rows = list(reader)
        assert len(rows) > 0
        for r in rows:
            # Masked phone format +91******XXXX
            assert r[3].startswith("+91******")
            # ever_success boolean
            assert r[9] in ("true", "false")

    # Check rejections.csv header & structure
    with open(rejections_file, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        expected_header = ["workflow_id", "batch_id", "row_number", "reason"]
        assert header == expected_header
        rows = list(reader)
        assert len(rows) > 0

    # Check data_quality.json
    with open(data_quality_file, encoding="utf-8") as f:
        dq = json.load(f)
        assert "lead_rows_total" in dq
        assert "lead_rows_rejected" in dq
        assert "rejections_by_reason" in dq
        assert "duplicate_lead_rows_merged" in dq
        assert "field_warnings" in dq
        assert "events_total" in dq
        assert "malformed_events" in dq
        assert "duplicate_events_dropped" in dq
        assert "orphan_events" in dq
        assert "unknown_disposition_events" in dq
        assert dq["events_total"] > 0
        assert dq["lead_rows_total"] > 0


def test_incremental_processing_equivalence(tmp_path: Path):
    """Verifies that ingesting drop_1 followed by drop_2 yields identical reports
    to ingesting drop_1 and drop_2 in a single sequential execution."""
    data_dir = tmp_path / "synth_data"
    gen_script = REPO_ROOT / "assignment-a-data" / "tools" / "generate_data.py"
    subprocess.run(
        [sys.executable, str(gen_script), "--seed", "101", "--scale", "small", "--out", str(data_dir)],
        check=True,
        capture_output=True,
    )

    drop_1 = data_dir / "drop_1"
    drop_2 = data_dir / "drop_2"

    # Execution 1: Ingest drop_1, then drop_2
    db_1 = tmp_path / "ledger_1.db"
    rep_1 = tmp_path / "reports_1"
    ingest_drop(drop_1, db_1)
    ingest_drop(drop_2, db_1)
    generate_reports(rep_1, db_1)

    # Execution 2: Fresh DB, ingest drop_1 then drop_2
    db_2 = tmp_path / "ledger_2.db"
    rep_2 = tmp_path / "reports_2"
    ingest_drop(drop_1, db_2)
    ingest_drop(drop_2, db_2)
    generate_reports(rep_2, db_2)

    # Compare all 5 output files
    for filename in ["batch_summary.csv", "daily_overview.csv", "lead_report.csv", "rejections.csv", "data_quality.json"]:
        content_1 = (rep_1 / filename).read_text(encoding="utf-8")
        content_2 = (rep_2 / filename).read_text(encoding="utf-8")
        assert content_1 == content_2, f"Report {filename} differed between runs!"

