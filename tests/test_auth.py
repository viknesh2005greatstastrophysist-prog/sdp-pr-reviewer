# ruff: noqa: S105, S106  (throwaway test values, not real credentials)
import asyncio
import json

import httpx
import pytest
from conftest import PASSWORD, SESSION_SECRET, wait_for_job
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from pr_reviewer import api, auth, cli_auth
from pr_reviewer.database import UserSession
from pr_reviewer.github import GitHubClient
from pr_reviewer.settings import Settings, normalise_database_url, parse_demo_accounts

OAUTH = {
    "GITHUB_OAUTH_CLIENT_ID": "client-id-123",
    "GITHUB_OAUTH_CLIENT_SECRET": "client-secret-456",
    "PUBLIC_BASE_URL": "http://localhost:8000",
}
TOKEN = "gho_super_secret_access_token"


@pytest.fixture
def github_stubs(monkeypatch):
    async def exchange(config, code, transport=None):
        return TOKEN

    async def current_user(self):
        return {"login": "octo", "name": "Octo Cat", "avatar_url": "https://example.test/a.png"}

    monkeypatch.setattr(auth, "exchange_code", exchange)
    monkeypatch.setattr(GitHubClient, "current_user", current_user)


def sign_in_with_github(client):
    login = client.get("/auth/github/login", follow_redirects=False)
    state = client.cookies.get(auth.STATE_COOKIE)
    callback = client.get(f"/auth/github/callback?code=abc&state={state}", follow_redirects=False)
    return login, callback


# --- OAuth ----------------------------------------------------------------------------------
def test_login_redirects_to_github_and_sets_state_cookie(harness):
    client = harness.build(**OAUTH)
    response = client.get("/auth/github/login", follow_redirects=False)
    assert response.status_code == 302
    location = response.headers["location"]
    state = client.cookies.get(auth.STATE_COOKIE)
    assert state
    assert location.startswith("https://github.com/login/oauth/authorize?")
    assert "client_id=client-id-123" in location
    assert f"state={state}" in location
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8000%2Fauth%2Fgithub%2Fcallback" in location
    cookie_header = response.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=lax" in cookie_header


def test_login_without_oauth_configured_is_503(harness):
    assert harness.build().get("/auth/github/login", follow_redirects=False).status_code == 503


