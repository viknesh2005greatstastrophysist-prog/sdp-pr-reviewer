from __future__ import annotations

from pathlib import Path

from .agents import CATEGORIES, BaseReviewAgent, OfflineAgent, dispatch
from .aggregator import aggregate, executive_summary
from .analyzers import StaticAnalyzer
from .context import PythonContextExtractor
from .models import ReviewResult
from .orchestrator import LangGraphOrchestrator
from .parser import parse_diff


async def review_diff(
    raw: str,
    repo: str | None = None,
    agents: list[BaseReviewAgent] | None = None,
    *,
    agent_timeout: float = 45,
    use_langgraph: bool = True,
    min_confidence: float = 0.0,
) -> ReviewResult:
    files = parse_diff(raw)
    warnings: list[str] = []
    if repo:
        warnings.extend(PythonContextExtractor().enrich(files, Path(repo)))
    static, static_warnings = StaticAnalyzer().analyze(files, repo)
    warnings.extend(static_warnings)
    selected = agents if agents is not None else [OfflineAgent(category) for category in CATEGORIES]
    if use_langgraph:
        try:
            agent_findings, agent_warnings, runs = await LangGraphOrchestrator(
                selected, agent_timeout
            ).run(files)
        except ImportError:
            agent_findings, agent_warnings = await dispatch(files, selected, agent_timeout)
            runs = []
            agent_warnings.append("LangGraph unavailable; used concurrent fallback.")
    else:
        agent_findings, agent_warnings = await dispatch(files, selected, agent_timeout)
        runs = []
    warnings.extend(agent_warnings)
    findings = aggregate(static + agent_findings)
    if min_confidence > 0:
        kept = [item for item in findings if item.confidence >= min_confidence]
        if len(kept) < len(findings):
            warnings.append(
                f"Suppressed {len(findings) - len(kept)} finding(s) below confidence "
                f"{min_confidence:.2f}."
            )
        findings = kept
    return ReviewResult(
        files=files,
        findings=findings,
        warnings=warnings,
        agent_runs=runs,
        summary=executive_summary(findings, warnings),
    )
