from __future__ import annotations

import asyncio

from telethon import TelegramClient

from jobresearcher.config import settings


async def login() -> None:
    if not settings.API_ID or not settings.API_HASH:
        raise RuntimeError(
            "Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env"
        )

    client = TelegramClient(
        settings.SESSION_NAME,
        settings.API_ID,
        settings.API_HASH,
    )

    try:
        await client.start(phone=settings.PHONE or None)
        me = await client.get_me()

        if me is None:
            raise RuntimeError("Telegram authentication did not complete")

        print("Telegram authentication complete.")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(login())