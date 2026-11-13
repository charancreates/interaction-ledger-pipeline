"""Dynamic schema loading and disposition catalogue management."""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from solution.config import SCHEMAS_DIR
from solution.normalization import normalize_header


@dataclass(frozen=True)
class FieldDefinition:
    name: str
    type: str  # 'string', 'phone', 'date', 'decimal', 'enum'
    required: bool = False
    pii: bool = False
    reference: bool = False
    default: Any = None
    values: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)

    @property
    def normalized_aliases(self) -> set[str]:
        names = {normalize_header(self.name)}
        for a in self.aliases:
            names.add(normalize_header(a))
        return names


@dataclass(frozen=True)
class WorkflowSchema:
    workflow_id: str
    description: str
    fields: list[FieldDefinition]

    @property
    def reference_field_name(self) -> str:
        for f in self.fields:
            if f.reference:
                return f.name
        raise ValueError(f"Schema for workflow '{self.workflow_id}' has no field marked reference=True")

    @property
    def pii_field_names(self) -> set[str]:
        return {f.name for f in self.fields if f.pii}

    def match_header_to_field(self, raw_header: str) -> FieldDefinition | None:
        norm = normalize_header(raw_header)
        for f in self.fields:
            if norm in f.normalized_aliases:
                return f
        return None


@dataclass(frozen=True)
class DispositionInfo:
    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


class SchemaRegistry:
    """Loads and caches workflow schemas and the disposition catalogue."""

    def __init__(self, schemas_dir: Path | str = SCHEMAS_DIR):
        self.schemas_dir = Path(schemas_dir)
        self.schemas: dict[str, WorkflowSchema] = {}
        self.dispositions: dict[tuple[str, str], DispositionInfo] = {}  # (workflow_id, code) -> DispositionInfo
        self.load_all()

    def load_all(self) -> None:
        # Load workflow json schemas
        for json_file in self.schemas_dir.glob("*.json"):
            with open(json_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            wf_id = data["workflow_id"]
            fields = []
            for fd in data.get("fields", []):
                fields.append(
                    FieldDefinition(
                        name=fd["name"],
                        type=fd["type"],
                        required=fd.get("required", False),
                        pii=fd.get("pii", False),
                        reference=fd.get("reference", False),
                        default=fd.get("default", None),
                        values=fd.get("values", []),
                        aliases=fd.get("aliases", []),
                    )
                )
            self.schemas[wf_id] = WorkflowSchema(
                workflow_id=wf_id,
                description=data.get("description", ""),
                fields=fields,
            )

        # Load disposition catalogue
        disp_csv = self.schemas_dir / "dispositions.csv"
        if disp_csv.exists():
            with open(disp_csv, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    is_term = row["is_terminal"].strip().lower() == "true"
                    is_succ = row["is_success"].strip().lower() == "true"
                    self.dispositions[(wf, code)] = DispositionInfo(
                        workflow_id=wf,
                        code=code,
                        label=row.get("label", "").strip(),
                        is_terminal=is_term,
                        is_success=is_succ,
                    )

    def get_schema(self, workflow_id: str) -> WorkflowSchema:
        if workflow_id not in self.schemas:
            raise KeyError(f"No schema found for workflow: {workflow_id}")
        return self.schemas[workflow_id]

    def get_disposition(self, workflow_id: str, code: str) -> DispositionInfo | None:
        return self.dispositions.get((workflow_id, code))

