# ruff: noqa: S105, S106  (throwaway test values, not real credentials)
"""Regression tests for the cold-review hardening round (anchoring, isolation, visibility)."""

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest
from conftest import PASSWORD, wait_for_job
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_parser import DIFF

from pr_reviewer import api, auth
from pr_reviewer.analyzers import StaticAnalyzer
from pr_reviewer.database import ReviewStore, UserSession
from pr_reviewer.github import GitHubClient
from pr_reviewer.jobs import JobManager
from pr_reviewer.llm import (
    LLMError,
    OpenAICompatibleClient,
    _quota_exhausted,
    _retry_delay,
    _strip_fence,
)
from pr_reviewer.models import Finding, PullRequestRef, ReviewResult, Severity, Suggestion
from pr_reviewer.parser import parse_diff
from pr_reviewer.refactor import RefactoringEngine

ROOT = Path(__file__).resolve().parent.parent
REF = PullRequestRef(owner="o", repo="r", number=1, head_sha="abc")
OAUTH = {
    "GITHUB_OAUTH_CLIENT_ID": "client-id-123",
    "GITHUB_OAUTH_CLIENT_SECRET": "client-secret-456",
    "PUBLIC_BASE_URL": "http://localhost:8000",
}


# --- 1. suggestion anchoring ---
def finding(line, suggestion=None):
    return Finding(
        path="example.py",
        line=line,
        category="bug",
        severity=Severity.LOW,
        message="m",
        evidence="e",
        source="test",
        suggestion=suggestion,
    )


def publish_comment(item):
    captured = {}

    def handler(request):
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    client = GitHubClient("t", transport=httpx.MockTransport(handler))
    result = ReviewResult(files=parse_diff(DIFF), findings=[item], summary="s")
    asyncio.run(client.publish_review(REF, result))
    return captured["comments"][0]


def test_suggestion_must_contain_the_finding_line():
    wrong = Suggestion(start_line=2, end_line=2, replacement="x = 3", rationale="r")
    assert RefactoringEngine().validate(finding(3, wrong), parse_diff(DIFF)[0]) is None
    right = Suggestion(start_line=2, end_line=3, replacement="x = 3\nreturn x", rationale="r")
    assert RefactoringEngine().validate(finding(3, right), parse_diff(DIFF)[0]).validated


def test_validate_all_drops_non_containing_suggestion_but_keeps_finding():
    item = finding(3, Suggestion(start_line=2, end_line=2, replacement="x = 3", rationale="r"))
    RefactoringEngine().validate_all([item], parse_diff(DIFF))
    assert item.suggestion is None and item.line == 3


def test_multi_line_suggestion_is_anchored_with_start_line():
    sug = Suggestion(start_line=2, end_line=3, replacement="x = 3\nreturn x", rationale="r")
    item = finding(2, sug)
    assert RefactoringEngine().validate(item, parse_diff(DIFF)[0])
    comment = publish_comment(item)
    assert comment["line"] == 3 and comment["start_line"] == 2
    assert comment["side"] == "RIGHT" and comment["start_side"] == "RIGHT"
    assert "```suggestion" in comment["body"]


def test_single_line_suggestion_has_no_start_line():
    sug = Suggestion(start_line=3, end_line=3, replacement="return x + 1", rationale="r")
    item = finding(3, sug)
    assert RefactoringEngine().validate(item, parse_diff(DIFF)[0])
    comment = publish_comment(item)
    assert comment["line"] == 3 and "start_line" not in comment and "start_side" not in comment


def test_publish_drops_a_hand_validated_suggestion_that_misses_the_line():
    sug = Suggestion(start_line=2, end_line=2, replacement="x = 3", rationale="r", validated=True)
    comment = publish_comment(finding(3, sug))
    assert comment["line"] == 3 and "start_line" not in comment
    assert "```suggestion" not in comment["body"]


# --- 2. analyzer isolation ---
HIDE_DIFF = (
    "diff --git a/app.py b/app.py\nnew file mode 100644\n--- /dev/null\n+++ b/app.py\n"
    "@@ -0,0 +1,2 @@\n+import os\n+eval('1')\n"
)


