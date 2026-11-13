# ruff: noqa: S105, S106  (throwaway test values, not real credentials)
import asyncio

import httpx
import pytest
from test_service_integration import DEMO_DIFF, FakeGitHub

from pr_reviewer import github as github_module
from pr_reviewer.agents import BaseReviewAgent
from pr_reviewer.aggregator import aggregate, rank_key
from pr_reviewer.database import ReviewStore
from pr_reviewer.github import GitHubClient, GitHubError
from pr_reviewer.models import Finding, PullRequestRef, Severity
from pr_reviewer.pipeline import review_diff
from pr_reviewer.service import ReviewService
from pr_reviewer.settings import Settings

REF = PullRequestRef(owner="academic", repo="demo", number=1)
TWO_LINE_DIFF = """diff --git a/app.py b/app.py
new file mode 100644
--- /dev/null
+++ b/app.py
@@ -0,0 +1,3 @@
+def one():
+    return 1
+
"""


TOPICS = ["sql injection", "unclosed socket", "mutable default", "race condition", "dead branch"]


def finding(index, severity, confidence):
    """Findings far apart on distinct topics, so aggregation never merges them."""
    topic = TOPICS[index % len(TOPICS)]
    return Finding(
        path="app.py",
        line=1 + index * 10,
        category="bug-hunter",
        severity=severity,
        title=f"Possible {topic}",
        message=f"The {topic} appears here",
        evidence=f"{topic} code",
        source="bug-hunter",
        confidence=confidence,
    )


# --- Ranking and the confidence threshold ---------------------------------------------------
def test_equal_severity_is_ordered_by_confidence():
    low, high, mid = (
        finding(0, Severity.HIGH, 0.55),
        finding(1, Severity.HIGH, 0.95),
        finding(2, Severity.HIGH, 0.75),
    )
    ranked = aggregate([low, high, mid])
    assert [item.confidence for item in ranked] == [0.95, 0.75, 0.55]


def test_severity_outranks_confidence():
    critical_unsure = finding(1, Severity.CRITICAL, 0.3)
    medium_sure = finding(2, Severity.MEDIUM, 1.0)
    assert [i.severity for i in aggregate([medium_sure, critical_unsure])] == [
        Severity.CRITICAL,
        Severity.MEDIUM,
    ]
    assert rank_key(critical_unsure) < rank_key(medium_sure)


class StubAgent(BaseReviewAgent):
    category = "bug-hunter"

    def __init__(self, findings):
        self.findings = findings

    async def review(self, files):
        return self.findings


def run_review(min_confidence, findings):
    return asyncio.run(
        review_diff(
            TWO_LINE_DIFF,
            agents=[StubAgent(findings)],
            use_langgraph=False,
            min_confidence=min_confidence,
        )
    )


def test_min_confidence_suppresses_weak_findings_and_warns():
    findings = [finding(1, Severity.HIGH, 0.9), finding(2, Severity.HIGH, 0.4)]
    result = run_review(0.5, findings)
    assert [item.confidence for item in result.findings] == [0.9]
    assert any("Suppressed 1 finding(s) below confidence 0.50" in w for w in result.warnings)


def test_min_confidence_zero_keeps_everything_without_warning():
    result = run_review(0.0, [finding(1, Severity.HIGH, 0.9), finding(2, Severity.LOW, 0.1)])
    assert len(result.findings) == 2
    assert not any("Suppressed" in warning for warning in result.warnings)


def test_threshold_equal_to_confidence_keeps_the_finding():
    result = run_review(0.9, [finding(1, Severity.HIGH, 0.9)])
    assert len(result.findings) == 1


# --- Publishing permission ------------------------------------------------------------------
def test_api_rejects_publish_when_server_permission_is_off(harness):
    client = harness.build(GITHUB_TOKEN="t")
    body = {"owner": "acme", "repo": "widgets", "number": 5, "publish": True}
    response = client.post("/github/review", json=body)
    assert response.status_code == 403 and "PUBLISH_REVIEWS" in response.json()["detail"]
    assert harness.runner_calls == []
    assert client.get("/jobs").json() == []


def test_api_accepts_publish_when_server_permission_is_on(harness):
    from conftest import wait_for_job

    client = harness.build(GITHUB_TOKEN="t", PUBLISH_REVIEWS="true")
    body = {"owner": "acme", "repo": "widgets", "number": 5, "publish": True}
    response = client.post("/github/review", json=body)
    assert response.status_code == 202
    wait_for_job(client, response.json()["job"]["id"])
    assert harness.runner_calls[0][2] is True


class PublishingGitHub(FakeGitHub):
    def __init__(self, error=None):
        self.published = []
        self.error = error

    async def publish_review(self, ref, result):
        if self.error:
            raise self.error
        self.published.append(result.summary)
        return {"id": 1}


def make_service(tmp_path, *, permission, github):
    url = f"sqlite:///{tmp_path / 'reviews.db'}"
    store = ReviewStore(url)
    store.create_schema()
    return ReviewService(
        Settings(database_url=url, publish_reviews=permission), github, store
    ), store


