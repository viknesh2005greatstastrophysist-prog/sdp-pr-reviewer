from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import platform
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .agents import SPECIALIZATIONS, LLMReviewAgent
from .aggregator import aggregate
from .llm import OpenAICompatibleClient
from .models import AgentRun, Finding, ReviewResult
from .parser import parse_diff
from .pipeline import review_diff
from .settings import Settings
from .verifier import PROMPT_VERSION as VERIFIER_PROMPT_VERSION
from .verifier import verify_findings


class EvaluationCase(BaseModel):
    case_id: str
    diff: str
    human_findings: list[Finding] = Field(default_factory=list)
    repo_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseRecord(BaseModel):
    """What one strategy produced for one case (the raw material for every table)."""

    findings: list[Finding] = Field(default_factory=list)
    agent_runs: list[AgentRun] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    latency_ms: float = 0.0
    passes: int = 1
    initial_failures: int = 0
    # Verification pass (one call per case): finding_key -> grounded. Empty if not run.
    verified: bool = False
    verdicts: dict[str, bool] = Field(default_factory=dict)
    verify_error: str | None = None


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
    # Failures seen on the first pass, before per-case retries (agent_failures is after).
    initial_agent_failures: int = 0
    cases_retried: int = 0
    # The flat counts above use the "category" policy; the same run under the other policy:
    location_only: PolicyMetrics | None = None
    # Finding and human comment inside the same diff hunk (a coarser, "right neighbourhood" test).
    hunk_level: PolicyMetrics | None = None
    # Micro precision of the top-k findings of each PR under the location-only policy.
    precision_at_k: dict[str, float] = Field(default_factory=dict)
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
    ground_truth: str = "actionable"
    run_config: dict[str, Any] = Field(default_factory=dict)
    # strategy -> case_id -> the exact findings produced (full review mode), so any filtering
    # or re-scoring can be replayed offline without new LLM calls.
    predictions: dict[str, dict[str, CaseRecord]] = Field(default_factory=dict)


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


def limit_per_repo(cases: list[EvaluationCase], per_repo: int) -> list[EvaluationCase]:
    """The first `per_repo` cases of each repository, in corpus order (deterministic)."""
    seen: dict[str, int] = {}
    kept = []
    for case in cases:
        repo = case.metadata.get("repo", "")
        if seen.get(repo, 0) < per_repo:
            seen[repo] = seen.get(repo, 0) + 1
            kept.append(case)
    return kept


def ground_truth(case: EvaluationCase, subset: str = "actionable") -> list[Finding]:
    """The human comments that count as expected findings.

    "actionable": comments the LLM classifier judged actionable (all comments when the corpus
    has no labels). "addressed": actionable comments that the PR later dealt with (outdated or
    line changed). "all": every kept comment.
    """
    if subset == "all":
        return list(case.human_findings)
    labels = case.metadata.get("finding_labels", [])
    kept: list[Finding] = []
    for index, finding in enumerate(case.human_findings):
        label = labels[index] if index < len(labels) else {}
        if label.get("actionable", True) is False:
            continue
        if subset == "addressed" and not label.get("addressed"):
            continue
        kept.append(finding)
    return kept


