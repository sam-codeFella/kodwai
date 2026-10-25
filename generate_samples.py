#!/usr/bin/env python3
"""Generate deterministic sample partner CSVs (3 partners x 7 days) with realistic drift.

    python generate_samples.py                  # writes data/inbox/<partner>/<partner>_<date>.csv
    python generate_samples.py --deliver-late   # later: move the withheld late file into the inbox
    python generate_samples.py --with-broken    # acme day 6 loses a required column (loud failure demo)

Drift schedule (day numbers are 1-based from --start):
  acme   day 3: `email` renamed `customerEmail`;  day 5: `amount` ("42.50") becomes `amount_cents` (4250)
  bolt   day 5: `total` keeps its name but flips from "42.50" to integer minor units;  day 6: extra `promo_code`
  cobalt day 5: `price` goes from "42.50" strings to floats (42.5);  day 6: extra `channel`

Realism injected on top:
  * a few malformed rows per file (bad email, blank id, junk amount, junk timestamp, negative, ragged)
  * acme day 2 is delivered twice (second copy is byte-identical, different file name)
  * cobalt day 4 arrives late: it is written to --late-dir instead of the inbox
  * some orders reappear the next day with a corrected amount

A manifest describing exactly what was injected and what the final `orders` table should
contain is returned by `generate()` and written to --manifest, so tests can assert on it.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

PARTNERS = ("acme", "bolt", "cobalt")
DAYS = 7
LATE = ("cobalt", 4)
DUPLICATE = ("acme", 2)
CURRENCIES = {"acme": ["USD", "EUR"], "bolt": ["USD", "GBP", "JPY"], "cobalt": ["USD", "EUR", "GBP"]}
EXPONENT = {"JPY": 0}
COBALT_TZ = timezone(timedelta(hours=-5))  # matches `timezone` in config/partners.json
BAD_KINDS = ("bad_email", "blank_id", "junk_amount", "junk_timestamp", "negative", "ragged")
BAD_REASON = {
    "bad_email": "invalid_email", "blank_id": "missing_order_id", "junk_amount": "invalid_amount",
    "junk_timestamp": "invalid_timestamp", "negative": "negative_amount", "ragged": "wrong_column_count",
}
FIRST = ["ava", "ben", "chloe", "dev", "ella", "finn", "gia", "hugo", "iris", "jon", "kai", "lena"]


def _money(minor: int, ccy: str, style: str) -> str:
    exp = EXPONENT.get(ccy, 2)
    if style == "minor":
        return str(minor)
    major = Decimal(minor).scaleb(-exp)
    if style == "float":  # what a JSON/float-based exporter would write: 42.5, 1234.56, 7.0
        return repr(float(major))
    return f"{major:.{exp}f}"  # "42.50"


def _layout(partner: str, day: int, with_broken: bool):
    """-> (header, row_builder(order, bad_amount=None, ...) -> list[str])"""
    if partner == "acme":
        email = "email" if day < 3 else "customerEmail"
        amount_col, style = ("amount", "major") if day < 5 else ("amount_cents", "minor")
        header = ["order_id", email, amount_col, "currency", "placed_at"]

        def build(o, amount=None):
            return [o["id"], o["email"], amount if amount is not None else _money(o["minor"], o["ccy"], style),
                    o["ccy"], o["placed"].strftime("%Y-%m-%dT%H:%M:%SZ")]
        if with_broken and day == 6:
            keep = [i for i, h in enumerate(header) if h != "currency"]
            header = [header[i] for i in keep]
            inner = build
            build = lambda o, amount=None: [inner(o, amount)[i] for i in keep]  # noqa: E731
        return header, build

    if partner == "bolt":
        style = "major" if day < 5 else "minor"
        header = ["OrderId", "customer_email", "total", "ccy", "order_ts"] + (["promo_code"] if day >= 6 else [])

        def build(o, amount=None):
            row = [o["id"], o["email"], amount if amount is not None else _money(o["minor"], o["ccy"], style),
                   o["ccy"], str(int(o["placed"].timestamp()))]
            return row + (["SAVE10"] if day >= 6 else [])
        return header, build

    style = "major" if day < 5 else "float"
    header = ["id", "buyer_email", "price", "currency", "created"] + (["channel"] if day >= 6 else [])

    def build(o, amount=None):
        local = o["placed"].astimezone(COBALT_TZ)
        row = [o["id"], o["email"], amount if amount is not None else _money(o["minor"], o["ccy"], style),
               o["ccy"], local.strftime("%m/%d/%Y %H:%M")]
        return row + (["web"] if day >= 6 else [])
    return header, build


def _new_order(rng, partner, day_date, seq):
    ccy = rng.choice(CURRENCIES[partner])
    minor = rng.randint(500, 90000)
    if ccy == "JPY":
        minor = rng.randint(5, 900) * 100
    placed = datetime.combine(day_date, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
        minutes=rng.randint(6 * 60, 23 * 60))
    return {"id": f"{partner[:2].upper()}-{day_date:%m%d}-{seq:04d}", "email": f"{rng.choice(FIRST)}{rng.randint(1, 999)}@example.com",
            "minor": minor, "ccy": ccy, "placed": placed}


AMOUNT_COL, TS_COL = 2, 4  # same positions in every partner layout


def _bad_row(rng, kind, partner, day_date, seq, build):
    """One malformed row; each kind breaks exactly one thing so its reject reason is predictable."""
    o = _new_order(rng, partner, day_date, 9000 + seq)
    o["id"] = f"BAD-{o['id']}"
    if kind == "bad_email":
        o["email"] = "not-an-email"
    elif kind == "blank_id":
        o["id"] = ""
    row = build(o, amount="abc" if kind == "junk_amount" else None)
    if kind == "negative":
        row[AMOUNT_COL] = "-" + row[AMOUNT_COL]
    elif kind == "junk_timestamp":
        row[TS_COL] = "yesterday-ish"
    elif kind == "ragged":
        row = row[:-1]
    return row


def generate(out_dir: str, late_dir: str, seed: int = 42, start: str = "2026-01-01", with_broken: bool = False) -> dict:
    rng = random.Random(seed)
    start_date = date.fromisoformat(start)
    manifest = {"start": start, "files": [], "expected_orders": {}, "late_files": [], "duplicate_files": [],
                "broken_files": []}
    prev_day_orders = {p: [] for p in PARTNERS}

    for day in range(1, DAYS + 1):
        day_date = start_date + timedelta(days=day - 1)
        for partner in PARTNERS:
            header, build = _layout(partner, day, with_broken)
            orders = [_new_order(rng, partner, day_date, i) for i in range(rng.randint(25, 40))]
            # Corrections: a couple of yesterday's orders come back with a changed amount.
            corrections = []
            for o in rng.sample(prev_day_orders[partner], min(2, len(prev_day_orders[partner]))):
                delta = 100 if o["ccy"] == "JPY" else rng.choice([-300, 300, 1000])
                corrections.append({**o, "minor": o["minor"] + delta})
            rows = [build(o) for o in orders + corrections]
            kinds = rng.sample(BAD_KINDS, 3)
            reasons = {}
            for n, kind in enumerate(kinds):
                if with_broken and partner == "acme" and day == 6:
                    continue
                rows.insert(rng.randint(0, len(rows)), _bad_row(rng, kind, partner, day_date, n, build))
                reasons[BAD_REASON[kind]] = reasons.get(BAD_REASON[kind], 0) + 1

            is_late = (partner, day) == LATE
            base = os.path.join(late_dir if is_late else out_dir, partner)
            os.makedirs(base, exist_ok=True)
            fname = f"{partner}_{day_date.isoformat()}.csv"
            path = os.path.join(base, fname)
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(header)
                w.writerows(rows)

            broken = with_broken and partner == "acme" and day == 6
            entry = {"partner": partner, "date": day_date.isoformat(), "day": day, "file": f"{partner}/{fname}",
                     "late": is_late, "broken": broken, "good_rows": 0 if broken else len(orders) + len(corrections),
                     "bad_rows": 0 if broken else sum(reasons.values()), "reject_reasons": {} if broken else reasons,
                     "columns": header}
            manifest["files"].append(entry)
            if is_late:
                manifest["late_files"].append(entry["file"])
            if broken:
                manifest["broken_files"].append(entry["file"])
            if (partner, day) == DUPLICATE:
                dup = os.path.join(base, f"{partner}_{day_date.isoformat()}_redelivery.csv")
                shutil.copyfile(path, dup)
                manifest["duplicate_files"].append(f"{partner}/{os.path.basename(dup)}")

            if not broken:  # later days overwrite earlier versions, mirroring "last file wins"
                for o in orders + corrections:
                    manifest["expected_orders"][f"{partner}|{o['id']}"] = {
                        "customer_email": o["email"], "amount_minor": o["minor"], "currency": o["ccy"],
                        "placed_at": o["placed"].strftime("%Y-%m-%dT%H:%M:%SZ")}
            prev_day_orders[partner] = orders
    return manifest


def deliver_late(out_dir: str, late_dir: str) -> list:
    moved = []
    for partner in os.listdir(late_dir) if os.path.isdir(late_dir) else []:
        src_dir = os.path.join(late_dir, partner)
        for name in sorted(os.listdir(src_dir)):
            dst_dir = os.path.join(out_dir, partner)
            os.makedirs(dst_dir, exist_ok=True)
            shutil.move(os.path.join(src_dir, name), os.path.join(dst_dir, name))
            moved.append(f"{partner}/{name}")
    return moved


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/inbox")
    ap.add_argument("--late-dir", default="data/late")
    ap.add_argument("--manifest", default="data/manifest.json")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--with-broken", action="store_true")
    ap.add_argument("--deliver-late", action="store_true", help="move withheld late files into the inbox and exit")
    args = ap.parse_args()

    if args.deliver_late:
        for f in deliver_late(args.out, args.late_dir):
            print("delivered late file:", f)
        return
    for d in (args.out, args.late_dir):
        shutil.rmtree(d, ignore_errors=True)
    manifest = generate(args.out, args.late_dir, args.seed, args.start, args.with_broken)
    os.makedirs(os.path.dirname(args.manifest) or ".", exist_ok=True)
    with open(args.manifest, "w") as fh:
        json.dump(manifest, fh, indent=2)
    print(f"wrote {len(manifest['files'])} files; withheld as late: {manifest['late_files']}; "
          f"redelivered: {manifest['duplicate_files']}; expected orders: {len(manifest['expected_orders'])}")


if __name__ == "__main__":
    main()