@pytest.mark.parametrize(
    ("permission", "opt_in", "expected"),
    [
        (True, True, True),
        (True, False, False),  # permission without opt-in
        (False, True, False),  # opt-in without permission
        (False, False, False),
        (True, None, True),  # legacy callers follow the server setting alone
        (False, None, False),
    ],
)
def test_service_publishes_only_with_permission_and_opt_in(tmp_path, permission, opt_in, expected):
    github = PublishingGitHub()
    service, store = make_service(tmp_path, permission=permission, github=github)
    result = asyncio.run(service.review_pull_request(REF, publish=opt_in))
    assert result.published is expected
    assert bool(github.published) is expected
    assert result.publish_error is None
    assert store.review(result.review_id)["result"]["published"] is expected


def test_publish_failure_is_captured_and_the_review_is_still_stored(tmp_path):
    github = PublishingGitHub(error=GitHubError("GitHub API POST returned 422: bad line"))
    service, store = make_service(tmp_path, permission=True, github=github)
    result = asyncio.run(service.review_pull_request(REF, publish=True))
    assert result.published is False
    assert "422" in result.publish_error
    stored = store.review(result.review_id)
    assert stored["finding_count"] >= 1
    assert "422" in stored["result"]["publish_error"]
    assert store.history("academic/demo")[0]["id"] == result.review_id


def test_demo_diff_fixture_still_has_the_expected_finding(tmp_path):
    service, _ = make_service(tmp_path, permission=False, github=PublishingGitHub())
    result = asyncio.run(service.review_pull_request(REF, publish=True))
    assert DEMO_DIFF and any(item.rule_id == "S307" for item in result.findings)


# --- GitHub client retry --------------------------------------------------------------------
@pytest.fixture
def no_delay(monkeypatch):
    monkeypatch.setattr(github_module, "RETRY_BASE_DELAY", 0)


def client_for(handler):
    return GitHubClient("token", transport=httpx.MockTransport(handler))


def test_503_then_200_succeeds(no_delay):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, text="unavailable")
        return httpx.Response(200, json={"head": {"sha": "abc"}})

    pull = asyncio.run(client_for(handler).pull_request(REF))
    assert pull["head"]["sha"] == "abc" and len(calls) == 2


def test_persistent_503_raises_github_error_after_bounded_attempts(no_delay):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, text="still down")

    with pytest.raises(GitHubError, match="503"):
        asyncio.run(client_for(handler).pull_request(REF))
    assert len(calls) == github_module.RETRY_ATTEMPTS


def test_transport_error_is_retried(no_delay):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("connection reset")
        return httpx.Response(200, json={"login": "octo"})

    assert asyncio.run(client_for(handler).current_user()) == {"login": "octo"}
    assert len(calls) == 3


def test_persistent_transport_error_raises_github_error(no_delay):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    with pytest.raises(GitHubError, match="unreachable"):
        asyncio.run(client_for(handler).current_user())


def test_client_errors_are_not_retried(no_delay):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, text="missing")

    with pytest.raises(GitHubError, match="404"):
        asyncio.run(client_for(handler).pull_request(REF))
    assert len(calls) == 1


def test_rate_limited_403_is_retried(no_delay):
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0"}, text="limit")
        return httpx.Response(200, json={"login": "octo"})

    assert asyncio.run(client_for(handler).current_user())["login"] == "octo"
    assert len(calls) == 2


# --- GitHub client reads --------------------------------------------------------------------
def test_user_repositories_and_pull_requests_are_trimmed_to_public_fields():
    def handler(request):
        if request.url.path == "/user/repos":
            return httpx.Response(
                200,
                json=[{"full_name": "o/r", "owner": {"login": "o"}, "name": "r", "private": True}],
            )
        return httpx.Response(
            200,
            json=[{"number": 3, "title": "T", "user": None, "head": {"sha": "s"}, "draft": True}],
        )

    client = client_for(handler)
    repos = asyncio.run(client.user_repositories())
    assert repos[0]["full_name"] == "o/r" and repos[0]["private"] is True
    pulls = asyncio.run(client.pull_requests("o", "r"))
    assert pulls[0]["number"] == 3 and pulls[0]["author"] is None and pulls[0]["draft"] is True


def test_diff_and_materialize_changed_files(tmp_path):
    import base64

    from pr_reviewer.parser import parse_diff

    def handler(request):
        if "/contents/" in request.url.path:
            content = base64.b64encode(b"print('hi')\n").decode()
            return httpx.Response(200, json={"encoding": "base64", "size": 12, "content": content})
        return httpx.Response(200, text=TWO_LINE_DIFF)

    client = client_for(handler)
    assert asyncio.run(client.diff(REF)) == TWO_LINE_DIFF
    warnings = asyncio.run(
        client.materialize_changed_files(REF, parse_diff(TWO_LINE_DIFF), tmp_path)
    )
    assert warnings == [] and (tmp_path / "app.py").read_text() == "print('hi')\n"


def test_materialize_rejects_unsafe_paths_and_reports_fetch_failures(tmp_path):
    from pr_reviewer.models import DiffFile

    client = client_for(lambda request: httpx.Response(404, text="nope"))
    files = [DiffFile(path="../escape.py"), DiffFile(path="ok.py")]
    warnings = asyncio.run(client.materialize_changed_files(REF, files, tmp_path))
    assert any("unsafe checkout path" in warning for warning in warnings)
    assert any("Could not materialize ok.py" in warning for warning in warnings)
    assert not (tmp_path.parent / "escape.py").exists()


def test_diff_error_raises_github_error(no_delay):
    with pytest.raises(GitHubError, match="404"):
        asyncio.run(client_for(lambda request: httpx.Response(404)).diff(REF))
