"""Partner registry loading and validation.

All partner-specific knowledge lives in the JSON config: which file belongs to a run
date, which header names map to which canonical field, how each is parsed. Adding a
partner is a config entry, not a code change.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .parsers import PARSERS, parse_tz

CANONICAL_FIELDS = ("order_id", "customer_email", "amount", "currency", "placed_at")

# Used when an alias doesn't name a parser. Amount units are deliberately not
# guessed from values, so config should say decimal_major vs minor_int explicitly.
DEFAULT_PARSERS = {
    "order_id": "text",
    "customer_email": "email",
    "amount": "decimal_major",
    "currency": "currency",
    "placed_at": "iso8601",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Alias:
    """One header name that can feed a canonical field."""

    column: str
    parser: str
    format: Optional[str] = None
    # Same header name, different meaning from this date on (e.g. units changed).
    effective_from: Optional[str] = None


@dataclass(frozen=True)
class PartnerConfig:
    name: str
    file_glob: str
    timezone: object
    fields: Dict[str, Tuple[Alias, ...]]


def _parse_alias(partner: str, field: str, raw) -> Alias:
    if isinstance(raw, str):
        raw = {"column": raw}
    if not isinstance(raw, dict) or "column" not in raw:
        raise ConfigError(f"{partner}.{field}: alias must be a string or an object with 'column'")
    unknown = set(raw) - {"column", "parser", "format", "effective_from"}
    if unknown:
        raise ConfigError(f"{partner}.{field}: unknown alias keys {sorted(unknown)}")
    parser = raw.get("parser", DEFAULT_PARSERS[field])
    if parser not in PARSERS:
        raise ConfigError(f"{partner}.{field}: unknown parser {parser!r} (known: {sorted(PARSERS)})")
    if parser == "datetime_fmt" and not raw.get("format"):
        raise ConfigError(f"{partner}.{field}: parser 'datetime_fmt' needs a 'format'")
    eff = raw.get("effective_from")
    if eff is not None:
        try:
            date.fromisoformat(eff)
        except ValueError:
            raise ConfigError(f"{partner}.{field}: effective_from {eff!r} is not YYYY-MM-DD")
    return Alias(raw["column"].strip(), parser, raw.get("format"), eff)


def load_config(path: str) -> Dict[str, PartnerConfig]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"cannot read config {path}: {e}")
    partners = doc.get("partners")
    if not isinstance(partners, dict) or not partners:
        raise ConfigError("config needs a non-empty 'partners' object")

    out: Dict[str, PartnerConfig] = {}
    for name, p in partners.items():
        glob = p.get("file_glob")
        if not glob or "{date}" not in glob:
            raise ConfigError(f"{name}: 'file_glob' is required and must contain {{date}}")
        try:
            tz = parse_tz(p.get("timezone", "UTC"))
        except ValueError as e:
            raise ConfigError(f"{name}: {e}")
        fields: Dict[str, Tuple[Alias, ...]] = {}
        for field in CANONICAL_FIELDS:
            aliases: List = p.get("fields", {}).get(field)
            if not aliases:
                raise ConfigError(f"{name}: no aliases configured for required field '{field}'")
            fields[field] = tuple(_parse_alias(name, field, a) for a in aliases)
        extra = set(p.get("fields", {})) - set(CANONICAL_FIELDS)
        if extra:
            raise ConfigError(f"{name}: unknown canonical fields {sorted(extra)}")
        out[name] = PartnerConfig(name, glob, tz, fields)
    return out
