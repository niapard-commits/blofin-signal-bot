"""One controlled DEMO order through the full pipeline.

    python demo_order.py

Builds a BTC-USDT LONG limit order 3% BELOW the live price (so it will not fill
right away) with a stop-loss and two take-profits, shows it, and only sends it
after you type YES. Refuses to run unless BLOFIN_ENV=demo.
"""
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

from signalbot import parse_signal
from signalbot.blofin import BlofinExchange
from signalbot.exchange import ExchangeError
from signalbot.executor import execute_signal

load_dotenv()
sys.stdout.reconfigure(errors="replace")


def need(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in the .env file")
    return value


def main() -> None:
    if os.getenv("BLOFIN_ENV", "demo").strip() != "demo":
        sys.exit("STOP: this script only runs with BLOFIN_ENV=demo")
    exchange = BlofinExchange(need("BLOFIN_API_KEY"), need("BLOFIN_API_SECRET"),
                              need("BLOFIN_PASSPHRASE"), "demo")
    price = exchange.get_last_price("BTC-USDT")
    entry = int(price * 97 / 100)
    text = (f"BTCUSDT LONG\nEntry: {entry}\nSL: {int(entry * 0.97)}\n"
            f"TP1: {int(entry * 1.05)}\nTP2: {int(entry * 1.08)}\nLeverage: 10x")
    print(f"Біржа: {exchange.name}, ціна BTC-USDT зараз {price}\n\nСигнал:\n{text}\n")
    parsed = parse_signal(text)
    if not parsed.ok:
        sys.exit(f"Parser refused: {parsed.reason}")

    dry = execute_signal(parsed.signal, exchange, signal_time=datetime.now(timezone.utc),
                         now=datetime.now(timezone.utc), dry_run=True)
    if not dry.ok:
        sys.exit(f"Not traded: {dry.reason}")
    req = dry.request
    print(f"Буде надіслано: {req.order_type} {req.side.value} {req.contracts} контр., "
          f"плече {req.leverage}x, ціна {req.price}, SL {req.stop_loss}")
    for tp_price, tp_size in req.take_profits:
        print(f"   TP {tp_price}: {tp_size} контр.")
    if input("\nНадіслати на ДЕМО-біржу? Введи YES: ").strip() != "YES":
        sys.exit("Скасовано, нічого не надіслано.")

    result = execute_signal(parsed.signal, exchange, signal_time=datetime.now(timezone.utc),
                            now=datetime.now(timezone.utc))
    if result.ok:
        print(f"\nOK  Ордер прийнято. ID: {result.order.order_id}, статус: {result.order.status}")
        for note in result.order.notes:
            print(f"УВАГА: {note}")
        if not result.order.notes:
            print("Стоп і всі тейки прийняті біржею.")
    else:
        print(f"\nFAIL {result.reason}")
        if result.order_attempted:
            print("Ордер міг дійти до біржі: перевір відкриті ордери в BloFin Demo.")


if __name__ == "__main__":
    try:
        main()
    except ExchangeError as exc:
        sys.exit(f"Помилка біржі: {exc}")
