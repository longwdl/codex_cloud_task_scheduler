"""Strict read-only status contract for automatic Runner reclamation planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat


STATUS_PATH = Path("/srv/codex-runner/reclamation-status/latest.json")
MINIMUM_AVAILABLE_BYTES = 64 * 1024 * 1024 * 1024
MAXIMUM_RELEASE_COUNT = 4
MINIMUM_RECLAIMABLE_BYTES = 8 * 1024 * 1024 * 1024
MAXIMUM_STATUS_AGE_SECONDS = 8 * 60 * 60

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
TRIGGER_REASONS = frozenset(
    {
        "host_available_below_threshold",
        "release_count_above_limit",
        "unreferenced_images_present",
        "reclaimable_bytes_above_threshold",
    }
)


class RunnerReclamationStatusError(ValueError):
    """Raised when protected automatic-planning status is missing or invalid."""


@dataclass(frozen=True, slots=True)
class RunnerReclamationStatus:
    checked_at: str
    host_available_bytes: int
    release_count: int
    release_target_count: int
    image_target_count: int
    expected_total_bytes: int
    trigger_reasons: tuple[str, ...]
    plan_sha256: str | None
    minimum_available_bytes: int = MINIMUM_AVAILABLE_BYTES
    maximum_release_count: int = MAXIMUM_RELEASE_COUNT
    minimum_reclaimable_bytes: int = MINIMUM_RECLAIMABLE_BYTES

    def __post_init__(self) -> None:
        try:
            moment = datetime.fromisoformat(self.checked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise RunnerReclamationStatusError("reclamation checked_at is invalid") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise RunnerReclamationStatusError("reclamation checked_at lacks timezone")
        numeric = (
            self.host_available_bytes,
            self.release_count,
            self.release_target_count,
            self.image_target_count,
            self.expected_total_bytes,
            self.minimum_available_bytes,
            self.maximum_release_count,
            self.minimum_reclaimable_bytes,
        )
        if any(type(value) is not int or value < 0 for value in numeric):
            raise RunnerReclamationStatusError("reclamation status values are invalid")
        if (
            self.minimum_available_bytes <= 0
            or self.maximum_release_count <= 1
            or self.minimum_reclaimable_bytes <= 0
            or self.minimum_available_bytes != MINIMUM_AVAILABLE_BYTES
            or self.maximum_release_count != MAXIMUM_RELEASE_COUNT
            or self.minimum_reclaimable_bytes != MINIMUM_RECLAIMABLE_BYTES
            or self.release_target_count > self.release_count
            or len(self.trigger_reasons) != len(set(self.trigger_reasons))
            or tuple(sorted(self.trigger_reasons)) != self.trigger_reasons
            or not set(self.trigger_reasons).issubset(TRIGGER_REASONS)
        ):
            raise RunnerReclamationStatusError("reclamation status is inconsistent")
        expected_reasons: set[str] = set()
        if self.host_available_bytes < self.minimum_available_bytes:
            expected_reasons.add("host_available_below_threshold")
        if self.release_count > self.maximum_release_count:
            expected_reasons.add("release_count_above_limit")
        if self.image_target_count:
            expected_reasons.add("unreferenced_images_present")
        if self.expected_total_bytes >= self.minimum_reclaimable_bytes:
            expected_reasons.add("reclaimable_bytes_above_threshold")
        if set(self.trigger_reasons) != expected_reasons:
            raise RunnerReclamationStatusError("reclamation trigger reasons are inconsistent")
        if self.trigger_reasons:
            if not isinstance(self.plan_sha256, str) or _SHA256_RE.fullmatch(
                self.plan_sha256
            ) is None:
                raise RunnerReclamationStatusError("triggered status requires exact plan")
        elif self.plan_sha256 is not None:
            raise RunnerReclamationStatusError("untriggered status cannot publish a plan")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "runner_reclamation_auto_plan_status",
            "checked_at": self.checked_at,
            "host_available_bytes": self.host_available_bytes,
            "release_count": self.release_count,
            "release_target_count": self.release_target_count,
            "image_target_count": self.image_target_count,
            "expected_total_bytes": self.expected_total_bytes,
            "minimum_available_bytes": self.minimum_available_bytes,
            "maximum_release_count": self.maximum_release_count,
            "minimum_reclaimable_bytes": self.minimum_reclaimable_bytes,
            "trigger_reasons": list(self.trigger_reasons),
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> RunnerReclamationStatus:
        expected = {
            "schema_version",
            "kind",
            "checked_at",
            "host_available_bytes",
            "release_count",
            "release_target_count",
            "image_target_count",
            "expected_total_bytes",
            "minimum_available_bytes",
            "maximum_release_count",
            "minimum_reclaimable_bytes",
            "trigger_reasons",
            "plan_sha256",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("schema_version") != 1
            or payload.get("kind") != "runner_reclamation_auto_plan_status"
        ):
            raise RunnerReclamationStatusError("reclamation status fields are invalid")
        reasons = payload.get("trigger_reasons")
        if not isinstance(reasons, list) or not all(
            isinstance(value, str) for value in reasons
        ):
            raise RunnerReclamationStatusError("reclamation trigger reasons are invalid")
        return cls(
            checked_at=payload.get("checked_at"),  # type: ignore[arg-type]
            host_available_bytes=payload.get("host_available_bytes"),  # type: ignore[arg-type]
            release_count=payload.get("release_count"),  # type: ignore[arg-type]
            release_target_count=payload.get("release_target_count"),  # type: ignore[arg-type]
            image_target_count=payload.get("image_target_count"),  # type: ignore[arg-type]
            expected_total_bytes=payload.get("expected_total_bytes"),  # type: ignore[arg-type]
            trigger_reasons=tuple(reasons),
            plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
            minimum_available_bytes=payload.get("minimum_available_bytes"),  # type: ignore[arg-type]
            maximum_release_count=payload.get("maximum_release_count"),  # type: ignore[arg-type]
            minimum_reclaimable_bytes=payload.get("minimum_reclaimable_bytes"),  # type: ignore[arg-type]
        )


def read_runner_reclamation_status(
    path: Path = STATUS_PATH, *, trusted_owner_uid: int = 0
) -> RunnerReclamationStatus:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise RunnerReclamationStatusError("reclamation status is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != trusted_owner_uid
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= 64 * 1024
    ):
        raise RunnerReclamationStatusError("reclamation status file is unsafe")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RunnerReclamationStatusError("reclamation status is malformed") from exc
    return RunnerReclamationStatus.from_mapping(payload)


def build_runner_reclamation_status(
    *,
    host_available_bytes: int,
    release_count: int,
    release_target_count: int,
    image_target_count: int,
    expected_total_bytes: int,
    plan_sha256: str,
    now: datetime | None = None,
) -> RunnerReclamationStatus:
    """Apply the reviewed fixed thresholds without granting deletion authority."""
    reasons: list[str] = []
    if host_available_bytes < MINIMUM_AVAILABLE_BYTES:
        reasons.append("host_available_below_threshold")
    if release_count > MAXIMUM_RELEASE_COUNT:
        reasons.append("release_count_above_limit")
    if image_target_count:
        reasons.append("unreferenced_images_present")
    if expected_total_bytes >= MINIMUM_RECLAIMABLE_BYTES:
        reasons.append("reclaimable_bytes_above_threshold")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise RunnerReclamationStatusError("reclamation timestamp must be timezone-aware")
    return RunnerReclamationStatus(
        checked_at=moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        host_available_bytes=host_available_bytes,
        release_count=release_count,
        release_target_count=release_target_count,
        image_target_count=image_target_count,
        expected_total_bytes=expected_total_bytes,
        trigger_reasons=tuple(sorted(reasons)),
        plan_sha256=plan_sha256 if reasons else None,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError("duplicate JSON field")
        payload[key] = value
    return payload
