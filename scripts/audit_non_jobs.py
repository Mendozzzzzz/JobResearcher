from __future__ import annotations

import argparse
import json
import logging

from jobresearcher.analysis.audit import (
    audit_candidate,
    audit_pending_jobs,
)
from jobresearcher.db import get_conn, init_db


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run consistency checks on job candidates."
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

        result = audit_candidate(dict(row))

    else:
        result = audit_pending_jobs(args.limit)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()