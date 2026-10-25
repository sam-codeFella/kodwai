"""Randomised and whole-week invariants: whatever the data looks like, the books must balance."""
import csv
import os
import random
import re
import string

import generate_samples
from helpers import HEADERS, Workspace, row

D = "2026-01-03"
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
NASTY = ["", " ", "NULL", "null", "N/A", "-", "0", "-1", "1e999", "NaN", "'; DROP TABLE orders;--", "<b>x</b>", "\t", "\\",
         "\"", "a,b", "line1\nline2", "\r", "😀", "Ünï", "１２３", "٣٤٥", "9" * 400, "0" * 400, "​", "﻿", "€42", "42€"]


def junk(rng):
    kind = rng.random()
    if kind < 0.35:
        return rng.choice(NASTY)
    if kind < 0.6:
        return "".join(rng.choice(string.printable.replace("\x0b", "").replace("\x0c", "")) for _ in range(rng.randint(0, 25)))
    if kind < 0.8:
        return str(rng.choice([0, 1, 42, 4250, -7, 10**12, 10**18])) + rng.choice(["", ".5", ".50", ".999", "e2"])
    return "".join(chr(rng.randint(0x20, 0x2FFF)) for _ in range(rng.randint(1, 12)))


class FuzzTests(Workspace):
    def fuzz_partner(self, partner, seed, n=1500):
        rng = random.Random(seed)
        rows = []
        for i in range(n):
            r = row(partner, f"F{seed}-{i}")
            for col in rng.sample(range(5), rng.randint(0, 5)):
                r[col] = junk(rng)
            if rng.random() < 0.05:
                r = r[: rng.randint(1, 4)] if rng.random() < 0.5 else r + [junk(rng)]
            rows.append(r)
        self.write(partner, D, rows, lineterminator="\r\n")  # so bare CR/LF inside fields get quoted
        return rows

    def test_random_garbage_never_crashes_and_every_row_is_accounted_for(self):
        for seed, partner in enumerate(("acme", "bolt", "cobalt")):
            with self.subTest(partner=partner):
                rows = self.fuzz_partner(partner, seed)
                s = self.run_day(D, batch_size=50)
                st = self.stat(s, partner)
                self.assertEqual(st["status"], "ok", st["error"])
                self.assertEqual(st["rows_in"], len(rows))
                self.assertEqual(st["rows_in"], st["rows_out"] + st["rows_rejected"])
                self.assertEqual(self.q("SELECT count(*) FROM rejects WHERE partner = ?", partner)[0][0], st["rows_rejected"])
                self.assertGreater(st["rows_out"], 0, "fuzz should still let some rows through")
                self.assertGreater(st["rows_rejected"], 0)

    def test_everything_that_lands_in_orders_satisfies_the_canonical_contract(self):
        for seed, partner in enumerate(("acme", "bolt", "cobalt")):
            self.fuzz_partner(partner, 100 + seed)
        self.run_day(D, batch_size=64)
        self.assertGreater(self.q("SELECT count(*) FROM orders")[0][0], 100)
        for oid, partner, email, amount, ccy, ts, ing, src, run_id in self.q("SELECT * FROM orders"):
            self.assertTrue(oid and oid == oid.strip(), repr(oid))
            self.assertRegex(email, EMAIL)
            self.assertEqual(email, email.lower())
            self.assertIsInstance(amount, int)
            self.assertGreaterEqual(amount, 0)
            self.assertLess(amount, 10**15)
            self.assertRegex(ccy, r"^[A-Z]{3}$")
            self.assertRegex(ts, TS)
            self.assertTrue(1990 <= int(ts[:4]) <= 2100)
            self.assertRegex(ing, TS)
            self.assertTrue(src.startswith(partner + "/"))

    def test_every_reject_has_a_known_reason_a_parseable_payload_and_a_line_number(self):
        import json
        known = re.compile(r"^(missing_(order_id|customer_email|amount|currency|placed_at)|invalid_email|invalid_amount|negative_amount|"
                           r"amount_not_integral_minor_units|invalid_currency|invalid_timestamp|wrong_column_count)$")
        for seed, partner in enumerate(("acme", "bolt", "cobalt")):
            self.fuzz_partner(partner, 200 + seed)
        self.run_day(D, batch_size=64)
        rows = self.q("SELECT partner, source_file, line_no, raw_row, reason, run_id FROM rejects")
        self.assertGreater(len(rows), 100)
        for partner, src, line_no, raw, reason, run_id in rows:
            self.assertRegex(reason, known)
            self.assertIsInstance(json.loads(raw), dict)
            self.assertGreaterEqual(line_no, 2)
            self.assertTrue(src.startswith(partner + "/"))
            self.assertTrue(run_id)

    def test_fuzzed_file_is_idempotent_too(self):
        self.fuzz_partner("acme", 5)
        self.run_day(D, batch_size=64)
        a = (self.q("SELECT * FROM orders ORDER BY order_id"), self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id"))
        s = self.run_day(D, batch_size=64)
        b = (self.q("SELECT * FROM orders ORDER BY order_id"), self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id"))
        self.assertEqual(a, b)
        self.assertEqual(self.stat(s, "acme")["rows_changed"], 0)

    def test_oracle_check_valid_rows_with_random_spellings_match_independent_expectations(self):
        rng = random.Random(99)
        rows, expect = [], {}
        for i in range(800):
            minor = rng.randint(0, 10_000_000)
            ccy = rng.choice(["USD", "EUR", "GBP"])
            spelling = rng.choice([
                lambda m: f"{m // 100}.{m % 100:02d}", lambda m: f"{m // 100}.{m % 100:02d}".rstrip("0").rstrip(".") or "0",
                lambda m: f" {m // 100}.{m % 100:02d} ", lambda m: f"+{m // 100}.{m % 100:02d}", lambda m: f"{m // 100:010d}.{m % 100:02d}"])
            email = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(rng.randint(1, 12)))
            r = row("acme", f"O{i}", email=f"{email}@Example.{rng.choice(['com', 'COM', 'io'])}", amount=spelling(minor),
                    ccy=rng.choice([ccy, ccy.lower(), f" {ccy} "]))
            rows.append(r)
            expect[f"O{i}"] = (minor, ccy)
        self.write("acme", D, rows)
        s = self.run_day(D, batch_size=97)
        self.assertEqual(self.stat(s, "acme")["rows_rejected"], 0)
        got = {oid: (amt, c) for (_, oid), (_, amt, c, _) in self.orders("acme").items()}
        self.assertEqual(got, expect)


class WholeWeekInvariantTests(Workspace):
    def setUp(self):
        super().setUp()
        self.inbox = os.path.join(self.tmp, "in")
        self.late = os.path.join(self.tmp, "late")
        self.manifest = generate_samples.generate(self.inbox, self.late)
        self.summaries = [self.run_day(f"2026-01-0{d}") for d in range(1, 8)]
        generate_samples.deliver_late(self.inbox, self.late)
        self.summaries.append(self.run_day("2026-01-04"))

    def test_rows_in_equals_out_plus_rejected_for_every_loaded_file(self):
        for s in self.summaries:
            for p in s["partners"]:
                if p["status"] == "ok":
                    self.assertEqual(p["rows_in"], p["rows_out"] + p["rows_rejected"], p)
                    self.assertEqual(p["rows_changed"] + p["rows_unchanged"], p["rows_out"], p)

    def test_non_ok_stats_rows_report_zero_rows(self):
        for s in self.summaries:
            for p in s["partners"]:
                if p["status"] in ("missing", "skipped_duplicate", "failed"):
                    self.assertEqual((p["rows_in"], p["rows_out"], p["rows_rejected"], p["rows_changed"]), (0, 0, 0, 0))

    def test_exactly_the_expected_statuses_appeared_across_the_week(self):
        seen = [(p["partner"], p["status"]) for s in self.summaries for p in s["partners"] if p["status"] != "ok"]
        self.assertEqual(sorted(seen), [("acme", "skipped_duplicate"), ("cobalt", "missing")])

    def test_rows_out_per_file_matches_what_the_generator_wrote(self):
        by_file = {p["source_file"]: p["rows_out"] for s in self.summaries for p in s["partners"] if p["status"] == "ok"}
        for f in self.manifest["files"]:
            self.assertEqual(by_file.get(f["file"], 0), f["good_rows"], f["file"])
        total_changed = sum(p["rows_changed"] for s in self.summaries for p in s["partners"])
        self.assertGreaterEqual(total_changed, self.q("SELECT count(*) FROM orders")[0][0])

    def test_a_full_second_pass_over_the_week_is_a_perfect_noop(self):
        before = self.q("SELECT * FROM orders ORDER BY partner, order_id")
        n_rejects = self.q("SELECT count(*) FROM rejects")
        for d in range(1, 8):
            s = self.run_day(f"2026-01-0{d}")
            self.assertEqual(sum(p["rows_changed"] for p in s["partners"]), 0, d)
            self.assertEqual(s["exit_code"], 0)
        self.assertEqual(self.q("SELECT * FROM orders ORDER BY partner, order_id"), before)
        self.assertEqual(self.q("SELECT count(*) FROM rejects"), n_rejects)

    def test_primary_key_holds_no_duplicates_and_every_order_traces_to_a_real_file(self):
        self.assertEqual(self.q("SELECT count(*) FROM (SELECT partner, order_id FROM orders GROUP BY 1, 2 HAVING count(*) > 1)"), [(0,)])
        for (src,) in self.q("SELECT DISTINCT source_file FROM orders"):
            self.assertTrue(os.path.exists(os.path.join(self.inbox, src)), src)
        self.assertEqual(self.q("SELECT count(*) FROM orders WHERE run_id NOT IN (SELECT run_id FROM pipeline_runs)"), [(0,)])

    def test_run_metrics_are_complete_and_consistent(self):
        runs = self.q("SELECT run_id, run_date, started_at, finished_at, duration_s, status FROM pipeline_runs")
        self.assertEqual(len(runs), 8)
        self.assertEqual(len({r[0] for r in runs}), 8, "run ids are unique even when a date is re-run")
        for run_id, run_date, started, finished, dur, status in runs:
            self.assertTrue(run_id.startswith(run_date))
            self.assertIsNotNone(finished)
            self.assertGreaterEqual(dur, 0)
            self.assertEqual(status, "ok")
        per_run = self.q("SELECT run_id, count(DISTINCT partner) FROM partner_run_stats GROUP BY run_id")
        self.assertTrue(all(n == 3 for _, n in per_run))

    def test_rejects_total_equals_what_the_generator_injected(self):
        self.assertEqual(self.q("SELECT count(*) FROM rejects")[0][0], sum(f["bad_rows"] for f in self.manifest["files"]))

    def test_rejected_rows_never_leak_into_orders(self):
        self.assertEqual(self.q("SELECT count(*) FROM orders WHERE order_id LIKE 'BAD-%' OR order_id = ''"), [(0,)])
