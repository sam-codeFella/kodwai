"""Field parsers.

Every parser has the signature ``parse(raw, spec, ctx)`` and returns the canonical
value, or raises ``RowReject(reason)``. ``raw`` is already stripped and non-empty.
``spec`` is the config alias that selected the parser (carries e.g. ``format``).

To support a new partner quirk that config can't express, add a function here and
register it in ``PARSERS``; config then refers to it by name.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Optional


class RowReject(Exception):
    """A single row cannot be normalized; it goes to ``rejects`` with this reason."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class Ctx:
    """Per-row state shared between parsers (currency is needed to parse amounts)."""

    tz: tzinfo
    currency: Optional[str] = None


# ISO 4217 minor-unit exponents that are not the default of 2.
CURRENCY_EXPONENT = {"JPY": 0, "KRW": 0, "VND": 0, "BHD": 3, "KWD": 3}
# Stay far inside SQLite's signed 64-bit INTEGER.
MAX_MINOR = 10**15

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")
_TZ_RE = re.compile(r"^UTC(?:([+-])(\d{2}):(\d{2}))?$")


def parse_tz(name: str) -> tzinfo:
    """'UTC' or a fixed offset such as 'UTC+05:30' (stdlib only, no tz database)."""
    m = _TZ_RE.match(name.strip())
    if not m:
        raise ValueError(f"unsupported timezone {name!r}; use 'UTC' or 'UTC+HH:MM'")
    if m.group(1) is None:
        return timezone.utc
    delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return timezone(delta if m.group(1) == "+" else -delta)


def parse_text(raw, spec, ctx):
    return raw


def parse_email(raw, spec, ctx):
    if not _EMAIL_RE.match(raw):
        raise RowReject("invalid_email")
    return raw.lower()


def parse_currency(raw, spec, ctx):
    if not _CURRENCY_RE.match(raw):
        raise RowReject("invalid_currency")
    return raw.upper()


def _to_decimal(raw: str) -> Decimal:
    try:
        d = Decimal(raw)
    except InvalidOperation:
        raise RowReject("invalid_amount")
    if not d.is_finite():
        raise RowReject("invalid_amount")
    return d


def _check_minor(minor: Decimal) -> int:
    if minor < 0:
        raise RowReject("negative_amount")
    if minor >= MAX_MINOR:
        raise RowReject("invalid_amount")
    return int(minor)


def parse_decimal_major(raw, spec, ctx):
    """'42.50' / '42.5' / '42' in major units -> integer minor units. Never uses float."""
    exponent = CURRENCY_EXPONENT.get(ctx.currency, 2)
    minor = _to_decimal(raw).scaleb(exponent)
    if minor != minor.to_integral_value():
        raise RowReject("amount_not_integral_minor_units")
    return _check_minor(minor)


def parse_minor_int(raw, spec, ctx):
    """Already in minor units (e.g. '4250'); fractional values are a data error."""
    d = _to_decimal(raw)
    if d != d.to_integral_value():
        raise RowReject("amount_not_integral_minor_units")
    return _check_minor(d)


def _utc_string(dt: datetime, ctx: Ctx) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ctx.tz)
    dt = dt.astimezone(timezone.utc)
    if not 1990 <= dt.year <= 2100:
        raise RowReject("invalid_timestamp")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_OFFSET_NO_COLON_RE = re.compile(r"(?<=\d)([+-]\d{2})(\d{2})$")


def parse_iso8601(raw, spec, ctx):
    s = raw[:-1] + "+00:00" if raw[-1] in "Zz" else raw
    if ":" in s:  # Python 3.10's fromisoformat rejects '+0530'; many exporters emit it
        s = _OFFSET_NO_COLON_RE.sub(r"\1:\2", s)
    try:
        return _utc_string(datetime.fromisoformat(s), ctx)
    except (ValueError, OverflowError):
        raise RowReject("invalid_timestamp")


def parse_epoch_s(raw, spec, ctx):
    try:
        return _utc_string(datetime.fromtimestamp(float(raw), tz=timezone.utc), ctx)
    except (ValueError, OverflowError, OSError):
        raise RowReject("invalid_timestamp")


def parse_datetime_fmt(raw, spec, ctx):
    """strptime with the alias's ``format``; naive times are read in the partner timezone."""
    try:
        return _utc_string(datetime.strptime(raw, spec.format), ctx)
    except (ValueError, OverflowError):
        raise RowReject("invalid_timestamp")


PARSERS = {
    "text": parse_text,
    "email": parse_email,
    "currency": parse_currency,
    "decimal_major": parse_decimal_major,
    "minor_int": parse_minor_int,
    "iso8601": parse_iso8601,
    "epoch_s": parse_epoch_s,
    "datetime_fmt": parse_datetime_fmt,
}