def test_analyzer_commands_ignore_repository_configuration(tmp_path):
    seen = {}

    def runner(command, **kwargs):
        seen[command[1]] = command
        import subprocess

        return subprocess.CompletedProcess(command, 0, "", "")

    StaticAnalyzer(runner=runner).analyze(parse_diff(HIDE_DIFF), tmp_path)
    assert "--isolated" in seen["check"]
    assert "--x-ignore-semgrepignore-files" in seen["scan"] and "--no-git-ignore" in seen["scan"]


def test_pr_config_files_cannot_hide_findings(tmp_path):
    (tmp_path / "app.py").write_text("import os\neval('1')\n", encoding="utf-8")
    (tmp_path / "ruff.toml").write_text('exclude = ["app.py"]\n', encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[tool.ruff]\nexclude = ["app.py"]\n')
    (tmp_path / ".semgrepignore").write_text("app.py\n", encoding="utf-8")
    findings, warnings = StaticAnalyzer(timeout=120).analyze(parse_diff(HIDE_DIFF), tmp_path)
    assert {"ruff", "semgrep"} <= {item.source for item in findings}, warnings


def test_materialize_writes_only_python_files(tmp_path):
    requested = []

    def handler(request):
        requested.append(request.url.path)
        body = base64.b64encode(b"x = 1\n").decode()
        return httpx.Response(200, json={"encoding": "base64", "size": 6, "content": body})

    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
        "diff --git a/ruff.toml b/ruff.toml\n--- a/ruff.toml\n+++ b/ruff.toml\n@@ -0,0 +1 @@\n+x\n"
        "diff --git a/.semgrepignore b/.semgrepignore\n--- a/.semgrepignore\n"
        "+++ b/.semgrepignore\n@@ -0,0 +1 @@\n+a.py\n"
    )
    client = GitHubClient("t", transport=httpx.MockTransport(handler))
    warnings = asyncio.run(client.materialize_changed_files(REF, parse_diff(diff), tmp_path))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a.py"]
    assert len(requested) == 1 and warnings == []


# --- 3. open endpoints ---
@pytest.fixture
def demo_env(password_hash):
    return {"DEMO_ACCOUNTS": f"reviewer:{password_hash}, guest:{password_hash}"}


def password_login(client, username="reviewer"):
    return client.post("/auth/password", json={"username": username, "password": PASSWORD})


def test_review_endpoint_requires_sign_in_and_same_origin(harness, demo_env):
    client = harness.build(**demo_env)
    assert client.post("/review", json={"diff": DIFF}).status_code == 401
    assert password_login(client).status_code == 200
    evil = {"Origin": "https://evil.example"}
    assert client.post("/review", json={"diff": DIFF}, headers=evil).status_code == 403
    assert client.post("/review", json={"diff": DIFF}).status_code == 200


def test_review_is_open_without_any_sign_in_method_but_still_rate_limited(harness):
    client = harness.build()
    codes = [client.post("/review", json={"diff": ""}).status_code for _ in range(21)]
    assert codes[:20] == [200] * 20 and codes[20] == 429


def test_demo_endpoint_is_rate_limited_per_client_ip(harness):
    client = harness.build()
    body = {"case_id": "no-such-case"}
    assert [client.post("/dashboard/demo", json=body).status_code for _ in range(20)] == [404] * 20
    limited = client.post("/dashboard/demo", json=body)
    assert limited.status_code == 429 and "Too many requests" in limited.json()["detail"]
    other = TestClient(api.app, client=("203.0.113.9", 4000))
    assert other.post("/dashboard/demo", json=body).status_code == 404


# --- 4. cross-user visibility --------------------------------------------------------------
@pytest.fixture
def github_users(monkeypatch):
    async def exchange(config, code, transport=None):
        return "tok-" + code

    async def current_user(self):
        return {"login": self.token.removeprefix("tok-"), "name": None, "avatar_url": None}

    monkeypatch.setattr(auth, "exchange_code", exchange)
    monkeypatch.setattr(GitHubClient, "current_user", current_user)


