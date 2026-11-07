"""Signal bot - full pipeline.

    Telegram channel -> parse -> duplicate guard -> validate -> size -> order
                     -> notification from portfolio_otc_bot

Which exchange gets the orders is set in .env:
    EXCHANGE=mock      (default) in-memory simulator, play money
    EXCHANGE=blofin    BloFin; BLOFIN_ENV=demo (default) uses the DEMO server.
                       BLOFIN_ENV=prod (REAL money) additionally needs
                       BLOFIN_ALLOW_REAL=yes, otherwise the bot refuses to start.

Run:   python bot.py          (stop with Ctrl+C)
Bot commands (owner only):  /status  /stop  /flatten  /resume  /help
"""
import asyncio
import json
import logging
import logging.handlers
import os
import random
import sys
import time
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv
from telethon import TelegramClient, events

from signalbot import Signal, Status, parse_signal
from signalbot.blofin import BlofinExchange, PooledTransport
from signalbot import demo
from signalbot.exchange import ExchangeError, MockExchange
from signalbot.executor import ExecutionResult, execute_signal
from signalbot.sizing import SizingConfig
from signalbot.store import SignalStore

load_dotenv()
sys.stdout.reconfigure(errors="replace")  # emojis must never crash the console

log = logging.getLogger("signalbot")


def setup_logging(path: str = "bot.log") -> None:
    """Everything the bot does goes to the console AND to a rotating file (1 MB x 3)."""
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3,
                                                        encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(file_handler)
    log.addHandler(console)


DEDUP_HOURS = 24  # the same setup posted again within this window is treated as a duplicate

HELP = (
    "Команди:\n"
    "/status - стан бота\n"
    "/stop (кнопка 🛑) - сигнали не виконуються, ордери, що ще не відкрились, скасовуються; відкриті позиції лишаються\n"
    "/flatten (кнопка 🔴) - те саме + закрити ВСІ відкриті позиції за ринком\n"
    "/resume - продовжити роботу\n"
    "/alloc N - частка рахунку під маржу однієї угоди, % (без N - показати)\n"
    "/lev N - плече за замовчуванням, якщо в сигналі його немає\n"
    "/show [хв] - (демо) годинний показ: потік різних ситуацій; /show stop - зупинити\n"
    "/demo - (демо) опублікувати в каналі випадковий сигнал за живою ціною\n"
    "/demo btc|eth|sol|xrp - те саме для вибраної монети\n"
    "/demo market [btc|eth|sol|xrp] - сигнал із входом по ринку: позиція відкривається одразу\n"
    "/demo bad - сигнал з помилкою в ціні (бот має відмовитись)\n"
    "/demo dup - той самий сигнал іншими словами (захист від дубля)\n"
    "/help - ця підказка"
)


