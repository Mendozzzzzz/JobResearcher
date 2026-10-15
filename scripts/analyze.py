from __future__ import annotations

import asyncio
import logging

from jobresearcher.analysis.local import analyze_pending_messages
from jobresearcher.db import init_db


async def main() -> None:
    init_db()

    totals = {
        "selected": 0,
        "done": 0,
        "failed": 0,
        "stale": 0,
    }

    while True:
        counts = await asyncio.to_thread(
            analyze_pending_messages,
            50,
        )

        for key in totals:
            totals[key] += counts[key]

        if counts["selected"] == 0:
            break

        if counts["done"] == 0 and counts["failed"] == 0:
            break

    logging.getLogger(__name__).info(
        "Local analysis complete: %s",
        totals,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())