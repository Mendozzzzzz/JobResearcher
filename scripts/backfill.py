from __future__ import annotations

import argparse
import asyncio
import logging

from jobresearcher.db import init_db
from jobresearcher.telegram.collector import TelegramCollector


async def backfill(limit: int) -> None:
    init_db()

    collector = TelegramCollector(
        initial_history_limit=limit,
    )

    try:
        await collector.client.connect()

        if not await collector.client.is_user_authorized():
            raise RuntimeError(
                "Telegram session is not authenticated. "
                "Run python -m scripts.login first."
            )

        await collector.discover_sources()
        await collector.sync_enabled_sources()

        logging.getLogger(__name__).info(
            "Incremental sync complete"
        )

    finally:
        await collector.stop()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sync enabled Telegram sources using the shared collector."
        )
    )
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    asyncio.run(backfill(args.limit))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()