"""Corpus split/addressed labels, comment classifier, review mode, verifier, retries, injection.

No network: LLM calls go through httpx.MockTransport.
"""

import asyncio
import json

import httpx
import pytest

from pr_reviewer.agents import BaseReviewAgent, dispatch_runs
from pr_reviewer.corpus import (
    addressed_evidence,
    changed_later_lines,
    is_outdated,
    split_repos,
)
from pr_reviewer.evaluation import (
    CaseRecord,
    EvaluationCase,
    hunk_policy,
    merge_results,
    metrics_from_records,
    run_case,
)
from pr_reviewer.injection import PER_CATEGORY, generate, new_file_diff
from pr_reviewer.labeling import (
    BatchJudgement,
    apply_judgements,
    classify_case,
    cohens_kappa,
    validate_batch,
)
from pr_reviewer.llm import LLMError, OpenAICompatibleClient
from pr_reviewer.models import AgentRun, Finding, ReviewResult, Severity
from pr_reviewer.parser import parse_diff
from pr_reviewer.review_mode import FocusConfig, apply_review_mode
from pr_reviewer.settings import parse_review_mode
from pr_reviewer.verifier import drop_ungrounded, finding_key, verify_findings


def finding(path="a.py", line=1, source="bug-hunter", confidence=0.9, rule_id=None, **extra):
    base = {
        "path": path,
        "line": line,
        "category": "bug-hunter",
        "severity": Severity.MEDIUM,
        "title": f"t{line}",
        "message": "m",
        "evidence": "e",
        "source": source,
        "confidence": confidence,
        "rule_id": rule_id,
    }
    base.update(extra)
    return Finding(**base)


def client_returning(payloads, status=200):
    """OpenAI-compatible client whose server answers with each payload in turn."""
    queue = list(payloads)

    def handle(request):
        body = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(body, int):
            return httpx.Response(body, json={"error": {"message": "x"}})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(body)}}], "usage": {}}
        )

    return OpenAICompatibleClient(
        api_key="k", model="m", transport=httpx.MockTransport(handle), max_retries=0
    )


# ---- split --------------------------------------------------------------------------------


def test_split_is_by_repository_halved_and_order_independent():
    repos = [f"o/r{i}" for i in range(10)]
    first = split_repos(repos)
    assert split_repos(list(reversed(repos))) == first
    assert sorted(set(first.values())) == ["dev", "test"]
    assert sum(v == "dev" for v in first.values()) == 5
    odd = split_repos(repos[:7])
    assert sum(v == "dev" for v in odd.values()) == 4


def test_split_depends_on_seed_not_on_results():
    repos = [f"o/r{i}" for i in range(10)]
    assert split_repos(repos, seed="a") != split_repos(repos, seed="b")


# ---- addressed ----------------------------------------------------------------------------


LATER_DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,3 +1,3 @@
 a = 1
-b = 2
+b = 3
 c = 4
"""


def test_addressed_evidence_outdated_and_changed_later():
    removed = changed_later_lines(LATER_DIFF)
    assert removed == {"pkg/mod.py": {2}}
    base = {"path": "pkg/mod.py", "line": None, "original_line": 2}
    assert is_outdated(base)
    result = addressed_evidence(base, removed)
    assert result == {"outdated": True, "changed_later": True, "addressed": True}
    live = {"path": "pkg/mod.py", "line": 3, "original_line": 3}
    assert addressed_evidence(live, removed)["addressed"] is False
    changed_only = {"path": "pkg/mod.py", "line": 2, "original_line": 2}
    assert addressed_evidence(changed_only, removed) == {
        "outdated": False,
        "changed_later": True,
        "addressed": True,
    }
    # Unknown later diff: not claimed as addressed unless GitHub marked it outdated.
    assert addressed_evidence(live, None) == {
        "outdated": False,
        "changed_later": None,
        "addressed": False,
    }


# ---- classifier ---------------------------------------------------------------------------

CASE_DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
--- /dev/null
+++ b/pkg/mod.py
@@ -0,0 +1,3 @@
+import os
+x = None
+y = x.strip()
"""


def labelled_case():
    return EvaluationCase(
        case_id="c1",
        diff=CASE_DIFF,
        human_findings=[
            finding("pkg/mod.py", 3, category="style"),
            finding("pkg/mod.py", 2, category="style"),
        ],
        metadata={
            "finding_labels": [
                {"comment_text": "this crashes when x is None", "heuristic_category": "bug-hunter"},
                {"comment_text": "thanks!", "heuristic_category": "style"},
            ]
        },
    )


GOOD = {
    "judgements": [
        {"id": 0, "actionable": True, "category": "bug-hunter", "reason": "None deref"},
        {"id": 1, "actionable": False, "category": "style", "reason": "thanks"},
    ]
}