def github_client(login):
    client = TestClient(api.app)
    client.get("/auth/github/login", follow_redirects=False)
    state = client.cookies.get(auth.STATE_COOKIE)
    client.get(f"/auth/github/callback?code={login}&state={state}", follow_redirects=False)
    assert client.get("/auth/status").json()["user"]["login"] == login
    return client


def seed(owner):
    ref = PullRequestRef(owner="acme", repo="widgets", number=1, head_sha="s")
    review_id = api.store().save(ref, ReviewResult(files=[], findings=[]), requested_by=owner)
    job, _ = api.store().create_job(
        PullRequestRef(owner="acme", repo="widgets", number=len(owner or "n") + 10),
        trigger="dashboard",
        requested_by=owner,
    )
    api.store().update_job(job["id"], status="completed", review_id=review_id)
    return review_id, job["id"]


def test_github_users_see_only_their_own_rows(harness, github_users):
    harness.build(**OAUTH, **{"DEMO_ACCOUNTS": ""})
    rows = {name: seed(name) for name in ("alice", "bob", None)}
    alice, bob = github_client("alice"), github_client("bob")
    assert alice.get(f"/reviews/{rows['alice'][0]}").status_code == 200
    assert alice.get(f"/jobs/{rows['alice'][1]}").status_code == 200
    for other in ("bob", None):
        assert alice.get(f"/reviews/{rows[other][0]}").status_code == 404
        assert alice.get(f"/jobs/{rows[other][1]}").status_code == 404
    assert [row["id"] for row in alice.get("/history/acme/widgets").json()] == [rows["alice"][0]]
    assert [job["id"] for job in alice.get("/jobs").json()] == [rows["alice"][1]]
    assert [job["id"] for job in bob.get("/jobs").json()] == [rows["bob"][1]]
    assert bob.get(f"/reviews/{rows['alice'][0]}").status_code == 404


def test_demo_users_see_demo_and_unowned_rows_but_not_github_users(harness, github_users, demo_env):
    harness.build(**OAUTH, **demo_env)
    rows = {name: seed(name) for name in ("alice", "reviewer", "guest", None)}
    demo = TestClient(api.app)
    assert password_login(demo).status_code == 200
    visible = {
        name: demo.get(f"/reviews/{ids[0]}").status_code == 200 for name, ids in rows.items()
    }
    assert visible == {"alice": False, "reviewer": True, "guest": True, None: True}
    assert demo.get(f"/jobs/{rows['alice'][1]}").status_code == 404
    listed = {job["id"] for job in demo.get("/jobs").json()}
    assert listed == {rows[name][1] for name in ("reviewer", "guest", None)}
    # A GitHub user who happens to share a demo account name gets no access to demo rows.
    twin = github_client("guest")
    assert twin.get(f"/reviews/{rows['guest'][0]}").status_code == 404


def test_everything_is_visible_when_no_sign_in_method_is_configured(harness):
    client = harness.build()
    rows = {name: seed(name) for name in ("alice", None)}
    for review_id, job_id in rows.values():
        assert client.get(f"/reviews/{review_id}").status_code == 200
        assert client.get(f"/jobs/{job_id}").status_code == 200
    assert len(client.get("/jobs").json()) == 2


def test_requested_by_flows_from_job_to_stored_review(harness, demo_env):
    client = harness.build(**demo_env)
    password_login(client)
    harness.monkeypatch.setenv("GITHUB_TOKEN", "t")
    api.settings.cache_clear()
    posted = client.post("/github/review", json={"owner": "a", "repo": "b", "number": 3})
    job = wait_for_job(client, posted.json()["job"]["id"])
    assert job["requested_by"] == "reviewer"
    assert api.store().review(job["review_id"])["requested_by"] == "reviewer"


def test_service_passes_requested_by_to_the_store(tmp_path):
    from test_service_integration import DEMO_DIFF, FakeGitHub

    from pr_reviewer.service import ReviewService
    from pr_reviewer.settings import Settings

    url = f"sqlite:///{tmp_path / 'svc.db'}"
    store = ReviewStore(url)
    store.create_schema()
    service = ReviewService(Settings(database_url=url), FakeGitHub(), store)
    assert DEMO_DIFF
    result = asyncio.run(
        service.review_pull_request(PullRequestRef(owner="a", repo="b", number=1), None, "carol")
    )
    assert store.review(result.review_id)["requested_by"] == "carol"


