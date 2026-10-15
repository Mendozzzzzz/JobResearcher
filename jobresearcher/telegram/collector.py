from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events, utils

from jobresearcher.config import settings
from jobresearcher.db import (
    get_telegram_sources,
    init_db,
    save_telegram_message,
    update_telegram_source_cursor,
    upsert_telegram_source,
)

logger = logging.getLogger(__name__)


class TelegramCollector:
    def __init__(
        self,
        initial_history_limit: int = 200,
        sync_interval: float = 60,
    ) -> None:
        if initial_history_limit < 1 or sync_interval <= 0:
            raise ValueError("Invalid sync limits")

        self.initial_history_limit = initial_history_limit
        self.sync_interval = sync_interval
        self.sync_task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()

        self.client = TelegramClient(
            settings.SESSION_NAME,
            settings.API_ID,
            settings.API_HASH,
        )

        self.client.add_event_handler(
            self._on_message,
            events.NewMessage(),
        )
        self.client.add_event_handler(
            self._on_message,
            events.MessageEdited(),
        )

    async def start(self) -> None:
        init_db()

        try:
            await self.client.connect()

            if not await self.client.is_user_authorized():
                raise RuntimeError(
                    "Telegram session is not authenticated. "
                    "Run python -m scripts.login once from the terminal."
                )

            await self.discover_sources()

            self.sync_task = asyncio.create_task(
                self._sync_loop(),
                name="telegram-source-sync",
            )

        except BaseException:
            await self.client.disconnect()
            raise

    async def stop(self) -> None:
        task = self.sync_task
        self.sync_task = None

        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        if self.client.is_connected():
            await self.client.disconnect()

    async def discover_sources(self) -> None:
        configured = {
            str(value).strip().lstrip("@").casefold()
            for value in settings.CHANNELS
        }

        configured_ids = set()

        for reference in settings.CHANNELS:
            try:
                entity = await self.client.get_entity(reference)
                configured_ids.add(utils.get_peer_id(entity))
            except Exception:
                logger.exception(
                    "Could not resolve configured Telegram source %r",
                    reference,
                )

        async for dialog in self.client.iter_dialogs():
            if not (dialog.is_group or dialog.is_channel):
                continue

            entity = dialog.entity
            chat_id = dialog.id
            username = getattr(entity, "username", None)

            enabled = (
                chat_id in configured_ids
                or (
                    username is not None
                    and username.casefold() in configured
                )
            )

            upsert_telegram_source(
                chat_id=chat_id,
                title=dialog.name or str(chat_id),
                username=username,
                source_kind="group" if dialog.is_group else "channel",
                enabled=enabled,
            )

    async def _save(self, source, message) -> bool:
        username = source["username"]

        return save_telegram_message(
            chat_id=source["chat_id"],
            channel=f"tg:{source['chat_id']}",
            message_id=message.id,
            text=message.message or "",
            date=message.date,
            legacy_channel=username,
        )

    async def _on_message(self, event) -> None:
        if event.chat_id is None:
            return

        sources = {
            row["chat_id"]: row
            for row in get_telegram_sources(enabled_only=True)
        }

        source = sources.get(event.chat_id)

        if source is None:
            return

        try:
            changed = await self._save(source, event.message)

            if changed:
                logger.info(
                    "Telegram message queued: chat=%s message=%s",
                    event.chat_id,
                    event.message.id,
                )

        except Exception:
            logger.exception(
                "Failed to save Telegram message: chat=%s message=%s",
                event.chat_id,
                event.message.id,
            )

    async def sync_enabled_sources(self) -> None:
        async with self._sync_lock:
            sources = get_telegram_sources(enabled_only=True)

            for source in sources:
                try:
                    await self._sync_source(source)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "Failed to sync Telegram source %s",
                        source["chat_id"],
                    )

    async def _sync_source(self, source) -> None:
        reference = source["username"] or source["chat_id"]
        entity = await self.client.get_entity(reference)

        cursor = source["last_message_id"]

        if cursor == 0:
            messages = await self.client.get_messages(
                entity,
                limit=self.initial_history_limit,
            )

            for message in reversed(messages):
                await self._save(source, message)

                update_telegram_source_cursor(
                    source["chat_id"],
                    message.id,
                )

                await asyncio.sleep(0)

        else:
            async for message in self.client.iter_messages(
                entity,
                min_id=cursor,
                reverse=True,
                limit=None,
            ):
                await self._save(source, message)

                update_telegram_source_cursor(
                    source["chat_id"],
                    message.id,
                )

                await asyncio.sleep(0)

    async def _sync_loop(self) -> None:
        while True:
            try:
                await self.sync_enabled_sources()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Telegram sync cycle failed")

            await asyncio.sleep(self.sync_interval)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    collector = TelegramCollector()

    try:
        await collector.client.start(phone=settings.PHONE)
        await collector.start()
        await collector.client.run_until_disconnected()
    finally:
        await collector.stop()


if __name__ == "__main__":
    asyncio.run(main())