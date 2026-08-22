from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from codex_dispatcher.delegation_evidence import (
    DelegationEvidenceError,
    delegation_receipt_to_mapping,
    observe_delegation_receipt,
    parse_delegation_receipt,
    snapshot_delegations,
)
from codex_dispatcher.runner_policy import PolicyBundle


ROOT_SESSION = "123e4567-e89b-12d3-a456-426614174000"
CHILD_SESSION = "223e4567-e89b-12d3-a456-426614174000"


def policy_bundle(root: Path) -> PolicyBundle:
    source = Path(__file__).resolve().parents[1] / "config" / "runner-codex-policy"
    destination = root / "policy"
    shutil.copytree(source, destination)
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    return PolicyBundle.load(destination, manifest["policy_digest"])


def create_state_database(codex_home: Path, *, agent_role: str = "terra_worker") -> Path:
    database = codex_home / "state_5.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            "CREATE TABLE thread_spawn_edges (parent_thread_id TEXT NOT NULL, "
            "child_thread_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, cli_version TEXT NOT NULL, "
            "agent_role TEXT, model TEXT NOT NULL, reasoning_effort TEXT NOT NULL, "
            "tokens_used INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
            (ROOT_SESSION, "0.147.0", None, "gpt-5.6-sol", "xhigh", 100),
        )
        connection.execute(
            "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
            (
                CHILD_SESSION,
                "0.147.0",
                agent_role,
                "gpt-5.6-terra",
                "medium",
                42,
            ),
        )
        connection.execute(
            "INSERT INTO thread_spawn_edges VALUES (?, ?, ?)",
            (ROOT_SESSION, CHILD_SESSION, "closed"),
        )
        connection.commit()
    database.chmod(0o600)
    return database


class DelegationEvidenceTests(unittest.TestCase):
    def test_observes_only_new_policy_verified_metadata(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            codex_home.mkdir(mode=0o700)
            policy = policy_bundle(root).runtime_policy()
            baseline = snapshot_delegations(codex_home, database_required=False)
            create_state_database(codex_home)

            receipt = observe_delegation_receipt(
                codex_home,
                baseline=baseline,
                root_thread_id=ROOT_SESSION,
                policy=policy,
            )

            self.assertEqual("gpt-5.6-sol", receipt.root_model)
            self.assertEqual(1, len(receipt.agents))
            self.assertEqual("terra_worker", receipt.agents[0].agent_name)
            self.assertEqual("gpt-5.6-terra", receipt.agents[0].model)
            self.assertEqual(42, receipt.agents[0].tokens_used)
            self.assertEqual(
                receipt,
                parse_delegation_receipt(delegation_receipt_to_mapping(receipt)),
            )

            baseline = snapshot_delegations(codex_home, database_required=True)
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
                connection.execute(
                    "UPDATE threads SET tokens_used = 50 WHERE id = ?",
                    (CHILD_SESSION,),
                )
                connection.commit()
            reused_receipt = observe_delegation_receipt(
                codex_home,
                baseline=baseline,
                root_thread_id=ROOT_SESSION,
                policy=policy,
            )
            self.assertEqual(8, reused_receipt.agents[0].tokens_used)

    def test_rejects_profile_drift_and_edge_deletion(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            codex_home = root / "codex-home"
            codex_home.mkdir(mode=0o700)
            policy = policy_bundle(root).runtime_policy()
            create_state_database(codex_home, agent_role="unknown_worker")
            with self.assertRaisesRegex(DelegationEvidenceError, "profile"):
                observe_delegation_receipt(
                    codex_home,
                    baseline=snapshot_delegations(
                        root / "missing-home", database_required=False
                    ),
                    root_thread_id=ROOT_SESSION,
                    policy=policy,
                )

            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
                connection.execute(
                    "UPDATE threads SET agent_role = 'terra_worker' WHERE id = ?",
                    (CHILD_SESSION,),
                )
                connection.commit()
            baseline = snapshot_delegations(codex_home, database_required=True)
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
                connection.execute(
                    "UPDATE threads SET tokens_used = 41 WHERE id = ?",
                    (CHILD_SESSION,),
                )
                connection.commit()
            with self.assertRaisesRegex(DelegationEvidenceError, "token usage"):
                observe_delegation_receipt(
                    codex_home,
                    baseline=baseline,
                    root_thread_id=ROOT_SESSION,
                    policy=policy,
                )

            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
                connection.execute(
                    "UPDATE threads SET tokens_used = 42 WHERE id = ?",
                    (CHILD_SESSION,),
                )
                connection.commit()
            baseline = snapshot_delegations(codex_home, database_required=True)
            with closing(sqlite3.connect(codex_home / "state_5.sqlite")) as connection:
                connection.execute("DELETE FROM thread_spawn_edges")
                connection.commit()
            with self.assertRaisesRegex(DelegationEvidenceError, "ambiguously"):
                observe_delegation_receipt(
                    codex_home,
                    baseline=baseline,
                    root_thread_id=ROOT_SESSION,
                    policy=policy,
                )


if __name__ == "__main__":
    unittest.main()
