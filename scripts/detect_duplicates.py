from __future__ import annotations

import argparse
import json
import logging

from jobresearcher.analysis.duplicates import (
    review_pending_duplicates,
)
from jobresearcher.db import init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Record duplicate suggestions without deleting or hiding posts."
        )
    )
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    init_db()

    counts = review_pending_duplicates(limit=args.limit)

    print(
        json.dumps(
            counts,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()