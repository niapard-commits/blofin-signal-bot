import unittest
from decimal import Decimal as D

from signalbot import Instrument, Side, Signal, SizingConfig, SizingError, plan_position

BTC = Instrument("BTC-USDT", contract_value=D("0.001"), lot_size=D("0.1"), min_size=D("0.1"),
                 max_leverage=100)


def sig(side="LONG", entries=("65000",), sl="64000", tps=("67000",), lev=None):
    return Signal("BTC-USDT", Side(side), tuple(D(e) for e in entries), D(sl),
                  tuple(D(t) for t in tps), lev)


class Sizing(unittest.TestCase):
    def test_allocation_and_leverage(self):
        # 1000 USDT * 25% = 250 margin, x10 = 2500 notional, /65000 = 0.03846 BTC
        # = 38.46 contracts -> rounded DOWN to 38.4 (lot 0.1)
        p = plan_position(sig(lev=10), D("1000"), BTC, SizingConfig(allocation_pct=D("25")))
        self.assertEqual(p.leverage, 10)
        self.assertEqual(p.contracts, D("38.4"))
        self.assertEqual(p.notional, D("38.4") * D("0.001") * D("65000"))
        self.assertEqual(p.margin, p.notional / 10)
        self.assertLessEqual(p.margin, D("250"))  # never more than the budget
        self.assertEqual(p.risk_at_stop, D("38.4") * D("0.001") * D("1000"))

    def test_signal_leverage_is_capped_by_config(self):
        p = plan_position(sig(lev=50), D("1000"), BTC, SizingConfig(max_leverage=20))
        self.assertEqual(p.leverage, 20)

    def test_default_leverage_when_signal_has_none(self):
        p = plan_position(sig(), D("1000"), BTC, SizingConfig(default_leverage=5, max_leverage=20))
        self.assertEqual(p.leverage, 5)

    def test_risk_per_trade_cap(self):
        # risk 0.5% of 1000 = 5 USDT; stop is 1000 away -> 0.005 BTC = 5 contracts
        cfg = SizingConfig(risk_per_trade_pct=D("0.5"))
        p = plan_position(sig(lev=10), D("1000"), BTC, cfg)
        self.assertEqual(p.contracts, D("5.0"))
        self.assertLessEqual(p.risk_at_stop, D("5"))

    def test_stop_too_close_to_liquidation_is_refused(self):
        # 20x -> liquidation ~5% away; stop 4.5% away exceeds the 80% buffer (4%)
        with self.assertRaises(SizingError):
            plan_position(sig(sl="62075", lev=20), D("1000"), BTC, SizingConfig(max_leverage=20))
        # same stop at 10x is fine
        plan_position(sig(sl="62075", lev=10), D("1000"), BTC, SizingConfig())

    def test_balance_too_small_for_minimum_order(self):
        with self.assertRaises(SizingError):
            plan_position(sig(lev=10), D("1"), BTC, SizingConfig())

    def test_invalid_balance_and_allocation(self):
        with self.assertRaises(SizingError):
            plan_position(sig(), D("0"), BTC)
        with self.assertRaises(SizingError):
            plan_position(sig(), D("1000"), BTC, SizingConfig(allocation_pct=D("150")))

    def test_short_uses_lowest_entry_of_zone_as_worst_case(self):
        s = sig("SHORT", ("64900", "65100"), "66000", ("64000",), lev=10)
        p = plan_position(s, D("1000"), BTC)
        self.assertEqual(p.reference_price, D("64900"))

    def test_long_uses_highest_entry_of_zone_as_worst_case(self):
        s = sig("LONG", ("64900", "65100"), "64000", ("66000",), lev=10)
        self.assertEqual(plan_position(s, D("1000"), BTC).reference_price, D("65100"))

    def test_market_entry_uses_live_price(self):
        s = sig(entries=(), lev=10)
        p = plan_position(s, D("1000"), BTC, last_price=D("65500"))
        self.assertEqual(p.reference_price, D("65500"))
        with self.assertRaises(SizingError):
            plan_position(s, D("1000"), BTC)


if __name__ == "__main__":
    unittest.main()
