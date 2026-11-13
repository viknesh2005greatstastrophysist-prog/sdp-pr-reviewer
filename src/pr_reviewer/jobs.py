"""Background review jobs.

Webhooks and dashboard requests return immediately with a job id; reviews run in the
background with bounded concurrency, and every state change is persisted so the dashboard
can show queued / running / completed / failed status and survive restarts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from .database import ReviewStore
from .models import PullRequestRef, ReviewResult

log = logging.getLogger(__name__)

Runner = Callable[[PullRequestRef, str | None, bool], Awaitable[ReviewResult]]


class JobManager:
    def __init__(self, store: ReviewStore, runner: Runner, concurrency: int = 2):
        self.store = store
        self.runner = runner
        self.semaphore = asyncio.Semaphore(max(1, concurrency))
        self.tasks: set[asyncio.Task] = set()

    def submit(
        self,
        ref: PullRequestRef,
        *,
        trigger: str,
        token: str | None = None,
        requested_by: str | None = None,
        publish: bool = False,
        delivery_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Queue a review. Returns (job, created). Tokens stay in memory, never in the DB."""
        job, created = self.store.create_job(
            ref,
            trigger=trigger,
            requested_by=requested_by,
            publish_requested=publish,
            delivery_id=delivery_id,
        )
        if created:
            task = asyncio.create_task(self._run(job["id"], ref.model_copy(), token, publish))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return job, created

    async def _run(self, job_id: str, ref: PullRequestRef, token: str | None, publish: bool):
        async with self.semaphore:
            self.store.update_job(job_id, status="running", started_at=datetime.now(UTC))
            try:
                result = await self.runner(ref, token, publish)
            except Exception as exc:  # noqa: BLE001 - every failure must reach the job record
                log.exception("Review job %s failed", job_id)
                self.store.update_job(
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {str(exc)[:500]}",
                    finished_at=datetime.now(UTC),
                )
                return
            self.store.update_job(
                job_id,
                status="completed",
                head_sha=result.head_sha,
                review_id=result.review_id,
                finding_count=len(result.findings),
                published=result.published,
                publish_error=result.publish_error,
                finished_at=datetime.now(UTC),
            )

    async def drain(self) -> None:
        """Wait for in-flight jobs (used by tests and graceful shutdown)."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)
