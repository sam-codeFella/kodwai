"""Value-level behaviour, driven through the real pipeline: every messy spelling of a
field either normalizes to the right canonical value or is rejected with the right reason."""
from helpers import Rej, Workspace, epoch, row


class AmountTests(Workspace):
    def test_decimal_major_spellings_all_mean_the_same_money(self):
        same = ["42.5", "42.50", "042.5", "+42.5", "4.25e1", " 42.50 ", "42.500", "4250E-2"]
        self.check_field("amount", [(v, 4250) for v in same])

    def test_decimal_major_edge_values(self):
        self.check_field("amount", [
            ("0", 0), ("0.00", 0), (".5", 50), ("42.", 4200), ("0.01", 1), ("0.29", 29), ("1.15", 115),
            ("19.99", 1999), ("1000000", 100000000), ("999999999999.99", 99999999999999),
        ])

    def test_decimal_major_garbage_is_rejected_not_guessed(self):
        self.check_field("amount", [
            ("abc", Rej("invalid_amount")), ("1,234.50", Rej("invalid_amount")), ("42,50", Rej("invalid_amount")),
            ("$42.50", Rej("invalid_amount")), ("42.50 USD", Rej("invalid_amount")), ("4 2", Rej("invalid_amount")),
            ("0x10", Rej("invalid_amount")), ("NaN", Rej("invalid_amount")), ("nan", Rej("invalid_amount")),
            ("Infinity", Rej("invalid_amount")), ("-Infinity", Rej("invalid_amount")), ("1e400", Rej("invalid_amount")),
            ("--5", Rej("invalid_amount")), ("42.50.1", Rej("invalid_amount")), ("", Rej("missing_amount")),
            ("   ", Rej("missing_amount")), ("1000000000000000", Rej("invalid_amount")),
        ])

    def test_sub_minor_unit_precision_and_negatives(self):
        self.check_field("amount", [
            ("42.505", Rej("amount_not_integral_minor_units")), ("0.001", Rej("amount_not_integral_minor_units")),
            ("-0.01", Rej("negative_amount")), ("-42.50", Rej("negative_amount")), ("-1e2", Rej("negative_amount")),
        ])

    def test_jpy_has_no_minor_unit_but_other_currencies_do(self):
        rows = [row("acme", "J1", amount="4200", ccy="JPY"), row("acme", "J2", amount="42.5", ccy="JPY"),
                row("acme", "B1", amount="1.234", ccy="BHD"), row("acme", "U1", amount="1.234", ccy="USD")]
        self.write("acme", "2026-01-03", rows)
        self.run_day("2026-01-03")
        o = self.orders("acme")
        self.assertEqual(o[("acme", "J1")][1], 4200)
        self.assertEqual(o[("acme", "B1")][1], 1234)
        self.assertEqual(self.reject_reasons("acme"), {"J2": "amount_not_integral_minor_units",
                                                      "U1": "amount_not_integral_minor_units"})

    def test_minor_units_column_spellings(self):
        # acme switches to amount_cents (integer minor units) from day 5 on
        rows = [row("acme", f"M{i}", amount=v) for i, v in enumerate(["4250", "4250.0", " 4250 ", "4.25e3", "0", "+4250"])]
        rows += [row("acme", "F1", amount="42.5"), row("acme", "N1", amount="-5"), row("acme", "T1", amount="abc")]
        self.write("acme", "2026-01-05", rows, header=["order_id", "email", "amount_cents", "currency", "placed_at"])
        self.run_day("2026-01-05")
        o = self.orders("acme")
        self.assertEqual([o[("acme", f"M{i}")][1] for i in range(6)], [4250, 4250, 4250, 4250, 0, 4250])
        self.assertEqual(self.reject_reasons("acme"), {
            "F1": "amount_not_integral_minor_units", "N1": "negative_amount", "T1": "invalid_amount"})


