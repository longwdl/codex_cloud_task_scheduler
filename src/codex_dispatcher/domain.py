"""Small environment-independent domain helpers shared by current state models."""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now_iso() -> str:
    """Return an RFC 3339 timestamp in UTC with an explicit ``Z`` suffix."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class InvalidStateTransition(ValueError):
    """Raised when a durable state model rejects a transition."""
