"""Shared test scaffolding: a temp workspace that can hold hand-crafted partner files."""
import csv
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from etl.runner import run  # noqa: E402

CONFIG = os.path.join(ROOT, "config", "partners.json")
logging.disable(logging.CRITICAL)

HEADERS = {
    "acme": ["order_id", "email", "amount", "currency", "placed_at"],
    "bolt": ["OrderId", "customer_email", "total", "ccy", "order_ts"],
    "cobalt": ["id", "buyer_email", "price", "currency", "created"],
}
ID_COLUMN = {"acme": "order_id", "bolt": "OrderId", "cobalt": "id"}
# Field -> (position in every layout above, position in the tuple returned by Workspace.orders()).
FIELD = {"email": (1, 0), "amount": (2, 1), "currency": (3, 2), "ts": (4, 3)}


def epoch(iso):
    return str(int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()))


def row(partner, oid="X1", email="u@example.com", amount="42.50", ccy="USD", ts=None):
    """A valid row in the partner's layout (dated 2026-01-03 10:00 UTC; cobalt is UTC-5, so 05:00 local)."""
    if partner == "acme":
        ts = ts or "2026-01-03T10:00:00Z"
    elif partner == "bolt":
        ts = ts or epoch("2026-01-03T10:00:00Z")
    else:
        ts = ts or "01/03/2026 05:00"
    return [oid, email, amount, ccy, ts]


class Rej:
    """Expected outcome: this input must be rejected with this reason."""

    def __init__(self, reason):
        self.reason = reason

    def __repr__(self):
        return f"Rej({self.reason})"


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = os.path.join(self.tmp, "inbox")
        self.db = os.path.join(self.tmp, "wh.db")
        self.config = CONFIG

    # -- building inputs ----------------------------------------------------
    def path(self, partner, date, name=None):
        d = os.path.join(self.inbox, partner)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, name or f"{partner}_{date}.csv")

    def write(self, partner, date, rows, header=None, name=None, lineterminator="\n", bom=False):
        p = self.path(partner, date, name)
        with open(p, "w", newline="", encoding="utf-8-sig" if bom else "utf-8") as fh:
            w = csv.writer(fh, lineterminator=lineterminator)
            w.writerow(header if header is not None else HEADERS[partner])
            w.writerows(rows)
        return p

    def write_raw(self, partner, date, data, name=None):
        p = self.path(partner, date, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def reset_db(self):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.db + suffix)
            except FileNotFoundError:
                pass

    # -- running / reading --------------------------------------------------
    def run_day(self, date, batch_size=5000, config=None):
        return run(date, self.db, self.inbox, config or self.config, batch_size)

    def q(self, sql, *args):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    def orders(self, partner=None):
        sql = "SELECT partner, order_id, customer_email, amount_minor, currency, placed_at FROM orders"
        args = ()
        if partner:
            sql, args = sql + " WHERE partner = ?", (partner,)
        return {(p, i): (e, a, c, t) for p, i, e, a, c, t in self.q(sql, *args)}

    def reject_reasons(self, partner=None):
        """order id (as the partner wrote it) -> reason; ragged rows are keyed by line number."""
        out = {}
        for p, line_no, raw, reason in self.q("SELECT partner, line_no, raw_row, reason FROM rejects ORDER BY id"):
            if partner and p != partner:
                continue
            doc = json.loads(raw)
            key = doc.get(ID_COLUMN[p]) if ID_COLUMN[p] in doc else f"line{line_no}"
            out[key] = reason
        return out

    @staticmethod
    def stat(summary, partner, source_file=None):
        for p in summary["partners"]:
            if p["partner"] == partner and (source_file is None or p["source_file"] == source_file):
                return p
        raise AssertionError(f"no stats for {partner}/{source_file}: {summary['partners']}")

    def check_field(self, field, cases, partner="acme", date="2026-01-03", **run_kw):
        """Load one row per (input, expected) case, varying a single field, and assert each outcome.

        expected is the canonical value if the row must load, or Rej(reason) if it must be rejected.
        """
        col, out_idx = FIELD[field]
        rows = []
        for n, (value, _) in enumerate(cases):
            r = row(partner, oid=f"R{n}")
            r[col] = value
            rows.append(r)
        self.write(partner, date, rows)
        summary = self.run_day(date, **run_kw)
        self.assertEqual(self.stat(summary, partner)["status"], "ok")
        loaded, rejected = self.orders(partner), self.reject_reasons(partner)
        for n, (value, expected) in enumerate(cases):
            with self.subTest(field=field, value=value):
                if isinstance(expected, Rej):
                    self.assertNotIn((partner, f"R{n}"), loaded, "should not have loaded")
                    self.assertEqual(rejected.get(f"R{n}"), expected.reason)
                else:
                    self.assertNotIn(f"R{n}", rejected, f"unexpectedly rejected: {rejected.get(f'R{n}')}")
                    self.assertEqual(loaded[(partner, f"R{n}")][out_idx], expected)
