"""Fail-closed GitHub Actions evidence importer using bounded ``gh api`` reads."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import quote

from codex_dispatcher.ci_evidence import (
    ActionsEvidenceSnapshot,
    ActionsRunEvidence,
    CiEvidenceError,
    RequiredCheckEvidence,
    RequiredCheckStatus,
)
from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.work_items import (
    validate_branch,
    validate_git_sha,
    validate_repository,
)


_RUN_PROJECTION = (
    "{total_count:.total_count,runs:[.workflow_runs[] | "
    "{id,name,workflow_id,head_branch,head_sha,event,status,conclusion,"
    "run_attempt,created_at,updated_at,html_url,"
    "repository:.repository.full_name,head_repository:.head_repository.full_name}]}"
)


class GitHubActionsEvidenceImporter:
    """Import exact-head workflow facts without relying on the denied Checks API."""

    def __init__(
        self,
        *,
        gh_path: str | Path,
        token: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        candidate = str(gh_path)
        if not candidate or not Path(candidate).is_absolute():
            raise ValueError("gh_path must be a non-empty absolute path")
        if token is not None and (
            not isinstance(token, str) or not token or "\x00" in token
        ):
            raise ValueError("token must be a non-empty string without NUL or None")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._gh_path = candidate
        self._token = token
        self._timeout_seconds = timeout_seconds

    def import_for_head(
        self,
        *,
        repository: str,
        task_branch: str,
        head_sha: str,
        required_checks: tuple[str, ...],
        observed_at: str,
    ) -> ActionsEvidenceSnapshot:
        repository = validate_repository(repository)
        task_branch = validate_branch(task_branch)
        head_sha = validate_git_sha(head_sha, "head_sha")
        _validate_required_checks(required_checks)

        first_ref = self._read_ref(repository, task_branch)
        if first_ref != head_sha:
            raise CiEvidenceError("remote task ref does not equal the requested HEAD")
        runs = self._read_runs(repository, task_branch)
        second_ref = self._read_ref(repository, task_branch)
        if second_ref != first_ref:
            raise CiEvidenceError("remote task ref changed during Actions observation")

        exact_runs = tuple(
            run
            for run in runs
            if run.repository == repository
            and run.head_repository == repository
            and run.head_branch == task_branch
            and run.head_sha == head_sha
        )
        selected: list[RequiredCheckEvidence] = []
        for name in required_checks:
            matches = tuple(run for run in exact_runs if run.name == name)
            if any(run.event != "pull_request" for run in matches):
                raise CiEvidenceError(
                    f"required check '{name}' has a non-pull_request Actions run"
                )
            if len(matches) > 1:
                raise CiEvidenceError(
                    f"multiple Actions runs ambiguously match required check '{name}'"
                )
            if not matches:
                selected.append(
                    RequiredCheckEvidence(
                        name=name,
                        status=RequiredCheckStatus.NOT_OBSERVED,
                        run=None,
                    )
                )
            else:
                run = matches[0]
                selected.append(
                    RequiredCheckEvidence(
                        name=name,
                        status=run.check_status,
                        run=run,
                    )
                )
        return ActionsEvidenceSnapshot(
            repository=repository,
            task_branch=task_branch,
            head_sha=head_sha,
            remote_ref_sha=second_ref,
            observed_at=observed_at,
            required_checks=tuple(selected),
        )

    def _read_ref(self, repository: str, task_branch: str) -> str:
        value = self._json_command(
            (
                self._gh_path,
                "api",
                "--method",
                "GET",
                "-H",
                "Accept: application/vnd.github+json",
                f"/repos/{repository}/git/ref/heads/{quote(task_branch, safe='/')}",
                "--jq",
                "{ref:.ref,sha:.object.sha,type:.object.type}",
            )
        )
        if not isinstance(value, dict) or set(value) != {"ref", "sha", "type"}:
            raise CiEvidenceError("GitHub task ref response has invalid fields")
        expected_ref = f"refs/heads/{task_branch}"
        if value["ref"] != expected_ref or value["type"] != "commit":
            raise CiEvidenceError("GitHub task ref identity is invalid")
        try:
            return validate_git_sha(value["sha"], "remote task ref SHA")
        except (TypeError, ValueError) as exc:
            raise CiEvidenceError(str(exc)) from exc

    def _read_runs(
        self, repository: str, task_branch: str
    ) -> tuple[ActionsRunEvidence, ...]:
        pages = self._json_lines_command(
            (
                self._gh_path,
                "api",
                "--method",
                "GET",
                "--paginate",
                "-H",
                "Accept: application/vnd.github+json",
                (
                    f"/repos/{repository}/actions/runs?"
                    f"branch={quote(task_branch, safe='')}&per_page=100"
                ),
                "--jq",
                _RUN_PROJECTION,
            )
        )
        if not pages:
            raise CiEvidenceError("GitHub Actions response omitted its result page")
        total_count: int | None = None
        values: list[object] = []
        for page in pages:
            if not isinstance(page, dict) or set(page) != {"runs", "total_count"}:
                raise CiEvidenceError("GitHub Actions result page has invalid fields")
            page_total = page["total_count"]
            page_runs = page["runs"]
            if type(page_total) is not int or page_total < 0 or page_total > 1_000:
                raise CiEvidenceError("GitHub Actions total_count exceeds its safe boundary")
            if not isinstance(page_runs, list) or len(page_runs) > 100:
                raise CiEvidenceError("GitHub Actions result page has an invalid run array")
            if total_count is None:
                total_count = page_total
            elif page_total != total_count:
                raise CiEvidenceError("GitHub Actions pagination total_count changed")
            values.extend(page_runs)
        assert total_count is not None
        if len(values) != total_count:
            raise CiEvidenceError("GitHub Actions pagination did not return every branch run")
        parsed: list[ActionsRunEvidence] = []
        seen_ids: set[int] = set()
        for index, value in enumerate(values):
            try:
                run = _parse_run(value)
            except (TypeError, ValueError) as exc:
                raise CiEvidenceError(
                    f"GitHub Actions run {index} is invalid: {exc}"
                ) from exc
            if run.run_id in seen_ids:
                raise CiEvidenceError("GitHub Actions response contains duplicate run ids")
            seen_ids.add(run.run_id)
            parsed.append(run)
        return tuple(parsed)

    def _json_lines_command(self, argv: tuple[str, ...]) -> tuple[Any, ...]:
        result = self._command(argv, max_output_bytes=1024 * 1024)
        values: list[Any] = []
        for line in result.stdout.splitlines():
            if not line:
                raise CiEvidenceError("gh returned malformed Actions JSON Lines")
            try:
                values.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise CiEvidenceError("gh returned malformed Actions JSON Lines") from exc
        return tuple(values)

    def _json_command(
        self, argv: tuple[str, ...], *, max_output_bytes: int = 65_536
    ) -> Any:
        result = self._command(argv, max_output_bytes=max_output_bytes)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CiEvidenceError("gh returned malformed Actions JSON") from exc

    def _command(
        self, argv: tuple[str, ...], *, max_output_bytes: int = 65_536
    ) -> CommandResult:
        with TemporaryDirectory(prefix="codex-dispatcher-gh-actions-") as config_directory:
            command_env = {
                "GH_CONFIG_DIR": config_directory,
                "GH_PROMPT_DISABLED": "1",
            }
            secrets: tuple[str, ...] = ()
            if self._token is not None:
                command_env["GH_TOKEN"] = self._token
                secrets = (self._token,)
            result = run_command(
                argv,
                timeout_seconds=self._timeout_seconds,
                max_output_bytes=max_output_bytes,
                env=command_env,
                secrets=secrets,
            )
        if result.timed_out:
            raise CiEvidenceError("gh Actions command timed out")
        if result.error is not None:
            raise CiEvidenceError("gh Actions command failed to start")
        if result.stdout_truncated or result.stderr_truncated:
            raise CiEvidenceError("gh Actions command output was truncated")
        if result.returncode != 0:
            raise CiEvidenceError(
                f"gh Actions command failed with exit code {result.returncode}"
            )
        return result


def _parse_run(value: object) -> ActionsRunEvidence:
    fields = {
        "conclusion",
        "created_at",
        "event",
        "head_branch",
        "head_repository",
        "head_sha",
        "html_url",
        "id",
        "name",
        "repository",
        "run_attempt",
        "status",
        "updated_at",
        "workflow_id",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Actions run has unexpected or missing fields")
    return ActionsRunEvidence(
        name=value["name"],
        workflow_id=value["workflow_id"],
        run_id=value["id"],
        run_attempt=value["run_attempt"],
        repository=value["repository"],
        head_repository=value["head_repository"],
        head_branch=value["head_branch"],
        head_sha=value["head_sha"],
        event=value["event"],
        status=value["status"],
        conclusion=value["conclusion"],
        created_at=value["created_at"],
        updated_at=value["updated_at"],
        html_url=value["html_url"],
    )


def _validate_required_checks(value: tuple[str, ...]) -> None:
    if (
        not isinstance(value, tuple)
        or not value
        or len(value) > 100
        or len(set(value)) != len(value)
        or any(
            not isinstance(item, str)
            or not item
            or len(item.encode("utf-8")) > 256
            or any(ord(character) < 32 or ord(character) == 127 for character in item)
            for item in value
        )
    ):
        raise ValueError("required_checks must be a bounded unique non-empty tuple")
