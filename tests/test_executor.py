import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from signalbot import parse_signal
from signalbot.exchange import ExchangeError, MockExchange
from signalbot.executor import execute_signal, split_take_profits

BTC_LONG = "BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP1: 66000\nTP2: 67000\nLeverage: 10x"


def sig(text):
    result = parse_signal(text)
    assert result.ok, result.reason
    return result.signal


class SplitTakeProfits(unittest.TestCase):
    def test_even_split_adds_up(self):
        legs = split_take_profits((D("66000"), D("67000")), D("38.4"), D("0.1"))
        self.assertEqual(legs, ((D("66000"), D("19.2")), (D("67000"), D("19.2"))))

    def test_remainder_goes_to_the_nearest_targets(self):
        legs = split_take_profits((D("1"), D("2"), D("3")), D("5"), D("1"))
        self.assertEqual([c for _, c in legs], [D("2"), D("2"), D("1")])
        self.assertEqual(sum(c for _, c in legs), D("5"))

    def test_fewer_lots_than_targets_uses_the_nearest_targets(self):
        legs = split_take_profits((D("1"), D("2"), D("3")), D("2"), D("1"))
        self.assertEqual(legs, ((D("1"), D("1")), (D("2"), D("1"))))

    def test_nothing_to_split(self):
        self.assertEqual(split_take_profits((D("1"),), D("0"), D("1")), ())


