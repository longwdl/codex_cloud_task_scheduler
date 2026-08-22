"""Pure invocation planning for persistent non-interactive Codex CLI sessions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from codex_dispatcher.work_items import validate_session_id


_FIXED_AUTH_CONFIG = (
    "-c",
    'forced_login_method="chatgpt"',
    "-c",
    'cli_auth_credentials_store="file"',
)
_RUNNER_POLICY_CONFIG = (
    "--strict-config",
    "--model",
    "gpt-5.6-sol",
    "-c",
    'model_reasoning_effort="xhigh"',
    "--enable",
    "multi_agent",
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
    egress_proxy_url: str | None = None,
) -> CodexAuthenticationPlan:
    """Build the fixed, non-secret authentication preflight invocation."""
    _validate_paths(((codex_path, "codex_path"), (codex_home, "codex_home")))
    return CodexAuthenticationPlan(
        (str(codex_path), *_FIXED_AUTH_CONFIG, "login", "status"),
        _codex_environment(codex_home, egress_proxy_url),
    )


def build_codex_invocation(
    *,
    codex_path: Path,
    repository_directory: Path,
    codex_home: Path,
    output_schema: Path,
    session_id: str | None = None,
    egress_proxy_url: str | None = None,
    enable_runner_policy: bool = False,
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
    if not isinstance(enable_runner_policy, bool):
        raise ValueError("enable_runner_policy must be a bool")
    policy = _RUNNER_POLICY_CONFIG if enable_runner_policy else ()
    common = (
        "--json",
        "--dangerously-bypass-approvals-and-sandbox",
        "--output-schema",
        str(output_schema),
    )
    if session_id is None:
        argv = (
            str(codex_path),
            *_FIXED_AUTH_CONFIG,
            *policy,
            "exec",
            *common,
            "-",
        )
    else:
        session_id = validate_session_id(session_id)
        argv = (
            str(codex_path),
            *_FIXED_AUTH_CONFIG,
            *policy,
            "exec",
            "resume",
            session_id,
            *common,
            "-",
        )
    return CodexInvocationPlan(
        argv,
        repository_directory,
        _codex_environment(codex_home, egress_proxy_url),
    )


def _validate_paths(paths: tuple[tuple[Path, str], ...]) -> None:
    for path, field in paths:
        if not isinstance(path, Path) or not path.is_absolute():
            raise ValueError(f"{field} must be an absolute path")
        if ".." in path.parts or "\x00" in str(path):
            raise ValueError(f"{field} must be a normalized absolute path")


def validate_egress_proxy_url(value: str) -> str:
    """Accept one credential-free, canonical HTTP proxy endpoint."""
    if (
        not isinstance(value, str)
        or not value
        or not value.isascii()
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ValueError("egress_proxy_url must be a canonical HTTP URL")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("egress_proxy_url must be a canonical HTTP URL") from exc
    hostname = parsed.hostname
    if (
        parsed.scheme != "http"
        or not hostname
        or port is None
        or port == 0
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or ":" in hostname
        or value != f"http://{hostname}:{port}"
    ):
        raise ValueError(
            "egress_proxy_url must be canonical http://host:port without credentials"
        )
    labels = hostname.split(".")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or any(
            not (character.islower() or character.isdigit() or character == "-")
            for character in label
        )
        for label in labels
    ):
        raise ValueError("egress_proxy_url host is invalid")
    return value


def _codex_environment(
    codex_home: Path,
    egress_proxy_url: str | None,
) -> Mapping[str, str]:
    environment = {
        "CODEX_HOME": str(codex_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
    }
    if egress_proxy_url is not None:
        proxy_url = validate_egress_proxy_url(egress_proxy_url)
        environment.update(
            {
                "HTTP_PROXY": proxy_url,
                "HTTPS_PROXY": proxy_url,
                "ALL_PROXY": proxy_url,
                "NO_PROXY": "",
                "http_proxy": proxy_url,
                "https_proxy": proxy_url,
                "all_proxy": proxy_url,
                "no_proxy": "",
            }
        )
    return MappingProxyType(environment)
