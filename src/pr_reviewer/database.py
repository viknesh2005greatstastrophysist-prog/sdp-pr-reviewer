from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    select,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import PullRequestRef, ReviewResult

ACTIVE_STATES = ("queued", "running")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:  # SQLite drops tzinfo; values are always stored as UTC.
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class Base(DeclarativeBase):
    pass


class ReviewRecord(Base):
    __tablename__ = "review_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pull_number: Mapped[int] = mapped_column(Integer, index=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finding_count: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReviewJob(Base):
    __tablename__ = "review_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pull_number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    trigger: Mapped[str] = mapped_column(String(16))
    requested_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    publish_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    published: Mapped[bool] = mapped_column(Boolean, default=False)
    publish_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_id: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    review_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finding_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "repository": self.repository,
            "pull_number": self.pull_number,
            "head_sha": self.head_sha,
            "status": self.status,
            "trigger": self.trigger,
            "requested_by": self.requested_by,
            "publish_requested": self.publish_requested,
            "published": self.published,
            "publish_error": self.publish_error,
            "error": self.error,
            "review_id": self.review_id,
            "finding_count": self.finding_count,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
        }


class UserSession(Base):
    """Server-side OAuth session. The cookie holds a random id; only its hash is stored."""

    __tablename__ = "user_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    login: Mapped[str] = mapped_column(String(100))
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    encrypted_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ReviewStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    # Review history -----------------------------------------------------------------
    def save(self, ref: PullRequestRef, result: ReviewResult) -> int:
        record = ReviewRecord(
            repository=f"{ref.owner}/{ref.repo}",
            pull_number=ref.number,
            head_sha=ref.head_sha,
            finding_count=len(result.findings),
            payload=result.model_dump(mode="json"),
        )
        with Session(self.engine) as session:
            session.add(record)
            session.commit()
            session.refresh(record)
            return record.id

    def update_payload(self, review_id: int, result: ReviewResult) -> None:
        with Session(self.engine) as session:
            record = session.get(ReviewRecord, review_id)
            if record is not None:
                record.payload = result.model_dump(mode="json")
                session.commit()

    def history(self, repository: str, limit: int = 50) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ReviewRecord)
                .where(ReviewRecord.repository == repository)
                .order_by(ReviewRecord.created_at.desc())
                .limit(max(1, min(limit, 100)))
            ).all()
            return [
                {
                    "id": row.id,
                    "repository": row.repository,
                    "pull_number": row.pull_number,
                    "head_sha": row.head_sha,
                    "created_at": _iso(row.created_at),
                    "finding_count": row.finding_count,
                }
                for row in rows
            ]

    def review(self, review_id: int) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.get(ReviewRecord, review_id)
            if row is None:
                return None
            return {
                "id": row.id,
                "repository": row.repository,
                "pull_number": row.pull_number,
                "head_sha": row.head_sha,
                "created_at": _iso(row.created_at),
                "finding_count": row.finding_count,
                "result": row.payload,
            }

    # Review jobs --------------------------------------------------------------------
    def create_job(
        self,
        ref: PullRequestRef,
        trigger: str,
        requested_by: str | None = None,
        publish_requested: bool = False,
        delivery_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a queued job, or return (existing, False) for duplicates.

        Duplicates are a repeated webhook delivery id, or an active job for the same pull
        request and head commit, so retries and double clicks never start a second review.
        """
        repository = f"{ref.owner}/{ref.repo}"
        with Session(self.engine) as session:
            if delivery_id:
                existing = session.scalars(
                    select(ReviewJob).where(ReviewJob.delivery_id == delivery_id)
                ).first()
                if existing:
                    return existing.as_dict(), False
            active = session.scalars(
                select(ReviewJob).where(
                    ReviewJob.repository == repository,
                    ReviewJob.pull_number == ref.number,
                    ReviewJob.status.in_(ACTIVE_STATES),
                )
            ).all()
            for job in active:
                if ref.head_sha is None or job.head_sha in (None, ref.head_sha):
                    return job.as_dict(), False
            job = ReviewJob(
                id=str(uuid.uuid4()),
                repository=repository,
                pull_number=ref.number,
                head_sha=ref.head_sha,
                status="queued",
                trigger=trigger,
                requested_by=requested_by,
                publish_requested=publish_requested,
                delivery_id=delivery_id,
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:  # concurrent delivery with the same id
                session.rollback()
                existing = session.scalars(
                    select(ReviewJob).where(ReviewJob.delivery_id == delivery_id)
                ).first()
                if existing is None:
                    raise
                return existing.as_dict(), False
            return job.as_dict(), True

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            job = session.get(ReviewJob, job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            session.commit()
            return job.as_dict()

    def job(self, job_id: str) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            job = session.get(ReviewJob, job_id)
            return job.as_dict() if job else None

    def jobs(self, limit: int = 25, repository: str | None = None) -> list[dict[str, Any]]:
        query = select(ReviewJob).order_by(ReviewJob.created_at.desc())
        if repository:
            query = query.where(ReviewJob.repository == repository)
        with Session(self.engine) as session:
            rows = session.scalars(query.limit(max(1, min(limit, 100)))).all()
            return [row.as_dict() for row in rows]

    def fail_interrupted_jobs(self) -> int:
        """Mark jobs left active by a previous process as failed instead of hanging forever."""
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ReviewJob).where(ReviewJob.status.in_(ACTIVE_STATES))
            ).all()
            for row in rows:
                row.status = "failed"
                row.error = "Interrupted by a service restart; request the review again."
                row.finished_at = _now()
            session.commit()
            return len(rows)

    # OAuth sessions -----------------------------------------------------------------
    def save_session(self, record: UserSession) -> None:
        with Session(self.engine) as session:
            session.add(record)
            session.commit()

    def get_session(self, id_hash: str) -> UserSession | None:
        with Session(self.engine) as session:
            record = session.get(UserSession, id_hash)
            if record is None:
                return None
            expires = record.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= _now():
                session.delete(record)
                session.commit()
                return None
            session.expunge(record)
            return record

    def delete_session(self, id_hash: str) -> None:
        with Session(self.engine) as session:
            session.execute(delete(UserSession).where(UserSession.id_hash == id_hash))
            session.commit()
