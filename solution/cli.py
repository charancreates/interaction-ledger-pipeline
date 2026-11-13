"""Command-line interface for the Interaction Ledger & Reporting Pipeline."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from solution.config import DB_PATH
from solution.database import get_connection, init_db, transaction
from solution.event_ingestion import EventIngestor
from solution.lead_ingestion import LeadIngestor
from solution.reporting import ReportGenerator
from solution.schemas import SchemaRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("pipeline")


def ingest_drop(drop_dir: Path, db_path: Path = DB_PATH) -> None:
    """Ingests all leads and events from a single drop directory."""
    if not drop_dir.exists():
        logger.error("Drop directory does not exist: %s", drop_dir)
        sys.exit(1)

    drop_id = drop_dir.name  # e.g. "drop_1"
    conn = get_connection(db_path)
    init_db(conn)

    schema_reg = SchemaRegistry()
    lead_ingestor = LeadIngestor(conn, schema_reg)
    event_ingestor = EventIngestor(conn, schema_reg)

    logger.info("Starting ingestion for %s (DB: %s)", drop_id, db_path)

    # 1. Ingest Leads
    # Directory structure: <drop>/leads/<workflow_id>/<batch_id>.csv
    leads_dir = drop_dir / "leads"
    if leads_dir.exists():
        # Process files in deterministic sorted order
        for wf_dir in sorted(leads_dir.iterdir()):
            if not wf_dir.is_dir():
                continue
            wf_id = wf_dir.name
            for csv_file in sorted(wf_dir.glob("*.csv")):
                rel_path = f"leads/{wf_id}/{csv_file.name}"
                logger.info("Ingesting lead file: %s (workflow: %s)", rel_path, wf_id)
                with transaction(conn):
                    res = lead_ingestor.ingest_lead_file(csv_file, drop_id, rel_path, wf_id)
                logger.info("Lead file result: %s", res)

    # 2. Ingest Events
    # Directory structure: <drop>/events/*.jsonl
    events_dir = drop_dir / "events"
    if events_dir.exists():
        for jsonl_file in sorted(events_dir.glob("*.jsonl")):
            rel_path = f"events/{jsonl_file.name}"
            logger.info("Ingesting event file: %s", rel_path)
            with transaction(conn):
                res = event_ingestor.ingest_event_file(jsonl_file, drop_id, rel_path)
            logger.info("Event file result: %s", res)

    conn.close()
    logger.info("Completed ingestion for %s", drop_id)


def generate_reports(out_dir: Path, db_path: Path = DB_PATH) -> None:
    """Generates all 5 output reports into the destination directory."""
    conn = get_connection(db_path)
    init_db(conn)

    schema_reg = SchemaRegistry()
    report_gen = ReportGenerator(conn, schema_reg)

    logger.info("Generating reports into: %s", out_dir)
    generated = report_gen.generate_all_reports(out_dir)
    for name, path in generated.items():
        logger.info("Written %s -> %s", name, path)

    conn.close()
    logger.info("All reports successfully generated.")


def reset_database(db_path: Path = DB_PATH) -> None:
    """Resets the database by dropping existing tables and recreating schema."""
    conn = get_connection(db_path)
    logger.warning("Resetting database at %s", db_path)
    conn.executescript(
        """
        DROP TABLE IF EXISTS interaction_ledger;
        DROP TABLE IF EXISTS call_lead_association;
        DROP TABLE IF EXISTS lead_rejections;
        DROP TABLE IF EXISTS leads;
        DROP TABLE IF EXISTS file_ingestion_log;
        """
    )
    init_db(conn)
    conn.close()
    logger.info("Database reset complete.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interaction Ledger & Reporting Pipeline CLI")
    parser.add_argument("--db", type=Path, default=DB_PATH, help="Path to SQLite database file")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # 'ingest' command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop directory")
    ingest_parser.add_argument("drop_path", type=Path, help="Path to drop directory (e.g. data/drop_1)")

    # 'report' command
    report_parser = subparsers.add_parser("report", help="Generate all output reports")
    report_parser.add_argument("--out", type=Path, default=Path("reports"), help="Output directory for reports")

    # 'reset' command
    subparsers.add_parser("reset", help="Reset the database schema")

    args = parser.parse_args()

    if args.command == "ingest":
        ingest_drop(args.drop_path, args.db)
    elif args.command == "report":
        generate_reports(args.out, args.db)
    elif args.command == "reset":
        reset_database(args.db)


if __name__ == "__main__":
    main()

