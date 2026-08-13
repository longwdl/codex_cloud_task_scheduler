"""Strict parsing and path policy checks for untrusted issue task specifications."""

from __future__ import annotations

from dataclasses import dataclass
import re


class TaskSpecError(ValueError):
    """Raised when an issue does not meet the fail-closed task template."""


_HEADINGS = (
    "目标",
    "背景",
    "范围",
    "非目标",
    "验收条件",
    "允许修改路径",
    "验证命令",
    "阻塞条件",
    "部署限制",
)
_HEADING_RE = re.compile(r"(?m)^##[ \t]+(.+?)[ \t]*$")
_GLOB_CHARS = frozenset("*?[]{}")


@dataclass(frozen=True, slots=True)
class TaskSpec:
    objective: str
    background: str
    scope: str
    non_goals: str
    acceptance_criteria: str
    allowed_paths: tuple[str, ...]
    verification_commands: str
    blockers: str
    deployment_constraints: str

    def permits_path(self, path: str) -> bool:
        return is_path_allowed(path, self.allowed_paths)


def normalize_repo_path(path: str) -> str:
    """Return a normalized repository-relative POSIX path, or reject it."""
    if not isinstance(path, str):
        raise TaskSpecError("path must be a string")
    if (
        not path
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise TaskSpecError("path must be a non-empty POSIX path")
    if path.startswith("/") or path.endswith("/"):
        raise TaskSpecError("path must be a non-root repository-relative path")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise TaskSpecError("path must not contain empty, dot, or parent components")
    if any(any(character in _GLOB_CHARS for character in part) for part in parts):
        raise TaskSpecError("path must not contain glob syntax")
    return "/".join(parts)


def is_hard_denied_path(path: str) -> bool:
    """Check the static denylist after strict normalization."""
    try:
        normalized = normalize_repo_path(path)
    except TaskSpecError:
        return True
    parts = normalized.split("/")
    lower_parts = [part.lower() for part in parts]
    for part in lower_parts:
        if part == ".env" or part.startswith(".env."):
            return True
        if (
            part in {".git", "key", "codeowners"}
            or part.startswith("id_rsa")
            or part.endswith((".pem", ".key"))
        ):
            return True
    lowered = "/".join(lower_parts)
    return (
        lowered == ".github/workflows"
        or lowered.startswith(".github/workflows/")
        or lowered == "infra/production"
        or lowered.startswith("infra/production/")
        or lowered == "terraform/production"
        or lowered.startswith("terraform/production/")
    )


def is_path_allowed(
    path: str,
    allowed_paths: tuple[str, ...] | list[str],
    denied_paths: tuple[str, ...] | list[str] = (),
) -> bool:
    """Return whether *path* is under one explicit allowed path and not denied."""
    try:
        normalized = normalize_repo_path(path)
    except TaskSpecError:
        return False
    if is_hard_denied_path(normalized):
        return False
    for denied in denied_paths:
        try:
            normalized_denied = normalize_repo_path(denied)
        except TaskSpecError:
            return False
        if normalized == normalized_denied or normalized.startswith(normalized_denied + "/"):
            return False
    for allowed in allowed_paths:
        try:
            normalized_allowed = normalize_repo_path(allowed)
        except TaskSpecError:
            continue
        if normalized == normalized_allowed or normalized.startswith(normalized_allowed + "/"):
            return True
    return False


def parse_task_spec(issue_body: str) -> TaskSpec:
    """Parse the mandatory issue sections without accepting duplicate headings."""
    if not isinstance(issue_body, str):
        raise TaskSpecError("issue body must be a string")
    matches = list(_HEADING_RE.finditer(issue_body))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        if heading not in _HEADINGS:
            continue
        if heading in sections:
            raise TaskSpecError(f"duplicate required section: {heading}")
        end = matches[index + 1].start() if index + 1 < len(matches) else len(issue_body)
        content = issue_body[match.end() : end].strip()
        if not content:
            raise TaskSpecError(f"empty required section: {heading}")
        sections[heading] = content
    missing = [heading for heading in _HEADINGS if heading not in sections]
    if missing:
        raise TaskSpecError("missing required section(s): " + ", ".join(missing))

    allowed_paths = _parse_allowed_paths(sections["允许修改路径"])
    return TaskSpec(
        objective=sections["目标"], background=sections["背景"], scope=sections["范围"],
        non_goals=sections["非目标"], acceptance_criteria=sections["验收条件"],
        allowed_paths=allowed_paths, verification_commands=sections["验证命令"],
        blockers=sections["阻塞条件"], deployment_constraints=sections["部署限制"],
    )


def _parse_allowed_paths(content: str) -> tuple[str, ...]:
    paths: list[str] = []
    for line in content.splitlines():
        candidate = line.strip()
        if candidate.startswith(("- ", "* ")):
            candidate = candidate[2:].strip()
        if not candidate:
            continue
        paths.append(normalize_repo_path(candidate))
    if not paths:
        raise TaskSpecError("允许修改路径 must contain at least one path")
    if any(is_hard_denied_path(path) for path in paths):
        raise TaskSpecError("允许修改路径 contains a hard-denied path")
    return tuple(dict.fromkeys(paths))
