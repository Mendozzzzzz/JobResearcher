import re

DEGREE_PATTERNS = [
    r"\b(bachelor|bs|ba|b\.sc|b\.eng|undergraduate|four[- ]year)\b.*\bdegree\b",
    r"\b(master|msc|ms|m\.sc|graduate)\b.*\bdegree\b",
    r"\b(phd|ph\.d|doctorate|doctoral)\b",
    r"\b(mba)\b",
]

NEGATION_PATTERNS = [
    r"\b(no|not)\s+(degree|bachelor|master|phd)\b",
    r"\bdegree\s+(not|is not)\s+required\b",
    r"\bwithout\s+(a\s+)?degree\b",
]

ALTERNATIVE_PATTERNS = [
    r"\b(or|in lieu of|instead of)\s+(equivalent|comparable|relevant)\s+(experience|work experience)\b",
    r"\bequivalent\s+(experience|combination)\b",
    r"\bor\s+equivalent\b",
]

PREFERRED_PATTERNS = [
    r"\b(preferred|nice to have|plus|desirable|a plus)\b",
]

SENIORITY_BLOCKLIST = [
    r"\b(10|15|\d{2,})\s*\+?\s*years?\b",
    r"\bsenior\b",
    r"\bprincipal\b",
    r"\bstaff engineer\b",
]

def classify_degree(text: str) -> str:
    t = text.lower()
    if any(re.search(p, t) for p in NEGATION_PATTERNS):
        return "not_required"
    if any(re.search(p, t) for p in DEGREE_PATTERNS):
        if any(re.search(p, t) for p in ALTERNATIVE_PATTERNS):
            return "not_required"
        if any(re.search(p, t) for p in PREFERRED_PATTERNS):
            return "preferred"
        return "required"
    return "unknown"

def is_too_senior(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in SENIORITY_BLOCKLIST)

def should_process(text: str) -> bool:
    if not text or len(text) < 50:
        return False
    if is_too_senior(text):
        return False
    if classify_degree(text) == "required":
        return False
    return True