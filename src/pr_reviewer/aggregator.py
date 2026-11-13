from __future__ import annotations

import keyword
import re

from .models import DiffFile, Finding, Severity

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


_KEYWORDS = set(keyword.kwlist) | {"self", "cls", "none", "true", "false"}


def _subtokens(value: str) -> set[str]:
    """Identifier tokens plus their snake_case parts (literal_eval -> literal, eval)."""
    out: set[str] = set()
    for word in re.findall(r"[a-z0-9_]+", value.casefold()):
        out.add(word)
        out.update(part for part in word.split("_") if part)
    return {word for word in out if len(word) > 2 and not word.isdigit()}


def _code_line(files: list[DiffFile] | None, path: str, line: int) -> str:
    for diff_file in files or []:
        if diff_file.path == path:
            return next((item.text for item in diff_file.lines if item.new_line == line), "")
    return ""


def _same_subject(left: Finding, right: Finding, files: list[DiffFile] | None) -> bool:
    """Different tools naming the same identifier on the same line describe one problem."""
    if left.path != right.path or left.line != right.line or left.source == right.source:
        return False
    salient = _subtokens(_code_line(files, left.path, left.line)) - _KEYWORDS
    if not salient:
        return False

    def mentions(item: Finding) -> set[str]:
        return _subtokens(f"{item.title} {item.message} {item.rule_id or ''}") & salient

    return bool(mentions(left) & mentions(right))


def aggregate(findings: list[Finding], files: list[DiffFile] | None = None) -> list[Finding]:
    """Collapse exact and semantically near findings, retaining strongest evidence."""
    unique: list[Finding] = []
    for finding in sorted(findings, key=lambda item: (-RANK[item.severity], -item.confidence)):
        duplicate = next(
            (
                item
                for item in unique
                if _similar(item, finding) or _same_subject(item, finding, files)
            ),
            None,
        )
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
    return sorted(unique, key=rank_key)


def rank_key(item: Finding) -> tuple:
    """Severity first, then confidence, then a stable location order."""
    return (-RANK[item.severity], -round(item.confidence, 3), item.path, item.line, item.category)


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
