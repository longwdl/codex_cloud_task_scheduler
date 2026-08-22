"""Validation for the Runner-owned, digest-pinned Codex policy bundle.

The bundle is deliberately a host input rather than a project input.  Its
contents are mounted into a rootless container one file at a time; this module
therefore validates both the immutable file set and every filesystem component
that can replace it.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PolicyBundleError(ValueError):
    """Raised when a Runner policy bundle is not a trusted exact artifact."""


_MAX_POLICY_FILE_BYTES = 128 * 1024
_MANIFEST_FILE = "manifest.json"
_REQUIRED_FILES = frozenset(
    {
        "config.toml",
        "requirements.toml",
        "agents/spark-worker.toml",
        "agents/luna-worker.toml",
        "agents/terra-worker.toml",
        "agents/sol-specialist.toml",
    }
)
_SHA256_LENGTH = 64


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """A validated immutable Runner policy bundle.

    ``validate`` intentionally re-reads all files.  Callers use it immediately
    before starting Codex so a post-startup replacement fails closed.
    """

    root: Path
    policy_digest: str

    @classmethod
    def load(cls, root: Path, expected_digest: str) -> "PolicyBundle":
        bundle = cls(_normalized_absolute(root, "policy_root"), _digest(expected_digest))
        bundle.validate()
        return bundle

    @property
    def config_path(self) -> Path:
        return self.root / "config.toml"

    @property
    def requirements_path(self) -> Path:
        return self.root / "requirements.toml"

    @property
    def agents_path(self) -> Path:
        return self.root / "agents"

    def validate(self) -> None:
        """Revalidate ownership, modes, exact hashes, and the manifest digest."""
        _trusted_directory(self.root, "policy_root")
        _trusted_parents(self.root, "policy_root")
        agents = self.agents_path
        _trusted_directory(agents, "policy agents directory")
        try:
            root_entries = {entry.name for entry in self.root.iterdir()}
            agent_entries = {entry.name for entry in agents.iterdir()}
        except OSError as exc:
            raise PolicyBundleError("policy bundle is unavailable") from exc
        if root_entries != {"config.toml", "requirements.toml", "agents", _MANIFEST_FILE}:
            raise PolicyBundleError("policy bundle contains unknown files")
        if agent_entries != {
            "spark-worker.toml",
            "luna-worker.toml",
            "terra-worker.toml",
            "sol-specialist.toml",
        }:
            raise PolicyBundleError("policy agents contain unknown files")
        manifest_path = self.root / _MANIFEST_FILE
        manifest = _read_json_file(manifest_path, "policy manifest")
        if set(manifest) != {
            "schema_version",
            "codex_version",
            "policy_digest",
            "files",
        }:
            raise PolicyBundleError("policy manifest fields are invalid")
        if manifest["schema_version"] != 1 or manifest["codex_version"] != "0.147.0":
            raise PolicyBundleError("policy manifest version is unsupported")
        manifest_digest = _digest(manifest["policy_digest"])
        if manifest_digest != self.policy_digest:
            raise PolicyBundleError("policy digest does not match manifest")
        file_hashes = manifest["files"]
        if not isinstance(file_hashes, dict) or set(file_hashes) != _REQUIRED_FILES:
            raise PolicyBundleError("policy manifest file set is invalid")

        observed: dict[str, str] = {}
        for relative_path in sorted(_REQUIRED_FILES):
            expected_hash = _digest(file_hashes[relative_path])
            target = self.root / relative_path
            _trusted_regular_file(target, f"policy file {relative_path}")
            observed[relative_path] = hashlib.sha256(target.read_bytes()).hexdigest()
            if observed[relative_path] != expected_hash:
                raise PolicyBundleError("policy file digest does not match manifest")
        calculated_policy_digest = hashlib.sha256(_canonical_json(observed)).hexdigest()
        if calculated_policy_digest != manifest_digest:
            raise PolicyBundleError("policy manifest digest is invalid")


def _normalized_absolute(value: Path, field: str) -> Path:
    if (
        not isinstance(value, Path)
        or not value.is_absolute()
        or ".." in value.parts
        or "\x00" in str(value)
    ):
        raise PolicyBundleError(f"{field} must be a normalized absolute path")
    try:
        root_stat = value.lstat()
    except OSError as exc:
        raise PolicyBundleError(f"{field} is unavailable") from exc
    if value.is_symlink() or not stat.S_ISDIR(root_stat.st_mode):
        raise PolicyBundleError(f"{field} must not be a symlink")
    return value


def _digest(value: Any) -> str:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        raise PolicyBundleError("policy digest is invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise PolicyBundleError("policy digest is invalid") from exc
    if value != value.lower():
        raise PolicyBundleError("policy digest is invalid")
    return value


def _trusted_directory(path: Path, field: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise PolicyBundleError(f"{field} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISDIR(path_stat.st_mode)
        or path_stat.st_uid not in {0, os.geteuid()}
        or path_stat.st_mode & 0o022
    ):
        raise PolicyBundleError(f"{field} must be a trusted protected directory")


def _trusted_regular_file(path: Path, field: str) -> None:
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise PolicyBundleError(f"{field} is unavailable") from exc
    if (
        path.is_symlink()
        or not stat.S_ISREG(path_stat.st_mode)
        or path_stat.st_uid not in {0, os.geteuid()}
        or path_stat.st_mode & 0o022
        or not 0 < path_stat.st_size <= _MAX_POLICY_FILE_BYTES
    ):
        raise PolicyBundleError(f"{field} must be a trusted protected regular file")
    _trusted_parents(path, field)


def _trusted_parents(path: Path, field: str) -> None:
    for parent in path.parents:
        try:
            parent_stat = parent.lstat()
        except OSError as exc:
            raise PolicyBundleError(f"{field} parent is unavailable") from exc
        root_sticky_directory = (
            parent_stat.st_uid == 0 and bool(parent_stat.st_mode & stat.S_ISVTX)
        )
        if (
            parent.is_symlink()
            or not stat.S_ISDIR(parent_stat.st_mode)
            or parent_stat.st_uid not in {0, os.geteuid()}
            or (parent_stat.st_mode & 0o022 and not root_sticky_directory)
        ):
            raise PolicyBundleError(
                f"{field} parent directories must be trusted and protected"
            )


def _read_json_file(path: Path, field: str) -> dict[str, Any]:
    _trusted_regular_file(path, field)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise PolicyBundleError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise PolicyBundleError(f"{field} is malformed")
    return payload


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result
