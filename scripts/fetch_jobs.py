from __future__ import annotations

import argparse
import asyncio
import logging

from jobresearcher.db import init_db
from jobresearcher.fetching.service import fetch_pending_jobs


async def main(
    limit: int,
    browser_enabled: bool = False,
    include_retries: bool = True,
) -> None:
    init_db()

    counts = await fetch_pending_jobs(
        limit=limit,
        browser_enabled=browser_enabled,
        include_retries=include_retries,
    )

    logging.getLogger(__name__).info(
        "Fetch batch complete: %s",
        counts,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Process a batch from the page-fetch queue."
    )

    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--browser",
        action="store_true",
        help="Enable browser fallback for configured hosts.",
    )
    parser.add_argument(
        "--no-retries",
        action="store_true",
        help="Process pending downloads only; skip retries.",
    )

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    return args


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    args = parse_args()

    asyncio.run(
        main(
            limit=args.limit,
            browser_enabled=args.browser,
            include_retries=not args.no_retries,
        )
    )