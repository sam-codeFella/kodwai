"""The command-line contract and the sample-data generator (which reviewers read too)."""
import csv
import filecmp
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT  # noqa: F401  (sets sys.path)
import generate_samples as gen


def cli(*args, cwd=ROOT):
    return subprocess.run([sys.executable, os.path.join(ROOT, "pipeline.py"), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.inbox = os.path.join(self.tmp, "inbox")
        self.db = os.path.join(self.tmp, "wh.db")
        self.manifest = gen.generate(self.inbox, os.path.join(self.tmp, "late"), with_broken=True)

    def args(self, date, **extra):
        a = ["--run-date", date, "--db", self.db, "--inbox", self.inbox]
        for k, v in extra.items():
            a += [f"--{k.replace('_', '-')}", v]
        return a

    def test_success_prints_a_json_summary_on_stdout_and_exits_zero(self):
        r = cli(*self.args("2026-01-01"))
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["status"], "ok")
        self.assertEqual({p["partner"] for p in doc["partners"]}, {"acme", "bolt", "cobalt"})
        for key in ("rows_in", "rows_out", "rows_rejected", "duration_s", "status"):
            self.assertTrue(all(key in p for p in doc["partners"]), key)
        self.assertIn("finished", r.stderr)

    def test_a_failed_partner_exits_one_but_the_summary_is_still_printed(self):
        r = cli(*self.args("2026-01-06"))
        self.assertEqual(r.returncode, 1)
        doc = json.loads(r.stdout)
        self.assertEqual(doc["status"], "failed")
        self.assertEqual({p["partner"]: p["status"] for p in doc["partners"]}, {"acme": "failed", "bolt": "ok", "cobalt": "ok"})
        self.assertIn("ERROR", r.stderr)

    def test_missing_partner_file_exits_zero(self):
        r = cli(*self.args("2026-01-04"))  # cobalt day 4 is the late one
        self.assertEqual(r.returncode, 0)
        statuses = {p["partner"]: p["status"] for p in json.loads(r.stdout)["partners"]}
        self.assertEqual(statuses["cobalt"], "missing")

    def test_rerunning_through_the_cli_is_a_noop(self):
        cli(*self.args("2026-01-02"))
        second = json.loads(cli(*self.args("2026-01-02")).stdout)
        self.assertEqual(sum(p["rows_changed"] for p in second["partners"]), 0)

    def test_bad_usage_exits_two(self):
        for argv in ([], ["--run-date", "06-01-2026"], ["--run-date", "2026-13-40"], ["--run-date", "tomorrow"], ["--bogus"]):
            with self.subTest(argv=argv):
                self.assertEqual(cli(*argv).returncode, 2)

    def test_bad_config_exits_two_with_a_message_and_no_traceback(self):
        bad = os.path.join(self.tmp, "bad.json")
        with open(bad, "w") as fh:
            fh.write("{nope")
        r = cli(*self.args("2026-01-01", config=bad))
        self.assertEqual(r.returncode, 2)
        self.assertIn("config error", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_help_documents_the_flags(self):
        r = cli("--help")
        self.assertEqual(r.returncode, 0)
        for flag in ("--run-date", "--db", "--inbox", "--config"):
            self.assertIn(flag, r.stdout)

    def test_works_from_any_working_directory_when_paths_are_absolute(self):
        r = cli(*self.args("2026-01-01") + ["--config", os.path.join(ROOT, "config", "partners.json")], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stderr)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make(self, name, **kw):
        out, late = os.path.join(self.tmp, name, "in"), os.path.join(self.tmp, name, "late")
        return out, late, gen.generate(out, late, **kw)

    @staticmethod
    def header(path):
        with open(path, newline="") as fh:
            return next(csv.reader(fh))

    def test_same_seed_gives_byte_identical_output_and_different_seed_does_not(self):
        a, _, ma = self.make("a")
        b, _, mb = self.make("b")
        c, _, _ = self.make("c", seed=7)
        self.assertEqual(ma["expected_orders"], mb["expected_orders"])
        files = [os.path.join(a, p, f) for p in ("acme", "bolt", "cobalt") for f in sorted(os.listdir(os.path.join(a, p)))]
        for f in files:
            rel = os.path.relpath(f, a)
            self.assertTrue(filecmp.cmp(f, os.path.join(b, rel), shallow=False), rel)
        self.assertFalse(filecmp.cmp(os.path.join(a, "acme", "acme_2026-01-01.csv"), os.path.join(c, "acme", "acme_2026-01-01.csv"), shallow=False))

    def test_three_partners_by_seven_days_with_one_late_and_one_redelivery(self):
        out, late, m = self.make("x")
        self.assertEqual(len(m["files"]), 21)
        self.assertEqual(m["late_files"], ["cobalt/cobalt_2026-01-04.csv"])
        self.assertFalse(os.path.exists(os.path.join(out, "cobalt", "cobalt_2026-01-04.csv")))
        self.assertTrue(os.path.exists(os.path.join(late, "cobalt", "cobalt_2026-01-04.csv")))
        n_inbox = sum(len(os.listdir(os.path.join(out, p))) for p in os.listdir(out))
        self.assertEqual(n_inbox, 21 - 1 + 1)  # minus the late file, plus the redelivery
        self.assertTrue(filecmp.cmp(os.path.join(out, "acme", "acme_2026-01-02.csv"),
                                    os.path.join(out, "acme", "acme_2026-01-02_redelivery.csv"), shallow=False))

    def test_documented_drift_schedule_is_really_in_the_files(self):
        out, late, _ = self.make("x")
        gen.deliver_late(out, late)
        h = lambda p, d: self.header(os.path.join(out, p, f"{p}_2026-01-0{d}.csv"))  # noqa: E731
        self.assertIn("email", h("acme", 2))
        self.assertIn("customerEmail", h("acme", 3))
        self.assertNotIn("email", h("acme", 3))
        self.assertIn("amount", h("acme", 4))
        self.assertIn("amount_cents", h("acme", 5))
        self.assertNotIn("amount", h("acme", 5))
        self.assertEqual(h("bolt", 4), h("bolt", 5))  # same header, different meaning
        self.assertNotIn("promo_code", h("bolt", 5))
        self.assertIn("promo_code", h("bolt", 6))
        self.assertIn("channel", h("cobalt", 6))
        self.assertNotIn("channel", h("cobalt", 5))

    def test_amount_representation_actually_drifts_per_partner(self):
        out, late, _ = self.make("x")

        def first_amount(p, d, col):
            with open(os.path.join(out, p, f"{p}_2026-01-0{d}.csv"), newline="") as fh:
                return next(csv.DictReader(fh))[col]
        self.assertRegex(first_amount("acme", 2, "amount"), r"^\d+\.\d\d$")          # "42.50"
        self.assertRegex(first_amount("acme", 5, "amount_cents"), r"^\d+$")          # 4250
        self.assertRegex(first_amount("bolt", 4, "total"), r"^\d+(\.\d\d)?$")
        self.assertRegex(first_amount("cobalt", 5, "price"), r"^\d+\.\d+$")          # 42.5 style float

    def test_manifest_row_counts_match_the_files_on_disk(self):
        out, late, m = self.make("x")
        gen.deliver_late(out, late)
        for f in m["files"]:
            with open(os.path.join(out, f["file"]), newline="") as fh:
                rows = list(csv.reader(fh))[1:]
            self.assertEqual(len(rows), f["good_rows"] + f["bad_rows"], f["file"])
            self.assertEqual(sum(f["reject_reasons"].values()), f["bad_rows"])

    def test_every_file_injects_three_bad_rows_of_distinct_kinds(self):
        _, _, m = self.make("x")
        for f in m["files"]:
            self.assertEqual(f["bad_rows"], 3, f["file"])
            self.assertEqual(len(f["reject_reasons"]), 3, f["file"])

    def test_jpy_orders_exist_so_the_exponent_path_is_exercised(self):
        _, _, m = self.make("x")
        self.assertTrue(any(v["currency"] == "JPY" for v in m["expected_orders"].values()))

    def test_corrections_exist_so_the_update_path_is_exercised(self):
        out, late, m = self.make("x")
        gen.deliver_late(out, late)
        seen = {}
        dup_ids = 0
        for d in range(1, 8):
            with open(os.path.join(out, "acme", f"acme_2026-01-0{d}.csv"), newline="") as fh:
                for r in csv.reader(fh):
                    if r and r[0].startswith("AC-"):
                        dup_ids += r[0] in seen
                        seen[r[0]] = 1
        self.assertGreater(dup_ids, 0)

    def test_with_broken_removes_a_required_column_from_exactly_one_file(self):
        out, late, m = self.make("x", with_broken=True)
        self.assertEqual(m["broken_files"], ["acme/acme_2026-01-06.csv"])
        self.assertNotIn("currency", self.header(os.path.join(out, "acme", "acme_2026-01-06.csv")))
        self.assertIn("currency", self.header(os.path.join(out, "acme", "acme_2026-01-05.csv")))

    def test_deliver_late_moves_files_and_is_idempotent(self):
        out, late, _ = self.make("x")
        self.assertEqual(gen.deliver_late(out, late), ["cobalt/cobalt_2026-01-04.csv"])
        self.assertTrue(os.path.exists(os.path.join(out, "cobalt", "cobalt_2026-01-04.csv")))
        self.assertEqual(gen.deliver_late(out, late), [])
        self.assertEqual(gen.deliver_late(out, os.path.join(self.tmp, "does-not-exist")), [])

    def test_no_generated_id_collides_across_days_within_a_partner(self):
        out, late, m = self.make("x")
        gen.deliver_late(out, late)
        for p in ("acme", "bolt", "cobalt"):
            ids = []
            for d in range(1, 8):
                fresh = [r[0] for r in csv.reader(open(os.path.join(out, p, f"{p}_2026-01-0{d}.csv"), newline="")) if r and r[0] and f"-0{1}0{d}-" in r[0]]
                ids += fresh
            self.assertEqual(len(ids), len(set(ids)), p)
