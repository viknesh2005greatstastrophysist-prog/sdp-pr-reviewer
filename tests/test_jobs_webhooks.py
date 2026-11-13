# ruff: noqa: S105, S106  (throwaway test values, not real credentials)
import asyncio
import hashlib
import hmac
import json

import pytest
from conftest import wait_for_job

from pr_reviewer import api
from pr_reviewer.database import ReviewStore
from pr_reviewer.jobs import JobManager
from pr_reviewer.models import Finding, PullRequestRef, ReviewResult, Severity

SECRET = "webhook-secret-for-tests"
REF = PullRequestRef(owner="acme", repo="widgets", number=5)


def result_with_findings(review_id=None, **extra):
    finding = Finding(
        path="a.py",
        line=1,
        category="style",
        severity=Severity.LOW,
        message="m",
        evidence="e",
        source="style",
    )
    return ReviewResult(files=[], findings=[finding], summary="s", review_id=review_id, **extra)


@pytest.fixture
def store(tmp_path):
    instance = ReviewStore(f"sqlite:///{tmp_path / 'jobs.db'}")
    instance.create_schema()
    return instance


# --- JobManager ------------------------------------------------------------------------------
def test_job_completes_with_result_and_review_id(harness):
    async def runner(ref, token, publish):
        review_id = api.store().save(ref, result_with_findings())
        return result_with_findings(review_id=review_id, head_sha="deadbeef", published=True)

    client = harness.build(runner=runner, GITHUB_TOKEN="t")
    response = client.post("/github/review", json={"owner": "acme", "repo": "widgets", "number": 5})
    assert response.status_code == 202 and response.json()["created"] is True
    job = wait_for_job(client, response.json()["job"]["id"])
    assert job["status"] == "completed"
    assert job["review_id"] is not None and job["finding_count"] == 1
    assert job["head_sha"] == "deadbeef" and job["published"] is True
    assert job["started_at"] and job["finished_at"] and job["error"] is None
    assert job["result"]["findings"][0]["path"] == "a.py"
    assert client.get(f"/reviews/{job['review_id']}").json()["finding_count"] == 1
    assert client.get("/history/acme/widgets").json()[0]["id"] == job["review_id"]
    assert client.get("/jobs").json()[0]["id"] == job["id"]
    assert client.get("/jobs?repository=other/repo").json() == []


def test_failed_job_records_the_error(harness):
    async def runner(ref, token, publish):
        raise RuntimeError("GitHub exploded")

    client = harness.build(runner=runner, GITHUB_TOKEN="t")
    created = client.post("/github/review", json={"owner": "acme", "repo": "widgets", "number": 5})
    job = wait_for_job(client, created.json()["job"]["id"])
    assert job["status"] == "failed"
    assert job["error"] == "RuntimeError: GitHub exploded"
    assert job["review_id"] is None and job["finished_at"]
    assert "result" not in job


def test_active_job_for_same_pull_request_is_deduplicated(store):
    async def scenario():
        gate = asyncio.Event()
        calls = []

        async def runner(ref, token, publish):
            calls.append(ref.number)
            await gate.wait()
            return result_with_findings()

        manager = JobManager(store, runner)
        first, created_first = manager.submit(REF, trigger="api")
        second, created_second = manager.submit(REF, trigger="dashboard")
        other, created_other = manager.submit(REF.model_copy(update={"number": 6}), trigger="api")
        gate.set()
        await manager.drain()
        return first, created_first, second, created_second, other, created_other, calls

    first, created_first, second, created_second, other, created_other, calls = asyncio.run(
        scenario()
    )
    assert created_first and not created_second and created_other
    assert first["id"] == second["id"] != other["id"]
    assert sorted(calls) == [5, 6]  # the duplicate never started a second review
    assert store.job(first["id"])["status"] == "completed"


def test_tokens_are_passed_to_the_runner_but_never_stored(store):
    seen = []

    async def runner(ref, token, publish):
        seen.append((token, publish))
        return result_with_findings()

    async def scenario():
        manager = JobManager(store, runner)
        job, _ = manager.submit(REF, trigger="dashboard", token="secret-token", publish=True)
        await manager.drain()
        return job

    job = asyncio.run(scenario())
    assert seen == [("secret-token", True)]
    assert "secret-token" not in json.dumps(store.job(job["id"]))


def test_fail_interrupted_jobs(store):
    queued, _ = store.create_job(REF, trigger="api")
    running, _ = store.create_job(REF.model_copy(update={"number": 6}), trigger="api")
    store.update_job(running["id"], status="running")
    done, _ = store.create_job(REF.model_copy(update={"number": 7}), trigger="api")
    store.update_job(done["id"], status="completed")
    assert store.fail_interrupted_jobs() == 2
    for job_id in (queued["id"], running["id"]):
        job = store.job(job_id)
        assert job["status"] == "failed" and "restart" in job["error"] and job["finished_at"]
    assert store.job(done["id"])["status"] == "completed"
    assert store.fail_interrupted_jobs() == 0


def test_unknown_job_and_review_are_404(harness):
    client = harness.build()
    assert client.get("/jobs/does-not-exist").status_code == 404
    assert client.get("/reviews/999").status_code == 404
    assert client.get("/jobs").json() == []


