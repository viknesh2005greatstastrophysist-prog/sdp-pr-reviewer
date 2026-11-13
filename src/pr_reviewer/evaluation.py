from __future__ import annotations

import argparse
import asyncio
import hashlib
import platform
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agents import SPECIALIZATIONS, LLMReviewAgent
from .llm import OpenAICompatibleClient
from .models import Finding, ReviewResult
from .pipeline import review_diff
from .settings import Settings


class EvaluationCase(BaseModel):
    case_id: str
    diff: str
    human_findings: list[Finding] = Field(default_factory=list)
    repo_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PolicyMetrics(BaseModel):
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float


class StrategyMetrics(BaseModel):
    strategy: str
    cases: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float
    recall: float
    f1: float
    mean_latency_ms: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    estimated_cost_usd: float | None = None
    # Agent health: a strategy whose agents failed (quota, timeout) silently degrades to
    # static-only results, so failures are reported next to the metrics they distort.
    agent_runs: int = 0
    agent_failures: int = 0
    cases_with_agent_failure: int = 0
    # The flat counts above use the "category" policy; the same run under the other policy:
    location_only: PolicyMetrics | None = None
    per_category: dict[str, PolicyMetrics] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    corpus_cases: int
    generated_at_epoch: int
    matching_policy: str
    matching_policies: dict[str, str] = Field(default_factory=dict)
    strategies: list[StrategyMetrics]
    strategies_not_run: dict[str, str] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    false_positive_adjudication: dict[str, list[Finding]]


def load_jsonl(path: str | Path) -> list[EvaluationCase]:
    cases: list[EvaluationCase] = []
    with Path(path).open(encoding="utf-8") as stream:
        for number, row in enumerate(stream, 1):
            if row.strip():
                try:
                    cases.append(EvaluationCase.model_validate_json(row))
                except ValueError as exc:
                    raise ValueError(f"Invalid corpus row {number}: {exc}") from exc
    if not cases:
        raise ValueError("Evaluation corpus is empty; metrics will not be fabricated")
    return cases


def location_matches(predicted: Finding, expected: Finding) -> bool:
    return predicted.path == expected.path and abs(predicted.line - expected.line) <= 2


def matches(predicted: Finding, expected: Finding) -> bool:
    return location_matches(predicted, expected) and predicted.category == expected.category


POLICIES: dict[str, Callable[[Finding, Finding], bool]] = {
    "category": matches,
    "location-only": location_matches,
}
POLICY_DESCRIPTIONS = {
    "category": "same path and category, within two lines; greedy one-to-one matching",
    "location-only": "same path, within two lines, any category; greedy one-to-one matching",
}


def match_detail(
    predicted: list[Finding],
    expected: list[Finding],
    policy: Callable[[Finding, Finding], bool] = matches,
) -> tuple[list[Finding], list[Finding], list[Finding]]:
    """Return (matched expected findings, false positives, missed expected findings)."""
    unmatched = set(range(len(expected)))
    false_positives: list[Finding] = []
    matched: list[Finding] = []
    for finding in predicted:
        match = next(
            (index for index in sorted(unmatched) if policy(finding, expected[index])), None
        )
        if match is None:
            false_positives.append(finding)
        else:
            unmatched.remove(match)
            matched.append(expected[match])
    return matched, false_positives, [expected[index] for index in sorted(unmatched)]


def score(predicted: list[Finding], expected: list[Finding]) -> tuple[int, int, int, list[Finding]]:
    matched, false_positives, missed = match_detail(predicted, expected)
    return len(matched), len(false_positives), len(missed), false_positives


def prf(tp: int, fp: int, fn: int) -> PolicyMetrics:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return PolicyMetrics(
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=precision,
        recall=recall,
        f1=f1,
    )


Strategy = Callable[[EvaluationCase], Awaitable[ReviewResult]]


async def evaluate(
    cases: list[EvaluationCase],
    strategies: dict[str, Strategy],
    *,
    not_run: dict[str, str] | None = None,
    environment: dict[str, Any] | None = None,
    case_delay: float = 0.0,
    usage: dict[str, tuple[int, int]] | None = None,
) -> EvaluationReport:
    reports: list[StrategyMetrics] = []
    adjudication: dict[str, list[Finding]] = {}
    for name, strategy in strategies.items():
        tp = fp = fn = 0
        loc = [0, 0, 0]
        by_category: dict[str, list[int]] = {}
        elapsed = 0.0
        strategy_false_positives: list[Finding] = []
        runs = failures = failed_cases = 0
        for index, case in enumerate(cases):
            if case_delay and index:
                await asyncio.sleep(case_delay)
            started = time.perf_counter()
            result = await strategy(case)
            elapsed += time.perf_counter() - started
            case_failures = sum(run.status != "success" for run in result.agent_runs)
            runs += len(result.agent_runs)
            failures += case_failures
            failed_cases += bool(case_failures)
            matched, false_pos, missed = match_detail(result.findings, case.human_findings)
            tp += len(matched)
            fp += len(false_pos)
            fn += len(missed)
            strategy_false_positives.extend(false_pos)
            for group, index in ((matched, 0), (false_pos, 1), (missed, 2)):
                for item in group:
                    by_category.setdefault(item.category, [0, 0, 0])[index] += 1
            l_matched, l_false, l_missed = match_detail(
                result.findings, case.human_findings, location_matches
            )
            loc[0] += len(l_matched)
            loc[1] += len(l_false)
            loc[2] += len(l_missed)
        overall = prf(tp, fp, fn)
        reports.append(
            StrategyMetrics(
                strategy=name,
                cases=len(cases),
                true_positives=tp,
                false_positives=fp,
                false_negatives=fn,
                precision=overall.precision,
                recall=overall.recall,
                f1=overall.f1,
                mean_latency_ms=elapsed * 1000 / len(cases),
                location_only=prf(*loc),
                per_category={key: prf(*by_category[key]) for key in sorted(by_category)},
                agent_runs=runs,
                agent_failures=failures,
                cases_with_agent_failure=failed_cases,
                prompt_tokens=(usage if usage is not None else {}).get(name, (None, None))[0],
                completion_tokens=(usage if usage is not None else {}).get(name, (None, None))[1],
            )
        )
        adjudication[name] = strategy_false_positives
    return EvaluationReport(
        corpus_cases=len(cases),
        generated_at_epoch=int(time.time()),
        matching_policy=POLICY_DESCRIPTIONS["category"],
        matching_policies=POLICY_DESCRIPTIONS,
        strategies=reports,
        strategies_not_run=not_run or {},
        environment=environment or {},
        false_positive_adjudication=adjudication,
    )


