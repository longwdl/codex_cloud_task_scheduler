"""Safe local Git mirror and worktree preparation primitives."""

from __future__ import annotations

import base64
import re
import shlex
import stat
from dataclasses import dataclass
from pathlib import Path

from codex_dispatcher.command_runner import CommandResult, run_command


class GitWorkspaceError(RuntimeError):
    """Raised when a repository cannot be prepared without executing its configuration."""


@dataclass(frozen=True, slots=True)
class PreparedWorktree:
    repository: str
    base_branch: str
    base_sha: str
    task_branch: str
    mirror_path: Path
    worktree_path: Path


@dataclass(frozen=True, slots=True)
class PublishedBranch:
    branch_name: str
    head_sha: str
    reused: bool


_REPOSITORY_RE = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
)
_BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_ALLOWED_REMOTE_SCHEMES = ("https://github.com/", "git@github.com:")


class GitWorkspace:
    """Prepare isolated local worktrees while disabling repository hooks and filters."""

    def __init__(
        self,
        *,
        git_path: str | Path,
        workspace_root: Path,
        github_token: str | None = None,
        ssh_auth_sock: Path | None = None,
        ssh_config_path: Path | None = None,
        timeout_seconds: float = 120.0,
    ) -> None:
        executable = str(git_path)
        if not executable or not Path(executable).is_absolute():
            raise ValueError("git_path must be a non-empty absolute path")
        if not workspace_root.is_absolute():
            raise ValueError("workspace_root must be absolute")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if github_token is not None and (
            not isinstance(github_token, str) or not github_token or "\x00" in github_token
        ):
            raise ValueError("github_token must be a non-empty string without NUL or None")
        if ssh_auth_sock is not None:
            if not ssh_auth_sock.is_absolute():
                raise ValueError("ssh_auth_sock must be an absolute path or None")
            try:
                mode = ssh_auth_sock.stat().st_mode
            except OSError as exc:
                raise ValueError("ssh_auth_sock must reference an existing Unix socket") from exc
            if not stat.S_ISSOCK(mode):
                raise ValueError("ssh_auth_sock must reference an existing Unix socket")
        if ssh_config_path is not None:
            if not ssh_config_path.is_absolute():
                raise ValueError("ssh_config_path must be an absolute path or None")
            try:
                config_stat = ssh_config_path.stat()
            except OSError as exc:
                raise ValueError("ssh_config_path must reference a protected regular file") from exc
            if not stat.S_ISREG(config_stat.st_mode) or config_stat.st_mode & 0o022:
                raise ValueError("ssh_config_path must reference a protected regular file")
        self._git_path = executable
        self._root = workspace_root
        self._github_token = github_token
        self._ssh_auth_sock = ssh_auth_sock
        self._ssh_config_path = ssh_config_path
        self._timeout_seconds = timeout_seconds

    def prepare(
        self,
        *,
        repository: str,
        remote_url: str,
        base_branch: str,
        task_branch: str,
        expected_base_sha: str | None = None,
    ) -> PreparedWorktree:
        """Prepare a clean worktree, optionally recovering one persisted base anchor."""
        repository = _validate_repository(repository)
        remote_url = _validate_remote(remote_url, repository)
        base_branch = _validate_branch(base_branch, "base_branch")
        task_branch = _validate_branch(task_branch, "task_branch")
        if expected_base_sha is not None and (
            not isinstance(expected_base_sha, str)
            or _SHA_RE.fullmatch(expected_base_sha) is None
        ):
            raise ValueError("expected_base_sha must be a lowercase Git object ID or None")
        if base_branch == task_branch:
            raise ValueError("task_branch must differ from base_branch")

        owner, name = repository.split("/", 1)
        mirror = self._root / "mirrors" / owner / f"{name}.git"
        worktree = self._root / "worktrees" / owner / name / task_branch.replace("/", "__")
        self._assert_under_root(mirror)
        self._assert_under_root(worktree)
        if worktree.exists() and (
            not worktree.is_dir() or not (worktree / ".git").is_file()
        ):
            raise GitWorkspaceError("task worktree path is not a linked Git worktree")
        mirror.parent.mkdir(parents=True, exist_ok=True)
        worktree.parent.mkdir(parents=True, exist_ok=True)

        if mirror.exists():
            if not mirror.is_dir():
                raise GitWorkspaceError("mirror path is not a directory")
            self._run(mirror, "remote", "set-url", "origin", remote_url)
        else:
            self._run(
                None,
                "clone",
                "--mirror",
                "--no-recurse-submodules",
                remote_url,
                str(mirror),
            )
        self._run(
            mirror,
            "fetch",
            "--prune",
            "--no-tags",
            "--recurse-submodules=no",
            "origin",
            f"+refs/heads/{base_branch}:refs/remotes/origin/{base_branch}",
        )
        current_base_sha = self._run(
            mirror, "rev-parse", "--verify", f"refs/remotes/origin/{base_branch}^{{commit}}"
        ).stdout.strip()
        if _SHA_RE.fullmatch(current_base_sha) is None:
            raise GitWorkspaceError("git returned an invalid base SHA")
        if expected_base_sha is not None:
            self._run(mirror, "cat-file", "-e", f"{expected_base_sha}^{{commit}}")
            merge_base = self._run(
                mirror, "merge-base", expected_base_sha, current_base_sha
            ).stdout.strip()
            if merge_base != expected_base_sha:
                raise GitWorkspaceError("persisted base SHA is not in the configured base branch")
        base_sha = expected_base_sha or current_base_sha
        remote_task_sha = self._remote_branch_sha(mirror, task_branch)
        if remote_task_sha is not None:
            if remote_task_sha != base_sha:
                raise GitWorkspaceError("remote task branch differs from the persisted base SHA")
        self._reject_unsafe_tree(mirror, base_sha)
        if worktree.exists():
            self._validate_worktree(worktree, mirror, task_branch, base_sha)
        else:
            self._run(
                mirror,
                "worktree",
                "add",
                "--detach",
                "--no-checkout",
                str(worktree),
                base_sha,
            )
            self._run(worktree, "checkout", "-B", task_branch, base_sha, "--")
            self._validate_worktree(worktree, mirror, task_branch, base_sha)
        return PreparedWorktree(
            repository,
            base_branch,
            base_sha,
            task_branch,
            mirror,
            worktree,
        )

    def publish_task_branch(self, prepared: PreparedWorktree) -> PublishedBranch:
        """Create the remote task branch without overwriting an existing different ref."""
        if not isinstance(prepared, PreparedWorktree):
            raise TypeError("prepared must be a PreparedWorktree")
        _validate_repository(prepared.repository)
        _validate_branch(prepared.task_branch, "task_branch")
        if (
            not isinstance(prepared.base_sha, str)
            or _SHA_RE.fullmatch(prepared.base_sha) is None
        ):
            raise ValueError("prepared base SHA is invalid")
        self._assert_under_root(prepared.mirror_path)
        self._assert_under_root(prepared.worktree_path)
        self._validate_worktree(
            prepared.worktree_path,
            prepared.mirror_path,
            prepared.task_branch,
            prepared.base_sha,
        )
        existing = self._remote_branch_sha(prepared.worktree_path, prepared.task_branch)
        if existing is not None:
            if existing != prepared.base_sha:
                raise GitWorkspaceError("remote task branch already points to a different commit")
            return PublishedBranch(prepared.task_branch, existing, True)

        reference = f"refs/heads/{prepared.task_branch}"
        self._run(
            prepared.worktree_path,
            "-c",
            "remote.origin.mirror=false",
            "push",
            "--porcelain",
            f"--force-with-lease={reference}:",
            "origin",
            f"{prepared.base_sha}:{reference}",
        )
        published = self._remote_branch_sha(prepared.worktree_path, prepared.task_branch)
        if published != prepared.base_sha:
            raise GitWorkspaceError("remote task branch verification failed")
        return PublishedBranch(prepared.task_branch, published, False)

    def _remote_branch_sha(self, repository: Path, branch: str) -> str | None:
        reference = f"refs/heads/{branch}"
        output = self._run(repository, "ls-remote", "--heads", "origin", reference).stdout
        lines = tuple(line for line in output.splitlines() if line)
        if not lines:
            return None
        if len(lines) != 1:
            raise GitWorkspaceError("remote task branch lookup was ambiguous")
        fields = lines[0].split("\t")
        if len(fields) != 2 or fields[1] != reference or _SHA_RE.fullmatch(fields[0]) is None:
            raise GitWorkspaceError("remote task branch lookup returned invalid data")
        return fields[0]

    def _validate_worktree(
        self, worktree: Path, mirror: Path, task_branch: str, base_sha: str
    ) -> None:
        head = self._inspect_worktree(worktree, mirror, task_branch)
        if head != base_sha:
            raise GitWorkspaceError("task worktree HEAD does not match its anchor")

    def _inspect_worktree(self, worktree: Path, mirror: Path, task_branch: str) -> str:
        common_dir = self._run(worktree, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if Path(common_dir.stdout.strip()).resolve(strict=False) != mirror.resolve(strict=False):
            raise GitWorkspaceError("task worktree belongs to an unexpected Git repository")
        branch = self._run(worktree, "symbolic-ref", "--short", "HEAD").stdout.strip()
        head = self._run(worktree, "rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
        if branch != task_branch or _SHA_RE.fullmatch(head) is None:
            raise GitWorkspaceError("task worktree branch or HEAD is invalid")
        if self._run(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout:
            raise GitWorkspaceError("task worktree is not clean")
        return head

    def _reject_unsafe_tree(self, mirror: Path, base_sha: str) -> None:
        paths = self._run(mirror, "ls-tree", "-r", "--name-only", "-z", base_sha).stdout
        names = tuple(path for path in paths.split("\x00") if path)
        if ".gitmodules" in names:
            raise GitWorkspaceError("repositories with submodules are not supported")
        if ".gitattributes" not in names:
            return
        attributes = self._run(mirror, "show", f"{base_sha}:.gitattributes").stdout
        for line in attributes.splitlines():
            rule = line.split("#", 1)[0].strip()
            if not rule:
                continue
            tokens = rule.split()[1:]
            if any(
                token.startswith(("filter=", "diff=")) or token == "working-tree-encoding"
                for token in tokens
            ):
                raise GitWorkspaceError("repository requires an unsupported Git attribute driver")

    def _run(self, repository: Path | None, *arguments: str) -> CommandResult:
        command_env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        if self._ssh_auth_sock is not None:
            command_env["SSH_AUTH_SOCK"] = str(self._ssh_auth_sock)
        secrets: tuple[str, ...] = ()
        if self._github_token is not None:
            basic = f"x-access-token:{self._github_token}"
            authorization = base64.b64encode(basic.encode("utf-8")).decode("ascii")
            command_env.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                    "GIT_CONFIG_VALUE_0": f"Authorization: Basic {authorization}",
                }
            )
            secrets = (self._github_token, authorization)
        prefix = (
            self._git_path,
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.allow=never",
            "-c",
            "protocol.file.allow=always",
            "-c",
            "protocol.http.allow=always",
            "-c",
            "protocol.https.allow=always",
            "-c",
            "protocol.ssh.allow=always",
            "-c",
            "submodule.recurse=false",
            "-c",
            "filter.lfs.required=false",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.clean=",
        )
        if self._ssh_config_path is not None:
            ssh_command = (
                f"/usr/bin/ssh -F {shlex.quote(str(self._ssh_config_path))} -oBatchMode=yes"
            )
            prefix += ("-c", f"core.sshCommand={ssh_command}")
        argv = prefix + (("-C", str(repository)) if repository is not None else ()) + arguments
        result = run_command(
            argv,
            timeout_seconds=self._timeout_seconds,
            env=command_env,
            secrets=secrets,
        )
        if (
            result.returncode != 0
            or result.timed_out
            or result.error is not None
            or result.stdout_truncated
            or result.stderr_truncated
        ):
            raise GitWorkspaceError(_command_failure(result))
        return result

    def _assert_under_root(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self._root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError("workspace path escapes workspace_root") from exc


def _validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or _REPOSITORY_RE.fullmatch(repository) is None:
        raise ValueError("repository must be in owner/repository form")
    return repository


def _validate_remote(remote_url: str, repository: str) -> str:
    if not isinstance(remote_url, str) or "\x00" in remote_url:
        raise ValueError("remote_url is invalid")
    expected = (
        f"https://github.com/{repository}.git",
        f"git@github.com:{repository}.git",
    )
    if remote_url not in expected or not remote_url.startswith(_ALLOWED_REMOTE_SCHEMES):
        raise ValueError("remote_url must be the configured GitHub repository")
    return remote_url


def _validate_branch(branch: str, field: str) -> str:
    if (
        not isinstance(branch, str)
        or _BRANCH_RE.fullmatch(branch) is None
        or branch.startswith(("/", "."))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
    ):
        raise ValueError(f"{field} is not a safe Git branch name")
    return branch


def _command_failure(result: CommandResult) -> str:
    if result.timed_out:
        return "git command timed out"
    if result.error is not None:
        return "git command failed to start"
    if result.stdout_truncated or result.stderr_truncated:
        return "git command output was truncated"
    return f"git command failed with exit code {result.returncode}"
