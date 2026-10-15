from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobresearcher.analysis.postprocessing import (
    load_json,
    save_result,
    snapshot_hash,
)
from jobresearcher.db import get_conn


logger = logging.getLogger(__name__)

STAGE = "duplicate_review"
VERSION = "suggestions-v1"

UNKNOWN = {"", "?", "unknown", "null", "none"}

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "msclkid",
}

MARKDOWN_LINK = re.compile(
    r"^\[[^\]]*\]\((https?://[^\s]+)\)$",
    re.IGNORECASE,
)


def normalize(value: str | None) -> str:
    value = " ".join(
        unicodedata.normalize("NFKC", value or "")
        .casefold()
        .split()
    )
    return "" if value in UNKNOWN else value


def text_fingerprint(value: str | None) -> str:
    text = " ".join(
        unicodedata.normalize("NFKC", value or "").split()
    )

    if len(text) < 100:
        return ""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalized_url(value: str | None) -> str:
    value = (value or "").strip()
    markdown = MARKDOWN_LINK.fullmatch(value)

    if markdown:
        value = markdown.group(1)

    try:
        parsed = urlsplit(value)

        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return ""

        host = parsed.hostname.casefold()
        port = parsed.port

        if ":" in host:
            host = f"[{host}]"

        default_port = (
            parsed.scheme.casefold() == "http" and port == 80
        ) or (
            parsed.scheme.casefold() == "https" and port == 443
        )

        if port is not None and not default_port:
            host += f":{port}"

        query = [
            (key, value)
            for key, value in parse_qsl(
                parsed.query,
                keep_blank_values=True,
            )
            if not key.casefold().startswith("utm_")
            and key.casefold() not in TRACKING_PARAMETERS
        ]

        return urlunsplit(
            (
                parsed.scheme.casefold(),
                host,
                parsed.path or "/",
                urlencode(query),
                "",
            )
        )

    except ValueError:
        return ""


def review_pending_duplicates(limit: int = 25) -> dict[str, int]:
    if limit < 1:
        raise ValueError("limit must be positive")

    conn = get_conn()

    try:
        raw_rows = conn.execute(
            """
            SELECT
                jc.*,
                p.version AS processed_version,
                p.input_hash AS processed_hash,
                p.result_json AS processed_result
            FROM job_candidates jc
            LEFT JOIN candidate_postprocessing p
                ON p.candidate_id = jc.id
               AND p.stage = ?
            WHERE jc.content_type = 'job'
              AND jc.analysis_status IN ('enriched', 'needs_review')
              AND COALESCE(jc.is_multi_job, 0) = 0
            ORDER BY jc.id ASC
            """,
            (STAGE,),
        ).fetchall()
    finally:
        conn.close()

    rows = [dict(row) for row in raw_rows]

    corpus_digest = hashlib.sha256()

    for row in rows:
        row["_snapshot_hash"] = snapshot_hash(row)
        corpus_digest.update(
            f"{row['id']}:{row['_snapshot_hash']}\n".encode("utf-8")
        )

    corpus_token = corpus_digest.hexdigest()

    text_index = defaultdict(set)
    identity_index = defaultdict(set)
    url_index = defaultdict(set)

    for row in rows:
        fingerprint = text_fingerprint(row["raw_telegram_text"])

        if fingerprint:
            text_index[fingerprint].add(row["id"])

        company = normalize(row["company"])
        title = normalize(row["title"])

        if company and title:
            identity_index[(company, title)].add(row["id"])

        for value in (row["external_url"], row["application_url"]):
            key = normalized_url(value)

            if key:
                url_index[key].add(row["id"])

    selected = []

    for row in rows:
        previous = load_json(row.get("processed_result"), {})

        if (
            row["processed_version"] == VERSION
            and row["processed_hash"] == row["_snapshot_hash"]
            and previous.get("corpus_token") == corpus_token
        ):
            continue

        selected.append(row)

        if len(selected) >= limit:
            break

    counts = {
        "selected": len(selected),
        "done": 0,
        "review": 0,
        "failed": 0,
        "stale": 0,
    }

    for row in selected:
        try:
            peers = defaultdict(set)
            record_id = row["id"]

            fingerprint = text_fingerprint(row["raw_telegram_text"])

            if fingerprint:
                for peer in text_index[fingerprint] - {record_id}:
                    peers[peer].add("same_normalized_telegram_text")

            company = normalize(row["company"])
            title = normalize(row["title"])

            if company and title:
                for peer in identity_index[(company, title)] - {record_id}:
                    peers[peer].add("same_company_and_title")

            for value in (row["external_url"], row["application_url"]):
                key = normalized_url(value)

                if key:
                    for peer in url_index[key] - {record_id}:
                        peers[peer].add("same_normalized_url")

            matches = [
                {
                    "candidate_id": peer,
                    "signals": sorted(signals),
                    "decision": "review",
                }
                for peer, signals in sorted(peers.items())
            ]

            result = {
                "status": "review" if matches else "no_matches",
                "corpus_token": corpus_token,
                "matches": matches,
                "duplicate_of_changed": False,
                "note": (
                    "Shared URLs and matching titles can belong to different "
                    "vacancies; records are never merged automatically."
                ),
            }

            if not save_result(row, STAGE, VERSION, result):
                counts["stale"] += 1
                continue

            counts["done"] += 1

            if matches:
                counts["review"] += 1

        except Exception:
            counts["failed"] += 1
            logger.exception(
                "Duplicate review failed for candidate #%s",
                row["id"],
            )

    return counts