class EffectiveDateUnitTests(Workspace):
    """bolt keeps the column name `total` but changes meaning on 2026-01-05."""

    def test_same_header_different_meaning_by_date(self):
        self.write("bolt", "2026-01-04", [row("bolt", "B4", amount="42.50")])
        self.write("bolt", "2026-01-05", [row("bolt", "B5", amount="4250")])
        self.write("bolt", "2026-01-07", [row("bolt", "B7", amount="4250")])
        for d in ("2026-01-04", "2026-01-05", "2026-01-07"):
            self.run_day(d)
        o = self.orders("bolt")
        self.assertEqual([o[("bolt", k)][1] for k in ("B4", "B5", "B7")], [4250, 4250, 4250])
        self.assertEqual(self.reject_reasons("bolt"), {})

    def test_wrong_units_for_the_date_are_rejected_loudly_not_silently_corrupted(self):
        # A pre-cutover spelling ("42.50") in a post-cutover file must not become 42 or 4250 by accident.
        self.write("bolt", "2026-01-05", [row("bolt", "W1", amount="42.50"), row("bolt", "W2", amount="4250")])
        self.run_day("2026-01-05")
        self.assertEqual(self.reject_reasons("bolt"), {"W1": "amount_not_integral_minor_units"})
        self.assertEqual(self.orders("bolt")[("bolt", "W2")][1], 4250)

    def test_post_cutover_integer_in_a_pre_cutover_file_is_major_units(self):
        # "4250" before the cutover means 4250.00, i.e. 425000 minor units: units come from config, never from magnitude.
        self.write("bolt", "2026-01-04", [row("bolt", "P1", amount="4250")])
        self.run_day("2026-01-04")
        self.assertEqual(self.orders("bolt")[("bolt", "P1")][1], 425000)


class TimestampTests(Workspace):
    def test_acme_iso8601_variants_normalize_to_utc(self):
        z = "2026-01-03T14:22:05Z"
        self.check_field("ts", [
            ("2026-01-03T14:22:05Z", z), ("2026-01-03T14:22:05z", z), ("2026-01-03T14:22:05+00:00", z),
            ("2026-01-03T19:52:05+05:30", z), ("2026-01-03T09:22:05-05:00", z), ("2026-01-03T19:52:05+0530", z),
            ("2026-01-03T09:22:05-0500", z), ("2026-01-03 14:22:05", z), ("2026-01-03T14:22:05.123Z", z),
            ("2026-01-03T14:22:05.123456+00:00", z), ("2026-01-03", "2026-01-03T00:00:00Z"),
            ("2026-01-03T23:59:59-12:00", "2026-01-04T11:59:59Z"), ("2025-12-31T23:30:00-01:00", "2026-01-01T00:30:00Z"),
        ])

    def test_acme_iso8601_garbage_is_rejected(self):
        bad = Rej("invalid_timestamp")
        self.check_field("ts", [
            ("yesterday-ish", bad), ("2026-13-01T00:00:00Z", bad), ("2026-02-30T00:00:00Z", bad),
            ("2026-01-03T25:00:00Z", bad), ("01/03/2026", bad), ("1767448800", bad), ("1970-01-01T00:00:00Z", bad),
            ("1989-12-31T23:59:59Z", bad), ("2101-01-01T00:00:00Z", bad), ("0000-00-00", bad), ("N/A", bad),
            ("", Rej("missing_placed_at")),
        ])

    def test_bolt_epoch_seconds(self):
        z = "2026-01-03T10:00:00Z"
        bad = Rej("invalid_timestamp")
        self.check_field("ts", [
            (epoch(z), z), (epoch(z) + ".9", z), (" " + epoch(z) + " ", z), ("1.7674344e9", z),
            ("0", bad), ("-5", bad), ("abc", bad), ("1767434400000", bad), ("1e20", bad), ("nan", bad), ("inf", bad),
            ("2026-01-03T10:00:00Z", bad),
        ], partner="bolt")

    def test_cobalt_local_time_crosses_utc_midnight_and_year(self):
        self.check_field("ts", [
            ("01/03/2026 05:00", "2026-01-03T10:00:00Z"),   # UTC-5
            ("01/05/2026 23:30", "2026-01-06T04:30:00Z"),   # rolls into next day
            ("12/31/2025 20:00", "2026-01-01T01:00:00Z"),   # rolls into next year
            ("1/5/2026 9:05", "2026-01-05T14:05:00Z"),      # unpadded
            ("13/45/2026 10:00", Rej("invalid_timestamp")), ("01/05/2026", Rej("invalid_timestamp")),
            ("01/05/2026 10:00:30", Rej("invalid_timestamp")), ("2026-01-05 10:00", Rej("invalid_timestamp")),
            ("02/30/2026 10:00", Rej("invalid_timestamp")),
        ], partner="cobalt")


