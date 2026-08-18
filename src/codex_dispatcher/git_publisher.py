"""Fixed-parameter publication of one verified commit to its bound task branch."""

from __future__ import annotations

import base64
import re
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from codex_dispatcher.command_runner import CommandResult, run_command
from codex_dispatcher.publisher import MAX_BUNDLE_BYTES, PublicationPlan
from codex_dispatcher.work_items import WorkItem, validate_git_sha


_SHA_RE = re.compile(r"[0-9a-f]{40,64}")


class GitPublicationError(RuntimeError):
    """Base class for fixed Publisher failures."""


class GitPublicationRejected(GitPublicationError):
    """The plan or trusted local state is inconsistent and must be blocked."""


class GitPublicationInterrupted(GitPublicationError):
    """The remote outcome is not proven and must be reconciled by read-back."""


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    work_item_id: str
    target_ref: str
    observed_remote_sha: str
    reused: bool


class GitTaskBranchPublisher:
    """Import a verified bundle and update only one derived GitHub task ref."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        mirror_root: Path,
        temporary_root: Path,
        github_token: str | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        for path, field in (
            (mirror_root, "mirror_root"),
            (temporary_root, "temporary_root"),
        ):
            if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field} must be a normalized absolute path")
        if github_token is not None and (
            not isinstance(github_token, str) or not github_token or "\x00" in github_token
        ):
            raise ValueError("github_token must be a non-empty string without NUL or None")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._git_path = executable
        self._mirror_root = mirror_root
        self._temporary_root = temporary_root
        self._github_token = github_token
        self._timeout_seconds = timeout_seconds

    def publish(
        self,
        artifact: bytes,
        *,
        plan: PublicationPlan,
        work_item: WorkItem,
    ) -> PublicationReceipt:
        self._validate_plan(artifact, plan, work_item)
        mirror = self._mirror(work_item.repository)
        origin = self._origin(mirror, work_item.repository)
        self._prepare_temporary_root()
        with tempfile.TemporaryDirectory(
            prefix="publish-", dir=self._temporary_root
        ) as temp_dir:
            bundle_path = Path(temp_dir) / "verified.bundle"
            bundle_path.write_bytes(artifact)
            self._require_local(
                mirror,
                "bundle",
                "verify",
                str(bundle_path),
                stage="bundle_verify",
            )
            advertised = self._require_local(
                mirror,
                "bundle",
                "list-heads",
                str(bundle_path),
                stage="list_heads",
            ).stdout
            advertised_ids = {
                line.split(" ", 1)[0]
                for line in advertised.splitlines()
                if " " in line and _SHA_RE.fullmatch(line.split(" ", 1)[0])
            }
            if plan.source_sha not in advertised_ids:
                raise GitPublicationRejected("verified bundle does not advertise the planned SHA")
            local_ref = f"refs/codex-publisher/{work_item.work_item_id}"
            self._require_local(
                mirror,
                "fetch",
                "--no-tags",
                "--no-write-fetch-head",
                str(bundle_path),
                f"{plan.source_sha}:{local_ref}",
                stage="bundle_import",
            )
            imported = self._require_local(
                mirror,
                "rev-parse",
                "--verify",
                f"{local_ref}^{{commit}}",
                stage="import_verify",
            ).stdout.strip()
            if imported != plan.source_sha:
                raise GitPublicationRejected("imported commit differs from the publication plan")

        observed = self._read_remote_head(mirror, origin, plan.target_ref)
        if observed == plan.source_sha:
            return PublicationReceipt(
                work_item.work_item_id,
                plan.target_ref,
                plan.source_sha,
                True,
            )
        if observed != plan.expected_remote_sha:
            raise GitPublicationRejected("remote task ref differs from its recorded lease")
        expected = plan.expected_remote_sha or ""
        result = self._execute(
            mirror,
            "push",
            "--porcelain",
            f"--force-with-lease={plan.target_ref}:{expected}",
            origin,
            f"{plan.source_sha}:{plan.target_ref}",
        )
        if not self._successful(result):
            raise GitPublicationInterrupted("task branch push outcome is ambiguous")
        read_back = self._read_remote_head(mirror, origin, plan.target_ref)
        if read_back != plan.source_sha:
            raise GitPublicationInterrupted("task branch read-back did not prove the planned SHA")
        return PublicationReceipt(
            work_item.work_item_id,
            plan.target_ref,
            read_back,
            False,
        )

    def _validate_plan(
        self, artifact: bytes, plan: PublicationPlan, work_item: WorkItem
    ) -> None:
        if not isinstance(artifact, bytes) or not 1 <= len(artifact) <= MAX_BUNDLE_BYTES:
            raise GitPublicationRejected("verified bundle artifact size is invalid")
        if not isinstance(plan, PublicationPlan) or not isinstance(work_item, WorkItem):
            raise TypeError("plan and work_item have invalid types")
        expected_ref = f"refs/heads/{work_item.task_branch}"
        if (
            plan.work_item_id != work_item.work_item_id
            or plan.repository != work_item.repository
            or plan.target_ref != expected_ref
            or plan.expected_remote_sha != work_item.last_published_sha
            or plan.force
            or plan.delete
            or sha256(artifact).hexdigest() != plan.bundle_sha256
        ):
            raise GitPublicationRejected("publication plan conflicts with trusted WorkItem state")
        validate_git_sha(plan.source_sha, "source_sha")
        if plan.target_ref in {
            f"refs/heads/{work_item.base_branch}",
            "refs/heads/main",
            "refs/heads/master",
        }:
            raise GitPublicationRejected("Publisher cannot target a protected branch")

    def _mirror(self, repository: str) -> Path:
        owner, name = repository.split("/", 1)
        mirror = self._mirror_root / owner / f"{name}.git"
        try:
            resolved_root = self._mirror_root.resolve(strict=True)
            resolved_mirror = mirror.resolve(strict=True)
            resolved_mirror.relative_to(resolved_root)
        except (OSError, ValueError) as exc:
            raise GitPublicationRejected("trusted Publisher mirror is unavailable") from exc
        if mirror.is_symlink() or not mirror.is_dir():
            raise GitPublicationRejected("trusted Publisher mirror must be a directory")
        bare = self._require_local(
            mirror,
            "rev-parse",
            "--is-bare-repository",
            stage="mirror_type",
        ).stdout.strip()
        if bare != "true":
            raise GitPublicationRejected("trusted Publisher mirror is not bare")
        return mirror

    def _origin(self, mirror: Path, repository: str) -> str:
        fetch_url = self._require_local(
            mirror,
            "remote",
            "get-url",
            "origin",
            stage="origin_fetch_url",
        ).stdout.strip()
        push_url = self._require_local(
            mirror,
            "remote",
            "get-url",
            "--push",
            "origin",
            stage="origin_push_url",
        ).stdout.strip()
        fetch_url = _validate_github_origin(fetch_url, repository)
        push_url = _validate_github_origin(push_url, repository)
        if fetch_url != push_url:
            raise GitPublicationRejected("origin fetch and push URLs differ")
        return push_url

    def _read_remote_head(self, mirror: Path, origin: str, target_ref: str) -> str | None:
        result = self._execute(
            mirror,
            "ls-remote",
            "--heads",
            origin,
            target_ref,
            max_output_bytes=4096,
        )
        if not self._successful(result):
            raise GitPublicationInterrupted("remote task ref lookup failed")
        lines = tuple(line for line in result.stdout.splitlines() if line)
        if not lines:
            return None
        if len(lines) != 1:
            raise GitPublicationRejected("remote task ref lookup is ambiguous")
        fields = lines[0].split("\t")
        if (
            len(fields) != 2
            or fields[1] != target_ref
            or _SHA_RE.fullmatch(fields[0]) is None
        ):
            raise GitPublicationRejected("remote task ref lookup returned invalid data")
        return fields[0]

    def _prepare_temporary_root(self) -> None:
        self._temporary_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self._temporary_root.is_symlink() or not self._temporary_root.is_dir():
            raise GitPublicationRejected("Publisher temporary_root must be a directory")

    def _require_local(
        self,
        repository: Path,
        *arguments: str,
        stage: str,
    ) -> CommandResult:
        result = self._execute(repository, *arguments)
        if not self._successful(result):
            raise GitPublicationRejected(f"local Publisher Git stage failed: {stage}")
        return result

    @staticmethod
    def _successful(result: CommandResult) -> bool:
        return (
            result.returncode == 0
            and not result.timed_out
            and result.error is None
            and not result.stdout_truncated
            and not result.stderr_truncated
        )

    def _execute(
        self,
        repository: Path,
        *arguments: str,
        max_output_bytes: int = 1024 * 1024,
    ) -> CommandResult:
        prefix = (
            self._git_path,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "protocol.http.allow=never",
            "-c",
            "protocol.ssh.allow=never",
            "-c",
            "submodule.recurse=false",
            "-c",
            "credential.helper=",
            "-c",
            "http.proxy=",
            "-c",
            "remote.origin.proxy=",
        )
        environment = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_NO_REPLACE_OBJECTS": "1",
        }
        secrets: tuple[str, ...] = ()
        if self._github_token is not None:
            basic = f"x-access-token:{self._github_token}"
            authorization = base64.b64encode(basic.encode("utf-8")).decode("ascii")
            environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
                }
            )
            secrets = (self._github_token, authorization)
        return run_command(
            prefix + ("-C", str(repository)) + arguments,
            timeout_seconds=self._timeout_seconds,
            max_output_bytes=max_output_bytes,
            env=environment,
            secrets=secrets,
        )


def _validate_github_origin(origin: str, repository: str) -> str:
    expected = f"https://github.com/{repository}.git"
    if origin != expected:
        raise GitPublicationRejected("Publisher origin is not the configured GitHub repository")
    return origin
