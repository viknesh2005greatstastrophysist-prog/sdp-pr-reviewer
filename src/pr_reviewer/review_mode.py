"""Review mode: how much of the raw analysis is shown to a pull-request author.

`full` keeps every finding (all Ruff families, all agent findings): useful for audits, noisy in
practice. `focused` (the default) keeps only findings that indicate a likely defect:

* Static findings from Semgrep rules (the project rules are all security/defect rules) and from
  Ruff families that flag real defects: security (S), bugbear (B), pyflakes errors (F) and syntax
  errors (E9). Cosmetic members of those families are excluded, see the constants below.
  Formatting (E1-E7, W), import order (I) and modernisation (UP) are never shown in focused mode:
  a formatter or a pre-commit hook owns those.
* Agent findings at or above a confidence threshold.
* At most `max_per_file` findings per file, strongest first, so one file cannot bury a review.

The rule list, threshold and cap were chosen on the DEV split of the evaluation corpus only
(see evaluation/RESULTS.md); the TEST split was never used for tuning.
Filtering happens after aggregation, so it can be replayed offline from saved predictions.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .aggregator import rank_key
from .models import Finding

MODES = ("focused", "full")
DEFAULT_MODE = "focused"
STATIC_SOURCES = frozenset({"ruff", "semgrep"})

# Ruff families that indicate defects. E9 = syntax/IO errors only (E902, E999).
DEFECT_FAMILIES = ("S", "B", "F", "E9")
# Members of those families that are cosmetic, idiomatic or name-guessing heuristics.
COSMETIC_RULES = frozenset(
    {
        # pyflakes: unused imports/variables and star imports are tidiness, not defects
        "F401",
        "F403",
        "F405",
        "F541",
        "F841",
        "F842",
        # bugbear: idiom/style opinions
        "B007",
        "B008",
        "B009",
        "B010",
        "B024",
        "B027",
        "B028",
        "B904",
        "B905",
        # bandit: name-based guesses and import-only warnings generate mostly false alarms
        "S101",
        "S105",
        "S106",
        "S107",
        "S311",
        "S404",
        "S603",
        "S607",
    }
)


@dataclass(frozen=True)
class FocusConfig:
    agent_min_confidence: float = 0.7
    max_per_file: int = 5
    # Cap on findings shown per pull request (None = no cap). Chosen on the dev split.
    max_per_pr: int | None = None
    # Added to the ranking score of findings corroborated by two sources (static + agent, or two
    # agents). It only changes order, so it matters when a cap applies.
    agreement_boost: float = 0.2
    families: tuple[str, ...] = DEFECT_FAMILIES
    cosmetic: frozenset[str] = field(default=COSMETIC_RULES)


def corroborated(findings: list[Finding]) -> set[int]:
    """Indexes of findings backed by a second source: a merged finding, or another finding
    from a different source within two lines of the same file."""
    marked: set[int] = set()
    for i, left in enumerate(findings):
        if len(set(left.source.split("+"))) >= 2:
            marked.add(i)
        for j in range(i + 1, len(findings)):
            right = findings[j]
            if (
                left.path == right.path
                and abs(left.line - right.line) <= 2
                and set(left.source.split("+")) != set(right.source.split("+"))
            ):
                marked.update((i, j))
    return marked


def boosted_rank(finding: Finding, config: FocusConfig, boosted: bool) -> tuple:
    """Severity first, then confidence plus the agreement boost for corroborated findings."""
    severity, negative_confidence, path, line, category = rank_key(finding)
    boost = config.agreement_boost if boosted else 0.0
    return (severity, negative_confidence - boost, path, line, category)


def static_rule_kept(finding: Finding, config: FocusConfig) -> bool:
    """Semgrep rules always count; Ruff rules count when in a defect family and not cosmetic."""
    if finding.source == "semgrep":
        return True
    code = (finding.rule_id or "").upper()
    if not code or code in config.cosmetic:
        return False
    return any(code.startswith(family) for family in config.families)


def keep_finding(finding: Finding, config: FocusConfig) -> bool:
    sources = finding.source.split("+")
    static = [s for s in sources if s in STATIC_SOURCES]
    agents = [s for s in sources if s not in STATIC_SOURCES]
    # A merged finding is corroborated by two independent analyses: either leg may carry it.
    # (Aggregation keeps the maximum confidence, so a merged finding is never weaker than its
    # strongest leg.)
    if static and static_rule_kept(finding, config):
        return True
    return bool(agents) and finding.confidence >= config.agent_min_confidence


def apply_review_mode(
    findings: list[Finding], mode: str = DEFAULT_MODE, config: FocusConfig | None = None
) -> tuple[list[Finding], dict[str, int]]:
    """Return (findings to show, suppressed counts by reason)."""
    if mode not in MODES:
        raise ValueError(f"review mode must be one of {MODES}, got {mode!r}")
    if mode == "full":
        return list(findings), {}
    config = config or FocusConfig()
    suppressed: Counter[str] = Counter()
    kept: list[Finding] = []
    for finding in findings:
        if keep_finding(finding, config):
            kept.append(finding)
        elif any(s in STATIC_SOURCES for s in finding.source.split("+")) and not any(
            s not in STATIC_SOURCES for s in finding.source.split("+")
        ):
            suppressed["static rule outside the defect families"] += 1
        else:
            suppressed["agent confidence below threshold"] += 1
    per_file: Counter[str] = Counter()
    capped: list[Finding] = []
    # Corroboration is judged on everything found, including findings that are hidden.
    boosted = {id(findings[i]) for i in corroborated(findings)}
    for finding in sorted(kept, key=lambda item: boosted_rank(item, config, id(item) in boosted)):
        per_file[finding.path] += 1
        if per_file[finding.path] > config.max_per_file:
            suppressed["per-file cap"] += 1
        else:
            capped.append(finding)
    if config.max_per_pr is not None and len(capped) > config.max_per_pr:
        suppressed["per-PR top-k cap"] += len(capped) - config.max_per_pr
        capped = capped[: config.max_per_pr]
    return capped, dict(suppressed)
