from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .agents import SPECIALIZATIONS, LLMReviewAgent, OfflineAgent
from .aggregator import executive_summary
from .database import ReviewStore
from .github import GitHubClient, GitHubError
from .llm import OpenAICompatibleClient, build_llm_client
from .models import PullRequestRef, ReviewResult
from .parser import parse_diff
from .pipeline import review_diff
from .refactor import RefactoringEngine
from .settings import Settings

log = logging.getLogger(__name__)


class ReviewService:
    def __init__(self, settings: Settings, github: GitHubClient, store: ReviewStore | None = None):
        self.settings = settings
        self.github = github
        self.store = store

    def agents(self):
        if not self.settings.llm_api_key:
            return [OfflineAgent(category) for category in SPECIALIZATIONS]
        client = build_llm_client(self.settings)
        return [LLMReviewAgent(category, client) for category in SPECIALIZATIONS]

    def verifier(self) -> OpenAICompatibleClient | None:
        """Client for the verification pass (focused mode with an LLM configured)."""
        if not (self.settings.llm_api_key and self.settings.review_verify):
            return None
        return build_llm_client(self.settings)

    async def review_pull_request(
        self, ref: PullRequestRef, publish: bool | None = None, requested_by: str | None = None
    ) -> ReviewResult:
        """Review one pull request.

        ``publish`` is the per-request opt-in. Publishing additionally requires the
        server-level PUBLISH_REVIEWS permission; ``None`` keeps the legacy behaviour of
        following that setting alone (CLI and older callers).
        """
        metadata = await self.github.pull_request(ref)
        ref.head_sha = metadata["head"]["sha"]
        raw_diff = await self.github.diff(ref)
        parsed = parse_diff(raw_diff)
        with tempfile.TemporaryDirectory(prefix="pr-review-") as temp:
            checkout = Path(temp)
            materialize_warnings = await self.github.materialize_changed_files(
                ref, parsed, checkout
            )
            result = await review_diff(
                raw_diff,
                str(checkout),
                self.agents(),
                agent_timeout=self.settings.agent_timeout,
                min_confidence=self.settings.min_confidence,
                review_mode=self.settings.review_mode,
                verifier=self.verifier(),
            )
            RefactoringEngine().validate_all(result.findings, result.files, checkout)
        result.head_sha = ref.head_sha
        result.warnings.extend(materialize_warnings)
        result.summary = executive_summary(result.findings, result.warnings)
        if self.store:
            result.review_id = self.store.save(ref, result, requested_by=requested_by)
        wants_publish = self.settings.publish_reviews if publish is None else publish
        if wants_publish and self.settings.publish_reviews:
            try:
                await self.github.publish_review(ref, result)
                result.published = True
            except GitHubError as exc:
                # The review itself succeeded and is stored; publishing is best effort.
                log.warning("Publishing review for %s failed: %s", ref, exc)
                result.publish_error = str(exc)[:500]
            if self.store and result.review_id is not None:
                self.store.update_payload(result.review_id, result)
        return result
