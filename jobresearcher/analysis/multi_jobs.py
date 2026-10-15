from __future__ import annotations

import logging
import re
import unicodedata

from jobresearcher.analysis.postprocessing import (
    load_json,
    load_pending,
    save_result,
)


logger = logging.getLogger(__name__)

STAGE = "multi_job_review"
VERSION = "evidence-v1"

BULLET = re.compile(
    r"^\s*(?:[-•▪◦*]|\d+[.)])\s+(.+?)\s*$"
)


def normalize(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", value or "")
        .casefold()
        .split()
    )


def term_in_text(term: str, text: str) -> bool:
    term = normalize(term)
    text = normalize(text)

    if not term:
        return False

    return re.search(
        rf"(?<!\w){re.escape(term)}(?!\w)",
        text,
    ) is not None


def validated_terms(row: dict) -> list[dict]:
    raw_terms = load_json(row.get("occupation_terms"), [])
    sources = {
        "telegram": row.get("raw_telegram_text") or "",
        "page": row.get("page_text") or "",
    }

    results = []
    seen = set()

    for item in raw_terms:
        if not isinstance(item, dict):
            continue

        term = item.get("term")
        evidence = item.get("evidence")
        source = item.get("source")

        if (
            not isinstance(term, str)
            or not isinstance(evidence, str)
            or not isinstance(source, str)
            or source not in sources
        ):
            continue

        if normalize(evidence) not in normalize(sources[source]):
            continue

        if not term_in_text(term, evidence):
            continue

        key = (normalize(term), source)

        if key in seen:
            continue

        seen.add(key)
        results.append(
            {
                "term": term,
                "evidence": evidence,
                "source": source,
            }
        )

    return results


def find_role_lines(
    text: str,
    terms: list[dict],
    source: str,
) -> list[dict]:
    matching_lines = []
    seen = set()

    for line in (text or "").splitlines():
        match = BULLET.match(line)
        item = match.group(1) if match else line.strip()

        if not item or len(item) > 240:
            continue

        matched_terms = [
            term["term"]
            for term in terms
            if term["source"] == source
            and term_in_text(term["term"], item)
        ]

        if not matched_terms:
            continue

        key = normalize(item)

        if key in seen:
            continue

        seen.add(key)
        matching_lines.append(
            {
                "source": source,
                "text": item,
                "bullet": match is not None,
                "terms": matched_terms,
            }
        )

    return matching_lines[:20]


def detect_multi_job(row: dict) -> dict:
    terms = validated_terms(row)

    telegram_terms = {
        normalize(item["term"])
        for item in terms
        if item["source"] == "telegram"
    }

    page_terms = {
        normalize(item["term"])
        for item in terms
        if item["source"] == "page"
    }

    role_lines = (
        find_role_lines(
            row.get("raw_telegram_text") or "",
            terms,
            "telegram",
        )
        + find_role_lines(
            row.get("page_text") or "",
            terms,
            "page",
        )
    )

    already_marked = bool(row.get("is_multi_job"))

    if already_marked:
        status = "marked_multi"
        reason = (
            "This candidate is already marked as multi-job. "
            "The detector leaves the existing flag unchanged."
        )

    elif len(telegram_terms) >= 2:
        status = "review"
        reason = (
            "Multiple role expressions appear in the Telegram post. "
            "Review whether they refer to separate vacancies rather "
            "than synonyms or mentions of the same role."
        )

    elif len(page_terms) >= 2:
        status = "review_page_only"
        reason = (
            "Multiple role expressions appear on the external page. "
            "The flag is unchanged because this may be a generic "
            "careers page."
        )

    else:
        status = "no_multiple_role_evidence"
        reason = (
            "The extracted evidence does not establish multiple roles. "
            "This does not guarantee that the post contains only one role."
        )

    return {
        "status": status,
        "reason": reason,
        "occupation_terms": terms,
        "role_lines": role_lines,
        "automatic_flag_changed": False,
    }


def review_pending_multi_jobs(limit: int = 25) -> dict[str, int]:
    rows = load_pending(STAGE, VERSION, limit)

    counts = {
        "selected": len(rows),
        "done": 0,
        "review": 0,
        "failed": 0,
        "stale": 0,
    }

    for row in rows:
        try:
            result = detect_multi_job(row)

            if not save_result(row, STAGE, VERSION, result):
                counts["stale"] += 1
                continue

            counts["done"] += 1

            if result["status"] in {"review", "review_page_only"}:
                counts["review"] += 1

        except Exception:
            counts["failed"] += 1
            logger.exception(
                "Multi-job review failed for candidate #%s",
                row["id"],
            )

    return counts