def _tool_version(name: str) -> str | None:
    if shutil.which(name) is None:
        return None
    try:
        process = subprocess.run(  # noqa: S603
            [name, "--version"], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (process.stdout.strip() or process.stderr.strip()) or None


def _git_commit() -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        process = subprocess.run(  # noqa: S603
            [git, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            cwd=Path(__file__).resolve().parent,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return process.stdout.strip() or None if process.returncode == 0 else None


def collect_environment(config: Settings, corpus: Path) -> dict[str, Any]:
    ruff = _tool_version("ruff")
    semgrep = _tool_version("semgrep")
    return {
        "model_id": config.llm_model if config.llm_api_key else None,
        "ruff_version": ruff,
        "semgrep_available": semgrep is not None,
        "semgrep_version": semgrep,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "corpus_path": str(corpus),
        "corpus_sha256": hashlib.sha256(corpus.read_bytes()).hexdigest(),
        "git_commit": _git_commit(),
    }


def build_strategies(config: Settings) -> dict[str, Strategy]:
    async def static_only(case: EvaluationCase) -> ReviewResult:
        return await review_diff(case.diff, case.repo_path, agents=[])

    strategies: dict[str, Strategy] = {"static-only": static_only}
    if config.llm_api_key:
        single_client = OpenAICompatibleClient(
            api_key=config.llm_api_key, model=config.llm_model, base_url=config.llm_base_url
        )
        hybrid_client = OpenAICompatibleClient(
            api_key=config.llm_api_key, model=config.llm_model, base_url=config.llm_base_url
        )
        generalist = LLMReviewAgent(
            "generalist",
            single_client,
            focus=(
                "Review for correctness, performance, style, and architecture. "
                "Prioritize precision."
            ),
        )
        specialists = [LLMReviewAgent(category, hybrid_client) for category in SPECIALIZATIONS]

        async def single_agent(case: EvaluationCase) -> ReviewResult:
            return await review_diff(
                case.diff, case.repo_path, [generalist], agent_timeout=config.agent_timeout
            )

        async def hybrid(case: EvaluationCase) -> ReviewResult:
            return await review_diff(
                case.diff, case.repo_path, specialists, agent_timeout=config.agent_timeout
            )

        strategies.update({"single-agent": single_agent, "hybrid-multi-agent": hybrid})
        TOKEN_CLIENTS.update({"single-agent": single_client, "hybrid-multi-agent": hybrid_client})
    return strategies


# Strategy name -> LLM client, so observed token usage can be reported per strategy.
TOKEN_CLIENTS: dict[str, OpenAICompatibleClient] = {}


class _LiveUsage(dict):
    """Reads token counters at report time, after the strategy has run."""

    def get(self, key, default=None):
        client = TOKEN_CLIENTS.get(key)
        return (client.prompt_tokens, client.completion_tokens) if client else default


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate reviewer strategies against a held-out JSONL corpus"
    )
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--strategies", help="comma-separated subset, e.g. single-agent,hybrid-multi-agent"
    )
    parser.add_argument("--limit", type=int, help="evaluate only the first N cases")
    parser.add_argument(
        "--case-delay", type=float, default=0.0, help="seconds between cases (rate limits)"
    )
    args = parser.parse_args()
    config = Settings.from_env()
    strategies = build_strategies(config)
    if args.strategies:
        wanted = {item.strip() for item in args.strategies.split(",") if item.strip()}
        strategies = {name: run for name, run in strategies.items() if name in wanted}
    not_run = {
        name: "LLM_API_KEY not set; agent strategy was not run"
        for name in ("single-agent", "hybrid-multi-agent")
        if name not in strategies
    }
    cases = load_jsonl(args.corpus)
    if args.limit:
        cases = cases[: args.limit]
    report = asyncio.run(
        evaluate(
            cases,
            strategies,
            not_run=not_run,
            environment=collect_environment(config, args.corpus),
            case_delay=args.case_delay,
            usage=_LiveUsage(),
        )
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote evaluation for {report.corpus_cases} case(s) to {args.output}")


if __name__ == "__main__":
    main()
