import asyncio
import base64
import json

import httpx
import pytest

from pr_reviewer.corpus import (
    build_corpus,
    classify_comment,
    comment_to_finding,
    keep_added_line_comments,
    python_only_diff,
    safe_relative_path,
    safe_target,
    select_comments,
)
from pr_reviewer.evaluation import (
    EvaluationCase,
    evaluate,
    location_matches,
    match_detail,
    prf,
)
from pr_reviewer.models import Finding, ReviewResult, Severity

DIFF = """diff --git a/pkg/mod.py b/pkg/mod.py
index 111..222 100644
--- a/pkg/mod.py
+++ b/pkg/mod.py
@@ -1,3 +1,5 @@
 import os
+import sys
+x = 1
 y = 2
 z = 3
diff --git a/README.md b/README.md
index 111..222 100644
--- a/README.md
+++ b/README.md
@@ -1 +1,2 @@
 hi
+there
"""


def comment(**overrides):
    base = {
        "id": 1,
        "user": {"login": "reviewer", "type": "User"},
        "path": "pkg/mod.py",
        "side": "RIGHT",
        "line": 2,
        "original_line": 2,
        "original_commit_id": "c1",
        "body": "this is a bug when x is None",
        "html_url": "https://github.com/o/r/pull/1#discussion_r1",
    }
    base.update(overrides)
    return base


def test_select_comments_filters():
    comments = [
        comment(id=1),
        comment(id=2, user={"login": "ci[bot]", "type": "Bot"}),
        comment(id=3, user={"login": "author", "type": "User"}),
        comment(id=4, in_reply_to_id=1),
        comment(id=5, path="docs/readme.md"),
        comment(id=6, side="LEFT"),
        comment(id=7, original_line=None, line=None),
        comment(id=8, body="   "),
    ]
    kept = select_comments(comments, "author")
    assert [c["id"] for c in kept] == [1]


def test_added_line_filter_and_python_only_diff():
    py_diff = python_only_diff(DIFF)
    assert "README.md" not in py_diff
    kept = keep_added_line_comments(
        [
            comment(id=1, original_line=2),
            comment(id=2, original_line=3),
            comment(id=3, original_line=4),
        ],
        py_diff,
    )
    # lines 2 and 3 are added; line 4 is unchanged context
    assert [c["id"] for c in kept] == [1, 2]


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("This will crash with an exception when None", "bug-hunter"),
        ("This is slow, cache the result for performance", "performance"),
        ("Breaking change to the public api, needs deprecation", "architecture"),
        ("nit: typo in docstring", "style"),
        ("please reconsider", "style"),
    ],
)
def test_classify_comment(text, category):
    assert classify_comment(text) == category


def test_comment_to_finding_fields():
    finding = comment_to_finding(comment(body="x" * 800))
    assert finding.source == "human-review"
    assert finding.severity == Severity.MEDIUM
    assert finding.confidence == 1.0
    assert len(finding.message) == 500
    assert finding.evidence.endswith("discussion_r1")


def test_path_safety(tmp_path):
    assert safe_relative_path("a/b.py") == "a/b.py"
    for bad in ("../x.py", "/etc/passwd", "a/../../x.py", ""):
        assert safe_relative_path(bad) is None
    assert safe_target(tmp_path, "../escape.py") is None
    assert safe_target(tmp_path, "ok/file.py") == (tmp_path / "ok/file.py").resolve()


def _handler(rate_limit_once: bool = False):
    state = {"limited": rate_limit_once}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/repos/o/r/pulls":
            if request.url.params["page"] != "1":
                return httpx.Response(200, json=[])
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 1,
                        "merged_at": "2026-01-01",
                        "user": {"login": "author"},
                        "base": {"sha": "b0"},
                        "html_url": "https://github.com/o/r/pull/1",
                    },
                    {
                        "number": 2,
                        "merged_at": None,
                        "user": {"login": "author"},
                        "base": {"sha": "b0"},
                    },
                ],
            )
        if path == "/repos/o/r/pulls/1/comments":
            if state["limited"]:
                state["limited"] = False
                return httpx.Response(
                    403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}
                )
            return httpx.Response(
                200,
                json=[
                    comment(id=1, original_line=2),
                    comment(id=2, original_line=4),  # not an added line
                    comment(id=3, user={"login": "ci[bot]", "type": "Bot"}),
                ],
            )
        if path == "/repos/o/r/compare/b0...c1":
            assert request.headers["accept"] == "application/vnd.github.v3.diff"
            return httpx.Response(200, text=DIFF)
        if path == "/repos/o/r/contents/pkg/mod.py":
            content = base64.b64encode(b"import os\nimport sys\nx = 1\n").decode()
            return httpx.Response(200, json={"encoding": "base64", "size": 30, "content": content})
        return httpx.Response(404)

    return handle


def test_build_corpus_end_to_end_with_mock_transport(tmp_path):
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    cases = asyncio.run(
        build_corpus(
            ["o/r"],
            token="test-token",  # noqa: S106
            max_prs=5,
            max_cases_per_repo=3,
            repos_dir=tmp_path,
            transport=httpx.MockTransport(_handler(rate_limit_once=True)),
            sleep=fake_sleep,
        )
    )
    assert slept, "403 with x-ratelimit-remaining=0 must trigger a sleep"
    assert len(cases) == 1
    case = cases[0]
    assert [f.line for f in case.human_findings] == [2]
    assert case.metadata["commit_sha"] == "c1"
    assert case.metadata["finding_labels"][0]["label_source"] == "heuristic-category"
    assert (tmp_path / case.case_id / "pkg" / "mod.py").read_text().startswith("import os")
    # row round-trips through the evaluation schema
    EvaluationCase.model_validate_json(json.dumps(case.model_dump()))


def f(path="a.py", line=10, category="style"):
    return Finding(
        path=path,
        line=line,
        category=category,
        severity=Severity.LOW,
        message="m",
        evidence="e",
        source="s",
    )


def test_location_only_policy_ignores_category():
    assert location_matches(f(category="bug-hunter"), f(category="style"))
    assert not location_matches(f(line=14), f())
    assert not location_matches(f(path="b.py"), f())


def test_prf_values():
    metrics = prf(1, 1, 1)
    assert metrics.precision == 0.5
    assert metrics.recall == 0.5
    assert metrics.f1 == 0.5
    assert prf(0, 0, 0).f1 == 0.0


def test_evaluate_reports_both_policies_and_per_category():
    case = EvaluationCase(
        case_id="c",
        diff="",
        human_findings=[f(category="style"), f(line=50, category="bug-hunter")],
    )

    async def strategy(_):
        # right place wrong category, plus one miss-category in the wrong place
        return ReviewResult(
            files=[], findings=[f(line=10, category="bug-hunter"), f(line=90, category="style")]
        )

    report = asyncio.run(
        evaluate([case], {"s": strategy}, not_run={"hybrid": "no key"}, environment={"k": 1})
    )
    metrics = report.strategies[0]
    assert (metrics.true_positives, metrics.false_positives, metrics.false_negatives) == (0, 2, 2)
    assert metrics.location_only.true_positives == 1
    assert metrics.location_only.false_positives == 1
    assert metrics.per_category["style"].false_negatives == 1
    assert metrics.per_category["style"].false_positives == 1
    assert metrics.per_category["bug-hunter"].false_positives == 1
    assert report.strategies_not_run == {"hybrid": "no key"}
    assert report.environment == {"k": 1}


def test_match_detail_one_to_one():
    matched, false_pos, missed = match_detail([f(), f()], [f()], location_matches)
    assert len(matched) == 1
    assert len(false_pos) == 1
    assert missed == []
