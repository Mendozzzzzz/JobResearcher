from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx
import trafilatura

from jobresearcher.fetching.policy import (
    is_allowed_url,
    normalize_url,
)


USER_AGENT = (
    "JobResearcher/0.2 "
    "(personal job research tool; contact: local-user)"
)

MIN_EXTRACTED_CHARS = 250
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_FETCH_TIMEOUT = 60

REDIRECT_STATUSES = {301, 302, 303, 307, 308}

CLOSED_JOB_PATTERNS = (
    "no longer accepting applications",
    "job is no longer available",
    "this position has been filled",
    "this job has expired",
    "position is closed",
    "job closed",
    "applications are closed",
    "non accettiamo più candidature",
    "non si accettano più candidature",
    "posizione chiusa",
    "posizione ricoperta",
    "annuncio scaduto",
    "candidature chiuse",
)

OPEN_JOB_PATTERNS = (
    "apply for this job",
    "apply now",
    "submit your application",
    "submit application",
    "candidati ora",
    "invia la tua candidatura",
    "invia il tuo curriculum",
    "invia il tuo cv",
)


@dataclass
class FetchResult:
    url: str
    status: str
    page_text: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    error: str | None = None
    job_status: str = "unknown"
    retryable: bool = False
    browser_fallback: bool = False
    short_text: bool = False


def detect_job_status(page_text: str | None) -> str:
    if not page_text:
        return "unknown"

    lowered = page_text.casefold()

    if any(pattern in lowered for pattern in CLOSED_JOB_PATTERNS):
        return "closed"

    if any(pattern in lowered for pattern in OPEN_JOB_PATTERNS):
        return "open"

    return "unknown"


def extract_main_text(html: str, url: str) -> str | None:
    text = trafilatura.extract(
        html,
        url=url,
        include_comments=False,
        include_tables=True,
        favor_recall=True,
    )

    if not text:
        return None

    return text.strip() or None


async def _fetch_http(
    client: httpx.AsyncClient,
    url: str,
) -> FetchResult:
    current_url = normalize_url(url)
    visited: set[str] = set()

    for hop in range(MAX_REDIRECTS + 1):
        if not is_allowed_url(current_url):
            return FetchResult(
                url=url,
                status="skipped",
                final_url=current_url,
                error="URL or redirect destination is not allowed",
            )

        if current_url in visited:
            return FetchResult(
                url=url,
                status="failed",
                final_url=current_url,
                error="Ciclo di redirect",
            )

        visited.add(current_url)

        async with client.stream(
            "GET",
            current_url,
            follow_redirects=False,
        ) as response:
            response_url = str(response.url)
            code = response.status_code

            if code in REDIRECT_STATUSES:
                location = response.headers.get("location")

                if not location:
                    return FetchResult(
                        url=url,
                        status="failed",
                        http_status=code,
                        final_url=response_url,
                        error="Redirect senza Location",
                    )

                next_url = urljoin(response_url, location)

                if not is_allowed_url(next_url):
                    return FetchResult(
                        url=url,
                        status="skipped",
                        http_status=code,
                        final_url=next_url,
                        error="Redirect destination is not allowed",
                    )

                if hop >= MAX_REDIRECTS:
                    return FetchResult(
                        url=url,
                        status="failed",
                        http_status=code,
                        final_url=response_url,
                        error="Maximum number of redirects exceeded",
                    )

                current_url = next_url
                continue

            if not 200 <= code < 300:
                return FetchResult(
                    url=url,
                    status="failed",
                    http_status=code,
                    final_url=response_url,
                    error=f"HTTP {code}",
                    retryable=code in {408, 425, 429} or code >= 500,
                )

            content_type = response.headers.get(
                "content-type", ""
            ).casefold()

            if (
                "text/html" not in content_type
                and "application/xhtml+xml" not in content_type
            ):
                return FetchResult(
                    url=url,
                    status="skipped",
                    http_status=code,
                    final_url=response_url,
                    error=(
                        "Content type non supportato: "
                        f"{content_type or 'unknown'}"
                    ),
                )

            body = bytearray()

            async for chunk in response.aiter_bytes():
                body.extend(chunk)

                if len(body) > MAX_RESPONSE_BYTES:
                    return FetchResult(
                        url=url,
                        status="skipped",
                        http_status=code,
                        final_url=response_url,
                        error="Page exceeds the size limit",
                    )

            encoding = response.encoding or "utf-8"

            try:
                html = body.decode(encoding, errors="replace")
            except LookupError:
                html = body.decode("utf-8", errors="replace")

            page_text = await asyncio.to_thread(
                extract_main_text,
                html,
                response_url,
            )

            if not page_text:
                return FetchResult(
                    url=url,
                    status="failed",
                    http_status=code,
                    final_url=response_url,
                    error="No usable text could be extracted",
                    browser_fallback=True,
                )

            return FetchResult(
                url=url,
                status="success",
                page_text=page_text,
                http_status=code,
                final_url=response_url,
                job_status=detect_job_status(page_text),
                short_text=len(page_text) < MIN_EXTRACTED_CHARS,
            )

    return FetchResult(
        url=url,
        status="failed",
        final_url=current_url,
        error="Redirect non risolto",
    )


async def fetch_one(
    client: httpx.AsyncClient,
    url: str,
) -> FetchResult:
    url = normalize_url(url)

    try:
        return await asyncio.wait_for(
            _fetch_http(client, url),
            timeout=TOTAL_FETCH_TIMEOUT,
        )

    except (asyncio.TimeoutError, httpx.TimeoutException):
        return FetchResult(
            url=url,
            status="failed",
            error="Timeout del download",
            retryable=True,
        )

    except httpx.HTTPError as exc:
        return FetchResult(
            url=url,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
            retryable=True,
        )

    except Exception as exc:
        return FetchResult(
            url=url,
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )


def make_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "it,en;q=0.9",
        },
        follow_redirects=False,
        trust_env=False,
        timeout=httpx.Timeout(
            connect=10.0,
            read=20.0,
            write=20.0,
            pool=20.0,
        ),
        limits=httpx.Limits(
            max_connections=5,
            max_keepalive_connections=3,
        ),
    )