class EmailAndCurrencyTests(Workspace):
    def test_email_normalization_and_validation(self):
        bad = Rej("invalid_email")
        self.check_field("email", [
            ("Ava@Example.COM", "ava@example.com"), (" a@b.co ", "a@b.co"), ("first.last+tag@sub.example.org", "first.last+tag@sub.example.org"),
            ("josé@exämple.com", "josé@exämple.com"),
            ("a@b", bad), ("a b@c.com", bad), ("@c.com", bad), ("a@c.", bad), ("a@@c.com", bad), ("plain", bad),
            ("a@b@c.com", bad), ("a@ b.com", bad), ("", Rej("missing_customer_email")),
        ])

    def test_currency_normalization_and_validation(self):
        bad = Rej("invalid_currency")
        self.check_field("currency", [
            ("usd", "USD"), ("Eur", "EUR"), (" gbp ", "GBP"), ("chf", "CHF"),
            ("US", bad), ("USDD", bad), ("U$D", bad), ("123", bad), ("€", bad), ("", Rej("missing_currency")),
        ])


class OrderIdTests(Workspace):
    def test_ids_are_trimmed_but_otherwise_kept_verbatim_and_case_sensitive(self):
        rows = [row("acme", "  A1 "), row("acme", "a1"), row("acme", "A-é-1"), row("acme", "x" * 500), row("acme", "A,1"),
                row("acme", "A\n1")]
        self.write("acme", "2026-01-03", rows)
        self.run_day("2026-01-03")
        ids = {k[1] for k in self.orders("acme")}
        self.assertEqual(ids, {"A1", "a1", "A-é-1", "x" * 500, "A,1", "A\n1"})

    def test_blank_id_is_rejected(self):
        self.write("acme", "2026-01-03", [row("acme", ""), row("acme", "   "), row("acme", "ok")])
        self.run_day("2026-01-03")
        self.assertEqual(list(self.orders("acme")), [("acme", "ok")])
        self.assertEqual(sorted(self.q("SELECT reason FROM rejects")), [("missing_order_id",)] * 2)

    def test_same_id_in_different_partners_are_different_orders(self):
        self.write("acme", "2026-01-03", [row("acme", "SAME", amount="1.00")])
        self.write("bolt", "2026-01-03", [row("bolt", "SAME", amount="2.00")])
        self.write("cobalt", "2026-01-03", [row("cobalt", "SAME", amount="3.00")])
        self.run_day("2026-01-03")
        o = self.orders()
        self.assertEqual({k: v[1] for k, v in o.items()},
                         {("acme", "SAME"): 100, ("bolt", "SAME"): 200, ("cobalt", "SAME"): 300})


class PrecedenceTests(Workspace):
    def test_first_failing_field_names_the_reason(self):
        # id, email, currency, amount, timestamp are checked in that order.
        r = row("acme", "P1", email="bad", amount="abc", ccy="??", ts="junk")
        r2 = row("acme", "P2", amount="abc", ccy="??", ts="junk")
        r3 = row("acme", "P3", amount="abc", ts="junk")
        r4 = row("acme", "P4", ts="junk")
        self.write("acme", "2026-01-03", [r, r2, r3, r4])
        self.run_day("2026-01-03")
        self.assertEqual(self.reject_reasons("acme"), {
            "P1": "invalid_email", "P2": "invalid_currency", "P3": "invalid_amount", "P4": "invalid_timestamp"})
