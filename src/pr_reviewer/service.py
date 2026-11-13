from __future__ import annotations

import tempfile
from pathlib import Path

from .agents import SPECIALIZATIONS, LLMReviewAgent, OfflineAgent
from .aggregator import executive_summary
from .database import ReviewStore
from .github import GitHubClient
from .llm import OpenAICompatibleClient
from .models import PullRequestRef, ReviewResult
from .parser import parse_diff
from .pipeline import review_diff
from .refactor import RefactoringEngine
from .settings import Settings


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

    async def review_pull_request(self, ref: PullRequestRef) -> ReviewResult:
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
            )
        result.head_sha = ref.head_sha
        result.warnings.extend(materialize_warnings)
        RefactoringEngine().validate_all(result.findings, result.files)
        result.summary = executive_summary(result.findings, result.warnings)
        if self.store:
            self.store.save(ref, result)
        if self.settings.publish_reviews:
            await self.github.publish_review(ref, result)
        return result
