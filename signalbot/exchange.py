"""Exchange layer: one small interface, interchangeable implementations.

The pipeline (``executor.py``) only talks to the ``Exchange`` interface, so the
same code runs against:

* ``MockExchange``  - an in-memory simulator that enforces the rules a real
  futures exchange enforces (contract sizes, lot step, leverage must be set,
  margin must be available, ``client_order_id`` must be unique);
* a real BloFin client (added later, once demo API keys are available).

All money values are ``Decimal``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import count
from typing import Protocol

from .models import Side
from .sizing import Instrument


class ExchangeError(Exception):
    """The exchange refused or failed a request."""


@dataclass(frozen=True)
class OrderRequest:
    """Everything the exchange needs for one trade (entry + protection)."""

    symbol: str
    side: Side
    order_type: str                                   # "market" | "limit"
    contracts: Decimal
    price: Decimal | None                             # limit price, None for market
    stop_loss: Decimal
    take_profits: tuple[tuple[Decimal, Decimal], ...]  # (price, contracts) per TP leg
    leverage: int
    client_order_id: str                              # makes a retry/duplicate harmless
    margin_mode: str = "isolated"


@dataclass(frozen=True)
class OrderResult:
    order_id: str
    status: str            # "filled" (market) | "live" (limit waiting for price)
    request: OrderRequest
    notes: tuple[str, ...] = ()   # warnings, e.g. "TP leg 2 was not placed: ..."


@dataclass(frozen=True)
class CancelReport:
    """Result of an emergency clean-up of pending (not yet filled) bot orders."""

    orders: int = 0                  # unfilled entry orders cancelled
    tpsl: int = 0                    # take-profit legs of those entries cancelled
    failed: tuple[str, ...] = ()     # things that could NOT be cancelled (why)


@dataclass(frozen=True)
class PositionInfo:
    """One open position on the exchange."""

    symbol: str
    side: Side
    size: Decimal                    # contracts, always positive
    entry_price: Decimal
    unrealized_pnl: Decimal          # USDT, at the exchange's mark price
    margin_mode: str = "isolated"
    position_side: str = "net"       # as the exchange names it: net | long | short


@dataclass(frozen=True)
class FlattenReport:
    """Result of closing every open position at market."""

    closed: tuple[PositionInfo, ...] = ()   # positions that are really gone
    tpsl: int = 0                           # leftover TP/SL orders of those positions removed
    failed: tuple[str, ...] = ()            # things that could NOT be closed/removed (why)


class Exchange(Protocol):
    name: str

    def get_instrument(self, symbol: str) -> Instrument: ...
    def get_last_price(self, symbol: str) -> Decimal: ...
    def get_balance(self) -> Decimal: ...
    def get_available(self) -> Decimal: ...
    def set_leverage(self, symbol: str, leverage: int, side: Side = Side.LONG) -> None: ...
    def place_order(self, request: OrderRequest) -> OrderResult: ...
    def cancel_pending(self) -> CancelReport: ...
    def get_positions(self) -> list[PositionInfo]: ...
    def close_all_positions(self) -> FlattenReport: ...


# --------------------------------------------------------------------------- #
# Simulator
# --------------------------------------------------------------------------- #

# Illustrative contract specs and prices (NOT live data). They are set close to
# the example signals used in tests and demos.
_SPECS = [
    # symbol,       price,   contract_value, lot,         min,         max_leverage
    ("BTC-USDT", "65000", "0.001", "0.1", "0.1", 100),
    ("ETH-USDT", "3250", "0.01", "0.1", "0.1", 100),
    ("SOL-USDT", "150", "1", "1", "1", 75),
    ("XRP-USDT", "0.52", "1", "1", "1", 75),
    ("LINK-USDT", "14.5", "1", "1", "1", 75),
    ("DOGE-USDT", "0.15", "1", "1", "1", 50),
    ("ADA-USDT", "0.45", "1", "1", "1", 75),
    ("AVAX-USDT", "35", "1", "1", "1", 50),
]
DEFAULT_PRICES = {s: Decimal(p) for s, p, *_ in _SPECS}
DEFAULT_INSTRUMENTS = {
    s: Instrument(s, Decimal(cv), Decimal(lot), Decimal(mn), mx)
    for s, _p, cv, lot, mn, mx in _SPECS
}


class MockExchange:
    name = "імітатор біржі"

    def __init__(
        self,
        balance: Decimal | str = "1000",
        prices: dict[str, Decimal] | None = None,
        instruments: dict[str, Instrument] | None = None,
    ) -> None:
        self.balance = Decimal(balance)
        self.prices = dict(DEFAULT_PRICES if prices is None else prices)
        self.instruments = dict(DEFAULT_INSTRUMENTS if instruments is None else instruments)
        self.leverage: dict[str, int] = {}
        self.orders: list[OrderResult] = []
        self.used_margin = Decimal(0)
        self.entry_prices: dict[str, Decimal] = {}    # order_id -> price the position was opened at
        self._client_ids: set[str] = set()
        self._ids = count(1)

    # -- market data ---------------------------------------------------------
    def get_instrument(self, symbol: str) -> Instrument:
        try:
            return self.instruments[symbol]
        except KeyError:
            raise ExchangeError(f"unknown instrument {symbol}") from None

    def get_last_price(self, symbol: str) -> Decimal:
        try:
            return self.prices[symbol]
        except KeyError:
            raise ExchangeError(f"no price for {symbol}") from None

    def set_price(self, symbol: str, price: Decimal | str) -> None:
        self.prices[symbol] = Decimal(price)

    # -- account -----------------------------------------------------------------
    def get_balance(self) -> Decimal:
        """Total account equity (what position sizing is based on)."""
        return self.balance

    @property
    def available_margin(self) -> Decimal:
        return self.balance - self.used_margin

    def get_available(self) -> Decimal:
        """Free margin: what a new trade can still use."""
        return self.available_margin

    def set_leverage(self, symbol: str, leverage: int, side: Side = Side.LONG) -> None:
        instrument = self.get_instrument(symbol)
        if not 1 <= leverage <= instrument.max_leverage:
            raise ExchangeError(
                f"leverage {leverage}x is outside 1..{instrument.max_leverage} for {symbol}"
            )
        self.leverage[symbol] = leverage

    # -- trading --------------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> OrderResult:
        instrument = self.get_instrument(request.symbol)

        if request.client_order_id in self._client_ids:
            raise ExchangeError(f"duplicate client_order_id {request.client_order_id}")
        if self.leverage.get(request.symbol) != request.leverage:
            raise ExchangeError(f"leverage for {request.symbol} is not set to {request.leverage}x")
        if request.contracts < instrument.min_size:
            raise ExchangeError(f"size {request.contracts} is below the minimum {instrument.min_size}")
        if request.contracts % instrument.lot_size != 0:
            raise ExchangeError(f"size {request.contracts} is not a multiple of lot {instrument.lot_size}")
        if request.order_type == "limit" and request.price is None:
            raise ExchangeError("limit order needs a price")
        if sum(c for _, c in request.take_profits) > request.contracts:
            raise ExchangeError("take-profit legs are larger than the position")

        ref_price = request.price if request.order_type == "limit" else self.get_last_price(request.symbol)
        margin = request.contracts * instrument.contract_value * ref_price / request.leverage
        if margin > self.available_margin:
            raise ExchangeError(
                f"insufficient margin: need {margin:.2f} USDT, available {self.available_margin:.2f} USDT"
            )

        self.used_margin += margin
        self._client_ids.add(request.client_order_id)
        result = OrderResult(
            order_id=f"mock-{next(self._ids)}",
            status="filled" if request.order_type == "market" else "live",
            request=request,
        )
        self.orders.append(result)
        self.entry_prices[result.order_id] = ref_price
        return result

    def cancel_pending(self) -> CancelReport:
        """Cancel every unfilled limit entry (and its take-profit legs); free its margin."""
        live = [o for o in self.orders if o.status == "live"]
        legs = 0
        for order in live:
            req = order.request
            ref = req.price
            self.used_margin -= req.contracts * self.instruments[req.symbol].contract_value * ref / req.leverage
            legs += len(req.take_profits) if len(req.take_profits) > 1 else 0
        self.orders = [o for o in self.orders if o.status != "live"]
        return CancelReport(orders=len(live), tpsl=legs)

    # -- positions ------------------------------------------------------------------
    def _unrealized(self, order: OrderResult) -> Decimal:
        req = order.request
        move = self.get_last_price(req.symbol) - self.entry_prices[order.order_id]
        sign = 1 if req.side is Side.LONG else -1
        return sign * move * req.contracts * self.instruments[req.symbol].contract_value

    def get_positions(self) -> list[PositionInfo]:
        return [
            PositionInfo(o.request.symbol, o.request.side, o.request.contracts,
                         self.entry_prices[o.order_id], self._unrealized(o))
            for o in self.orders if o.status == "filled"
        ]

    def close_all_positions(self) -> FlattenReport:
        """Close every filled position at the live price; profit/loss goes into the balance."""
        closed = self.get_positions()
        for order in [o for o in self.orders if o.status == "filled"]:
            req = order.request
            self.balance += self._unrealized(order)
            margin = (req.contracts * self.instruments[req.symbol].contract_value
                      * self.entry_prices[order.order_id] / req.leverage)
            self.used_margin -= margin
        self.orders = [o for o in self.orders if o.status != "filled"]
        return FlattenReport(closed=tuple(closed))
