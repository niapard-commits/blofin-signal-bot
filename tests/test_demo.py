import random
import unittest
from datetime import datetime, timezone
from decimal import Decimal as D
from itertools import product

from signalbot import Status, parse_signal
from signalbot import demo as demo_mod
from signalbot.demo import OTHER_STYLE, STYLES, SYMBOLS, bad_text, build, pick, render
from signalbot.exchange import MockExchange
from signalbot.executor import execute_signal

PRICES = {"BTC": D("84500"), "ETH": D("2494.6"), "SOL": D("108.23"), "XRP": D("1.3892")}
NOW = datetime(2026, 10, 21, 20, 0, tzinfo=timezone.utc)


def exchange():
    ex = MockExchange(balance="5000")
    for sym, price in PRICES.items():
        ex.set_price(f"{sym}-USDT", price)
    return ex


class Generated(unittest.TestCase):
    def test_every_style_side_symbol_parses_and_passes_every_safety_check(self):
        for seed, (sym, side, style) in enumerate(product(SYMBOLS, ("LONG", "SHORT"), STYLES)):
            for k in range(5):
                rng = random.Random(seed * 10 + k)
                v = build(sym, side, PRICES[sym], rng, style)
                text = render(v)
                with self.subTest(sym=sym, side=side, style=style, k=k, text=text):
                    parsed = parse_signal(text)
                    self.assertIs(parsed.status, Status.SIGNAL, parsed.reason)
                    self.assertEqual((parsed.signal.symbol, parsed.signal.side.value),
                                     (f"{sym}-USDT", side))
                    result = execute_signal(parsed.signal, exchange(), signal_time=NOW, now=NOW,
                                            dry_run=True)
                    self.assertTrue(result.ok, result.reason)

    def test_same_trade_in_another_style_is_the_same_fingerprint(self):
        for style in STYLES:
            v = build("ETH", "SHORT", PRICES["ETH"], random.Random(7), style)
            a = parse_signal(render(v)).signal
            b = parse_signal(render(v, OTHER_STYLE[style])).signal
            with self.subTest(style=style):
                self.assertEqual(a.fingerprint, b.fingerprint)

    def test_decimal_shift_typo_is_refused_by_the_pipeline(self):
        for sym in SYMBOLS:
            v = build(sym, "LONG", PRICES[sym], random.Random(3), "clean")
            parsed = parse_signal(bad_text(v))
            if parsed.status is not Status.SIGNAL:
                continue  # already refused by the parser: fine too
            result = execute_signal(parsed.signal, exchange(), signal_time=NOW, now=NOW)
            with self.subTest(sym=sym):
                self.assertFalse(result.ok)
                self.assertFalse(result.order_attempted)

    def test_pick_keeps_a_requested_symbol(self):
        self.assertEqual(pick(random.Random(1), "SOL")[0], "SOL")
        self.assertIn(pick(random.Random(1))[0], SYMBOLS)


class Show(unittest.TestCase):
    prices = staticmethod(lambda sym: PRICES[sym])

    def test_program_always_contains_market_entries_that_leave_open_positions(self):
        for n, minimum in ((8, 2), (16, 2), (48, 6), (80, 10)):
            for seed in range(20):
                program = demo_mod.build_program(n, random.Random(seed))
                self.assertGreaterEqual(program.count("valid_market"), minimum, (n, seed))
                self.assertEqual(program[0], "valid_limit")
                self.assertEqual((program[n // 2], program[n - 2]), ("stop_scene", "margin_scene"))

    def test_program_has_the_scenes_and_a_valid_start(self):
        for n in (8, 24, 48):
            program = demo_mod.build_program(n, random.Random(n))
            self.assertEqual(len(program), n)
            self.assertEqual(program[0], "valid_limit")
            self.assertEqual(program[n // 2], "stop_scene")
            self.assertEqual(program[n - 2], "margin_scene")

    def test_a_dup_never_comes_before_a_valid_signal(self):
        for seed in range(200):
            program = demo_mod.build_program(12, random.Random(seed))
            first_valid = next(i for i, k in enumerate(program) if k.startswith("valid"))
            self.assertTrue(all(k != "dup" for k in program[:first_valid]))

    def test_every_kind_does_what_it_promises(self):
        ex = exchange()
        previous = None
        for seed in range(40):
            for kind in demo_mod.WEIGHTS:
                rng = random.Random(seed)
                if kind == "dup" and previous is None:
                    previous = build("BTC", "LONG", PRICES["BTC"], random.Random(1), "clean")
                text, values = demo_mod.make_event(kind, rng, self.prices, previous)
                parsed = parse_signal(text)
                with self.subTest(kind=kind, seed=seed, text=text):
                    if kind.startswith("valid"):
                        self.assertIs(parsed.status, Status.SIGNAL, parsed.reason)
                        self.assertIsNotNone(values)
                        r = execute_signal(parsed.signal, ex, signal_time=NOW, now=NOW, dry_run=True)
                        self.assertTrue(r.ok, r.reason)
                        if kind == "valid_market":
                            self.assertTrue(parsed.signal.is_market)
                        if kind == "valid_highlev":
                            self.assertEqual(r.request.leverage, 20)   # 50x asked, capped
                    elif kind == "ignored":
                        self.assertIs(parsed.status, Status.IGNORED, parsed.reason)
                    elif kind == "rejected":
                        self.assertIs(parsed.status, Status.REJECTED)
                    elif kind == "dup":
                        self.assertEqual(parsed.signal.fingerprint,
                                         parse_signal(render(previous)).signal.fingerprint)
                    else:  # typo, wrong_geometry, unknown_symbol: must NEVER reach an order
                        if parsed.status is Status.SIGNAL:
                            r = execute_signal(parsed.signal, ex, signal_time=NOW, now=NOW)
                            self.assertFalse(r.ok)
                            self.assertEqual(ex.orders, [])
                        else:
                            self.assertIs(parsed.status, Status.REJECTED)


if __name__ == "__main__":
    unittest.main()
