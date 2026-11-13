from __future__ import annotations

import json
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agents import SPECIALIZATIONS, LLMReviewAgent
from .database import ReviewStore
from .demos import DEMO_CASES, demo_agents
from .github import GitHubClient, app_installation_client, verify_webhook_signature
from .llm import OpenAICompatibleClient
from .models import PullRequestRef
from .pipeline import review_diff
from .refactor import RefactoringEngine
from .service import ReviewService
from .settings import Settings

app = FastAPI(title="Evidence-Grounded Multi-Agent PR Reviewer", version="1.0.0")
STATIC_DIR = Path(__file__).with_name("static")
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="dashboard-assets")


class ReviewRequest(BaseModel):
    diff: str = Field(max_length=5_000_000)


class GitHubReviewRequest(BaseModel):
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    number: int = Field(gt=0)


class DemoReviewRequest(BaseModel):
    case_id: str = Field(default="full-spectrum", pattern=r"^[a-z0-9-]+$")


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


@lru_cache(maxsize=1)
def settings() -> Settings:
    return Settings.from_env()


async def _github_service(ref: PullRequestRef) -> ReviewService:
    config = settings()
    if config.github_token:
        github = GitHubClient(config.github_token, config.github_api_url)
    elif config.github_app_id and config.github_private_key:
        github = await app_installation_client(
            config.github_app_id,
            config.github_private_key,
            ref.owner,
            ref.repo,
            config.github_api_url,
        )
    else:
        raise HTTPException(status_code=503, detail="GitHub credentials are not configured")
    store = ReviewStore(config.database_url)
    store.create_schema()
    return ReviewService(config, github, store)


@app.post("/review")
async def review(request: ReviewRequest):
    """Review submitted diff data. Static tools are intentionally disabled on this endpoint."""
    config = settings()
    agents = _agents_for(config)
    return await review_diff(request.diff, agents=agents, agent_timeout=config.agent_timeout)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard/status")
def dashboard_status():
    """Return capability state without exposing credentials or secret values."""
    config = settings()
    return {
        "service": "ready",
        "github_connected": bool(
            config.github_token or (config.github_app_id and config.github_private_key)
        ),
        "llm_connected": bool(config.llm_api_key),
        "webhook_configured": bool(config.github_webhook_secret),
        "publishing_enabled": config.publish_reviews,
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


@app.post("/dashboard/demo")
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


@app.post("/github/review")
async def review_github(request: GitHubReviewRequest):
    ref = PullRequestRef(**request.model_dump())
    service = await _github_service(ref)
    return await service.review_pull_request(ref)


@app.post("/webhooks/github", status_code=202)
async def github_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
):
    config = settings()
    if not config.github_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    body = await request.body()
    if not verify_webhook_signature(body, x_hub_signature_256, config.github_webhook_secret):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": "unsupported event"}
    try:
        payload = json.loads(body)
        if payload.get("action") not in {"opened", "reopened", "synchronize", "ready_for_review"}:
            return {"status": "ignored", "reason": "non-review action"}
        repository = payload["repository"]["full_name"].split("/", 1)
        ref = PullRequestRef(
            owner=repository[0], repo=repository[1], number=payload["pull_request"]["number"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed GitHub webhook payload") from exc
    service = await _github_service(ref)
    result = await service.review_pull_request(ref)
    return {"status": "reviewed", "findings": len(result.findings), "head_sha": result.head_sha}


@app.get("/history/{owner}/{repo}")
def history(owner: str, repo: str, limit: int = 50):
    config = settings()
    store = ReviewStore(config.database_url)
    store.create_schema()
    return store.history(f"{owner}/{repo}", limit)


@app.get("/health")
def health():
    return {"status": "ok", "service": "sdp-pr-reviewer"}
