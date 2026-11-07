"""Read-only BloFin check. Places NO orders, changes NOTHING.

    python blofin_check.py

Prints: mode (demo/real), position mode, balance, BTC-USDT contract specs and
the last price. Secrets are never printed.
"""
import os
import sys

from dotenv import load_dotenv

from signalbot.blofin import BlofinExchange
from signalbot.exchange import ExchangeError

load_dotenv()
sys.stdout.reconfigure(errors="replace")


def need(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in the .env file")
    return value


def main() -> None:
    exchange = BlofinExchange(
        need("BLOFIN_API_KEY"), need("BLOFIN_API_SECRET"), need("BLOFIN_PASSPHRASE"),
        os.getenv("BLOFIN_ENV", "demo").strip() or "demo",
    )
    print(f"Біржа: {exchange.name}")
    steps = [
        ("Режим позицій", lambda: exchange.position_mode()),
        ("Баланс USDT", lambda: exchange.get_balance()),
        ("Вільна маржа", lambda: exchange.get_available()),
        ("Контракт BTC-USDT", lambda: exchange.get_instrument("BTC-USDT")),
        ("Ціна BTC-USDT", lambda: exchange.get_last_price("BTC-USDT")),
    ]
    failed = False
    for title, action in steps:
        try:
            print(f"OK   {title}: {action()}")
        except ExchangeError as exc:
            failed = True
            print(f"FAIL {title}: {exc}")
    print("\nГотово. Ордери не виставлялись." if not failed else "\nЄ помилки, див. вище.")


if __name__ == "__main__":
    main()
