"""Tests for Stage 4: Dynamic Schema Loading, Lead Ingestion, Rejections, and Merging."""
from __future__ import annotations

import csv
import json
from pathlib import Path
import pytest

from solution.crypto import decrypt_json, hash_phone
from solution.database import get_connection, init_db, transaction
from solution.lead_ingestion import LeadIngestor
from solution.schemas import SchemaRegistry


@pytest.fixture
def test_env(tmp_path: Path):
    conn = get_connection(":memory:")
    init_db(conn)
    registry = SchemaRegistry()
    ingestor = LeadIngestor(conn, registry)
    return conn, registry, ingestor, tmp_path


def test_schema_registry_loading(test_env):
    _, registry, _, _ = test_env
    assert "motor_renewal" in registry.schemas
    assert "policy_feedback" in registry.schemas

    schema = registry.get_schema("motor_renewal")
    assert schema.reference_field_name == "policy_number"
    assert "customer_name" in schema.pii_field_names
    assert "phone" in schema.pii_field_names

    disp = registry.get_disposition("motor_renewal", "renewed")
    assert disp is not None
    assert disp.is_terminal is True
    assert disp.is_success is True


def test_lead_ingestion_basic_and_aliases(test_env):
    conn, _, ingestor, tmp_path = test_env

    # Create CSV with aliased and reordered headers
    csv_file = tmp_path / "batch_001.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Language", "Insured Name", "Policy No", "Mobile No", "Expiry Date", "Premium"])
        # Row 1: Valid row
        writer.writerow(["hi", "Aarav Sharma", "MOT/2026/100001", "9876543210", "2026-11-20", "₹12,500"])
        # Blank row (should be skipped and not counted)
        writer.writerow(["", "   ", "", "", "", ""])
        # Row 2: Valid row with another phone format
        writer.writerow(["en", "Diya Patel", "MOT/2026/100002", "+91 91234 56789", "05/12/2026", "15000.50"])

    with transaction(conn):
        res = ingestor.ingest_lead_file(csv_file, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")

    assert res["status"] == "success"
    assert res["rows_total"] == 2
    assert res["rows_rejected"] == 0
    assert res["duplicates_merged"] == 0

    # Verify database contents
    cur = conn.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal';")
    leads = cur.fetchall()
    assert len(leads) == 2

    l1 = next(l for l in leads if l["external_ref"] == "MOT/2026/100001")
    assert l1["batch_id"] == "batch_001"
    assert l1["phone_masked"] == "+91******3210"

    # Verify PII is encrypted
    decrypted_pii = decrypt_json(l1["encrypted_pii_json"])
    assert decrypted_pii["customer_name"] == "Aarav Sharma"
    assert decrypted_pii["phone"] == "+919876543210"

    # Verify non-PII attributes
    attrs = json.loads(l1["attributes_json"])
    assert attrs["preferred_language"] == "hi"
    assert attrs["premium_amount"] == 12500.0


def test_rejections_and_warnings(test_env):
    conn, _, ingestor, tmp_path = test_env

    csv_file = tmp_path / "batch_002.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "policy_expiry", "preferred_language"])
        # Row 1: Missing required customer_name -> reject missing_required:customer_name
        writer.writerow(["", "9876543210", "MOT/1001", "2026-10-10", "en"])
        # Row 2: Invalid phone -> reject invalid_phone
        writer.writerow(["Kavya Nair", "5555555555", "MOT/1002", "2026-10-10", "en"])
        # Row 3: Valid row but invalid optional field (date 31/02/2026) -> field warning, not rejected!
        writer.writerow(["Rohan Iyer", "9876543211", "MOT/1003", "31/02/2026", "en"])
        # Row 4: Missing required phone -> reject missing_required:phone
        writer.writerow(["Sneha Rao", "", "MOT/1004", "2026-10-10", "en"])

    with transaction(conn):
        res = ingestor.ingest_lead_file(csv_file, "drop_1", "leads/motor_renewal/batch_002.csv", "motor_renewal")

    assert res["rows_total"] == 4
    assert res["rows_rejected"] == 3
    assert res["field_warnings"] == 1

    # Check rejections table
    cur = conn.execute("SELECT row_number, reason FROM lead_rejections ORDER BY row_number;")
    rejections = cur.fetchall()
    assert len(rejections) == 3
    assert rejections[0]["row_number"] == 1
    assert rejections[0]["reason"] == "missing_required:customer_name"
    assert rejections[1]["row_number"] == 2
    assert rejections[1]["reason"] == "invalid_phone"
    assert rejections[2]["row_number"] == 4
    assert rejections[2]["reason"] == "missing_required:phone"

    # Check the single valid lead with warning
    cur = conn.execute("SELECT external_ref, attributes_json FROM leads WHERE workflow_id = 'motor_renewal';")
    leads = cur.fetchall()
    assert len(leads) == 1
    attrs = json.loads(leads[0]["attributes_json"])
    assert attrs["policy_expiry"] is None  # invalid optional field stored as null


def test_duplicate_lead_merging(test_env):
    conn, _, ingestor, tmp_path = test_env

    # Drop 1: batch_001 creates the lead
    f1 = tmp_path / "batch_001.csv"
    with open(f1, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        writer.writerow(["Aditya Joshi", "9876543210", "MOT/2001", "10000", "en"])

    with transaction(conn):
        ingestor.ingest_lead_file(f1, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")

    # Drop 2: batch_003 updates the lead with new premium and language, but null policy_number
    f2 = tmp_path / "batch_003.csv"
    with open(f2, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number", "premium_amount", "preferred_language"])
        writer.writerow(["Aditya Joshi", "9876543210", "MOT/2001", "12000", "ta"])

    with transaction(conn):
        res2 = ingestor.ingest_lead_file(f2, "drop_2", "leads/motor_renewal/batch_003.csv", "motor_renewal")

    assert res2["duplicates_merged"] == 1

    # Verify lead in DB: batch_id must still be batch_001 (first write wins)
    cur = conn.execute("SELECT * FROM leads WHERE workflow_id = 'motor_renewal';")
    leads = cur.fetchall()
    assert len(leads) == 1
    lead = leads[0]
    assert lead["batch_id"] == "batch_001", "First write batch_id must be permanent"

    attrs = json.loads(lead["attributes_json"])
    assert attrs["premium_amount"] == 12000.0, "Last non-null write should update premium"
    assert attrs["preferred_language"] == "ta", "Last non-null write should update language"


def test_file_ingestion_idempotency(test_env):
    conn, _, ingestor, tmp_path = test_env

    f1 = tmp_path / "batch_001.csv"
    with open(f1, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["customer_name", "phone", "policy_number"])
        writer.writerow(["Nikhil Das", "9876543210", "MOT/3001"])

    with transaction(conn):
        res1 = ingestor.ingest_lead_file(f1, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")
    assert res1["status"] == "success"

    # Ingest the same file again
    with transaction(conn):
        res2 = ingestor.ingest_lead_file(f1, "drop_1", "leads/motor_renewal/batch_001.csv", "motor_renewal")
    assert res2["status"] == "skipped"

    # Verify no duplicate leads or rejections
    cur = conn.execute("SELECT COUNT(*) FROM leads;")
    assert cur.fetchone()[0] == 1

