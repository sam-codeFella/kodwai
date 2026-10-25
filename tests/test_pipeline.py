import csv
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import tracemalloc
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_samples  # noqa: E402
from etl.parsers import Ctx, RowReject, PARSERS, parse_tz  # noqa: E402
from etl.runner import run  # noqa: E402

CONFIG = os.path.join(ROOT, "config", "partners.json")
DAYS = [f"2026-01-0{d}" for d in range(1, 8)]


def setUpModule():
    logging.disable(logging.CRITICAL)


class Env(unittest.TestCase):
    """Fresh inbox + database per test, populated by the real generator."""

    with_broken = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = os.path.join(self.tmp, "inbox")
        self.late = os.path.join(self.tmp, "late")
        self.db = os.path.join(self.tmp, "wh.db")
        self.manifest = generate_samples.generate(self.inbox, self.late, with_broken=self.with_broken)

    def run_day(self, day, config=CONFIG, batch_size=5000):
        return run(day, self.db, self.inbox, config, batch_size)

    def run_all(self):
        return [self.run_day(d) for d in DAYS]

    def q(self, sql, *args):
        con = sqlite3.connect(self.db)
        try:
            return con.execute(sql, args).fetchall()
        finally:
            con.close()

    @staticmethod
    def by_partner(summary):
        return {p["partner"]: p for p in summary["partners"] if p["source_file"] and "redelivery" not in p["source_file"]}


class IdempotencyTests(Env):
    def test_rerun_same_date_is_a_noop(self):
        self.run_day("2026-01-03")
        before = self.q("SELECT * FROM orders ORDER BY partner, order_id")
        second = self.run_day("2026-01-03")
        self.assertEqual(before, self.q("SELECT * FROM orders ORDER BY partner, order_id"))
        for p in second["partners"]:
            self.assertEqual(p["rows_changed"], 0, p)
            self.assertEqual(p["rows_unchanged"], p["rows_out"], p)

    def test_changed_source_updates_cleanly_without_duplicating(self):
        self.run_day("2026-01-03")
        path = os.path.join(self.inbox, "acme", "acme_2026-01-03.csv")
        with open(path, newline="") as fh:
            rows = list(csv.reader(fh))
        target = next(r for r in rows[1:] if r[0].startswith("AC-"))
        old = int(self.q("SELECT amount_minor FROM orders WHERE partner='acme' AND order_id=?", target[0])[0][0])
        target[2] = f"{(old + 777) / 100:.2f}"
        with open(path, "w", newline="") as fh:
            csv.writer(fh).writerows(rows)
        before = self.q("SELECT count(*) FROM orders")
        s = self.by_partner(self.run_day("2026-01-03"))["acme"]
        self.assertEqual(s["rows_changed"], 1)
        self.assertEqual(self.q("SELECT count(*) FROM orders"), before)
        self.assertEqual(self.q("SELECT amount_minor FROM orders WHERE partner='acme' AND order_id=?", target[0])[0][0], old + 777)

    def test_byte_identical_redelivery_is_skipped(self):
        s = self.run_day("2026-01-02")
        statuses = {p["source_file"]: p["status"] for p in s["partners"]}
        self.assertEqual(statuses["acme/acme_2026-01-02.csv"], "ok")
        self.assertEqual(statuses["acme/acme_2026-01-02_redelivery.csv"], "skipped_duplicate")
        n = self.q("SELECT count(*) FROM orders")
        again = self.run_day("2026-01-02")
        self.assertEqual(self.q("SELECT count(*) FROM orders"), n)
        self.assertEqual(sum(p["rows_changed"] for p in again["partners"]), 0)

    def test_older_file_arriving_late_does_not_clobber_newer_data(self):
        # cobalt day 4 is withheld; day 5 carries corrections to day-4 orders.
        for d in DAYS[:3] + DAYS[4:]:
            self.run_day(d)
        corrected = self.q("SELECT order_id, amount_minor FROM orders WHERE source_file LIKE 'cobalt/cobalt_2026-01-05%' "
                           "AND order_id LIKE 'CO-0104-%'")
        self.assertTrue(corrected, "generator should correct some day-4 orders on day 5")
        generate_samples.deliver_late(self.inbox, self.late)
        self.run_day("2026-01-04")
        for oid, amount in corrected:
            self.assertEqual(self.q("SELECT amount_minor FROM orders WHERE order_id=?", oid)[0][0], amount)


class LateAndMissingTests(Env):
    def test_missing_file_is_not_a_failure_and_loads_when_it_lands(self):
        s = self.run_day("2026-01-04")
        self.assertEqual(s["exit_code"], 0)
        cobalt = next(p for p in s["partners"] if p["partner"] == "cobalt")
        self.assertEqual((cobalt["status"], cobalt["source_file"]), ("missing", None))
        self.assertEqual(self.q("SELECT count(*) FROM orders WHERE partner='cobalt'")[0][0], 0)
        generate_samples.deliver_late(self.inbox, self.late)
        s = self.run_day("2026-01-04")
        self.assertEqual(self.by_partner(s)["cobalt"]["status"], "ok")
        self.assertGreater(self.q("SELECT count(*) FROM orders WHERE partner='cobalt'")[0][0], 0)


