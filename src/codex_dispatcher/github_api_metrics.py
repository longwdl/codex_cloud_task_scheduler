"""Bounded, durable GitHub API command metrics for one Control sweep."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class GitHubApiSweepOutcome(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True, slots=True)
class GitHubApiMetrics:
    command_count: int
    read_count: int
    write_count: int
    failure_count: int
    elapsed_milliseconds: int
    core_remaining: int | None = None
    core_limit: int | None = None
    core_reset_epoch: int | None = None
    graphql_remaining: int | None = None
    graphql_limit: int | None = None
    graphql_reset_epoch: int | None = None
    rate_limit_error: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "command_count",
            "read_count",
            "write_count",
            "failure_count",
            "elapsed_milliseconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.read_count + self.write_count != self.command_count:
            raise ValueError("GitHub API read/write counts must equal command_count")
        if self.failure_count > self.command_count:
            raise ValueError("GitHub API failure_count exceeds command_count")
        self._validate_budget("core")
        self._validate_budget("graphql")
        if self.rate_limit_error not in {None, "github_rate_limit_unavailable"}:
            raise ValueError("GitHub API rate_limit_error is invalid")

    def _validate_budget(self, name: str) -> None:
        remaining = getattr(self, f"{name}_remaining")
        limit = getattr(self, f"{name}_limit")
        reset = getattr(self, f"{name}_reset_epoch")
        values = (remaining, limit, reset)
        if all(value is None for value in values):
            return
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError(f"GitHub API {name} budget must be complete and non-negative")
        assert isinstance(remaining, int) and isinstance(limit, int)
        if remaining > limit:
            raise ValueError(f"GitHub API {name} remaining exceeds limit")

    def to_mapping(self) -> dict[str, object]:
        return {
            "command_count": self.command_count,
            "read_count": self.read_count,
            "write_count": self.write_count,
            "failure_count": self.failure_count,
            "elapsed_milliseconds": self.elapsed_milliseconds,
            "core_remaining": self.core_remaining,
            "core_limit": self.core_limit,
            "core_reset_epoch": self.core_reset_epoch,
            "graphql_remaining": self.graphql_remaining,
            "graphql_limit": self.graphql_limit,
            "graphql_reset_epoch": self.graphql_reset_epoch,
            "rate_limit_error": self.rate_limit_error,
        }


@dataclass(frozen=True, slots=True)
class GitHubApiSweepMetric:
    sequence: int
    started_at: str
    completed_at: str
    outcome: GitHubApiSweepOutcome
    sweep_status: str | None
    error_code: str | None
    metrics: GitHubApiMetrics

    def to_mapping(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "outcome": self.outcome.value,
            "sweep_status": self.sweep_status,
            "error_code": self.error_code,
            **self.metrics.to_mapping(),
        }


class GitHubApiMetricsCollector:
    """Aggregate every GitHub CLI command issued by collaborating adapters."""

    def __init__(self) -> None:
        self._command_count = 0
        self._read_count = 0
        self._write_count = 0
        self._failure_count = 0
        self._elapsed_milliseconds = 0

    def record_command(
        self, *, read: bool, failed: bool, elapsed_milliseconds: int
    ) -> None:
        if type(read) is not bool or type(failed) is not bool:
            raise TypeError("GitHub API command flags must be booleans")
        if type(elapsed_milliseconds) is not int or elapsed_milliseconds < 0:
            raise ValueError("GitHub API elapsed time must be non-negative")
        self._command_count += 1
        if read:
            self._read_count += 1
        else:
            self._write_count += 1
        if failed:
            self._failure_count += 1
        self._elapsed_milliseconds += elapsed_milliseconds

    def snapshot(
        self,
        *,
        core_remaining: int | None = None,
        core_limit: int | None = None,
        core_reset_epoch: int | None = None,
        graphql_remaining: int | None = None,
        graphql_limit: int | None = None,
        graphql_reset_epoch: int | None = None,
        rate_limit_error: str | None = None,
    ) -> GitHubApiMetrics:
        return GitHubApiMetrics(
            command_count=self._command_count,
            read_count=self._read_count,
            write_count=self._write_count,
            failure_count=self._failure_count,
            elapsed_milliseconds=self._elapsed_milliseconds,
            core_remaining=core_remaining,
            core_limit=core_limit,
            core_reset_epoch=core_reset_epoch,
            graphql_remaining=graphql_remaining,
            graphql_limit=graphql_limit,
            graphql_reset_epoch=graphql_reset_epoch,
            rate_limit_error=rate_limit_error,
        )
