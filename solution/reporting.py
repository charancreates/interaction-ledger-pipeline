"""Report generation engine: CSV and JSON exporters complying with the exact output contract."""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from solution.derived_facts import DerivedFactsEngine, LeadFact
from solution.event_ingestion import EventIngestor
from solution.schemas import SchemaRegistry


class ReportGenerator:
    """Generates all five contract-specified report files."""

    def __init__(self, conn: sqlite3.Connection, schema_registry: SchemaRegistry):
        self.conn = conn
        self.schema_registry = schema_registry
        self.derived_engine = DerivedFactsEngine(conn, schema_registry)
        self.event_ingestor = EventIngestor(conn, schema_registry)

    def generate_all_reports(self, output_dir: Path | str) -> dict[str, Path]:
        """Generates all 5 reports into the specified output directory."""
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        call_facts = self.derived_engine.compute_all_call_facts()
        lead_facts = self.derived_engine.compute_all_lead_facts(call_facts)

        files = {
            "batch_summary": self.write_batch_summary(out_path / "batch_summary.csv", lead_facts),
            "daily_overview": self.write_daily_overview(out_path / "daily_overview.csv", call_facts),
            "lead_report": self.write_lead_report(out_path / "lead_report.csv", lead_facts),
            "rejections": self.write_rejections(out_path / "rejections.csv"),
            "data_quality": self.write_data_quality(out_path / "data_quality.json"),
        }
        return files

    def write_batch_summary(self, target_file: Path, lead_facts: list[LeadFact]) -> Path:
        """Writes batch_summary.csv, sorted by workflow_id, batch_id.
        
        Header:
        workflow_id,batch_id,total_leads,yet_to_reach_out,in_progress_telephony_failed,in_progress_dialed,completed
        """
        # Group by (workflow_id, batch_id)
        batches: dict[tuple[str, str], dict[str, int]] = {}
        for lf in lead_facts:
            key = (lf.workflow_id, lf.batch_id)
            if key not in batches:
                batches[key] = {
                    "total_leads": 0,
                    "yet_to_reach_out": 0,
                    "in_progress_telephony_failed": 0,
                    "in_progress_dialed": 0,
                    "completed": 0,
                }
            batches[key]["total_leads"] += 1
            batches[key][lf.bucket] += 1

        # Sort by workflow_id, batch_id
        sorted_keys = sorted(batches.keys())

        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "batch_id",
                "total_leads",
                "yet_to_reach_out",
                "in_progress_telephony_failed",
                "in_progress_dialed",
                "completed",
            ])
            for key in sorted_keys:
                counts = batches[key]
                writer.writerow([
                    key[0],
                    key[1],
                    counts["total_leads"],
                    counts["yet_to_reach_out"],
                    counts["in_progress_telephony_failed"],
                    counts["in_progress_dialed"],
                    counts["completed"],
                ])
        return target_file

    def write_daily_overview(self, target_file: Path, call_facts: dict[Any, Any]) -> Path:
        """Writes daily_overview.csv: one row per workflow per IST date with >= 1 call, sorted by workflow_id, date.
        
        Header:
        workflow_id,date,total_calls,connected_calls,connectivity_rate,success_leads
        """
        # Group calls by (workflow_id, ist_date)
        days: dict[tuple[str, str], dict[str, Any]] = {}
        for cf in call_facts.values():
            key = (cf.workflow_id, cf.ist_date)
            if key not in days:
                days[key] = {
                    "total_calls": 0,
                    "connected_calls": 0,
                    "success_lead_hashes": set(),
                }
            days[key]["total_calls"] += 1
            if cf.is_connected:
                days[key]["connected_calls"] += 1
            if cf.is_success:
                days[key]["success_lead_hashes"].add(cf.phone_hash)

        # Sort by workflow_id, date
        sorted_keys = sorted(days.keys())

        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "date",
                "total_calls",
                "connected_calls",
                "connectivity_rate",
                "success_leads",
            ])
            for key in sorted_keys:
                info = days[key]
                total = info["total_calls"]
                connected = info["connected_calls"]
                # 4-decimal connectivity rate formatting
                conn_rate = f"{(connected / total):.4f}" if total > 0 else "0.0000"
                succ_count = len(info["success_lead_hashes"])
                writer.writerow([
                    key[0],
                    key[1],
                    total,
                    connected,
                    conn_rate,
                    succ_count,
                ])
        return target_file

    def write_lead_report(self, target_file: Path, lead_facts: list[LeadFact]) -> Path:
        """Writes lead_report.csv: one row per lead, sorted by workflow_id, external_ref.
        
        Header:
        workflow_id,external_ref,batch_id,phone_masked,bucket,total_calls,connected_calls,last_call_at,current_disposition,ever_success
        """
        # Sort by workflow_id, external_ref
        sorted_leads = sorted(lead_facts, key=lambda x: (x.workflow_id, x.external_ref))

        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id",
                "external_ref",
                "batch_id",
                "phone_masked",
                "bucket",
                "total_calls",
                "connected_calls",
                "last_call_at",
                "current_disposition",
                "ever_success",
            ])
            for lf in sorted_leads:
                writer.writerow([
                    lf.workflow_id,
                    lf.external_ref,
                    lf.batch_id,
                    lf.phone_masked,
                    lf.bucket,
                    lf.total_calls,
                    lf.connected_calls,
                    lf.last_call_at_ist,
                    lf.current_disposition,
                    "true" if lf.ever_success else "false",
                ])
        return target_file

    def write_rejections(self, target_file: Path) -> Path:
        """Writes rejections.csv, sorted by workflow_id, batch_id, row_number. No PII.
        
        Header:
        workflow_id,batch_id,row_number,reason
        """
        cur = self.conn.execute(
            """
            SELECT workflow_id, batch_id, row_number, reason
            FROM lead_rejections
            ORDER BY workflow_id, batch_id, row_number ASC;
            """
        )
        rows = cur.fetchall()

        with open(target_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
            for r in rows:
                writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])
        return target_file

    def write_data_quality(self, target_file: Path) -> Path:
        """Writes data_quality.json with exact schema and durable accounting metrics."""
        # Aggregate from file_ingestion_log
        cur = self.conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN file_type = 'leads' THEN rows_total ELSE 0 END), 0) AS lead_rows_total,
                COALESCE(SUM(CASE WHEN file_type = 'leads' THEN duplicates_merged ELSE 0 END), 0) AS duplicate_lead_rows_merged,
                COALESCE(SUM(CASE WHEN file_type = 'leads' THEN field_warnings ELSE 0 END), 0) AS field_warnings,
                COALESCE(SUM(CASE WHEN file_type = 'events' THEN rows_total ELSE 0 END), 0) AS events_total,
                COALESCE(SUM(CASE WHEN file_type = 'events' THEN malformed_lines ELSE 0 END), 0) AS malformed_events,
                COALESCE(SUM(CASE WHEN file_type = 'events' THEN duplicate_events_dropped ELSE 0 END), 0) AS duplicate_events_dropped,
                COALESCE(SUM(CASE WHEN file_type = 'events' THEN unknown_dispositions ELSE 0 END), 0) AS unknown_disposition_events
            FROM file_ingestion_log;
            """
        )
        totals = cur.fetchone()

        # Rejection metrics from lead_rejections
        cur = self.conn.execute("SELECT COUNT(*) FROM lead_rejections;")
        lead_rows_rejected = cur.fetchone()[0]

        cur = self.conn.execute(
            "SELECT reason, COUNT(*) as cnt FROM lead_rejections GROUP BY reason ORDER BY reason ASC;"
        )
        rejections_by_reason = {row["reason"]: row["cnt"] for row in cur.fetchall()}

        # Dynamic orphan count reflecting final state
        orphan_events = self.event_ingestor.get_current_orphan_count()

        payload = {
            "lead_rows_total": totals["lead_rows_total"],
            "lead_rows_rejected": lead_rows_rejected,
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": totals["duplicate_lead_rows_merged"],
            "field_warnings": totals["field_warnings"],
            "events_total": totals["events_total"],
            "malformed_events": totals["malformed_events"],
            "duplicate_events_dropped": totals["duplicate_events_dropped"],
            "orphan_events": orphan_events,
            "unknown_disposition_events": totals["unknown_disposition_events"],
        }

        with open(target_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.write("\n")

        return target_file

