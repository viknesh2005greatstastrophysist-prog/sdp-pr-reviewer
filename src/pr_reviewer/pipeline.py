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
    return ReviewResult(
        files=files,
        findings=findings,
        warnings=warnings,
        agent_runs=runs,
        summary=executive_summary(findings, warnings),
    )
