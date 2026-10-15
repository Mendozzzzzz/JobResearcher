from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlsplit

from jobresearcher.db import (
    get_pending_fetch_jobs,
    save_fetch_job_result,
)
from jobresearcher.fetching.policy import (
    BROWSER_HOSTS,
    normalize_url,
)
from jobresearcher.scraping.fetcher import (
    FetchResult,
    MIN_EXTRACTED_CHARS,
    detect_job_status,
    extract_main_text,
    fetch_one,
    make_http_client,
)


logger = logging.getLogger(__name__)
_fetch_lock = asyncio.Lock()


async def _browser_fallback(
    url: str,
    http_result: FetchResult,
) -> FetchResult:
    from jobresearcher.fetching.browser import fetch_with_browser

    browser_result = await fetch_with_browser(url)

    if browser_result.status != "success":
        return FetchResult(
            url=url,
            status=browser_result.status,
            final_url=browser_result.final_url,
            http_status=browser_result.http_status,
            error=(
                f"HTTP: {http_result.error}; "
                f"browser: {browser_result.error}"
            ),
            retryable=browser_result.retryable,
        )

    final_url = browser_result.final_url or url

    page_text = await asyncio.to_thread(
        extract_main_text,
        browser_result.html or "",
        final_url,
    )

    if not page_text:
        return FetchResult(
            url=url,
            status="failed",
            final_url=final_url,
            http_status=browser_result.http_status,
            error="No text could be extracted from the browser page",
        )

    return FetchResult(
        url=url,
        status="success",
        page_text=page_text,
        final_url=final_url,
        http_status=browser_result.http_status,
        job_status=detect_job_status(page_text),
        short_text=len(page_text) < MIN_EXTRACTED_CHARS,
    )


async def fetch_pending_jobs(
    limit: int = 10,
    browser_enabled: bool = False,
    include_retries: bool = True,
) -> dict[str, int | str | None]:
    async with _fetch_lock:
        candidates = get_pending_fetch_jobs(
            limit=limit,
            include_retries=include_retries,
        )

        counts: dict[str, int | str | None] = {
            "selected": len(candidates),
            "success": 0,
            "failed": 0,
            "skipped": 0,
            "stale": 0,
            "last_error": None,
        }
        async with make_http_client() as client:
            for candidate in candidates:
                original_url = candidate["external_url"]
                original_text = candidate["raw_telegram_text"] or ""
                url = normalize_url(original_url)

                result = await fetch_one(client, url)

                try:
                    host = urlsplit(url).hostname
                except ValueError:
                    host = None

                if (
                    browser_enabled
                    and result.status == "failed"
                    and result.browser_fallback
                    and host in BROWSER_HOSTS
                ):
                    try:
                        result = await _browser_fallback(url, result)
                    except Exception as exc:
                        result = FetchResult(
                            url=url,
                            status="failed",
                            error=(
                                "Browser fallback failed: "
                                f"{type(exc).__name__}: {exc}"
                            ),
                        )

                saved = save_fetch_job_result(
                    candidate_id=candidate["id"],
                    expected_url=original_url,
                    expected_text=original_text,
                    fetch_status=result.status,
                    page_text=result.page_text,
                    final_url=result.final_url,
                    job_status=result.job_status,
                    error=result.error,
                    retryable=result.retryable,
                    short_text=result.short_text,
                )

                if not saved:
                    counts["stale"] += 1
                    logger.info(
                        "Discarded stale fetch result for candidate %s",
                        candidate["id"],
                    )
                    continue

                counts[result.status] += 1

                if result.status == "failed":
                    http_detail = (
                        f"HTTP {result.http_status} — "
                        if result.http_status is not None
                        else ""
                    )

                    counts["last_error"] = (
                        f"fetch #{candidate['id']}: "
                        f"{http_detail}"
                        f"{result.error or 'Download failed without details'}"
                    )

                log = (
                    logger.warning
                    if result.status == "failed"
                    else logger.info
                )

                log(
                    "Fetch candidate %s: status=%s, "
                    "http=%s, retryable=%s, dettaglio=%s",
                    candidate["id"],
                    result.status,
                    result.http_status,
                    result.retryable,
                    result.error or "none",
                )
                await asyncio.sleep(0.2)

        return counts