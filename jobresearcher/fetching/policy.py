from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


DEFAULT_ALLOWED_HOSTS = {
    "jobs.gem.com",
    "jobs.ashbyhq.com",
    "www.dejob.ai",
    "dejob.ai",
    "jobs.solana.com",
    "job-boards.greenhouse.io",
    "job-boards.eu.greenhouse.io",
    "boards.greenhouse.io",
    "apply.workable.com",
    "www.offchainlabs.com",
    "offchainlabs.com",
}

BROWSER_HOSTS = {
    "jobs.ashbyhq.com",
    "www.dejob.ai",
    "dejob.ai",
}

MARKDOWN_LINK = re.compile(
    r"^\[[^\]]*\]\((https?://[^\s]+)\)$",
    re.IGNORECASE,
)


def normalize_url(value: str | None) -> str:
    value = (value or "").strip()
    match = MARKDOWN_LINK.fullmatch(value)
    return match.group(1) if match else value


def allowed_hosts() -> set[str]:
    extra = {
        item.strip().casefold()
        for item in os.getenv("JOB_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    return DEFAULT_ALLOWED_HOSTS | extra


def is_allowed_url(url: str) -> bool:
    try:
        if not url or any(ord(char) < 32 for char in url):
            return False

        parsed = urlsplit(url)

        return (
            parsed.scheme.casefold() == "https"
            and parsed.hostname is not None
            and parsed.hostname.casefold() in allowed_hosts()
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except (TypeError, ValueError):
        return False


def is_browser_resource_allowed(url: str) -> bool:
    return is_allowed_url(url)