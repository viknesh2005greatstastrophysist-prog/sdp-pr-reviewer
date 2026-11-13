from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    inspect,
    or_,
    select,
    text,
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


@dataclass(frozen=True)
class Scope:
    """Which rows a signed-in viewer may read (``None`` instead of a Scope means everything).

    A row is visible when it was requested by one of ``logins``, or has no requester at all
    (webhook / API / server-credential reviews) and ``include_unowned`` is set.
    """

    logins: frozenset[str]
    include_unowned: bool = False


def _scoped(query, column, scope: Scope | None):
    if scope is None:
        return query
    clause = column.in_(sorted(scope.logins)) if scope.logins else column.in_([])
    if scope.include_unowned:
        clause = or_(clause, column.is_(None))
    return query.where(clause)


class ReviewRecord(Base):
    __tablename__ = "review_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pull_number: Mapped[int] = mapped_column(Integer, index=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    finding_count: Mapped[int] = mapped_column(Integer)
    requested_by: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
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
        # create_all never alters an existing table: add columns introduced after first deploy.
        columns = {col["name"] for col in inspect(self.engine).get_columns("review_history")}
        if "requested_by" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE review_history ADD COLUMN requested_by VARCHAR(100)")
                )

    # Review history -----------------------------------------------------------------
    def save(
        self, ref: PullRequestRef, result: ReviewResult, requested_by: str | None = None
    ) -> int:
        record = ReviewRecord(
            requested_by=requested_by,
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

    def history(
        self, repository: str, limit: int = 50, scope: Scope | None = None
    ) -> list[dict[str, Any]]:
        query = select(ReviewRecord).where(ReviewRecord.repository == repository)
        query = _scoped(query, ReviewRecord.requested_by, scope)
        with Session(self.engine) as session:
            rows = session.scalars(
                query.order_by(ReviewRecord.created_at.desc()).limit(max(1, min(limit, 100)))
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

    def review(self, review_id: int, scope: Scope | None = None) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            row = session.scalars(
                _scoped(
                    select(ReviewRecord).where(ReviewRecord.id == review_id),
                    ReviewRecord.requested_by,
                    scope,
                )
            ).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "requested_by": row.requested_by,
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
                if existing and existing.status == "failed":
                    # A redelivery of a failed job must retry, not replay the old failure.
                    existing.delivery_id = None
                    session.commit()
                elif existing:
                    return existing.as_dict(), False
            # Active duplicates are shared only between identical requesters, so one user's
            # job is never handed to (or hidden from) another.
            active = session.scalars(
                select(ReviewJob).where(
                    ReviewJob.repository == repository,
                    ReviewJob.pull_number == ref.number,
                    ReviewJob.status.in_(ACTIVE_STATES),
                    ReviewJob.requested_by == requested_by
                    if requested_by is not None
                    else ReviewJob.requested_by.is_(None),
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

    def job(self, job_id: str, scope: Scope | None = None) -> dict[str, Any] | None:
        with Session(self.engine) as session:
            job = session.scalars(
                _scoped(
                    select(ReviewJob).where(ReviewJob.id == job_id), ReviewJob.requested_by, scope
                )
            ).first()
            return job.as_dict() if job else None

    def jobs(
        self, limit: int = 25, repository: str | None = None, scope: Scope | None = None
    ) -> list[dict[str, Any]]:
        query = select(ReviewJob).order_by(ReviewJob.created_at.desc())
        query = _scoped(query, ReviewJob.requested_by, scope)
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

    def purge_expired_sessions(self) -> int:
        with Session(self.engine) as session:
            result = session.execute(delete(UserSession).where(UserSession.expires_at < _now()))
            session.commit()
            return result.rowcount or 0

    def delete_session(self, id_hash: str) -> None:
        with Session(self.engine) as session:
            session.execute(delete(UserSession).where(UserSession.id_hash == id_hash))
            session.commit()
