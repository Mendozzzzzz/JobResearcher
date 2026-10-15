from __future__ import annotations

import re
from urllib.parse import urlsplit

from jobresearcher.db import get_conn


MARKDOWN_LINK = re.compile(
    r"^\[[^\]]*\]\((https?://.+)\)$",
    re.IGNORECASE,
)

ANGLE_LINK = re.compile(
    r"^<(https?://.+)>$",
    re.IGNORECASE,
)


def normalize_url(value: str | None) -> str | None:
    if value is None:
        return None

    original = value
    candidate = value.strip()

    match = (
        MARKDOWN_LINK.fullmatch(candidate)
        or ANGLE_LINK.fullmatch(candidate)
    )

    if match:
        candidate = match.group(1).strip()

    if (
        not candidate
        or any(char.isspace() for char in candidate)
        or any(ord(char) < 32 for char in candidate)
    ):
        return original

    try:
        parsed = urlsplit(candidate)

        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return original

        # Also validate the port syntax.
        _ = parsed.port

    except ValueError:
        return original

    return candidate


def normalize_pending_urls(limit: int = 25) -> dict[str, int]:
    if limit < 1:
        raise ValueError("limit must be positive")

    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            SELECT id, external_url, application_url
            FROM job_candidates
            ORDER BY id ASC
            """
        )

        changes = []

        for row in cursor:
            external = normalize_url(row["external_url"])
            application = normalize_url(row["application_url"])

            if (
                external != row["external_url"]
                or application != row["application_url"]
            ):
                changes.append(
                    (
                        row["id"],
                        row["external_url"],
                        row["application_url"],
                        external,
                        application,
                    )
                )

            if len(changes) >= limit:
                break

        counts = {
            "selected": len(changes),
            "done": 0,
            "failed": 0,
            "stale": 0,
        }

        for record_id, old_external, old_application, external, application in changes:
            updated = conn.execute(
                """
                UPDATE job_candidates
                SET
                    external_url = ?,
                    application_url = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND external_url IS ?
                  AND application_url IS ?
                """,
                (
                    external,
                    application,
                    record_id,
                    old_external,
                    old_application,
                ),
            )

            if updated.rowcount:
                counts["done"] += 1
            else:
                counts["stale"] += 1

        conn.commit()
        return counts

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()