"""Configuration management for the ledger pipeline."""
from __future__ import annotations

import os
from pathlib import Path

# Base paths
SOLUTION_DIR = Path(__file__).resolve().parent
REPO_ROOT = SOLUTION_DIR.parent
SCHEMAS_DIR = REPO_ROOT / "assignment-a-data" / "schemas"

# Database path (default to data/ledger.db, overrideable via environment)
DEFAULT_DB_PATH = REPO_ROOT / "data" / "ledger.db"
DB_PATH = Path(os.getenv("LEDGER_DB_PATH", str(DEFAULT_DB_PATH)))

# Secret keys for PII protection (kept outside the database)
# In production, these are injected via environment variables or secret manager (e.g. AWS KMS / HashiCorp Vault)
# Default keys provided for reproducible local development and testing
HMAC_SECRET_KEY = os.getenv("HMAC_SECRET_KEY", "agentic-universe-hmac-secret-salt-2026").encode("utf-8")

# AES-256 requires a 32-byte key (64 hex characters)
# Default test key: 32 bytes of deterministic entropy
DEFAULT_AES_KEY_HEX = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
AES_ENCRYPTION_KEY_HEX = os.getenv("AES_ENCRYPTION_KEY_HEX", DEFAULT_AES_KEY_HEX)
AES_ENCRYPTION_KEY = bytes.fromhex(AES_ENCRYPTION_KEY_HEX)