def test_classifier_parses_strict_json_and_applies_labels():
    case = labelled_case()
    judgements = asyncio.run(classify_case(client_returning([GOOD]), case))
    apply_judgements(case, judgements, "model-x")
    labels = case.metadata["finding_labels"]
    assert labels[0]["actionable"] is True and labels[1]["actionable"] is False
    assert labels[0]["label_source"] == "llm-classifier"
    assert labels[0]["classifier_model"] == "model-x"
    assert case.human_findings[0].category == "bug-hunter"
    assert case.metadata["labelled"] is True


def test_classifier_rejects_missing_or_extra_ids_and_retries_once():
    bad = {"judgements": [GOOD["judgements"][0]]}
    with pytest.raises(LLMError):
        validate_batch(BatchJudgement.model_validate(bad), 2)
    # First answer is incomplete, the retry is complete.
    judgements = asyncio.run(classify_case(client_returning([bad, GOOD]), labelled_case()))
    assert set(judgements) == {0, 1}
    with pytest.raises(LLMError):
        asyncio.run(classify_case(client_returning([bad]), labelled_case()))


def test_classifier_rejects_unknown_category():
    broken = {"judgements": [{"id": 0, "actionable": True, "category": "vibes", "reason": "r"}]}
    with pytest.raises(LLMError):
        asyncio.run(classify_case(client_returning([broken]), labelled_case()))


def test_kappa_perfect_and_chance():
    assert cohens_kappa([("a", "a"), ("b", "b")]) == 1.0
    assert cohens_kappa([("a", "b"), ("b", "a")]) == -1.0
    assert cohens_kappa([]) is None


# ---- review mode --------------------------------------------------------------------------


def test_full_mode_keeps_everything():
    items = [finding(source="ruff", rule_id="E501", confidence=1.0), finding(confidence=0.1)]
    kept, suppressed = apply_review_mode(items, "full")
    assert len(kept) == 2 and suppressed == {}


def test_focused_keeps_defect_rules_and_drops_cosmetic_ones():
    items = [
        finding(line=1, source="ruff", rule_id="S608", confidence=1.0),
        finding(line=2, source="ruff", rule_id="B006", confidence=1.0),
        finding(line=3, source="ruff", rule_id="F821", confidence=1.0),
        finding(line=4, source="ruff", rule_id="E999", confidence=1.0),
        finding(line=5, source="ruff", rule_id="F401", confidence=1.0),
        finding(line=6, source="ruff", rule_id="E501", confidence=1.0),
        finding(line=7, source="ruff", rule_id="I001", confidence=1.0),
        finding(line=8, source="ruff", rule_id="UP006", confidence=1.0),
        finding(line=9, source="ruff", rule_id="S101", confidence=1.0),
        finding(line=10, source="semgrep", rule_id="python-shell-true", confidence=1.0),
    ]
    kept, suppressed = apply_review_mode(items, "focused", FocusConfig(max_per_file=99))
    assert sorted(f.line for f in kept) == [1, 2, 3, 4, 10]
    assert suppressed["static rule outside the defect families"] == 5


def test_focused_agent_confidence_threshold_and_caps():
    config = FocusConfig(agent_min_confidence=0.7, max_per_file=2, max_per_pr=3)
    items = [
        finding("a.py", 1, confidence=0.65),
        finding("a.py", 2, confidence=0.9),
        finding("a.py", 3, confidence=0.8),
        finding("a.py", 4, confidence=0.95),
        finding("b.py", 1, confidence=0.9),
        finding("c.py", 1, confidence=0.85),
        finding("c.py", 9, confidence=0.7),
    ]
    kept, suppressed = apply_review_mode(items, "focused", config)
    assert suppressed["agent confidence below threshold"] == 1
    assert suppressed["per-file cap"] == 1  # a.py: three survive the threshold, two are shown
    assert len(kept) == 3 and suppressed["per-PR top-k cap"] == 2
    assert (0.65) not in [f.confidence for f in kept]


def test_agreement_boost_changes_which_finding_survives_the_cap():
    plain = finding("a.py", 10, source="bug-hunter", confidence=0.80)
    backed = finding("a.py", 20, source="bug-hunter", confidence=0.75)
    other_source = finding("a.py", 21, source="ruff", rule_id="B006", confidence=0.5)
    on = FocusConfig(max_per_pr=1, agreement_boost=0.2)
    off = FocusConfig(max_per_pr=1, agreement_boost=0.0)
    items = [plain, backed, other_source]
    kept_on, _ = apply_review_mode(items, "focused", on)
    kept_off, _ = apply_review_mode(items, "focused", off)
    assert [f.line for f in kept_off] == [10]
    assert [f.line for f in kept_on] == [20]


