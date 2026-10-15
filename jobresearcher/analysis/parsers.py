from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


INTRO_COMPANY_RE = re.compile(
    r"""
    ^
    \s*
    (?P<company>[A-Z][A-Za-z0-9&.'\- ]{1,70})
    \s+
    (?:is|are|builds|develops|creates|operates|runs|provides|makes|offers)
    \b
    """,
    re.IGNORECASE | re.MULTILINE | re.VERBOSE,
)
MARKDOWN_LINK_RE = re.compile(
    r"\[[^\]]+\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)

URL_RE = re.compile(
    r"""(?ix)
    \b(
        https?://
        [^\s<>"'()\[\]{}]+
    )
    """
)

EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

TELEGRAM_HANDLE_RE = re.compile(
    r"(?<![\w@])@([a-zA-Z][a-zA-Z0-9_]{4,31})\b"
)

MONEY_RE = re.compile(
    r"""(?ix)
    (?P<currency>USD|EUR|GBP|\$|€|£)\s*
    (?P<min>\d{1,3}(?:[,\s]\d{3})*|\d+(?:\.\d+)?\s*[kK])
    \s*
    (?:
        (?:-|–|—|to)\s*
        (?P<currency2>USD|EUR|GBP|\$|€|£)?\s*
        (?P<max>\d{1,3}(?:[,\s]\d{3})*|\d+(?:\.\d+)?\s*[kK])
    )?
    """
)

TIMEZONE_RE = re.compile(
    r"\b(?:UTC|GMT)\s*[+-]\s*\d{1,2}(?::?\d{2})?"
    r"(?:\s*(?:to|-|–)\s*(?:UTC|GMT)?\s*[+-]?\s*\d{1,2}(?::?\d{2})?)?",
    re.IGNORECASE,
)

DISCORD_URL_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?(?:discord\.gg|discord\.com/invite)/[A-Za-z0-9-]+",
    re.IGNORECASE,
)

JOB_URL_HINTS = (
    "jobs.",
    "job.",
    "careers.",
    "career.",
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "workable.com",
    "smartrecruiters.com",
    "applytojob.com",
    "myworkdayjobs.com",
)

SKILL_PATTERNS = {
    "Rust": r"\brust\b",
    "Solidity": r"\bsolidity\b",
    "EVM": r"\bevm\b",
    "Ethereum": r"\bethereum\b",
    "Web3": r"\bweb\s?3\b",
    "Blockchain": r"\bblockchain\b",
    "DeFi": r"\bdefi\b",
    "ZK": r"\b(?:zk|zero[-\s]?knowledge|zkevm|zkp)\b",
    "STARKs": r"\bstarks?\b",
    "SNARKs": r"\bsnarks?\b",
    "Cryptography": r"\bcryptograph(?:y|ic)\b",
    "Smart Contracts": r"\bsmart[-\s]?contracts?\b",
    "Security": r"\bsecurity\b|\baudit(?:ing|or|s)?\b",
    "Foundry": r"\bfoundry\b",
    "Hardhat": r"\bhardhat\b",
    "TypeScript": r"\btypescript\b|\bts\b",
    "JavaScript": r"\bjavascript\b|\bnode\.?js\b",
    "Python": r"\bpython\b",
    "React": r"\breact(?:\.js)?\b",
    "Next.js": r"\bnext\.?js\b",
    "Go": r"\bgolang\b|\bgo language\b",
    "C++": r"\bc\+\+\b",
    "Kubernetes": r"\bkubernetes\b|\bk8s\b",
    "Docker": r"\bdocker\b",
    "AWS": r"\baws\b|\bamazon web services\b",
}

REMOTE_PATTERNS = [
    r"\bfully remote\b",
    r"\bremote[-\s]?first\b",
    r"\bremote\b",
    r"\bwork from (?:home|anywhere)\b",
    r"\bdistributed team\b",
    r"\banywhere in the world\b",
    r"\bworldwide\b",
]

HYBRID_PATTERNS = [
    r"\bhybrid\b",
    r"\bremote\s*/\s*onsite\b",
]

ONSITE_PATTERNS = [
    r"\bon[-\s]?site\b",
    r"\bin[-\s]?office\b",
    r"\brelocat(?:e|ion)\b",
]

