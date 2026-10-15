from __future__ import annotations

import argparse
import json
import logging

from jobresearcher.analysis.multi_jobs import (
    detect_multi_job,
    review_pending_multi_jobs,
)
from jobresearcher.db import get_conn, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Controllo multi-job basato sulle evidenze estratte."
    )
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--candidate-id", type=int)
    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be positive")

    init_db()

    if args.candidate_id is not None:
        conn = get_conn()

        try:
            row = conn.execute(
                "SELECT * FROM job_candidates WHERE id = ?",
                (args.candidate_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            parser.error("Candidate not found")

        print(
            json.dumps(
                detect_multi_job(dict(row)),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    print(
        json.dumps(
            review_pending_multi_jobs(args.limit),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()