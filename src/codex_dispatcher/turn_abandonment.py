"""Durable evidence for explicit inactive Runner Turn abandonment."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from codex_dispatcher.runner_transport import RunnerInactiveContainerState
from codex_dispatcher.work_items import (
    validate_session_generation_id,
    validate_session_id,
    validate_sha256,
    validate_turn_id,
    validate_work_item_id,
)


@dataclass(frozen=True, slots=True)
class TurnExecutionAbandonment:
    turn_id: str
    work_item_id: str
    session_generation_id: str
    generation_number: int
    policy_sha256: str
    session_id: str | None
    inactive_container_state: RunnerInactiveContainerState
    inactive_observed_at: str
    recorded_at: str

    def __post_init__(self) -> None:
        validate_turn_id(self.turn_id)
        validate_work_item_id(self.work_item_id)
        validate_session_generation_id(self.session_generation_id)
        if type(self.generation_number) is not int or self.generation_number <= 0:
            raise ValueError("generation_number must be a positive integer")
        validate_sha256(self.policy_sha256, "policy_sha256")
        if self.session_id is not None:
            validate_session_id(self.session_id)
        if not isinstance(
            self.inactive_container_state, RunnerInactiveContainerState
        ):
            raise ValueError("inactive_container_state is invalid")
        for value, field in (
            (self.inactive_observed_at, "inactive_observed_at"),
            (self.recorded_at, "recorded_at"),
        ):
            if not isinstance(value, str):
                raise ValueError(f"{field} must be an aware timestamp")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as exc:
                raise ValueError(f"{field} is invalid") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(f"{field} must include a timezone")

