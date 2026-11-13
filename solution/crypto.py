"""Cryptographic operations for PII protection: HMAC blind indexing and AES-256-GCM AEAD encryption."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from solution.config import AES_ENCRYPTION_KEY, HMAC_SECRET_KEY

# AES-GCM standard nonce size is 12 bytes (96 bits)
NONCE_SIZE_BYTES = 12


def hash_phone(canonical_phone: str, key: bytes = HMAC_SECRET_KEY) -> str:
    """Computes deterministic HMAC-SHA256 hash for phone lookup and lead identity.
    
    Why HMAC instead of plain SHA-256:
    Plain hashes of 10-digit mobile numbers are easily cracked via precomputed rainbow tables
    (10 billion possible combinations). A secret key ensures hashes cannot be reverse-engineered
    from a database dump alone.
    """
    if not canonical_phone:
        raise ValueError("Cannot hash an empty phone number")
    return hmac.new(key, canonical_phone.encode("utf-8"), hashlib.sha256).hexdigest()


def mask_phone(canonical_phone: str) -> str:
    """Masks a canonical phone number (+91XXXXXXXXXX) to +91******XXXX for reporting.
    
    If the phone format differs from standard +91, masks everything except prefix and last 4 digits.
    """
    if not canonical_phone:
        return ""
    if canonical_phone.startswith("+91") and len(canonical_phone) == 13:
        return f"+91******{canonical_phone[-4:]}"
    if len(canonical_phone) >= 4:
        return f"+91******{canonical_phone[-4:]}"
    return "+91******0000"


def encrypt_data(data: str | dict[str, Any] | list[Any], key: bytes = AES_ENCRYPTION_KEY) -> str:
    """Encrypts plaintext string or JSON data using AES-256-GCM with a fresh random nonce.
    
    Returns base64-encoded string: base64(nonce_12_bytes + ciphertext_and_tag_16_bytes).
    """
    if isinstance(data, (dict, list)):
        plaintext_bytes = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    elif isinstance(data, str):
        plaintext_bytes = data.encode("utf-8")
    else:
        plaintext_bytes = str(data).encode("utf-8")

    nonce = os.urandom(NONCE_SIZE_BYTES)
    aesgcm = AESGCM(key)
    ciphertext_and_tag = aesgcm.encrypt(nonce, plaintext_bytes, None)
    return base64.b64encode(nonce + ciphertext_and_tag).decode("ascii")


def decrypt_data(ciphertext_b64: str, key: bytes = AES_ENCRYPTION_KEY) -> str:
    """Decrypts base64-encoded AES-256-GCM ciphertext, verifying integrity."""
    raw = base64.b64decode(ciphertext_b64.encode("ascii"))
    if len(raw) < NONCE_SIZE_BYTES + 16:
        raise ValueError("Ciphertext too short to contain nonce and authentication tag")
    nonce = raw[:NONCE_SIZE_BYTES]
    ciphertext_and_tag = raw[NONCE_SIZE_BYTES:]
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext_and_tag, None)
    return plaintext_bytes.decode("utf-8")


def decrypt_json(ciphertext_b64: str, key: bytes = AES_ENCRYPTION_KEY) -> Any:
    """Decrypts base64-encoded AES-256-GCM ciphertext and parses JSON payload."""
    plaintext = decrypt_data(ciphertext_b64, key)
    return json.loads(plaintext)

