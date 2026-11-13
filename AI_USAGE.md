# AI Usage Disclosure

## Tools used
- **ChatGPT (OpenAI)**: Brainstorming architectural trade-offs, sanity-checking prompt phrasing, reviewing edge cases, and verifying cryptographic choices.
- **Antigravity (Google DeepMind)**: Incremental code generation, test scaffolding, and pipeline orchestration.

## What I used them for (Stage-by-Stage Workflow)
I followed a disciplined 8-stage engineering process, using AI as an assistant and technical sounding board:

- **Stage 1 (Architecture & Plan):** Used ChatGPT to brainstorm out-of-order event resolution, evaluate HMAC vs. salted SHA-256 for phone numbers, and refine the phased implementation roadmap.
- **Stage 2 (Database & Cryptography):** Scaffolding the initial SQLite schema structure with PRAGMA optimizations (WAL mode, foreign keys) and implementing AES-256-GCM AEAD encryption with fresh 12-byte nonces.
- **Stage 3 (Normalization & Validation):** Generating regex patterns for messy phone numbers, currency symbols (`₹`, `Rs`, `INR`), case-insensitive dates (`DD-Mon-YYYY`), and drafting isolated test cases.
- **Stage 4 (Dynamic Schema & Lead Ingestion):** Scaffolding the dynamic schema loader, header alias matching, and the `last-non-null-write-wins` dictionary merging logic.
- **Stage 5 (Event Ingestion & Ledger):** Scaffolding the streaming JSON Lines parser, global event deduplication, and handling out-of-order supervisor overrides.
- **Stage 6 (Derived Facts & Reporting Engine):** Drafting SQL queries for call initiation rules, 4-decimal connectivity rates, supervisor override precedence, and formatting the 5 output CSV/JSON reports.
- **Stage 7 (CLI & Pipeline Integration):** Building the CLI command parser and running automated end-to-end integration tests using `generate_data.py` on fresh random seeds.
- **Stage 8 (Documentation & Security Review):** Structuring `SUBMISSION.md`, auditing code for PII leakage, and drafting production scaling recommendations.

## What I wrote or decided myself
- **Decoupled Append-Only Ledger:** Separated immutable event ingestion (`interaction_ledger`) from mutable call-to-phone mappings (`call_lead_association`), ensuring zero mutations on historical events when orphans resolve.
- **Composite Call Identity:** Enforced `(workflow_id, call_id)` as the primary key for calls and foreign key in the ledger rather than assuming globally unique `call_id`s across tenants/workflows.
- **Strict PII Encryption Standard:** Standardized strictly on AES-256-GCM authenticated encryption with 96-bit random nonces, separating `HMAC_KEY` (for phone blind indexing) and `ENCRYPTION_KEY` (for data confidentiality).
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
