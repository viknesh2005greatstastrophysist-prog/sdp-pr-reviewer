from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import jwt

from .models import DiffFile, PullRequestRef, ReviewResult
from .refactor import RefactoringEngine

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.5


class GitHubError(RuntimeError):
    pass


def verify_webhook_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not signature.startswith("sha256=") or not secret:
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.transport = transport

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Send with bounded retry for rate limits, 5xx responses and transport errors."""
        headers = kwargs.pop("headers", self.headers)
        response: httpx.Response | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                async with httpx.AsyncClient(timeout=30, transport=self.transport) as client:
                    response = await client.request(method, url, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise GitHubError(f"GitHub API {method} unreachable: {exc}") from exc
                await asyncio.sleep(RETRY_BASE_DELAY * 2**attempt)
                continue
            retryable = response.status_code in {429, 502, 503, 504} or (
                response.status_code == 403 and response.headers.get("x-ratelimit-remaining") == "0"
            )
            if not retryable or attempt == RETRY_ATTEMPTS - 1:
                return response
            await asyncio.sleep(RETRY_BASE_DELAY * 2**attempt)
        assert response is not None
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = await self._send(method, f"{self.api_url}{path}", **kwargs)
        if response.status_code >= 400:
            raise GitHubError(
                f"GitHub API {method} {path} returned {response.status_code}: {response.text[:300]}"
            )
        return response

    async def pull_request(self, ref: PullRequestRef) -> dict[str, Any]:
        return (
            await self._request("GET", f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}")
        ).json()

    async def diff(self, ref: PullRequestRef) -> str:
        headers = {**self.headers, "Accept": "application/vnd.github.v3.diff"}
        response = await self._send(
            "GET",
            f"{self.api_url}/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}",
            headers=headers,
        )
        if response.status_code >= 400:
            raise GitHubError(f"Unable to fetch PR diff: {response.status_code}")
        return response.text

    async def current_user(self) -> dict[str, Any]:
        return (await self._request("GET", "/user")).json()

    async def user_repositories(self, limit: int = 100) -> list[dict[str, Any]]:
        repos = (
            await self._request(
                "GET",
                "/user/repos",
                params={
                    "sort": "updated",
                    "per_page": min(limit, 100),
                    "affiliation": "owner,collaborator,organization_member",
                },
            )
        ).json()
        return [
            {
                "full_name": item["full_name"],
                "owner": item["owner"]["login"],
                "name": item["name"],
                "private": item.get("private", False),
                "language": item.get("language"),
                "updated_at": item.get("updated_at"),
                "open_issues": item.get("open_issues_count", 0),
            }
            for item in repos
        ]

    async def pull_requests(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        pulls = (
            await self._request(
                "GET", f"/repos/{owner}/{repo}/pulls", params={"state": state, "per_page": 50}
            )
        ).json()
        return [
            {
                "number": item["number"],
                "title": item["title"],
                "author": (item.get("user") or {}).get("login"),
                "head_sha": item["head"]["sha"],
                "updated_at": item.get("updated_at"),
                "draft": item.get("draft", False),
                "html_url": item.get("html_url"),
            }
            for item in pulls
        ]

    async def materialize_changed_files(
        self, ref: PullRequestRef, files: list[DiffFile], destination: Path
    ) -> list[str]:
        """Fetch source as data through the contents API; never run repository setup hooks."""
        warnings: list[str] = []
        destination = destination.resolve()
        for item in files:
            # Only Python source is fetched: analyzer configuration files (ruff.toml,
            # pyproject.toml, .semgrepignore, ...) from the PR must never reach the checkout.
            if item.status == "deleted" or not item.path.endswith(".py"):
                continue
            target = (destination / item.path).resolve()
            try:
                target.relative_to(destination)
            except ValueError:
                warnings.append(f"Rejected unsafe checkout path {item.path}")
                continue
            try:
                response = await self._request(
                    "GET",
                    f"/repos/{ref.owner}/{ref.repo}/contents/{quote(item.path, safe='/')}",
                    params={"ref": ref.head_sha},
                )
                payload = response.json()
                if payload.get("encoding") != "base64" or int(payload.get("size", 0)) > 1_000_000:
                    warnings.append(f"Skipped unsupported or oversized content {item.path}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(base64.b64decode(payload["content"], validate=True))
            except (GitHubError, ValueError, OSError) as exc:
                warnings.append(f"Could not materialize {item.path}: {exc}")
        return warnings

    async def publish_review(self, ref: PullRequestRef, result: ReviewResult) -> dict[str, Any]:
        if not ref.head_sha:
            raise GitHubError("head SHA is required to publish a review")
        files = {item.path: item for item in result.files}
        renderer = RefactoringEngine()
        comments = []
        for finding in result.findings:
            diff_file = files.get(finding.path)
            if diff_file is None or finding.line not in diff_file.added_lines():
                continue
            suggestion = renderer.publishable_suggestion(finding, diff_file)
            comment: dict[str, Any] = {
                "path": finding.path,
                "line": suggestion.end_line if suggestion else finding.line,
                "side": "RIGHT",
                "body": renderer.github_body(finding, include_suggestion=suggestion is not None),
            }
            if suggestion and suggestion.start_line < suggestion.end_line:
                comment["start_line"] = suggestion.start_line
                comment["start_side"] = "RIGHT"
            comments.append(comment)
        payload = {
            "commit_id": ref.head_sha,
            "event": "COMMENT",
            "body": result.summary,
            "comments": comments[:50],
        }
        return (
            await self._request(
                "POST", f"/repos/{ref.owner}/{ref.repo}/pulls/{ref.number}/reviews", json=payload
            )
        ).json()


async def app_installation_client(
    app_id: str, private_key: str, owner: str, repo: str, api_url: str = "https://api.github.com"
) -> GitHubClient:
    now = int(time.time())
    app_jwt = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": app_id}, private_key, algorithm="RS256"
    )
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        installation = await client.get(
            f"{api_url}/repos/{owner}/{repo}/installation", headers=headers
        )
        if installation.status_code >= 400:
            raise GitHubError(
                f"Unable to resolve GitHub App installation: {installation.status_code}"
            )
        installation_id = installation.json()["id"]
        token_response = await client.post(
            f"{api_url}/app/installations/{installation_id}/access_tokens", headers=headers
        )
        if token_response.status_code >= 400:
            raise GitHubError(f"Unable to create installation token: {token_response.status_code}")
    return GitHubClient(token_response.json()["token"], api_url)
