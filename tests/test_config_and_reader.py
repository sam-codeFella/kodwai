"""Config validation, header resolution and the small pure functions behind them."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from datetime import timezone, timedelta

from helpers import CONFIG, ROOT, Workspace, row  # noqa: F401  (sets sys.path)
from etl.config import ConfigError, load_config
from etl.parsers import PARSERS, Ctx, RowReject, parse_tz
from etl.reader import SchemaError, resolve_header


def base_doc():
    with open(CONFIG) as fh:
        return json.load(fh)


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def load(self, doc):
        p = os.path.join(self.tmp, "c.json")
        with open(p, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return load_config(p)

    def assertRejected(self, doc, fragment):
        with self.assertRaises(ConfigError) as cm:
            self.load(doc)
        self.assertIn(fragment, str(cm.exception))

    def mutate(self, fn):
        d = copy.deepcopy(base_doc())
        fn(d["partners"])
        return d

    def test_shipped_config_loads_with_all_three_partners(self):
        cfg = self.load(base_doc())
        self.assertEqual(list(cfg), ["acme", "bolt", "cobalt"])

    def test_alias_shorthand_and_object_forms_are_equivalent(self):
        d = self.mutate(lambda p: p["acme"]["fields"].update(order_id=["order_id"]))
        d2 = self.mutate(lambda p: p["acme"]["fields"].update(order_id=[{"column": "order_id"}]))
        self.assertEqual(self.load(d)["acme"].fields["order_id"], self.load(d2)["acme"].fields["order_id"])

    def test_default_parsers_apply_when_none_given(self):
        a = self.load(base_doc())["acme"].fields
        self.assertEqual(a["customer_email"][0].parser, "email")
        self.assertEqual(a["currency"][0].parser, "currency")

    def test_broken_configs_are_rejected_with_a_useful_message(self):
        cases = [
            ("not json at all", "cannot read config"),
            ("[]", "partners"),
            ({}, "partners"),
            ({"partners": {}}, "partners"),
            (self.mutate(lambda p: p["acme"].pop("file_glob")), "file_glob"),
            (self.mutate(lambda p: p["acme"].update(file_glob="acme/*.csv")), "{date}"),
            (self.mutate(lambda p: p["acme"].update(timezone="Europe/Paris")), "timezone"),
            (self.mutate(lambda p: p["acme"].update(timezone="UTC+5")), "timezone"),
            (self.mutate(lambda p: p["acme"]["fields"].pop("amount")), "amount"),
            (self.mutate(lambda p: p["acme"]["fields"].update(amount=[])), "amount"),
            (self.mutate(lambda p: p["acme"]["fields"].update(bogus=["x"])), "bogus"),
            (self.mutate(lambda p: p["acme"]["fields"].update(amount=[{"column": "a", "parser": "nope"}])), "nope"),
            (self.mutate(lambda p: p["acme"]["fields"].update(amount=[{"parser": "decimal_major"}])), "column"),
            (self.mutate(lambda p: p["acme"]["fields"].update(amount=[{"column": "a", "colour": "red"}])), "colour"),
            (self.mutate(lambda p: p["acme"]["fields"].update(amount=[{"column": "a", "effective_from": "05/01/2026"}])), "effective_from"),
            (self.mutate(lambda p: p["acme"]["fields"].update(placed_at=[{"column": "t", "parser": "datetime_fmt"}])), "format"),
            (self.mutate(lambda p: p["acme"]["fields"].update(order_id=[42])), "alias"),
        ]
        for doc, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assertRejected(doc, fragment)

    def test_missing_config_file(self):
        with self.assertRaises(ConfigError):
            load_config(os.path.join(self.tmp, "nope.json"))

    def test_error_messages_name_the_partner_and_field(self):
        with self.assertRaises(ConfigError) as cm:
            self.load(self.mutate(lambda p: p["bolt"]["fields"].update(amount=[{"column": "a", "parser": "nope"}])))
        self.assertIn("bolt.amount", str(cm.exception))


class TimezoneParsingTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_tz("UTC"), timezone.utc)
        self.assertEqual(parse_tz(" UTC "), timezone.utc)
        self.assertEqual(parse_tz("UTC+05:30").utcoffset(None), timedelta(hours=5, minutes=30))
        self.assertEqual(parse_tz("UTC-08:00").utcoffset(None), timedelta(hours=-8))
        self.assertEqual(parse_tz("UTC+00:00").utcoffset(None), timedelta(0))

    def test_invalid(self):
        for bad in ("", "GMT", "UTC+5", "UTC+0530", "utc", "Europe/Paris", "UTC +05:00", "UTC+5:30"):
            with self.subTest(tz=bad), self.assertRaises(ValueError):
                parse_tz(bad)


class HeaderResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = load_config(CONFIG)

    def resolve(self, partner, header, date="2026-01-03"):
        return resolve_header(header, self.cfg[partner], date)

    def test_renamed_email_resolves_to_the_same_canonical_field(self):
        for col in ("email", "customerEmail"):
            r = self.resolve("acme", ["order_id", col, "amount", "currency", "placed_at"])
            self.assertEqual(r.bindings["customer_email"].index, 1)
            self.assertEqual(r.extra_columns, [])

    def test_amount_alias_selects_the_parser(self):
        a = self.resolve("acme", ["order_id", "email", "amount", "currency", "placed_at"]).bindings["amount"].alias
        b = self.resolve("acme", ["order_id", "email", "amount_cents", "currency", "placed_at"]).bindings["amount"].alias
        self.assertEqual((a.parser, b.parser), ("decimal_major", "minor_int"))

    def test_effective_from_boundaries(self):
        h = ["OrderId", "customer_email", "total", "ccy", "order_ts"]
        got = {d: self.resolve("bolt", h, d).bindings["amount"].alias.parser
               for d in ("2025-12-31", "2026-01-04", "2026-01-05", "2026-01-06", "2030-01-01")}
        self.assertEqual(got, {"2025-12-31": "decimal_major", "2026-01-04": "decimal_major", "2026-01-05": "minor_int",
                               "2026-01-06": "minor_int", "2030-01-01": "minor_int"})

    def test_missing_columns_are_all_reported_on_the_exception(self):
        with self.assertRaises(SchemaError) as cm:
            self.resolve("acme", ["order_id", "note"])
        self.assertEqual(set(cm.exception.missing), {"customer_email", "amount", "currency", "placed_at"})

    def test_extra_columns_keep_header_order(self):
        r = self.resolve("acme", ["zzz", "order_id", "email", "aaa", "amount", "currency", "placed_at"])
        self.assertEqual(r.extra_columns, ["zzz", "aaa"])
        self.assertEqual(r.width, 7)

    def test_a_column_name_valid_for_another_partner_does_not_count(self):
        with self.assertRaises(SchemaError):
            self.resolve("acme", ["OrderId", "customer_email", "total", "ccy", "order_ts"])


class ParserCornerTests(unittest.TestCase):
    def test_every_registered_parser_is_exercised_by_config_or_tests(self):
        used = {a.parser for p in load_config(CONFIG).values() for al in p.fields.values() for a in al}
        self.assertTrue({"decimal_major", "minor_int", "iso8601", "epoch_s", "datetime_fmt", "email", "currency", "text"} <= used | {"text"})
        self.assertEqual(set(PARSERS), {"text", "email", "currency", "decimal_major", "minor_int", "iso8601", "epoch_s", "datetime_fmt"})

    def test_decimal_precision_is_exact_for_values_floats_get_wrong(self):
        ctx = Ctx(timezone.utc, "USD")
        for text, minor in (("0.1", 10), ("0.7", 70), ("1.005", None), ("2.675", None), ("4.35", 435), ("8.2", 820), ("1.1", 110)):
            with self.subTest(text=text):
                if minor is None:
                    with self.assertRaises(RowReject):
                        PARSERS["decimal_major"](text, None, ctx)
                else:
                    self.assertEqual(PARSERS["decimal_major"](text, None, ctx), minor)

    def test_decimal_major_without_a_known_currency_defaults_to_two_places(self):
        self.assertEqual(PARSERS["decimal_major"]("1.5", None, Ctx(timezone.utc)), 150)

    def test_epoch_and_iso_agree_on_the_same_instant(self):
        ctx = Ctx(timezone.utc)
        self.assertEqual(PARSERS["epoch_s"]("1767434400", None, ctx), PARSERS["iso8601"]("2026-01-03T10:00:00Z", None, ctx))
