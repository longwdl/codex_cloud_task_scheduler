from __future__ import annotations

import unittest

from codex_dispatcher.domain import InvalidStateTransition, Run, RunState


class DomainTests(unittest.TestCase):
    def test_normal_path_reaches_terminal_review(self) -> None:
        run = self._new_run()
        for state in (
            RunState.CLAIMED,
            RunState.BRANCH_PREPARED,
            RunState.DISPATCHING,
            RunState.RUNNING,
            RunState.RESULT_READY,
            RunState.APPLYING,
            RunState.VALIDATING,
            RunState.DELIVERING,
            RunState.REVIEW,
        ):
            run = run.transition_to(state)
        self.assertTrue(run.is_terminal)
        self.assertFalse(run.is_active)

    def test_any_active_run_can_be_blocked_but_terminal_runs_cannot_move(self) -> None:
        run = self._new_run()
        blocked = run.transition_to(RunState.BLOCKED)
        self.assertTrue(blocked.is_terminal)
        with self.assertRaisesRegex(InvalidStateTransition, "blocked.*claimed"):
            blocked.transition_to(RunState.CLAIMED)

    def test_skipped_normal_transition_is_rejected(self) -> None:
        run = self._new_run()
        with self.assertRaisesRegex(InvalidStateTransition, "discovered.*running"):
            run.transition_to(RunState.RUNNING)

    def test_rejects_invalid_identifiers_and_hashes_before_persistence(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/repository"):
            self._new_run(repository="repo")
        with self.assertRaisesRegex(ValueError, "positive integer"):
            self._new_run(issue_number=0)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self._new_run(prompt_sha256="short")

    @staticmethod
    def _new_run(
        *,
        repository: str = "owner/repo",
        issue_number: int = 1,
        prompt_sha256: str = "a" * 64,
    ) -> Run:
        return Run.new(
            repository=repository,
            issue_number=issue_number,
            prompt_sha256=prompt_sha256,
            base_branch="main",
            cloud_environment_id="env",
        )


if __name__ == "__main__":
    unittest.main()
