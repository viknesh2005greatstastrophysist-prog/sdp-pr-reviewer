from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import PullRequestRef, ReviewResult


class Base(DeclarativeBase):
    pass


class ReviewRecord(Base):
    __tablename__ = "review_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository: Mapped[str] = mapped_column(String(300), index=True)
    pull_number: Mapped[int] = mapped_column(Integer, index=True)
    head_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    finding_count: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class ReviewStore:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, pool_pre_ping=True)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

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

    def history(self, repository: str, limit: int = 50) -> list[dict[str, Any]]:
        with Session(self.engine) as session:
            rows = session.scalars(
                select(ReviewRecord)
                .where(ReviewRecord.repository == repository)
                .order_by(ReviewRecord.created_at.desc())
                .limit(min(limit, 100))
            ).all()
            return [
                {
                    "id": row.id,
                    "repository": row.repository,
                    "pull_number": row.pull_number,
                    "head_sha": row.head_sha,
                    "created_at": row.created_at.isoformat(),
                    "finding_count": row.finding_count,
                }
                for row in rows
            ]
