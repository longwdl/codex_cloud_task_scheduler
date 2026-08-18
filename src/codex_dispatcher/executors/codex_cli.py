"""Pure invocation planning for persistent non-interactive Codex CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from codex_dispatcher.work_items import validate_session_id


_FIXED_AUTH_CONFIG = (
    "-c",
    'forced_login_method="chatgpt"',
    "-c",
    'cli_auth_credentials_store="file"',
)


@dataclass(frozen=True, slots=True)
class CodexInvocationPlan:
    argv: tuple[str, ...]
    cwd: Path
    environment: Mapping[str, str]
    reads_prompt_from_stdin: bool = True


@dataclass(frozen=True, slots=True)
class CodexAuthenticationPlan:
    argv: tuple[str, ...]
    environment: Mapping[str, str]


def build_codex_login_status_invocation(
    *,
    codex_path: Path,
    codex_home: Path,
) -> CodexAuthenticationPlan:
    """Build the fixed, non-secret authentication preflight invocation."""
    _validate_paths(((codex_path, "codex_path"), (codex_home, "codex_home")))
    return CodexAuthenticationPlan(
        (str(codex_path), *_FIXED_AUTH_CONFIG, "login", "status"),
        _codex_environment(codex_home),
    )


def build_codex_invocation(
    *,
    codex_path: Path,
    repository_directory: Path,
    codex_home: Path,
    output_schema: Path,
    session_id: str | None = None,
) -> CodexInvocationPlan:
    """Build fixed argv; the prompt is deliberately absent and must use standard input."""
    _validate_paths(
        (
            (codex_path, "codex_path"),
            (repository_directory, "repository_directory"),
            (codex_home, "codex_home"),
            (output_schema, "output_schema"),
        )
    )
    common = (
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema",
        str(output_schema),
    )
    if session_id is None:
        argv = (str(codex_path), *_FIXED_AUTH_CONFIG, "exec", *common, "-")
    else:
        session_id = validate_session_id(session_id)
        argv = (
            str(codex_path),
            *_FIXED_AUTH_CONFIG,
            "exec",
            "resume",
            session_id,
            *common,
            "-",
        )
    return CodexInvocationPlan(
        argv,
        repository_directory,
        _codex_environment(codex_home),
    )


def _validate_paths(paths: tuple[tuple[Path, str], ...]) -> None:
    for path, field in paths:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError(f"{field} must be an absolute path")
        if ".." in path.parts or "\x00" in str(path):
            raise ValueError(f"{field} must be a normalized absolute path")


def _codex_environment(codex_home: Path) -> Mapping[str, str]:
    return MappingProxyType(
        {
            "CODEX_HOME": str(codex_home),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        }
    )
