from __future__ import annotations

import re

from .models import Finding, Severity

RANK = {
    Severity.CRITICAL: 5,
    Severity.HIGH: 4,
    Severity.MEDIUM: 3,
    Severity.LOW: 2,
    Severity.INFO: 1,
}


def _tokens(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9_]+", value.casefold()) if len(word) > 2}


def _similar(left: Finding, right: Finding) -> bool:
    if left.path != right.path or abs(left.line - right.line) > 2:
        return False
    a, b = _tokens(left.title + " " + left.message), _tokens(right.title + " " + right.message)
    return bool(a and b) and len(a & b) / len(a | b) >= 0.55


def aggregate(findings: list[Finding]) -> list[Finding]:
    """Collapse exact and semantically near findings, retaining strongest evidence."""
    unique: list[Finding] = []
    for finding in sorted(findings, key=lambda item: (-RANK[item.severity], -item.confidence)):
        duplicate = next((item for item in unique if _similar(item, finding)), None)
        if duplicate is None:
            unique.append(finding.model_copy(deep=True))
            continue
        if finding.source not in duplicate.source.split("+"):
            duplicate.source += f"+{finding.source}"
        if finding.evidence not in duplicate.evidence:
            duplicate.evidence += f" Corroboration: {finding.evidence}"
        duplicate.confidence = max(duplicate.confidence, finding.confidence)
        if duplicate.suggestion is None:
            duplicate.suggestion = finding.suggestion
    return sorted(
        unique, key=lambda item: (-RANK[item.severity], item.path, item.line, item.category)
    )


def executive_summary(findings: list[Finding], warnings: list[str]) -> str:
    counts = {
        severity: sum(item.severity == severity for item in findings) for severity in Severity
    }
    material = counts[Severity.CRITICAL] + counts[Severity.HIGH]
    headline = f"Found {len(findings)} actionable issue(s), including {material} high/critical."
    breakdown = ", ".join(
        f"{severity.value}: {count}" for severity, count in counts.items() if count
    )
    degraded = f" Analysis warnings: {len(warnings)}." if warnings else ""
    return f"{headline} {breakdown or 'No findings.'}.{degraded}".replace("..", ".")