REGION_PATTERNS = {
    "Worldwide": r"\bworldwide\b|\bglobal\b|\banywhere\b",
    "Europe": r"\beurope\b|\beu\b|\bemea\b",
    "Italy": r"\bitaly\b|\bitalia\b|\bitalian\b",
    "United Kingdom": r"\buk\b|\bunited kingdom\b|\bbritain\b",
    "United States": r"\busa\b|\bu\.s\.a?\b|\bunited states\b",
    "North America": r"\bnorth america\b|\bcanada\b",
    "Asia": r"\basia\b|\bapac\b",
    "LATAM": r"\blatam\b|\blatin america\b",
}

EMPLOYMENT_PATTERNS = {
    "full_time": r"\bfull[-\s]?time\b|\bpermanent\b",
    "part_time": r"\bpart[-\s]?time\b",
    "contract": r"\bcontract(?:or|ing)?\b|\bcontract role\b",
    "freelance": r"\bfreelance\b|\bfreelancer\b",
    "internship": r"\bintern(?:ship)?\b",
}

SENIORITY_PATTERNS = {
    "intern": r"\bintern(?:ship)?\b|\btrainee\b",
    "junior": r"\bjunior\b|\bentry[-\s]?level\b|\bgraduate\b",
    "mid": r"\bmid[-\s]?(?:level)?\b",
    "senior": r"\bsenior\b|\bsr\.?\b",
    "lead": r"\blead\b|\bprincipal\b|\bstaff\b|\bhead of\b",
}

DEGREE_REQUIRED_PATTERNS = [
    r"\b(?:bachelor'?s?|master'?s?|ph\.?d\.?|degree)\b.{0,40}\b(?:required|must have|mandatory)\b",
    r"\b(?:bachelor'?s?|master'?s?|ph\.?d\.?)\s+(?:in|degree)\b",
]

DEGREE_PREFERRED_PATTERNS = [
    r"\b(?:bachelor'?s?|master'?s?|ph\.?d\.?|degree)\b.{0,40}\b(?:preferred|desired|a plus|nice to have)\b",
]

DEGREE_NOT_REQUIRED_PATTERNS = [
    r"\bno\s+(?:degree|bachelor'?s?|master'?s?)\s+(?:required|needed)\b",
    r"\bdegree\s+(?:is\s+)?not\s+(?:required|needed)\b",
    r"\b(?:degree|bachelor'?s?)\s+or\s+equivalent\s+experience\b",
    r"\bequivalent\s+(?:professional\s+)?experience\b",
]

CONTACT_CUE_RE = re.compile(
    r"""(?ix)
    (?:
        apply|application|send|email|contact|reach\s+out|dm|message|submit
    )
    .{0,140}
    """
)
POSITION_RE = re.compile(
    r"(?im)^\s*(?:position|role|job\s*title)\s*:\s*([^\n]{2,100})"
)

COMPANY_RE = re.compile(
    r"(?im)^\s*(?:company|employer|organization)\s*:\s*([^\n]{2,100})"
)

RECRUITMENT_COMPANY_RE = re.compile(
    r"""
    \#recruitment
    [^\nA-Za-z]{0,20}
    \s*
    (?P<company>[A-Za-z0-9][A-Za-z0-9&.'\- ]{1,70}?)
    \s*
    (?=\#|🛵|📚|🌱|💰|\n|$)
    """,
    re.IGNORECASE | re.VERBOSE,
)

MULTI_ROLE_PATTERNS = [
    r"\b(?:several|multiple|various)\s+(?:open\s+)?roles?\b",
    r"\bopen positions?\s*:",
    r"\bwe(?:'| a)?re hiring (?:for )?(?:multiple|several)\b",
    r"\bteam across\b",
]

MARKDOWN_LINK_RE = re.compile(
    r"\[[^\]]+\]\((https?://[^)\s]+)\)",
    re.IGNORECASE,
)

EDITORIAL_PATTERNS = [
    r"\btop skills\b",
    r"\bskills in demand\b",
    r"\bwant to land\b",
    r"\bhere are the hottest\b",
    r"\bcareer advice\b",
    r"\btips? (?:for|to)\b",
    r"\bmarket trends?\b",
]

@dataclass
class ParsedMessage:
    urls: list[str] = field(default_factory=list)
    primary_url: Optional[str] = None
    application_url: Optional[str] = None

    emails: list[str] = field(default_factory=list)
    telegram_handles: list[str] = field(default_factory=list)
    discord_urls: list[str] = field(default_factory=list)
    contact_text: Optional[str] = None
    application_method: str = "unknown"

    salary_text: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    salary_period: str = "unknown"

    location_type: str = "unknown"
    location_text: Optional[str] = None
    eligible_regions: list[str] = field(default_factory=list)
    timezone_text: Optional[str] = None

    title: Optional[str] = None
    company: Optional[str] = None
    multi_role: bool = False
    is_job_posting: bool = True

    employment_type: str = "unknown"
    seniority: str = "unknown"
    degree_status: str = "unknown"

    skills: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


