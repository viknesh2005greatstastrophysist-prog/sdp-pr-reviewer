"""Build an evaluation corpus from human inline review comments on merged public PRs.

Ground truth here is weak by design: a human review comment on an added line is treated as
"a reviewer thought this line deserved attention". At build time categories come from the
transparent keyword heuristic below (label_source = "heuristic-category"); the separate
labelling step (pr_reviewer.labeling) then adds an LLM actionable/category judgement. Neither is
a human label. Each comment also records whether it was later ADDRESSED (outdated, or the line
was changed by a later commit), which is independent evidence that it was actionable.

Repository code is fetched as data through the contents API and is never executed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import sys
import time
from base64 import b64decode
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx

from .evaluation import EvaluationCase, load_jsonl
from .models import Finding, Severity
from .parser import parse_diff

DEFAULT_REPOS = (
    "pallets/flask",
    "psf/requests",
    "encode/httpx",
    "pydantic/pydantic",
    "fastapi/fastapi",
    "python-poetry/poetry",
    "pallets/click",
    "Kludex/starlette",
    "pytest-dev/pytest",
    "fastapi/sqlmodel",
    "pallets/werkzeug",
    "pallets/jinja",
    "aio-libs/aiohttp",
    "psf/black",
    "scikit-learn/scikit-learn",
)
SPLIT_SEED = "sdp-pr-reviewer-split-v1"
COMMENT_TEXT_CHARS = 2000
MIN_FINDINGS_PER_CASE = 2
MAX_DIFF_BYTES = 200_000
MAX_PY_FILES = 25
MAX_FILE_BYTES = 1_000_000
MESSAGE_CHARS = 500
LABEL_SOURCE = "heuristic-category"
JSON_ACCEPT = "application/vnd.github+json"
DIFF_ACCEPT = "application/vnd.github.v3.diff"
MAX_RATE_WAIT = 3700.0

# Keyword heuristic. Each pattern that matches counts once for its category; the category with
# the most matches wins, ties resolve in CATEGORY_PRIORITY order, no match falls back to
# DEFAULT_CATEGORY. This is a documented approximation, not a human label.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "bug-hunter": (
        r"\bbugs?\b",
        r"\bwrong\b",
        r"\bincorrect",
        r"\bfail(s|ed|ing|ure|ures)?\b",
        r"\bexceptions?\b",
        r"\braises?\b",
        r"\bcrash",
        r"\bnone\b",
        r"\bnull\b",
        r"\brace condition",
        r"\bdeadlock",
        r"\bthread[- ]?safe",
        r"\bsecurity\b",
        r"\bvulnerab",
        r"\binjection\b",
        r"\bunsafe\b",
        r"\bleaks?\b",
        r"\bedge cases?\b",
        r"\binfinite loop",
        r"\boff[- ]by[- ]one",
        r"\bunhandled\b",
        r"\bbroken\b",
        r"\bwon'?t work",
        r"\bdoesn'?t work",
        r"\bnever (called|used|reached)\b",
    ),
    "performance": (
        r"\bperformance\b",
        r"\bslow(er|ly)?\b",
        r"\bfast(er)?\b",
        r"\bspeed",
        r"\boptimi[sz]",
        r"\bcach(e|es|ed|ing)\b",
        r"\bmemory\b",
        r"\bo\(",
        r"\bquadratic\b",
        r"\bcomplexity\b",
        r"\bbenchmark",
        r"\boverhead\b",
        r"\blatency\b",
        r"\bexpensive\b",
        r"\ballocat",
        r"\blazy\b",
        r"\bn\+1\b",
    ),
    "architecture": (
        r"\barchitectur",
        r"\bdesign\b",
        r"\bpublic api\b",
        r"\bbackwards?[- ]compat",
        r"\bdeprecat",
        r"\bbreaking change",
        r"\binterface\b",
        r"\babstraction",
        r"\bcoupl",
        r"\bcircular import",
        r"\brefactor",
        r"\bseparation of concerns",
        r"\bresponsibilit",
        r"\bextensib",
        r"\bdependenc(y|ies)\b",
    ),
    "style": (
        r"\bnits?\b",
        r"\btypos?\b",
        r"\bnaming\b",
        r"\brename",
        r"\bdocstring",
        r"\bformat",
        r"\bwhitespace\b",
        r"\bindent",
        r"\btype hints?\b",
        r"\bannotation",
        r"\breadab",
        r"\bspelling\b",
        r"\blint",
        r"\bconsisten",
        r"\bunused\b",
        r"\bsimplif",
        r"\bpythonic\b",
        r"\bf-string",
        r"\bcomments?\b",
    ),
}
CATEGORY_PRIORITY = ("bug-hunter", "performance", "architecture", "style")
DEFAULT_CATEGORY = "style"
_COMPILED = {
    category: [re.compile(pattern, re.IGNORECASE) for pattern in patterns]
    for category, patterns in CATEGORY_KEYWORDS.items()
}


def classify_comment(text: str) -> str:
    """Assign one of the project's four categories with the keyword heuristic above."""
    scores = {
        category: sum(1 for pattern in patterns if pattern.search(text))
        for category, patterns in _COMPILED.items()
    }
    best = max(scores.values())
    if best == 0:
        return DEFAULT_CATEGORY
    return next(category for category in CATEGORY_PRIORITY if scores[category] == best)


