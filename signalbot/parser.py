"""Tolerant parser for free-form trading signals posted in Telegram channels.

Real signal channels are messy: emojis everywhere, markdown, "Target 1" instead
of "TP", Ukrainian / Russian / English labels, ``65,000`` vs ``0,52`` numbers,
single-line posts, trade updates ("TP1 hit"), adverts...

Design rules
------------
* Be liberal in what is *recognised* (labels, languages, separators, emojis).
* Be strict in what is *accepted*: anything missing or ambiguous -> REJECTED,
  i.e. the bot must NOT trade (the client's explicit requirement).
* Messages that are not signals at all (chatter, results, updates) -> IGNORED,
  so they never trigger an alarm or an order.

The parser only decides *what the message says*. Whether the trade makes sense
(SL on the right side of entry, price close to market, ...) is decided later
by ``validation.validate_signal``.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .models import ParseResult, Side, Signal, Status

# --------------------------------------------------------------------------- #
# 1. Normalisation
# --------------------------------------------------------------------------- #

_TRANSLATE = str.maketrans(
    {
        "–": "-", "—": "-", "−": "-", "‒": "-", "―": "-",
        "_": " ", "*": " ", "`": " ", "~": " ", "|": " ",
        "•": " ", "·": " ", "«": " ", "»": " ",
    }
)
_KEYCAP = re.compile(r"[0-9#*]️?⃣")           # 1️⃣ 2️⃣ ...
_INVISIBLE = re.compile(r"[​-‏‪-‮⁠﻿︀-️]")
_LIST_PREFIX = re.compile(r"(?m)^[ \t]*\d{1,2}[ \t]*[).][ \t]+(?=\S)")  # "1) 0.82"


def normalize(text: str) -> str:
    """Turn a raw Telegram message into plain, predictable text.

    * full-width / compatibility characters -> ASCII (NFKC)
    * emojis, markdown markers, zero-width characters -> removed
    * dashes unified, whitespace collapsed, list numbering ("1)") removed
    """
    text = _KEYCAP.sub(" ", text)
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = text.translate(_TRANSLATE)
    text = "".join(
        " " if unicodedata.category(ch) in ("So", "Cs", "Cn", "Co") else ch for ch in text
    )
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = _LIST_PREFIX.sub("", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


# --------------------------------------------------------------------------- #
# 2. Numbers
# --------------------------------------------------------------------------- #

# A number that is NOT: part of a word (10x, v2), a percentage (3.1%), a time (14:30).
_NUM = re.compile(r"(?<![\w.,:])\d+(?:[.,]\d+)*(?![\w%]|\s*%|[.,]\d|:\d)")

_GAP_ITEM = (
    r"(?:[\s,;/\-)(\[\]$>→:=.+]"                     # separators
    r"|\d+(?:[.,]\d+)?\s*%"                          # "(+3.1%)" next to a value
    r"|(?:to|and|or|then|до|та|і|и|або|или)(?![^\W\d_]))"
)
_SEP_GAP = re.compile(rf"^{_GAP_ITEM}*$", re.IGNORECASE)


def _to_decimal(token: str) -> Decimal | None:
    """Parse '65,000' / '0,52' / '1.234,5' / '0.1520' into Decimal.

    Rules (documented because they are inherently ambiguous):
    * both ',' and '.' present -> the LAST one is the decimal separator
    * only ',' : 'd{1,3}(,ddd)+' with non-zero lead is thousands (65,000),
      anything else is a decimal comma (0,52 / 0,500 / 3,25)
    * only '.' : several '.ddd' groups = thousands (65.000.000), else decimal
    A price mis-read by a factor 1000 is caught later by the market-price
    deviation check in validation.
    """
    s = token
    if len(s) > 24:
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        if re.fullmatch(r"[1-9]\d{0,2}(,\d{3})+", s):
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif re.fullmatch(r"[1-9]\d{0,2}(\.\d{3}){2,}", s):
        s = s.replace(".", "")
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if value > 0 else None


def _scan_numbers(segment: str) -> list[str]:
    """Raw number tokens that directly follow a label (contiguous list only)."""
    out: list[str] = []
    pos = 0
    for m in _NUM.finditer(segment):
        gap = segment[pos:m.start()]
        if not out:
            # up to a few filler words ("zone:", "at", "по", "price -") but no digits
            if len(gap) > 40 or re.search(r"\d", gap):
                break
        elif not _SEP_GAP.match(gap):
            break
        out.append(m.group())
        pos = m.end()
    return out


# --------------------------------------------------------------------------- #
# 3. Labels
# --------------------------------------------------------------------------- #

_NOT_LETTER_BEFORE = r"(?<![^\W\d_])"
_NOT_LETTER_AFTER = r"(?![^\W\d_])"
_L = r"[^\W\d_]*"  # trailing letters of inflected Cyrillic words

_LABEL_RE = re.compile(
    _NOT_LETTER_BEFORE
    + r"(?:"
    # stop loss ------------------------------------------------------------
    + r"(?P<sl>stop(?:[\s\-]*loss)?|sl|стоп(?:[\s\-]*лос+)?)"
    # take profit / targets ---------------------------------------------------
    + r"|(?P<tp>take[\s\-]*profits?|tps?|tgts?|targets?"
    + r"|тейк(?:[\s\-]*проф[іи]т)?" + _L
    + r"|тп|таргет" + _L + r"|ці(?:ль|лі)|це(?:ль|ли))"
    # entry --------------------------------------------------------------------
    + r"|(?P<entry>entr(?:y|ies)|enter|zone|area|вх[іио]д" + _L + r"|зон[аиу]|ентрі|ентер)"
    # leverage ------------------------------------------------------------------
    + r"|(?P<lev>leverage|lev|плеч" + _L + r")"
    # numbers we must NOT confuse with prices ---------------------------------
    + r"|(?P<noise>risk|ризик|риск|rr|r\s*[/:]\s*r|reward|ratio|balance|баланс|size|розмір"
    + r"|размер|amount|сума|сумма|volume|об'єм|объем|win\s*rate|pnl|profit|прибуток"
    + r"|профіт|профит|result|результат|time|час|время|date|дата|timeframe|tf|margin|маржа"
    + r"|dca|duration|expected|potential|stake|capital|deposit|депозит|signal|id)"
    + r")"
    + _NOT_LETTER_AFTER,
    re.IGNORECASE,
)

# TP/entry index written next to the label: "TP1: 3100", "Target 2 : 66500", "TP#3 5", "TP 3 - 0.17"
_IDX_ATTACHED = re.compile(r"^\d{1,2}(?!\d)(?=\s*[:)=\-]?\s*[\d$])")
_IDX_HASH = re.compile(r"^\s*[#№]\s*\d{1,2}(?!\d)")
_IDX_PUNCT = re.compile(r"^\s+\d{1,2}(?!\d)(?=\s*[:)=]\s*[\d$]|\s*-\s+[\d$])")

# --------------------------------------------------------------------------- #
# 4. Side, symbol, leverage, market, update detection
# --------------------------------------------------------------------------- #

_LONG_RE = re.compile(
    _NOT_LETTER_BEFORE + r"(?:longs?|buy|лонг" + _L + r"|купи" + _L + r"|купів" + _L
    + r"|покупк" + _L + r")" + _NOT_LETTER_AFTER,
    re.IGNORECASE,
)
_SHORT_RE = re.compile(
    _NOT_LETTER_BEFORE + r"(?:shorts?|sell|шорт" + _L + r"|продаж" + _L + r"|продат" + _L
    + r"|продам)" + _NOT_LETTER_AFTER,
    re.IGNORECASE,
)

_NOT_A_SYMBOL = frozenset(
    """
    LONG SHORT BUY SELL ENTRY TP SL IN OF TO AT ON IS FOR PRICE PERP PAIR COIN SIZE RISK
    AMOUNT MARGIN BALANCE VOLUME PROFIT LOSS FUTURES SPOT SIGNAL SIGNALS TRADE TARGET TARGETS
    STOP LEV LEVERAGE POSITION NOW TERM ZONE LIMIT MARKET ORDER SETUP ALERT IDEA SCALP SWING
    UPDATE CALL PLAY ONLY THE VIP FREE CRYPTO TRADING BINANCE BLOFIN BYBIT FOREX USDT USDC
    USD BUSD PERPETUAL ENTER AREA TIME DATE
    """.split()
)

_SYM_USDT = re.compile(
    r"(?<![A-Z0-9])([A-Z0-9]{2,12}?)\s?[/\-]?\s?USDT(?:\.P|\s?PERP(?:ETUAL)?)?(?![A-Z0-9])"
)
_SYM_LABELED = re.compile(
    r"(?:COIN|PAIR|SYMBOL|TOKEN|ASSET|МОНЕТА|ПАРА|ТОКЕН|ТІКЕР|ТИКЕР)\s*[:\-]\s*[#$]?([A-Z0-9]{2,12})"
)
_SYM_TAG = re.compile(r"[#$]([A-Z][A-Z0-9]{1,11})(?![A-Z0-9])")
_SIDE_UP = r"(?:LONG|SHORT|BUY|SELL|ЛОНГ|ШОРТ)"
_SYM_BESIDE_SIDE = (
    re.compile(rf"(?<![A-Z0-9])([A-Z]{{2,10}})\s+{_SIDE_UP}(?![A-ZА-Я])"),
    re.compile(rf"(?<![A-ZА-Я]){_SIDE_UP}\s+([A-Z]{{2,10}})(?![A-Z0-9])"),
)


def _clean_symbol(raw: str) -> str | None:
    base = raw[:-4] if raw.endswith("USDT") and len(raw) > 4 else raw
    if base in _NOT_A_SYMBOL or not re.search(r"[A-Z]", base):
        return None
    return base


def _find_symbols(norm: str) -> set[str]:
    """Candidate base assets, normalised to BloFin instId style 'BASE-USDT'."""
    up = norm.upper()
    for regex in (_SYM_USDT, _SYM_LABELED, _SYM_TAG):
        found = {s for raw in regex.findall(up) if (s := _clean_symbol(raw))}
        if found:
            return {f"{s}-USDT" for s in found}
    found = set()
    for regex in _SYM_BESIDE_SIDE:
        found |= {s for raw in regex.findall(up) if (s := _clean_symbol(raw))}
    return {f"{s}-USDT" for s in found}


_LEV_LABELED = re.compile(
    _NOT_LETTER_BEFORE + r"(?:leverage|lev|плеч" + _L + r")[\s:=\-]*"
    + r"(?:cross|isolated|iso|кросс?|ізольован" + _L + r")?[\s:=\-]*[xх]?\s*"
    + r"(\d{1,3})(?:\s*-\s*(\d{1,3}))?(?![\d.,])",
    re.IGNORECASE,
)
# 10x, 10х (Cyrillic х!), x10
_LEV_X = re.compile(
    r"(?<![\w.,:])(\d{1,3})\s*[xх](?![^\W\d_])|(?<![\w.,:])[xх]\s*(\d{1,3})(?![\w.,%])",
    re.IGNORECASE,
)


def _find_leverage(norm: str) -> int | None:
    found: list[int] = []
    for m in _LEV_LABELED.finditer(norm):
        found += [int(g) for g in m.groups() if g]
    for m in _LEV_X.finditer(norm):
        found.append(int(m.group(1) or m.group(2)))
    # several different values -> be conservative and take the lowest
    return min(found) if found else None


_MARKET_WORD = re.compile(
    _NOT_LETTER_BEFORE + r"(?:market|cmp|now|current|ринк" + _L + r"|рынк" + _L + r"|поточн"
    + _L + r"|текущ" + _L + r"|зараз|сейчас)" + _NOT_LETTER_AFTER,
    re.IGNORECASE,
)
_MARKET_PHRASE = re.compile(
    r"(?:at\s+market|market\s+(?:price|order|entry|execution)|\bcmp\b"
    r"|(?:enter|buy|sell|long|short)\s+now|по\s+ринку|по\s+рынку)",
    re.IGNORECASE,
)
_UPDATE_RE = re.compile(
    _NOT_LETTER_BEFORE + r"(?:hit|reached|achieved|done|closed?|moved?|moving|cancel(?:l?ed)?"
    + r"|booked|breakeven|break-even|be|досягнут" + _L + r"|закрит" + _L + r"|закрыт" + _L
    + r"|перенес" + _L + r"|перенос" + _L + r"|відмін" + _L + r"|отмен" + _L + r"|взят" + _L
    + r"|безубит" + _L + r"|безубыт" + _L + r")" + _NOT_LETTER_AFTER,
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# 5. Parser
# --------------------------------------------------------------------------- #

@dataclass
class _Segment:
    kind: str                 # "sl" | "tp" | "entry"
    text: str                 # text between this label and the next one
    tokens: list[str]
    lead_candidate: str | None = None   # small leading int that MIGHT be a list index


def _segments(norm: str) -> list[_Segment]:
    labels = list(_LABEL_RE.finditer(norm))
    segments: list[_Segment] = []
    for i, m in enumerate(labels):
        kind = m.lastgroup
        if kind not in ("sl", "tp", "entry"):
            continue
        end = labels[i + 1].start() if i + 1 < len(labels) else len(norm)
        text = norm[m.end():end]
        lead: str | None = None
        if kind in ("tp", "entry"):
            idx = _IDX_ATTACHED.match(text) or _IDX_HASH.match(text) or _IDX_PUNCT.match(text)
            if idx:
                text = text[idx.end():]
        tokens = _scan_numbers(text)
        if kind in ("tp", "entry") and len(tokens) >= 2 and re.fullmatch(r"\d{1,2}", tokens[0]):
            lead = tokens[0]   # "TP 1 67000": index or a very cheap price? decided later
        segments.append(_Segment(kind, text, tokens, lead))
    return segments


def _drop_ambiguous_leads(segments: list[_Segment], sl: Decimal | None) -> None:
    """Decide whether a small leading integer ("Target 1 65800") is a list index."""
    tp_leads = [s.lead_candidate for s in segments if s.kind == "tp" and s.lead_candidate]
    sequential = len(tp_leads) >= 2 and [int(x) for x in tp_leads] == list(
        range(int(tp_leads[0]), int(tp_leads[0]) + len(tp_leads))
    )
    for s in segments:
        if s.lead_candidate is None:
            continue
        # Prices >= 100: a "target" of 1..99 is absurd, so it is surely an index.
        if sequential or (sl is not None and sl >= 100):
            s.tokens = s.tokens[1:]
        s.lead_candidate = None


def _distinct(values: list[Decimal]) -> list[Decimal]:
    return sorted(set(values))


def parse_signal(text: str) -> ParseResult:
    """Parse one Telegram message. Never raises; never guesses."""
    raw = text or ""
    norm = normalize(raw)
    if not norm:
        return ParseResult(Status.IGNORED, reason="empty message")

    segments = _segments(norm)
    kinds = {s.kind for s in segments}
    if not ({"sl", "tp"} & kinds):
        return ParseResult(Status.IGNORED, reason="no SL/TP labels - not a trade signal")

    # ---- stop loss (resolved first: it tells us the price scale) --------------
    sl_segments = [s for s in segments if s.kind == "sl"]
    if any(len(s.tokens) > 1 for s in sl_segments):
        sl_ambiguous = True
    else:
        sl_ambiguous = False
    sl_values = [v for s in sl_segments for t in s.tokens[:1] if (v := _to_decimal(t))]
    sl_scale = max(sl_values) if sl_values else None
    _drop_ambiguous_leads(segments, sl_scale)

    tp_values = [v for s in segments if s.kind == "tp" for t in s.tokens if (v := _to_decimal(t))]
    entry_values = [
        v for s in segments if s.kind == "entry" for t in s.tokens if (v := _to_decimal(t))
    ]

    # ---- direction -----------------------------------------------------------------
    sides = set()
    if _LONG_RE.search(norm):
        sides.add(Side.LONG)
    if _SHORT_RE.search(norm):
        sides.add(Side.SHORT)

    symbols = _find_symbols(norm)

    if not sides:
        if _UPDATE_RE.search(norm):
            return ParseResult(Status.IGNORED, reason="looks like a trade update")
        if symbols and sl_values and tp_values:
            return ParseResult(Status.REJECTED, reason="direction (long/short) not found")
        return ParseResult(Status.IGNORED, reason="no direction found")
    if len(sides) > 1:
        return ParseResult(Status.REJECTED, reason="conflicting direction: both LONG and SHORT mentioned")
    side = next(iter(sides))

    if not symbols:
        return ParseResult(Status.REJECTED, reason="symbol not found (only USDT pairs are supported)")
    if len(symbols) > 1:
        return ParseResult(Status.REJECTED, reason=f"ambiguous symbol: {', '.join(sorted(symbols))}")
    symbol = next(iter(symbols))

    # ---- stop loss --------------------------------------------------------------------
    if sl_ambiguous:
        return ParseResult(Status.REJECTED, reason="ambiguous stop-loss: several values after SL")
    sl_distinct = _distinct(sl_values)
    if not sl_distinct:
        return ParseResult(Status.REJECTED, reason="stop-loss value not found")
    if len(sl_distinct) > 1:
        return ParseResult(Status.REJECTED, reason="conflicting stop-loss values")
    stop_loss = sl_distinct[0]

    # ---- take profit ----------------------------------------------------------------------
    tps = _distinct(tp_values)
    if not tps:
        return ParseResult(Status.REJECTED, reason="take-profit value not found")
    if side is Side.SHORT:
        tps.reverse()  # nearest first

    # ---- entry ---------------------------------------------------------------------------------
    entries = _distinct(entry_values)
    if not entries:
        entry_segs = [s for s in segments if s.kind == "entry"]
        market = (
            any(_MARKET_WORD.search(s.text) for s in entry_segs)
            if entry_segs
            else bool(_MARKET_PHRASE.search(norm))
        )
        if not market:
            return ParseResult(Status.REJECTED, reason="entry price not found")

    return ParseResult(
        Status.SIGNAL,
        Signal(
            symbol=symbol,
            side=side,
            entries=tuple(entries),
            stop_loss=stop_loss,
            take_profits=tuple(tps),
            leverage=_find_leverage(norm),
            raw_text=raw,
        ),
    )
