from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from .agents import SPECIALIZATIONS, LLMReviewAgent, OfflineAgent
from .aggregator import executive_summary
from .database import ReviewStore
from .github import GitHubClient, GitHubError
from .llm import OpenAICompatibleClient
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
        client = OpenAICompatibleClient(
            api_key=self.settings.llm_api_key,
            model=self.settings.llm_model,
            base_url=self.settings.llm_base_url,
            timeout=self.settings.agent_timeout,
        )
        return [LLMReviewAgent(category, client) for category in SPECIALIZATIONS]

    async def review_pull_request(
        self, ref: PullRequestRef, publish: bool | None = None
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
            )
        result.head_sha = ref.head_sha
        result.warnings.extend(materialize_warnings)
        RefactoringEngine().validate_all(result.findings, result.files)
        result.summary = executive_summary(result.findings, result.warnings)
        if self.store:
            result.review_id = self.store.save(ref, result)
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
