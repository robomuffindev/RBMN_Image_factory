"""CSV / JSON batch parser + validator.

Accepts:
    - CSV with header row (required column: ``prompt``)
    - JSON with optional ``project_name``, ``defaults``, and ``items[]``.

Returns a normalized ``ParsedBatch`` ready for the Preview tab.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger

_log = get_logger("factory.batch")

RECOGNIZED_COLUMNS = {
    "prompt", "negative_prompt", "ref1", "ref2", "ref3", "ref4",
    "width", "height", "seed", "enhance", "name",
    # Scale hints — free-form text describing the subject's real-world size
    # so the model renders it in correct proportion to the surroundings.
    # All three are optional; supply any subset.
    "size", "dimensions", "relative_size",
}


# Synonyms that CSV-makers commonly type. We canonicalize to the three
# names the runner expects (size, dimensions, relative_size).
COLUMN_ALIASES = {
    "physical_size":        "size",
    "real_size":            "size",
    "actual_size":          "size",
    "physical_dimensions":  "dimensions",
    "dims":                 "dimensions",
    "scale":                "relative_size",
    "relative":             "relative_size",
}


def _scale_value(raw: dict, key: str) -> str | None:
    """Read a scale-hint column with alias support. Returns trimmed string
    or None if blank / absent."""
    if key in raw:
        v = (raw.get(key) or "").strip()
        if v:
            return v
    for alias, target in COLUMN_ALIASES.items():
        if target == key and alias in raw:
            v = (raw.get(alias) or "").strip()
            if v:
                return v
    return None


@dataclass
class BatchRow:
    prompt: str = ""
    negative_prompt: str = ""
    ref1: str = ""
    ref2: str = ""
    ref3: str = ""
    ref4: str = ""
    width: int | None = None
    height: int | None = None
    seed: int | str | None = None  # int or "random"
    enhance: bool | None = None
    name: str = ""
    # Optional scale hints — woven into the prompt by the runner before
    # submitting to ComfyUI. See app/services/framing.py:build_scale_clause.
    size: str | None = None
    dimensions: str | None = None
    relative_size: str | None = None
    _warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "ref1": self.ref1,
            "ref2": self.ref2,
            "ref3": self.ref3,
            "ref4": self.ref4,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "enhance": self.enhance,
            "name": self.name,
            "size": self.size,
            "dimensions": self.dimensions,
            "relative_size": self.relative_size,
            "_warnings": self._warnings,
        }


@dataclass
class ParsedBatch:
    project_name: str = ""
    defaults: dict[str, Any] = field(default_factory=dict)
    rows: list[BatchRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_name": self.project_name,
            "defaults": self.defaults,
            "rows": [r.to_dict() for r in self.rows],
        }


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _coerce_bool(v: Any) -> bool | None:
    if v is None or v == "":
        return None
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _coerce_seed(v: Any) -> int | str | None:
    if v is None or v == "":
        return None
    s = str(v).strip().lower()
    if s in {"random", "r"}:
        return "random"
    try:
        return int(s)
    except ValueError:
        return None


def _validate(row: BatchRow, defaults: dict[str, Any]) -> BatchRow:
    if not row.prompt or not row.prompt.strip():
        row._warnings.append("empty prompt")
    if row.width is None:
        row.width = defaults.get("width") or 1024
    if row.height is None:
        row.height = defaults.get("height") or 1024
    if row.width % 16 != 0 or row.height % 16 != 0:
        row._warnings.append(f"size {row.width}x{row.height} not divisible by 16")
    if row.enhance is None:
        row.enhance = defaults.get("enhance")
    # Reference path existence is a soft warning — paths live on ComfyUI host.
    for i, r in enumerate([row.ref1, row.ref2, row.ref3, row.ref4], start=1):
        if r and not Path(r).exists():
            row._warnings.append(f"ref{i} path not present locally (may be remote)")
    refs = [row.ref1, row.ref2, row.ref3, row.ref4]
    populated = [r for r in refs if r]
    if len(populated) > 4:
        row._warnings.append("more than 4 refs — extras ignored")
    return row


def parse_csv(content: str) -> ParsedBatch:
    batch = ParsedBatch()
    rdr = csv.DictReader(io.StringIO(content))
    if not rdr.fieldnames:
        return batch
    unknown = [c for c in rdr.fieldnames if c not in RECOGNIZED_COLUMNS]
    if unknown:
        _log.info("batch.csv.unknown_columns", columns=unknown)
    for raw in rdr:
        row = BatchRow(
            prompt=(raw.get("prompt") or "").strip(),
            negative_prompt=(raw.get("negative_prompt") or "").strip(),
            ref1=(raw.get("ref1") or "").strip(),
            ref2=(raw.get("ref2") or "").strip(),
            ref3=(raw.get("ref3") or "").strip(),
            ref4=(raw.get("ref4") or "").strip(),
            width=_coerce_int(raw.get("width")),
            height=_coerce_int(raw.get("height")),
            seed=_coerce_seed(raw.get("seed")),
            enhance=_coerce_bool(raw.get("enhance")),
            name=(raw.get("name") or "").strip(),
            size=_scale_value(raw, "size"),
            dimensions=_scale_value(raw, "dimensions"),
            relative_size=_scale_value(raw, "relative_size"),
        )
        batch.rows.append(_validate(row, batch.defaults))
    return batch


def parse_json(content: str) -> ParsedBatch:
    data = json.loads(content)
    batch = ParsedBatch()
    if isinstance(data, list):
        items = data
    else:
        batch.project_name = data.get("project_name") or ""
        batch.defaults = data.get("defaults") or {}
        items = data.get("items") or []
    for raw in items:
        row = BatchRow(
            prompt=(raw.get("prompt") or "").strip(),
            negative_prompt=(raw.get("negative_prompt") or "").strip(),
            ref1=(raw.get("ref1") or "").strip(),
            ref2=(raw.get("ref2") or "").strip(),
            ref3=(raw.get("ref3") or "").strip(),
            ref4=(raw.get("ref4") or "").strip(),
            width=_coerce_int(raw.get("width")),
            height=_coerce_int(raw.get("height")),
            seed=_coerce_seed(raw.get("seed")),
            enhance=_coerce_bool(raw.get("enhance")),
            name=(raw.get("name") or "").strip(),
            size=_scale_value(raw, "size"),
            dimensions=_scale_value(raw, "dimensions"),
            relative_size=_scale_value(raw, "relative_size"),
        )
        batch.rows.append(_validate(row, batch.defaults))
    return batch


def parse_auto(content: str, filename: str = "") -> ParsedBatch:
    name = filename.lower()
    if name.endswith(".json"):
        return parse_json(content)
    if name.endswith(".csv") or name.endswith(".tsv"):
        return parse_csv(content)
    # Try JSON first, then CSV.
    try:
        return parse_json(content)
    except Exception:  # noqa: BLE001
        return parse_csv(content)
