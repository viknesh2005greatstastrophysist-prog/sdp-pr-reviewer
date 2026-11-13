from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, ratelimit
from .agents import SPECIALIZATIONS, LLMReviewAgent
from .database import ReviewStore, Scope
from .demos import DEMO_CASES, demo_agents
from .github import GitHubClient, GitHubError, app_installation_client, verify_webhook_signature
from .jobs import JobManager
from .llm import OpenAICompatibleClient
from .models import PullRequestRef, ReviewResult
from .pipeline import review_diff
from .refactor import RefactoringEngine
from .service import ReviewService
from .settings import Settings

STATIC_DIR = Path(__file__).with_name("static")
NAME = r"^[A-Za-z0-9_.-]+$"
REPOS_NEED_GITHUB_DETAIL = (
    "Sign in with GitHub to browse your repositories; enter owner/repo/number instead"
)
login_throttle = auth.LoginThrottle()  # failures per (username, client address)
ip_throttle = auth.LoginThrottle(attempts=auth.IP_LOCKOUT_ATTEMPTS)  # per client address only
rate_limiter = ratelimit.RateLimiter()
ReviewId = Annotated[int, PathParam(ge=1, le=2**31 - 1)]
REVIEW_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=4)
def _store_for(database_url: str) -> ReviewStore:
    store = ReviewStore(database_url)
    store.create_schema()
    return store


def store() -> ReviewStore:
    return _store_for(settings().database_url)


def job_manager() -> JobManager:
    manager = getattr(app.state, "jobs", None)
    if manager is None:
        manager = JobManager(store(), run_review, settings().review_concurrency)
        app.state.jobs = manager
    return manager


@asynccontextmanager
async def lifespan(application: FastAPI):
    store().fail_interrupted_jobs()
    application.state.jobs = JobManager(store(), run_review, settings().review_concurrency)
    yield
    await application.state.jobs.drain()