# Buttons under the chat input (a Telegram reply keyboard). A tap sends the button's text,
# which is mapped to the matching command below.
BUTTONS = {
    "🛑 Скасувати неспрацьовані": "/stop",    # cancel orders that have not opened yet (+ pause)
    "🔴 Закрити ВСЕ": "/flatten",             # also close the positions that are already open
    "📊 Статус": "/status",
    "▶️ Продовжити": "/resume",
}
KEYBOARD = {
    "keyboard": [[{"text": "🛑 Скасувати неспрацьовані"}, {"text": "🔴 Закрити ВСЕ"}],
                 [{"text": "📊 Статус"}, {"text": "▶️ Продовжити"}]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in the .env file")
    return value


class State:
    def __init__(self) -> None:
        self.started = time.time()
        self.paused = False
        self.counts = {"signal": 0, "rejected": 0, "ignored": 0, "duplicate": 0,
                       "executed": 0, "refused": 0}
        self.last_signal = "ще не було"
        self.latencies: list[float] = []   # ms, signal received -> order accepted by the exchange
        self.last_demo: dict | None = None
        self.show_task = None
        # trading settings, changeable at runtime with /alloc and /lev; defaults from .env
        self.sizing = SizingConfig(
            allocation_pct=Decimal(os.getenv("ALLOCATION_PCT", "25")),
            default_leverage=int(os.getenv("DEFAULT_LEVERAGE", "10")),
            max_leverage=int(os.getenv("MAX_LEVERAGE", "20")),
        )


def fmt_signal(s: Signal) -> str:
    entry = "по ринку" if s.is_market else " - ".join(str(e) for e in s.entries)
    leverage = f"{s.leverage}x (з сигналу)" if s.leverage else "не вказано"
    return (
        f"{s.symbol} {s.side.value}\n"
        f"Вхід: {entry}\n"
        f"Стоп-лосс: {s.stop_loss}\n"
        f"Тейк-профіти: {', '.join(str(t) for t in s.take_profits)}\n"
        f"Плече: {leverage}\n"
        f"ID: {s.fingerprint}"
    )


def fmt_order(result: ExecutionResult) -> str:
    plan, req = result.plan, result.request
    entry = "по ринку" if req.order_type == "market" else f"limit {req.price}"
    legs = "\n".join(f"   TP {price}: {size} контр." for price, size in req.take_profits)
    return (
        f"{req.symbol} {req.side.value}, плече {req.leverage}x\n"
        f"Вхід: {entry}\n"
        f"Контрактів: {req.contracts}\n"
        f"Номінал: {plan.notional:.2f} USDT, маржа: {plan.margin:.2f} USDT\n"
        f"Стоп-лосс: {req.stop_loss} (ризик ≈ {plan.risk_at_stop:.2f} USDT)\n"
        f"Тейк-профіти:\n{legs}"
    )


def fmt_pct(value: Decimal) -> str:
    return format(value.normalize(), "f")  # 10 -> "10", never "1E+1"


def build_exchange():
    kind = os.getenv("EXCHANGE", "mock").strip().lower()
    if kind == "mock":
        return MockExchange(balance="1000")  # play money
    if kind != "blofin":
        sys.exit(f'EXCHANGE must be "mock" or "blofin", got "{kind}"')
    env_name = os.getenv("BLOFIN_ENV", "demo").strip().lower() or "demo"
    if env_name == "prod" and os.getenv("BLOFIN_ALLOW_REAL", "").strip().lower() != "yes":
        sys.exit("REAL trading requested (BLOFIN_ENV=prod) but BLOFIN_ALLOW_REAL=yes is not set. Refusing to start.")
    extra = {}
    if os.getenv("BLOFIN_KEEPALIVE", "yes").strip().lower() != "no":
        extra["transport"] = PooledTransport()   # reuse connections: much lower latency
    return BlofinExchange(env("BLOFIN_API_KEY"), env("BLOFIN_API_SECRET"),
                          env("BLOFIN_PASSPHRASE"), env_name, **extra)


def _send_sync(token: str, chat_id: int, text: str, keyboard: dict | None = None) -> None:
    body = {"chat_id": chat_id, "text": text}
    if keyboard:
        body["reply_markup"] = keyboard
    payload = json.dumps(body).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()


async def find_channel(client: TelegramClient, title: str):
    names = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            names.append(dialog.name)
            if dialog.name.strip().lower() == title.strip().lower():
                return dialog.entity, names
    return None, names


async def main() -> None:
    setup_logging()
    api_id = int(env("TG_API_ID"))
    api_hash = env("TG_API_HASH")
    title = env("TG_CHANNEL")
    token = env("BOT_TOKEN")

    user = TelegramClient("listener", api_id, api_hash)  # reads the channel as YOU
    bot = TelegramClient("notifier", api_id, api_hash)   # your bot: commands + replies

    try:
        await user.start()
        await bot.start(bot_token=token)

        me = await user.get_me()
        owner_id = me.id  # the only person the bot talks to

        channel, names = await find_channel(user, title)
        if channel is None:
            log.error(f'Channel "{title}" not found. Channels on this account: {names}')
            return

        state = State()
        store = SignalStore("signals.db")
        exchange = build_exchange()

        def latency_text() -> str:
            if not state.latencies:
                return ""
            avg = sum(state.latencies) / len(state.latencies)
            return (f"Затримка до ордера: остання {state.latencies[-1]:.0f} мс, "
                    f"середня {avg:.0f} мс, найгірша {max(state.latencies):.0f} мс\n")

        async def balance_text() -> str:
            try:
                return f"{await asyncio.to_thread(exchange.get_balance):.2f} USDT"
            except ExchangeError as exc:
                return f"недоступний ({exc})"

        # fail fast: a bot that cannot reach its exchange must not pretend to be running
        try:
            await asyncio.to_thread(exchange.get_balance)
            if hasattr(exchange, "position_mode"):
                await asyncio.to_thread(exchange.position_mode)
        except ExchangeError as exc:
            log.error(f"Cannot start: the exchange check failed: {exc}")
            return

        async def notify(text: str, keyboard: dict | None = None) -> None:
            try:
                await asyncio.to_thread(_send_sync, token, owner_id, text, keyboard)
            except Exception as exc:  # never let a notification crash the bot
                log.warning(f"[!] Could not send a message via the bot: {type(exc).__name__}: {exc}")
                log.warning("    Open your bot in Telegram and press /start, then try again.")

        # Posts are handled strictly one at a time, in the order they arrive.
        # Without this, two quick posts run concurrently: notifications can swap
        # places and (later, with real orders) two copies of one signal could race.
        post_lock = asyncio.Lock()

        handled_ids: set[int] = set()  # a channel post is processed exactly once

        @user.on(events.NewMessage(chats=channel))
        async def on_post(event):
            async with post_lock:
                await process_post(event.message)

        async def process_post(message):
            if message.id in handled_ids:
                return
            handled_ids.add(message.id)
            t0 = time.perf_counter()
            text = message.message or ""
            result = parse_signal(text)
            state.counts[result.status.value] += 1
            log.info("-" * 60)
            log.info(text if text.strip() else "(no text)")
            log.info(f"  => {result.status.value}: {result.reason or result.signal.symbol}")
            store.log(result.status.value, result.reason or fmt_signal(result.signal), text)

            if result.status is Status.SIGNAL:
                state.last_signal = f"{result.signal.symbol} {result.signal.side.value}"
                s = result.signal
                if state.paused:
                    await notify("⏸ Сигнал отримано, але бот на паузі (/resume). Не торгую.\n\n"
                                 + fmt_signal(s))
                elif not store.claim(s.fingerprint, s.symbol, s.side.value,
                                     ttl_seconds=DEDUP_HOURS * 3600):
                    state.counts["duplicate"] += 1
                    state.counts["signal"] -= 1
                    log.info("  => DUPLICATE, skipped")
                    await notify("🔁 Дубль: такий сигнал уже оброблено, пропускаю.\n\n" + fmt_signal(s))
                else:
                    if hasattr(exchange, "drain_timings"):
                        exchange.drain_timings()  # forget requests made by earlier commands
                    try:
                        outcome = await asyncio.to_thread(
                            execute_signal, s, exchange,
                            sizing=state.sizing, signal_time=message.date, now=datetime.now(timezone.utc),
                        )
                    except Exception as exc:  # unknown state -> keep the duplicate guard, do not retry
                        state.counts["refused"] += 1
                        log.error(f"  => INTERNAL ERROR: {type(exc).__name__}: {exc}")
                        await notify(f"⛔ Внутрішня помилка, НЕ торгую: {type(exc).__name__}: {exc}\n\n"
                                     + fmt_signal(s))
                        return
                    store.log("execution", "ok" if outcome.ok else outcome.reason, text)
                    ms = (time.perf_counter() - t0) * 1000
                    if hasattr(exchange, "drain_timings"):
                        requests = exchange.drain_timings()
                        log.info(f"  http: {len(requests)} requests: "
                                 + " | ".join(f"{name} {took:.0f} ms" for name, took in requests))
                    if outcome.ok:
                        state.counts["executed"] += 1
                        state.latencies.append(ms)
                        log.info(f"  => ORDER {outcome.order.order_id} in {ms:.0f} ms")
                        warning = "".join(f"\n⚠️ {n}" for n in outcome.order.notes)
                        await notify(f"✅ Ордер виставлено ({exchange.name})\n\n" + fmt_order(outcome)
                                     + f"\nID ордера: {outcome.order.order_id}"
                                     + f"\n⏱ {ms:.0f} мс від сигналу до ордера (розбір, перевірки, запити до біржі)"
                                     + warning)
                    else:
                        state.counts["refused"] += 1
                        if not outcome.order_attempted:
                            store.release(s.fingerprint)  # nothing was ordered, a re-post may retry
                        log.info(f"  => NOT TRADED: {outcome.reason} ({ms:.0f} ms)")
                        await notify(f"⛔ НЕ торгую: {outcome.reason}\n\n" + fmt_signal(s))
            elif result.status is Status.REJECTED:
                await notify(f"⚠️ Схоже на сигнал, але НЕ торгую: {result.reason}\n\n"
                             + text[:500])

        async def post_to_channel(text: str) -> None:
            """Publish as the channel owner. If the listener does not echo our own post
            within 3 s, process it directly (process_post ignores a message seen twice)."""
            sent = await user.send_message(channel, text)
            await asyncio.sleep(3)
            async with post_lock:
                await process_post(sent)

        async def run_demo(event, args: list[str]) -> None:
            if getattr(exchange, "env", "demo") == "prod":
                await event.respond("/demo вимкнено на реальній біржі.")
                return
            rng = random.Random()
            wanted = next((a.upper() for a in args if a.upper() in demo.SYMBOLS), None)
            try:
                if "dup" in args:
                    if state.last_demo is None:
                        await event.respond("Спочатку надішли /demo, потім /demo dup.")
                        return
                    values = state.last_demo
                    text = demo.render(values, demo.OTHER_STYLE[values["style"]])
                else:
                    symbol, side, style = demo.pick(rng, wanted)
                    market = "market" in args
                    if market:  # single-entry styles only: a market entry has no zone
                        style = rng.choice(("clean", "ukrainian"))
                    last = await asyncio.to_thread(exchange.get_last_price, f"{symbol}-USDT")
                    values = demo.build(symbol, side, last, rng, style, market=market)
                    text = demo.bad_text(values) if "bad" in args else demo.render(values)
                    state.last_demo = values
            except ExchangeError as exc:
                await event.respond(f"Не вдалось отримати ціну: {exc}")
                return
            await event.respond("📤 Публікую в канал:\n\n" + text)
            await post_to_channel(text)

        async def emergency_cancel() -> str:
            """Cancel this bot's unfilled entries (+ their take-profit legs). Positions stay."""
            try:  # serialised with order placement, so an entry cannot slip in mid-cleanup
                async with post_lock:
                    report = await asyncio.to_thread(exchange.cancel_pending)
            except ExchangeError as exc:
                store.log("emergency", f"cancel failed: {exc}")
                return f"⚠️ Не вдалось скасувати відкриті ордери: {exc}. Перевір біржу вручну."
            text = (f"Скасовано непрацюючих вхідних ордерів: {report.orders}, "
                    f"тейків до них: {report.tpsl}. Відкриті позиції не чіпав.")
            if report.failed:
                text += "\n⚠️ Не скасовано:\n" + "\n".join(report.failed)
            store.log("emergency", text)
            log.info(f"EMERGENCY STOP: {text}")
            return text

        async def flatten_all() -> tuple[str, Decimal | None]:
            """Cancel unfilled entries, close every position at market. -> (report text, balance after)."""
            try:
                async with post_lock:
                    before = await asyncio.to_thread(exchange.get_balance)
                    cancelled = await asyncio.to_thread(exchange.cancel_pending)
                    report = await asyncio.to_thread(exchange.close_all_positions)
                    after = await asyncio.to_thread(exchange.get_balance)
            except ExchangeError as exc:
                store.log("flatten", f"failed: {exc}")
                return f"⚠️ Не вдалось закрити позиції: {exc}. Перевір біржу вручну.", None
            lines = [f"Скасовано ордерів, що не спрацювали: {cancelled.orders} (тейків до них: {cancelled.tpsl})."]
            if report.closed:
                lines.append(f"Закрито позицій: {len(report.closed)}")
                for p in report.closed:
                    lines.append(f"   {p.symbol} {p.side.value}, {p.size} контр., "
                                 f"вхід {fmt_pct(p.entry_price)}, PnL ≈ {p.unrealized_pnl:+.2f} USDT")
                total = sum((p.unrealized_pnl for p in report.closed), Decimal(0))
                lines.append(f"Сумарний PnL закритих ≈ {total:+.2f} USDT. "
                             f"Залишки TP/SL прибрано: {report.tpsl}.")
            else:
                lines.append("Відкритих позицій не було.")
            lines.append(f"Баланс: {before:.2f} → {after:.2f} USDT ({after - before:+.2f}, з комісіями біржі)")
            for problem in (*cancelled.failed, *report.failed):
                lines.append(f"⚠️ {problem}")
            text = "\n".join(lines)
            store.log("flatten", text)
            log.info(f"FLATTEN: {text}")
            return text, after

        def demo_prices(sym: str):
            return exchange.get_last_price(f"{sym}-USDT")

        async def scene_stop(rng, pause: float) -> None:
            await notify("🎬 Сцена: аварійна зупинка (/stop). Далі прийде сигнал, його торгувати не можна.")
            state.paused = True
            await notify("🛑 " + await emergency_cancel())
            text, values = await asyncio.to_thread(demo.make_event, "valid_limit", rng, demo_prices)
            await post_to_channel(text)
            await asyncio.sleep(min(pause, 20))
            state.paused = False
            await notify("🎬 Сцена: продовжую роботу (/resume). Той самий сигнал вдруге:")
            await post_to_channel(text)
            state.last_demo = values

        async def scene_margin(rng) -> None:
            saved = state.sizing
            await notify("🎬 Сцена: нестача маржі. Тимчасово ставлю 100% рахунку на угоду: "
                         "вільної маржі вистачить максимум на один сигнал.")
            state.sizing = replace(saved, allocation_pct=Decimal(100))
            try:
                for _ in range(2):
                    text, values = await asyncio.to_thread(demo.make_event, "valid_limit", rng, demo_prices)
                    await post_to_channel(text)
                    await asyncio.sleep(5)
            finally:
                state.sizing = saved
                await notify(f"🎬 Частку на угоду повернуто: {fmt_pct(saved.allocation_pct)}%.")

        async def scene_close(start_balance: Decimal | None, start_counts: dict) -> None:
            await notify("🎬 Фінал: закриваю всі позиції за ринком і підбиваю підсумок.")
            state.paused = True
            text, after = await flatten_all()
            await notify("🏁 " + text)
            state.paused = False
            c = {k: state.counts[k] - start_counts[k] for k in state.counts}
            lines = ["🎬 Підсумок показу:",
                     f"Сигналів: {c['signal']}, ордерів виставлено: {c['executed']}, "
                     f"відмов: {c['refused']}, дублів: {c['duplicate']}, "
                     f"відхилено парсером: {c['rejected']}, проігноровано: {c['ignored']}"]
            if start_balance is not None and after is not None:
                lines.append(f"Баланс: {start_balance:.2f} → {after:.2f} USDT "
                             f"({after - start_balance:+.2f})")
            await notify("\n".join(lines))

        async def run_show(minutes: int) -> None:
            rng = random.Random()
            try:
                start_balance = await asyncio.to_thread(exchange.get_balance)
            except ExchangeError:
                start_balance = None
            start_counts = dict(state.counts)
            if state.paused:  # a show is pointless on pause (e.g. after /flatten)
                state.paused = False
                await notify("▶️ Бот був на паузі, знімаю її на час показу.")
            n = max(4, round(minutes * 0.8))
            program = demo.build_program(n, rng)
            pause = minutes * 60 / n
            await notify(f"🎬 Показ: {n} подій приблизно за {minutes} хв (пауза ~{int(pause)} с). "
                         "Зупинити: /show stop")
            try:
                for i, kind in enumerate(program, 1):
                    log.info(f"[show {i}/{n}] {kind}")
                    try:
                        if kind == "stop_scene":
                            await scene_stop(rng, pause)
                        elif kind == "margin_scene":
                            await scene_margin(rng)
                        elif kind == "dup" and state.last_demo is None:
                            continue
                        else:
                            text, values = await asyncio.to_thread(
                                demo.make_event, kind, rng, demo_prices, state.last_demo)
                            if values:
                                state.last_demo = values
                            await post_to_channel(text)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # one failed event must not end the show
                        log.warning(f"  [show] event {kind} failed: {type(exc).__name__}: {exc}")
                    await asyncio.sleep(pause * rng.uniform(0.8, 1.2))
                try:
                    await scene_close(start_balance, start_counts)
                except Exception as exc:
                    log.warning(f"  [show] final scene failed: {type(exc).__name__}: {exc}")
                await notify("🎬 Показ завершено.")
            except asyncio.CancelledError:
                await notify("🎬 Показ зупинено.")
                raise
            finally:
                state.show_task = None

        async def run_show_command(event, args: list[str]) -> None:
            if getattr(exchange, "env", "demo") == "prod":
                await event.respond("/show вимкнено на реальній біржі.")
            elif args[:1] == ["stop"]:
                if state.show_task is None:
                    await event.respond("Показ не запущено.")
                else:
                    state.show_task.cancel()
                    await event.respond("Зупиняю показ.")
            elif state.show_task is not None:
                await event.respond("Показ уже йде. /show stop - зупинити.")
            else:
                try:
                    minutes = int(args[0]) if args else 60
                except ValueError:
                    await event.respond("Приклад: /show 60")
                    return
                minutes = max(4, min(minutes, 240))
                state.show_task = asyncio.create_task(run_show(minutes))
                await event.respond(f"🎬 Запускаю показ на {minutes} хв.")

        async def set_sizing(event, args: list[str], field: str) -> None:
            cfg = state.sizing
            if not args:
                await event.respond(f"Зараз: {fmt_pct(cfg.allocation_pct)}% рахунку під маржу угоди, "
                                    f"плече за замовчуванням {cfg.default_leverage}x "
                                    f"(максимум {cfg.max_leverage}x).")
                return
            try:
                value = Decimal(args[0].replace(",", "."))
                if field == "alloc":
                    if not Decimal(0) < value <= 100:
                        raise ValueError
                    state.sizing = replace(cfg, allocation_pct=value.normalize())
                else:
                    if value != value.to_integral_value() or not 1 <= value <= cfg.max_leverage:
                        raise ValueError
                    state.sizing = replace(cfg, default_leverage=int(value))
            except (InvalidOperation, ValueError):
                limit = "0 < N <= 100" if field == "alloc" else f"ціле 1..{cfg.max_leverage}"
                await event.respond(f"Не розумію значення. Потрібно: {limit}.")
                return
            cfg = state.sizing
            await event.respond(f"✅ Тепер: {fmt_pct(cfg.allocation_pct)}% рахунку під маржу угоди, "
                                f"плече за замовчуванням {cfg.default_leverage}x.")

        @bot.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_command(event):
            if event.sender_id != owner_id:
                await event.respond("Це приватний бот.")
                return
            raw = (event.raw_text or "").strip()
            words = BUTTONS.get(raw, raw).split()
            command = words[0].lower().split("@")[0] if words else ""
            if command in ("/start", "/help"):
                await notify("Привіт! Я слухаю канал і повідомляю, що розпізнав.\n\n" + HELP, KEYBOARD)
            elif command == "/status":
                minutes = int((time.time() - state.started) / 60)
                c = state.counts
                await event.respond(
                    f"Біржа: {exchange.name}, баланс {await balance_text()}\n"
                    f"Стан: {'ПАУЗА' if state.paused else 'працює'}"
                    f"{', іде показ' if state.show_task else ''}\n"
                    f"Налаштування: {fmt_pct(state.sizing.allocation_pct)}% рахунку під маржу, "
                    f"плече {state.sizing.default_leverage}x (макс {state.sizing.max_leverage}x)\n"
                    f"Працює: {minutes} хв\n"
                    f"{latency_text()}"
                    f"Сигналів: {c['signal']}, ордерів: {c['executed']}, відмов: {c['refused']}\n"
                    f"Дублів: {c['duplicate']}, відхилено парсером: {c['rejected']}, "
                    f"проігноровано: {c['ignored']}\n"
                    f"Останній сигнал: {state.last_signal}"
                )
            elif command == "/alloc":
                await set_sizing(event, words[1:], "alloc")
            elif command == "/lev":
                await set_sizing(event, words[1:], "lev")
            elif command == "/show":
                await run_show_command(event, [w.lower() for w in words[1:]])
            elif command == "/demo":
                await run_demo(event, [w.lower() for w in words[1:]])
            elif command == "/stop":
                state.paused = True
                await event.respond("🛑 Аварійна зупинка: нові сигнали не виконуються. "
                                    "Скасовую вхідні ордери, які ще не спрацювали...")
                await event.respond(await emergency_cancel() + "\n/resume щоб продовжити.")
            elif command == "/flatten":
                state.paused = True
                await event.respond("🛑 Закриваю всі позиції за ринком, скасовую ордери, що не спрацювали. "
                                    "Бот на паузі...")
                text, _ = await flatten_all()
                await event.respond(text + "\n/resume щоб продовжити.")
            elif command == "/resume":
                state.paused = False
                await event.respond("▶️ Продовжую роботу.")
            else:
                await event.respond("Не знаю такої команди.\n\n" + HELP)

        async def keep_connections_warm() -> None:
            """Every few seconds, when no signal is being processed: refresh the connections."""
            while True:
                await asyncio.sleep(4)
                if post_lock.locked():
                    continue
                try:
                    await asyncio.to_thread(exchange.keepalive)
                except Exception:  # a failed ping must never disturb trading
                    pass

        if hasattr(exchange, "keepalive") and isinstance(getattr(exchange, "_transport", None),
                                                         PooledTransport):
            state.warm_task = asyncio.create_task(keep_connections_warm())
            await asyncio.to_thread(exchange.keepalive)               # first connections before the first signal
        log.info(f'Running on {exchange.name}. Channel: "{title}". Ctrl+C to stop.')
        await notify(f"✅ Бот запущено ({exchange.name}, {await balance_text()}). "
                     "Слухаю канал. /help - команди\n\n"
                     "Кнопки внизу: 🛑 скасувати ордери, що ще не відкрились; "
                     "🔴 закрити ВСЕ (і відкриті позиції теж).", KEYBOARD)
        await asyncio.gather(user.run_until_disconnected(), bot.run_until_disconnected())
    finally:
        for client in (user, bot):
            try:
                await client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