def safe_relative_path(path: str) -> str | None:
    """Return a normalised relative POSIX path, or None if it could escape a directory."""
    if not path or "\x00" in path:
        return None
    candidate = PurePosixPath(path.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    return candidate.as_posix()


def safe_target(destination: Path, path: str) -> Path | None:
    relative = safe_relative_path(path)
    if relative is None:
        return None
    destination = destination.resolve()
    target = (destination / relative).resolve()
    try:
        target.relative_to(destination)
    except ValueError:
        return None
    return target


def comment_line(comment: dict[str, Any]) -> int | None:
    """Line in the blob at original_commit_id (the commit the comment was written against)."""
    value = comment.get("original_line")
    if value is None:
        value = comment.get("line")
    return value if isinstance(value, int) and value >= 1 else None


def select_comments(comments: list[dict[str, Any]], pr_author: str | None) -> list[dict[str, Any]]:
    """Keep top-level human comments on the RIGHT side of a .py file, by someone other than
    the PR author, with a usable line and commit."""
    kept: list[dict[str, Any]] = []
    for item in comments:
        user = item.get("user") or {}
        login = user.get("login") or ""
        if user.get("type") != "User" or login.endswith("[bot]"):
            continue
        if pr_author and login == pr_author:
            continue
        if item.get("in_reply_to_id") is not None:
            continue
        if item.get("side") != "RIGHT":
            continue
        path = item.get("path") or ""
        if not path.endswith(".py") or safe_relative_path(path) is None:
            continue
        if comment_line(item) is None or not item.get("original_commit_id"):
            continue
        if not (item.get("body") or "").strip():
            continue
        kept.append(item)
    return kept


def group_by_commit(comments: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Commits ordered by number of kept comments, largest first (ties: first seen)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in comments:
        groups.setdefault(item["original_commit_id"], []).append(item)
    return sorted(groups.items(), key=lambda pair: -len(pair[1]))


def python_only_diff(raw: str) -> str:
    sections = re.split(r"(?m)^(?=diff --git )", raw)
    kept = [
        section
        for section in sections
        if section.startswith("diff --git ") and (section.splitlines()[0].strip().endswith(".py"))
    ]
    return "".join(kept)


def keep_added_line_comments(comments: list[dict[str, Any]], diff: str) -> list[dict[str, Any]]:
    added = {item.path: item.added_lines() for item in parse_diff(diff)}
    return [c for c in comments if comment_line(c) in added.get(c["path"], set())]


def split_repos(repos: list[str], seed: str = SPLIT_SEED) -> dict[str, str]:
    """Assign whole repositories to "dev" or "test" (about half each).

    The order is a seeded hash of the repository name, so the split is reproducible and cannot
    depend on how any strategy scores on a repository. Splitting by repository (not by case)
    keeps one project's idioms from leaking between the tuning and the reporting data.
    """
    ranked = sorted(
        set(repos), key=lambda name: hashlib.sha256(f"{seed}:{name}".encode()).hexdigest()
    )
    cut = (len(ranked) + 1) // 2
    return {name: ("dev" if index < cut else "test") for index, name in enumerate(ranked)}


def is_outdated(comment: dict[str, Any]) -> bool:
    """GitHub nulls `line` once the commented code is no longer in the PR's latest diff."""
    return comment.get("line") is None and comment.get("original_line") is not None


def changed_later_lines(later_diff: str) -> dict[str, set[int]]:
    """Lines (old-side numbering, per path) that a later commit deleted or rewrote."""
    removed: dict[str, set[int]] = {}
    for item in parse_diff(later_diff):
        deleted = {line.old_line for line in item.lines if line.kind == "DELETE" and line.old_line}
        removed.setdefault(item.path, set()).update(deleted)
        if item.old_path:
            removed.setdefault(item.old_path, set()).update(deleted)
    return removed


def addressed_evidence(comment: dict[str, Any], removed: dict[str, set[int]] | None) -> dict:
    """Did the PR later deal with this comment?

    outdated: GitHub marked it outdated (can also happen after a force-push or rebase, so it
    is evidence, not proof). changed_later: a commit after the one the comment was written on
    deleted or rewrote exactly that line (None when the later diff was unavailable).
    """
    outdated = is_outdated(comment)
    changed = (
        None if removed is None else comment_line(comment) in removed.get(comment["path"], set())
    )
    return {
        "outdated": outdated,
        "changed_later": changed,
        "addressed": bool(outdated or changed),
    }


def comment_to_finding(comment: dict[str, Any]) -> Finding:
    body = comment["body"].strip()
    return Finding(
        path=comment["path"],
        line=comment_line(comment) or 1,
        category=classify_comment(body),
        severity=Severity.MEDIUM,
        title="Human review comment",
        message=body[:MESSAGE_CHARS],
        evidence=comment.get("html_url") or "https://github.com (comment URL unavailable)",
        source="human-review",
        confidence=1.0,
    )


class GitHubFetcher:
    """Minimal read-only GitHub client with rate-limit handling. The token is only ever put in
    the Authorization header; it is not logged and not included in error messages."""

    def __init__(
        self,
        token: str,
        *,
        api_url: str = "https://api.github.com",
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ):
        self._client = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            timeout=60,
            follow_redirects=True,  # renamed repositories answer with 301
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._sleep = sleep
        self._clock = clock

    async def aclose(self) -> None:
        await self._client.aclose()

    def _rate_wait(self, response: httpx.Response) -> float | None:
        if response.status_code not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after and retry_after.isdigit():
            return min(float(retry_after) + 1, MAX_RATE_WAIT)
        if response.headers.get("x-ratelimit-remaining") == "0":
            reset = response.headers.get("x-ratelimit-reset", "")
            if reset.isdigit():
                return min(max(float(reset) - self._clock(), 0) + 2, MAX_RATE_WAIT)
            return 60.0
        if "rate limit" in response.text.lower():
            return 60.0
        return None

    async def get(
        self, path: str, *, params: dict[str, Any] | None = None, accept: str = JSON_ACCEPT
    ) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(8):
            try:
                response = await self._client.get(path, params=params, headers={"Accept": accept})
            except httpx.TransportError:
                await self._sleep(2.0 * (attempt + 1))
                continue
            wait = self._rate_wait(response)
            if wait is not None:
                print(
                    f"rate limited on {path.split('?')[0]}; sleeping {wait:.0f}s", file=sys.stderr
                )
                await self._sleep(wait)
                continue
            if response.status_code in (502, 503, 504):
                await self._sleep(2.0 * (attempt + 1))
                continue
            return response
        raise RuntimeError(f"GitHub request failed after retries: {path}")

    async def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any | None:
        response = await self.get(path, params=params)
        return response.json() if response.status_code == 200 else None


async def fetch_all_comments(fetcher: GitHubFetcher, owner: str, repo: str, number: int) -> list:
    comments: list[dict[str, Any]] = []
    for page in (1, 2, 3):
        batch = await fetcher.get_json(
            f"/repos/{owner}/{repo}/pulls/{number}/comments", {"per_page": 100, "page": page}
        )
        if not batch:
            break
        comments.extend(batch)
        if len(batch) < 100:
            break
    return comments


async def materialize(
    fetcher: GitHubFetcher, owner: str, repo: str, sha: str, paths: list[str], destination: Path
) -> set[str]:
    written: set[str] = set()
    for path in paths:
        target = safe_target(destination, path)
        if target is None:
            continue
        payload = await fetcher.get_json(
            f"/repos/{owner}/{repo}/contents/{quote(path, safe='/')}", {"ref": sha}
        )
        if (
            not isinstance(payload, dict)
            or payload.get("encoding") != "base64"
            or int(payload.get("size", 0)) > MAX_FILE_BYTES
        ):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b64decode(payload["content"]))
        written.add(path)
    return written


async def _later_changes(
    fetcher: GitHubFetcher, owner: str, repo: str, commit: str, head: dict[str, Any]
) -> dict[str, set[int]] | None:
    """Old-side lines changed between the commented commit and the PR head, or None if unknown."""
    head_sha = head.get("sha")
    if not head_sha:
        return None
    if head_sha == commit:
        return {}
    response = await fetcher.get(
        f"/repos/{owner}/{repo}/compare/{commit}...{head_sha}", accept=DIFF_ACCEPT
    )
    if response.status_code != 200 or len(response.content) > 4 * MAX_DIFF_BYTES:
        return None
    try:
        return changed_later_lines(python_only_diff(response.text))
    except ValueError:
        return None


async def build_case(
    fetcher: GitHubFetcher,
    owner: str,
    repo: str,
    pr: dict[str, Any],
    repos_dir: Path,
    *,
    min_findings: int = MIN_FINDINGS_PER_CASE,
    split: str | None = None,
) -> EvaluationCase | None:
    number = pr["number"]
    author = (pr.get("user") or {}).get("login")
    kept = select_comments(await fetch_all_comments(fetcher, owner, repo, number), author)
    if not kept:
        return None
    base_sha = pr["base"]["sha"]
    for commit, group in group_by_commit(kept)[:2]:
        response = await fetcher.get(
            f"/repos/{owner}/{repo}/compare/{base_sha}...{commit}", accept=DIFF_ACCEPT
        )
        if response.status_code != 200 or len(response.content) > MAX_DIFF_BYTES:
            continue
        diff = python_only_diff(response.text)
        if not diff:
            continue
        try:
            usable = keep_added_line_comments(group, diff)
            files = parse_diff(diff)
        except ValueError:
            continue
        if len(usable) < min_findings:
            continue
        changed = [f.path for f in files if f.status != "deleted"]
        if len(changed) > MAX_PY_FILES:
            continue
        case_id = f"{owner}__{repo}__{number}"
        destination = repos_dir / case_id
        written = await materialize(fetcher, owner, repo, commit, changed, destination)
        usable = [c for c in usable if c["path"] in written]
        if len(usable) < min_findings:
            continue
        removed = await _later_changes(fetcher, owner, repo, commit, (pr.get("head") or {}))
        findings = [comment_to_finding(c) for c in usable]
        return EvaluationCase(
            case_id=case_id,
            diff=diff,
            human_findings=findings,
            repo_path=str(destination),
            metadata={
                "repo": f"{owner}/{repo}",
                "pr_number": number,
                "pr_url": pr.get("html_url") or f"https://github.com/{owner}/{repo}/pull/{number}",
                "commit_sha": commit,
                "base_sha": base_sha,
                "head_sha": (pr.get("head") or {}).get("sha"),
                "split": split,
                "finding_labels": [
                    {
                        "label_source": LABEL_SOURCE,
                        "comment_id": c.get("id"),
                        "comment_url": c.get("html_url"),
                        "comment_text": c["body"].strip()[:COMMENT_TEXT_CHARS],
                        "heuristic_category": classify_comment(c["body"]),
                        **addressed_evidence(c, removed),
                    }
                    for c in usable
                ],
            },
        )
    return None


async def build_repo_cases(
    fetcher: GitHubFetcher,
    full_name: str,
    *,
    max_prs: int,
    max_cases: int,
    repos_dir: Path,
    min_findings: int = MIN_FINDINGS_PER_CASE,
    split: str | None = None,
    skip_merged: int = 0,
    already: int = 0,
) -> list[EvaluationCase]:
    """`skip_merged` ignores that many most-recent merged PRs (already scanned by an earlier
    pass); `already` is the number of cases the repository has from that pass (counts toward
    `max_cases`)."""
    owner, repo = full_name.split("/", 1)
    cases: list[EvaluationCase] = []
    scanned = 0
    seen_merged = 0
    page = 1
    while scanned < max_prs and len(cases) + already < max_cases:
        batch = await fetcher.get_json(
            f"/repos/{owner}/{repo}/pulls",
            {
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": 50,
                "page": page,
            },
        )
        if not batch:
            break
        for pr in batch:
            if not pr.get("merged_at"):
                continue
            seen_merged += 1
            if seen_merged <= skip_merged:
                continue
            scanned += 1
            case = await build_case(
                fetcher, owner, repo, pr, repos_dir, min_findings=min_findings, split=split
            )
            if case:
                cases.append(case)
                print(f"{full_name}: case {case.case_id} ({len(case.human_findings)} findings)")
            if scanned >= max_prs or len(cases) + already >= max_cases:
                break
        page += 1
    print(f"{full_name}: scanned {scanned} merged PRs, kept {len(cases)} cases")
    return cases


async def build_corpus(
    repos: list[str],
    *,
    token: str,
    max_prs: int,
    max_cases_per_repo: int,
    repos_dir: Path,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    min_findings: int = MIN_FINDINGS_PER_CASE,
    skip_merged: int = 0,
    existing: dict[str, int] | None = None,
) -> list[EvaluationCase]:
    fetcher = GitHubFetcher(token, transport=transport, sleep=sleep)
    # The split is defined over the full default list, so adding repositories later never
    # moves an existing repository between dev and test.
    splits = split_repos(sorted(set(DEFAULT_REPOS) | set(repos)))
    existing = existing or {}
    cases: list[EvaluationCase] = []
    limit = asyncio.Semaphore(4)

    async def one(full_name: str) -> list[EvaluationCase]:
        async with limit:
            return await build_repo_cases(
                fetcher,
                full_name,
                max_prs=max_prs,
                max_cases=max_cases_per_repo,
                repos_dir=repos_dir,
                min_findings=min_findings,
                split=splits[full_name],
                skip_merged=skip_merged,
                already=existing.get(full_name, 0),
            )

    try:
        for batch in await asyncio.gather(*(one(name) for name in repos)):
            cases.extend(batch)
    finally:
        await fetcher.aclose()
    return sorted(cases, key=lambda c: (c.metadata["repo"], c.metadata["pr_number"]))


def write_corpus(cases: list[EvaluationCase], output: Path) -> str:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = "".join(case.model_dump_json() + "\n" for case in cases)
    output.write_text(rows, encoding="utf-8")
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()


def corpus_card(cases: list[EvaluationCase], sha256: str, built_on: str) -> str:
    """Markdown card. Works on a raw corpus and on one that the labelling step has enriched."""
    per_repo = Counter(c.metadata["repo"] for c in cases)
    split_of = {c.metadata["repo"]: c.metadata.get("split") or "-" for c in cases}
    findings_per_repo: Counter[str] = Counter()
    labels = [(case, label) for case in cases for label in case.metadata.get("finding_labels", [])]
    for case in cases:
        findings_per_repo[case.metadata["repo"]] += len(case.human_findings)
    heuristic = Counter(label.get("heuristic_category", "?") for _, label in labels)
    labelled = [label for _, label in labels if "actionable" in label]
    addressed = [label for _, label in labels if label.get("addressed")]
    split_cases = Counter(c.metadata.get("split") or "-" for c in cases)
    split_findings: Counter[str] = Counter()
    for case in cases:
        split_findings[case.metadata.get("split") or "-"] += len(case.human_findings)
    lines = [
        "# Evaluation corpus card",
        "",
        f"- Built (UTC): {built_on}",
        f"- Corpus SHA-256 (evaluation/corpus.jsonl): `{sha256}`",
        f"- Cases: {len(cases)}; human-comment findings: {sum(findings_per_repo.values())}",
        "- Source: public merged PRs; inline human review comments on added `.py` lines; "
        f"every case has at least {MIN_FINDINGS_PER_CASE} kept comments.",
        f"- Split: whole repositories, seeded hash order (`{SPLIT_SEED}`), first half `dev`, "
        "second half `test`. Rules and thresholds may be tuned on `dev` only; `test` is "
        "reported, never tuned on.",
        "- Location is human. Heuristic category is a keyword label; the LLM classifier "
        "(see RESULTS.md) adds actionable/category judgements. Neither is a human label.",
        f"- Comments later addressed (outdated or line changed by a later commit): "
        f"{len(addressed)} of {sum(findings_per_repo.values())}",
    ]
    if labelled:
        actionable = sum(1 for label in labelled if label["actionable"])
        lines.append(
            f"- LLM-classified comments: {len(labelled)}; actionable: {actionable} "
            f"({actionable / len(labelled):.1%})"
        )
    lines += ["", "| Repo | Split | Cases | Findings |", "| --- | --- | ---: | ---: |"]
    lines += [
        f"| {repo} | {split_of[repo]} | {per_repo[repo]} | {findings_per_repo[repo]} |"
        for repo in sorted(per_repo)
    ]
    lines += ["", "| Split | Cases | Findings |", "| --- | ---: | ---: |"]
    lines += [
        f"| {name} | {split_cases[name]} | {split_findings[name]} |" for name in sorted(split_cases)
    ]
    lines += ["", "| Heuristic category | Findings |", "| --- | ---: |"]
    lines += [f"| {name} | {heuristic[name]} |" for name in sorted(heuristic)]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build an evaluation corpus from public PRs")
    parser.add_argument("repos", nargs="*", default=list(DEFAULT_REPOS), help="owner/name")
    parser.add_argument("--max-prs", type=int, default=150, help="merged PRs scanned per repo")
    parser.add_argument("--max-cases-per-repo", type=int, default=9)
    parser.add_argument(
        "--skip-merged", type=int, default=0, help="skip the N most recent merged PRs (scanned)"
    )
    parser.add_argument(
        "--merge", action="store_true", help="add to the existing corpus instead of replacing it"
    )
    parser.add_argument("--min-findings", type=int, default=MIN_FINDINGS_PER_CASE)
    parser.add_argument("--output", type=Path, default=Path("evaluation/corpus.jsonl"))
    parser.add_argument("--repos-dir", type=Path, default=Path("evaluation/repos"))
    parser.add_argument("--card", type=Path, default=Path("evaluation/CORPUS.md"))
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN (authenticated requests; 5000/hour limit).")
    previous: list[EvaluationCase] = []
    if args.merge and args.output.exists():
        previous = load_jsonl(args.output)
    cases = asyncio.run(
        build_corpus(
            args.repos,
            token=token,
            max_prs=args.max_prs,
            max_cases_per_repo=args.max_cases_per_repo,
            repos_dir=args.repos_dir,
            min_findings=args.min_findings,
            skip_merged=args.skip_merged,
            existing=dict(Counter(c.metadata["repo"] for c in previous)),
        )
    )
    known = {c.case_id for c in previous}
    cases = sorted(
        previous + [c for c in cases if c.case_id not in known],
        key=lambda c: (c.metadata["repo"], c.metadata["pr_number"]),
    )
    if not cases:
        raise SystemExit("No cases built; corpus not written.")
    # One split over the final repository set, applied to every case (also merged ones).
    final = split_repos(sorted({c.metadata["repo"] for c in cases}))
    for case in cases:
        case.metadata["split"] = final[case.metadata["repo"]]
    digest = write_corpus(cases, args.output)
    args.card.write_text(
        corpus_card(cases, digest, datetime.now(UTC).strftime("%Y-%m-%d %H:%M")), encoding="utf-8"
    )
    print(f"Wrote {len(cases)} case(s) to {args.output} (sha256 {digest})")


if __name__ == "__main__":
    main()
