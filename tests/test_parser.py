import unittest
from decimal import Decimal

from signalbot import Side, Status, normalize, parse_signal

from .corpus import IGNORED, REJECTED, VALID


def D(values):
    return tuple(Decimal(v) for v in values)


class ValidCorpus(unittest.TestCase):
    def test_all_valid_messages_are_parsed_exactly(self):
        for case in VALID:
            with self.subTest(case["name"]):
                result = parse_signal(case["text"])
                self.assertEqual(result.status, Status.SIGNAL, result.reason)
                sig, exp = result.signal, case["expect"]
                self.assertEqual(sig.symbol, exp["symbol"])
                self.assertEqual(sig.side, Side(exp["side"]))
                self.assertEqual(sig.entries, D(exp["entries"]))
                self.assertEqual(sig.stop_loss, Decimal(exp["sl"]))
                self.assertEqual(sig.take_profits, D(exp["tps"]))
                self.assertEqual(sig.leverage, exp.get("leverage"))
                self.assertEqual(sig.is_market, exp.get("market", False))


class NotTradable(unittest.TestCase):
    def test_chatter_updates_and_ads_are_ignored(self):
        for case in IGNORED:
            with self.subTest(case["name"]):
                result = parse_signal(case["text"])
                self.assertEqual(result.status, Status.IGNORED, f"{result.status} {result.reason}")
                self.assertIsNone(result.signal)

    def test_incomplete_or_ambiguous_signals_are_rejected(self):
        for case in REJECTED:
            with self.subTest(case["name"]):
                result = parse_signal(case["text"])
                self.assertEqual(result.status, Status.REJECTED, f"{result.status} {result.reason}")
                self.assertIsNone(result.signal)
                self.assertIn(case["reason"], result.reason)


class Details(unittest.TestCase):
    def test_none_and_garbage_input_never_raise(self):
        for text in (None, "", "\x00\x01", "SL " * 5000, "9" * 10000):
            with self.subTest(repr(text)[:20]):
                self.assertIn(parse_signal(text).status, set(Status))

    def test_take_profits_are_ordered_nearest_first(self):
        long_ = parse_signal("BTCUSDT LONG Entry 65000 SL 64000 TP3 68000 TP1 66000 TP2 67000").signal
        short = parse_signal("BTCUSDT SHORT Entry 65000 SL 66000 TP1 64000 TP2 63000 TP3 62000").signal
        self.assertEqual(long_.take_profits, D(["66000", "67000", "68000"]))
        self.assertEqual(short.take_profits, D(["64000", "63000", "62000"]))

    def test_fingerprint_ignores_wording_and_emojis_but_not_prices(self):
        a = parse_signal("BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP: 67000").signal
        b = parse_signal("🚀 #BTC/USDT 🟢 long 🚀 entry 65000 / stop 64000 / target 67000").signal
        c = parse_signal("BTCUSDT LONG\nEntry: 65000\nSL: 63900\nTP: 67000").signal
        self.assertEqual(a.fingerprint, b.fingerprint)
        self.assertNotEqual(a.fingerprint, c.fingerprint)

    def test_prices_are_decimal_not_float(self):
        sig = parse_signal("DOGEUSDT LONG Entry 0.1 SL 0.09 TP 0.12").signal
        self.assertIsInstance(sig.entries[0], Decimal)
        self.assertEqual(sig.entries[0], Decimal("0.1"))  # no 0.1000000000000000055...

    def test_decimal_comma_vs_thousands_comma(self):
        sig = parse_signal("XRPUSDT LONG Entry 0,500 SL 0,48 TP 0,55").signal
        self.assertEqual(sig.entries, D(["0.5"]))
        sig = parse_signal("BTCUSDT LONG Entry 65,500 SL 64,000 TP 67,000").signal
        self.assertEqual(sig.entries, D(["65500"]))

    def test_normalize_strips_emojis_markdown_and_fullwidth(self):
        self.assertEqual(normalize("🔥**ＢＴＣ**  _long_ 1️⃣"), "BTC long")


if __name__ == "__main__":
    unittest.main()
