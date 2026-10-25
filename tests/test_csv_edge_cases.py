"""File-level oddities: encodings, line endings, quoting, odd headers, damaged files."""
import os

from helpers import HEADERS, Workspace, row

D = "2026-01-03"


class FormattingTests(Workspace):
    def test_utf8_bom_is_tolerated(self):
        self.write("acme", D, [row("acme", "B1")], bom=True)
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "ok")
        self.assertIn(("acme", "B1"), self.orders())

    def test_crlf_line_endings(self):
        self.write("acme", D, [row("acme", "C1"), row("acme", "C2")], lineterminator="\r\n")
        self.run_day(D)
        self.assertEqual({k[1] for k in self.orders()}, {"C1", "C2"})

    def test_no_trailing_newline_at_end_of_file(self):
        self.write_raw("acme", D, ("\n".join([",".join(HEADERS["acme"]), ",".join(row("acme", "E1"))])).encode())
        self.run_day(D)
        self.assertIn(("acme", "E1"), self.orders())

    def test_blank_lines_are_ignored_and_not_counted(self):
        text = ",".join(HEADERS["acme"]) + "\n\n" + ",".join(row("acme", "L1")) + "\n\n\n" + ",".join(row("acme", "L2")) + "\n\n"
        self.write_raw("acme", D, text.encode())
        s = self.run_day(D)
        self.assertEqual((self.stat(s, "acme")["rows_in"], self.stat(s, "acme")["rows_out"]), (2, 2))

    def test_quoted_fields_with_commas_quotes_and_newlines(self):
        self.write("acme", D, [row("acme", 'Q,"1"'), row("acme", "multi\nline"), row("acme", "plain")])
        self.run_day(D)
        self.assertEqual({k[1] for k in self.orders()}, {'Q,"1"', "multi\nline", "plain"})

    def test_reject_line_numbers_are_physical_lines_even_after_multiline_fields(self):
        # header = line 1, multiline id spans lines 2-3, bad row is on line 4
        self.write("acme", D, [row("acme", "multi\nline"), row("acme", "BAD", email="nope")])
        self.run_day(D)
        self.assertEqual(self.q("SELECT line_no, reason FROM rejects"), [(4, "invalid_email")])

    def test_semicolon_delimited_file_fails_loudly_rather_than_guessing(self):
        text = ";".join(HEADERS["acme"]) + "\n" + ";".join(row("acme", "S1")) + "\n"
        self.write_raw("acme", D, text.encode())
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "failed")
        self.assertEqual(s["exit_code"], 1)
        self.assertEqual(self.orders(), {})

    def test_unicode_survives_into_orders_and_reject_payloads(self):
        self.write("acme", D, [row("acme", "Ünï-1", email="zoë@exämple.com"), row("acme", "Ünï-2", email="日本語")])
        self.run_day(D)
        self.assertEqual(self.orders()[("acme", "Ünï-1")][0], "zoë@exämple.com")
        raw = self.q("SELECT raw_row FROM rejects")[0][0]
        self.assertIn("日本語", raw)  # stored readable, not \u-escaped


