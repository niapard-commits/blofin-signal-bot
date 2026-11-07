"""Read-only listener: shows how the parser understands every new post in the signal channel.

NO trading happens here. It is the first, safe stage of the pipeline:

    Telegram channel -> THIS SCRIPT -> parse_signal() -> printed result

Run:   python listen.py          (stop with Ctrl+C)
First run asks for your phone number and the login code from Telegram,
then stores a local session file (never share or commit it).
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
from telethon import TelegramClient, events

from signalbot import Status, parse_signal

load_dotenv()
sys.stdout.reconfigure(errors="replace")  # emojis must never crash the console


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        sys.exit(f"Missing {name} in the .env file")
    return value


async def find_channel(client: TelegramClient, title: str):
    names = []
    async for dialog in client.iter_dialogs():
        if dialog.is_channel:
            names.append(dialog.name)
            if dialog.name.strip().lower() == title.strip().lower():
                return dialog.entity, names
    return None, names


def describe(text: str) -> str:
    result = parse_signal(text)
    if result.status is Status.SIGNAL:
        s = result.signal
        entry = "MARKET" if s.is_market else " - ".join(str(e) for e in s.entries)
        return (
            f"SIGNAL  {s.symbol} {s.side.value}\n"
            f"        entry: {entry}\n"
            f"        stop-loss: {s.stop_loss}\n"
            f"        take-profits: {', '.join(str(t) for t in s.take_profits)}\n"
            f"        leverage: {s.leverage or 'not specified'}\n"
            f"        id: {s.fingerprint}"
        )
    label = "REJECTED (would NOT trade)" if result.status is Status.REJECTED else "ignored"
    return f"{label}: {result.reason}"


async def main() -> None:
    api_id = int(env("TG_API_ID"))
    api_hash = env("TG_API_HASH")
    title = env("TG_CHANNEL")

    async with TelegramClient("listener", api_id, api_hash) as client:
        channel, names = await find_channel(client, title)
        if channel is None:
            print(f'Channel "{title}" not found. Channels on this account: {names}')
            return

        @client.on(events.NewMessage(chats=channel))
        async def on_post(event):
            text = event.message.message or ""
            print("-" * 60)
            print(text if text.strip() else "(no text)")
            print("  =>", describe(text))

        print(f'Listening to "{title}". Post a message there. Press Ctrl+C to stop.')
        await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