def hunk_policy(case: EvaluationCase) -> Callable[[Finding, Finding], bool]:
    """Match when both lines fall inside one diff hunk of the same file."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    for item in parse_diff(case.diff):
        for hunk in item.hunks:
            end = hunk.new_start + max(hunk.new_count, 1) - 1
            ranges.setdefault(item.path, []).append((hunk.new_start, end))

    def same_hunk(predicted: Finding, expected: Finding) -> bool:
        if predicted.path != expected.path:
            return False
        return any(
            lo <= predicted.line <= hi and lo <= expected.line <= hi
            for lo, hi in ranges.get(expected.path, [])
        )

    return same_hunk


PRECISION_KS = (1, 3, 5)


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
Sleep = Callable[[float], Awaitable[None]]


def failed_agents(result: ReviewResult) -> list[str]:
    return [run.agent for run in result.agent_runs if run.status != "success"]


def merge_results(first: ReviewResult, again: ReviewResult) -> ReviewResult:
    """Combine a first pass with a re-run of only its failed agents.

    Successful runs from the first pass are kept, failed ones are replaced by the re-run, and the
    findings are aggregated again (static findings repeat identically and collapse).
    """
    reran = {run.agent for run in again.agent_runs}
    runs = [run for run in first.agent_runs if run.agent not in reran] + again.agent_runs
    findings = aggregate(first.findings + again.findings, again.files or first.files)
    return again.model_copy(
        update={
            "findings": findings,
            "agent_runs": runs,
            "warnings": list(dict.fromkeys(first.warnings + again.warnings)),
        }
    )


async def run_case(
    strategy: Strategy,
    case: EvaluationCase,
    *,
    max_passes: int = 1,
    retry_wait: float = 30.0,
    sleep: Sleep = asyncio.sleep,
) -> CaseRecord:
    """Run one case; re-run only the agents that failed (up to `max_passes` passes in total).

    Waits between passes grow (retry_wait, 3x, 9x ...) so a per-minute quota can recover.
    Only time spent inside the strategy counts as latency, not the waits.
    """
    started = time.perf_counter()
    result = await strategy(case)
    elapsed = time.perf_counter() - started
    initial = len(failed_agents(result))
    passes = 1
    rerun = getattr(strategy, "rerun", None)
    while passes < max_passes and rerun is not None and (names := failed_agents(result)):
        await sleep(retry_wait * 3 ** (passes - 1))
        started = time.perf_counter()
        again = await rerun(case, names)
        elapsed += time.perf_counter() - started
        result = merge_results(result, again)
        passes += 1
    return CaseRecord(
        findings=result.findings,
        agent_runs=result.agent_runs,
        warnings=result.warnings,
        latency_ms=elapsed * 1000,
        passes=passes,
        initial_failures=initial,
    )


def metrics_from_records(
    name: str,
    cases: list[EvaluationCase],
    records: dict[str, CaseRecord],
    *,
    subset: str = "actionable",
    transform: Callable[[CaseRecord], list[Finding]] | None = None,
) -> tuple[StrategyMetrics, list[Finding]]:
    """Score saved predictions. `transform` (e.g. the focused review mode) receives each saved
    record and returns the findings to match. Also returns the false positives (category
    policy) for adjudication."""
    tp = fp = fn = 0
    loc = [0, 0, 0]
    hunk = [0, 0, 0]
    at_k = {k: [0, 0] for k in PRECISION_KS}  # hits, returned
    by_category: dict[str, list[int]] = {}
    elapsed = 0.0
    false_positives: list[Finding] = []
    runs = failures = failed_cases = initial = retried = 0
    for case in cases:
        record = records[case.case_id]
        findings = transform(record) if transform else record.findings
        expected = ground_truth(case, subset)
        elapsed += record.latency_ms / 1000
        case_failures = sum(run.status != "success" for run in record.agent_runs)
        runs += len(record.agent_runs)
        failures += case_failures
        failed_cases += bool(case_failures)
        initial += record.initial_failures
        retried += record.passes > 1
        matched, false_pos, missed = match_detail(findings, expected)
        tp += len(matched)
        fp += len(false_pos)
        fn += len(missed)
        false_positives.extend(false_pos)
        for group, index in ((matched, 0), (false_pos, 1), (missed, 2)):
            for item in group:
                by_category.setdefault(item.category, [0, 0, 0])[index] += 1
        l_matched, l_false, l_missed = match_detail(findings, expected, location_matches)
        loc[0] += len(l_matched)
        loc[1] += len(l_false)
        loc[2] += len(l_missed)
        h_matched, h_false, h_missed = match_detail(findings, expected, hunk_policy(case))
        hunk[0] += len(h_matched)
        hunk[1] += len(h_false)
        hunk[2] += len(h_missed)
        for k in PRECISION_KS:
            top = findings[:k]
            at_k[k][0] += len(match_detail(top, expected, location_matches)[0])
            at_k[k][1] += len(top)
    overall = prf(tp, fp, fn)
    metrics = StrategyMetrics(
        strategy=name,
        cases=len(cases),
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=overall.precision,
        recall=overall.recall,
        f1=overall.f1,
        mean_latency_ms=elapsed * 1000 / len(cases) if cases else 0.0,
        location_only=prf(*loc),
        hunk_level=prf(*hunk),
        precision_at_k={
            f"p@{k}": (hits / returned if returned else 0.0) for k, (hits, returned) in at_k.items()
        },
        per_category={key: prf(*by_category[key]) for key in sorted(by_category)},
        agent_runs=runs,
        agent_failures=failures,
        cases_with_agent_failure=failed_cases,
        initial_agent_failures=initial,
        cases_retried=retried,
    )
    return metrics, false_positives


def _save_checkpoint(path: Path, predictions: dict[str, dict[str, CaseRecord]]) -> None:
    payload = {
        name: {case_id: rec.model_dump(mode="json") for case_id, rec in records.items()}
        for name, records in predictions.items()
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


class QuotaExhausted(RuntimeError):
    """Raised when consecutive cases fail on every agent: stop instead of burning quota."""


def case_is_clean(
    predictions: dict[str, dict[str, CaseRecord]], names: list[str], case_id: str
) -> bool:
    """True when no agent run and no verification of the case failed, in any named strategy."""
    for name in names:
        record = predictions[name][case_id]
        if record.verify_error or any(run.status != "success" for run in record.agent_runs):
            return False
    return True


def drop_failed_records(predictions: dict[str, dict[str, CaseRecord]]) -> int:
    """Forget records whose agents or verification failed so a resume re-runs exactly those."""
    dropped = 0
    for records in predictions.values():
        for case_id in [
            cid
            for cid, rec in records.items()
            if any(run.status != "success" for run in rec.agent_runs) or rec.verify_error
        ]:
            del records[case_id]
            dropped += 1
    return dropped


def _load_checkpoint(path: Path) -> dict[str, dict[str, CaseRecord]]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        name: {case_id: CaseRecord.model_validate(rec) for case_id, rec in records.items()}
        for name, records in raw.items()
    }


async def verify_record(
    client: OpenAICompatibleClient,
    case: EvaluationCase,
    record: CaseRecord,
    *,
    passes: int = 3,
    wait: float = 30.0,
    sleep: Sleep = asyncio.sleep,
) -> None:
    """Run the verification pass on a case's full prediction and store the verdicts."""
    files = parse_diff(case.diff)
    for attempt in range(passes):
        outcome = await verify_findings(client, files, record.findings)
        if outcome.error is None:
            record.verdicts, record.verify_error, record.verified = outcome.verdicts, None, True
            return
        record.verify_error = outcome.error
        if attempt + 1 < passes:
            await sleep(wait * 3**attempt)
    record.verified = True  # attempted; verify_error says it failed (findings stay unverified)


