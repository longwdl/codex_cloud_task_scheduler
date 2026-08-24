"""Strict status contract for automatic Control Host reclamation planning."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import stat


STATUS_PATH = Path("/var/lib/codex-dispatcher/control-reclamation-status/latest.json")
MINIMUM_AVAILABLE_BYTES = 8 * 1024 * 1024 * 1024
MAXIMUM_RELEASE_COUNT = 4
MAXIMUM_RECOVERY_ROOT_COUNT = 4
MINIMUM_RECLAIMABLE_BYTES = 1024 * 1024 * 1024
MAXIMUM_STATUS_AGE_SECONDS = 8 * 60 * 60

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
TRIGGER_REASONS = frozenset(
    {
        "host_available_below_threshold",
        "release_count_above_limit",
        "recovery_root_count_above_limit",
        "verified_bundle_targets_present",
        "reclaimable_bytes_above_threshold",
        "offhost_confirmation_missing",
    }
)


class ControlReclamationStatusError(ValueError):
    """Raised when the protected Control reclamation status is invalid."""


@dataclass(frozen=True, slots=True)
class ControlReclamationStatus:
    checked_at: str
    current_release_commit: str
    host_available_bytes: int
    release_count: int
    recovery_root_count: int
    target_count: int
    bundle_target_count: int
    unconfirmed_bundle_count: int
    expected_total_bytes: int
    trigger_reasons: tuple[str, ...]
    plan_sha256: str | None
    minimum_available_bytes: int = MINIMUM_AVAILABLE_BYTES
    maximum_release_count: int = MAXIMUM_RELEASE_COUNT
    maximum_recovery_root_count: int = MAXIMUM_RECOVERY_ROOT_COUNT
    minimum_reclaimable_bytes: int = MINIMUM_RECLAIMABLE_BYTES

    def __post_init__(self) -> None:
        try:
            moment = datetime.fromisoformat(self.checked_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ControlReclamationStatusError("Control reclamation checked_at is invalid") from exc
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ControlReclamationStatusError("Control reclamation checked_at lacks timezone")
        numeric = (
            self.host_available_bytes,
            self.release_count,
            self.recovery_root_count,
            self.target_count,
            self.bundle_target_count,
            self.unconfirmed_bundle_count,
            self.expected_total_bytes,
            self.minimum_available_bytes,
            self.maximum_release_count,
            self.maximum_recovery_root_count,
            self.minimum_reclaimable_bytes,
        )
        if any(type(value) is not int or value < 0 for value in numeric):
            raise ControlReclamationStatusError("Control reclamation values are invalid")
        if (
            not isinstance(self.current_release_commit, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.current_release_commit) is None
            or self.minimum_available_bytes != MINIMUM_AVAILABLE_BYTES
            or self.maximum_release_count != MAXIMUM_RELEASE_COUNT
            or self.maximum_recovery_root_count != MAXIMUM_RECOVERY_ROOT_COUNT
            or self.minimum_reclaimable_bytes != MINIMUM_RECLAIMABLE_BYTES
            or self.maximum_release_count <= 1
            or self.maximum_recovery_root_count <= 0
            or self.bundle_target_count > self.target_count
            or tuple(sorted(self.trigger_reasons)) != self.trigger_reasons
            or len(set(self.trigger_reasons)) != len(self.trigger_reasons)
            or not set(self.trigger_reasons).issubset(TRIGGER_REASONS)
        ):
            raise ControlReclamationStatusError("Control reclamation status is inconsistent")
        expected: set[str] = set()
        if self.host_available_bytes < self.minimum_available_bytes:
            expected.add("host_available_below_threshold")
        if self.release_count > self.maximum_release_count:
            expected.add("release_count_above_limit")
        if self.recovery_root_count > self.maximum_recovery_root_count:
            expected.add("recovery_root_count_above_limit")
        if self.bundle_target_count:
            expected.add("verified_bundle_targets_present")
        if self.expected_total_bytes >= self.minimum_reclaimable_bytes:
            expected.add("reclaimable_bytes_above_threshold")
        if self.unconfirmed_bundle_count:
            expected.add("offhost_confirmation_missing")
        if set(self.trigger_reasons) != expected:
            raise ControlReclamationStatusError("Control reclamation triggers are inconsistent")
        if self.trigger_reasons:
            if not isinstance(self.plan_sha256, str) or _SHA256_RE.fullmatch(
                self.plan_sha256
            ) is None:
                raise ControlReclamationStatusError("triggered Control status requires a plan")
        elif self.plan_sha256 is not None:
            raise ControlReclamationStatusError("healthy Control status cannot publish a plan")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "kind": "control_reclamation_auto_plan_status",
            "checked_at": self.checked_at,
            "current_release_commit": self.current_release_commit,
            "host_available_bytes": self.host_available_bytes,
            "release_count": self.release_count,
            "recovery_root_count": self.recovery_root_count,
            "target_count": self.target_count,
            "bundle_target_count": self.bundle_target_count,
            "unconfirmed_bundle_count": self.unconfirmed_bundle_count,
            "expected_total_bytes": self.expected_total_bytes,
            "minimum_available_bytes": self.minimum_available_bytes,
            "maximum_release_count": self.maximum_release_count,
            "maximum_recovery_root_count": self.maximum_recovery_root_count,
            "minimum_reclaimable_bytes": self.minimum_reclaimable_bytes,
            "trigger_reasons": list(self.trigger_reasons),
            "plan_sha256": self.plan_sha256,
        }

    @classmethod
    def from_mapping(cls, payload: object) -> ControlReclamationStatus:
        expected = {
            "schema_version",
            "kind",
            "checked_at",
            "current_release_commit",
            "host_available_bytes",
            "release_count",
            "recovery_root_count",
            "target_count",
            "bundle_target_count",
            "unconfirmed_bundle_count",
            "expected_total_bytes",
            "minimum_available_bytes",
            "maximum_release_count",
            "maximum_recovery_root_count",
            "minimum_reclaimable_bytes",
            "trigger_reasons",
            "plan_sha256",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("schema_version") != 1
            or payload.get("kind") != "control_reclamation_auto_plan_status"
        ):
            raise ControlReclamationStatusError("Control reclamation fields are invalid")
        reasons = payload.get("trigger_reasons")
        if not isinstance(reasons, list) or not all(isinstance(item, str) for item in reasons):
            raise ControlReclamationStatusError("Control reclamation triggers are invalid")
        return cls(
            checked_at=payload.get("checked_at"),  # type: ignore[arg-type]
            current_release_commit=payload.get("current_release_commit"),  # type: ignore[arg-type]
            host_available_bytes=payload.get("host_available_bytes"),  # type: ignore[arg-type]
            release_count=payload.get("release_count"),  # type: ignore[arg-type]
            recovery_root_count=payload.get("recovery_root_count"),  # type: ignore[arg-type]
            target_count=payload.get("target_count"),  # type: ignore[arg-type]
            bundle_target_count=payload.get("bundle_target_count"),  # type: ignore[arg-type]
            unconfirmed_bundle_count=payload.get("unconfirmed_bundle_count"),  # type: ignore[arg-type]
            expected_total_bytes=payload.get("expected_total_bytes"),  # type: ignore[arg-type]
            minimum_available_bytes=payload.get("minimum_available_bytes"),  # type: ignore[arg-type]
            maximum_release_count=payload.get("maximum_release_count"),  # type: ignore[arg-type]
            maximum_recovery_root_count=payload.get("maximum_recovery_root_count"),  # type: ignore[arg-type]
            minimum_reclaimable_bytes=payload.get("minimum_reclaimable_bytes"),  # type: ignore[arg-type]
            trigger_reasons=tuple(reasons),
            plan_sha256=payload.get("plan_sha256"),  # type: ignore[arg-type]
        )


def build_control_reclamation_status(
    *,
    host_available_bytes: int,
    current_release_commit: str,
    release_count: int,
    recovery_root_count: int,
    target_count: int,
    bundle_target_count: int,
    unconfirmed_bundle_count: int,
    expected_total_bytes: int,
    plan_sha256: str,
    now: datetime | None = None,
) -> ControlReclamationStatus:
    """Apply fixed Control thresholds without granting deletion authority."""
    reasons: list[str] = []
    if host_available_bytes < MINIMUM_AVAILABLE_BYTES:
        reasons.append("host_available_below_threshold")
    if release_count > MAXIMUM_RELEASE_COUNT:
        reasons.append("release_count_above_limit")
    if recovery_root_count > MAXIMUM_RECOVERY_ROOT_COUNT:
        reasons.append("recovery_root_count_above_limit")
    if bundle_target_count:
        reasons.append("verified_bundle_targets_present")
    if expected_total_bytes >= MINIMUM_RECLAIMABLE_BYTES:
        reasons.append("reclaimable_bytes_above_threshold")
    if unconfirmed_bundle_count:
        reasons.append("offhost_confirmation_missing")
    moment = datetime.now(timezone.utc) if now is None else now
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ControlReclamationStatusError("Control reclamation timestamp must be aware")
    return ControlReclamationStatus(
        checked_at=moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        current_release_commit=current_release_commit,
        host_available_bytes=host_available_bytes,
        release_count=release_count,
        recovery_root_count=recovery_root_count,
        target_count=target_count,
        bundle_target_count=bundle_target_count,
        unconfirmed_bundle_count=unconfirmed_bundle_count,
        expected_total_bytes=expected_total_bytes,
        trigger_reasons=tuple(sorted(reasons)),
        plan_sha256=plan_sha256 if reasons else None,
    )


def load_control_reclamation_status(
    path: Path = STATUS_PATH, *, trusted_owner_uid: int = 0
) -> ControlReclamationStatus:
    try:
        metadata = path.stat(follow_symlinks=False)
        raw = path.read_bytes()
    except OSError as exc:
        raise ControlReclamationStatusError("Control reclamation status is unavailable") from exc
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != trusted_owner_uid
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not 0 < len(raw) <= 64 * 1024
    ):
        raise ControlReclamationStatusError("Control reclamation status file is unsafe")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ControlReclamationStatusError("Control reclamation status is malformed") from exc
    return ControlReclamationStatus.from_mapping(payload)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result
