from __future__ import annotations

import argparse
import json

from jobresearcher.db import init_db
from jobresearcher.fetching.urls import normalize_pending_urls


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Normalize recognized URL formats."
    )
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    init_db()

    result = normalize_pending_urls(args.limit)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()