@pytest.mark.parametrize("state", ["wrong-state", None])
def test_callback_rejects_wrong_or_missing_state(harness, github_stubs, state):
    client = harness.build(**OAUTH)
    client.get("/auth/github/login", follow_redirects=False)
    suffix = f"&state={state}" if state else ""
    response = client.get(f"/auth/github/callback?code=abc{suffix}", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/?auth=error"
    assert client.get("/auth/status").json()["authenticated"] is False


def test_callback_without_prior_login_cookie_redirects_with_error(harness, github_stubs):
    client = harness.build(**OAUTH)
    response = client.get("/auth/github/callback?code=abc&state=anything", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/?auth=error"


def test_callback_denied_redirects_without_session(harness):
    client = harness.build(**OAUTH)
    response = client.get("/auth/github/callback?error=access_denied", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/?auth=denied"


def test_callback_success_creates_session_and_never_leaks_token(harness, github_stubs):
    client = harness.build(**OAUTH)
    seen = []
    login, callback = sign_in_with_github(client)
    seen += [login, callback]
    assert callback.status_code == 302 and callback.headers["location"] == "/?auth=ok"
    assert client.cookies.get(auth.SESSION_COOKIE)
    assert "httponly" in callback.headers["set-cookie"].lower()
    status = client.get("/auth/status")
    seen += [status, client.get("/dashboard/status"), client.get("/jobs")]
    body = status.json()
    assert body["authenticated"] is True
    assert body["user"]["login"] == "octo" and body["user"]["auth_method"] == "github"
    assert "token" not in body["user"]
    for response in seen:
        assert TOKEN not in response.text
        assert TOKEN not in json.dumps(dict(response.headers))
    assert TOKEN not in "".join(client.cookies.values())


def test_session_token_is_encrypted_at_rest(harness, github_stubs):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    with Session(api.store().engine) as session:
        rows = session.scalars(select(UserSession)).all()
    assert len(rows) == 1
    assert rows[0].encrypted_token != TOKEN and TOKEN not in rows[0].encrypted_token
    assert client.cookies.get(auth.SESSION_COOKIE) not in rows[0].id_hash
    loaded = auth.load_user(api.store(), api.settings(), client.cookies.get(auth.SESSION_COOKIE))
    assert loaded is not None and loaded.token == TOKEN


def test_expired_session_is_rejected(harness, github_stubs):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    assert client.get("/auth/status").json()["authenticated"] is True
    with Session(api.store().engine) as session:
        session.execute(
            update(UserSession).values(expires_at=auth.datetime(2000, 1, 1, tzinfo=auth.UTC))
        )
        session.commit()
    assert client.get("/auth/status").json()["authenticated"] is False
    with Session(api.store().engine) as session:
        assert session.scalars(select(UserSession)).all() == []


def test_session_signed_with_other_secret_is_rejected(harness, github_stubs, monkeypatch):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    monkeypatch.setenv("SESSION_SECRET", "a-different-secret-" + "y" * 32)
    api.settings.cache_clear()
    assert client.get("/auth/status").json()["authenticated"] is False


def test_logout_clears_session_and_cookie(harness, github_stubs):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    response = client.post("/auth/logout")
    assert response.status_code == 200
    assert auth.SESSION_COOKIE in response.headers["set-cookie"]
    assert client.cookies.get(auth.SESSION_COOKIE) is None
    assert client.get("/auth/status").json()["authenticated"] is False
    with Session(api.store().engine) as session:
        assert session.scalars(select(UserSession)).all() == []


def test_cross_origin_post_is_rejected(harness, github_stubs):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    evil = {"Origin": "https://evil.example"}
    assert client.post("/auth/logout", headers=evil).status_code == 403
    assert client.get("/auth/status").json()["authenticated"] is True  # logout did not happen
    review = {"owner": "o", "repo": "r", "number": 1}
    assert client.post("/github/review", json=review, headers=evil).status_code == 403
    assert (
        client.post(
            "/auth/password", json={"username": "a", "password": "b"}, headers=evil
        ).status_code
        == 403
    )
    same = {"Origin": "http://localhost:8000"}
    assert client.post("/auth/logout", headers=same).status_code == 200


def test_oauth_user_still_lists_repositories(harness, github_stubs, monkeypatch):
    async def repos(self, limit=100):
        return [{"full_name": "octo/hello"}]

    monkeypatch.setattr(GitHubClient, "user_repositories", repos)
    client = harness.build(**OAUTH)
    assert client.get("/me/repos").status_code == 401
    sign_in_with_github(client)
    assert client.get("/me/repos").json() == [{"full_name": "octo/hello"}]


def test_oauth_review_uses_the_users_token(harness, github_stubs):
    client = harness.build(**OAUTH)
    sign_in_with_github(client)
    response = client.post("/github/review", json={"owner": "o", "repo": "r", "number": 3})
    assert response.status_code == 202
    assert wait_for_job(client, response.json()["job"]["id"])["status"] == "completed"
    assert harness.runner_calls[0][1] == TOKEN
    assert "octo" == response.json()["job"]["requested_by"]
    assert TOKEN not in response.text


# --- Password hashing ----------------------------------------------------------------------
def test_hash_and_verify_password(password_hash):
    assert password_hash.startswith("pbkdf2_sha256$600000$")
    assert auth.verify_password(PASSWORD, password_hash)
    assert not auth.verify_password(PASSWORD + "x", password_hash)
    assert PASSWORD not in password_hash


def test_hashes_are_salted(password_hash):
    assert auth.hash_password(PASSWORD) != password_hash


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "garbage",
        "md5$600000$AAAA$AAAA",
        "pbkdf2_sha256$abc$AAAA$AAAA",
        "pbkdf2_sha256$600000$!!$!!",
    ],
)
def test_verify_rejects_malformed_hashes(encoded):
    assert auth.verify_password(PASSWORD, encoded) is False


def test_verify_rejects_weak_or_extreme_iteration_counts(password_hash):
    _, _, salt, digest = password_hash.split("$")
    assert not auth.verify_password(PASSWORD, f"pbkdf2_sha256$1000${salt}${digest}")
    assert not auth.verify_password(PASSWORD, f"pbkdf2_sha256$999999999${salt}${digest}")
    with pytest.raises(ValueError):
        auth.hash_password(PASSWORD, iterations=1000)


def test_authenticate_unknown_user_still_runs_a_hash(monkeypatch, password_hash):
    calls = []
    real = auth.verify_password
    monkeypatch.setattr(auth, "verify_password", lambda p, e: calls.append(e) or real(p, e))
    settings = Settings(demo_accounts=(("reviewer", password_hash),))
    assert auth.authenticate(settings, "reviewer", PASSWORD) is True
    assert auth.authenticate(settings, "nobody", PASSWORD) is False
    assert len(calls) == 2 and calls[0] == password_hash
    # the stand-in is a real, full-cost hash, so unknown users take as long as known ones
    assert calls[1] != password_hash
    assert calls[1].startswith("pbkdf2_sha256$600000$")


def test_demo_accounts_and_database_url_settings(password_hash):
    raw = f"alice:{password_hash}, bob:{password_hash},broken,:nohash,carol:plain"
    parsed = parse_demo_accounts(raw)
    assert [name for name, _ in parsed] == ["alice", "bob"]
    assert parse_demo_accounts(None) == () and parse_demo_accounts("") == ()
    assert Settings(demo_accounts=parsed).password_login_enabled
    assert not Settings().password_login_enabled
    assert normalise_database_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_database_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert normalise_database_url("postgresql+psycopg://u@h/db") == "postgresql+psycopg://u@h/db"
    assert normalise_database_url("sqlite:///x.db") == "sqlite:///x.db"


def test_settings_from_env_reads_demo_accounts_and_normalises_url(monkeypatch, password_hash):
    monkeypatch.setenv("DEMO_ACCOUNTS", f"alice:{password_hash}")
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@host/db")
    settings = Settings.from_env()
    assert settings.password_login_enabled and settings.login_required
    assert settings.database_url == "postgresql+psycopg://u:p@host/db"


def test_throttle_window_expires_and_resets():
    now = [0.0]
    throttle = auth.LoginThrottle(attempts=2, window=10, clock=lambda: now[0])
    throttle.record_failure("a")
    assert not throttle.locked("a")
    throttle.record_failure("a")
    assert throttle.locked("a", "b")
    now[0] = 11
    assert not throttle.locked("a")
    throttle.record_failure("a")
    throttle.record_failure("a")
    throttle.reset("a")
    assert not throttle.locked("a")


# --- Password login over HTTP --------------------------------------------------------------
@pytest.fixture
def demo_env(password_hash):
    return {"DEMO_ACCOUNTS": f"reviewer:{password_hash}"}


def login(client, username="reviewer", password=PASSWORD):
    return client.post("/auth/password", json={"username": username, "password": password})


def test_password_login_sets_session_and_status(harness, demo_env):
    client = harness.build(**demo_env)
    assert client.get("/auth/status").json()["password_login_enabled"] is True
    response = login(client)
    assert response.status_code == 200
    assert PASSWORD not in response.text
    assert "httponly" in response.headers["set-cookie"].lower()
    body = client.get("/auth/status").json()
    assert body["authenticated"] is True
    assert body["user"]["login"] == "reviewer" and body["user"]["auth_method"] == "password"
    assert client.get("/dashboard/status").json()["password_login_enabled"] is True
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/status").json()["authenticated"] is False


def test_password_session_stores_no_github_token(harness, demo_env):
    client = harness.build(**demo_env)
    login(client)
    user = auth.load_user(api.store(), api.settings(), client.cookies.get(auth.SESSION_COOKIE))
    assert user.token is None and user.auth_method == "password"


def test_wrong_password_and_unknown_user_are_401_with_same_message(harness, demo_env):
    client = harness.build(**demo_env)
    wrong = login(client, password="not it")
    unknown = login(client, username="nobody")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert client.get("/auth/status").json()["authenticated"] is False


def test_password_login_disabled_returns_503(harness):
    assert login(harness.build()).status_code == 503


def test_password_request_limits(harness, demo_env):
    client = harness.build(**demo_env)
    assert client.post("/auth/password", json={"username": "", "password": "x"}).status_code == 422
    assert (
        client.post("/auth/password", json={"username": "a" * 65, "password": "x"}).status_code
        == 422
    )
    assert (
        client.post("/auth/password", json={"username": "a", "password": "x" * 257}).status_code
        == 422
    )


def test_lockout_after_five_failures_blocks_even_the_right_password(harness, demo_env):
    client = harness.build(**demo_env)
    assert [login(client, password="bad").status_code for _ in range(5)] == [401] * 5
    locked = login(client)  # correct password, but the account is locked
    assert locked.status_code == 429
    assert "10 minutes" in locked.json()["detail"]
    assert client.get("/auth/status").json()["authenticated"] is False


def test_successful_login_resets_username_failures(harness, demo_env):
    client = harness.build(**demo_env)
    for _ in range(4):
        login(client, password="bad")
    assert login(client).status_code == 200


def test_password_user_cannot_browse_repositories(harness, demo_env):
    client = harness.build(**demo_env)
    login(client)
    expected = "Sign in with GitHub to browse your repositories; enter owner/repo/number instead"
    for path in ("/me/repos", "/me/repos/o/r/pulls"):
        response = client.get(path)
        assert response.status_code == 403 and response.json()["detail"] == expected


def test_password_user_queues_review_with_server_credentials(harness, demo_env):
    client = harness.build(GITHUB_TOKEN="server-token", **demo_env)
    login(client)
    response = client.post("/github/review", json={"owner": "o", "repo": "r", "number": 7})
    assert response.status_code == 202
    job = response.json()["job"]
    assert job["requested_by"] == "reviewer" and job["trigger"] == "dashboard"
    assert wait_for_job(client, job["id"])["status"] == "completed"
    assert harness.runner_calls[0][1] is None  # no user token: the server credentials are used


def test_password_user_review_needs_server_credentials(harness, demo_env):
    client = harness.build(**demo_env)
    login(client)
    response = client.post("/github/review", json={"owner": "o", "repo": "r", "number": 7})
    assert response.status_code == 503
    assert harness.runner_calls == []


def test_password_login_alone_protects_review_data(harness, demo_env):
    client = harness.build(**demo_env)
    for path in ("/jobs", "/jobs/x", "/reviews/1", "/history/o/r"):
        assert client.get(path).status_code == 401, path
    assert (
        client.post("/github/review", json={"owner": "o", "repo": "r", "number": 1}).status_code
        == 401
    )
    login(client)
    assert client.get("/jobs").status_code == 200


def test_cli_hash_password(monkeypatch, capsys):
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr(cli_auth.getpass, "getpass", lambda prompt="": next(answers))
    assert cli_auth.main() == 0
    printed = capsys.readouterr().out.strip()
    assert auth.verify_password(PASSWORD, printed) and PASSWORD not in printed


@pytest.mark.parametrize("answers", [["a" * 14, "b" * 14], ["short", "short"]])
def test_cli_rejects_mismatch_and_short_passwords(monkeypatch, capsys, answers):
    iterator = iter(answers)
    monkeypatch.setattr(cli_auth.getpass, "getpass", lambda prompt="": next(iterator))
    assert cli_auth.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "" and answers[0] not in captured.err


def test_session_secret_constant_is_used(harness):
    harness.build()
    assert api.settings().session_secret == SESSION_SECRET


# --- OAuth code exchange (no network: httpx.MockTransport) ----------------------------------
def exchange(handler):
    settings = Settings(github_oauth_client_id="id", github_oauth_client_secret="secret")
    return asyncio.run(auth.exchange_code(settings, "code", httpx.MockTransport(handler)))


def test_exchange_code_returns_the_access_token():
    assert exchange(lambda request: httpx.Response(200, json={"access_token": "tok"})) == "tok"


def test_exchange_code_failures_become_oauth_errors():
    with pytest.raises(auth.OAuthError, match="status 500"):
        exchange(lambda request: httpx.Response(500))
    with pytest.raises(auth.OAuthError, match="bad_verification_code"):
        exchange(lambda request: httpx.Response(200, json={"error": "bad_verification_code"}))


def test_callback_redirects_with_error_on_github_failure(harness, monkeypatch):
    async def failing(config, code, transport=None):
        raise auth.OAuthError("GitHub rejected the authorization code: expired")

    monkeypatch.setattr(auth, "exchange_code", failing)
    client = harness.build(**OAUTH)
    client.get("/auth/github/login", follow_redirects=False)
    state = client.cookies.get(auth.STATE_COOKIE)
    response = client.get(f"/auth/github/callback?code=abc&state={state}", follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/?auth=error"
    assert (
        "set-cookie" not in response.headers
        or auth.SESSION_COOKIE not in response.headers["set-cookie"]
    )
    assert client.get("/auth/status").json()["authenticated"] is False