class HeaderTests(Workspace):
    def test_header_whitespace_is_trimmed(self):
        self.write("acme", D, [row("acme", "H1")], header=[" order_id ", "email\t", " amount", "currency ", " placed_at "])
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "ok")

    def test_column_order_does_not_matter(self):
        h = HEADERS["acme"]
        order = [4, 2, 0, 3, 1]
        self.write("acme", D, [[r[i] for i in order] for r in [row("acme", "O1", amount="1.00"), row("acme", "O2", amount="2.00")]],
                   header=[h[i] for i in order])
        self.run_day(D)
        o = self.orders()
        self.assertEqual((o[("acme", "O1")][1], o[("acme", "O2")][1]), (100, 200))
        self.assertEqual(o[("acme", "O1")][0], "u@example.com")

    def test_header_names_are_case_sensitive_so_a_case_change_is_a_loud_failure(self):
        self.write("acme", D, [row("acme")], header=["Order_ID", "email", "amount", "currency", "placed_at"])
        s = self.run_day(D)
        a = self.stat(s, "acme")
        self.assertEqual(a["status"], "failed")
        self.assertIn("order_id", a["error"])

    def test_many_extra_columns_are_tolerated_and_listed(self):
        h = HEADERS["acme"] + ["promo", "channel", "utm_source", "notes"]
        self.write("acme", D, [row("acme", "X1") + ["P", "web", "g", "hi"]], header=h)
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(a["extra_columns"], "promo,channel,utm_source,notes")

    def test_duplicate_header_names_use_the_first_and_report_the_second_as_extra(self):
        h = ["order_id", "email", "email", "amount", "currency", "placed_at"]
        self.write("acme", D, [["D1", "first@example.com", "second@example.com", "1.00", "USD", "2026-01-03T10:00:00Z"]], header=h)
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "ok")
        self.assertEqual(self.orders()[("acme", "D1")][0], "first@example.com")
        self.assertEqual(a["extra_columns"], "email")

    def test_trailing_comma_creates_an_unnamed_extra_column_that_is_tolerated(self):
        self.write("acme", D, [row("acme", "T1") + [""]], header=HEADERS["acme"] + [""])
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "ok")

    def test_both_old_and_new_amount_columns_present_prefers_the_first_configured(self):
        h = ["order_id", "email", "amount", "amount_cents", "currency", "placed_at"]
        self.write("acme", D, [["B1", "u@example.com", "10.00", "999", "USD", "2026-01-03T10:00:00Z"]], header=h)
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(self.orders()[("acme", "B1")][1], 1000)
        self.assertEqual(a["extra_columns"], "amount_cents")

    def test_every_missing_required_column_is_named_in_the_error(self):
        self.write("acme", D, [["a", "b"]], header=["order_id", "note"])
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "failed")
        for f in ("customer_email", "amount", "currency", "placed_at"):
            self.assertIn(f, a["error"])

    def test_header_only_file_is_a_successful_empty_load(self):
        self.write("acme", D, [])
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual((a["status"], a["rows_in"], a["rows_out"]), ("ok", 0, 0))
        self.assertEqual(self.q("SELECT count(*) FROM file_loads"), [(1,)])

    def test_zero_byte_file_fails_loudly(self):
        self.write_raw("acme", D, b"")
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "failed")
        self.assertIn("empty", a["error"])

    def test_whitespace_only_file_fails_loudly(self):
        self.write_raw("acme", D, b"\n\n   \n")
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "failed")

    def test_another_partners_header_in_this_partners_file_fails(self):
        self.write("acme", D, [row("bolt")], header=HEADERS["bolt"])
        self.assertEqual(self.stat(self.run_day(D), "acme")["status"], "failed")


class RaggedRowTests(Workspace):
    def test_too_few_and_too_many_fields_are_rejected_with_the_raw_values_kept(self):
        good = row("acme", "G1")
        self.write("acme", D, [good, good[:-1], good + ["extra"], ["only-one"], ["", "", "", "", "", ""], good[:2]])
        self.run_day(D)
        self.assertEqual(list(self.orders()), [("acme", "G1")])
        self.assertEqual(self.q("SELECT reason, count(*) FROM rejects GROUP BY 1"), [("wrong_column_count", 5)])
        for (raw,) in self.q("SELECT raw_row FROM rejects"):
            self.assertIn("__values__", raw)
            self.assertIn("__header__", raw)

    def test_ragged_row_does_not_poison_following_rows(self):
        self.write("acme", D, [row("acme", "A"), ["broken"], row("acme", "B")])
        self.run_day(D)
        self.assertEqual({k[1] for k in self.orders()}, {"A", "B"})


