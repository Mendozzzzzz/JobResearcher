from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
from dataclasses import asdict

from jobresearcher.analysis.qwen import extract_job_fields
from jobresearcher.db import get_conn, init_db
from jobresearcher.taxonomy.esco import EscoCatalog


logger = logging.getLogger(__name__)

ENRICHMENT_VERSION = "esco-evidence-v2"
MAX_ATTEMPTS = 3

CONTENT_TYPES = {
    "job",
    "article",
    "event",
    "company_page",
    "unknown",
}

ENUM_FIELDS = (
    "seniority",
    "employment_type",
    "location_type",
    "degree_status",
    "salary_period",
)

_enrichment_lock = asyncio.Lock()


class StaleEnrichmentError(RuntimeError):
    pass


def ensure_schema() -> None:
    init_db()


def input_hash(telegram_text: str, page_text: str) -> str:
    payload = json.dumps(
        [telegram_text, page_text],
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_candidate(candidate_id: int) -> dict:
    conn = get_conn()

    try:
        row = conn.execute(
            """
            SELECT
                jc.*,
                m.text AS current_message_text
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE jc.id = ?
            """,
            (candidate_id,),
        ).fetchone()

        if row is None:
            raise ValueError(
                f"Candidate not found: {candidate_id}"
            )

        return dict(row)

    finally:
        conn.close()


def verify_snapshot(
    current: dict,
    expected_hash: str,
    expected_url: str | None,
) -> bool:
    telegram_text = current.get("raw_telegram_text") or ""
    page_text = current.get("page_text") or ""
    message_text = current.get("current_message_text") or ""

    return (
        message_text == telegram_text
        and input_hash(telegram_text, page_text) == expected_hash
        and current.get("external_url") == expected_url
    )


def build_esco_candidates(extraction: dict) -> dict:
   
    result = {
        "status": "pending",
        "catalog_version": None,
        "occupations": [],
        "skills": [],
        "error": None,
    }

    if extraction.get("content_type") != "job":
        result["status"] = "not_applicable"
        return result

    try:
        catalog = EscoCatalog()
        result["catalog_version"] = catalog.metadata().get("version")

        search_languages = ("it", "en")
        cache = {}

        for field, kind, destination in (
            ("occupation_terms", "occupation", "occupations"),
            ("skill_terms", "skill", "skills"),
        ):
            for item in extraction.get(field, []):
                term = item["term"]
                key = (kind, term.casefold(), search_languages)

                if key not in cache:
                    matches = catalog.search(
                        term,
                        kind=kind,
                        languages=search_languages,
                        limit=20,
                        allow_prefix=False,
                    )

                  
                    exact_matches = [
                        match
                        for match in matches
                        if match.match_type == "exact"
                    ]

                    cache[key] = [
                        asdict(match)
                        for match in exact_matches[:5]
                    ]

                accepted = cache[key]

                result[destination].append(
                    {
                        "term": term,
                        "evidence": item["evidence"],
                        "source": item["source"],
                        "search_languages": list(search_languages),
                        "mapping_status": (
                            "exact_candidates"
                            if accepted
                            else "unmapped"
                        ),
                        "matches": accepted,
                        "selection_status": "not_selected",
                    }
                )

        entries = [
            item
            for destination in ("occupations", "skills")
            for item in result[destination]
        ]

        mapped_count = sum(
            bool(item["matches"])
            for item in entries
        )

        if not entries:
            result["status"] = "no_terms"

        elif mapped_count == 0:
            result["status"] = "unmapped"

        elif mapped_count < len(entries):
            result["status"] = "partial_candidates"

        else:
            result["status"] = "candidates_ready"

    except Exception as exc:
        result["status"] = "unavailable"
        result["error"] = f"{type(exc).__name__}: {exc}"
        logger.exception("ESCO candidate lookup failed")

    return result

def prepare_result(
    current: dict,
    extraction: dict,
    taxonomy: dict,
) -> dict:
    result = dict(current)

    content_type = extraction.get("content_type", "unknown")

    if content_type not in CONTENT_TYPES:
        content_type = "unknown"

    is_job = content_type == "job"
    extracted_multi = extraction.get("is_multi_job") is True

    is_multi = bool(current.get("is_multi_job")) or extracted_multi

    result["content_type"] = content_type
    result["is_multi_job"] = int(is_multi)

    for field in ("title", "company", "location_text"):
        result[field] = (
            extraction.get(field)
            if is_job and not is_multi
            else None
        )

    for field in ENUM_FIELDS:
        result[field] = (
            extraction.get(field) or "unknown"
            if is_job and not is_multi
            else "unknown"
        )

    for field in (
        "salary_min",
        "salary_max",
        "salary_currency",
    ):
        result[field] = (
            extraction.get(field)
            if is_job and not is_multi
            else None
        )

    result["skills"] = json.dumps(
        extraction.get("skills", [])
        if is_job and not is_multi
        else [],
        ensure_ascii=False,
    )

    result["occupation_terms"] = json.dumps(
        extraction.get("occupation_terms", []) if is_job else [],
        ensure_ascii=False,
    )

    result["skill_terms"] = json.dumps(
        extraction.get("skill_terms", []) if is_job else [],
        ensure_ascii=False,
    )

    result["esco_candidates"] = json.dumps(
        taxonomy,
        ensure_ascii=False,
    )

    result["esco_classification_status"] = taxonomy["status"]

    result["extraction_meta"] = json.dumps(
        extraction.get("extraction_meta", {}),
        ensure_ascii=False,
    )

    if content_type in {"article", "event", "company_page"}:
        result["analysis_status"] = "rejected_non_job"

    elif content_type == "unknown" or is_multi:
        result["analysis_status"] = "needs_review"

    elif result["title"] and result["company"]:
        result["analysis_status"] = "enriched"

    else:
        result["analysis_status"] = "needs_review"

    return result


def save_result(
    candidate_id: int,
    result: dict,
    expected_hash: str,
    expected_url: str | None,
) -> bool:
    fields = (
        "title",
        "company",
        "seniority",
        "employment_type",
        "location_type",
        "location_text",
        "degree_status",
        "salary_min",
        "salary_max",
        "salary_currency",
        "salary_period",
        "skills",
        "content_type",
        "analysis_status",
        "is_multi_job",
        "occupation_terms",
        "skill_terms",
        "esco_candidates",
        "esco_classification_status",
        "extraction_meta",
    )

    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT
                jc.*,
                m.text AS current_message_text
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE jc.id = ?
            """,
            (candidate_id,),
        ).fetchone()

        if row is None or not verify_snapshot(
            dict(row),
            expected_hash,
            expected_url,
        ):
            conn.rollback()
            return False

        if bool(row["is_multi_job"]) != bool(result["is_multi_job"]):
            if row["is_multi_job"]:
                conn.rollback()
                return False

        assignments = ", ".join(
            f"{field} = ?" for field in fields
        )

        conn.execute(
            f"""
            UPDATE job_candidates
            SET
                {assignments},
                enrichment_version = ?,
                enrichment_input_hash = ?,
                enrichment_attempt_version = ?,
                enrichment_attempt_hash = ?,
                enrichment_attempts = 0,
                enrichment_last_error = NULL,
                enrichment_next_attempt_at = NULL,
                enrichment_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                *(result[field] for field in fields),
                ENRICHMENT_VERSION,
                expected_hash,
                ENRICHMENT_VERSION,
                expected_hash,
                candidate_id,
            ),
        )

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def record_failure(
    candidate_id: int,
    expected_hash: str,
    expected_url: str | None,
    error: str,
) -> None:
    conn = get_conn()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT
                jc.*,
                m.text AS current_message_text
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE jc.id = ?
            """,
            (candidate_id,),
        ).fetchone()

        if row is None or not verify_snapshot(
            dict(row),
            expected_hash,
            expected_url,
        ):
            conn.rollback()
            return

        same_attempt = (
            row["enrichment_attempt_hash"] == expected_hash
            and row["enrichment_attempt_version"] == ENRICHMENT_VERSION
        )

        attempts = (
            row["enrichment_attempts"] + 1
            if same_attempt
            else 1
        )

        delay = min(300 * (2 ** (attempts - 1)), 3600)

        conn.execute(
            """
            UPDATE job_candidates
            SET
                enrichment_attempt_hash = ?,
                enrichment_attempt_version = ?,
                enrichment_attempts = ?,
                enrichment_last_error = ?,
                enrichment_next_attempt_at = datetime('now', ?),
                enrichment_updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (
                expected_hash,
                ENRICHMENT_VERSION,
                attempts,
                error[:2000],
                f"+{delay} seconds",
                candidate_id,
            ),
        )

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


