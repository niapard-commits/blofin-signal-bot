"""Plain data types shared by the parser, validator and position sizer."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


class Status(str, Enum):
    SIGNAL = "signal"      # a complete, structurally valid signal was found
    IGNORED = "ignored"    # not a trade signal at all (chatter, ads, trade updates)
    REJECTED = "rejected"  # looks like a signal but is incomplete/ambiguous -> DO NOT TRADE


def _fmt(d: Decimal) -> str:
    return format(d.normalize(), "f")


@dataclass(frozen=True)
class Signal:
    """A parsed trading signal. All prices are Decimal (never float)."""

    symbol: str                        # BloFin instId style, e.g. "BTC-USDT"
    side: Side
    entries: tuple[Decimal, ...]       # ascending; empty tuple => market entry
    stop_loss: Decimal
    take_profits: tuple[Decimal, ...]  # nearest first (asc for LONG, desc for SHORT)
    leverage: int | None = None        # advisory; the bot's own cap always wins
    raw_text: str = ""

    @property
    def is_market(self) -> bool:
        return not self.entries

    @property
    def fingerprint(self) -> str:
        """Stable hash of the trade itself (not of the message text).

        The same setup re-posted with different emojis/wording gets the same
        fingerprint, which is what the duplicate guard stores.
        """
        parts = [
            self.symbol,
            self.side.value,
            ",".join(_fmt(e) for e in self.entries) or "MKT",
            _fmt(self.stop_loss),
            ",".join(_fmt(t) for t in self.take_profits),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class ParseResult:
    status: Status
    signal: Signal | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is Status.SIGNAL
