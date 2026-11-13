"""Lead file ingestion engine: CSV parsing, schema validation, rejections, and last-non-null duplicate merging."""
from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solution.crypto import encrypt_data, decrypt_json, hash_phone, mask_phone
from solution.database import transaction
from solution.normalization import (
    is_blank_row,
    normalize_date,
    normalize_decimal,
    normalize_enum,
    normalize_phone,
)
from solution.schemas import SchemaRegistry, WorkflowSchema


def compute_file_hash(file_path: Path) -> str:
    """Calculates SHA-256 hash of a file for change detection."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


class LeadIngestor:
    """Processes lead CSV files against dynamic workflow schemas."""

    def __init__(self, conn: sqlite3.Connection, schema_registry: SchemaRegistry):
        self.conn = conn
        self.schema_registry = schema_registry

    def ingest_lead_file(
        self,
        file_path: Path,
        drop_id: str,
        relative_path: str,
        workflow_id: str,
    ) -> dict[str, int]:
        """Ingests a single lead CSV file idempotently within an ACID transaction."""
        file_hash = compute_file_hash(file_path)

        # Idempotency check: if file already ingested with identical content hash, skip safely
        cur = self.conn.execute(
            "SELECT file_hash FROM file_ingestion_log WHERE drop_id = ? AND relative_path = ?;",
            (drop_id, relative_path),
        )
        existing = cur.fetchone()
        if existing and existing["file_hash"] == file_hash:
            return {"status": "skipped", "reason": "already_ingested"}

        schema = self.schema_registry.get_schema(workflow_id)
        batch_id = file_path.stem  # e.g. "batch_001.csv" -> "batch_001"
        now_iso = datetime.now(timezone.utc).isoformat()

        rows_total = 0
        rows_rejected = 0
        duplicates_merged = 0
        field_warnings_total = 0

        rejections_to_insert = []
        leads_to_create = []
        leads_to_update = []

        with open(file_path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            try:
                raw_header = next(reader)
            except StopIteration:
                # Empty file
                raw_header = []

            # Map raw header column indices to FieldDefinitions
            col_to_field = {}
            for idx, col_name in enumerate(raw_header):
                matched = schema.match_header_to_field(col_name)
                if matched:
                    col_to_field[idx] = matched

            row_number = 0
            for raw_row in reader:
                # Specification rule: "Blank rows (every cell empty or whitespace) are skipped and not counted."
                if is_blank_row(raw_row):
                    continue

                # Specification rule: "Row numbers count non-blank data rows within a file, starting at 1. The header is not counted."
                row_number += 1
                rows_total += 1

                # Build row mapping from column indices
                raw_values = {}
                for idx, cell_val in enumerate(raw_row):
                    if idx in col_to_field:
                        field_def = col_to_field[idx]
                        raw_values[field_def.name] = cell_val

                # Validate row fields in strict schema order
                row_rejected = False
                row_rejection_reason = ""
                clean_values: dict[str, Any] = {}
                row_warnings = 0

                for field_def in schema.fields:
                    val = raw_values.get(field_def.name)
                    trimmed = val.strip() if val is not None else ""
                    is_missing = (trimmed == "")

                    if is_missing:
                        if field_def.required:
                            # Rule: "a required field is missing -> missing_required:<field_name>"
                            row_rejected = True
                            row_rejection_reason = f"missing_required:{field_def.name}"
                            break
                        else:
                            clean_values[field_def.name] = None
                            continue

                    # Value is present -> parse according to field type
                    try:
                        if field_def.type == "string":
                            clean_values[field_def.name] = trimmed
                        elif field_def.type == "phone":
                            # normalize_phone returns canonical +91XXXXXXXXXX or raises ValueError
                            clean_values[field_def.name] = normalize_phone(trimmed)
                        elif field_def.type == "date":
                            clean_values[field_def.name] = normalize_date(trimmed)
                        elif field_def.type == "decimal":
                            clean_values[field_def.name] = normalize_decimal(trimmed)
                        elif field_def.type == "enum":
                            clean_values[field_def.name] = normalize_enum(trimmed, field_def.values)
                        else:
                            clean_values[field_def.name] = trimmed
                    except ValueError:
                        # Validation failed for present value
                        if field_def.type == "phone":
                            # Rule: "the phone is present but invalid -> invalid_phone"
                            row_rejected = True
                            row_rejection_reason = "invalid_phone"
                            break
                        elif field_def.required:
                            # Other required field invalid (e.g. invalid required string or format)
                            row_rejected = True
                            row_rejection_reason = f"missing_required:{field_def.name}"
                            break
                        else:
                            # Rule: "Optional field present but invalid -> store null and count one field warning."
                            clean_values[field_def.name] = None
                            row_warnings += 1

                if row_rejected:
                    rows_rejected += 1
                    rejections_to_insert.append(
                        (
                            workflow_id,
                            batch_id,
                            drop_id,
                            relative_path,
                            row_number,
                            row_rejection_reason,
                            now_iso,
                        )
                    )
                    continue

                # Non-rejected row: accumulate warnings
                # Rule: "Count warnings only for rows that are not rejected."
                field_warnings_total += row_warnings

                # Lead identity: (workflow_id, canonical phone)
                canonical_phone = clean_values["phone"]
                phone_h = hash_phone(canonical_phone)
                phone_m = mask_phone(canonical_phone)
                ext_ref = clean_values[schema.reference_field_name]

                # Separate PII and non-PII fields
                pii_data = {f.name: clean_values[f.name] for f in schema.fields if f.pii}
                non_pii_data = {f.name: clean_values[f.name] for f in schema.fields if not f.pii}

                # Check for existing lead for duplicate merging
                cur = self.conn.execute(
                    "SELECT batch_id, encrypted_pii_json, attributes_json FROM leads WHERE workflow_id = ? AND phone_hash = ?;",
                    (workflow_id, phone_h),
                )
                existing_lead = cur.fetchone()

                if existing_lead is None:
                    # New lead: first write locks batch_id permanently
                    enc_pii = encrypt_data(pii_data)
                    attr_json = json.dumps(non_pii_data, ensure_ascii=False)
                    self.conn.execute(
                        """
                        INSERT INTO leads (
                            workflow_id, phone_hash, batch_id, phone_masked,
                            external_ref, encrypted_pii_json, attributes_json,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        (
                            workflow_id,
                            phone_h,
                            batch_id,
                            phone_m,
                            ext_ref,
                            enc_pii,
                            attr_json,
                            now_iso,
                            now_iso,
                        ),
                    )
                else:
                    # Duplicate lead: last-non-null-write-wins merging
                    # Permanent batch_id remains unchanged
                    duplicates_merged += 1
                    old_attr = json.loads(existing_lead["attributes_json"])
                    old_pii = decrypt_json(existing_lead["encrypted_pii_json"])

                    # Merge non-null fields
                    for k, v in non_pii_data.items():
                        if v is not None:
                            old_attr[k] = v

                    for k, v in pii_data.items():
                        if v is not None:
                            old_pii[k] = v

                    updated_ext_ref = old_attr.get(schema.reference_field_name, ext_ref)
                    new_enc_pii = encrypt_data(old_pii)
                    new_attr_json = json.dumps(old_attr, ensure_ascii=False)

                    self.conn.execute(
                        """
                        UPDATE leads
                        SET phone_masked = ?, external_ref = ?,
                            encrypted_pii_json = ?, attributes_json = ?,
                            updated_at = ?
                        WHERE workflow_id = ? AND phone_hash = ?;
                        """,
                        (
                            phone_m,
                            updated_ext_ref,
                            new_enc_pii,
                            new_attr_json,
                            now_iso,
                            workflow_id,
                            phone_h,
                        ),
                    )

        # Batch insert rejections
        if rejections_to_insert:
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO lead_rejections (
                    workflow_id, batch_id, drop_id, relative_path,
                    row_number, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                rejections_to_insert,
            )

        # Record file ingestion audit log in the same transaction
        self.conn.execute(
            """
            INSERT OR REPLACE INTO file_ingestion_log (
                drop_id, relative_path, file_type, file_hash,
                rows_total, rows_rejected, duplicates_merged,
                field_warnings, malformed_lines, duplicate_events_dropped,
                unknown_dispositions, ingested_at
            ) VALUES (?, ?, 'leads', ?, ?, ?, ?, ?, 0, 0, 0, ?);
            """,
            (
                drop_id,
                relative_path,
                file_hash,
                rows_total,
                rows_rejected,
                duplicates_merged,
                field_warnings_total,
                now_iso,
            ),
        )

        return {
            "status": "success",
            "rows_total": rows_total,
            "rows_rejected": rows_rejected,
            "duplicates_merged": duplicates_merged,
            "field_warnings": field_warnings_total,
        }

