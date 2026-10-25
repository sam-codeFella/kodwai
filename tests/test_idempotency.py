"""Idempotency, ordering and atomicity: the properties the whole design exists to guarantee."""
import os
import random
from unittest import mock

import generate_samples
from etl.db import Warehouse
from helpers import HEADERS, Workspace, row

D = "2026-01-03"


def snapshot(ws):
    return ws.q("SELECT * FROM orders ORDER BY partner, order_id")


class RerunTests(Workspace):
    def test_rerun_many_times_never_changes_anything(self):
        self.write("acme", D, [row("acme", f"R{i}", amount=f"{i}.25") for i in range(50)] + [row("acme", "BAD", email="x")])
        first = self.run_day(D)
        snap, rejects = snapshot(self), self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id")
        for _ in range(5):
            again = self.run_day(D)
            a = self.stat(again, "acme")
            self.assertEqual((a["rows_changed"], a["rows_unchanged"], a["rows_out"]), (0, 50, 50))
            self.assertEqual(snapshot(self), snap)
            self.assertEqual(self.q("SELECT partner, source_file, line_no, raw_row, reason FROM rejects ORDER BY id"), rejects)
        self.assertEqual({r[8] for r in snap}, {first["run_id"]}, "rows keep the run_id that first loaded them")

    def test_only_changed_rows_get_new_ingest_metadata(self):
        self.write("acme", D, [row("acme", "KEEP", amount="1.00"), row("acme", "EDIT", amount="2.00")])
        r1 = self.run_day(D)
        self.write("acme", D, [row("acme", "KEEP", amount="1.00"), row("acme", "EDIT", amount="9.99")])
        r2 = self.run_day(D)
        meta = {oid: (amt, run_id) for oid, amt, run_id in self.q("SELECT order_id, amount_minor, run_id FROM orders")}
        self.assertEqual(meta["KEEP"], (100, r1["run_id"]))
        self.assertEqual(meta["EDIT"], (999, r2["run_id"]))
        self.assertEqual(self.stat(r2, "acme")["rows_changed"], 1)

    def test_every_business_column_change_is_detected_as_an_update(self):
        base = row("acme", "Z", email="a@x.com", amount="1.00", ccy="USD", ts="2026-01-03T10:00:00Z")
        self.write("acme", D, [base])
        self.run_day(D)
        variants = {"email": (1, "b@x.com"), "amount": (2, "2.00"), "currency": (3, "EUR"), "placed_at": (4, "2026-01-03T11:00:00Z")}
        for name, (idx, value) in variants.items():
            with self.subTest(changed=name):
                r = list(base)
                r[idx] = value
                self.write("acme", D, [r])
                self.assertEqual(self.stat(self.run_day(D), "acme")["rows_changed"], 1)
                self.write("acme", D, [r])
                self.assertEqual(self.stat(self.run_day(D), "acme")["rows_changed"], 0)
                base = r

    def test_equivalent_respellings_of_the_same_data_are_not_changes(self):
        self.write("acme", D, [row("acme", "S", email="A@X.com", amount="42.50", ccy="usd", ts="2026-01-03T10:00:00Z")])
        self.run_day(D)
        self.write("acme", D, [row("acme", "S", email=" a@x.com ", amount="42.5", ccy="USD", ts="2026-01-03T05:00:00-05:00")])
        self.assertEqual(self.stat(self.run_day(D), "acme")["rows_changed"], 0)

    def test_row_removed_from_a_corrected_file_stays_in_orders(self):
        # Orders are upserts, not a mirror of the latest file: omission is not deletion.
        self.write("acme", D, [row("acme", "A"), row("acme", "B")])
        self.run_day(D)
        self.write("acme", D, [row("acme", "A")])
        self.run_day(D)
        self.assertEqual({k[1] for k in self.orders()}, {"A", "B"})


