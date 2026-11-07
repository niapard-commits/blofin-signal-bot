"""Tiny SQLite storage: duplicate guard + audit journal.

* ``claim`` is atomic: the first caller for a fingerprint wins, everybody else
  gets False. Entries expire after ``ttl_seconds`` so the same setup posted
  again much later (a genuinely new trade) is allowed.
* ``log`` keeps a permanent journal of everything the bot saw and decided.

The data lives in one local file (default ``signals.db``) and survives restarts,
so a restart can never replay old signals.
"""
from __future__ import annotations

import sqlite3
import time


class SignalStore:
    def __init__(self, path: str = "signals.db") -> None:
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (
                fingerprint TEXT PRIMARY KEY,
                first_seen  REAL NOT NULL,
                symbol      TEXT,
                side        TEXT
            );
            CREATE TABLE IF NOT EXISTS journal (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       REAL NOT NULL,
                status   TEXT NOT NULL,
                detail   TEXT,
                raw_text TEXT
            );
            """
        )

    def claim(
        self,
        fingerprint: str,
        symbol: str,
        side: str,
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> bool:
        """True if this signal is new (and is now recorded), False if it is a duplicate."""
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("DELETE FROM seen WHERE first_seen < ?", (now - ttl_seconds,))
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO seen (fingerprint, first_seen, symbol, side) VALUES (?, ?, ?, ?)",
                (fingerprint, now, symbol, side),
            )
        return cursor.rowcount == 1

    def release(self, fingerprint: str) -> None:
        """Forget a signal so the same post can be retried (only when nothing was ordered)."""
        with self.db:
            self.db.execute("DELETE FROM seen WHERE fingerprint = ?", (fingerprint,))

    def log(self, status: str, detail: str = "", raw_text: str = "", *, now: float | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO journal (ts, status, detail, raw_text) VALUES (?, ?, ?, ?)",
                (time.time() if now is None else now, status, detail, raw_text),
            )

    def journal_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM journal").fetchone()[0]

    def close(self) -> None:
        self.db.close()
