import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from signalbot import Side, Signal, ValidationLimits, parse_signal, validate_signal


def sig(side="LONG", entries=("65000",), sl="64000", tps=("67000",), lev=None, symbol="BTC-USDT"):
    return Signal(
        symbol=symbol,
        side=Side(side),
        entries=tuple(D(e) for e in entries),
        stop_loss=D(sl),
        take_profits=tuple(D(t) for t in tps),
        leverage=lev,
    )


class Validation(unittest.TestCase):
    def check(self, signal, **kw):
        return validate_signal(signal, **kw)

    def test_good_long_and_short_pass(self):
        self.assertEqual(self.check(sig(), last_price=D("65020")), [])
        short = sig("SHORT", ("65000",), "66000", ("64000", "63000"))
        self.assertEqual(self.check(short, last_price=D("64990")), [])

    def test_long_with_stop_above_entry_is_rejected(self):
        errs = self.check(sig(sl="65500"))
        self.assertTrue(any("stop-loss" in e and "below entry" in e for e in errs), errs)

    def test_short_with_take_profit_above_entry_is_rejected(self):
        errs = self.check(sig("SHORT", ("65000",), "66000", ("65500",)))
        self.assertTrue(any("take-profit" in e for e in errs), errs)

    def test_typo_in_entry_breaks_geometry(self):
        # "Entry: 6500 SL 64000 TP: 67000" -> parsed fine, but nonsense as a trade
        parsed = parse_signal("BTCUSDT LONG Entry: 6500 SL 64000 TP: 67000")
        self.assertTrue(parsed.ok)
        self.assertTrue(self.check(parsed.signal))

    def test_stop_too_far_and_too_close(self):
        self.assertTrue(any("max" in e for e in self.check(sig(sl="50000"))))
        self.assertTrue(any("min" in e for e in self.check(sig(sl="64999.9"))))

    def test_absurd_take_profit(self):
        errs = self.check(sig(tps=("67000", "650000")))
        self.assertTrue(any("typo" in e for e in errs), errs)

    def test_entry_far_from_market_catches_scale_errors(self):
        errs = self.check(sig(), last_price=D("6500"))
        self.assertTrue(any("away from the live price" in e for e in errs), errs)

    def test_market_entry_needs_live_price(self):
        market = sig(entries=())
        self.assertTrue(any("live price" in e for e in self.check(market)))
        self.assertEqual(self.check(market, last_price=D("65000")), [])

    def test_symbol_allow_list(self):
        limits = ValidationLimits(allowed_symbols=frozenset({"ETH-USDT"}))
        self.assertTrue(any("not tradable" in e for e in validate_signal(sig(), limits)))

    def test_stale_signal(self):
        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        errs = self.check(sig(), signal_time=now - timedelta(minutes=10), now=now)
        self.assertTrue(any("stale" in e for e in errs), errs)
        self.assertEqual(self.check(sig(), signal_time=now - timedelta(seconds=5), now=now), [])

    def test_leverage_sanity(self):
        self.assertTrue(any("leverage" in e for e in self.check(sig(lev=0))))
        self.assertTrue(any("leverage" in e for e in self.check(sig(lev=500))))

    def test_entry_zone_must_not_contain_stop_or_targets(self):
        zone_sl = sig(entries=("64900", "65100"), sl="65000")
        self.assertTrue(self.check(zone_sl))
        zone_tp = sig(entries=("64900", "65100"), tps=("65000",))
        self.assertTrue(self.check(zone_tp))

    def test_min_reward_risk(self):
        limits = ValidationLimits(min_reward_risk=D("1"))
        weak = sig(tps=("65200",))  # reward 200 vs risk 1000
        self.assertTrue(any("reward/risk" in e for e in validate_signal(weak, limits)))


if __name__ == "__main__":
    unittest.main()
