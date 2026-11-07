import base64
import hashlib
import hmac
import http.server
import json
import threading
import time
import unittest
import urllib.error
import urllib.parse
from decimal import Decimal as D

from signalbot import parse_signal
from signalbot.blofin import (BASE_URLS, USER_AGENT, BlofinExchange, PooledTransport, sign,
                              urllib_transport)
from signalbot.exchange import ExchangeError, OrderRequest
from signalbot.executor import execute_signal
from signalbot.models import Side

OK = {"code": "0", "msg": "success"}


class FakeBlofin:
    """Stands in for the network: records requests, answers from a route table."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        path = url.split("openapi.blofin.com", 1)[1]
        self.calls.append((method, path, headers, body))
        route, _, qs = path.partition("?")
        reply = self.routes[(method, route)]
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):  # dynamic answer: reply(parsed body, parsed query)
            reply = reply(json.loads(body) if body else None, dict(urllib.parse.parse_qsl(qs)))
        return 200, json.dumps(reply)

    def bodies(self, path):
        return [json.loads(b) for m, p, h, b in self.calls if p == path and b]


INSTRUMENT = {"instId": "BTC-USDT", "contractValue": "0.001", "lotSize": "0.1",
              "minSize": "0.1", "maxLeverage": "100", "state": "live"}


def routes(over=None):
    table = {
        ("GET", "/api/v1/market/instruments"): {**OK, "data": [INSTRUMENT]},
        ("GET", "/api/v1/market/tickers"): {**OK, "data": [{"instId": "BTC-USDT", "last": "65000.5"}]},
        ("GET", "/api/v1/account/balance"): {**OK, "data": {
            "totalEquity": "5000",
            "details": [{"currency": "USDT", "equity": "5000", "available": "3742.7286"}]}},
        ("GET", "/api/v1/account/position-mode"): {**OK, "data": {
            "positionMode": "net_mode", "multiPosition": "false"}},
        ("POST", "/api/v1/account/set-leverage"): {**OK, "data": {"leverage": "10"}},
        ("POST", "/api/v1/trade/order"): {"code": "0", "msg": "", "data": [
            {"orderId": "1000019027199", "clientOrderId": "", "msg": "Order placed", "code": "0"}]},
        ("POST", "/api/v1/trade/order-tpsl"): {**OK, "data": {"tpslId": "1012", "code": "0", "msg": None}},
    }
    table.update(over or {})
    return table


def make(table=None, env="demo"):
    fake = FakeBlofin(table or routes())
    ex = BlofinExchange("KEY", "SECRET", "PASS", env, transport=fake,
                        clock=lambda: 1700000000.0, nonce=lambda: "nonce-1")
    return ex, fake


def request(**kw):
    base = dict(symbol="BTC-USDT", side=Side.LONG, order_type="limit", contracts=D("38.4"),
                price=D("65000"), stop_loss=D("64000"),
                take_profits=((D("66000"), D("19.2")), (D("67000"), D("19.2"))),
                leverage=10, client_order_id="abc123def4567890")
    base.update(kw)
    return OrderRequest(**base)


class Signing(unittest.TestCase):
    def test_signature_is_base64_of_the_hex_hmac(self):
        got = sign("secret", "/api/v1/trade/order", "post", "1700000000000", "n", '{"a":"1"}')
        prehash = '/api/v1/trade/orderPOST1700000000000n{"a":"1"}'
        hexed = hmac.new(b"secret", prehash.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(got, base64.b64encode(hexed.encode()).decode())

    def test_query_string_is_part_of_the_signed_path(self):
        ex, fake = make()
        ex.get_balance()
        method, path, headers, _ = fake.calls[0]
        self.assertEqual(path, "/api/v1/account/balance?productType=USDT-FUTURES")
        expected = sign("SECRET", path, "GET", "1700000000000", "nonce-1", "")
        self.assertEqual(headers["ACCESS-SIGN"], expected)
        self.assertEqual(headers["ACCESS-KEY"], "KEY")
        self.assertEqual(headers["ACCESS-PASSPHRASE"], "PASS")

    def test_the_signed_body_is_exactly_the_sent_body(self):
        ex, fake = make()
        ex.set_leverage("BTC-USDT", 10)
        method, path, headers, body = fake.calls[-1]
        self.assertEqual(headers["ACCESS-SIGN"],
                         sign("SECRET", path, "POST", "1700000000000", "nonce-1", body.decode()))

    def test_market_data_is_not_signed(self):
        ex, fake = make()
        ex.get_last_price("BTC-USDT")
        self.assertNotIn("ACCESS-SIGN", fake.calls[0][2])


class DefaultTransport(unittest.TestCase):
    def test_sends_its_own_user_agent_and_reads_the_reply(self):
        seen = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen["ua"] = self.headers.get("User-Agent")
                body = b'{"code":"0"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        status, text = urllib_transport("GET", f"http://127.0.0.1:{server.server_port}/x", {}, None)
        server.server_close()
        self.assertEqual((status, text), (200, '{"code":"0"}'))
        self.assertEqual(seen["ua"], USER_AGENT)
        self.assertNotIn("Python-urllib", seen["ua"])


class Pooled(unittest.TestCase):
    """PooledTransport against a real local HTTP/1.1 server (keep-alive)."""

    def start(self, close_after_reply=False, send_connection_close=False):
        stats = {"connections": 0, "requests": 0, "ua": []}

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def setup(self):
                super().setup()
                stats["connections"] += 1

            def _reply(self):
                stats["requests"] += 1
                stats["ua"].append(self.headers.get("User-Agent"))
                length = int(self.headers.get("Content-Length") or 0)
                echo = self.rfile.read(length) if length else b""
                body = b'{"code":"0","echo":"' + echo + b'"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                if send_connection_close:
                    self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                if close_after_reply:
                    self.close_connection = True      # closes WITHOUT telling the client

            do_GET = do_POST = _reply

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", stats

    def test_requests_one_after_another_share_one_connection(self):
        url, stats = self.start()
        transport = PooledTransport()
        self.addCleanup(transport.close)
        for _ in range(3):
            status, text = transport("GET", url + "/x?a=1", {}, None)
            self.assertEqual((status, json.loads(text)["code"]), (200, "0"))
        self.assertEqual((stats["requests"], stats["connections"], transport.opened), (3, 1, 1))
        self.assertEqual(set(stats["ua"]), {USER_AGENT})

    def test_post_body_arrives_intact_on_a_reused_connection(self):
        url, stats = self.start()
        transport = PooledTransport()
        self.addCleanup(transport.close)
        transport("GET", url + "/a", {}, None)
        status, text = transport("POST", url + "/b", {"Content-Type": "application/json"}, b"hello")
        self.assertEqual(json.loads(text)["echo"], "hello")
        self.assertEqual(stats["connections"], 1)

    def test_a_connection_idle_for_too_long_is_not_reused(self):
        url, stats = self.start()
        transport = PooledTransport(max_idle=0.0)
        self.addCleanup(transport.close)
        transport("GET", url + "/x", {}, None)
        time.sleep(0.01)
        transport("GET", url + "/x", {}, None)
        self.assertEqual(stats["connections"], 2)

    def test_connection_close_from_the_server_is_respected(self):
        url, stats = self.start(send_connection_close=True)
        transport = PooledTransport()
        self.addCleanup(transport.close)
        transport("GET", url + "/x", {}, None)
        transport("GET", url + "/x", {}, None)
        self.assertEqual(stats["connections"], 2)

    def test_a_connection_the_server_closed_quietly_is_detected_before_reuse(self):
        url, stats = self.start(close_after_reply=True)
        transport = PooledTransport()
        self.addCleanup(transport.close)
        transport("POST", url + "/x", {}, b"1")
        time.sleep(0.2)                                   # the FIN arrives while the connection idles
        status, _ = transport("POST", url + "/x", {}, b"2")   # must not be sent into the dead socket
        self.assertEqual((status, stats["requests"], stats["connections"]), (200, 2, 2))

    def test_parallel_requests_get_their_own_connections(self):
        url, stats = self.start()
        transport = PooledTransport()
        self.addCleanup(transport.close)
        threads = [threading.Thread(target=transport, args=("GET", url + "/x", {}, None)) for _ in range(4)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(stats["requests"], 4)
        self.assertGreaterEqual(stats["connections"], 1)

    def test_an_unreachable_server_is_an_oserror_the_client_understands(self):
        transport = PooledTransport(timeout=1)
        with self.assertRaises(OSError):
            transport("GET", "http://127.0.0.1:9/x", {}, None)

    def test_exchange_keepalive_pings_without_polluting_the_latency_log(self):
        ex, fake = make()
        ex.keepalive(3)
        self.assertEqual(len([c for c in fake.calls if c[1].startswith("/api/v1/market/tickers")]), 3)
        self.assertEqual(ex.drain_timings(), [])


class Environment(unittest.TestCase):
    def test_demo_and_prod_urls(self):
        self.assertIn("demo-trading", BASE_URLS["demo"])
        self.assertNotIn("demo", BASE_URLS["prod"])

    def test_unknown_env_is_refused(self):
        with self.assertRaises(ValueError):
            BlofinExchange("k", "s", "p", "production")

    def test_name_makes_the_mode_obvious(self):
        self.assertIn("DEMO", make()[0].name)
        self.assertIn("REAL", make(env="prod")[0].name)


class MarketAndAccount(unittest.TestCase):
    def test_instrument_is_parsed_and_cached(self):
        ex, fake = make()
        inst = ex.get_instrument("BTC-USDT")
        self.assertEqual((inst.contract_value, inst.lot_size, inst.min_size, inst.max_leverage),
                         (D("0.001"), D("0.1"), D("0.1"), 100))
        ex.get_instrument("BTC-USDT")
        self.assertEqual(len(fake.calls), 1)

    def test_unknown_instrument(self):
        ex, _ = make(routes({("GET", "/api/v1/market/instruments"): {**OK, "data": []}}))
        with self.assertRaisesRegex(ExchangeError, "unknown instrument"):
            ex.get_instrument("FOO-USDT")

    def test_suspended_instrument_is_refused(self):
        row = {**INSTRUMENT, "state": "suspend"}
        ex, _ = make(routes({("GET", "/api/v1/market/instruments"): {**OK, "data": [row]}}))
        with self.assertRaisesRegex(ExchangeError, "not tradable"):
            ex.get_instrument("BTC-USDT")

    def test_missing_field_gives_a_readable_error(self):
        row = {k: v for k, v in INSTRUMENT.items() if k != "lotSize"}
        ex, _ = make(routes({("GET", "/api/v1/market/instruments"): {**OK, "data": [row]}}))
        with self.assertRaisesRegex(ExchangeError, "lotSize"):
            ex.get_instrument("BTC-USDT")

    def test_price_and_balance(self):
        ex, _ = make()
        self.assertEqual(ex.get_last_price("BTC-USDT"), D("65000.5"))
        self.assertEqual(ex.get_balance(), D("5000"))

    def test_free_margin(self):
        self.assertEqual(make()[0].get_available(), D("3742.7286"))

    def test_free_margin_falls_back_to_available_equity(self):
        r = routes({("GET", "/api/v1/account/balance"): {**OK, "data": {
            "details": [{"currency": "USDT", "equity": "5000", "availableEquity": "4100"}]}}})
        self.assertEqual(make(r)[0].get_available(), D("4100"))

    def test_missing_free_margin_field_means_no_trade(self):
        r = routes({("GET", "/api/v1/account/balance"): {**OK, "data": {
            "details": [{"currency": "USDT", "equity": "5000"}]}}})
        with self.assertRaisesRegex(ExchangeError, "free-margin"):
            make(r)[0].get_available()

    def test_zero_price_is_refused(self):
        ex, _ = make(routes({("GET", "/api/v1/market/tickers"): {
            **OK, "data": [{"instId": "BTC-USDT", "last": "0"}]}}))
        with self.assertRaises(ExchangeError):
            ex.get_last_price("BTC-USDT")


class AccountCache(unittest.TestCase):
    def balance_calls(self, fake):
        return [c for c in fake.calls if c[1].startswith("/api/v1/account/balance")]

    def test_balance_and_free_margin_cost_one_request(self):
        ex, fake = make()
        self.assertEqual((ex.get_balance(), ex.get_available()), (D("5000"), D("3742.7286")))
        self.assertEqual(len(self.balance_calls(fake)), 1)

    def test_an_order_makes_the_next_read_fresh(self):
        ex, fake = make()
        ex.get_balance()
        ex.place_order(request())
        ex.get_available()
        self.assertEqual(len(self.balance_calls(fake)), 2)

    def test_cancel_and_close_also_throw_the_cache_away(self):
        table = routes({("GET", "/api/v1/trade/orders-pending"): {**OK, "data": []},
                        ("GET", "/api/v1/trade/orders-tpsl-pending"): {**OK, "data": []},
                        ("GET", "/api/v1/account/positions"): {**OK, "data": []}})
        ex, fake = make(table)
        ex.get_balance(); ex.cancel_pending(); ex.get_balance()
        ex.close_all_positions(); ex.get_balance()
        self.assertEqual(len(self.balance_calls(fake)), 3)

    def test_an_old_answer_is_not_reused(self):
        ex, fake = make()
        ex.get_balance()
        ex._account_cache = (ex._account_cache[0] - 10, ex._account_cache[1])   # 10 s old
        ex.get_balance()
        self.assertEqual(len(self.balance_calls(fake)), 2)

    def test_every_request_is_timed_for_the_latency_log(self):
        ex, fake = make()
        ex.get_last_price("BTC-USDT"); ex.get_balance()
        timings = ex.drain_timings()
        self.assertEqual([name for name, _ in timings],
                         ["GET market/tickers", "GET account/balance"])
        self.assertTrue(all(ms >= 0 for _, ms in timings))
        self.assertEqual(ex.drain_timings(), [])


class Errors(unittest.TestCase):
    def test_top_level_error(self):
        ex, _ = make(routes({("GET", "/api/v1/account/balance"): {"code": "152403", "msg": "bad key"}}))
        with self.assertRaisesRegex(ExchangeError, "152403"):
            ex.get_balance()

    def test_error_hidden_inside_data_is_not_a_success(self):
        bad = {"code": "0", "msg": "", "data": [
            {"orderId": "555", "clientOrderId": "x", "msg": "Position does not exist", "code": "1120x"}]}
        ex, _ = make(routes({("POST", "/api/v1/trade/order"): bad}))
        with self.assertRaisesRegex(ExchangeError, "Position does not exist"):
            ex.place_order(request())

    def test_post_is_never_retried_after_a_network_error(self):
        ex, fake = make(routes({("POST", "/api/v1/trade/order"): urllib.error.URLError("timeout")}))
        with self.assertRaisesRegex(ExchangeError, "network error"):
            ex.place_order(request())
        self.assertEqual(len([c for c in fake.calls if c[1] == "/api/v1/trade/order"]), 1)

    def test_get_is_retried_once(self):
        ex, fake = make(routes({("GET", "/api/v1/account/balance"): urllib.error.URLError("x")}))
        with self.assertRaises(ExchangeError):
            ex.get_balance()
        self.assertEqual(len(fake.calls), 2)

    def test_garbage_response(self):
        ex = BlofinExchange("k", "s", "p", transport=lambda *a: (502, "<html>bad gateway</html>"))
        with self.assertRaisesRegex(ExchangeError, "not JSON"):
            ex.get_balance()


class PositionMode(unittest.TestCase):
    def test_multi_position_is_refused(self):
        r = routes({("GET", "/api/v1/account/position-mode"): {**OK, "data": {
            "positionMode": "long_short_mode", "multiPosition": "true"}}})
        with self.assertRaisesRegex(ExchangeError, "Multi-Position"):
            make(r)[0].set_leverage("BTC-USDT", 10)

    def test_hedge_mode_uses_long_and_short_sides(self):
        r = routes({("GET", "/api/v1/account/position-mode"): {**OK, "data": {
            "positionMode": "long_short_mode", "multiPosition": "false"}}})
        ex, fake = make(r)
        ex.place_order(request(side=Side.SHORT, take_profits=((D("63000"), D("38.4")),),
                               stop_loss=D("66000")))
        body = fake.bodies("/api/v1/trade/order")[0]
        self.assertEqual((body["positionSide"], body["side"]), ("short", "sell"))
        ex.set_leverage("BTC-USDT", 10, Side.SHORT)
        self.assertEqual(fake.bodies("/api/v1/account/set-leverage")[0]["positionSide"], "short")

    def test_unknown_mode_is_refused(self):
        r = routes({("GET", "/api/v1/account/position-mode"): {**OK, "data": {"positionMode": "??"}}})
        with self.assertRaises(ExchangeError):
            make(r)[0].set_leverage("BTC-USDT", 10)


class PlaceOrder(unittest.TestCase):
    def test_set_leverage_body(self):
        ex, fake = make()
        ex.set_leverage("BTC-USDT", 10)
        self.assertEqual(fake.bodies("/api/v1/account/set-leverage")[0],
                         {"instId": "BTC-USDT", "leverage": "10", "marginMode": "isolated"})

    def test_limit_long_with_two_tps(self):
        ex, fake = make()
        result = ex.place_order(request())
        entry = fake.bodies("/api/v1/trade/order")[0]
        self.assertEqual(entry["orderType"], "limit")
        self.assertEqual(entry["price"], "65000")
        self.assertEqual(entry["size"], "38.4")
        self.assertEqual((entry["side"], entry["positionSide"], entry["marginMode"]),
                         ("buy", "net", "isolated"))
        self.assertEqual((entry["slTriggerPrice"], entry["slOrderPrice"]), ("64000", "-1"))
        self.assertNotIn("tpTriggerPrice", entry)               # TPs go as separate orders
        self.assertEqual(entry["clientOrderId"], "abc123def4567890")
        legs = sorted(fake.bodies("/api/v1/trade/order-tpsl"), key=lambda l: l["clientOrderId"])
        self.assertEqual([(l["tpTriggerPrice"], l["size"], l["side"], l["reduceOnly"]) for l in legs],
                         [("66000", "19.2", "sell", "true"), ("67000", "19.2", "sell", "true")])
        self.assertEqual(result.order_id, "1000019027199")
        self.assertEqual((result.status, result.notes), ("live", ()))

    def test_single_tp_rides_on_the_entry(self):
        ex, fake = make()
        ex.place_order(request(take_profits=((D("67000"), D("38.4")),)))
        entry = fake.bodies("/api/v1/trade/order")[0]
        self.assertEqual((entry["tpTriggerPrice"], entry["tpOrderPrice"]), ("67000", "-1"))
        self.assertEqual(fake.bodies("/api/v1/trade/order-tpsl"), [])

    def test_market_short_has_no_price(self):
        ex, fake = make()
        result = ex.place_order(request(side=Side.SHORT, order_type="market", price=None,
                                        stop_loss=D("66000"),
                                        take_profits=((D("63000"), D("19.2")), (D("62000"), D("19.2")))))
        entry = fake.bodies("/api/v1/trade/order")[0]
        self.assertNotIn("price", entry)
        self.assertEqual((entry["side"], entry["orderType"]), ("sell", "market"))
        self.assertEqual({l["side"] for l in fake.bodies("/api/v1/trade/order-tpsl")}, {"buy"})
        self.assertEqual(result.status, "filled")

    def test_failed_tp_leg_is_reported_not_hidden(self):
        r = routes({("POST", "/api/v1/trade/order-tpsl"): {"code": "152001", "msg": "bad tp"}})
        result = make(r)[0].place_order(request())
        self.assertEqual(len(result.notes), 2)
        self.assertIn("NOT placed", result.notes[0])

    def test_decimals_are_never_sent_in_scientific_notation(self):
        ex, fake = make()
        ex.place_order(request(price=D("0.00001230"), stop_loss=D("0.0000100"),
                               contracts=D("100000000"),
                               take_profits=((D("0.0000150"), D("100000000")),)))
        entry = fake.bodies("/api/v1/trade/order")[0]
        self.assertEqual((entry["price"], entry["size"], entry["slTriggerPrice"]),
                         ("0.0000123", "100000000", "0.00001"))


P1, P2, P3 = "aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"   # fingerprints of bot signals


def pending_order(oid, cid, state="live", reduce_only="false", inst="BTC-USDT"):
    return {"orderId": oid, "clientOrderId": cid, "instId": inst, "state": state,
            "reduceOnly": reduce_only}


def pending_leg(tid, cid, inst="BTC-USDT"):
    return {"tpslId": tid, "clientOrderId": cid, "instId": inst}


def cancel_routes(orders, legs, fail_orders=()):
    """A tiny fake exchange: orders/legs lists, cancels remove them."""
    cancelled = {"orders": [], "legs": []}

    def list_orders(body, query):
        return {**OK, "data": [o for o in orders if o["orderId"] not in cancelled["orders"]]}

    def list_legs(body, query):
        return {**OK, "data": [t for t in legs if t["tpslId"] not in cancelled["legs"]]}

    def cancel_orders(body, query):
        out = []
        for item in body:
            if item["orderId"] in fail_orders:
                out.append({"orderId": item["orderId"], "clientOrderId": None, "code": "1000",
                            "msg": "Cancel failed as the order has been filled"})
            else:
                cancelled["orders"].append(item["orderId"])
                out.append({"orderId": item["orderId"], "clientOrderId": None, "code": "0", "msg": None})
        return {**OK, "data": out}

    def cancel_legs(body, query):
        cancelled["legs"] += [i["tpslId"] for i in body]
        return {**OK, "data": [{"tpslId": i["tpslId"], "code": "0", "msg": None} for i in body]}

    table = routes({
        ("GET", "/api/v1/trade/orders-pending"): list_orders,
        ("GET", "/api/v1/trade/orders-tpsl-pending"): list_legs,
        ("POST", "/api/v1/trade/cancel-batch-orders"): cancel_orders,
        ("POST", "/api/v1/trade/cancel-tpsl"): cancel_legs,
    })
    return table, cancelled


class CancelPending(unittest.TestCase):
    def test_cancels_only_our_unfilled_entries_and_exactly_their_legs(self):
        orders = [
            pending_order("1", P1),                              # ours, unfilled -> cancel
            pending_order("2", P2),                              # ours, unfilled -> cancel
            pending_order("3", "manual-order-xyz"),              # someone else's -> keep
            pending_order("4", P3, state="partially_filled"),    # partly filled -> keep
            pending_order("5", P3 + "t1", reduce_only="true"),   # a reduce-only order -> keep
        ]
        legs = [
            pending_leg("L1", P1 + "t1"), pending_leg("L2", P1 + "t2"),   # legs of cancelled P1
            pending_leg("L3", P2 + "t1"),                                  # leg of cancelled P2
            pending_leg("L4", P3 + "t1"),                                  # leg of a LIVE position
            pending_leg("L5", ""),                                         # not ours
        ]
        table, cancelled = cancel_routes(orders, legs)
        report = make(table)[0].cancel_pending()
        self.assertEqual((report.orders, report.tpsl, report.failed), (2, 3, ()))
        self.assertEqual(sorted(cancelled["orders"]), ["1", "2"])
        self.assertEqual(sorted(cancelled["legs"]), ["L1", "L2", "L3"])

    def test_an_entry_that_filled_meanwhile_keeps_its_legs(self):
        orders = [pending_order("1", P1)]
        legs = [pending_leg("L1", P1 + "t1")]
        table, cancelled = cancel_routes(orders, legs, fail_orders={"1"})
        report = make(table)[0].cancel_pending()
        self.assertEqual((report.orders, report.tpsl), (0, 0))
        self.assertEqual(cancelled["legs"], [])                  # the position needs its take-profit
        self.assertIn("filled", report.failed[0])

    def test_nothing_pending(self):
        table, _ = cancel_routes([], [])
        self.assertEqual(make(table)[0].cancel_pending().orders, 0)

    def test_pagination_follows_the_after_cursor(self):
        rows = [pending_order(str(i), "%016x" % i) for i in range(1, 104)]   # 103 rows, page = 100
        seen_queries = []

        def list_orders(body, query):
            seen_queries.append(query)
            start = int(query["after"]) if "after" in query else 0
            return {**OK, "data": rows[start:start + 100]}

        table, cancelled = cancel_routes([], [])
        table[("GET", "/api/v1/trade/orders-pending")] = list_orders
        report = make(table)[0].cancel_pending()
        self.assertEqual(len(seen_queries), 2)
        self.assertEqual(seen_queries[1]["after"], "100")
        self.assertEqual(report.orders, 103)

    def test_entries_are_cancelled_in_small_batches(self):
        rows = [pending_order(str(i), "%016x" % i) for i in range(1, 26)]
        table, _ = cancel_routes(rows, [])
        ex, fake = make(table)
        ex.cancel_pending()
        sizes = [len(b) for b in fake.bodies("/api/v1/trade/cancel-batch-orders")]
        self.assertEqual(sizes, [10, 10, 5])


class Positions(unittest.TestCase):
    ROW_NET_LONG = {"instId": "BTC-USDT", "positionSide": "net", "positions": "12.5",
                    "marginMode": "isolated", "averagePrice": "65000", "unrealizedPnl": "-3.5"}
    ROW_NET_SHORT = {"instId": "ETH-USDT", "positionSide": "net", "positions": "-290",
                     "marginMode": "cross", "averagePrice": "1890.5", "unrealizedPnl": "2.9"}

    def flatten_routes(self, rows_after, tpsl=()):
        """Positions answer: first call = open rows, later calls = rows_after (what is left)."""
        state = {"n": 0, "closed": [], "legs": []}
        first = [self.ROW_NET_LONG, self.ROW_NET_SHORT]

        def positions(body, query):
            state["n"] += 1
            return {**OK, "data": first if state["n"] == 1 else rows_after}

        def close(body, query):
            state["closed"].append(body)
            return {**OK, "data": {"instId": body["instId"], "positionSide": body["positionSide"]}}

        def cancel_legs(body, query):
            state["legs"] += [b["tpslId"] for b in body]
            return {**OK, "data": [{"tpslId": b["tpslId"], "code": "0", "msg": None} for b in body]}

        table = routes({
            ("GET", "/api/v1/account/positions"): positions,
            ("POST", "/api/v1/trade/close-position"): close,
            ("GET", "/api/v1/trade/orders-tpsl-pending"): {**OK, "data": list(tpsl)},
            ("POST", "/api/v1/trade/cancel-tpsl"): cancel_legs,
        })
        return table, state

    def test_positions_are_parsed_net_mode_sign_gives_the_side(self):
        table, _ = self.flatten_routes([])
        rows = make(table)[0].get_positions()
        self.assertEqual([(p.symbol, p.side, p.size) for p in rows],
                         [("BTC-USDT", Side.LONG, D("12.5")), ("ETH-USDT", Side.SHORT, D("290"))])
        self.assertEqual(rows[0].unrealized_pnl, D("-3.5"))
        self.assertEqual(rows[1].margin_mode, "cross")

    def test_empty_rows_are_skipped_and_hedge_mode_rows_use_their_side(self):
        rows = [{"instId": "BTC-USDT", "positionSide": "short", "positions": "5",
                 "averagePrice": "1", "unrealizedPnl": ""},
                {"instId": "ETH-USDT", "positionSide": "long", "positions": "0"}]
        table = routes({("GET", "/api/v1/account/positions"): {**OK, "data": rows}})
        got = make(table)[0].get_positions()
        self.assertEqual([(p.symbol, p.side, p.unrealized_pnl) for p in got],
                         [("BTC-USDT", Side.SHORT, D("0"))])

    def test_close_all_closes_every_position_and_removes_their_leftover_tpsl(self):
        legs = [{"tpslId": "T1", "instId": "BTC-USDT"}, {"tpslId": "T2", "instId": "ETH-USDT"},
                {"tpslId": "T3", "instId": "SOL-USDT"}]          # SOL has no position here
        table, state = self.flatten_routes([], legs)
        ex, _ = make(table)
        ex._sleep = lambda s: None
        report = ex.close_all_positions()
        self.assertEqual([p.symbol for p in report.closed], ["BTC-USDT", "ETH-USDT"])
        self.assertEqual(report.failed, ())
        self.assertEqual(state["closed"], [
            {"instId": "BTC-USDT", "marginMode": "isolated", "positionSide": "net"},
            {"instId": "ETH-USDT", "marginMode": "cross", "positionSide": "net"}])
        self.assertEqual((report.tpsl, sorted(state["legs"])), (2, ["T1", "T2"]))   # T3 untouched

    def test_a_position_that_stays_open_is_reported_and_keeps_its_protection(self):
        legs = [{"tpslId": "T1", "instId": "BTC-USDT"}, {"tpslId": "T2", "instId": "ETH-USDT"}]
        table, state = self.flatten_routes([self.ROW_NET_LONG], legs)   # BTC refuses to close
        ex, _ = make(table)
        ex._sleep = lambda s: None
        report = ex.close_all_positions()
        self.assertEqual([p.symbol for p in report.closed], ["ETH-USDT"])
        self.assertTrue(any("BTC-USDT" in f and "still open" in f for f in report.failed))
        self.assertEqual(state["legs"], ["T2"])                   # BTC keeps its TP/SL

    def test_a_rejected_close_request_is_reported(self):
        table, state = self.flatten_routes([self.ROW_NET_LONG])
        table[("POST", "/api/v1/trade/close-position")] = lambda body, q: (
            {"code": "1", "msg": "nope"} if body["instId"] == "BTC-USDT"
            else {**OK, "data": {"instId": body["instId"]}})
        ex, _ = make(table)
        ex._sleep = lambda s: None
        report = ex.close_all_positions()
        self.assertEqual(len([f for f in report.failed if f.startswith("BTC-USDT")]), 1)
        self.assertEqual([p.symbol for p in report.closed], ["ETH-USDT"])

    def test_no_positions_means_nothing_is_sent(self):
        table = routes({("GET", "/api/v1/account/positions"): {**OK, "data": []},
                        ("GET", "/api/v1/trade/orders-tpsl-pending"): {**OK, "data": []}})
        ex, fake = make(table)
        report = ex.close_all_positions()
        self.assertEqual((report.closed, report.failed, report.tpsl), ((), (), 0))
        self.assertEqual(fake.bodies("/api/v1/trade/close-position"), [])


class FullPipeline(unittest.TestCase):
    def test_signal_to_http_requests(self):
        signal = parse_signal("BTCUSDT LONG\nEntry: 65000\nSL: 64000\nTP1: 66000\nTP2: 67000\n"
                              "Leverage: 10x").signal
        ex, fake = make()
        result = execute_signal(signal, ex)
        self.assertTrue(result.ok, result.reason)
        paths = [c[1].split("?")[0] for c in fake.calls]
        self.assertLess(paths.index("/api/v1/account/set-leverage"), paths.index("/api/v1/trade/order"))
        entry = fake.bodies("/api/v1/trade/order")[0]
        self.assertEqual(entry["clientOrderId"], signal.fingerprint)
        self.assertLessEqual(len(entry["clientOrderId"]), 32)
        self.assertTrue(entry["clientOrderId"].isalnum())
        self.assertEqual(entry["size"], "192.3")  # 5000 x 25% x 10x / (65000 x 0.001)
        self.assertEqual(sum(D(l["size"]) for l in fake.bodies("/api/v1/trade/order-tpsl")), D(entry["size"]))


if __name__ == "__main__":
    unittest.main()
