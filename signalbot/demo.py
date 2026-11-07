"""Demo signal generator (for the portfolio video and manual testing).

Builds realistic signals around the LIVE price, so they pass the bot's own
safety checks, in the messy styles real channels use. ``bad_text`` makes the
classic decimal-shift typo that the bot must refuse.

Pure functions only (no network, no Telegram): the bot's /demo command passes
in the live price and posts the returned text to the channel.
"""
from __future__ import annotations

import random
from decimal import ROUND_HALF_UP, Decimal

SYMBOLS = ("BTC", "ETH", "SOL", "XRP")
STYLES = ("clean", "emoji", "ukrainian", "card")
# the same trade, written differently (single-entry styles pair up, zone styles pair up)
OTHER_STYLE = {"clean": "ukrainian", "ukrainian": "clean", "emoji": "card", "card": "emoji"}
ZONE_STYLES = ("emoji", "card")


def _sig(price: Decimal, digits: int = 4) -> Decimal:
    """Round to 4 significant digits: a sane tick for any coin."""
    return price.quantize(Decimal(1).scaleb(price.adjusted() - digits + 1), ROUND_HALF_UP)


def _f(d: Decimal) -> str:
    return format(d.normalize(), "f")


def build(symbol: str, side: str, last: Decimal, rng: random.Random, style: str,
          market: bool = False) -> dict:
    """Trade values around the live price ``last``. side: "LONG" | "SHORT".

    ``market=True`` -> entry "market" (no entry price; single-entry styles only).
    """
    sign = 1 if side == "LONG" else -1
    pct = lambda lo, hi: Decimal(str(round(rng.uniform(lo, hi), 2))) / 100  # noqa: E731
    if market:
        entry, zone = last, None                                    # fills at once at the live price
    else:
        entry = _sig(last * (1 - sign * pct(1.0, 2.5)))             # waits for a small pullback
        zone = _sig(entry * (1 + sign * Decimal("0.002"))) if style in ZONE_STYLES else None
    worst = zone if zone is not None else entry                      # LONG: highest, SHORT: lowest
    stop = _sig(min(entry, worst) * (1 - Decimal("0.025")) if side == "LONG"
                else max(entry, worst) * (1 + Decimal("0.025")))
    steps = [Decimal("0.02"), Decimal("0.04"), Decimal("0.06")][: rng.choice((2, 3))]
    tps = [_sig(last * (1 + sign * s)) for s in steps]              # all beyond the live price
    entries = [] if market else (sorted([entry, zone]) if zone is not None else [entry])
    return {
        "symbol": symbol, "side": side, "entries": entries, "stop": stop, "tps": tps,
        "leverage": rng.choice((5, 10, 10, 15)), "style": style,
    }


def render(v: dict, style: str | None = None) -> str:
    style = style or v["style"]
    sym, side = v["symbol"], v["side"]
    long = side == "LONG"
    market = not v["entries"]
    lo, hi = (None, None) if market else (v["entries"][0], v["entries"][-1])
    e = "market" if market else _f(lo)
    tps = [_f(t) for t in v["tps"]]
    sl, lev = _f(v["stop"]), v["leverage"]
    if style == "clean":
        lines = [f"{sym}USDT {side}", f"Entry: {e}", f"SL: {sl}"]
        lines += [f"TP{i}: {t}" for i, t in enumerate(tps, 1)] + [f"Leverage: {lev}x"]
    elif style == "ukrainian":
        lines = [f"{'🟢 ЛОНГ' if long else '🔴 ШОРТ'} {sym}USDT", f"Вхід: {e}"]
        lines += [f"Ціль {i}: {t}" for i, t in enumerate(tps, 1)]
        lines += [f"Стоп-лосс: {sl}", f"Плече: {lev}х"]
    elif style == "emoji":
        lines = [f"🔥🔥 {sym}/USDT {'🟢 LONG' if long else '🔴 SHORT'} 🔥🔥", "",
                 f"🎯 Entry zone: {_f(lo)} - {_f(hi)}"]
        lines += [f"🎯 Target {i}: {t}" for i, t in enumerate(tps, 1)]
        lines += [f"🛑 Stop: {sl}", f"⚡ Leverage: Cross {lev}x"]
    elif style == "card":
        lines = ["📊 SIGNAL #1042", f"🪙 Pair: {sym}/USDT",
                 f"{'📈' if long else '📉'} Direction: {side}", f"💵 Entry: {_f(lo)} – {_f(hi)}"]
        lines += [f"🎯 TP{i}: {t}" for i, t in enumerate(tps, 1)]
        lines += [f"🛑 SL: {sl}", f"⚡ Leverage: {lev}x", "⚖️ Risk: 1-2%", "🕐 Time: 14:30"]
    else:
        raise ValueError(f"unknown style {style!r}")
    return "\n".join(lines)


def bad_text(v: dict) -> str:
    """The same signal with every price 10x too small (a missed digit): must be refused."""
    shifted = dict(v, entries=[_sig(p / 10) for p in v["entries"]], stop=_sig(v["stop"] / 10),
                   tps=[_sig(t / 10) for t in v["tps"]])
    return render(shifted, "clean")


def pick(rng: random.Random, symbol: str | None = None) -> tuple[str, str, str]:
    """Random (symbol, side, style); a given symbol is kept."""
    return (symbol or rng.choice(SYMBOLS), rng.choice(("LONG", "SHORT")), rng.choice(STYLES))


# --------------------------------------------------------------------------- #
# The show: a program of situations for a long demo / video
# --------------------------------------------------------------------------- #
from typing import Callable  # noqa: E402

