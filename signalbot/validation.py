"""Safety checks that run AFTER parsing and BEFORE any order is built.

``validate_signal`` returns a list of human-readable problems. An empty list
means the signal is safe to size and send; anything else means: do not trade
(and tell the owner why).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .models import Side, Signal

HUNDRED = Decimal(100)


@dataclass(frozen=True)
class ValidationLimits:
    allowed_symbols: frozenset[str] | None = None   # e.g. loaded from the exchange's instrument list
    max_sanity_leverage: int = 125
    min_sl_distance_pct: Decimal = Decimal("0.05")
    max_sl_distance_pct: Decimal = Decimal("15")
    max_tp_distance_pct: Decimal = Decimal("100")
    max_entry_deviation_pct: Decimal = Decimal("5")  # signal entry vs live price
    max_signal_age_seconds: int = 120
    min_reward_risk: Decimal | None = None           # e.g. Decimal("1") to require RR >= 1 on TP1


def worst_case_entry(signal: Signal, last_price: Decimal | None = None) -> Decimal | None:
    """Entry price that gives the largest distance to the stop (used for risk/sizing).

    LONG -> highest entry, SHORT -> lowest entry, market -> live price.
    """
    if signal.is_market:
        return last_price
    return max(signal.entries) if signal.side is Side.LONG else min(signal.entries)


def _pct(a: Decimal, b: Decimal) -> Decimal:
    return abs(a - b) / b * HUNDRED


def validate_signal(
    signal: Signal,
    limits: ValidationLimits = ValidationLimits(),
    *,
    last_price: Decimal | None = None,
    signal_time: datetime | None = None,
    now: datetime | None = None,
) -> list[str]:
    errors: list[str] = []

    # --- instrument -----------------------------------------------------------
    if limits.allowed_symbols is not None and signal.symbol not in limits.allowed_symbols:
        errors.append(f"symbol {signal.symbol} is not tradable on the exchange")

    # --- freshness --------------------------------------------------------------
    if signal_time is not None and now is not None:
        age = (now - signal_time).total_seconds()
        if age > limits.max_signal_age_seconds:
            errors.append(f"signal is stale ({int(age)}s old, limit {limits.max_signal_age_seconds}s)")
        if age < -5:
            errors.append("signal timestamp is in the future")

    # --- leverage -----------------------------------------------------------------
    if signal.leverage is not None and not 1 <= signal.leverage <= limits.max_sanity_leverage:
        errors.append(f"leverage {signal.leverage}x is outside 1..{limits.max_sanity_leverage}")

    # --- price geometry -----------------------------------------------------------
    if signal.is_market:
        if last_price is None:
            errors.append("market entry requires a live price, none available")
            return errors
        lo = hi = last_price
    else:
        lo, hi = min(signal.entries), max(signal.entries)

    is_long = signal.side is Side.LONG
    sl = signal.stop_loss
    if is_long:
        if sl >= lo:
            errors.append(f"LONG: stop-loss {sl} must be below entry {lo}")
        bad_tps = [t for t in signal.take_profits if t <= hi]
        if bad_tps:
            errors.append(f"LONG: take-profit {bad_tps[0]} must be above entry {hi}")
    else:
        if sl <= hi:
            errors.append(f"SHORT: stop-loss {sl} must be above entry {hi}")
        bad_tps = [t for t in signal.take_profits if t >= lo]
        if bad_tps:
            errors.append(f"SHORT: take-profit {bad_tps[0]} must be below entry {lo}")
    if errors and any("must be" in e for e in errors):
        return errors  # distances below are meaningless on a broken geometry

    ref = hi if is_long else lo  # worst-case entry
    sl_pct = _pct(sl, ref)
    if sl_pct < limits.min_sl_distance_pct:
        errors.append(f"stop-loss is only {sl_pct:.3f}% from entry (min {limits.min_sl_distance_pct}%)")
    if sl_pct > limits.max_sl_distance_pct:
        errors.append(f"stop-loss is {sl_pct:.1f}% from entry (max {limits.max_sl_distance_pct}%)")

    far_tp = max(abs(t - ref) / ref * HUNDRED for t in signal.take_profits)
    if far_tp > limits.max_tp_distance_pct:
        errors.append(f"take-profit is {far_tp:.0f}% from entry (max {limits.max_tp_distance_pct}%) - typo?")

    if limits.min_reward_risk is not None:
        reward = abs(signal.take_profits[0] - ref)
        risk = abs(ref - sl)
        if risk > 0 and reward / risk < limits.min_reward_risk:
            errors.append(f"reward/risk on TP1 is {reward / risk:.2f} (min {limits.min_reward_risk})")

    # --- protection orders versus the live price ------------------------------------
    # The exchange rejects a take-profit that is already behind the market (seen on
    # BloFin demo: code 102037), and a stop on the wrong side would fire at once.
    # A half-protected position is worse than no trade, so refuse the whole signal.
    if last_price is not None:
        if is_long:
            behind = [t for t in signal.take_profits if t <= last_price]
            if behind:
                errors.append(f"LONG: take-profit {behind[0]} is already at or below the live price {last_price}")
            if sl >= last_price:
                errors.append(f"LONG: stop-loss {sl} is already at or above the live price {last_price}")
        else:
            behind = [t for t in signal.take_profits if t >= last_price]
            if behind:
                errors.append(f"SHORT: take-profit {behind[0]} is already at or above the live price {last_price}")
            if sl <= last_price:
                errors.append(f"SHORT: stop-loss {sl} is already at or below the live price {last_price}")

    # --- sanity versus the live market (catches 1000x / decimal-separator mistakes) ----
    if last_price is not None and not signal.is_market:
        for e in signal.entries:
            dev = _pct(e, last_price)
            if dev > limits.max_entry_deviation_pct:
                errors.append(
                    f"entry {e} is {dev:.1f}% away from the live price {last_price} "
                    f"(max {limits.max_entry_deviation_pct}%)"
                )
                break
    return errors