async def enrich_candidate(candidate_id: int) -> dict:
    async with _enrichment_lock:
        ensure_schema()

        current = load_candidate(candidate_id)

        telegram_text = current.get("raw_telegram_text") or ""
        page_text = current.get("page_text") or ""
        expected_url = current.get("external_url")
        expected_hash = input_hash(telegram_text, page_text)

        if (current.get("current_message_text") or "") != telegram_text:
            raise StaleEnrichmentError(
                "The message must be reanalyzed locally"
            )

        try:
            if not telegram_text.strip() and not page_text.strip():
                raise ValueError("No text is available")

            extraction = await extract_job_fields(
                telegram_text,
                page_text,
            )

            taxonomy = await asyncio.to_thread(
                build_esco_candidates,
                extraction,
            )

            result = prepare_result(
                current,
                extraction,
                taxonomy,
            )

            saved = save_result(
                candidate_id,
                result,
                expected_hash,
                expected_url,
            )

            if not saved:
                raise StaleEnrichmentError(
                    "Data changed during extraction; "
                    "the result was discarded"
                )

            return result

        except StaleEnrichmentError:
            raise

        except Exception as exc:
            record_failure(
                candidate_id,
                expected_hash,
                expected_url,
                f"{type(exc).__name__}: {exc}",
            )
            raise


