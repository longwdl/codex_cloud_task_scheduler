"""Redact credentials from untrusted command output and error details."""

from __future__ import annotations

import re
from collections.abc import Iterable


_PATTERNS = (
    re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)(\b(?:token|password|api[_-]?key|secret)\s*[=:]\s*[\"']?)([^\s\"',;]+)"),
    re.compile(r"(?i)(\b(?:private[_ -]?key|codex[_ -]?auth)\s*[=:]\s*[\"']?)([^\s\"',;]+)"),
    re.compile(r"(?i)(\b(?:token|password|key)\s+)([^\s,;]+)"),
)

_BARE_TOKEN_PATTERNS = (
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
)


def redact_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace recognizable or explicitly supplied secret values with ``[REDACTED]``."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    redacted = text
    explicit_secrets = {item for item in secrets if isinstance(item, str) and item}
    for secret in sorted(explicit_secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    for pattern in _PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    for pattern in _BARE_TOKEN_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted
