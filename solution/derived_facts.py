"""Derived facts computation: Call resolution, effective dispositions, and lead buckets."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from solution.normalization import IST_TIMEZONE, utc_to_ist_date, utc_to_ist_str
from solution.schemas import SchemaRegistry


@dataclass
class CallFact:
    workflow_id: str
    call_id: str
    phone_hash: str
    initiated_at_utc: datetime
    ist_date: str
    is_connected: bool
    has_telephony_failed: bool
    effective_disposition: str | None
    is_terminal: bool
    is_success: bool


@dataclass
class LeadFact:
    workflow_id: str
    phone_hash: str
    batch_id: str
    phone_masked: str
    external_ref: str
    bucket: str
    total_calls: int
    connected_calls: int
    last_call_at_ist: str
    current_disposition: str
    ever_success: bool


class DerivedFactsEngine:
    """Computes call and lead metrics strictly following specification rules."""

    def __init__(self, conn: sqlite3.Connection, schema_registry: SchemaRegistry):
        self.conn = conn
        self.schema_registry = schema_registry

    def compute_all_call_facts(self) -> dict[tuple[str, str], CallFact]:
        """Resolves all valid calls linked to valid leads.
        
        Rules:
        - A call is counted only if its call.initiated event is present and linked.
        - The call's date is the IST date of call.initiated.
        - Connected if call.connected is present.
        - Effective disposition: latest disposition.override by occurred_at;
          if none, latest disposition.set by occurred_at; ignoring invalid disposition codes.
        """
        # Fetch all linked events for calls that have a call.initiated event
        query = """
        SELECT
            e.event_id,
            e.event_type,
            e.workflow_id,
            e.call_id,
            e.occurred_at_utc,
            e.payload_json,
            e.is_valid_disposition,
            c.phone_hash
        FROM interaction_ledger e
        JOIN call_lead_association c
          ON e.workflow_id = c.workflow_id AND e.call_id = c.call_id
        JOIN leads l
          ON c.workflow_id = l.workflow_id AND c.phone_hash = l.phone_hash
        ORDER BY e.workflow_id, e.call_id, e.occurred_at_utc ASC;
        """
        cur = self.conn.execute(query)
        events_by_call: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in cur.fetchall():
            key = (row["workflow_id"], row["call_id"])
            events_by_call.setdefault(key, []).append(row)

        call_facts: dict[tuple[str, str], CallFact] = {}

        for (wf_id, cid), events in events_by_call.items():
            # A call is counted ONLY if call.initiated is present
            init_events = [e for e in events if e["event_type"] == "call.initiated"]
            if not init_events:
                continue

            init_event = init_events[0]
            phone_h = init_event["phone_hash"]
            init_dt_utc = datetime.fromisoformat(init_event["occurred_at_utc"])
            ist_date = utc_to_ist_date(init_dt_utc)

            is_connected = any(e["event_type"] == "call.connected" for e in events)
            has_telephony_failed = any(e["event_type"] == "call.telephony_failed" for e in events)

            # Effective disposition resolution:
            # Latest disposition.override by occurred_at (with is_valid_disposition = 1)
            overrides = [
                e for e in events
                if e["event_type"] == "disposition.override" and e["is_valid_disposition"] == 1
            ]
            sets = [
                e for e in events
                if e["event_type"] == "disposition.set" and e["is_valid_disposition"] == 1
            ]

            effective_disp_code = None
            if overrides:
                # Latest override by occurred_at
                latest_override = max(overrides, key=lambda x: x["occurred_at_utc"])
                payload = json.loads(latest_override["payload_json"])
                effective_disp_code = payload.get("disposition_code")
            elif sets:
                # Latest disposition.set by occurred_at
                latest_set = max(sets, key=lambda x: x["occurred_at_utc"])
                payload = json.loads(latest_set["payload_json"])
                effective_disp_code = payload.get("disposition_code")

            is_terminal = False
            is_success = False
            if effective_disp_code:
                disp_info = self.schema_registry.get_disposition(wf_id, effective_disp_code)
                if disp_info:
                    is_terminal = disp_info.is_terminal
                    is_success = disp_info.is_success

            call_facts[(wf_id, cid)] = CallFact(
                workflow_id=wf_id,
                call_id=cid,
                phone_hash=phone_h,
                initiated_at_utc=init_dt_utc,
                ist_date=ist_date,
                is_connected=is_connected,
                has_telephony_failed=has_telephony_failed,
                effective_disposition=effective_disp_code,
                is_terminal=is_terminal,
                is_success=is_success,
            )

        return call_facts

    def compute_all_lead_facts(
        self, call_facts: dict[tuple[str, str], CallFact]
    ) -> list[LeadFact]:
        """Computes metrics and bucket assignments for every lead in the database."""
        # Group calls by (workflow_id, phone_hash)
        calls_by_lead: dict[tuple[str, str], list[CallFact]] = {}
        for cf in call_facts.values():
            key = (cf.workflow_id, cf.phone_hash)
            calls_by_lead.setdefault(key, []).append(cf)

        # Query all leads
        cur = self.conn.execute(
            """
            SELECT workflow_id, phone_hash, batch_id, phone_masked, external_ref
            FROM leads
            ORDER BY workflow_id, external_ref;
            """
        )
        leads = cur.fetchall()

        lead_facts: list[LeadFact] = []
        for lead in leads:
            wf_id = lead["workflow_id"]
            phone_h = lead["phone_hash"]
            lead_calls = calls_by_lead.get((wf_id, phone_h), [])

            total_calls = len(lead_calls)
            connected_calls = sum(1 for c in lead_calls if c.is_connected)

            # Bucket resolution: first matching rule
            # 1. yet_to_reach_out: no calls
            # 2. completed: any call's effective disposition is terminal
            # 3. in_progress_telephony_failed: every call has a call.telephony_failed event
            # 4. in_progress_dialed: everything else
            if total_calls == 0:
                bucket = "yet_to_reach_out"
            elif any(c.is_terminal for c in lead_calls):
                bucket = "completed"
            elif all(c.has_telephony_failed for c in lead_calls):
                bucket = "in_progress_telephony_failed"
            else:
                bucket = "in_progress_dialed"

            # last_call_at: latest call.initiated time as ISO-8601 in IST
            if total_calls > 0:
                latest_call = max(lead_calls, key=lambda c: c.initiated_at_utc)
                last_call_at_ist = utc_to_ist_str(latest_call.initiated_at_utc)
            else:
                last_call_at_ist = ""

            # current_disposition: effective disposition of the most recent call
            # (by initiated time) that has one. Empty if none.
            calls_with_disp = [c for c in lead_calls if c.effective_disposition]
            if calls_with_disp:
                most_recent_disp_call = max(calls_with_disp, key=lambda c: c.initiated_at_utc)
                current_disp = most_recent_disp_call.effective_disposition or ""
            else:
                current_disp = ""

            # ever_success: whether any call's effective disposition has is_success=true
            ever_success = any(c.is_success for c in lead_calls)

            lead_facts.append(
                LeadFact(
                    workflow_id=wf_id,
                    phone_hash=phone_h,
                    batch_id=lead["batch_id"],
                    phone_masked=lead["phone_masked"],
                    external_ref=lead["external_ref"],
                    bucket=bucket,
                    total_calls=total_calls,
                    connected_calls=connected_calls,
                    last_call_at_ist=last_call_at_ist,
                    current_disposition=current_disp,
                    ever_success=ever_success,
                )
            )

        return lead_facts