def test_require_viewer_is_401_when_oauth_is_configured(harness):
    client = harness.build(GITHUB_OAUTH_CLIENT_ID="id", GITHUB_OAUTH_CLIENT_SECRET="secret")
    for path in ("/jobs", "/jobs/x", "/reviews/1", "/history/o/r"):
        response = client.get(path)
        assert response.status_code == 401 and "Sign in" in response.json()["detail"], path
    assert client.get("/health").status_code == 200  # public


def test_review_data_is_open_when_no_sign_in_method_is_configured(harness):
    assert harness.build().get("/jobs").status_code == 200


def test_github_review_requires_credentials_and_valid_input(harness):
    client = harness.build()
    body = {"owner": "acme", "repo": "widgets", "number": 5}
    assert client.post("/github/review", json=body).status_code == 503
    assert client.post("/github/review", json={**body, "owner": "bad owner"}).status_code == 422
    assert client.post("/github/review", json={**body, "number": 0}).status_code == 422


# --- Webhooks --------------------------------------------------------------------------------
def pull_request_payload(action="opened", draft=False, number=9):
    return {
        "action": action,
        "pull_request": {"number": number, "draft": draft, "head": {"sha": "abc123"}},
        "repository": {"full_name": "acme/widgets"},
    }


def deliver(
    client, payload, *, event="pull_request", delivery="delivery-1", secret=SECRET, raw=None
):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if delivery:
        headers["X-GitHub-Delivery"] = delivery
    if secret:
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = f"sha256={digest}"
    return client.post("/webhooks/github", content=body, headers=headers)


@pytest.fixture
def hook_client(harness):
    return harness.build(GITHUB_WEBHOOK_SECRET=SECRET, GITHUB_TOKEN="server-token")


def test_webhook_bad_signature_is_401(hook_client):
    assert deliver(hook_client, pull_request_payload(), secret="wrong").status_code == 401
    assert deliver(hook_client, pull_request_payload(), secret=None).status_code == 401
    assert hook_client.get("/jobs").json() == []


def test_webhook_ping(hook_client):
    response = deliver(hook_client, {"zen": "hi"}, event="ping")
    assert response.status_code == 202 and response.json() == {"status": "pong"}


def test_webhook_ignores_other_events_actions_and_drafts(hook_client):
    other = deliver(hook_client, {}, event="issues")
    assert other.json() == {"status": "ignored", "reason": "unsupported event"}
    closed = deliver(hook_client, pull_request_payload(action="closed"))
    assert closed.json()["reason"] == "non-review action"
    draft = deliver(hook_client, pull_request_payload(draft=True))
    assert draft.json() == {"status": "ignored", "reason": "draft pull request"}
    assert hook_client.get("/jobs").json() == []


def test_webhook_opened_queues_a_job_and_duplicate_delivery_is_ignored(harness, hook_client):
    first = deliver(hook_client, pull_request_payload())
    assert first.status_code == 202
    assert first.json()["status"] == "queued" and first.json()["job_id"]
    job = wait_for_job(hook_client, first.json()["job_id"])
    assert job["status"] == "completed" and job["trigger"] == "webhook"
    assert job["repository"] == "acme/widgets" and job["pull_number"] == 9
    repeat = deliver(hook_client, pull_request_payload())
    assert repeat.status_code == 202
    assert repeat.json() == {"status": "duplicate", "job_id": first.json()["job_id"]}
    assert len(harness.runner_calls) == 1
    assert harness.runner_calls[0][0].head_sha == "abc123"
    assert harness.runner_calls[0][1] is None  # webhooks always use server credentials


def test_webhook_without_server_credentials_is_503(harness):
    client = harness.build(GITHUB_WEBHOOK_SECRET=SECRET)
    response = deliver(client, pull_request_payload())
    assert response.status_code == 503
    assert harness.runner_calls == []


def test_webhook_without_secret_is_503(harness):
    client = harness.build()
    assert deliver(client, pull_request_payload(), secret=None).status_code == 503


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "opened"},
        {"action": "opened", "pull_request": {"number": 1, "head": {}}, "repository": {}},
        {
            "action": "opened",
            "pull_request": {"number": 1, "head": {"sha": "a"}},
            "repository": {"full_name": "no-slash"},
        },
        {
            "action": "opened",
            "pull_request": {"number": 0, "head": {"sha": "a"}},
            "repository": {"full_name": "a/b"},
        },
    ],
)
def test_webhook_malformed_payload_is_400(hook_client, payload):
    assert deliver(hook_client, payload).status_code == 400


def test_webhook_invalid_json_is_400(hook_client):
    assert deliver(hook_client, None, raw=b"{not json").status_code == 400


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, False),
        ({"WEBHOOK_AUTO_PUBLISH": "true"}, False),  # needs the server permission too
        ({"PUBLISH_REVIEWS": "true"}, False),  # needs the auto-publish opt-in too
        ({"PUBLISH_REVIEWS": "true", "WEBHOOK_AUTO_PUBLISH": "true"}, True),
    ],
)
def test_webhook_auto_publish_needs_both_settings(harness, env, expected):
    client = harness.build(GITHUB_WEBHOOK_SECRET=SECRET, GITHUB_TOKEN="t", **env)
    response = deliver(client, pull_request_payload())
    wait_for_job(client, response.json()["job_id"])
    assert harness.runner_calls[0][2] is expected
