# AI Usage Disclosure

## Tools used
- **ChatGPT (OpenAI)**: Brainstorming architectural trade-offs, sanity-checking prompt phrasing, reviewing edge cases, and verifying cryptographic choices.
- **Antigravity (Google DeepMind)**: Incremental code generation, test scaffolding, and pipeline orchestration.

## What I used them for
- Brainstorming edge-case handling for out-of-order event delivery (e.g., how supervisor overrides without phone numbers can link to leads).
- Drafting unit test cases for complex regex parsing (dates, currencies, phones).
- Scaffolding the initial SQLite schema structure and transaction context managers.
- Reviewing cryptographic trade-offs between Fernet and standard AES-256-GCM AEAD.

## What I wrote or decided myself
- **Decoupled Append-Only Ledger:** Separated immutable event ingestion (`interaction_ledger`) from mutable call-to-phone mappings (`call_lead_association`), ensuring zero mutations on historical events when orphans resolve.
- **Composite Call Identity:** Enforced `(workflow_id, call_id)` as the primary key for calls and foreign key in the ledger rather than assuming globally unique `call_id`s across tenants/workflows.
- **Strict PII Encryption Standard:** Standardized on AES-256-GCM authenticated encryption with 96-bit random nonces, separating `HMAC_KEY` (for phone blind indexing) and `ENCRYPTION_KEY` (for data confidentiality).
- **Explicit Conflict Resolution:** Added explicit checks in call association to prevent silent overwrites if conflicting non-null phone numbers arrive for the same call ID.
- **Durable File Ingestion Accounting:** Designed `file_ingestion_log` with atomic single-transaction boundaries so file metrics never drift from table states.

## Where the AI was wrong or unhelpful
- **Mutable Ledger Mutation:** The initial AI draft suggested updating `is_orphan` directly on the `interaction_ledger` table when a lead arrived. I rejected this because it broke the append-only guarantee; I directed it to derive orphan status dynamically via SQL joins.
- **Ambiguous Cryptography Definition:** The initial plan mentioned "AES-GCM / Fernet" interchangeably. I pointed out that AES-GCM and Fernet are distinct encryption mechanisms and mandated standard AES-256-GCM.
- **Rejection Collision:** The early rejection table assumed batch names were globally unique across all drops. I corrected this to `UNIQUE(drop_id, relative_path, row_number)` to avoid collision across drops sharing batch names.

## How I verified AI-generated code
- **Exhaustive Automated Testing:** Ran 74 automated unit and integration tests with `pytest` covering phone parsing, impossible dates, currency prefixes, duplicate lead merging, out-of-order overrides, and transaction rollbacks.
- **Seed Validation:** Tested the pipeline using the actual synthetic data generator (`generate_data.py`) with fresh seeds (`seed=789` and `seed=101`), confirming byte-for-byte report reproducibility.
- **Line-by-Line Code Review:** Manually inspected SQL queries, transaction boundaries, timezone conversions, and cryptographic nonces.