class DuplicateKeyTests(Workspace):
    def test_same_key_twice_in_one_file_last_row_wins_and_one_row_exists(self):
        self.write("acme", D, [row("acme", "DUP", amount="1.00"), row("acme", "OTHER"), row("acme", "DUP", amount="2.00")])
        s = self.run_day(D)
        self.assertEqual(self.orders()[("acme", "DUP")][1], 200)
        self.assertEqual(self.q("SELECT count(*) FROM orders"), [(2,)])
        self.assertEqual(self.stat(s, "acme")["rows_in"], 3)

    def test_same_key_twice_with_identical_content_is_harmless(self):
        self.write("acme", D, [row("acme", "DUP"), row("acme", "DUP")])
        self.run_day(D)
        self.assertEqual(self.q("SELECT count(*) FROM orders"), [(1,)])

    def test_same_key_straddling_a_batch_boundary(self):
        rows = [row("acme", "DUP", amount="1.00")] + [row("acme", f"F{i}") for i in range(7)] + [row("acme", "DUP", amount="5.00")]
        self.write("acme", D, rows)
        self.run_day(D, batch_size=3)
        self.assertEqual(self.orders()[("acme", "DUP")][1], 500)
        self.assertEqual(self.q("SELECT count(*) FROM orders"), [(8,)])
        self.assertEqual(self.stat(self.run_day(D, batch_size=3), "acme")["rows_changed"], 0)

    def test_conflicting_duplicate_keys_in_one_file_still_rerun_as_a_true_noop(self):
        # Regression: v1 then v2 for one key used to flip the row (and its run_id) on every run.
        self.write("acme", D, [row("acme", "DUP", amount="1.00"), row("acme", "X"), row("acme", "DUP", amount="2.00")])
        first = self.run_day(D)
        before = snapshot(self)
        for _ in range(3):
            again = self.run_day(D)
            self.assertEqual(self.stat(again, "acme")["rows_changed"], 0)
            self.assertEqual(snapshot(self), before)
        self.assertEqual({r[8] for r in before}, {first["run_id"]})

    def test_correction_arriving_in_a_later_days_file_updates_in_place(self):
        self.write("acme", "2026-01-03", [row("acme", "O1", amount="10.00")])
        self.write("acme", "2026-01-04", [row("acme", "O1", amount="12.00")])
        self.run_day("2026-01-03")
        self.run_day("2026-01-04")
        self.assertEqual(self.q("SELECT amount_minor, source_file FROM orders"), [(1200, "acme/acme_2026-01-04.csv")])


class StaleWriteGuardTests(Workspace):
    def test_an_older_file_arriving_late_cannot_overwrite_newer_data(self):
        self.write("acme", "2026-01-04", [row("acme", "O1", amount="12.00")])
        self.write("acme", "2026-01-03", [row("acme", "O1", amount="10.00")])
        self.run_day("2026-01-04")
        s = self.run_day("2026-01-03")
        self.assertEqual(self.q("SELECT amount_minor, source_file FROM orders"), [(1200, "acme/acme_2026-01-04.csv")])
        self.assertEqual(self.stat(s, "acme")["rows_changed"], 0)

    def test_load_order_of_days_does_not_change_the_final_table(self):
        files = {"2026-01-03": "10.00", "2026-01-04": "11.00", "2026-01-05": "12.00"}
        results = []
        for order in (list(files), list(reversed(files)), ["2026-01-04", "2026-01-05", "2026-01-03"]):
            self.reset_db()
            for d, amt in files.items():
                self.write("acme", d, [row("acme", "O1", amount=amt), row("acme", f"only-{d}")])
            for d in order:
                self.run_day(d)
            results.append(self.q("SELECT order_id, amount_minor, source_file FROM orders ORDER BY order_id"))
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])
        self.assertIn(("O1", 1200, "acme/acme_2026-01-05.csv"), results[0])

    def test_guard_at_the_sql_layer(self):
        wh = Warehouse(self.db)
        base = dict(order_id="O", partner="p", customer_email="a@x.com", amount_minor=100, currency="USD",
                    placed_at="2026-01-01T00:00:00Z", ingested_at="t", run_id="r")
        wh.begin()

        def merge(row_):
            wh.begin_stage()
            wh.stage_orders([row_])
            return wh.merge_stage()
        self.assertEqual(merge({**base, "source_file": "p/p_2026-01-05.csv"}), 1)
        self.assertEqual(merge({**base, "amount_minor": 999, "source_file": "p/p_2026-01-04.csv"}), 0, "older: ignored")
        self.assertEqual(merge({**base, "amount_minor": 777, "source_file": "p/p_2026-01-05.csv"}), 1, "same file: allowed")
        self.assertEqual(merge({**base, "amount_minor": 555, "source_file": "p/p_2026-01-06.csv"}), 1, "newer: allowed")
        self.assertEqual(merge({**base, "amount_minor": 555, "source_file": "p/p_2026-01-06.csv"}), 0, "identical: no-op")
        wh.commit()
        self.assertEqual(wh.conn.execute("SELECT amount_minor, source_file FROM orders").fetchall(), [(555, "p/p_2026-01-06.csv")])
        wh.close()

    def test_redelivery_with_different_content_under_a_new_name_is_applied(self):
        self.write("acme", D, [row("acme", "O1", amount="1.00")])
        self.run_day(D)
        self.write("acme", D, [row("acme", "O1", amount="2.00")], name=f"acme_{D}_corrected.csv")
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme", f"acme/acme_{D}_corrected.csv")["status"], "ok")
        self.assertEqual(self.orders()[("acme", "O1")][1], 200)