def test_existing_database_gets_the_requested_by_column(tmp_path):
    from sqlalchemy import create_engine, text

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE review_history (id INTEGER PRIMARY KEY, repository VARCHAR(300), "
                "pull_number INTEGER, head_sha VARCHAR(64), created_at DATETIME, "
                "finding_count INTEGER, payload JSON)"
            )
        )
    store = ReviewStore(url)
    store.create_schema()
    review_id = store.save(REF, ReviewResult(files=[], findings=[]), requested_by="x")
    assert store.review(review_id)["requested_by"] == "x"


# --- 5. lockout keyed on (username, ip) ---
def test_a_stranger_cannot_lock_out_an_evaluator_on_another_address(harness, demo_env):
    client = harness.build(**demo_env)
    attacker = TestClient(api.app, client=("198.51.100.7", 1111))
    evaluator = TestClient(api.app, client=("203.0.113.50", 2222))
    for _ in range(5):
        assert attacker.post("/auth/password", json={"username": "reviewer", "password": "no"})
    locked = attacker.post("/auth/password", json={"username": "reviewer", "password": PASSWORD})
    assert locked.status_code == 429
    assert password_login(evaluator).status_code == 200
    assert client is not None


def test_one_address_is_still_limited_across_usernames(harness, demo_env, monkeypatch):
    client = harness.build(**demo_env)
    monkeypatch.setattr(auth, "authenticate", lambda config, user, password: password == "good")
    for index in range(20):
        response = client.post("/auth/password", json={"username": f"u{index}", "password": "x"})
        assert response.status_code == 401
    final = client.post("/auth/password", json={"username": "reviewer", "password": "good"})
    assert final.status_code == 429


# --- 6. bounded path integers ---
@pytest.mark.parametrize("value", ["99999999999999999999", "0", "-1", str(2**31)])
def test_review_id_out_of_range_is_422(harness, value):
    client = harness.build()
    assert client.get(f"/reviews/{value}").status_code == 422
    assert client.get("/reviews/1").status_code == 404


# --- 7. OAuth failures redirect ---
def test_dashboard_script_toasts_auth_errors():
    script = (ROOT / "src/pr_reviewer/static/dashboard.js").read_text(encoding="utf-8")
    assert '"error"' in script.split("function handleAuthRedirect")[1].split("// Live mode")[0]
    assert 'result === "error"' in script


# --- 8. webhook redelivery of a failed job ---
def test_failed_job_releases_its_delivery_id_for_a_fresh_job(tmp_path):
    store = ReviewStore(f"sqlite:///{tmp_path / 'redeliver.db'}")
    store.create_schema()
    first, created = store.create_job(REF, trigger="webhook", delivery_id="d-1")
    assert created
    store.update_job(first["id"], status="failed", error="boom")
    second, created_again = store.create_job(REF, trigger="webhook", delivery_id="d-1")
    assert created_again and second["id"] != first["id"] and second["status"] == "queued"
    duplicate, created_third = store.create_job(REF, trigger="webhook", delivery_id="d-1")
    assert not created_third and duplicate["id"] == second["id"]
    assert store.job(first["id"])["status"] == "failed"


def test_active_duplicates_are_not_shared_between_different_requesters(tmp_path):
    store = ReviewStore(f"sqlite:///{tmp_path / 'dupes.db'}")
    store.create_schema()
    a, _ = store.create_job(REF, trigger="dashboard", requested_by="alice")
    b, created = store.create_job(REF, trigger="dashboard", requested_by="bob")
    assert created and a["id"] != b["id"]


# --- 9. docker context ---
def test_dockerignore_excludes_private_and_bulky_paths():
    lines = {
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines() if line.strip()
    }
    for expected in ("evaluation", "tests", ".venv", ".env*", "*.db", ".coverage", "htmlcov"):
        assert expected in lines or expected + "/" in lines


