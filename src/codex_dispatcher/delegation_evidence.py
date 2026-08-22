"""Trusted, metadata-only evidence for Codex local subagent delegation."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from codex_dispatcher.runner_policy import AgentRuntimePolicy
from codex_dispatcher.work_items import validate_session_id


DELEGATION_OBSERVER: Final = "codex_state_db_delta_v1"
DELEGATION_SCHEMA_VERSION: Final = 1
MAX_DELEGATED_AGENTS: Final = 32
_CODEX_STATE_DATABASE = "state_5.sqlite"
_MAX_STATE_DATABASE_BYTES = 512 * 1024 * 1024
_AGENT_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_EFFORTS = frozenset({"minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_EDGE_STATES = frozenset({"open", "closed"})


class DelegationEvidenceError(ValueError):
    """Raised when Codex delegation metadata is absent, malformed, or ambiguous."""


@dataclass(frozen=True, slots=True, order=True)
class DelegatedAgent:
    parent_thread_id: str
    child_thread_id: str
    agent_name: str
    model: str
    reasoning_effort: str
    edge_status: str
    tokens_used: int

    def __post_init__(self) -> None:
        try:
            validate_session_id(self.parent_thread_id)
            validate_session_id(self.child_thread_id)
        except ValueError as exc:
            raise DelegationEvidenceError(str(exc)) from exc
        if self.parent_thread_id == self.child_thread_id:
            raise DelegationEvidenceError("delegation parent and child must differ")
        if not isinstance(self.agent_name, str) or not _AGENT_NAME_RE.fullmatch(
            self.agent_name
        ):
            raise DelegationEvidenceError("delegated agent name is invalid")
        if not isinstance(self.model, str) or not _MODEL_RE.fullmatch(self.model):
            raise DelegationEvidenceError("delegated agent model is invalid")
        if self.reasoning_effort not in _EFFORTS:
            raise DelegationEvidenceError("delegated agent reasoning effort is invalid")
        if self.edge_status not in _EDGE_STATES:
            raise DelegationEvidenceError("delegation edge status is invalid")
        if (
            isinstance(self.tokens_used, bool)
            or not isinstance(self.tokens_used, int)
            or self.tokens_used < 0
        ):
            raise DelegationEvidenceError("delegated agent token usage is invalid")


@dataclass(frozen=True, slots=True)
class DelegationReceipt:
    codex_version: str
    root_thread_id: str
    root_model: str
    root_reasoning_effort: str
    agents: tuple[DelegatedAgent, ...]
    observer: str = DELEGATION_OBSERVER
    schema_version: int = DELEGATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DELEGATION_SCHEMA_VERSION:
            raise DelegationEvidenceError("delegation receipt schema is unsupported")
        if self.observer != DELEGATION_OBSERVER:
            raise DelegationEvidenceError("delegation receipt observer is unsupported")
        if self.codex_version != "0.147.0":
            raise DelegationEvidenceError("delegation receipt Codex version is unsupported")
        try:
            validate_session_id(self.root_thread_id)
        except ValueError as exc:
            raise DelegationEvidenceError(str(exc)) from exc
        if not isinstance(self.root_model, str) or not _MODEL_RE.fullmatch(
            self.root_model
        ):
            raise DelegationEvidenceError("delegation root model is invalid")
        if self.root_reasoning_effort not in _EFFORTS:
            raise DelegationEvidenceError("delegation root reasoning effort is invalid")
        if (
            not isinstance(self.agents, tuple)
            or len(self.agents) > MAX_DELEGATED_AGENTS
            or any(not isinstance(agent, DelegatedAgent) for agent in self.agents)
        ):
            raise DelegationEvidenceError("delegated agents must be a bounded tuple")
        identities = {(agent.parent_thread_id, agent.child_thread_id) for agent in self.agents}
        if len(identities) != len(self.agents):
            raise DelegationEvidenceError("delegated agents contain duplicate edges")
        if self.agents != _canonical_agents(self.root_thread_id, self.agents):
            raise DelegationEvidenceError("delegated agents must use canonical rooted order")


@dataclass(frozen=True, slots=True)
class DelegationSnapshot:
    edges: frozenset[tuple[str, str]]
    child_tokens: tuple[tuple[str, int], ...]
    database_existed: bool

    def __post_init__(self) -> None:
        child_ids = {child_thread_id for child_thread_id, _ in self.child_tokens}
        if len(child_ids) != len(self.child_tokens):
            raise DelegationEvidenceError("delegation baseline contains duplicate children")
        if child_ids != {child_thread_id for _, child_thread_id in self.edges}:
            raise DelegationEvidenceError("delegation baseline metadata is incomplete")
        if any(tokens_used < 0 for _, tokens_used in self.child_tokens):
            raise DelegationEvidenceError("delegation baseline token usage is invalid")


def delegation_receipt_to_mapping(receipt: DelegationReceipt) -> dict[str, object]:
    if not isinstance(receipt, DelegationReceipt):
        raise DelegationEvidenceError("delegation receipt has an invalid type")
    return {
        "schema_version": receipt.schema_version,
        "observer": receipt.observer,
        "codex_version": receipt.codex_version,
        "root_thread_id": receipt.root_thread_id,
        "root_model": receipt.root_model,
        "root_reasoning_effort": receipt.root_reasoning_effort,
        "agents": [
            {
                "parent_thread_id": agent.parent_thread_id,
                "child_thread_id": agent.child_thread_id,
                "agent_name": agent.agent_name,
                "model": agent.model,
                "reasoning_effort": agent.reasoning_effort,
                "edge_status": agent.edge_status,
                "tokens_used": agent.tokens_used,
            }
            for agent in receipt.agents
        ],
    }


def delegation_receipt_to_json(receipt: DelegationReceipt) -> str:
    return json.dumps(
        delegation_receipt_to_mapping(receipt),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_delegation_receipt(value: object) -> DelegationReceipt:
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "observer",
        "codex_version",
        "root_thread_id",
        "root_model",
        "root_reasoning_effort",
        "agents",
    }:
        raise DelegationEvidenceError("delegation receipt fields are invalid")
    raw_agents = value["agents"]
    if not isinstance(raw_agents, list) or len(raw_agents) > MAX_DELEGATED_AGENTS:
        raise DelegationEvidenceError("delegated agents must be a bounded array")
    agents: list[DelegatedAgent] = []
    for raw in raw_agents:
        if not isinstance(raw, dict) or set(raw) != {
            "parent_thread_id",
            "child_thread_id",
            "agent_name",
            "model",
            "reasoning_effort",
            "edge_status",
            "tokens_used",
        }:
            raise DelegationEvidenceError("delegated agent fields are invalid")
        try:
            agents.append(DelegatedAgent(**raw))
        except TypeError as exc:
            raise DelegationEvidenceError("delegated agent fields are invalid") from exc
    try:
        return DelegationReceipt(
            schema_version=value["schema_version"],
            observer=value["observer"],
            codex_version=value["codex_version"],
            root_thread_id=value["root_thread_id"],
            root_model=value["root_model"],
            root_reasoning_effort=value["root_reasoning_effort"],
            agents=tuple(agents),
        )
    except TypeError as exc:
        raise DelegationEvidenceError("delegation receipt fields are invalid") from exc


def snapshot_delegations(
    codex_home: Path,
    *,
    database_required: bool,
) -> DelegationSnapshot:
    """Read only the durable parent-child edge identities before one Turn."""
    database = codex_home / _CODEX_STATE_DATABASE
    if not database.exists():
        if database.is_symlink() or database_required:
            raise DelegationEvidenceError("Codex state database is unavailable")
        return DelegationSnapshot(frozenset(), (), False)
    with closing(_open_state_database(database)) as connection:
        _require_schema(connection)
        rows = connection.execute(
            "SELECT edges.parent_thread_id, edges.child_thread_id, threads.tokens_used "
            "FROM thread_spawn_edges AS edges "
            "JOIN threads ON threads.id = edges.child_thread_id"
        ).fetchall()
        edges = frozenset(
            (str(row["parent_thread_id"]), str(row["child_thread_id"]))
            for row in rows
        )
        child_tokens = tuple(
            sorted(
                (str(row["child_thread_id"]), _tokens_used(row["tokens_used"]))
                for row in rows
            )
        )
        edge_count = connection.execute(
            "SELECT COUNT(*) FROM thread_spawn_edges"
        ).fetchone()[0]
        if edge_count != len(rows):
            raise DelegationEvidenceError("Codex spawn edge metadata is incomplete")
    for parent_thread_id, child_thread_id in edges:
        try:
            validate_session_id(parent_thread_id)
            validate_session_id(child_thread_id)
        except ValueError as exc:
            raise DelegationEvidenceError("Codex spawn edge identity is invalid") from exc
    return DelegationSnapshot(edges, child_tokens, True)


def observe_delegation_receipt(
    codex_home: Path,
    *,
    baseline: DelegationSnapshot,
    root_thread_id: str,
    policy: AgentRuntimePolicy,
) -> DelegationReceipt:
    """Build a receipt from new or newly-active spawn edges without messages."""
    if not isinstance(baseline, DelegationSnapshot):
        raise DelegationEvidenceError("delegation baseline is invalid")
    if not isinstance(policy, AgentRuntimePolicy):
        raise DelegationEvidenceError("agent runtime policy is invalid")
    validate_session_id(root_thread_id)
    database = codex_home / _CODEX_STATE_DATABASE
    with closing(_open_state_database(database)) as connection:
        _require_schema(connection)
        edge_rows = connection.execute(
            "SELECT parent_thread_id, child_thread_id, status "
            "FROM thread_spawn_edges ORDER BY parent_thread_id, child_thread_id"
        ).fetchall()
        after_edges = frozenset(
            (str(row["parent_thread_id"]), str(row["child_thread_id"]))
            for row in edge_rows
        )
        if not baseline.edges.issubset(after_edges):
            raise DelegationEvidenceError("Codex spawn edges changed ambiguously")
        root = connection.execute(
            "SELECT id, cli_version, model, reasoning_effort FROM threads WHERE id = ?",
            (root_thread_id,),
        ).fetchone()
        if root is None:
            raise DelegationEvidenceError("Codex root thread metadata is missing")
        if (
            root["cli_version"] != policy.codex_version
            or root["model"] != policy.primary_model
            or root["reasoning_effort"] != policy.primary_reasoning_effort
        ):
            raise DelegationEvidenceError("Codex root thread conflicts with agent policy")
        edge_status = {
            (str(row["parent_thread_id"]), str(row["child_thread_id"])): row[
                "status"
            ]
            for row in edge_rows
        }
        profiles = policy.profiles_by_name
        baseline_tokens = dict(baseline.child_tokens)
        current_tokens: dict[str, int] = {}
        for _, child_thread_id in after_edges:
            child_row = connection.execute(
                "SELECT tokens_used FROM threads WHERE id = ?", (child_thread_id,)
            ).fetchone()
            if child_row is None:
                raise DelegationEvidenceError("Codex child thread metadata is missing")
            current_tokens[child_thread_id] = _tokens_used(child_row["tokens_used"])
        for child_thread_id, before_tokens in baseline_tokens.items():
            if current_tokens.get(child_thread_id, -1) < before_tokens:
                raise DelegationEvidenceError("Codex child token usage changed ambiguously")

        observed_edges = {
            edge
            for edge in after_edges
            if edge not in baseline.edges
            or current_tokens[edge[1]] > baseline_tokens[edge[1]]
        }
        observed: list[DelegatedAgent] = []
        for parent_thread_id, child_thread_id in sorted(observed_edges):
            if parent_thread_id != root_thread_id:
                raise DelegationEvidenceError(
                    "Codex delegated agents must be direct children of the root thread"
                )
            child = connection.execute(
                "SELECT id, cli_version, agent_role, model, reasoning_effort, tokens_used "
                "FROM threads WHERE id = ?",
                (child_thread_id,),
            ).fetchone()
            if child is None:
                raise DelegationEvidenceError("Codex child thread metadata is missing")
            profile = profiles.get(child["agent_role"])
            if (
                child["cli_version"] != policy.codex_version
                or profile is None
                or child["model"] != profile.model
                or child["reasoning_effort"] != profile.reasoning_effort
            ):
                raise DelegationEvidenceError(
                    "Codex child thread conflicts with an agent policy profile"
                )
            observed.append(
                DelegatedAgent(
                    parent_thread_id=parent_thread_id,
                    child_thread_id=child_thread_id,
                    agent_name=profile.name,
                    model=profile.model,
                    reasoning_effort=profile.reasoning_effort,
                    edge_status=edge_status[(parent_thread_id, child_thread_id)],
                    tokens_used=(
                        current_tokens[child_thread_id]
                        - baseline_tokens.get(child_thread_id, 0)
                    ),
                )
            )
    return DelegationReceipt(
        codex_version=policy.codex_version,
        root_thread_id=root_thread_id,
        root_model=policy.primary_model,
        root_reasoning_effort=policy.primary_reasoning_effort,
        agents=_canonical_agents(root_thread_id, tuple(observed)),
    )


def _canonical_agents(
    root_thread_id: str,
    agents: tuple[DelegatedAgent, ...],
) -> tuple[DelegatedAgent, ...]:
    remaining = set(agents)
    known_threads = {root_thread_id}
    ordered: list[DelegatedAgent] = []
    while remaining:
        ready = sorted(
            agent for agent in remaining if agent.parent_thread_id in known_threads
        )
        if not ready:
            raise DelegationEvidenceError("delegation receipt contains an orphan edge")
        for agent in ready:
            remaining.remove(agent)
            known_threads.add(agent.child_thread_id)
            ordered.append(agent)
    return tuple(ordered)


def _tokens_used(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DelegationEvidenceError("Codex child token usage is invalid")
    return value


def _open_state_database(database: Path) -> sqlite3.Connection:
    _protected_database_file(database)
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
        connection.close()
        raise DelegationEvidenceError("Codex state database integrity is invalid")
    return connection


def _protected_database_file(database: Path) -> None:
    try:
        metadata = database.lstat()
    except OSError as exc:
        raise DelegationEvidenceError("Codex state database is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & 0o022
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_STATE_DATABASE_BYTES
    ):
        raise DelegationEvidenceError("Codex state database is not protected")
    for suffix in ("-wal", "-shm"):
        auxiliary = database.with_name(database.name + suffix)
        if not auxiliary.exists():
            if auxiliary.is_symlink():
                raise DelegationEvidenceError("Codex state database is not protected")
            continue
        auxiliary_metadata = auxiliary.lstat()
        if (
            not stat.S_ISREG(auxiliary_metadata.st_mode)
            or auxiliary_metadata.st_uid != os.geteuid()
            or auxiliary_metadata.st_mode & 0o022
        ):
            raise DelegationEvidenceError("Codex state database is not protected")


def _require_schema(connection: sqlite3.Connection) -> None:
    required = {
        "thread_spawn_edges": {"parent_thread_id", "child_thread_id", "status"},
        "threads": {
            "id",
            "cli_version",
            "agent_role",
            "model",
            "reasoning_effort",
            "tokens_used",
        },
    }
    tables = {
        str(row["name"])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    if not set(required).issubset(tables):
        raise DelegationEvidenceError("Codex state database schema is unsupported")
    for table, expected_columns in required.items():
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not expected_columns.issubset(columns):
            raise DelegationEvidenceError("Codex state database schema is unsupported")