def test_review_mode_setting_is_validated():
    assert parse_review_mode(None) == "focused"
    assert parse_review_mode(" FULL ") == "full"
    with pytest.raises(ValueError):
        parse_review_mode("loud")
    with pytest.raises(ValueError):
        apply_review_mode([], "loud")


# ---- verifier -----------------------------------------------------------------------------


def test_verifier_drops_ungrounded_agent_findings_but_not_static_or_on_error():
    files = parse_diff(CASE_DIFF)
    agent_a = finding("pkg/mod.py", 3, title="real")
    agent_b = finding("pkg/mod.py", 2, title="made up")
    static = finding("pkg/mod.py", 1, source="ruff", rule_id="B006")
    verdicts = {
        "verdicts": [
            {"id": 0, "grounded": True, "reason": "x"},
            {"id": 1, "grounded": False, "reason": "y"},
        ]
    }
    outcome = asyncio.run(
        verify_findings(client_returning([verdicts]), files, [agent_a, agent_b, static])
    )
    kept, dropped = drop_ungrounded([agent_a, agent_b, static], outcome.verdicts)
    assert dropped == 1 and agent_b not in kept and static in kept
    assert outcome.verdicts[finding_key(agent_b)] is False
    failed = asyncio.run(verify_findings(client_returning([500]), files, [agent_a]))
    assert failed.error and failed.verdicts == {}
    assert drop_ungrounded([agent_a], failed.verdicts) == ([agent_a], 0)


# ---- retries ------------------------------------------------------------------------------


def make_result(findings, statuses):
    runs = [
        AgentRun(agent=a, status=s, duration_ms=1, finding_count=0) for a, s in statuses.items()
    ]
    return ReviewResult(files=[], findings=findings, agent_runs=runs)


class FlakyStrategy:
    """Specialist `performance` fails on the first `failures` attempts, then succeeds."""

    def __init__(self, failures):
        self.failures = failures
        self.calls: list[set[str] | None] = []

    async def __call__(self, case, only=None):
        self.calls.append(only)
        names = ["bug-hunter", "performance"] if only is None else sorted(only)
        statuses = {}
        findings = []
        for name in names:
            if name == "performance" and self.failures > 0:
                self.failures -= 1
                statuses[name] = "failed"
            else:
                statuses[name] = "success"
                findings.append(finding(line=1 if name == "bug-hunter" else 5, source=name))
        return make_result(findings, statuses)

    async def rerun(self, case, names):
        return await self(case, only=set(names))


def test_failed_agents_are_rerun_with_growing_waits_and_results_merged():
    strategy = FlakyStrategy(failures=2)
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    case = EvaluationCase(case_id="c", diff="")
    record = asyncio.run(run_case(strategy, case, max_passes=3, retry_wait=10, sleep=fake_sleep))
    assert waits == [10, 30]
    assert strategy.calls == [None, {"performance"}, {"performance"}]
    assert record.passes == 3 and record.initial_failures == 1
    assert all(run.status == "success" for run in record.agent_runs)
    assert sorted(f.line for f in record.findings) == [1, 5]


def test_retry_gives_up_after_max_passes_and_reports_the_failure():
    strategy = FlakyStrategy(failures=99)

    async def no_sleep(seconds):
        pass

    record = asyncio.run(
        run_case(strategy, EvaluationCase(case_id="c", diff=""), max_passes=2, sleep=no_sleep)
    )
    assert record.passes == 2
    assert [r.status for r in record.agent_runs if r.agent == "performance"] == ["failed"]


def test_no_retry_when_everything_succeeds_or_single_pass():
    strategy = FlakyStrategy(failures=0)
    record = asyncio.run(run_case(strategy, EvaluationCase(case_id="c", diff=""), max_passes=3))
    assert record.passes == 1 and strategy.calls == [None]
    flaky = FlakyStrategy(failures=1)
    record = asyncio.run(run_case(flaky, EvaluationCase(case_id="c", diff=""), max_passes=1))
    assert record.passes == 1 and record.initial_failures == 1


def test_merge_results_replaces_only_rerun_agents():
    first = make_result([finding(line=1)], {"a": "success", "b": "failed"})
    again = make_result([finding(line=9, source="b")], {"b": "success"})
    merged = merge_results(first, again)
    assert {r.agent: r.status for r in merged.agent_runs} == {"a": "success", "b": "success"}
    assert sorted(f.line for f in merged.findings) == [1, 9]


class Slow(BaseReviewAgent):
    def __init__(self, name, log):
        self.category = name
        self.log = log

    async def review(self, files):
        self.log.append(f"start {self.category}")
        await asyncio.sleep(0.01)
        self.log.append(f"end {self.category}")
        return []