class DuplicateFileTests(Workspace):
    def test_identical_copy_under_another_name_is_skipped_and_recorded(self):
        p = self.write("acme", D, [row("acme", "O1"), row("acme", "O2")])
        with open(p, "rb") as src, open(self.path("acme", D, f"acme_{D}_copy.csv"), "wb") as dst:
            dst.write(src.read())
        s = self.run_day(D)
        by_file = {x["source_file"]: x for x in s["partners"]}
        self.assertEqual(by_file[f"acme/acme_{D}.csv"]["status"], "ok")
        dup = by_file[f"acme/acme_{D}_copy.csv"]
        self.assertEqual((dup["status"], dup["rows_in"]), ("skipped_duplicate", 0))
        self.assertIn(f"acme_{D}.csv", dup["error"])
        self.assertEqual(s["exit_code"], 0)

    def test_identical_bytes_for_different_partners_are_not_duplicates(self):
        content = [row("acme", "O1")]
        self.write("acme", D, content)
        self.write("bolt", D, content, header=HEADERS["acme"])  # fails (wrong header) but must not be judged a dup of acme
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "ok")
        self.assertNotEqual(self.stat(s, "bolt")["status"], "skipped_duplicate")

    def test_rerunning_the_same_file_name_is_reprocessed_not_hash_skipped(self):
        # A hash-skip on same-name files would block reprocessing after a config fix; the upsert makes it free anyway.
        self.write("acme", D, [row("acme", "O1")])
        self.run_day(D)
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "ok")

    def test_failed_load_is_not_remembered_as_a_loaded_file(self):
        self.write("acme", D, [row("acme", "O1")], header=["nope"])
        self.run_day(D)
        self.assertEqual(self.q("SELECT count(*) FROM file_loads"), [(0,)])
        self.write("acme", D, [row("acme", "O1")], name=f"acme_{D}_fixed.csv")
        self.assertEqual(self.stat(self.run_day(D), "acme", f"acme/acme_{D}_fixed.csv")["status"], "ok")


class RejectLifecycleTests(Workspace):
    def test_fixing_a_bad_row_removes_its_reject_on_rerun(self):
        self.write("acme", D, [row("acme", "A"), row("acme", "B", email="bad"), row("acme", "C", amount="x")])
        self.run_day(D)
        self.assertEqual(self.reject_reasons(), {"B": "invalid_email", "C": "invalid_amount"})
        self.write("acme", D, [row("acme", "A"), row("acme", "B"), row("acme", "C", amount="x")])
        self.run_day(D)
        self.assertEqual(self.reject_reasons(), {"C": "invalid_amount"})
        self.assertIn(("acme", "B"), self.orders())

    def test_a_new_bad_row_in_a_corrected_file_is_added(self):
        self.write("acme", D, [row("acme", "A")])
        self.run_day(D)
        self.write("acme", D, [row("acme", "A"), row("acme", "N", ts="junk")])
        self.run_day(D)
        self.assertEqual(self.reject_reasons(), {"N": "invalid_timestamp"})

    def test_rejects_of_other_files_are_untouched_when_one_file_is_reprocessed(self):
        self.write("acme", "2026-01-03", [row("acme", "A", email="bad")])
        self.write("acme", "2026-01-04", [row("acme", "B", email="bad")])
        self.run_day("2026-01-03")
        self.run_day("2026-01-04")
        self.run_day("2026-01-03")
        self.assertEqual(self.q("SELECT source_file FROM rejects ORDER BY source_file"),
                         [("acme/acme_2026-01-03.csv",), ("acme/acme_2026-01-04.csv",)])

    def test_reject_carries_run_id_source_file_and_original_values(self):
        self.write("bolt", D, [row("bolt", "BAD1", email="nope", amount="9.99")])
        s = self.run_day(D)
        rid, partner, src, line_no, raw, reason = self.q(
            "SELECT run_id, partner, source_file, line_no, raw_row, reason FROM rejects")[0]
        self.assertEqual((rid, partner, src, line_no, reason), (s["run_id"], "bolt", f"bolt/bolt_{D}.csv", 2, "invalid_email"))
        import json
        doc = json.loads(raw)
        self.assertEqual((doc["OrderId"], doc["customer_email"], doc["total"]), ("BAD1", "nope", "9.99"))


