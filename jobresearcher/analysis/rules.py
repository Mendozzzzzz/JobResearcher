from __future__ import annotations

from dataclasses import dataclass, field

from jobresearcher.analysis.parsers import ParsedMessage


CORE_SKILLS = {
    "Rust": 22,
    "ZK": 22,
    "Cryptography": 18,
    "STARKs": 16,
    "SNARKs": 16,
    "Security": 14,
    "Smart Contracts": 14,
    "Solidity": 12,
    "EVM": 10,
    "Blockchain": 10,
    "Web3": 8,
    "DeFi": 7,
    "Foundry": 6,
    "TypeScript": 4,
    "Python": 3,
}

ROLE_POSITIVE_KEYWORDS = {
    "engineer": 8,
    "developer": 8,
    "researcher": 8,
    "security": 8,
    "auditor": 8,
    "protocol": 7,
    "cryptography": 7,
    "intern": 5,
    "junior": 5,
}

ROLE_NEGATIVE_KEYWORDS = {
    "marketing": -25,
    "community manager": -25,
    "sales": -20,
    "business development": -20,
    "designer": -12,
    "content creator": -15,
    "ambassador": -20,
    "trader": -12,
}

SENIORITY_PENALTIES = {
    "senior": -15,
    "lead": -25,
}

DEGREE_SCORES = {
    "not_required": 12,
    "preferred": 3,
    "unknown": 0,
    "required": -35,
}


@dataclass
class RuleEvaluation:
    score: int
    reasons: list[str] = field(default_factory=list)
    relevant: bool = False


def evaluate_message(text: str, parsed: ParsedMessage) -> RuleEvaluation:
    lowered = text.lower()
    score = 0
    reasons: list[str] = []

    for skill in parsed.skills:
        value = CORE_SKILLS.get(skill, 0)
        if value:
            score += value
            reasons.append(f"{skill} +{value}")

    for keyword, value in ROLE_POSITIVE_KEYWORDS.items():
        if keyword in lowered:
            score += value
            reasons.append(f"{keyword} +{value}")

    for keyword, value in ROLE_NEGATIVE_KEYWORDS.items():
        if keyword in lowered:
            score += value
            reasons.append(f"{keyword} {value}")

    if parsed.location_type == "remote":
        score += 10
        reasons.append("remote +10")
    elif parsed.location_type == "hybrid":
        score += 2
        reasons.append("hybrid +2")
    elif parsed.location_type == "onsite":
        score -= 8
        reasons.append("onsite -8")

    if "Italy" in parsed.eligible_regions:
        score += 5
        reasons.append("Italy +5")
    elif "Europe" in parsed.eligible_regions:
        score += 4
        reasons.append("Europe +4")
    elif "Worldwide" in parsed.eligible_regions:
        score += 5
        reasons.append("Worldwide +5")

    degree_value = DEGREE_SCORES[parsed.degree_status]
    score += degree_value

    if degree_value:
        sign = "+" if degree_value > 0 else ""
        reasons.append(f"degree={parsed.degree_status} {sign}{degree_value}")

    seniority_penalty = SENIORITY_PENALTIES.get(parsed.seniority, 0)
    score += seniority_penalty

    if seniority_penalty:
        reasons.append(f"{parsed.seniority} {seniority_penalty}")

    if parsed.application_method != "unknown":
        score += 3
        reasons.append("apply path +3")

    if parsed.salary_min is not None or parsed.salary_max is not None:
        score += 3
        reasons.append("salary disclosed +3")

    if parsed.employment_type in {"full_time", "contract", "freelance"}:
        score += 2
        reasons.append(f"{parsed.employment_type} +2")

    score = max(0, min(score, 100))

    return RuleEvaluation(
        score=score,
        reasons=reasons,
        relevant=score >= 20,
    )