def test_sequential_dispatch_never_overlaps_agents():
    log: list[str] = []
    agents = [Slow("a", log), Slow("b", log)]
    asyncio.run(dispatch_runs([], agents, 5, sequential=True))
    assert log == ["start a", "end a", "start b", "end b"]
    log.clear()
    asyncio.run(dispatch_runs([], agents, 5))
    assert log[:2] == ["start a", "start b"]


# ---- metrics: hunk and precision@k --------------------------------------------------------

TWO_HUNKS = """diff --git a/m.py b/m.py
--- a/m.py
+++ b/m.py
@@ -1,3 +1,4 @@
 a
+b
 c
 d
@@ -50,3 +51,4 @@
 e
+f
 g
 h
"""


def test_hunk_policy_matches_inside_the_same_hunk_only():
    case = EvaluationCase(case_id="c", diff=TWO_HUNKS)
    same = hunk_policy(case)
    expected = finding("m.py", 2)
    assert same(finding("m.py", 4), expected)
    assert not same(finding("m.py", 52), expected)
    assert not same(finding("other.py", 2), expected)


def test_precision_at_k_uses_ranked_order():
    case = EvaluationCase(
        case_id="c", diff=TWO_HUNKS, human_findings=[finding("m.py", 2, category="style")]
    )
    ranked = [finding("m.py", 40), finding("m.py", 2), finding("m.py", 80)]
    record = CaseRecord(findings=ranked)
    metrics, _ = metrics_from_records("s", [case], {"c": record})
    assert metrics.precision_at_k["p@1"] == 0.0
    assert metrics.precision_at_k["p@3"] == pytest.approx(1 / 3)
    assert metrics.hunk_level.true_positives == 1
    best_first = CaseRecord(findings=[finding("m.py", 2), finding("m.py", 40)])
    metrics, _ = metrics_from_records("s", [case], {"c": best_first})
    assert metrics.precision_at_k["p@1"] == 1.0
    assert metrics.precision_at_k["p@3"] == 0.5


# ---- injection ----------------------------------------------------------------------------

SAMPLE = '''import os


def load(path, mode=None, retries=3):
    """Load a file."""
    handle = open(path)
    try:
        data = handle.read()
    except OSError:
        raise
    for index in range(len(data)):
        if index == 3:
            break
    return data


def other(items, flag=None):
    return [item for item in items if item]
''' + "\n".join(f"# filler {i}" for i in range(40))


def make_corpus(tmp_path):
    root = tmp_path / "src_case"
    (root / "pkg").mkdir(parents=True)
    for index in range(8):
        (root / "pkg" / f"mod{index}.py").write_text(SAMPLE, encoding="utf-8")
    return [
        EvaluationCase(
            case_id="src",
            diff="",
            repo_path=str(root),
            metadata={"repo": "o/r", "split": "dev"},
        )
    ]


def test_injection_is_deterministic_and_records_the_defect_line(tmp_path):
    corpus = make_corpus(tmp_path)
    one = generate(corpus, tmp_path / "out1", seed=7, per_category=2)
    two = generate(corpus, tmp_path / "out2", seed=7, per_category=2)
    assert [c.diff for c in one] == [c.diff for c in two]
    assert [c.metadata["mutation"] for c in one] == [c.metadata["mutation"] for c in two]
    assert {c.metadata["category"] for c in one} == {
        "bug-hunter",
        "performance",
        "style",
        "architecture",
    }
    for case in one:
        line = case.human_findings[0].line
        added = parse_diff(case.diff)[0].added_lines()
        assert line in added
        mutated = tmp_path / "out1" / case.case_id / case.human_findings[0].path
        rows = mutated.read_text(encoding="utf-8").split("\n")
        assert rows[line - 1].strip() == case.metadata["line_after"]
        compile(mutated.read_text(encoding="utf-8"), "x", "exec")  # mutants stay valid Python
    other = generate(corpus, tmp_path / "out3", seed=8, per_category=2)
    assert PER_CATEGORY > 0 and other  # a different seed still produces cases


def test_new_file_diff_parses_as_added_file():
    parsed = parse_diff(new_file_diff("p/q.py", "a = 1\nb = 2\n"))
    assert parsed[0].status == "added" and parsed[0].added_lines() == {1, 2}


def test_quota_detection_survives_list_shaped_error_bodies():
    from pr_reviewer.llm import _quota_exhausted

    listed = httpx.Response(429, json=[{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}])
    assert _quota_exhausted(listed) is False
    billing = httpx.Response(429, json=[{"error": {"code": "insufficient_quota"}}])
    assert _quota_exhausted(billing) is True
    assert _quota_exhausted(httpx.Response(429, text="not json")) is False