def _clean_field(value: str | None) -> Optional[str]:
    if not value:
        return None

    cleaned = value.strip(" \t:-–—•#")
    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned[:120] or None


def extract_title(text: str) -> Optional[str]:
    match = POSITION_RE.search(text)
    if match:
        return _clean_field(match.group(1))

    match = re.search(
        r"(?im)#(?:senior|junior|lead|staff|principal|backend|frontend|"
        r"platform|security|smart[\s·-]?contract|blockchain|protocol|"
        r"devops|qa|software)[·\s-]+(?:engineer|developer|researcher|auditor)\b",
        text,
    )
    if match:
        title = match.group(0).replace("#", "").replace("·", " ")
        return _clean_field(title)

    return None


def extract_company(text: str) -> Optional[str]:
    match = COMPANY_RE.search(text)
    if match:
        return _clean_field(match.group(1))

    match = RECRUITMENT_COMPANY_RE.search(text)
    if match:
        company = _clean_field(match.group("company"))

        if company:
            return company
    match = INTRO_COMPANY_RE.search(text)
    if match:
        return _clean_field(match.group("company"))
    return None

def is_multi_role_post(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in MULTI_ROLE_PATTERNS)


def is_editorial_post(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(pattern, lowered) for pattern in EDITORIAL_PATTERNS)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []

    for item in items:
        value = item.strip()
        if value and value not in seen:
            seen.add(value)
            output.append(value)

    return output


def _clean_url(url: str) -> str:
    return url.strip().rstrip(".,;:!?)]}>\"'")

def extract_urls(text: str) -> list[str]:
    markdown_urls = [
        _clean_url(match.group(1))
        for match in MARKDOWN_LINK_RE.finditer(text)
    ]

    plain_urls = [
        _clean_url(match.group(1))
        for match in URL_RE.finditer(text)
    ]

    return _unique(markdown_urls + plain_urls)
def extract_emails(text: str) -> list[str]:
    return _unique(EMAIL_RE.findall(text))


def extract_telegram_handles(text: str) -> list[str]:
    handles = [f"@{handle}" for handle in TELEGRAM_HANDLE_RE.findall(text)]
    return _unique(handles)


def extract_discord_urls(text: str) -> list[str]:
    return _unique(DISCORD_URL_RE.findall(text))


def choose_primary_url(urls: list[str]) -> Optional[str]:
    if not urls:
        return None

    for url in urls:
        if any(hint in url.lower() for hint in JOB_URL_HINTS):
            return url

    return urls[0]


def choose_application_url(urls: list[str]) -> Optional[str]:
    if not urls:
        return None

    for url in urls:
        if any(hint in url.lower() for hint in JOB_URL_HINTS):
            return url

    return urls[0]

def _normalize_money(value: str) -> Optional[int]:
    cleaned = value.lower().replace(",", "").replace(" ", "")

    try:
        if cleaned.endswith("k"):
            return int(float(cleaned[:-1]) * 1000)

        return int(float(cleaned))
    except ValueError:
        return None


def _currency_code(value: str | None) -> Optional[str]:
    if not value:
        return None

    normalized = value.upper()

    return {
        "$": "USD",
        "USD": "USD",
        "€": "EUR",
        "EUR": "EUR",
        "£": "GBP",
        "GBP": "GBP",
    }.get(normalized)


def extract_salary(text: str) -> tuple[
    Optional[str],
    Optional[int],
    Optional[int],
    Optional[str],
    str,
]:
    match = MONEY_RE.search(text)

    if not match:
        return None, None, None, None, "unknown"

    salary_text = match.group(0).strip()
    salary_min = _normalize_money(match.group("min"))
    salary_max = _normalize_money(match.group("max")) if match.group("max") else None

    currency = _currency_code(match.group("currency"))
    if currency is None:
        currency = _currency_code(match.group("currency2"))

    lower_text = text.lower()
    start = max(0, match.start() - 60)
    end = min(len(text), match.end() + 100)
    nearby = lower_text[start:end]

    if any(word in nearby for word in ("per year", "/year", "annual", "annually", "a year", "yearly", "pa")):
        period = "year"
    elif any(word in nearby for word in ("per month", "/month", "monthly", "a month")):
        period = "month"
    elif any(word in nearby for word in ("per day", "/day", "daily")):
        period = "day"
    elif any(word in nearby for word in ("per hour", "/hour", "hourly", "an hour")):
        period = "hour"
    else:
        period = "unknown"

    return salary_text, salary_min, salary_max, currency, period


