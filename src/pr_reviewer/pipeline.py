from __future__ import annotations

import asyncio
from pathlib import Path

from .agents import CATEGORIES, BaseReviewAgent, OfflineAgent, dispatch, dispatch_runs
from .aggregator import aggregate, executive_summary
from .analyzers import StaticAnalyzer
from .context import PythonContextExtractor
from .llm import OpenAICompatibleClient
from .models import ReviewResult
from .orchestrator import LangGraphOrchestrator
from .parser import parse_diff
from .review_mode import FocusConfig, apply_review_mode
from .settings import Settings
from .verifier import drop_ungrounded, verify_findings


async def review_diff(
    raw: str,
    repo: str | None = None,
    agents: list[BaseReviewAgent] | None = None,
    *,
    agent_timeout: float = 45,
    use_langgraph: bool = True,
    min_confidence: float = 0.0,
    review_mode: str | None = None,
    sequential_agents: bool = False,
    agent_delay: float = 0.0,
    verifier: OpenAICompatibleClient | None = None,
    focus: FocusConfig | None = None,
) -> ReviewResult:
    files = parse_diff(raw)
    warnings: list[str] = []
    if repo:
        # Parsing and the analyzers block; run them off the event loop so the server keeps
        # answering health checks and other users while a review is in progress.
        warnings.extend(await asyncio.to_thread(PythonContextExtractor().enrich, files, Path(repo)))
    static, static_warnings = await asyncio.to_thread(StaticAnalyzer().analyze, files, repo)
    warnings.extend(static_warnings)
    selected = agents if agents is not None else [OfflineAgent(category) for category in CATEGORIES]
    if sequential_agents:
        agent_findings, agent_warnings, runs = await dispatch_runs(
            files, selected, agent_timeout, sequential=True, delay=agent_delay
        )
    elif use_langgraph:
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
    findings = aggregate(static + agent_findings, files)
    # Focus after aggregation so the filter can be replayed offline on saved predictions.
    # None means "use the configured REVIEW_MODE" (default focused).
    config = Settings.from_env()
    mode = review_mode if review_mode is not None else config.review_mode
    if mode == "focused" and verifier is not None and findings:
        outcome = await verify_findings(verifier, files, findings)
        if outcome.error:
            warnings.append(f"Verification pass failed; findings kept unverified: {outcome.error}")
        else:
            findings, dropped = drop_ungrounded(findings, outcome.verdicts)
            if dropped:
                warnings.append(f"Verification dropped {dropped} ungrounded agent finding(s).")
    if focus is None and config.review_top_k:
        focus = FocusConfig(max_per_pr=config.review_top_k)
    findings, suppressed = apply_review_mode(findings, mode, focus)
    if suppressed:
        detail = ", ".join(f"{count} {reason}" for reason, count in sorted(suppressed.items()))
        warnings.append(f"Focused review mode hid {sum(suppressed.values())} finding(s): {detail}.")
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
