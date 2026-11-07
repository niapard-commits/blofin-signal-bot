import os
import tempfile
import unittest

from signalbot import parse_signal
from signalbot.store import SignalStore


class Store(unittest.TestCase):
    def setUp(self):
        self.store = SignalStore(":memory:")

    def claim(self, fp="abc", now=1000.0, ttl=3600):
        return self.store.claim(fp, "BTC-USDT", "LONG", ttl_seconds=ttl, now=now)

    def test_second_identical_signal_is_a_duplicate(self):
        self.assertTrue(self.claim())
        self.assertFalse(self.claim(now=1001.0))

    def test_different_signals_do_not_collide(self):
        self.assertTrue(self.claim("a"))
        self.assertTrue(self.claim("b"))

    def test_same_signal_is_allowed_again_after_the_window(self):
        self.assertTrue(self.claim(now=1000.0, ttl=60))
        self.assertFalse(self.claim(now=1030.0, ttl=60))
        self.assertTrue(self.claim(now=1100.0, ttl=60))

    def test_reposted_with_other_wording_is_still_a_duplicate(self):
        a = parse_signal("BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP: 67000").signal
        b = parse_signal("🚀 #BTC/USDT long 🚀 entry 65000 / stop 64000 / target 67000").signal
        self.assertTrue(self.store.claim(a.fingerprint, a.symbol, a.side.value, ttl_seconds=3600))
        self.assertFalse(self.store.claim(b.fingerprint, b.symbol, b.side.value, ttl_seconds=3600))

    def test_released_signal_can_be_claimed_again(self):
        self.assertTrue(self.claim("r"))
        self.store.release("r")
        self.assertTrue(self.claim("r", now=1001.0))

    def test_journal_records_events(self):
        self.store.log("signal", "BTC-USDT LONG", "raw")
        self.store.log("rejected", "stop-loss value not found", "raw2")
        self.assertEqual(self.store.journal_count(), 2)

    def test_state_survives_a_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "signals.db")
        first = SignalStore(path)
        self.assertTrue(first.claim("x", "ETH-USDT", "SHORT", ttl_seconds=3600))
        first.close()
        second = SignalStore(path)  # "restart"
        self.assertFalse(second.claim("x", "ETH-USDT", "SHORT", ttl_seconds=3600))
        second.close()


if __name__ == "__main__":
    unittest.main()