class Execute(unittest.TestCase):
    def setUp(self):
        self.ex = MockExchange(balance="1000")

    def test_long_limit_order_is_built_and_sent(self):
        r = execute_signal(sig(BTC_LONG), self.ex)
        self.assertTrue(r.ok, r.reason)
        req = r.order.request
        self.assertEqual((req.order_type, req.price, req.leverage), ("limit", D("65000"), 10))
        self.assertEqual(req.contracts, D("38.4"))              # 250 margin x10 / 65000
        self.assertEqual(req.stop_loss, D("64000"))
        self.assertEqual(sum(c for _, c in req.take_profits), req.contracts)
        self.assertEqual(r.order.status, "live")
        self.assertEqual(self.ex.leverage["BTC-USDT"], 10)
        self.assertLessEqual(self.ex.used_margin, D("250"))

    def test_short_zone_uses_worst_case_price(self):
        s = sig("ETH/USDT SHORT\nEntry: 3250 - 3270\nTP1: 3200\nTP2: 3150\nSL: 3320\nLeverage: 10x")
        r = execute_signal(s, self.ex)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.request.price, D("3250"))            # lowest entry for a SHORT

    def test_market_entry_uses_the_live_price(self):
        s = sig("AVAX LONG\nEntry: market\nSL: 33\nTP1: 36\nTP2: 38\nLev 10x")
        r = execute_signal(s, self.ex)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual((r.request.order_type, r.request.price), ("market", None))
        self.assertEqual(r.order.status, "filled")
        self.assertEqual(r.request.contracts, D("71"))          # 250 x10 / 35 = 71.4 -> 71
        self.assertEqual(sum(c for _, c in r.request.take_profits), D("71"))

    def test_signal_leverage_is_capped_by_config(self):
        s = sig("BTCUSDT LONG Entry 65000 SL 64000 TP 67000 Leverage 50x")
        r = execute_signal(s, self.ex)
        self.assertTrue(r.ok, r.reason)
        self.assertEqual(r.request.leverage, 20)

    def test_bad_geometry_never_reaches_the_exchange(self):
        r = execute_signal(sig("BTCUSDT LONG Entry 65000 SL 66000 TP 67000"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("stop-loss", r.reason)
        self.assertFalse(r.order_attempted)
        self.assertEqual(self.ex.orders, [])
        self.assertEqual(self.ex.leverage, {})

    def test_price_scale_typo_is_caught_against_the_live_price(self):
        r = execute_signal(sig("ETHUSDT LONG Entry 325 SL 320 TP 330"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("away from the live price", r.reason)
        self.assertEqual(self.ex.orders, [])

    def test_take_profit_already_behind_the_live_price_refuses_the_whole_trade(self):
        # live BTC 65000; limit entry 63000 is fine, but TP1 64000 is below the market
        r = execute_signal(sig("BTCUSDT LONG Entry 63000 SL 62000 TP1 64000 TP2 66000"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("take-profit 64000 is already", r.reason)
        self.assertEqual((self.ex.orders, self.ex.leverage), ([], {}))

    def test_short_take_profit_behind_the_live_price(self):
        r = execute_signal(sig("BTCUSDT SHORT Entry 67000 SL 68000 TP1 66000 TP2 64000"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("take-profit 66000 is already", r.reason)

    def test_stop_loss_on_the_wrong_side_of_the_live_price(self):
        r = execute_signal(sig("BTCUSDT LONG Entry 63000 SL 65500 TP1 66000"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("stop-loss", r.reason)

    def test_limit_entry_below_market_with_targets_above_market_is_fine(self):
        r = execute_signal(sig("BTCUSDT LONG Entry 63500 SL 62000 TP1 66000 TP2 67000"), self.ex)
        self.assertTrue(r.ok, r.reason)

    def test_stale_signal_is_rejected(self):
        now = datetime(2026, 10, 12, 20, 0, tzinfo=timezone.utc)
        r = execute_signal(sig(BTC_LONG), self.ex, signal_time=now - timedelta(minutes=10), now=now)
        self.assertFalse(r.ok)
        self.assertIn("stale", r.reason)
        self.assertEqual(self.ex.orders, [])

    def test_unknown_symbol_is_rejected_before_any_order(self):
        r = execute_signal(sig("FOOUSDT LONG Entry 10 SL 9 TP 12"), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("unknown instrument", r.reason)
        self.assertFalse(r.order_attempted)

    def test_balance_too_small_is_rejected(self):
        r = execute_signal(sig(BTC_LONG), MockExchange(balance="1"))
        self.assertFalse(r.ok)
        self.assertIn("sizing", r.reason)
        self.assertFalse(r.order_attempted)

    def test_dry_run_builds_the_order_but_sends_nothing(self):
        r = execute_signal(sig(BTC_LONG), self.ex, dry_run=True)
        self.assertTrue(r.ok)
        self.assertIsNotNone(r.request)
        self.assertIsNone(r.order)
        self.assertEqual(self.ex.orders, [])
        self.assertEqual(self.ex.leverage, {})

    def test_exchange_rejects_a_second_copy_of_the_same_signal(self):
        s = sig(BTC_LONG)
        self.assertTrue(execute_signal(s, self.ex).ok)
        again = execute_signal(s, self.ex)
        self.assertFalse(again.ok)
        self.assertIn("duplicate client_order_id", again.reason)
        self.assertTrue(again.order_attempted)                  # keep the duplicate-guard entry
        self.assertEqual(len(self.ex.orders), 1)

    def test_margin_runs_out_after_four_25_percent_trades(self):
        results = [
            execute_signal(sig(f"BTCUSDT LONG Entry 65000 SL 64000 TP {67000 + i} Leverage 10x"), self.ex)
            for i in range(5)
        ]
        self.assertEqual([r.ok for r in results], [True, True, True, True, False])
        self.assertIn("insufficient free margin", results[4].reason)
        self.assertFalse(results[4].order_attempted)          # refused before any order was sent
        self.assertEqual(len(self.ex.orders), 4)
        self.assertLessEqual(self.ex.used_margin, self.ex.balance)

    def test_free_margin_is_checked_before_the_exchange_is_asked(self):
        self.ex.used_margin = D("900")                           # only 100 USDT free, trade needs ~250
        r = execute_signal(sig(BTC_LONG), self.ex)
        self.assertFalse(r.ok)
        self.assertIn("needs 249.60 USDT, only 100.00 USDT is free", r.reason)
        self.assertFalse(r.order_attempted)                       # a re-post may retry later
        self.assertEqual((self.ex.orders, self.ex.leverage), ([], {}))

    def test_cancel_pending_frees_the_margin_of_unfilled_entries_only(self):
        execute_signal(sig(BTC_LONG), self.ex)                                    # limit -> live
        execute_signal(sig("AVAX LONG\nEntry: market\nSL: 33\nTP1: 36\nTP2: 38"), self.ex)  # filled
        used = self.ex.used_margin
        report = self.ex.cancel_pending()
        self.assertEqual((report.orders, report.tpsl), (1, 2))
        self.assertEqual([o.status for o in self.ex.orders], ["filled"])
        self.assertLess(self.ex.used_margin, used)
        self.assertGreater(self.ex.used_margin, 0)                                # the position keeps its margin

    def test_close_all_books_the_profit_and_frees_the_margin(self):
        execute_signal(sig("AVAX LONG\nEntry: market\nSL: 33\nTP1: 36\nTP2: 38"), self.ex)   # filled at 35
        self.ex.set_price("AVAX-USDT", "36")
        (pos,) = self.ex.get_positions()
        self.assertEqual((pos.symbol, pos.size, pos.entry_price, pos.unrealized_pnl),
                         ("AVAX-USDT", D("71"), D("35"), D("71")))
        report = self.ex.close_all_positions()
        self.assertEqual(len(report.closed), 1)
        self.assertEqual(self.ex.balance, D("1071"))
        self.assertEqual((self.ex.used_margin, self.ex.get_positions()), (0, []))

    def test_close_all_leaves_unfilled_entries_to_cancel_pending(self):
        execute_signal(sig(BTC_LONG), self.ex)                                    # limit -> live
        self.assertEqual(self.ex.close_all_positions().closed, ())
        self.assertEqual(len(self.ex.orders), 1)

    def test_short_position_profits_when_price_falls(self):
        execute_signal(sig("AVAX SHORT\nEntry: market\nSL: 37\nTP1: 34\nTP2: 33"), self.ex)
        self.ex.set_price("AVAX-USDT", "34")
        self.assertEqual(self.ex.get_positions()[0].unrealized_pnl, D("71"))

    def test_mock_enforces_the_rules_of_a_real_exchange(self):
        r = execute_signal(sig(BTC_LONG), self.ex, dry_run=True)
        with self.assertRaises(ExchangeError):                   # leverage was never set
            self.ex.place_order(r.request)


if __name__ == "__main__":
    unittest.main()
