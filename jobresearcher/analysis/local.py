from __future__ import annotations

import json
import logging

from jobresearcher.analysis.parsers import parse_message
from jobresearcher.db import (
    finish_message_analysis,
    get_conn,
    get_pending_message_analysis,
    upsert_job_candidate,
)

logger = logging.getLogger(__name__)


def build_telegram_url(row) -> str | None:
    chat_id = row["source_chat_id"]

    if chat_id is not None:
        conn = get_conn()

        try:
            source = conn.execute(
                """
                SELECT username
                FROM telegram_sources
                WHERE chat_id = ?
                """,
                (chat_id,),
            ).fetchone()
        finally:
            conn.close()

        if source is not None and source["username"]:
            return (
                f"https://t.me/{source['username']}/"
                f"{row['message_id']}"
            )

        return None

    channel = (row["channel"] or "").strip().lstrip("@")

    if not channel or " " in channel or channel.startswith("tg:"):
        return None

    return f"https://t.me/{channel}/{row['message_id']}"


def build_candidate(row) -> dict:
    text = row["text"] or ""
    parsed = parse_message(text)

    return {
        "message_db_id": row["id"],
        "source_channel": row["channel"],
        "telegram_message_id": row["message_id"],
        "telegram_url": build_telegram_url(row),
        "posted_at": row["date"],
        "source_type": (
            "telegram_plus_url"
            if parsed.primary_url
            else "telegram_only"
        ),
        "raw_telegram_text": text,
        "external_url": parsed.primary_url,
        "fetch_status": (
            "pending" if parsed.primary_url else "not_needed"
        ),
        "title": parsed.title,
        "company": parsed.company,
        "employment_type": parsed.employment_type,
        "seniority": parsed.seniority,
        "location_type": parsed.location_type,
        "location_text": parsed.location_text,
        "eligible_regions": json.dumps(
            parsed.eligible_regions,
            ensure_ascii=False,
        ),
        "timezone_text": parsed.timezone_text,
        "salary_min": parsed.salary_min,
        "salary_max": parsed.salary_max,
        "salary_currency": parsed.salary_currency,
        "salary_period": parsed.salary_period,
        "salary_text": parsed.salary_text,
        "degree_status": parsed.degree_status,
        "skills": json.dumps(parsed.skills, ensure_ascii=False),
        "keywords": json.dumps(parsed.keywords, ensure_ascii=False),
        "application_url": parsed.application_url,
        "contact_email": parsed.emails[0] if parsed.emails else None,
        "contact_telegram": (
            parsed.telegram_handles[0]
            if parsed.telegram_handles
            else None
        ),
        "contact_discord": (
            parsed.discord_urls[0]
            if parsed.discord_urls
            else None
        ),
        "contact_text": parsed.contact_text,
        "application_method": parsed.application_method,
        "rule_score": 0,
        "final_score": 0.0,
        "analysis_status": "rules_complete",
    }


def analyze_pending_messages(limit: int = 50) -> dict[str, int]:
    rows = get_pending_message_analysis(limit=limit)
    counts = {
        "selected": len(rows),
        "done": 0,
        "failed": 0,
        "stale": 0,
    }

    for row in rows:
        message_id = row["id"]
        content_hash = row["content_hash"]

        try:
            candidate = build_candidate(row)

            saved = upsert_job_candidate(
                candidate,
                expected_content_hash=content_hash,
            )

            if not saved:
                counts["stale"] += 1
                continue

            completed = finish_message_analysis(
                message_db_id=message_id,
                content_hash=content_hash,
            )

            if completed:
                counts["done"] += 1
            else:
                counts["stale"] += 1

        except Exception as exc:
            recorded = finish_message_analysis(
                message_db_id=message_id,
                content_hash=content_hash,
                error=f"{type(exc).__name__}: {exc}",
            )

            counts["failed" if recorded else "stale"] += 1
            logger.exception(
                "Local analysis failed for message %s",
                message_id,
            )

    return counts