"""Position sizing from account balance, configured allocation and leverage.

Model
-----
* ``allocation_pct`` of the balance is used as MARGIN for one trade.
* notional = margin x leverage, quantity = notional / entry price.
* Optional risk cap: loss at the stop-loss never exceeds ``risk_per_trade_pct``
  of the balance.
* Leverage = min(signal leverage or default, configured max, instrument max).
* Refuses trades whose stop-loss lies too close to the (approximate)
  liquidation price - the stop would never get a chance to fire.
* Quantity is rounded DOWN to the instrument's lot size; below the minimum
  size -> SizingError (no trade).

Everything is Decimal; no floats anywhere near money.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from .models import Side, Signal
from .validation import worst_case_entry

HUNDRED = Decimal(100)


class SizingError(Exception):
    """The trade cannot be sized safely - do not send an order."""


@dataclass(frozen=True)
class Instrument:
    """Exchange contract specification (fetched from the exchange at runtime)."""

    symbol: str
    contract_value: Decimal   # base-asset units per contract
    lot_size: Decimal         # contract quantity step
    min_size: Decimal         # minimum order size in contracts
    max_leverage: int = 100


@dataclass(frozen=True)
class SizingConfig:
    allocation_pct: Decimal = Decimal("25")          # % of balance used as margin per trade
    default_leverage: int = 10                       # used when the signal has no leverage
    max_leverage: int = 20                           # hard cap, overrides the signal
    risk_per_trade_pct: Decimal | None = None        # optional cap on loss at SL, % of balance
    liquidation_buffer: Decimal = Decimal("0.8")     # SL must be inside 80% of liq. distance


@dataclass(frozen=True)
class PositionPlan:
    symbol: str
    side: Side
    leverage: int
    reference_price: Decimal
    contracts: Decimal
    base_quantity: Decimal
    notional: Decimal
    margin: Decimal
    risk_at_stop: Decimal    # expected loss if the stop-loss is hit (excl. fees/slippage)


def plan_position(
    signal: Signal,
    balance: Decimal,
    instrument: Instrument,
    config: SizingConfig = SizingConfig(),
    *,
    last_price: Decimal | None = None,
) -> PositionPlan:
    if balance <= 0:
        raise SizingError("balance must be positive")
    if not Decimal(0) < config.allocation_pct <= HUNDRED:
        raise SizingError("allocation_pct must be in (0, 100]")

    ref = worst_case_entry(signal, last_price)
    if ref is None or ref <= 0:
        raise SizingError("no reference price for a market entry")

    leverage = min(
        signal.leverage or config.default_leverage, config.max_leverage, instrument.max_leverage
    )
    if leverage < 1:
        raise SizingError("leverage must be at least 1")

    sl_distance = abs(ref - signal.stop_loss)
    if sl_distance == 0:
        raise SizingError("stop-loss equals entry price")

    # Approximate liquidation distance for isolated margin = 1/leverage of price.
    liq_fraction = Decimal(1) / Decimal(leverage)
    if sl_distance / ref >= liq_fraction * config.liquidation_buffer:
        raise SizingError(
            f"stop-loss ({sl_distance / ref * HUNDRED:.2f}% away) is too close to the "
            f"liquidation price at {leverage}x leverage"
        )

    margin_budget = balance * config.allocation_pct / HUNDRED
    base_qty = margin_budget * leverage / ref
    if config.risk_per_trade_pct is not None:
        base_qty = min(base_qty, balance * config.risk_per_trade_pct / HUNDRED / sl_distance)

    contracts = (base_qty / instrument.contract_value / instrument.lot_size).to_integral_value(
        ROUND_DOWN
    ) * instrument.lot_size
    if contracts < instrument.min_size:
        raise SizingError(
            f"position of {contracts} contracts is below the minimum order size "
            f"{instrument.min_size} (balance too small for this trade)"
        )

    actual_qty = contracts * instrument.contract_value
    notional = actual_qty * ref
    return PositionPlan(
        symbol=signal.symbol,
        side=signal.side,
        leverage=leverage,
        reference_price=ref,
        contracts=contracts,
        base_quantity=actual_qty,
        notional=notional,
        margin=notional / leverage,
        risk_at_stop=actual_qty * sl_distance,
    )
