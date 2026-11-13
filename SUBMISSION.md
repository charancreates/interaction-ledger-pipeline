# Submission

## Candidate
- **Name:** Charan R
- **Assignment chosen:** A (Data: Interaction Ledger & Reporting Pipeline)
- **Time spent (honest estimate):** 5h

## How to run
```bash
# 1. Environment setup from clean clone
python -m venv .venv
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
# .\.venv\Scripts\Activate.ps1

pip install -r requirements.txt

# 2. Run automated test suite (74 tests)
pytest solution/tests/ -v

# 3. Generate sample data (if needed)
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# 4. Ingest drops sequentially
python -m solution.cli ingest data/drop_1
python -m solution.cli ingest data/drop_2

# 5. Generate all 5 contract reports
python -m solution.cli report --out reports/
```

## Design
The pipeline adopts an **append-only ledger** architecture with decoupled entity linkage:

```
[Raw Event Stream]
       │
       ▼ (Deduplicated via event_id & validated)
┌─────────────────────────────────────────┐
│     interaction_ledger (Append-Only)    │
│  event_id, call_id, event_type, payload │  <-- STRICTLY IMMUTABLE
└─────────────────────────────────────────┘
       │
       ▼ (Links call to phone hash)
┌─────────────────────────────────────────┐
│         call_lead_association           │
│  (workflow_id, call_id) -> phone_hash   │  <-- Composite PK; handles overrides
└─────────────────────────────────────────┘
       │ (Dynamic Join at query/report time)
       ▼
┌─────────────────────────────────────────┐
│                 leads                   │
│  (workflow_id, phone_hash) -> lead data │  <-- AES-256-GCM encrypted PII
└─────────────────────────────────────────┘
```

### Key Database Tables:
1. **`file_ingestion_log`:** Primary key `(drop_id, relative_path)`. Tracks content hashes (`file_hash`) and durable counts (`rows_total`, `malformed_lines`, `duplicates_merged`, etc.). Commits in the same transaction as data writes to ensure atomic idempotency.
2. **`leads`:** Primary key `(workflow_id, phone_hash)` where `phone_hash` is an HMAC-SHA256 blind index. Stores `batch_id` permanently (first-seen batch stem), masked phone, encrypted PII (`encrypted_pii_json` using AES-256-GCM), and non-PII attributes (`attributes_json`).
3. **`lead_rejections`:** Unique constraint on `(drop_id, relative_path, row_number)`. Records rejected rows with exact single reasons without storing any PII.
4. **`call_lead_association`:** Primary key `(workflow_id, call_id)`. Maps calls to `phone_hash`. When an override arrives before call events, `phone_hash` is initialized to `NULL` and populated once carrier call events arrive.
5. **`interaction_ledger`:** Immutable event log with composite foreign key `(workflow_id, call_id)`. Preserves telephony telemetry. Overrides and invalid dispositions are recorded without modifying historical records.

## Decisions & assumptions

| Decision | Why | Alternative considered |
|---|---|---|
| **Decoupled Linkage vs. Mutating Ledger** | Resolves out-of-order events arriving before leads and overrides arriving before calls via dynamic SQL joins without mutating ledger rows. | Updating an `is_orphan` column on ledger rows (violates append-only immutability). |
| **HMAC-SHA256 for Phone Lookups** | 10-digit mobile numbers have a search space of $\approx 10^{10}$, making unkeyed SHA-256 vulnerable to rainbow tables. Keyed HMAC prevents offline rainbow table attacks. | Plain SHA-256 or reversible deterministic encryption. |
| **AES-256-GCM for PII** | NIST-standard AEAD cipher providing confidentiality and cryptographic tamper-detection via authentication tags with 12-byte random nonces. | Fernet (AES-128-CBC + HMAC) or unauthenticated AES-CBC. |
| **Composite Call Key `(workflow_id, call_id)`** | Telephony systems may scope call IDs per tenant/carrier or recycle sequences. Composite key guarantees strict tenant isolation. | Globally unique `call_id`. |
| **Rejection Uniqueness via `(drop_id, relative_path, row_number)`** | Different drops may contain identically named batches (`batch_001.csv`). Scoping uniqueness prevents cross-drop collisions. | Global `(batch_id, row_number)` constraint. |
| **Retaining Unknown Dispositions with `is_valid_disposition = 0`** | Voice agent telemetry must be preserved for telephony audits, but excluded from business effective disposition aggregations. | Dropping unknown disposition events before inserting into ledger. |
| **Dynamic Orphan Counting** | Orphan count reflects the true final state at report time, automatically resolving when late leads arrive in later drops. | Static counters incremented/decremented across files (subject to drift). |

## Failure modes considered
- **Out-of-order drops:** Call events arriving in `drop_1` before their lead arrives in `drop_2` are retained in the ledger as orphans and automatically resolve when the lead is ingested.
- **Out-of-order supervisor overrides:** Overrides arriving without phone numbers register `(workflow_id, call_id)` with `phone_hash = NULL`. Subsequent call events backfill the phone hash.
- **Conflicting telephony phone numbers:** If a later event emits a different non-null phone number for an existing `(workflow_id, call_id)`, the conflict is logged and the initial association is preserved (never silently overwritten).
- **Process crash mid-file:** Every file's processing (leads/events, rejections, and audit log) executes inside an atomic `BEGIN IMMEDIATE ... COMMIT` transaction. Incomplete writes roll back completely.
- **Re-running ingestion:** Checking `(drop_id, relative_path)` and SHA-256 content hashes guarantees clean idempotency without duplicate counts or rows.
- **Known limitation:** If two distinct files in different drops emit identical event IDs with different payloads, the second is dropped as a duplicate based on global `event_id` uniqueness per specification.

## Testing
- **Test Suite:** 74 automated tests executed with `pytest solution/tests/ -v`.
- **Unit Tests:**
  - Phone normalisation across all 6 raw formats and invalid variants.
  - Gregorian calendar date parsing and impossible dates (Feb 31, non-leap Feb 29).
  - Currency prefixes (`₹`, `Rs`, `INR`) and decimal rounding.
  - Case-insensitive enum matching.
  - Header alias matching and blank row filtering.
  - AES-256-GCM tamper detection and random nonce uniqueness.
- **Integration Tests:**
  - Lead rejections and optional field warning accounting.
  - Duplicate lead merging (`last-non-null-write-wins` and first batch ownership).
  - Global event deduplication and append-only ledger immutability.
  - Supervisor override precedence over AI dispositions.
  - Dynamic orphan resolution when leads arrive after events.
  - Full pipeline verification against `generate_data.py` on fresh seeds (`seed=789` and `seed=101`).

## Results
- **Timings (`--scale small`):**
  - Drop 1 (Leads + Events): ~0.8s
  - Drop 2 (Leads + Events): ~0.7s
  - Report Generation (All 5 files): ~0.15s
  - **Total Pipeline Execution:** Under 2 seconds.
- **Observations in Data:**
  - The generator produces out-of-order overrides, duplicate events crossing into later drops, and unlinked call events to numbers never uploaded.
  - All orphans resolve deterministically, and final orphan counts match expected unlinked telephony calls.

## If I had another day
If extending this pipeline for a production environment handling 100k+ leads/day and millions of call events:

1. **Dead-Letter Queue (DLQ) for Malformed Events:**
   - Rather than dropping malformed JSONL lines, stream them to a dead-letter S3/GCS bucket or Kafka DLQ topic with alert metrics so telephony webhook payloads can be debugged without data loss.
2. **Chunked Bulk Ingestion & Batch Upserts:**
   - Instead of per-row queries, use chunked staging tables (`CREATE TEMP TABLE ...`) and bulk SQL upserts (`INSERT INTO ... ON CONFLICT DO UPDATE`), significantly accelerating ingest throughput on 500k+ event drops.
3. **Incremental Pre-Aggregated Summary Rollups:**
   - Currently, reports are generated by querying the entire dataset at report time. In production, we would maintain a lightweight daily rollup table (`workflow_id, date, total_calls, connected_calls, success_lead_ids`) updated incrementally on each drop, enabling sub-second dashboard reporting regardless of ledger size.
4. **Envelope Encryption & Secret Manager Integration:**
   - Replace static environment variables with cloud-native key management (AWS KMS or HashiCorp Vault), using Data Encryption Keys (DEKs) that rotate periodically with envelope encryption.
5. **PostgreSQL Partitioning:**
   - Migrate from SQLite to PostgreSQL with declarative range partitioning on `interaction_ledger` by `occurred_at_utc` (e.g., monthly partitions) to enable fast vacuuming and cold-storage archiving of historical campaign logs.
