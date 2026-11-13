import asyncio

import pytest

from pr_reviewer.database import ReviewStore
from pr_reviewer.evaluation import EvaluationCase, evaluate, load_jsonl
from pr_reviewer.models import Finding, PullRequestRef, ReviewResult, Severity


def finding(line=2):
    return Finding(
        path="a.py",
        line=line,
        category="bug",
        severity=Severity.HIGH,
        message="bug",
        evidence="code",
        source="human",
    )


def test_evaluation_uses_real_labels():
    case = EvaluationCase(case_id="1", diff="", human_findings=[finding()])

    async def strategy(_):
        return ReviewResult(files=[], findings=[finding(3), finding(20)])

    report = asyncio.run(evaluate([case], {"test": strategy}))
    metrics = report.strategies[0]
    assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (1, 1, 0)
    assert metrics.f1 == pytest.approx(2 / 3)
    assert len(report.false_positive_adjudication["test"]) == 1


def test_empty_evaluation_is_rejected(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="not be fabricated"):
        load_jsonl(path)


def test_review_history_round_trip(tmp_path):
    store = ReviewStore(f"sqlite:///{tmp_path / 'history.db'}")
    store.create_schema()
    ref = PullRequestRef(owner="o", repo="r", number=7, head_sha="abc")
    record_id = store.save(ref, ReviewResult(files=[], findings=[finding()]))
    rows = store.history("o/r")
    assert rows[0]["id"] == record_id
    assert rows[0]["finding_count"] == 1