def load_pending_enrichment(
    limit: int = 10,
    force: bool = False,
) -> list[int]:
    if limit < 1:
        raise ValueError("limit must be positive")

    conn = get_conn()

    try:
        cursor = conn.execute(
            """
            SELECT
                jc.*,
                m.text AS current_message_text,
                CASE
                    WHEN enrichment_next_attempt_at IS NULL
                      OR datetime(enrichment_next_attempt_at)
                         <= datetime('now')
                    THEN 1
                    ELSE 0
                END AS retry_due
            FROM job_candidates jc
            JOIN messages m ON m.id = jc.message_db_id
            WHERE COALESCE(jc.analysis_status, 'pending') != 'pending'
              AND (
                  COALESCE(jc.fetch_status, 'not_needed') != 'pending'
                  OR jc.external_url IS NULL
                  OR LENGTH(TRIM(jc.external_url)) = 0
              )
            ORDER BY jc.id ASC
            """
        )

        selected = []

        for row in cursor:
            telegram_text = row["raw_telegram_text"] or ""
            page_text = row["page_text"] or ""

            if (row["current_message_text"] or "") != telegram_text:
                continue

            revision = input_hash(telegram_text, page_text)

            already_done = (
                row["enrichment_version"] == ENRICHMENT_VERSION
                and row["enrichment_input_hash"] == revision
            )

            same_attempt = (
                row["enrichment_attempt_version"] == ENRICHMENT_VERSION
                and row["enrichment_attempt_hash"] == revision
            )

            if not force:
                if already_done:
                    continue

                if same_attempt and (
                    row["enrichment_attempts"] >= MAX_ATTEMPTS
                    or not row["retry_due"]
                ):
                    continue

            selected.append(row["id"])

            if len(selected) >= limit:
                break

        return selected

    finally:
        conn.close()


async def enrich_pending(
    limit: int = 10,
    force: bool = False,
    candidate_id: int | None = None,
) -> dict[str, int]:
    ensure_schema()

    ids = (
        [candidate_id]
        if candidate_id is not None
        else load_pending_enrichment(limit=limit, force=force)
    )

    counts = {
        "selected": len(ids),
        "done": 0,
        "failed": 0,
        "stale": 0,
    }

    for record_id in ids:
        try:
            result = await enrich_candidate(record_id)
            counts["done"] += 1

            logger.info(
                "Enrichment #%s: %s / %s / ESCO=%s",
                record_id,
                result["content_type"],
                result["analysis_status"],
                result["esco_classification_status"],
            )

        except StaleEnrichmentError as exc:
            counts["stale"] += 1
            logger.info(
                "Enrichment #%s rinviato: %s",
                record_id,
                exc,
            )

        except Exception:
            counts["failed"] += 1
            logger.exception(
                "Enrichment failed for candidate #%s",
                record_id,
            )

    return counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract job fields with Qwen and look up ESCO candidates."
    )

    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--candidate-id", type=int)

    args = parser.parse_args()

    if args.limit < 1:
        parser.error("--limit must be at least 1")

    return args


async def main() -> None:
    args = parse_args()

    counts = await enrich_pending(
        limit=args.limit,
        force=args.force,
        candidate_id=args.candidate_id,
    )

    logger.info("Batch complete: %s", counts)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())