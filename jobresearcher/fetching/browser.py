from __future__ import annotations

import asyncio
from dataclasses import dataclass

from playwright.async_api import async_playwright

from jobresearcher.fetching.policy import (
    is_allowed_url,
    is_browser_resource_allowed,
    normalize_url,
)


MAX_BROWSER_HTML_BYTES = 5 * 1024 * 1024


@dataclass
class BrowserResult:
    status: str
    html: str | None = None
    final_url: str | None = None
    http_status: int | None = None
    error: str | None = None
    retryable: bool = False


async def _fetch_browser(
    url: str,
    timeout_ms: int,
) -> BrowserResult:
    blocked_navigation: list[str] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        try:
            context = await browser.new_context(
                service_workers="block",
                accept_downloads=False,
            )

            try:
                page = await context.new_page()
                page.set_default_timeout(timeout_ms)
                page.set_default_navigation_timeout(timeout_ms)

                async def handle_route(route) -> None:
                    request = route.request

                    if not is_browser_resource_allowed(request.url):
                        if (
                            request.is_navigation_request()
                            and request.frame == page.main_frame
                        ):
                            blocked_navigation.append(request.url)

                        await route.abort()
                        return

                    if request.resource_type in {
                        "image", "media", "font"
                    }:
                        await route.abort()
                        return

                    await route.continue_()

                await context.route("**/*", handle_route)

                response = await page.goto(
                    url,
                    wait_until="domcontentloaded",
                )

                await page.wait_for_timeout(1500)

                if blocked_navigation:
                    return BrowserResult(
                        status="skipped",
                        final_url=blocked_navigation[-1],
                        error="Navigazione browser non consentita",
                    )

                final_url = page.url

                if not is_allowed_url(final_url):
                    return BrowserResult(
                        status="skipped",
                        final_url=final_url,
                        error="URL finale browser non consentito",
                    )

                if response is None:
                    return BrowserResult(
                        status="failed",
                        final_url=final_url,
                        error="Navigazione senza risposta HTTP",
                    )

                code = response.status

                if not 200 <= code < 300:
                    return BrowserResult(
                        status="failed",
                        final_url=final_url,
                        http_status=code,
                        error=f"Browser HTTP {code}",
                        retryable=code in {408, 425, 429} or code >= 500,
                    )

                html = await page.content()

                if len(html.encode("utf-8")) > MAX_BROWSER_HTML_BYTES:
                    return BrowserResult(
                        status="skipped",
                        final_url=final_url,
                        http_status=code,
                        error="Browser HTML exceeds the size limit",
                    )

                return BrowserResult(
                    status="success",
                    html=html,
                    final_url=final_url,
                    http_status=code,
                )

            finally:
                await context.close()

        finally:
            await browser.close()


async def fetch_with_browser(
    url: str,
    timeout_ms: int = 30000,
) -> BrowserResult:
    url = normalize_url(url)

    if timeout_ms < 1:
        raise ValueError("timeout_ms must be positive")

    if not is_allowed_url(url):
        return BrowserResult(
            status="skipped",
            final_url=url,
            error="URL iniziale browser non consentito",
        )

    try:
        return await asyncio.wait_for(
            _fetch_browser(url, timeout_ms),
            timeout=(timeout_ms / 1000) + 10,
        )

    except asyncio.TimeoutError:
        return BrowserResult(
            status="failed",
            error="Timeout complessivo browser",
            retryable=True,
        )

    except Exception as exc:
        return BrowserResult(
            status="failed",
            error=f"{type(exc).__name__}: {exc}",
        )