async def evaluate(
    cases: list[EvaluationCase],
    strategies: dict[str, Strategy],
    *,
    not_run: dict[str, str] | None = None,
    environment: dict[str, Any] | None = None,
    case_delay: float = 0.0,
    usage: dict[str, tuple[int, int]] | None = None,
    max_passes: int = 1,
    retry_wait: float = 30.0,
    sleep: Sleep = asyncio.sleep,
    subset: str = "actionable",
    run_config: dict[str, Any] | None = None,
    checkpoint: Path | None = None,
    verifier: OpenAICompatibleClient | None = None,
    verify_passes: int = 3,
    redo_failed: bool = False,
    stop_after_dead_cases: int = 2,
) -> EvaluationReport:
    predictions = _load_checkpoint(checkpoint) if checkpoint else {}
    if redo_failed:
        drop_failed_records(predictions)
    for name in strategies:
        predictions.setdefault(name, {})
    streaks = dict.fromkeys(strategies, 0)
    stopped: str | None = None
    first = True
    # Case-major: every strategy finishes a case before the next case starts, so a run that
    # stops early (quota) still has complete, comparable cases.
    try:
        for case in cases:
            for name, strategy in strategies.items():
                records = predictions[name]
                wants_verify = verifier is not None and hasattr(strategy, "rerun")
                done = records.get(case.case_id)
                if done is not None and (done.verified or not wants_verify):
                    continue  # resumed from the checkpoint
                if case_delay and not first:
                    await sleep(case_delay)
                first = False
                if done is None:
                    done = records[case.case_id] = await run_case(
                        strategy, case, max_passes=max_passes, retry_wait=retry_wait, sleep=sleep
                    )
                if wants_verify:
                    await verify_record(
                        verifier, case, done, passes=verify_passes, wait=retry_wait, sleep=sleep
                    )
                if checkpoint:
                    _save_checkpoint(checkpoint, predictions)
                dead = bool(done.agent_runs) and all(r.status != "success" for r in done.agent_runs)
                streaks[name] = streaks[name] + 1 if dead else 0
                if streaks[name] >= stop_after_dead_cases:
                    raise QuotaExhausted(
                        f"{name}: every agent failed on {streaks[name]} consecutive cases "
                        f"(last: {case.case_id}); stopped to protect quota. "
                        "Resume with --redo-failed."
                    )
    except QuotaExhausted as exc:
        stopped = str(exc)
        # Cases that died are not results: forget them so the report only holds real runs.
        for records in predictions.values():
            for case_id in [
                cid
                for cid, rec in records.items()
                if rec.agent_runs and all(r.status != "success" for r in rec.agent_runs)
            ]:
                del records[case_id]
        if checkpoint:
            _save_checkpoint(checkpoint, predictions)
        cases = [c for c in cases if all(c.case_id in predictions[n] for n in strategies)]
    reports: list[StrategyMetrics] = []
    adjudication: dict[str, list[Finding]] = {}
    for name in strategies:
        metrics, false_positives = metrics_from_records(
            name, cases, predictions[name], subset=subset
        )
        token_usage = (usage if usage is not None else {}).get(name, (None, None))
        metrics.prompt_tokens, metrics.completion_tokens = token_usage
        reports.append(metrics)
        adjudication[name] = false_positives
    return EvaluationReport(
        corpus_cases=len(cases),
        generated_at_epoch=int(time.time()),
        matching_policy=POLICY_DESCRIPTIONS["category"],
        matching_policies=POLICY_DESCRIPTIONS,
        strategies=reports,
        strategies_not_run=not_run or {},
        environment=environment or {},
        false_positive_adjudication=adjudication,
        ground_truth=subset,
        run_config={**(run_config or {}), **({"stopped_early": stopped} if stopped else {})},
        predictions={
            name: {c.case_id: predictions[name][c.case_id] for c in cases} for name in strategies
        },
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


class StaticStrategy:
    async def __call__(self, case: EvaluationCase) -> ReviewResult:
        return await review_diff(case.diff, case.repo_path, agents=[], review_mode="full")


class AgentStrategy:
    """Runs agents on a case in full review mode (focusing is replayed offline).

    `rerun` repeats only the named agents, which is what per-case retry needs.
    """

    def __init__(
        self,
        agents: list[LLMReviewAgent],
        *,
        timeout: float,
        sequential: bool = False,
        agent_delay: float = 0.0,
    ):
        self.agents = agents
        self.timeout = timeout
        self.sequential = sequential
        self.agent_delay = agent_delay

    async def __call__(self, case: EvaluationCase, only: set[str] | None = None) -> ReviewResult:
        chosen = [a for a in self.agents if only is None or a.category in only]
        return await review_diff(
            case.diff,
            case.repo_path,
            chosen,
            agent_timeout=self.timeout,
            review_mode="full",
            sequential_agents=self.sequential,
            agent_delay=self.agent_delay,
        )

    async def rerun(self, case: EvaluationCase, names: list[str]) -> ReviewResult:
        return await self(case, only=set(names))


def build_strategies(
    config: Settings,
    *,
    sequential: bool = False,
    agent_delay: float = 0.0,
    agent_timeout: float | None = None,
) -> dict[str, Strategy]:
    timeout = agent_timeout or config.agent_timeout
    strategies: dict[str, Strategy] = {"static-only": StaticStrategy()}
    if config.llm_api_key:
        single_client = OpenAICompatibleClient(
            api_key=config.llm_api_key,
            model=config.llm_model,
            base_url=config.llm_base_url,
            timeout=timeout,
        )
        hybrid_client = OpenAICompatibleClient(
            api_key=config.llm_api_key,
            model=config.llm_model,
            base_url=config.llm_base_url,
            timeout=timeout,
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
        strategies["single-agent"] = AgentStrategy([generalist], timeout=timeout)
        strategies["hybrid-multi-agent"] = AgentStrategy(
            specialists, timeout=timeout, sequential=sequential, agent_delay=agent_delay
        )
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
    parser.add_argument("--only-split", choices=("dev", "test"), help="evaluate one split only")
    parser.add_argument(
        "--splits-first",
        help="comma-separated split order, e.g. test,dev: run those splits first, in that order",
    )
    parser.add_argument(
        "--per-repo",
        type=int,
        help="evaluate only the first N cases of every repository (quota-limited runs)",
    )
    parser.add_argument(
        "--case-delay", type=float, default=0.0, help="seconds between cases (rate limits)"
    )
    parser.add_argument(
        "--sequential-agents",
        action="store_true",
        help="run the specialists one after another within a case (rate-limited providers)",
    )
    parser.add_argument("--agent-delay", type=float, default=0.0, help="seconds between agents")
    parser.add_argument("--agent-timeout", type=float, help="seconds per agent (default: settings)")
    parser.add_argument(
        "--max-passes", type=int, default=3, help="passes per case; failed agents are re-run"
    )
    parser.add_argument(
        "--retry-wait", type=float, default=30.0, help="wait before the first re-run (x3 each)"
    )
    parser.add_argument(
        "--no-verify", action="store_true", help="skip the verification pass (one call per case)"
    )
    parser.add_argument(
        "--redo-failed",
        action="store_true",
        help="drop checkpointed cases with failed agents/verification and run them again",
    )
    parser.add_argument(
        "--clean-only",
        action="store_true",
        help="with --report-only: also drop cases where any agent run or verification failed",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="no LLM calls: report only the cases the checkpoint already holds for every strategy",
    )
    parser.add_argument("--checkpoint", type=Path, help="resume file; finished cases are kept")
    parser.add_argument(
        "--subset", choices=("actionable", "addressed", "all"), default="actionable"
    )
    args = parser.parse_args()
    config = Settings.from_env()
    strategies = build_strategies(
        config,
        sequential=args.sequential_agents,
        agent_delay=args.agent_delay,
        agent_timeout=args.agent_timeout,
    )
    if args.strategies:
        wanted = {item.strip() for item in args.strategies.split(",") if item.strip()}
        strategies = {name: run for name, run in strategies.items() if name in wanted}
    not_run = {
        name: "LLM_API_KEY not set; agent strategy was not run"
        for name in ("single-agent", "hybrid-multi-agent")
        if name not in strategies
    }
    cases = load_jsonl(args.corpus)
    if args.only_split:
        cases = [c for c in cases if c.metadata.get("split") == args.only_split]
    if args.per_repo:
        cases = limit_per_repo(cases, args.per_repo)
    if args.splits_first:
        order = {name.strip(): i for i, name in enumerate(args.splits_first.split(","))}
        cases = sorted(cases, key=lambda c: order.get(c.metadata.get("split"), len(order)))
    if args.report_only:
        if not args.checkpoint:
            raise SystemExit("--report-only needs --checkpoint")
        held = _load_checkpoint(args.checkpoint)
        wanted = [name for name in strategies if name != "static-only"] or list(strategies)
        cases = [c for c in cases if all(c.case_id in held.get(n, {}) for n in wanted)]
        if args.clean_only:
            cases = [c for c in cases if case_is_clean(held, wanted, c.case_id)]
        if not cases:
            raise SystemExit("checkpoint holds no complete case for these strategies")
    if args.limit:
        cases = cases[: args.limit]
    verifier = None
    if config.llm_api_key and not args.no_verify:
        verifier = OpenAICompatibleClient(
            api_key=config.llm_api_key,
            model=config.llm_model,
            base_url=config.llm_base_url,
            timeout=args.agent_timeout or config.agent_timeout,
        )
    report = asyncio.run(
        evaluate(
            cases,
            strategies,
            not_run=not_run,
            environment=collect_environment(config, args.corpus),
            case_delay=args.case_delay,
            usage=_LiveUsage(),
            max_passes=max(1, args.max_passes),
            retry_wait=args.retry_wait,
            subset=args.subset,
            checkpoint=args.checkpoint,
            verifier=verifier,
            redo_failed=args.redo_failed,
            run_config={
                "case_delay_s": args.case_delay,
                "sequential_agents": args.sequential_agents,
                "agent_delay_s": args.agent_delay,
                "agent_timeout_s": args.agent_timeout or config.agent_timeout,
                "max_passes": max(1, args.max_passes),
                "retry_wait_s": args.retry_wait,
                "review_mode_for_predictions": "full",
            },
        )
    )
    if verifier is not None:
        report.environment["verifier_tokens"] = {
            "prompt": verifier.prompt_tokens,
            "completion": verifier.completion_tokens,
            "model": verifier.model,
            "prompt_version": VERIFIER_PROMPT_VERSION,
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"Wrote evaluation for {report.corpus_cases} case(s) to {args.output}")


if __name__ == "__main__":
    main()
