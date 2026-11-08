"""Immutable, strict input and result contracts for trusted local controllers."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or ".git" in path.parts
        or "\\" in value
        or any(c in value for c in "*?[]\x00\n")
        or str(path) != value
        or value == "."
    ):
        raise ValueError("require a normalized repository-relative file or directory")
    return value


class Check(Contract):
    id: Identifier
    statement: Annotated[str, Field(min_length=1, max_length=4000)]
    argv: Annotated[list[str], Field(min_length=1, max_length=40)]
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 60
    exclusive: bool = False

    @field_validator("argv")
    @classmethod
    def valid_argv(cls, values: list[str]) -> list[str]:
        if any(not item or "\x00" in item for item in values):
            raise ValueError("argv must contain non-empty strings")
        return values


class TaskSpec(Contract):
    schema_version: Literal["v1"] = "v1"
    id: Identifier
    goal: Annotated[str, Field(min_length=1, max_length=4000)]
    source_repo: Annotated[str, Field(min_length=1)]
    base_ref: Annotated[str, Field(min_length=1)] = "HEAD"
    allowed_paths: Annotated[list[str], Field(min_length=1)]
    protected_paths: list[str] = Field(
        default_factory=lambda: ["tests", "AGENTS.md", "Makefile", "pyproject.toml"]
    )
    context_paths: list[str] = Field(default_factory=list)
    checks: Annotated[list[Check], Field(min_length=1)]
    depends_on: list[Identifier] = Field(default_factory=list)
    complexity: Literal["specified", "design"] = "specified"
    max_attempts: Annotated[int, Field(ge=1, le=5)] = 3
    small_attempts: Annotated[int, Field(ge=1, le=5)] = 2
    lease_seconds: Annotated[int, Field(ge=1, le=86400)] = 1800
    lifetime_seconds: Annotated[int, Field(ge=1, le=604800)] = 86400
    requires_human: bool = False
    effect: Literal["local_code"] = "local_code"

    @field_validator("allowed_paths", "protected_paths", "context_paths")
    @classmethod
    def valid_paths(cls, paths: list[str]) -> list[str]:
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate paths")
        return [relative_path(path) for path in paths]

    @model_validator(mode="after")
    def distinct_checks(self) -> TaskSpec:
        ids = [check.id for check in self.checks]
        if len(ids) != len(set(ids)) or "independent-review" in ids:
            raise ValueError("check identifiers must be distinct and not reserved")
        if self.id in self.depends_on or len(self.depends_on) != len(set(self.depends_on)):
            raise ValueError("invalid dependencies")
        return self


class WorkerReport(Contract):
    token: Identifier
    outcome: Literal["ready", "needs_decision"]


class ReviewReport(Contract):
    token: Identifier
    revision: Annotated[str, Field(min_length=1)]
    verdict: Literal["pass", "changes", "needs_decision"]
    covered_criteria: list[Identifier]
    findings: Annotated[list[str], Field(max_length=30)] = Field(default_factory=list)

    @field_validator("findings")
    @classmethod
    def bounded_findings(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 4000 for value in values):
            raise ValueError("findings must be non-empty and bounded")
        return values