def extract_location(text: str) -> tuple[str, Optional[str], list[str], Optional[str]]:
    lowered = text.lower()

    regions = [
        region
        for region, pattern in REGION_PATTERNS.items()
        if re.search(pattern, lowered, re.IGNORECASE)
    ]

    timezone_match = TIMEZONE_RE.search(text)
    timezone = timezone_match.group(0).strip() if timezone_match else None

    if any(re.search(pattern, lowered) for pattern in HYBRID_PATTERNS):
        location_type = "hybrid"
    elif any(re.search(pattern, lowered) for pattern in REMOTE_PATTERNS):
        location_type = "remote"
    elif any(re.search(pattern, lowered) for pattern in ONSITE_PATTERNS):
        location_type = "onsite"
    else:
        location_type = "unknown"

    location_bits = []

    if location_type != "unknown":
        location_bits.append(location_type)

    if regions:
        location_bits.append(", ".join(regions))

    if timezone:
        location_bits.append(timezone)

    location_text = " · ".join(location_bits) if location_bits else None

    return location_type, location_text, regions, timezone


def extract_skills(text: str) -> list[str]:
    lowered = text.lower()
    skills = [
        name
        for name, pattern in SKILL_PATTERNS.items()
        if re.search(pattern, lowered, re.IGNORECASE)
    ]
    return _unique(skills)


def extract_employment_type(text: str) -> str:
    lowered = text.lower()

    for name, pattern in EMPLOYMENT_PATTERNS.items():
        if re.search(pattern, lowered, re.IGNORECASE):
            return name

    return "unknown"


def extract_seniority(text: str) -> str:
    lowered = text.lower()

    for name, pattern in SENIORITY_PATTERNS.items():
        if re.search(pattern, lowered, re.IGNORECASE):
            return name

    return "unknown"


def extract_degree_status(text: str) -> str:
    lowered = text.lower()

    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in DEGREE_NOT_REQUIRED_PATTERNS):
        return "not_required"

    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in DEGREE_PREFERRED_PATTERNS):
        return "preferred"

    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in DEGREE_REQUIRED_PATTERNS):
        return "required"

    return "unknown"


def extract_contact_text(text: str) -> Optional[str]:
    match = CONTACT_CUE_RE.search(text)

    if not match:
        return None

    value = " ".join(match.group(0).split())

    if len(value) > 180:
        value = f"{value[:177]}..."

    return value


def determine_application_method(
    application_url: Optional[str],
    emails: list[str],
    handles: list[str],
    discord_urls: list[str],
) -> str:
    if application_url:
        return "url"
    if emails:
        return "email"
    if handles:
        return "telegram"
    if discord_urls:
        return "discord"
    return "unknown"


def parse_message(text: str) -> ParsedMessage:
    urls = extract_urls(text)
    emails = extract_emails(text)
    handles = extract_telegram_handles(text)
    discord_urls = extract_discord_urls(text)

    salary_text, salary_min, salary_max, salary_currency, salary_period = extract_salary(text)
    location_type, location_text, regions, timezone = extract_location(text)

    primary_url = choose_primary_url(urls)
    application_url = choose_application_url(urls)

    return ParsedMessage(
        urls=urls,
        primary_url=primary_url,
        application_url=application_url,
        emails=emails,
        telegram_handles=handles,
        discord_urls=discord_urls,
        contact_text=extract_contact_text(text),
        application_method=determine_application_method(
            application_url,
            emails,
            handles,
            discord_urls,
        ),
        salary_text=salary_text,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        salary_period=salary_period,
        location_type=location_type,
        location_text=location_text,
        eligible_regions=regions,
        timezone_text=timezone,
        employment_type=extract_employment_type(text),
        seniority=extract_seniority(text),
        degree_status=extract_degree_status(text),
        skills=extract_skills(text),
        keywords=extract_skills(text),
        title=extract_title(text),
        company=extract_company(text),
        multi_role=is_multi_role_post(text),
        is_job_posting=not is_editorial_post(text),
    )