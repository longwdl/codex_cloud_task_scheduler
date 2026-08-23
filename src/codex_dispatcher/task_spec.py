"""Strict parsing and path policy checks for untrusted issue task specifications."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
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
_STRUCTURED_ACCEPTANCE_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?\[(AC-[1-9][0-9]{0,3})\][ \t]+"
    r"([a-z][a-z0-9-]{0,63})(?::[ \t]*(.*?))?[ \t]*$"
)
_STRUCTURED_ACCEPTANCE_PREFIX_RE = re.compile(
    r"^[ \t]*(?:[-*][ \t]+)?\[AC-"
)
_SUPPORTED_ACCEPTANCE_PREDICATES = frozenset(
    {
        "changed-paths-within-allowed",
        "audit",
        "manual",
        "required-check",
        "task-head-published",
    }
)
_CHECKLIST_PREFIX_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?\[[ xX]\][ \t]+")
_BULLET_PREFIX_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)")


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    """One normalized Issue acceptance item and its optional trusted predicate."""

    criterion_id: str
    description: str
    predicate: str
    argument: str | None = None


@dataclass(frozen=True, slots=True)
class TaskSpec:
    objective: str
    background: str
    scope: str
    non_goals: str
    acceptance_criteria: str
    acceptance_items: tuple[AcceptanceCriterion, ...]
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

    acceptance_items = parse_acceptance_criteria(sections["验收条件"])
    allowed_paths = _parse_allowed_paths(sections["允许修改路径"])
    return TaskSpec(
        objective=sections["目标"], background=sections["背景"], scope=sections["范围"],
        non_goals=sections["非目标"], acceptance_criteria=sections["验收条件"],
        acceptance_items=acceptance_items, allowed_paths=allowed_paths,
        verification_commands=sections["验证命令"],
        blockers=sections["阻塞条件"], deployment_constraints=sections["部署限制"],
    )


def parse_acceptance_criteria(content: str) -> tuple[AcceptanceCriterion, ...]:
    """Parse a small Markdown-compatible AC language without guessing prose semantics.

    Supported machine-evaluable forms are exact, one-per-line directives::

        - [AC-1] required-check: unit-tests
        - [AC-2] changed-paths-within-allowed
        - [AC-3] task-head-published
        - [AC-4] audit: reviewer verifies the behavioral edge cases
        - [AC-5] manual: maintainer verifies the external system

    Every other non-empty line remains an explicit ``manual`` criterion. A line that
    starts like a structured directive but is malformed is rejected so a typo cannot
    silently downgrade an intended automated assertion.
    """
    if not isinstance(content, str) or not content.strip():
        raise TaskSpecError("验收条件 must be non-empty text")
    items: list[AcceptanceCriterion] = []
    structured_ids: set[str] = set()
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        if not raw_line.strip():
            continue
        match = _STRUCTURED_ACCEPTANCE_RE.fullmatch(raw_line)
        if match is not None:
            criterion_id, predicate, raw_argument = match.groups()
            if criterion_id in structured_ids:
                raise TaskSpecError(f"duplicate acceptance criterion id: {criterion_id}")
            structured_ids.add(criterion_id)
            if predicate not in _SUPPORTED_ACCEPTANCE_PREDICATES:
                raise TaskSpecError(
                    f"unsupported acceptance predicate on line {line_number}: {predicate}"
                )
            argument = raw_argument.strip() if raw_argument is not None else None
            if predicate in {"required-check", "audit", "manual"}:
                if argument is None or not _bounded_acceptance_text(argument, maximum=256):
                    raise TaskSpecError(
                        f"{predicate} on line {line_number} needs a bounded argument"
                    )
            elif argument is not None:
                raise TaskSpecError(
                    f"acceptance predicate on line {line_number} does not take an argument"
                )
            items.append(
                AcceptanceCriterion(
                    criterion_id=criterion_id,
                    description=(
                        f"{predicate}: {argument}" if argument is not None else predicate
                    ),
                    predicate=predicate,
                    argument=argument,
                )
            )
            continue
        if _STRUCTURED_ACCEPTANCE_PREFIX_RE.match(raw_line):
            raise TaskSpecError(
                f"malformed structured acceptance criterion on line {line_number}"
            )
        description = _CHECKLIST_PREFIX_RE.sub("", raw_line, count=1)
        description = _BULLET_PREFIX_RE.sub("", description, count=1).strip()
        if not _bounded_acceptance_text(description, maximum=4_096):
            raise TaskSpecError(
                f"manual acceptance criterion on line {line_number} is invalid or too large"
            )
        digest = sha256(description.encode("utf-8")).hexdigest()[:10].upper()
        items.append(
            AcceptanceCriterion(
                criterion_id=f"AC-TEXT-{len(items) + 1:03d}-{digest}",
                description=description,
                predicate="manual",
            )
        )
    if not items:
        raise TaskSpecError("验收条件 must contain at least one criterion")
    if len(items) > 100:
        raise TaskSpecError("验收条件 exceeds the 100-criterion safety boundary")
    if sum(len(item.description.encode("utf-8")) for item in items) > 32 * 1024:
        raise TaskSpecError("验收条件 exceeds the structured evidence size boundary")
    return tuple(items)


def _bounded_acceptance_text(value: str, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= maximum
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
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