class BatchBoundaryTests(Workspace):
    def test_row_counts_are_right_around_every_batch_boundary(self):
        for n in (0, 1, 2, 4, 5, 6, 9, 10, 11, 24, 25, 26):
            with self.subTest(rows=n):
                self.reset_db()
                self.write("acme", D, [row("acme", f"R{i}") for i in range(n)])
                a = self.stat(self.run_day(D, batch_size=5), "acme")
                self.assertEqual((a["rows_in"], a["rows_out"], a["rows_changed"]), (n, n, n))
                self.assertEqual(self.q("SELECT count(*) FROM orders"), [(n,)])
                self.assertEqual(self.stat(self.run_day(D, batch_size=5), "acme")["rows_changed"], 0)

    def test_rejects_spanning_many_batches(self):
        rows = [row("acme", f"B{i}", email="bad") for i in range(23)] + [row("acme", "GOOD")]
        self.write("acme", D, rows)
        a = self.stat(self.run_day(D, batch_size=4), "acme")
        self.assertEqual((a["rows_in"], a["rows_out"], a["rows_rejected"]), (24, 1, 23))
        self.assertEqual(self.q("SELECT count(*) FROM rejects"), [(23,)])
        self.run_day(D, batch_size=4)
        self.assertEqual(self.q("SELECT count(*) FROM rejects"), [(23,)])

    def test_result_does_not_depend_on_batch_size(self):
        rows = [row("acme", f"R{i}", amount=f"{i}.10") for i in range(40)] + [row("acme", "BAD", email="x")]
        self.write("acme", D, rows)
        outcomes = []
        for bs in (1, 3, 7, 40, 41, 5000):
            self.reset_db()
            self.run_day(D, batch_size=bs)
            outcomes.append((self.q("SELECT order_id, amount_minor FROM orders ORDER BY order_id"), self.q("SELECT line_no, reason FROM rejects")))
        self.assertTrue(all(o == outcomes[0] for o in outcomes))


class OrderingInvarianceTests(Workspace):
    def test_row_order_within_a_file_does_not_change_the_result(self):
        rng = random.Random(7)
        rows = [row("acme", f"R{i}", amount=f"{rng.randint(1, 9999)}.{rng.randint(0, 99):02d}") for i in range(60)]
        outcomes = []
        for _ in range(5):
            rng.shuffle(rows)
            self.reset_db()
            self.write("acme", D, rows)
            self.run_day(D, batch_size=8)
            outcomes.append(self.q("SELECT order_id, customer_email, amount_minor, currency, placed_at FROM orders ORDER BY order_id"))
        self.assertTrue(all(o == outcomes[0] for o in outcomes))

    def test_splitting_a_file_in_two_gives_the_same_orders_as_one_file(self):
        rows = [row("acme", f"R{i}", amount=f"{i + 1}.00") for i in range(30)]
        self.write("acme", D, rows)
        self.run_day(D)
        whole = self.q("SELECT order_id, amount_minor FROM orders ORDER BY order_id")
        self.reset_db()
        os.remove(self.path("acme", D))
        self.write("acme", D, rows[:12], name=f"acme_{D}_a.csv")
        self.write("acme", D, rows[12:], name=f"acme_{D}_b.csv")
        self.run_day(D)
        self.assertEqual(self.q("SELECT order_id, amount_minor FROM orders ORDER BY order_id"), whole)

    def test_any_day_order_yields_exactly_the_generators_expected_orders(self):
        days = [f"2026-01-0{d}" for d in range(1, 8)]
        rng = random.Random(11)
        for trial in range(4):
            with self.subTest(trial=trial):
                self.reset_db()
                inbox, late = os.path.join(self.tmp, f"in{trial}"), os.path.join(self.tmp, f"late{trial}")
                self.inbox = inbox
                manifest = generate_samples.generate(inbox, late)
                generate_samples.deliver_late(inbox, late)
                order = days[:]
                rng.shuffle(order)
                for d in order:
                    self.run_day(d)
                got = {f"{p}|{i}": (e, a, c, t) for (p, i), (e, a, c, t) in self.orders().items()}
                exp = {k: (v["customer_email"], v["amount_minor"], v["currency"], v["placed_at"]) for k, v in manifest["expected_orders"].items()}
                self.assertEqual(got, exp, f"order {order}")


