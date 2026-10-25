"""Row -> canonical record, or ``RowReject``."""
from __future__ import annotations

import json
from typing import Dict, List

from .parsers import PARSERS, Ctx, RowReject
from .reader import ResolvedHeader

# Currency first: parsing the amount needs its minor-unit exponent.
_ORDER = ("order_id", "customer_email", "currency", "amount", "placed_at")


def normalize_row(values: List[str], resolved: ResolvedHeader, tz) -> Dict[str, object]:
    if len(values) != resolved.width:
        raise RowReject("wrong_column_count")
    ctx = Ctx(tz)
    out: Dict[str, object] = {}
    for field in _ORDER:
        binding = resolved.bindings[field]
        raw = values[binding.index].strip()
        if raw == "":
            raise RowReject(f"missing_{field}")
        out[field] = PARSERS[binding.alias.parser](raw, binding.alias, ctx)
        if field == "currency":
            ctx.currency = out[field]
    return {
        "order_id": out["order_id"],
        "customer_email": out["customer_email"],
        "amount_minor": out["amount"],
        "currency": out["currency"],
        "placed_at": out["placed_at"],
    }


def raw_row_json(header: List[str], values: List[str]) -> str:
    """The original row exactly as received, keyed by the partner's own header names."""
    if len(values) == len(header):
        return json.dumps(dict(zip(header, values)), ensure_ascii=False)
    return json.dumps({"__header__": header, "__values__": values}, ensure_ascii=False)
