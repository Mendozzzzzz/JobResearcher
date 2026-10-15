from __future__ import annotations

import logging

from jobresearcher.analysis.postprocessing import (
    load_json,
    load_pending,
    save_result,
)


logger = logging.getLogger(__name__)

STAGE = "quality_audit"
VERSION = "quality-v1"


def audit_candidate(row: dict) -> dict:
    findings = []

    def add(code: str, message: str) -> None:
        findings.append({"code": code, "message": message})

    telegram = row.get("raw_telegram_text") or ""
    page = row.get("page_text") or ""
    multi = bool(row.get("is_multi_job"))

    if not telegram.strip() and not page.strip():
        add("EMPTY", "No text is available")

    if not multi:
        if not row.get("title"):
            add("MISSING_TITLE", "Title was not extracted")

        if not row.get("company"):
            add("MISSING_COMPANY", "Employer was not extracted")

    minimum = row.get("salary_min")
    maximum = row.get("salary_max")

    if (
        minimum is not None
        and maximum is not None
        and minimum > maximum
    ):
        add("INVALID_SALARY_RANGE", "Salary minimum exceeds the maximum")

    if minimum is not None or maximum is not None:
        if not row.get("salary_currency"):
            add("SALARY_CURRENCY_UNKNOWN", "Salary currency is unknown")

        if row.get("salary_period") in {None, "", "unknown"}:
            add("SALARY_PERIOD_UNKNOWN", "Salary period is unknown")

    if multi and any(
        row.get(field) is not None
        for field in ("salary_min", "salary_max")
    ):
        add(
            "MULTI_SALARY",
            "Individual salary amounts are present on a multi-job post.",
        )

    meta = load_json(row.get("extraction_meta"), {})

    if meta.get("telegram_truncated") or meta.get("page_truncated"):
        add(
            "TRUNCATED_INPUT",
            "Qwen did not receive the full available text.",
        )

    sources = {
        "telegram": telegram,
        "page": page,
    }

    for field in ("occupation_terms", "skill_terms"):
        terms = load_json(row.get(field), [])

        for item in terms:
            if not isinstance(item, dict):
                add("INVALID_EVIDENCE", f"Invalid item in {field}")
                continue

            source = item.get("source")
            evidence = item.get("evidence")

            if (
                not isinstance(source, str)
                or source not in sources
                or not isinstance(evidence, str)
                or not evidence.strip()
            ):
                add(
                    "INVALID_EVIDENCE",
                    f"Incomplete evidence in {field}",
                )
                continue

            normalized_evidence = " ".join(evidence.split()).casefold()
            normalized_source = " ".join(
                sources[source].split()
            ).casefold()

            if normalized_evidence not in normalized_source:
                add(
                    "EVIDENCE_NOT_FOUND",
                    f"Quote not found in the {source} source.",
                )

    if row.get("analysis_status") == "needs_review":
        add(
            "EXTRACTION_REVIEW",
            "The extraction needs review; this is not an automatic rejection.",
        )

    if row.get("esco_classification_status") == "unavailable":
        add(
            "ESCO_UNAVAILABLE",
            "The catalog was unavailable during normalization.",
        )

    return {
        "status": "review" if findings else "checks_passed",
        "findings": findings,
        "automatic_rejection": False,
        "note": (
            "These are consistency checks, not proof that the vacancy is "
            "authentic or still available."
        ),
    }


def audit_pending_jobs(limit: int = 25) -> dict[str, int]:
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
            result = audit_candidate(row)

            if not save_result(row, STAGE, VERSION, result):
                counts["stale"] += 1
                continue

            counts["done"] += 1

            if result["status"] == "review":
                counts["review"] += 1

        except Exception:
            counts["failed"] += 1
            logger.exception("Audit failed for candidate #%s", row["id"])

    return counts