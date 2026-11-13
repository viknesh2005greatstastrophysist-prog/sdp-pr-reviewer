from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Severity(StrEnum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class ChangedLine(BaseModel):
    old_line: int | None = None
    new_line: int | None = None
    position: int | None = None
    kind: Literal["ADD", "DELETE", "CONTEXT"]
    text: str


class DiffHunk(BaseModel):
    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: list[ChangedLine] = Field(default_factory=list)


class CodeContext(BaseModel):
    symbol: str | None = None
    node_type: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    source: str = ""


class DiffFile(BaseModel):
    path: str
    old_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    patch: str = ""
    hunks: list[DiffHunk] = Field(default_factory=list)
    lines: list[ChangedLine] = Field(default_factory=list)
    contexts: dict[int, CodeContext] = Field(default_factory=dict)

    def added_lines(self) -> set[int]:
        return {line.new_line for line in self.lines if line.kind == "ADD" and line.new_line}

    def github_position(self, line_number: int) -> int | None:
        return next(
            (
                line.position
                for line in self.lines
                if line.new_line == line_number and line.kind == "ADD"
            ),
            None,
        )


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_line: int
    end_line: int
    replacement: str
    rationale: str
    validated: bool = False

    @field_validator("replacement")
    @classmethod
    def no_markdown_fence(cls, value: str) -> str:
        if "```" in value:
            raise ValueError("replacement must be raw code, not a Markdown fence")
        return value


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    line: int = Field(ge=1)
    end_line: int | None = None
    category: str
    severity: Severity
    title: str = "Review finding"
    message: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    source: str
    confidence: float = Field(default=0.8, ge=0, le=1)
    rule_id: str | None = None
    suggestion: Suggestion | None = None


class PullRequestRef(BaseModel):
    owner: str
    repo: str
    number: int = Field(gt=0)
    head_sha: str | None = None


class AgentRun(BaseModel):
    agent: str
    status: Literal["success", "failed", "timed_out"]
    duration_ms: int = Field(ge=0)
    finding_count: int = Field(ge=0)
    error: str | None = None


class ReviewResult(BaseModel):
    files: list[DiffFile]
    findings: list[Finding]
    warnings: list[str] = Field(default_factory=list)
    agent_runs: list[AgentRun] = Field(default_factory=list)
    summary: str = ""
    head_sha: str | None = None