app = FastAPI(title="Evidence-Grounded Multi-Agent PR Reviewer", version="2.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="dashboard-assets")


class ReviewRequest(BaseModel):
    diff: str = Field(max_length=5_000_000)


class GitHubReviewRequest(BaseModel):
    owner: str = Field(pattern=NAME)
    repo: str = Field(pattern=NAME)
    number: int = Field(gt=0)
    publish: bool = False


class PasswordLoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class DemoReviewRequest(BaseModel):
    case_id: str = Field(default="full-spectrum", pattern=r"^[a-z0-9-]+$")


# Authentication helpers -----------------------------------------------------------------
def current_user(request: Request) -> auth.AuthenticatedUser | None:
    return auth.load_user(store(), settings(), request.cookies.get(auth.SESSION_COOKIE))


def _sign_in_prompt(config: Settings) -> str:
    if config.oauth_configured and config.password_login_enabled:
        return "Sign in with GitHub or a demo account first"
    return "Sign in with GitHub first" if config.oauth_configured else "Sign in first"


def require_viewer(request: Request) -> auth.AuthenticatedUser | None:
    """When any sign-in method is configured, review data is visible to signed-in users only."""
    user = current_user(request)
    config = settings()
    if config.login_required and user is None:
        raise HTTPException(status_code=401, detail=_sign_in_prompt(config))
    return user


def require_login(request: Request) -> auth.AuthenticatedUser:
    """A signed-in user holding a GitHub token (repository browsing needs one)."""
    if not settings().login_required:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in with GitHub first")
    if user.token is None:
        raise HTTPException(status_code=403, detail=REPOS_NEED_GITHUB_DETAIL)
    return user


def viewer_scope(user: auth.AuthenticatedUser | None) -> Scope | None:
    """Which stored rows this viewer may read (``None`` means all of them).

    No sign-in method configured: everything (local development). A GitHub user: only rows
    they requested. A demo account: rows requested by any demo account plus unowned rows
    (webhook, API and server-credential reviews).
    """
    config = settings()
    if not config.login_required:
        return None
    demo_names = frozenset(name for name, _ in config.demo_accounts)
    if user is None:
        return Scope(frozenset())
    if user.auth_method == "password":
        return Scope(demo_names, include_unowned=True)
    # A GitHub login that collides with a demo account name must not inherit demo rows.
    return Scope(frozenset({user.login}) - demo_names)


def client_address(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request) -> None:
    if not rate_limiter.allow(client_address(request)):
        raise HTTPException(
            status_code=429, detail="Too many requests from this address. Try again in a minute."
        )


def require_same_origin(origin: str | None = Header(default=None)) -> None:
    if not auth.same_origin(settings(), origin):
        raise HTTPException(status_code=403, detail="Cross-origin request rejected")


# Review execution -------------------------------------------------------------------------
def _agents_for(config: Settings):
    if not config.llm_api_key:
        return None
    client = OpenAICompatibleClient(
        api_key=config.llm_api_key,
        model=config.llm_model,
        base_url=config.llm_base_url,
        timeout=config.agent_timeout,
    )
    return [LLMReviewAgent(category, client) for category in SPECIALIZATIONS]


async def _server_github(ref: PullRequestRef) -> GitHubClient:
    config = settings()
    if config.github_token:
        return GitHubClient(config.github_token, config.github_api_url)
    if config.github_app_id and config.github_private_key:
        return await app_installation_client(
            config.github_app_id,
            config.github_private_key,
            ref.owner,
            ref.repo,
            config.github_api_url,
        )
    raise GitHubError("GitHub credentials are not configured")


async def run_review(
    ref: PullRequestRef, token: str | None, publish: bool, requested_by: str | None = None
) -> ReviewResult:
    """Job runner: a signed-in user's token, otherwise the server's App/token credentials."""
    config = settings()
    github = GitHubClient(token, config.github_api_url) if token else await _server_github(ref)
    return await ReviewService(config, github, store()).review_pull_request(
        ref, publish=publish, requested_by=requested_by
    )


# Dashboard and status ---------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard/status")
def dashboard_status():
    """Return capability state without exposing credentials or secret values."""
    config = settings()
    return {
        "service": "ready",
        "github_connected": config.server_github_configured,
        "oauth_configured": config.oauth_configured,
        "password_login_enabled": config.password_login_enabled,
        "llm_connected": bool(config.llm_api_key),
        "llm_model": config.llm_model if config.llm_api_key else None,
        "webhook_configured": bool(config.github_webhook_secret),
        "publishing_enabled": config.publish_reviews,
        "webhook_auto_publish": config.publish_reviews and config.webhook_auto_publish,
        "min_confidence": config.min_confidence,
        "review_concurrency": config.review_concurrency,
        "database": "postgresql" if config.database_url.startswith("postgresql") else "sqlite",
        "analyzers": {
            "ruff": shutil.which("ruff") is not None,
            "semgrep": shutil.which("semgrep") is not None,
        },
        "agents": list(SPECIALIZATIONS),
    }


@app.get("/dashboard/demo-cases")
def dashboard_demo_cases():
    return [case.metadata() for case in DEMO_CASES.values()]


@app.post("/dashboard/demo", dependencies=[Depends(rate_limit)])
async def dashboard_demo(request: DemoReviewRequest | None = None):
    """Exercise the complete safe pipeline against a bundled, trusted demonstration PR."""
    config = settings()
    case_id = request.case_id if request else "full-spectrum"
    case = DEMO_CASES.get(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Unknown demonstration case")
    with tempfile.TemporaryDirectory(prefix=f"dashboard-demo-{case_id}-") as temp:
        checkout = Path(temp)
        source_file = checkout / case.filename
        source_file.parent.mkdir(parents=True, exist_ok=True)
        source_file.write_text(case.source, encoding="utf-8")
        result = await review_diff(
            case.diff,
            str(checkout),
            # Demo mode is deliberately deterministic and never consumes external LLM quota.
            agents=demo_agents(case),
            agent_timeout=config.agent_timeout,
        )
        RefactoringEngine().validate_all(result.findings, result.files)
        payload = result.model_dump(mode="json")
        payload["demo_case"] = case.metadata()
        return payload


# OAuth ------------------------------------------------------------------------------------
@app.get("/auth/status")
def auth_status(request: Request):
    user = current_user(request)
    return {
        "oauth_configured": settings().oauth_configured,
        "password_login_enabled": settings().password_login_enabled,
        "authenticated": user is not None,
        "user": user.public() if user else None,
    }


@app.get("/auth/github/login", include_in_schema=False)
def github_login():
    config = settings()
    if not config.oauth_configured:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    state = auth.new_state()
    response = RedirectResponse(auth.authorize_url(config, state), status_code=302)
    response.set_cookie(
        auth.STATE_COOKIE,
        state,
        max_age=auth.STATE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure(config),
        path="/auth/github",
    )
    return response


def _auth_error_redirect() -> RedirectResponse:
    response = RedirectResponse("/?auth=error", status_code=302)
    response.delete_cookie(auth.STATE_COOKIE, path="/auth/github")
    return response


@app.get("/auth/github/callback", include_in_schema=False)
async def github_callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
):
    config = settings()
    if not config.oauth_configured:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    if error:
        return RedirectResponse("/?auth=denied", status_code=302)
    if not code or not auth.states_match(request.cookies.get(auth.STATE_COOKIE), state):
        return _auth_error_redirect()
    try:
        token = await auth.exchange_code(config, code)
        profile = await GitHubClient(token, config.github_api_url).current_user()
    except (auth.OAuthError, GitHubError):
        # Browsers land here from GitHub: send them back to the dashboard, never raw JSON.
        return _auth_error_redirect()
    session_id = auth.create_session(store(), config, profile, token)
    response = RedirectResponse("/?auth=ok", status_code=302)
    response.delete_cookie(auth.STATE_COOKIE, path="/auth/github")
    response.set_cookie(
        auth.SESSION_COOKIE,
        session_id,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure(config),
    )
    return response


@app.post("/auth/password", dependencies=[Depends(require_same_origin)])
def password_login(body: PasswordLoginRequest, request: Request):
    config = settings()
    if not config.password_login_enabled:
        raise HTTPException(status_code=503, detail="Demo account sign-in is not configured")
    client = client_address(request)
    # Keyed on (username, address) so a stranger cannot lock an evaluator out of their own
    # account; a separate, looser per-address limit still caps spraying across usernames.
    pair = f"{body.username.casefold()}|{client}"
    if login_throttle.locked(pair) or ip_throttle.locked(client):
        raise HTTPException(
            status_code=429, detail="Too many failed sign-in attempts. Try again in 10 minutes."
        )
    if not auth.authenticate(config, body.username, body.password):
        login_throttle.record_failure(pair)
        ip_throttle.record_failure(client)
        raise HTTPException(status_code=401, detail="Invalid username or password")
    login_throttle.reset(pair)
    session_id = auth.create_session(
        store(), config, {"login": body.username, "name": "Demo account"}, None
    )
    response = JSONResponse({"status": "signed_in", "user": {"login": body.username}})
    response.set_cookie(
        auth.SESSION_COOKIE,
        session_id,
        max_age=int(auth.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=auth.cookie_secure(config),
    )
    return response


@app.post("/auth/logout", dependencies=[Depends(require_same_origin)])
def logout(request: Request):
    auth.destroy_session(store(), settings(), request.cookies.get(auth.SESSION_COOKIE))
    response = JSONResponse({"status": "signed_out"})
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


@app.get("/me/repos")
async def my_repositories(user: auth.AuthenticatedUser = Depends(require_login)):
    try:
        return await GitHubClient(user.token, settings().github_api_url).user_repositories()
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/me/repos/{owner}/{repo}/pulls")
async def my_pull_requests(
    owner: str,
    repo: str,
    state: str = "open",
    user: auth.AuthenticatedUser = Depends(require_login),
):
    if state not in {"open", "closed", "all"}:
        raise HTTPException(status_code=422, detail="state must be open, closed or all")
    try:
        return await GitHubClient(user.token, settings().github_api_url).pull_requests(
            owner, repo, state
        )
    except GitHubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


# Reviews and jobs -------------------------------------------------------------------------
@app.post(
    "/review",
    dependencies=[Depends(require_same_origin), Depends(require_viewer), Depends(rate_limit)],
)
async def review(request: ReviewRequest):
    """Review submitted diff data. Static tools are intentionally disabled on this endpoint."""
    config = settings()
    agents = _agents_for(config)
    return await review_diff(
        request.diff,
        agents=agents,
        agent_timeout=config.agent_timeout,
        min_confidence=config.min_confidence,
    )


@app.post("/github/review", status_code=202, dependencies=[Depends(require_same_origin)])
async def review_github(body: GitHubReviewRequest, request: Request):
    """Queue a review of a real pull request and return its job immediately."""
    config = settings()
    user = current_user(request)
    if config.login_required and user is None:
        raise HTTPException(status_code=401, detail=_sign_in_prompt(config))
    if (user is None or user.token is None) and not config.server_github_configured:
        raise HTTPException(status_code=503, detail="GitHub credentials are not configured")
    if body.publish and not config.publish_reviews:
        raise HTTPException(
            status_code=403, detail="Publishing is disabled on this server (PUBLISH_REVIEWS)"
        )
    ref = PullRequestRef(owner=body.owner, repo=body.repo, number=body.number)
    job, created = job_manager().submit(
        ref,
        trigger="dashboard" if user else "api",
        token=user.token if user else None,
        requested_by=user.login if user else None,
        publish=body.publish,
    )
    return {"job": job, "created": created}


@app.get("/jobs")
def list_jobs(limit: int = 25, repository: str | None = None, user=Depends(require_viewer)):
    return store().jobs(limit, repository, viewer_scope(user))


@app.get("/jobs/{job_id}")
def get_job(job_id: str, user=Depends(require_viewer)):
    scope = viewer_scope(user)
    job = store().job(job_id, scope)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job")
    if job["status"] == "completed" and job["review_id"] is not None:
        review_record = store().review(job["review_id"], scope)
        job["result"] = review_record["result"] if review_record else None
    return job


@app.get("/reviews/{review_id}")
def get_review(review_id: ReviewId, user=Depends(require_viewer)):
    record = store().review(review_id, viewer_scope(user))
    if record is None:
        raise HTTPException(status_code=404, detail="Unknown review")
    return record


@app.get("/history/{owner}/{repo}")
def history(owner: str, repo: str, limit: int = 50, user=Depends(require_viewer)):
    return store().history(f"{owner}/{repo}", limit, viewer_scope(user))


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
    x_github_delivery: str | None = Header(default=None),
):
    config = settings()
    if not config.github_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    body = await request.body()
    if not verify_webhook_signature(body, x_hub_signature_256, config.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    if x_github_event == "ping":
        return {"status": "pong"}
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "unsupported event"}
    try:
        payload = json.loads(body)
        if payload.get("action") not in REVIEW_ACTIONS:
            return {"status": "ignored", "reason": "non-review action"}
        if payload["pull_request"].get("draft"):
            return {"status": "ignored", "reason": "draft pull request"}
        owner, repo = payload["repository"]["full_name"].split("/", 1)
        ref = PullRequestRef(
            owner=owner,
            repo=repo,
            number=payload["pull_request"]["number"],
            head_sha=payload["pull_request"]["head"]["sha"],
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed GitHub webhook payload") from exc
    if not config.server_github_configured:
        raise HTTPException(status_code=503, detail="GitHub credentials are not configured")
    job, created = job_manager().submit(
        ref,
        trigger="webhook",
        publish=config.publish_reviews and config.webhook_auto_publish,
        delivery_id=x_github_delivery,
    )
    return {"status": "queued" if created else "duplicate", "job_id": job["id"]}


@app.get("/health")
def health():
    try:
        store().jobs(limit=1)
        database = "ok"
    except Exception:  # noqa: BLE001 - health must report, not raise
        database = "unavailable"
    return {"status": "ok", "service": "sdp-pr-reviewer", "database": database}