class SchemaDriftTests(Env):
    with_broken = True

    def test_missing_required_column_fails_loudly_but_isolates_the_partner(self):
        s = self.run_day("2026-01-06")
        self.assertEqual(s["exit_code"], 1)
        parts = {p["partner"]: p for p in s["partners"]}
        self.assertEqual(parts["acme"]["status"], "failed")
        self.assertIn("currency", parts["acme"]["error"])
        self.assertEqual(self.q("SELECT count(*) FROM orders WHERE source_file LIKE 'acme/%'")[0][0], 0)
        self.assertEqual(parts["bolt"]["status"], "ok")
        self.assertEqual(parts["cobalt"]["status"], "ok")
        self.assertGreater(self.q("SELECT count(*) FROM orders WHERE partner='bolt'")[0][0], 0)
        row = self.q("SELECT status, error FROM partner_run_stats WHERE partner='acme'")[0]
        self.assertEqual(row[0], "failed")


class DemoWeekTests(Env):
    def test_full_week_matches_what_the_generator_injected(self):
        for d in DAYS:
            self.run_day(d)
        generate_samples.deliver_late(self.inbox, self.late)
        self.run_day("2026-01-04")
        got = {f"{p}|{i}": (e, a, c, t) for p, i, e, a, c, t in self.q(
            "SELECT partner, order_id, customer_email, amount_minor, currency, placed_at FROM orders")}
        exp = {k: (v["customer_email"], v["amount_minor"], v["currency"], v["placed_at"])
               for k, v in self.manifest["expected_orders"].items()}
        self.assertEqual(got, exp)

        expected_rejects = {}
        for f in self.manifest["files"]:
            for reason, n in f["reject_reasons"].items():
                expected_rejects[reason] = expected_rejects.get(reason, 0) + n
        self.assertEqual(dict(self.q("SELECT reason, count(*) FROM rejects GROUP BY reason")), expected_rejects)

    def test_rejects_keep_original_row_and_do_not_pile_up_on_rerun(self):
        self.run_day("2026-01-01")
        first = self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id")
        self.assertTrue(first)
        for partner, src, line_no, raw, reason in first:
            self.assertTrue(src.startswith(partner + "/"))
            self.assertIsInstance(json.loads(raw), dict)
            self.assertGreater(line_no, 1)
        self.run_day("2026-01-01")
        self.assertEqual(first, self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id"))

    def test_additive_column_is_tolerated_and_reported(self):
        s = self.run_day("2026-01-06")
        bolt = {p["partner"]: p for p in s["partners"]}["bolt"]
        self.assertEqual(bolt["status"], "ok")
        self.assertEqual(bolt["extra_columns"], "promo_code")

    def test_metrics_recorded_per_run_and_partner(self):
        s = self.run_day("2026-01-01")
        run_row = self.q("SELECT status, duration_s FROM pipeline_runs WHERE run_id=?", s["run_id"])[0]
        self.assertEqual(run_row[0], "ok")
        self.assertIsNotNone(run_row[1])
        stats = self.q("SELECT partner, rows_in, rows_out, rows_rejected FROM partner_run_stats WHERE run_id=?", s["run_id"])
        self.assertEqual({r[0] for r in stats}, {"acme", "bolt", "cobalt"})
        for _, rin, rout, rrej in stats:
            self.assertEqual(rin, rout + rrej)


class ParserTests(unittest.TestCase):
    def amount(self, parser, raw, ccy="USD"):
        ctx = Ctx(parse_tz("UTC"), ccy)
        return PARSERS[parser](raw, None, ctx)

    def reason(self, parser, raw, ccy="USD"):
        with self.assertRaises(RowReject) as cm:
            self.amount(parser, raw, ccy)
        return cm.exception.reason

    def test_three_shapes_of_the_same_price(self):
        self.assertEqual(self.amount("decimal_major", "42.50"), 4250)
        self.assertEqual(self.amount("minor_int", "4250"), 4250)
        self.assertEqual(self.amount("decimal_major", "42.5"), 4250)
        self.assertEqual(self.amount("decimal_major", "42"), 4200)

    def test_no_float_rounding_surprises(self):
        self.assertEqual(self.amount("decimal_major", "0.29"), 29)
        self.assertEqual(self.amount("decimal_major", "1.15"), 115)

    def test_currency_exponent(self):
        self.assertEqual(self.amount("decimal_major", "4200", "JPY"), 4200)
        self.assertEqual(self.amount("decimal_major", "1.234", "BHD"), 1234)

    def test_bad_amounts_are_rejected_with_a_reason(self):
        self.assertEqual(self.reason("decimal_major", "42.505"), "amount_not_integral_minor_units")
        self.assertEqual(self.reason("minor_int", "42.5"), "amount_not_integral_minor_units")
        self.assertEqual(self.reason("decimal_major", "abc"), "invalid_amount")
        self.assertEqual(self.reason("decimal_major", "NaN"), "invalid_amount")
        self.assertEqual(self.reason("decimal_major", "-1.00"), "negative_amount")
        self.assertEqual(self.reason("minor_int", "99999999999999999999"), "invalid_amount")

    def test_timestamps_normalize_to_utc(self):
        ctx = Ctx(parse_tz("UTC-05:00"))
        self.assertEqual(PARSERS["iso8601"]("2026-01-03T14:22:05Z", None, ctx), "2026-01-03T14:22:05Z")
        self.assertEqual(PARSERS["iso8601"]("2026-01-03T09:22:05-05:00", None, ctx), "2026-01-03T14:22:05Z")
        self.assertEqual(PARSERS["iso8601"]("2026-01-03 09:22:05", None, ctx), "2026-01-03T14:22:05Z")  # naive = partner tz
        self.assertEqual(PARSERS["epoch_s"]("1767450125", None, ctx), "2026-01-03T14:22:05Z")

        class Spec:
            format = "%m/%d/%Y %H:%M"
        self.assertEqual(PARSERS["datetime_fmt"]("01/03/2026 09:22", Spec, ctx), "2026-01-03T14:22:00Z")
        with self.assertRaises(RowReject):
            PARSERS["iso8601"]("yesterday-ish", None, ctx)

    def test_email_and_currency(self):
        ctx = Ctx(parse_tz("UTC"))
        self.assertEqual(PARSERS["email"]("Ava@Example.COM", None, ctx), "ava@example.com")
        with self.assertRaises(RowReject):
            PARSERS["email"]("not-an-email", None, ctx)
        self.assertEqual(PARSERS["currency"]("usd", None, ctx), "USD")
        with self.assertRaises(RowReject):
            PARSERS["currency"]("US", None, ctx)


class ExtensibilityTests(unittest.TestCase):
    def test_fourth_partner_is_config_only(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(CONFIG) as fh:
            cfg = json.load(fh)
        cfg["partners"]["delta"] = {
            "file_glob": "{partner}/{partner}_{date}*.csv",
            "timezone": "UTC+01:00",
            "fields": {
                "order_id": ["ref"], "customer_email": ["mail"],
                "amount": [{"column": "cents", "parser": "minor_int"}],
                "currency": ["cur"],
                "placed_at": [{"column": "ts", "parser": "datetime_fmt", "format": "%d.%m.%Y %H:%M"}],
            },
        }
        cfg_path = os.path.join(tmp, "partners.json")
        with open(cfg_path, "w") as fh:
            json.dump(cfg, fh)
        os.makedirs(os.path.join(tmp, "inbox", "delta"))
        with open(os.path.join(tmp, "inbox", "delta", "delta_2026-01-01.csv"), "w", newline="") as fh:
            csv.writer(fh).writerows([["ref", "mail", "cents", "cur", "ts", "note"],
                                      ["D-1", "x@y.io", "1999", "eur", "01.01.2026 10:30", "hi"]])
        s = run("2026-01-01", os.path.join(tmp, "wh.db"), os.path.join(tmp, "inbox"), cfg_path)
        delta = next(p for p in s["partners"] if p["partner"] == "delta")
        self.assertEqual((delta["status"], delta["rows_out"]), ("ok", 1))
        con = sqlite3.connect(os.path.join(tmp, "wh.db"))
        self.assertEqual(con.execute("SELECT amount_minor, currency, placed_at FROM orders WHERE partner='delta'").fetchone(),
                         (1999, "EUR", "2026-01-01T09:30:00Z"))
        con.close()


class ScaleTests(unittest.TestCase):
    def test_large_file_streams_in_bounded_memory(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, "inbox", "acme"))
        path = os.path.join(tmp, "inbox", "acme", "acme_2026-01-01.csv")
        n = 150_000
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["order_id", "email", "amount", "currency", "placed_at"])
            for i in range(n):
                w.writerow([f"A-{i}", f"u{i}@example.com", "42.50", "USD", "2026-01-01T10:00:00Z"])
            w.writerow(["A-bad", "nope", "1.00", "USD", "2026-01-01T10:00:00Z"])
        size_mb = os.path.getsize(path) / 1e6
        tracemalloc.start()
        try:
            s = run("2026-01-01", os.path.join(tmp, "wh.db"), os.path.join(tmp, "inbox"), CONFIG)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        acme = next(p for p in s["partners"] if p["partner"] == "acme")
        self.assertEqual((acme["rows_out"], acme["rows_rejected"]), (n, 1))
        # Peak must be a small constant, nowhere near the data volume.
        self.assertLess(peak / 1e6, max(size_mb, 10), f"peak {peak / 1e6:.1f}MB for a {size_mb:.1f}MB file")


if __name__ == "__main__":
    unittest.main()
