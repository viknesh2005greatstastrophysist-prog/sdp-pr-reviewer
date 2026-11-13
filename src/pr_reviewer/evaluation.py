from __future__ import annotations

import argparse
import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

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


class EvaluationReport(BaseModel):
    corpus_cases: int
    generated_at_epoch: int
    matching_policy: str
    strategies: list[StrategyMetrics]
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


def matches(predicted: Finding, expected: Finding) -> bool:
    return (
        predicted.path == expected.path
        and abs(predicted.line - expected.line) <= 2
        and predicted.category == expected.category
    )


def score(predicted: list[Finding], expected: list[Finding]) -> tuple[int, int, int, list[Finding]]:
    unmatched = set(range(len(expected)))
    false_positives: list[Finding] = []
    true_positives = 0
    for finding in predicted:
        match = next((index for index in unmatched if matches(finding, expected[index])), None)
        if match is None:
            false_positives.append(finding)
        else:
            unmatched.remove(match)
            true_positives += 1
    return true_positives, len(false_positives), len(unmatched), false_positives


Strategy = Callable[[EvaluationCase], Awaitable[ReviewResult]]


async def evaluate(
    cases: list[EvaluationCase], strategies: dict[str, Strategy]
) -> EvaluationReport:
    reports: list[StrategyMetrics] = []
    adjudication: dict[str, list[Finding]] = {}
    for name, strategy in strategies.items():
        tp = fp = fn = 0
        elapsed = 0.0
        strategy_false_positives: list[Finding] = []
        for case in cases:
            started = time.perf_counter()
            result = await strategy(case)
            elapsed += time.perf_counter() - started
            case_tp, case_fp, case_fn, case_false = score(result.findings, case.human_findings)
            tp += case_tp
            fp += case_fp
            fn += case_fn
            strategy_false_positives.extend(case_false)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        reports.append(
            StrategyMetrics(
                strategy=name,
                cases=len(cases),
                true_positives=tp,
                false_positives=fp,
                false_negatives=fn,
                precision=precision,
                recall=recall,
                f1=f1,
                mean_latency_ms=elapsed * 1000 / len(cases),
            )
        )
        adjudication[name] = strategy_false_positives
    return EvaluationReport(
        corpus_cases=len(cases),
        generated_at_epoch=int(time.time()),
        matching_policy="same path and category, within two lines; greedy one-to-one matching",
        strategies=reports,
        false_positive_adjudication=adjudication,
    )


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
    return strategies


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate reviewer strategies against a held-out JSONL corpus"
    )
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = asyncio.run(evaluate(load_jsonl(args.corpus), build_strategies(Settings.from_env())))
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote evaluation for {report.corpus_cases} case(s) to {args.output}")


if __name__ == "__main__":
    main()
