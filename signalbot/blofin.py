"""Real BloFin futures client (REST).

Implements the same ``Exchange`` interface as ``MockExchange``.

Endpoints used (BloFin API docs, USDT-M futures):
    GET  /api/v1/market/instruments      contract specs        (public)
    GET  /api/v1/market/tickers          last price            (public)
    GET  /api/v1/account/balance         equity                (signed)
    GET  /api/v1/account/position-mode   net / hedge           (signed)
    POST /api/v1/account/set-leverage                          (signed)
    POST /api/v1/trade/order             entry (+ attached SL) (signed)
    POST /api/v1/trade/order-tpsl        one take-profit leg   (signed)
    GET  /api/v1/account/positions       open positions        (signed)
    POST /api/v1/trade/close-position    close one at market   (signed)

Safety rules built in:
* ``env`` must be "demo" or "prod"; demo is the default and prod needs an
  explicit ``BLOFIN_ENV=prod``.
* Only hedge-free setups are traded: "net" mode, or long/short mode WITHOUT
  multi-position. Anything else -> refuse.
* A POST is NEVER retried automatically (a retry could open the position twice).
  GET requests are retried once on a network error.
* A response is only a success when the top-level ``code`` AND every item's
  ``code`` is "0" (the docs show business errors hidden inside ``data``).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import json
import re
import select
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal, InvalidOperation
from typing import Callable

from .exchange import (CancelReport, ExchangeError, FlattenReport, OrderRequest, OrderResult,
                       PositionInfo)
from .models import Side
from .sizing import Instrument

BASE_URLS = {
    "demo": "https://demo-trading-openapi.blofin.com",
    "prod": "https://openapi.blofin.com",
}

# BloFin's web firewall answers HTTP 403 (an HTML page) to Python's default
# "Python-urllib/x.y" User-Agent, so the client identifies itself by name.
USER_AGENT = "blofin-signal-bot/1.0"

# transport(method, url, headers, body) -> (http_status, response_text)
Transport = Callable[[str, str, dict, bytes | None], tuple[int, str]]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:  # 4xx/5xx still carry a JSON body worth reading
        return exc.code, exc.read().decode(errors="replace")


def _dropped(conn: http.client.HTTPConnection) -> bool:
    """True if an IDLE connection is no longer clean: the server closed it (a FIN is waiting
    to be read) or sent something unexpected. Checked before every reuse."""
    sock = conn.sock
    if sock is None:
        return True
    try:
        readable, _, _ = select.select([sock], [], [], 0)
    except (OSError, ValueError):
        return True
    return bool(readable)


class PooledTransport:
    """Same signature as ``urllib_transport`` but keeps HTTPS connections open between
    requests. A new connection costs several network round trips (TCP + TLS); reusing one
    costs a single round trip, which is most of the latency of a trade.

    Safety rules:
    * a connection idle longer than ``max_idle`` seconds, or one the server has closed, is
      never reused (a POST must not be sent into a dead connection);
    * any error closes the connection, nothing broken is ever returned to the pool;
    * connections are used by one request at a time, so parallel requests get their own.
    """

    def __init__(self, timeout: float = 10.0, max_idle: float = 10.0, max_pool: int = 8) -> None:
        self._timeout, self._max_idle, self._max_pool = timeout, max_idle, max_pool
        self._idle: dict[tuple[str, str], list[tuple[http.client.HTTPConnection, float]]] = {}
        self._lock = threading.Lock()
        self.opened = 0          # connections opened so far (statistics, tests)

    def _take(self, key):
        while True:
            with self._lock:
                entries = self._idle.get(key) or []
                if not entries:
                    return None
                conn, last_used = entries.pop()
            if time.monotonic() - last_used <= self._max_idle and not _dropped(conn):
                return conn
            conn.close()

    def _give_back(self, key, conn) -> None:
        with self._lock:
            entries = self._idle.setdefault(key, [])
            if len(entries) < self._max_pool:
                entries.append((conn, time.monotonic()))
                return
        conn.close()

    def close(self) -> None:
        with self._lock:
            old, self._idle = [c for v in self._idle.values() for c, _ in v], {}
        for conn in old:
            conn.close()

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
        parts = urllib.parse.urlsplit(url)
        key = (parts.scheme, parts.netloc)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
        conn = self._take(key)
        if conn is None:
            factory = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = factory(parts.netloc, timeout=self._timeout)
            with self._lock:
                self.opened += 1
        try:
            conn.request(method, target, body=body, headers=headers)
            response = conn.getresponse()
            text = response.read().decode(errors="replace")
        except (http.client.HTTPException, OSError) as exc:
            conn.close()
            raise OSError(f"{type(exc).__name__}: {exc}") from None
        if response.will_close:
            conn.close()
        else:
            self._give_back(key, conn)
        return response.status, text


def sign(secret: str, path: str, method: str, timestamp: str, nonce: str, body: str) -> str:
    """BloFin signature: base64( hex( HMAC-SHA256(secret, prehash) ) )."""
    prehash = f"{path}{method.upper()}{timestamp}{nonce}{body}"
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).hexdigest()
    return base64.b64encode(digest.encode()).decode()


# Orders this bot created carry the signal fingerprint (16 hex chars) as clientOrderId;
# its take-profit legs add "t1", "t2", ... Nothing else is ever cancelled by the bot.
_OUR_ENTRY = re.compile(r"^[0-9a-f]{16}$")
_OUR_LEG = re.compile(r"^([0-9a-f]{16})t\d+$")
_BATCH = 10      # cancel requests per call
_PAGE = 100      # rows per list request (the maximum)
_MAX_PAGES = 10
_ACCOUNT_TTL = 2.0   # seconds: balance and free margin come from ONE request and are shared


def _dec(value, what: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ExchangeError(f"unexpected {what} in the exchange response: {value!r}") from None


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


class BlofinExchange:
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        env: str = "demo",
        *,
        transport: Transport = urllib_transport,
        clock: Callable[[], float] = time.time,
        nonce: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        if env not in BASE_URLS:
            raise ValueError(f'BLOFIN_ENV must be "demo" or "prod", got {env!r}')
        self.env = env
        self.name = f"BloFin {'DEMO' if env == 'demo' else 'REAL'}"
        self._base = BASE_URLS[env]
        self._key, self._secret, self._pass = api_key, api_secret, passphrase
        self._transport, self._clock, self._nonce = transport, clock, nonce
        self._position_mode: str | None = None
        self._instruments: dict[str, Instrument] = {}
        self._sleep: Callable[[float], None] = time.sleep
        self._account_cache: tuple[float, dict] | None = None
        self._account_lock = threading.Lock()
        self.timings: list[tuple[str, float]] = []      # (request, milliseconds), see drain_timings()

    # ------------------------------------------------------------------ plumbing
    def _call(self, method: str, path: str, *, query: dict | None = None,
              body: dict | list | None = None, signed: bool = True, strict_items: bool = True,
              record: bool = True):
        full_path = path + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if signed:
            timestamp, nonce = str(int(self._clock() * 1000)), self._nonce()
            headers.update({
                "ACCESS-KEY": self._key,
                "ACCESS-SIGN": sign(self._secret, full_path, method, timestamp, nonce, payload),
                "ACCESS-TIMESTAMP": timestamp,
                "ACCESS-NONCE": nonce,
                "ACCESS-PASSPHRASE": self._pass,
            })
        data = payload.encode() if body is not None else None

        attempts = 2 if method == "GET" else 1  # never retry a POST
        last_error: Exception | None = None
        started = time.perf_counter()
        for _ in range(attempts):
            try:
                status, text = self._transport(method, self._base + full_path, headers, data)
                break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        else:
            raise ExchangeError(f"network error on {method} {path}: {last_error}") from None
        if record:
            self.timings.append((f"{method} {path.removeprefix('/api/v1/')}",
                                 (time.perf_counter() - started) * 1000))

        try:
            reply = json.loads(text)
        except json.JSONDecodeError:
            raise ExchangeError(f"HTTP {status} from {path}: not JSON ({text[:120]!r})") from None
        if status == 429:
            raise ExchangeError("rate limit hit (HTTP 429), slow down")
        if str(reply.get("code")) != "0":
            raise ExchangeError(f"{path}: code {reply.get('code')}: {reply.get('msg')}")
        items = reply.get("data")
        if strict_items:
            for item in (items if isinstance(items, list) else [items]):
                if isinstance(item, dict) and str(item.get("code", "0")) != "0":
                    raise ExchangeError(f"{path}: code {item.get('code')}: {item.get('msg')}")
        return items

    def keepalive(self, connections: int = 4) -> None:
        """Keep ``connections`` connections to the exchange warm (cheap public request each),
        so the next signal does not pay for opening new ones. Errors are not important here."""
        def ping(_):
            try:
                self._call("GET", "/api/v1/market/tickers", query={"instId": "BTC-USDT"},
                           signed=False, record=False)
            except ExchangeError:
                pass
        with ThreadPoolExecutor(max_workers=connections) as pool:
            list(pool.map(ping, range(connections)))

    def drain_timings(self) -> list[tuple[str, float]]:
        """Duration of every request since the last call (for the latency log)."""
        done, self.timings = self.timings, []
        return done

    # --------------------------------------------------------------- market data
    def get_instrument(self, symbol: str) -> Instrument:
        if symbol not in self._instruments:
            rows = self._call("GET", "/api/v1/market/instruments",
                              query={"instId": symbol}, signed=False)
            row = next((r for r in rows or [] if r.get("instId") == symbol), None)
            if row is None:
                raise ExchangeError(f"unknown instrument {symbol}")
            if str(row.get("state", "live")) != "live":
                raise ExchangeError(f"{symbol} is not tradable right now (state {row.get('state')})")
            try:
                self._instruments[symbol] = Instrument(
                    symbol,
                    _dec(row["contractValue"], "contractValue"),
                    _dec(row["lotSize"], "lotSize"),
                    _dec(row["minSize"], "minSize"),
                    int(Decimal(str(row["maxLeverage"]))),
                )
            except KeyError as exc:
                raise ExchangeError(
                    f"instrument response has no {exc}; fields: {sorted(row)}") from None
        return self._instruments[symbol]

    def get_last_price(self, symbol: str) -> Decimal:
        rows = self._call("GET", "/api/v1/market/tickers", query={"instId": symbol}, signed=False)
        row = next((r for r in rows or [] if r.get("instId") == symbol), None)
        if row is None or "last" not in row:
            raise ExchangeError(f"no price for {symbol}")
        price = _dec(row["last"], "last price")
        if price <= 0:
            raise ExchangeError(f"invalid price for {symbol}: {price}")
        return price

    # ------------------------------------------------------------------- account
    def _account(self) -> dict:
        """The balance response. Balance and free margin come from the same request, so one
        fetch serves both for a moment; any order/close/cancel throws it away."""
        with self._account_lock:
            cached = self._account_cache
            if cached is not None and time.monotonic() - cached[0] < _ACCOUNT_TTL:
                return cached[1]
            data = self._call("GET", "/api/v1/account/balance",
                              query={"productType": "USDT-FUTURES"}) or {}
            self._account_cache = (time.monotonic(), data)
            return data

    def _forget_account(self) -> None:
        with self._account_lock:
            self._account_cache = None

    def get_balance(self) -> Decimal:
        """USDT equity of the futures account."""
        data = self._account()
        details = data.get("details") or []
        usdt = next((d for d in details if d.get("currency") == "USDT"), None)
        if usdt is not None and "equity" in usdt:
            return _dec(usdt["equity"], "USDT equity")
        if "totalEquity" in data:
            return _dec(data["totalEquity"], "totalEquity")
        raise ExchangeError("balance response has no USDT equity")

    def get_available(self) -> Decimal:
        """Free USDT margin (balance not locked by open positions and orders)."""
        data = self._account()
        details = data.get("details") or []
        usdt = next((d for d in details if d.get("currency") == "USDT"), None)
        for field in ("available", "availableEquity"):
            if usdt is not None and usdt.get(field) not in (None, ""):
                return _dec(usdt[field], f"USDT {field}")
        raise ExchangeError(f"balance response has no free-margin field; fields: "
                            f"{sorted(usdt) if usdt else 'no USDT row'}")

    def position_mode(self) -> str:
        """'net' or 'long_short'. Refuses multi-position accounts."""
        if self._position_mode is None:
            data = self._call("GET", "/api/v1/account/position-mode") or {}
            if str(data.get("multiPosition", "false")).lower() == "true":
                raise ExchangeError("Multi-Position mode is on; this bot does not trade it")
            mode = data.get("positionMode")
            if mode == "net_mode":
                self._position_mode = "net"
            elif mode == "long_short_mode":
                self._position_mode = "long_short"
            else:
                raise ExchangeError(f"unknown position mode: {mode!r}")
        return self._position_mode

    def _position_side(self, side: Side) -> str:
        if self.position_mode() == "net":
            return "net"
        return "long" if side is Side.LONG else "short"

    # ------------------------------------------------------------------- trading
    def set_leverage(self, symbol: str, leverage: int, side: Side = Side.LONG,
                     margin_mode: str = "isolated") -> None:
        body = {"instId": symbol, "leverage": str(leverage), "marginMode": margin_mode}
        if self.position_mode() == "long_short" and margin_mode == "isolated":
            body["positionSide"] = "long" if side is Side.LONG else "short"
        self._call("POST", "/api/v1/account/set-leverage", body=body)

    def place_order(self, request: OrderRequest) -> OrderResult:
        is_long = request.side is Side.LONG
        pos_side = self._position_side(request.side)
        legs = request.take_profits
        body = {
            "instId": request.symbol,
            "marginMode": request.margin_mode,
            "positionSide": pos_side,
            "side": "buy" if is_long else "sell",
            "orderType": request.order_type,
            "size": _fmt(request.contracts),
            "clientOrderId": request.client_order_id,
            # the stop-loss is attached to the entry: it protects the whole position
            "slTriggerPrice": _fmt(request.stop_loss),
            "slOrderPrice": "-1",
            "slTriggerPriceType": "last",
        }
        if request.order_type == "limit":
            if request.price is None:
                raise ExchangeError("limit order needs a price")
            body["price"] = _fmt(request.price)
        if len(legs) == 1:  # a single TP can ride on the entry order itself
            body.update({"tpTriggerPrice": _fmt(legs[0][0]), "tpOrderPrice": "-1",
                         "tpTriggerPriceType": "last"})

        rows = self._call("POST", "/api/v1/trade/order", body=body)
        row = rows[0] if isinstance(rows, list) and rows else rows
        if not isinstance(row, dict) or not row.get("orderId"):
            raise ExchangeError(f"order response has no orderId: {rows!r}")

        notes = []
        if len(legs) > 1:  # several TPs: one reduce-only TP order per leg, sent in parallel
            close_side = "sell" if is_long else "buy"

            def place_leg(i: int, price: Decimal, size: Decimal) -> str | None:
                tp_body = {
                    "instId": request.symbol,
                    "marginMode": request.margin_mode,
                    "positionSide": pos_side,
                    "side": close_side,
                    "tpTriggerPrice": _fmt(price),
                    "tpOrderPrice": "-1",
                    "tpTriggerPriceType": "last",
                    "size": _fmt(size),
                    "reduceOnly": "true",
                    "clientOrderId": f"{request.client_order_id}t{i}",
                }
                try:
                    self._call("POST", "/api/v1/trade/order-tpsl", body=tp_body)
                except ExchangeError as exc:
                    return f"TP{i} ({_fmt(price)}) was NOT placed: {exc}"
                return None

            with ThreadPoolExecutor(max_workers=len(legs)) as pool:
                futures = [pool.submit(place_leg, i, price, size)
                           for i, (price, size) in enumerate(legs, start=1)]
                notes = [n for n in (f.result() for f in futures) if n]
        self._forget_account()   # the order locked margin: the cached balance is outdated
        return OrderResult(
            order_id=str(row["orderId"]),
            status="filled" if request.order_type == "market" else "live",
            request=request,
            notes=tuple(notes),
        )

    # ----------------------------------------------------------- emergency clean-up
    def _list_pending(self, path: str, id_field: str) -> list[dict]:
        rows: list[dict] = []
        query: dict = {"limit": str(_PAGE)}
        for _ in range(_MAX_PAGES):
            page = self._call("GET", path, query=query) or []
            rows += page
            if len(page) < _PAGE:
                return rows
            query = {"limit": str(_PAGE), "after": page[-1][id_field]}
        raise ExchangeError(f"{path}: more than {_MAX_PAGES * _PAGE} pending rows, refusing to guess")

    def cancel_pending(self) -> CancelReport:
        """Cancel this bot's UNFILLED entry orders and the take-profit legs of exactly those.

        Open positions and their protection are never touched: a leg is only cancelled
        when its parent entry was really cancelled (never when it filled meanwhile).
        """
        failed: list[str] = []
        entries = [
            o for o in self._list_pending("/api/v1/trade/orders-pending", "orderId")
            if _OUR_ENTRY.match(o.get("clientOrderId") or "")
            and o.get("state") == "live" and str(o.get("reduceOnly")).lower() != "true"
        ]
        cancelled_parents: set[str] = set()
        for i in range(0, len(entries), _BATCH):
            chunk = entries[i:i + _BATCH]
            body = [{"instId": o["instId"], "orderId": o["orderId"]} for o in chunk]
            result = self._call("POST", "/api/v1/trade/cancel-batch-orders", body=body,
                                strict_items=False) or []
            by_id = {str(r.get("orderId")): r for r in result if isinstance(r, dict)}
            for o in chunk:
                r = by_id.get(str(o["orderId"]))
                if r is not None and str(r.get("code", "0")) == "0":
                    cancelled_parents.add(o["clientOrderId"])
                else:
                    why = r.get("msg") if r else "no answer"
                    failed.append(f"order {o['orderId']} ({o['instId']}): {why}")

        legs = [
            t for t in self._list_pending("/api/v1/trade/orders-tpsl-pending", "tpslId")
            if (m := _OUR_LEG.match(t.get("clientOrderId") or "")) and m.group(1) in cancelled_parents
        ]
        legs_done = 0
        for i in range(0, len(legs), _BATCH):
            chunk = legs[i:i + _BATCH]
            body = [{"instId": t["instId"], "tpslId": t["tpslId"]} for t in chunk]
            result = self._call("POST", "/api/v1/trade/cancel-tpsl", body=body,
                                strict_items=False) or []
            by_id = {str(r.get("tpslId")): r for r in result if isinstance(r, dict)}
            for t in chunk:
                r = by_id.get(str(t["tpslId"]))
                if r is not None and str(r.get("code", "0")) == "0":
                    legs_done += 1
                else:
                    failed.append(f"TP/SL {t['tpslId']} ({t['instId']}): {r.get('msg') if r else 'no answer'}")
        self._forget_account()
        return CancelReport(orders=len(cancelled_parents), tpsl=legs_done, failed=tuple(failed))

    # ------------------------------------------------------------------ positions
    def get_positions(self) -> list[PositionInfo]:
        """Open positions (empty rows are skipped)."""
        found = []
        for row in self._call("GET", "/api/v1/account/positions") or []:
            size = _dec(row.get("positions", "0"), "positions")
            if size == 0:
                continue
            pos_side = row.get("positionSide", "net")
            if pos_side == "net":                      # net mode: the sign says long/short
                side = Side.LONG if size > 0 else Side.SHORT
            elif pos_side in ("long", "short"):
                side = Side.LONG if pos_side == "long" else Side.SHORT
            else:
                raise ExchangeError(f"unknown positionSide in the exchange response: {pos_side!r}")
            found.append(PositionInfo(
                symbol=row["instId"], side=side, size=abs(size),
                entry_price=_dec(row.get("averagePrice", "0"), "averagePrice"),
                unrealized_pnl=_dec(row.get("unrealizedPnl") or "0", "unrealizedPnl"),
                margin_mode=row.get("marginMode", "isolated"), position_side=pos_side,
            ))
        return found

    def close_all_positions(self) -> FlattenReport:
        """Close every open position at market, confirm they are gone, then remove the
        TP/SL orders that were protecting them (they protect nothing any more).

        Take-profit/stop-loss orders of instruments whose position could NOT be closed
        are left alone: those positions are still protected.
        """
        failed: list[str] = []
        before = self.get_positions()
        for pos in before:
            body = {"instId": pos.symbol, "marginMode": pos.margin_mode,
                    "positionSide": pos.position_side}
            try:
                self._call("POST", "/api/v1/trade/close-position", body=body)
            except ExchangeError as exc:
                failed.append(f"{pos.symbol}: {exc}")

        remaining = before
        for attempt in range(3):                       # a market close is near-instant; check, do not assume
            remaining = [p for p in self.get_positions()
                         if (p.symbol, p.position_side) in {(b.symbol, b.position_side) for b in before}]
            if not remaining:
                break
            if attempt < 2:
                self._sleep(0.5)
        still_open = {(p.symbol, p.position_side) for p in remaining}
        for p in remaining:
            if not any(f.startswith(f"{p.symbol}:") for f in failed):
                failed.append(f"{p.symbol}: the position is still open after the close request")
        closed = tuple(b for b in before if (b.symbol, b.position_side) not in still_open)

        gone = {p.symbol for p in closed}
        legs = [t for t in self._list_pending("/api/v1/trade/orders-tpsl-pending", "tpslId")
                if t.get("instId") in gone]
        removed = 0
        for i in range(0, len(legs), _BATCH):
            chunk = legs[i:i + _BATCH]
            body = [{"instId": t["instId"], "tpslId": t["tpslId"]} for t in chunk]
            result = self._call("POST", "/api/v1/trade/cancel-tpsl", body=body,
                                strict_items=False) or []
            by_id = {str(r.get("tpslId")): r for r in result if isinstance(r, dict)}
            for t in chunk:
                r = by_id.get(str(t["tpslId"]))
                if r is not None and str(r.get("code", "0")) == "0":
                    removed += 1
                else:
                    failed.append(f"TP/SL {t['tpslId']} ({t['instId']}): {r.get('msg') if r else 'no answer'}")
        self._forget_account()
        return FlattenReport(closed=closed, tpsl=removed, failed=tuple(failed))
