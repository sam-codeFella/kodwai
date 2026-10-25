"""Streaming CSV access and header -> canonical field resolution.

Schema drift is resolved once per file from the header, not once per row. A file whose
header can't supply every required canonical field raises ``SchemaError`` before any
row is read; unknown extra columns are tolerated and reported.
"""
from __future__ import annotations

import csv
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Iterator, List, Tuple

from .config import CANONICAL_FIELDS, Alias, PartnerConfig


class SchemaError(Exception):
    """The file's structure is unusable (missing required columns, empty file)."""

    def __init__(self, message: str, missing: Tuple[str, ...] = ()):
        super().__init__(message)
        self.missing = missing


@dataclass(frozen=True)
class FieldBinding:
    index: int
    alias: Alias


@dataclass(frozen=True)
class ResolvedHeader:
    bindings: Dict[str, FieldBinding]
    extra_columns: List[str]
    width: int


def resolve_header(header: List[str], partner: PartnerConfig, run_date: str) -> ResolvedHeader:
    positions = {}
    for i, col in enumerate(header):
        positions.setdefault(col, i)

    bindings: Dict[str, FieldBinding] = {}
    missing = []
    for field in CANONICAL_FIELDS:
        best = None
        for alias in partner.fields[field]:
            if alias.column not in positions:
                continue
            if alias.effective_from and alias.effective_from > run_date:
                continue
            # Date-scoped aliases win over undated ones; ties go to the first listed.
            if best is None or (alias.effective_from or "") > (best.effective_from or ""):
                best = alias
        if best is None:
            missing.append(field)
        else:
            bindings[field] = FieldBinding(positions[best.column], best)

    if missing:
        wanted = {f: [a.column for a in partner.fields[f]] for f in missing}
        raise SchemaError(
            f"missing required column(s) for {missing}; accepted headers {wanted}; got {header}",
            tuple(missing),
        )
    used = {b.index for b in bindings.values()}
    extra = [c for i, c in enumerate(header) if i not in used]
    return ResolvedHeader(bindings, extra, len(header))


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


@contextmanager
def open_rows(path: str):
    """Yield ``(header, rows)`` where ``rows`` lazily yields ``(line_no, values)``.

    The file is never read into memory. utf-8-sig tolerates a BOM; decoding errors
    surface as exceptions (loud) rather than being silently replaced.
    """
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        if not header:
            raise SchemaError("file is empty (no header row)", ())
        header = [h.strip() for h in header]

        def rows() -> Iterator[Tuple[int, List[str]]]:
            for values in reader:
                if values:  # csv yields [] for blank lines
                    yield reader.line_num, values

        yield header, rows()