class DamagedFileTests(Workspace):
    def test_giant_field_over_csv_limit_fails_the_file_and_loads_nothing(self):
        self.write("acme", D, [row("acme", "A"), row("acme", "B" * 200_000), row("acme", "C")])
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "failed")
        self.assertEqual(self.orders(), {})

    def test_nul_byte_fails_the_file_and_loads_nothing(self):
        text = ",".join(HEADERS["acme"]) + "\n" + ",".join(row("acme", "N\x001")) + "\n" + ",".join(row("acme", "N2")) + "\n"
        self.write_raw("acme", D, text.encode())
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "failed")
        self.assertEqual(self.orders(), {})

    def test_invalid_utf8_late_in_a_large_file_rolls_back_the_rows_already_flushed(self):
        rows = [row("acme", f"U{i}", email=f"user{i}@example.com") for i in range(600)]
        p = self.write("acme", D, rows)
        with open(p, "ab") as fh:
            fh.write(b"U-bad,caf\xe9@example.com,1.00,USD,2026-01-03T10:00:00Z\n")  # latin-1 byte, not utf-8
        self.assertGreater(os.path.getsize(p), 16384, "file must span several decode blocks for this test to mean anything")
        s = self.run_day(D, batch_size=10)
        a = self.stat(s, "acme")
        self.assertEqual(a["status"], "failed")
        self.assertIn("UnicodeDecodeError", a["error"])
        self.assertEqual(self.orders(), {}, "rows flushed before the decode error must be rolled back")
        self.assertEqual(self.q("SELECT count(*) FROM file_loads"), [(0,)])
        self.assertEqual(s["exit_code"], 1)

    def test_truncated_file_with_open_quote_does_not_crash(self):
        text = ",".join(HEADERS["acme"]) + "\n" + ",".join(row("acme", "T1")) + '\nT2,"never closed,1.00,USD,2026-01-03T10:00:00Z\n'
        self.write_raw("acme", D, text.encode())
        a = self.stat(self.run_day(D), "acme")
        self.assertEqual(a["status"], "ok")
        self.assertIn(("acme", "T1"), self.orders())
        self.assertEqual(a["rows_rejected"], 1)

    def test_unreadable_entry_fails_that_file_but_not_the_run(self):
        os.makedirs(os.path.join(self.inbox, "acme", f"acme_{D}_dir.csv"))  # matches the glob, can't be read as a file
        self.write("bolt", D, [row("bolt", "OK")])
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "failed")
        self.assertEqual(self.stat(s, "bolt")["status"], "ok")
        self.assertEqual(s["exit_code"], 1)
        self.assertIn(("bolt", "OK"), self.orders())


class MissingInboxTests(Workspace):
    def test_nonexistent_inbox_means_every_partner_is_missing_not_an_error(self):
        s = self.run_day(D)
        self.assertEqual(s["exit_code"], 0)
        self.assertEqual({p["status"] for p in s["partners"]}, {"missing"})
        self.assertEqual(len(s["partners"]), 3)

    def test_files_for_other_dates_are_not_picked_up(self):
        self.write("acme", "2026-01-02", [row("acme", "D2")])
        self.write("acme", "2026-01-04", [row("acme", "D4")])
        self.write("acme", D, [row("acme", "D3")])
        s = self.run_day(D)
        self.assertEqual([p["source_file"] for p in s["partners"] if p["partner"] == "acme"], [f"acme/acme_{D}.csv"])
        self.assertEqual(list(self.orders()), [("acme", "D3")])

    def test_non_csv_and_other_partners_files_are_ignored(self):
        self.write_raw("acme", D, b"junk", name=f"acme_{D}.txt")
        self.write_raw("acme", D, b"junk", name=f"readme_{D}.csv")
        self.write("bolt", D, [row("bolt", "OK")])
        s = self.run_day(D)
        self.assertEqual(self.stat(s, "acme")["status"], "missing")
        self.assertEqual(self.stat(s, "bolt")["status"], "ok")

    def test_extra_files_for_the_same_date_are_each_loaded_and_reported(self):
        self.write("acme", D, [row("acme", "P1")])
        self.write("acme", D, [row("acme", "P2")], name=f"acme_{D}_part2.csv")
        s = self.run_day(D)
        files = sorted(p["source_file"] for p in s["partners"] if p["partner"] == "acme")
        self.assertEqual(files, [f"acme/acme_{D}.csv", f"acme/acme_{D}_part2.csv"])
        self.assertEqual({k[1] for k in self.orders()}, {"P1", "P2"})