class AtomicityTests(Workspace):
    def test_failure_midway_through_a_file_loads_nothing_and_restores_old_rejects(self):
        self.write("acme", D, [row("acme", "OLD1"), row("acme", "OLDBAD", email="bad")])
        self.run_day(D)
        before_orders, before_rejects = snapshot(self), self.q("SELECT * FROM rejects")
        self.write("acme", D, [row("acme", f"NEW{i}") for i in range(20)] + [row("acme", "NEWBAD", ts="junk")])

        real, calls = Warehouse.stage_orders, []

        def flaky(self_, rows):
            calls.append(len(rows))
            if len(calls) == 3:
                raise RuntimeError("disk on fire")
            return real(self_, rows)

        with mock.patch.object(Warehouse, "stage_orders", flaky):
            s = self.run_day(D, batch_size=4)
        a = self.stat(s, "acme")
        self.assertEqual(a["status"], "failed")
        self.assertIn("disk on fire", a["error"])
        self.assertEqual(s["exit_code"], 1)
        self.assertEqual(snapshot(self), before_orders)
        self.assertEqual(self.q("SELECT * FROM rejects"), before_rejects)
        self.assertEqual(self.q("SELECT count(*) FROM file_loads WHERE run_id = ?", s["run_id"]), [(0,)])
        # and the very next run, with the fault gone, loads it fully
        self.assertEqual(self.stat(self.run_day(D, batch_size=4), "acme")["rows_out"], 20)

    def test_one_partner_blowing_up_does_not_stop_the_next_partner(self):
        self.write("acme", D, [row("acme", "A1")])
        self.write("bolt", D, [row("bolt", "B1")])
        self.write("cobalt", D, [row("cobalt", "C1")])
        real = Warehouse.stage_orders

        def boom_for_bolt(self_, rows):
            if rows and rows[0]["partner"] == "bolt":
                raise RuntimeError("bolt exploded")
            return real(self_, rows)

        with mock.patch.object(Warehouse, "stage_orders", boom_for_bolt):
            s = self.run_day(D)
        self.assertEqual([p["status"] for p in s["partners"]], ["ok", "failed", "ok"])
        self.assertEqual({k[0] for k in self.orders()}, {"acme", "cobalt"})
        self.assertEqual(s["status"], "failed")
        self.assertEqual(self.q("SELECT status FROM pipeline_runs WHERE run_id = ?", s["run_id"]), [("failed",)])

    def test_breaking_a_previously_good_file_leaves_loaded_data_and_rejects_intact(self):
        self.write("acme", D, [row("acme", "A"), row("acme", "B", email="bad")])
        self.run_day(D)
        before = (snapshot(self), self.q("SELECT * FROM rejects"))
        self.write("acme", D, [row("acme", "A")], header=["order_id", "email", "amount", "currency"])  # lost a column
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "failed")
        self.assertEqual((snapshot(self), self.q("SELECT * FROM rejects")), before)
        self.write("acme", D, [row("acme", "A"), row("acme", "B")])
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "ok")
        self.assertEqual(self.reject_reasons(), {})

    def test_database_stays_usable_after_a_failed_run(self):
        self.write("acme", D, [row("acme", "A")], header=["nope"])
        self.run_day(D)
        wh = Warehouse(self.db)
        self.assertFalse(wh.conn.in_transaction)
        wh.close()
        self.write("acme", D, [row("acme", "A")])
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "ok")
