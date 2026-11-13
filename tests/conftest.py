"""Shared fixtures: an isolated FastAPI app (temporary SQLite, no network, no real secrets)."""

from __future__ import annotations

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from pr_reviewer import api, auth
from pr_reviewer.database import ReviewStore
from pr_reviewer.jobs import JobManager
from pr_reviewer.models import ReviewResult

CONFIG_VARS = (
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_OAUTH_CLIENT_ID",
    "GITHUB_OAUTH_CLIENT_SECRET",
    "GITHUB_OAUTH_SCOPES",
    "SESSION_SECRET",
    "PUBLIC_BASE_URL",
    "LLM_API_KEY",
    "MIN_CONFIDENCE",
    "PUBLISH_REVIEWS",
    "WEBHOOK_AUTO_PUBLISH",
    "DEMO_ACCOUNTS",
    "DATABASE_URL",
    "REVIEW_CONCURRENCY",
)
SESSION_SECRET = "test-session-secret-" + "x" * 32


class AppHarness:
    """Builds a client for the shared ``api.app`` with a given environment."""

    def __init__(self, tmp_path, monkeypatch):
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        self.stack = ExitStack()
        self.runner_calls: list[tuple] = []

    def build(self, runner=None, **env: str) -> TestClient:
        for name in CONFIG_VARS:
            self.monkeypatch.delenv(name, raising=False)
        self.monkeypatch.setenv("DATABASE_URL", f"sqlite:///{self.tmp_path / 'test.db'}")
        self.monkeypatch.setenv("SESSION_SECRET", SESSION_SECRET)
        for key, value in env.items():
            self.monkeypatch.setenv(key, value)
        api.settings.cache_clear()
        api._store_for.cache_clear()
        api.login_throttle = auth.LoginThrottle()
        client = self.stack.enter_context(TestClient(api.app))
        # The lifespan installs the real JobManager; swap in the fake runner afterwards.
        api.app.state.jobs = JobManager(api.store(), runner or self.default_runner)
        return client

    async def default_runner(self, ref, token, publish):
        self.runner_calls.append((ref, token, publish))
        review_id = api.store().save(ref, ReviewResult(files=[], findings=[], summary="ok"))
        return ReviewResult(files=[], findings=[], summary="ok", review_id=review_id)

    def close(self) -> None:
        self.stack.close()
        api.settings.cache_clear()
        api._store_for.cache_clear()
        if hasattr(api.app.state, "jobs"):
            del api.app.state.jobs


@pytest.fixture
def harness(tmp_path, monkeypatch):
    instance = AppHarness(tmp_path, monkeypatch)
    yield instance
    instance.close()


@pytest.fixture(scope="session")
def password_hash() -> str:
    """One PBKDF2 hash (600k iterations) reused by every test that needs a demo account."""
    return auth.hash_password("correct horse battery staple")


PASSWORD = "correct horse battery staple"  # noqa: S105


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    import time

    deadline = time.monotonic() + timeout
    while True:
        job = client.get(f"/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"} or time.monotonic() > deadline:
            return job
        time.sleep(0.02)


def store_for_tests() -> ReviewStore:
    return api.store()
