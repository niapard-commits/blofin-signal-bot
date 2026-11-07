"""The execution pipeline: validate -> size -> build order -> send.

    Signal  ->  exchange data (instrument, price, balance)
            ->  validate_signal()   any problem  => do NOT trade
            ->  plan_position()     cannot size  => do NOT trade
            ->  OrderRequest (entry + stop-loss + take-profit legs)
            ->  exchange.set_leverage() + exchange.place_order()

``ExecutionResult.order_attempted`` tells the caller whether an order may
already exist on the exchange. If it does (e.g. a timeout *after* sending), the
duplicate-guard entry must be KEPT, otherwise a retry could open the position
twice. The signal's fingerprint is also sent as ``client_order_id`` so the
exchange itself rejects a second copy.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from .exchange import Exchange, ExchangeError, OrderRequest, OrderResult
from .models import Signal
from .sizing import PositionPlan, SizingConfig, SizingError, plan_position
from .validation import ValidationLimits, validate_signal


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    reason: str = ""
    plan: PositionPlan | None = None
    request: OrderRequest | None = None
    order: OrderResult | None = None
    order_attempted: bool = False


def split_take_profits(
    take_profits: tuple[Decimal, ...], contracts: Decimal, lot_size: Decimal
) -> tuple[tuple[Decimal, Decimal], ...]:
    """Split a position evenly across take-profit levels, in whole lots.

    The nearest levels get the remainder. If the position has fewer lots than
    there are targets, only the nearest targets are used. The legs always add
    up to exactly ``contracts``.
    """
    lots = int((contracts / lot_size).to_integral_value(ROUND_DOWN))
    n = min(len(take_profits), lots)
    if n <= 0:
        return ()
    base, extra = divmod(lots, n)
    return tuple(
        (price, (base + (1 if i < extra else 0)) * lot_size)
        for i, price in enumerate(take_profits[:n])
    )


def execute_signal(
    signal: Signal,
    exchange: Exchange,
    *,
    sizing: SizingConfig = SizingConfig(),
    limits: ValidationLimits = ValidationLimits(),
    signal_time: datetime | None = None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> ExecutionResult:
    # 1. facts from the exchange ------------------------------------------------
    # three independent requests: asked at the same time, so the slowest one sets the pace
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(exchange.get_instrument, signal.symbol),
                   pool.submit(exchange.get_last_price, signal.symbol),
                   pool.submit(exchange.get_balance)]
    try:
        instrument, last_price, balance = (f.result() for f in futures)
    except ExchangeError as exc:  # the first failure in this fixed order is reported
        return ExecutionResult(False, f"exchange: {exc}")

    # 2. safety checks ---------------------------------------------------------------
    errors = validate_signal(
        signal, limits, last_price=last_price, signal_time=signal_time, now=now
    )
    if errors:
        return ExecutionResult(False, "; ".join(errors))

    # 3. position size -----------------------------------------------------------------
    try:
        plan = plan_position(signal, balance, instrument, sizing, last_price=last_price)
    except SizingError as exc:
        return ExecutionResult(False, f"sizing: {exc}")

    # 3b. is there enough FREE margin? (open positions and pending orders lock margin)
    try:
        available = exchange.get_available()
    except ExchangeError as exc:
        return ExecutionResult(False, f"exchange: {exc}", plan=plan)
    if plan.margin > available:
        return ExecutionResult(
            False,
            f"insufficient free margin: this trade needs {plan.margin:.2f} USDT, "
            f"only {available:.2f} USDT is free",
            plan=plan,
        )

    legs = split_take_profits(signal.take_profits, plan.contracts, instrument.lot_size)
    request = OrderRequest(
        symbol=signal.symbol,
        side=signal.side,
        order_type="market" if signal.is_market else "limit",
        contracts=plan.contracts,
        price=None if signal.is_market else plan.reference_price,
        stop_loss=signal.stop_loss,
        take_profits=legs,
        leverage=plan.leverage,
        client_order_id=signal.fingerprint,
    )
    if dry_run:
        return ExecutionResult(True, plan=plan, request=request)

    # 4. send ---------------------------------------------------------------------------
    try:
        exchange.set_leverage(signal.symbol, plan.leverage, signal.side)
    except ExchangeError as exc:  # nothing was ordered yet, safe to retry later
        return ExecutionResult(False, f"exchange: {exc}", plan=plan, request=request)
    try:
        order = exchange.place_order(request)
    except ExchangeError as exc:  # an order MAY exist (e.g. timeout after sending)
        return ExecutionResult(
            False, f"exchange: {exc}", plan=plan, request=request, order_attempted=True
        )
    return ExecutionResult(True, plan=plan, request=request, order=order, order_attempted=True)