# kind -> relative frequency in the random part of the program
WEIGHTS = {
    "valid_limit": 30,    # clean / emoji / ukrainian / card, LONG and SHORT, 4 coins
    "valid_market": 14,   # "Entry: market": the position opens at once
    "valid_highlev": 4,   # the signal asks for 50x: the bot caps it
    "ignored": 14,        # chatter, ads, trade updates: no reaction expected
    "rejected": 12,       # incomplete / ambiguous signals: parser says "do not trade"
    "typo": 8,            # a digit is missing in every price: live-price check refuses
    "wrong_geometry": 5,  # stop-loss on the wrong side of the entry
    "unknown_symbol": 3,  # a coin the exchange does not list
    "dup": 8,             # the same trade in other words: duplicate guard
}
SCENES = ("stop_scene", "margin_scene")  # scripted by the bot, placed by build_program

CHATTER = [
    "✅ BTCUSDT TP1 reached! +3.2% profit 🔥",
    "Good morning traders! Market looks bullish today 🌞",
    "BTC update: moving SL to breakeven, TP1 hit. Keep holding",
    "🚨 VIP SALE -50% today only! Join now",
    "Results for the week: ETHUSDT LONG +12%, SOLUSDT SHORT +8%, BTCUSDT LONG +5%",
    "🔥🔥🔥",
    "Long-term outlook for BTC: I'm bullish. Entry points soon.",
    "Try our SL/TP calculator bot!",
    "⚠️ Reminder: trading is risky, never invest more than you can lose.",
    "ETH update: TP2 reached, closing the trade. Thanks everyone 🙏",
]

UNKNOWN = "XYZUSDT LONG\nEntry: 12.5\nSL: 12.1\nTP1: 13.2\nTP2: 14\nLeverage: 10x"


def _valid(sym: str, side: str, style: str, rng: random.Random, prices, market=False,
           leverage: int | None = None) -> tuple[str, dict]:
    v = build(sym, side, prices(sym), rng, style, market=market)
    if leverage:
        v["leverage"] = leverage
    return render(v), v


def build_program(n: int, rng: random.Random) -> list[str]:
    """``n`` situations. The first is a clean valid signal; the emergency-stop scene
    sits in the middle and the margin scene near the end (n >= 8)."""
    kinds = list(WEIGHTS)
    weights = [WEIGHTS[k] for k in kinds]
    program = ["valid_limit"] + [rng.choices(kinds, weights)[0] for _ in range(n - 1)]
    if n >= 8:
        program[n // 2] = "stop_scene"
        program[n - 2] = "margin_scene"
        program[n - 1] = "ignored"
    if n >= 8:  # market entries open positions at once: the final scene needs some to close
        taken = {0, n // 2, n - 2, n - 1}
        slots = [i for i in range(1, n) if i not in taken]
        want = max(2, n // 8)
        step = len(slots) / want
        for j in range(want):
            program[slots[min(len(slots) - 1, int(j * step + step / 2))]] = "valid_market"
    have_valid = False
    for i, kind in enumerate(program):                    # a duplicate needs something to copy
        if kind.startswith("valid") or kind == "stop_scene":
            have_valid = True
        elif kind == "dup" and not have_valid:
            program[i] = "valid_limit"
            have_valid = True
    return program


def make_event(kind: str, rng: random.Random, prices: Callable[[str], Decimal],
               previous: dict | None = None) -> tuple[str, dict | None]:
    """(text to post, trade values if it is a valid trade else None)."""
    sym, side, style = pick(rng)
    if kind == "valid_limit":
        return _valid(sym, side, style, rng, prices)
    if kind == "valid_market":
        return _valid(sym, side, rng.choice(("clean", "ukrainian")), rng, prices, market=True)
    if kind == "valid_highlev":
        return _valid(sym, side, "clean", rng, prices, leverage=50)
    if kind == "ignored":
        return rng.choice(CHATTER), None
    if kind == "typo":
        v = build(sym, side, prices(sym), rng, "clean")
        return bad_text(v), None
    if kind == "wrong_geometry":
        v = build(sym, side, prices(sym), rng, "clean")
        mirror = v["entries"][0] * 2 - v["stop"]          # stop-loss on the other side of the entry
        return render(dict(v, stop=_sig(mirror)), "clean"), None
    if kind == "unknown_symbol":
        return UNKNOWN, None
    if kind == "rejected":
        v = build(sym, side, prices(sym), rng, "clean")
        text = render(v, "clean").split("\n")
        e, tps = _f(v["entries"][0]), [_f(t) for t in v["tps"]]
        sl = _f(v["stop"])
        broken = [
            "\n".join(x for x in text if not x.startswith("SL")),                     # no stop-loss
            "\n".join(x for x in text if not x.startswith("TP")),                     # no take-profit
            f"{sym}USDT LONG or SHORT? Entry {e} SL {sl} TP {tps[0]}",                # no direction
            f"{sym}USDT {side} Entry {e} SL {sl} SL {_f(v['stop'] * Decimal('0.99'))} TP {tps[0]}",
            f"{sym}USDT {side} Entry {e} SL 2% TP {tps[0]}",                          # stop only in %
            f"{sym}/USDC {side} Entry {e} SL {sl} TP {tps[0]}",                       # wrong quote
        ]
        return rng.choice(broken), None
    if kind == "dup":
        assert previous is not None, "dup needs a previous trade"
        return render(previous, OTHER_STYLE[previous["style"]]), None
    raise ValueError(f"unknown kind {kind!r}")