# --- 11. dashboard honesty ---
def test_dashboard_does_not_invent_agent_counts_or_readiness():
    script = (ROOT / "src/pr_reviewer/static/dashboard.js").read_text(encoding="utf-8")
    assert "|| 4}" not in script and "/4 agents" not in script
    essential = script.split("const essential = [")[1].split("];")[0]
    assert "llm_connected" in essential and "github_connected" in essential


# --- 12. job failure before the run starts ---
def test_database_error_on_start_marks_the_job_failed(tmp_path):
    store = ReviewStore(f"sqlite:///{tmp_path / 'start.db'}")
    store.create_schema()
    real = store.update_job
    calls = []

    def flaky(job_id, **fields):
        calls.append(fields.get("status"))
        if len(calls) == 1:
            raise RuntimeError("database hiccup")
        return real(job_id, **fields)

    store.update_job = flaky

    async def runner(ref, token, publish, requested_by=None):
        return ReviewResult(files=[], findings=[])

    async def scenario():
        manager = JobManager(store, runner)
        job, _ = manager.submit(REF, trigger="api")
        await manager.drain()
        return job["id"]

    job_id = asyncio.run(scenario())
    stored = store.job(job_id)
    assert stored["status"] == "failed" and "database hiccup" in stored["error"]


# --- 13. expired sessions are purged on login ---
def test_login_purges_expired_sessions(harness, demo_env):
    from datetime import UTC, datetime, timedelta

    client = harness.build(**demo_env)
    api.store().save_session(
        UserSession(
            id_hash="e" * 64,
            login="old",
            encrypted_token="",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    assert password_login(client).status_code == 200
    with Session(api.store().engine) as session:
        assert (
            session.scalars(select(UserSession).where(UserSession.id_hash == "e" * 64)).first()
            is None
        )


# --- 14. llm helpers ---
def test_quota_exhausted_detection():
    assert _quota_exhausted(httpx.Response(429, json={"error": {"code": "insufficient_quota"}}))
    assert _quota_exhausted(httpx.Response(429, json={"error": [{"code": "insufficient_quota"}]}))
    assert not _quota_exhausted(httpx.Response(429, json={"error": {"code": "rate_limited"}}))
    assert not _quota_exhausted(httpx.Response(429, text="not json"))


def test_insufficient_quota_429_fails_fast_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(429, json={"error": {"code": "insufficient_quota"}})

    client = OpenAICompatibleClient(
        api_key="k", model="m", transport=httpx.MockTransport(handler), max_retries=3
    )
    with pytest.raises(LLMError, match="insufficient_quota"):
        asyncio.run(client.structured("s", "u", Suggestion))
    assert len(calls) == 1


def test_retry_after_header_is_honoured_and_bounded():
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "7"}), 0) == 7.0
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "0.1"}), 0) == 1.0
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "9999"}), 0) == 60.0
    assert _retry_delay(httpx.Response(429, headers={"retry-after": "soon"}), 1) == 8
    assert _retry_delay(httpx.Response(503), 2) == 4


def test_strip_fence_removes_json_code_fences():
    assert _strip_fence('```json\n{"a": 1}\n```').strip() == '{"a": 1}'
    assert _strip_fence('```\n{"a": 1}\n```\n').strip() == '{"a": 1}'
    assert _strip_fence(' {"a": 1} ') == '{"a": 1}'


def test_materialize_accepts_githubs_line_wrapped_base64(tmp_path):
    """GitHub's contents API wraps base64 at 60 characters with newlines (found live)."""
    source = ("def handler(value):\n    return value * 2\n" * 20).encode()
    encoded = base64.encodebytes(source).decode()  # newline-wrapped, like GitHub
    assert "\n" in encoded.strip()

    def handler(request):
        return httpx.Response(
            200, json={"encoding": "base64", "size": len(source), "content": encoded}
        )

    diff = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -0,0 +1 @@\n+x = 1\n"
    client = GitHubClient("t", transport=httpx.MockTransport(handler))
    warnings = asyncio.run(client.materialize_changed_files(REF, parse_diff(diff), tmp_path))
    assert warnings == []
    assert (tmp_path / "a.py").read_bytes() == source
