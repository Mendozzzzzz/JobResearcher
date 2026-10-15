from __future__ import annotations

import hashlib
import json

from jobresearcher.db import get_conn


SNAPSHOT_FIELDS = (
    "id",
    "title",
    "company",
    "posted_at",
    "location_type",
    "location_text",
    "external_url",
    "application_url",
    "raw_telegram_text",
    "page_text",
    "content_type",
    "analysis_status",
    "is_multi_job",
    "occupation_terms",
    "skill_terms",
    "extraction_meta",
    "enrichment_version",
    "enrichment_input_hash",
)


def snapshot_hash(row: dict) -> str:
    payload = {
        field: row.get(field)
        for field in SNAPSHOT_FIELDS
    }

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    return hashlib.sha256(
        serialized.encode("utf-8")
    ).hexdigest()


def load_json(value, default):
    if not isinstance(value, str):
        return default

    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default

    if not isinstance(parsed, type(default)):
        return default

    return parsed


def load_pending(
    stage: str,
    version: str,
    limit: int,
    *,
    all_single_jobs_changed: bool = False,
) -> list[dict]:
    if limit < 1:
        raise ValueError("limit must be positive")

    conn = get_conn()

    try:
        rows = conn.execute(
            """
            SELECT
                jc.*,
                p.version AS processed_version,
                p.input_hash AS processed_hash
            FROM job_candidates jc
            LEFT JOIN candidate_postprocessing p
                ON p.candidate_id = jc.id
               AND p.stage = ?
            WHERE jc.content_type = 'job'
              AND jc.analysis_status IN ('enriched', 'needs_review')
            ORDER BY jc.id ASC
            """,
            (stage,),
        )

        selected = []

        for row in rows:
            item = dict(row)

            if all_single_jobs_changed and item["is_multi_job"]:
                continue

            revision = snapshot_hash(item)

            if (
                item["processed_version"] == version
                and item["processed_hash"] == revision
            ):
                continue

            item["_snapshot_hash"] = revision
            selected.append(item)

            if len(selected) >= limit:
                break

        return selected

    finally:
        conn.close()


def save_result(
    row: dict,
    stage: str,
    version: str,
    result: dict,
) -> bool:
    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        current = conn.execute(
            """
            SELECT jc.*, m.text AS current_message_text
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE jc.id = ?
            """,
            (row["id"],),
        ).fetchone()

        if current is None:
            conn.rollback()
            return False

        current = dict(current)

        if (
            snapshot_hash(current) != row["_snapshot_hash"]
            or (current["current_message_text"] or "")
                != (current["raw_telegram_text"] or "")
        ):
            conn.rollback()
            return False

        conn.execute(
            """
            INSERT INTO candidate_postprocessing (
                candidate_id,
                stage,
                version,
                input_hash,
                result_json
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(candidate_id, stage) DO UPDATE SET
                version = excluded.version,
                input_hash = excluded.input_hash,
                result_json = excluded.result_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                row["id"],
                stage,
                version,
                row["_snapshot_hash"],
                json.dumps(result, ensure_ascii=False),
            ),
        )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()