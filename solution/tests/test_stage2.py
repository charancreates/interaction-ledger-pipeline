"""Tests for Stage 2: Database Schema, Integrity Constraints, and Crypto Utilities."""
from __future__ import annotations

import sqlite3
import pytest
from cryptography.exceptions import InvalidTag

from solution.crypto import (
    decrypt_data,
    decrypt_json,
    encrypt_data,
    hash_phone,
    mask_phone,
)
from solution.database import get_connection, init_db, transaction


def test_hmac_phone_hashing():
    p1 = "+919876543210"
    p2 = "+919876543211"
    h1 = hash_phone(p1)
    h2 = hash_phone(p1)
    h3 = hash_phone(p2)

    assert h1 == h2, "HMAC must be deterministic for identical inputs"
    assert h1 != h3, "HMAC must differ for different inputs"
    assert len(h1) == 64, "HMAC-SHA256 hex digest should be 64 characters"

    # Different key produces different hash
    alt_key = b"different-secret-key-salt-12345"
    h_alt = hash_phone(p1, key=alt_key)
    assert h_alt != h1, "Different secret keys must produce different hashes"


def test_phone_masking():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("+917718750036") == "+91******0036"
    assert mask_phone("") == ""


def test_aes_gcm_encryption_roundtrip():
    # String payload
    secret_text = "Rohan Sharma"
    encrypted = encrypt_data(secret_text)
    decrypted = decrypt_data(encrypted)
    assert decrypted == secret_text

    # JSON dictionary payload
    payload = {"customer_name": "Rohan Sharma", "phone": "+919876543210"}
    enc_json = encrypt_data(payload)
    dec_json = decrypt_json(enc_json)
    assert dec_json == payload

    # Nonce randomization: encrypting same plaintext twice produces different ciphertexts
    enc1 = encrypt_data(secret_text)
    enc2 = encrypt_data(secret_text)
    assert enc1 != enc2, "Each encryption must use a fresh random nonce"


def test_aes_gcm_tamper_detection():
    secret_text = "Sensitive medical or financial info"
    encrypted_b64 = encrypt_data(secret_text)

    # Tamper with base64 string
    import base64
    raw = bytearray(base64.b64decode(encrypted_b64))
    raw[-1] ^= 0x01  # Flip one bit in authentication tag
    tampered_b64 = base64.b64encode(raw).decode("ascii")

    with pytest.raises(InvalidTag):
        decrypt_data(tampered_b64)


def test_database_initialization():
    conn = get_connection(":memory:")
    init_db(conn)

    # Check tables exist
    cursor = conn.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = {row[0] for row in cursor.fetchall()}
    expected = {
        "file_ingestion_log",
        "leads",
        "lead_rejections",
        "call_lead_association",
        "interaction_ledger",
    }
    assert expected.issubset(tables)


def test_database_foreign_key_enforcement():
    conn = get_connection(":memory:")
    init_db(conn)

    # Attempting to insert an event into interaction_ledger without call_lead_association must fail
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO interaction_ledger (
                event_id, event_type, workflow_id, call_id,
                occurred_at_utc, payload_json, is_valid_disposition,
                source_drop_id, source_relative_path, created_at
            ) VALUES (
                'evt_001', 'call.initiated', 'motor_renewal', 'call_999',
                '2026-10-05T10:00:00Z', '{}', 1, 'drop_1', 'events/e1.jsonl', '2026-10-05T10:00:00Z'
            );
            """
        )

    # With call_lead_association present, it must succeed
    conn.execute(
        """
        INSERT INTO call_lead_association (
            workflow_id, call_id, phone_hash, created_at, updated_at
        ) VALUES (
            'motor_renewal', 'call_999', 'phone_hash_123', '2026-10-05T10:00:00Z', '2026-10-05T10:00:00Z'
        );
        """
    )
    conn.execute(
        """
        INSERT INTO interaction_ledger (
            event_id, event_type, workflow_id, call_id,
            occurred_at_utc, payload_json, is_valid_disposition,
            source_drop_id, source_relative_path, created_at
        ) VALUES (
            'evt_001', 'call.initiated', 'motor_renewal', 'call_999',
            '2026-10-05T10:00:00Z', '{}', 1, 'drop_1', 'events/e1.jsonl', '2026-10-05T10:00:00Z'
        );
        """
    )


def test_transaction_rollback():
    conn = get_connection(":memory:")
    init_db(conn)

    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute(
                """
                INSERT INTO call_lead_association (
                    workflow_id, call_id, phone_hash, created_at, updated_at
                ) VALUES (
                    'motor_renewal', 'call_100', 'phone_hash_100', '2026-10-05T10:00:00Z', '2026-10-05T10:00:00Z'
                );
                """
            )
            raise RuntimeError("Simulated failure inside transaction")

    # Verify rollback: record should not exist
    cur = conn.execute("SELECT COUNT(*) FROM call_lead_association WHERE call_id = 'call_100';")
    assert cur.fetchone()[0] == 0

