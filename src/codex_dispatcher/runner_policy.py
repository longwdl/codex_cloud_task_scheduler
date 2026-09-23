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
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class PolicyBundleError(ValueError):
    """Raised when a Runner policy bundle is not a trusted exact artifact."""


_MAX_POLICY_FILE_BYTES = 128 * 1024
_MANIFEST_FILE = "manifest.json"
_V1_REQUIRED_FILES = frozenset(
    {
        "config.toml",
        "requirements.toml",
        "agents/spark-worker.toml",
        "agents/luna-worker.toml",
        "agents/terra-worker.toml",
        "agents/sol-specialist.toml",
    }
)
_V2_REQUIRED_FILES = frozenset(
    {
        "config.toml",
        "repair.config.toml",
        "audit.config.toml",
        "requirements.toml",
        "agents/luna-worker.toml",
        "agents/sol-specialist.toml",
    }
)
_GENERATION_PROFILES = {
    "implementation": None,
    "ci_repair": "repair",
    "audit": "audit",
}
_V2_AGENT_CONTRACT = {
    "luna_worker": ("gpt-6-luna", "medium"),
    "sol_specialist": ("gpt-6-sol", "high"),
}
_SHA256_LENGTH = 64
_REASONING_EFFORTS = frozenset(
    {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)


@dataclass(frozen=True, slots=True)
class AgentPolicyProfile:
    name: str
    model: str
    reasoning_effort: str


@dataclass(frozen=True, slots=True)
class AgentRuntimePolicy:
    codex_version: str
    primary_model: str
    primary_reasoning_effort: str
    profiles: tuple[AgentPolicyProfile, ...]
    config_profile: str | None = None
    delegation_allowed: bool = True
    bundle_schema_version: int = 1

    @property
    def profiles_by_name(self) -> dict[str, AgentPolicyProfile]:
        return {profile.name: profile for profile in self.profiles}


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

    def profile_path(self, profile: str) -> Path:
        if profile not in {"repair", "audit"}:
            raise PolicyBundleError("generation profile is invalid")
        return self.root / f"{profile}.config.toml"

    @property
    def agents_path(self) -> Path:
        return self.root / "agents"

    def validate(self) -> None:
        """Revalidate ownership, modes, exact hashes, and the manifest digest."""
        _trusted_directory(self.root, "policy_root")
        _trusted_parents(self.root, "policy_root")
        agents = self.agents_path
        _trusted_directory(agents, "policy agents directory")
        manifest_path = self.root / _MANIFEST_FILE
        manifest = _read_json_file(manifest_path, "policy manifest")
        if set(manifest) != {
            "schema_version",
            "codex_version",
            "policy_digest",
            "files",
        }:
            raise PolicyBundleError("policy manifest fields are invalid")
        schema_version = manifest["schema_version"]
        if (
            type(schema_version) is not int
            or schema_version not in {1, 2}
            or manifest["codex_version"] != "0.147.0"
        ):
            raise PolicyBundleError("policy manifest version is unsupported")
        required_files = _V1_REQUIRED_FILES if schema_version == 1 else _V2_REQUIRED_FILES
        try:
            root_entries = {entry.name for entry in self.root.iterdir()}
            agent_entries = {entry.name for entry in agents.iterdir()}
        except OSError as exc:
            raise PolicyBundleError("policy bundle is unavailable") from exc
        expected_root = {name for name in required_files if "/" not in name} | {
            "agents", _MANIFEST_FILE
        }
        expected_agents = {
            name.removeprefix("agents/")
            for name in required_files
            if name.startswith("agents/")
        }
        if root_entries != expected_root:
            raise PolicyBundleError("policy bundle contains unknown files")
        if agent_entries != expected_agents:
            raise PolicyBundleError("policy agents contain unknown files")
        manifest_digest = _digest(manifest["policy_digest"])
        if manifest_digest != self.policy_digest:
            raise PolicyBundleError("policy digest does not match manifest")
        file_hashes = manifest["files"]
        if not isinstance(file_hashes, dict) or set(file_hashes) != required_files:
            raise PolicyBundleError("policy manifest file set is invalid")

        observed: dict[str, str] = {}
        for relative_path in sorted(required_files):
            expected_hash = _digest(file_hashes[relative_path])
            target = self.root / relative_path
            _trusted_regular_file(target, f"policy file {relative_path}")
            observed[relative_path] = hashlib.sha256(target.read_bytes()).hexdigest()
            if observed[relative_path] != expected_hash:
                raise PolicyBundleError("policy file digest does not match manifest")
        calculated_policy_digest = hashlib.sha256(_canonical_json(observed)).hexdigest()
        if calculated_policy_digest != manifest_digest:
            raise PolicyBundleError("policy manifest digest is invalid")

    def runtime_policy(self, generation_role: str) -> AgentRuntimePolicy:
        """Return the effective root and child execution contract for one role."""
        self.validate()
        if not isinstance(generation_role, str) or generation_role not in _GENERATION_PROFILES:
            raise PolicyBundleError("generation role is unsupported")
        manifest = _read_json_file(self.root / _MANIFEST_FILE, "policy manifest")
        schema_version = manifest["schema_version"]
        config = _read_toml_file(self.config_path, "policy config")
        profile = _GENERATION_PROFILES[generation_role] if schema_version == 2 else None
        if profile is not None:
            config = {**config, **_read_toml_file(self.profile_path(profile), "generation profile")}
        primary_model = _bounded_policy_text(config.get("model"), "primary model")
        primary_effort = _reasoning_effort(
            config.get("model_reasoning_effort"), "primary reasoning effort"
        )
        profiles: list[AgentPolicyProfile] = []
        required_files = _V1_REQUIRED_FILES if schema_version == 1 else _V2_REQUIRED_FILES
        for relative_path in sorted(required_files):
            if not relative_path.startswith("agents/"):
                continue
            table = _read_toml_file(self.root / relative_path, "agent profile")
            profiles.append(
                AgentPolicyProfile(
                    name=_bounded_policy_text(table.get("name"), "agent name"),
                    model=_bounded_policy_text(table.get("model"), "agent model"),
                    reasoning_effort=_reasoning_effort(
                        table.get("model_reasoning_effort"),
                        "agent reasoning effort",
                    ),
                )
            )
        names = {profile.name for profile in profiles}
        if len(names) != len(profiles):
            raise PolicyBundleError("policy agent names must be unique")
        if schema_version == 2:
            expected_effort = "medium" if generation_role == "implementation" else "high"
            if (primary_model, primary_effort) != ("gpt-6-sol", expected_effort):
                raise PolicyBundleError("generation root model or effort is unsupported")
            actual_agents = {
                item.name: (item.model, item.reasoning_effort) for item in profiles
            }
            if actual_agents != _V2_AGENT_CONTRACT:
                raise PolicyBundleError("generation agent profiles are unsupported")
        delegation_allowed = schema_version == 1 or generation_role != "audit"
        if not delegation_allowed:
            agents_config = config.get("agents")
            if not isinstance(agents_config, dict) or agents_config.get("enabled") is not False:
                raise PolicyBundleError("audit profile must disable delegation")
        return AgentRuntimePolicy(
            codex_version=manifest["codex_version"],
            primary_model=primary_model,
            primary_reasoning_effort=primary_effort,
            profiles=tuple(sorted(profiles, key=lambda profile: profile.name)),
            config_profile=profile,
            delegation_allowed=delegation_allowed,
            bundle_schema_version=schema_version,
        )


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


def _read_toml_file(path: Path, field: str) -> dict[str, Any]:
    _trusted_regular_file(path, field)
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PolicyBundleError(f"{field} is malformed") from exc
    if not isinstance(payload, dict):
        raise PolicyBundleError(f"{field} is malformed")
    return payload


def _bounded_policy_text(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise PolicyBundleError(f"{field} is invalid")
    return value


def _reasoning_effort(value: Any, field: str) -> str:
    if value not in _REASONING_EFFORTS:
        raise PolicyBundleError(f"{field} is invalid")
    return value


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
