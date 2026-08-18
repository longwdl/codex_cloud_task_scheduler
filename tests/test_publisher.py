from __future__ import annotations

import unittest
from dataclasses import replace

from codex_dispatcher.publisher import (
    PublicationError,
    PublishRequest,
    VerifiedBundle,
    parse_publish_request,
    plan_publication,
)
from codex_dispatcher.work_items import WorkItem, WorkItemState


def work_item() -> WorkItem:
    item = WorkItem.new(
        repository="owner/repo",
        issue_number=42,
        issue_node_id="I_kwDOFixture42",
        base_branch="main",
        base_sha="a" * 40,
        at="2026-01-01T00:00:00.000000Z",
    )
    return item.transition_to(WorkItemState.PREPARING).transition_to(WorkItemState.READY)


def bundle(*, paths: tuple[str, ...] = ("src/main.py",)) -> VerifiedBundle:
    return VerifiedBundle(
        bundle_sha256="b" * 64,
        head_sha="c" * 40,
        parent_anchor_sha="a" * 40,
        changed_paths=paths,
        commit_count=1,
        size_bytes=1_024,
    )


class PublisherTests(unittest.TestCase):
    def test_request_contract_accepts_only_work_item_and_full_sha(self) -> None:
        request = PublishRequest(work_item().work_item_id, "c" * 40)
        self.assertEqual(request, parse_publish_request(request.to_json()))
        with self.assertRaises(PublicationError):
            parse_publish_request(
                request.to_json()[:-1] + ',"remote_url":"https://evil.invalid/repo"}'
            )
        with self.assertRaises(PublicationError):
            parse_publish_request('{"work_item_id":"x","work_item_id":"y"}')

    def test_plan_is_exact_fast_forward_task_ref_without_force_or_delete(self) -> None:
        item = work_item()
        verified = bundle()
        plan = plan_publication(
            request=PublishRequest(item.work_item_id, verified.head_sha),
            work_item=item,
            bundle=verified,
            issue_allowed_paths=("src",),
            repository_allowed_paths=("src", "tests"),
        )
        self.assertEqual(f"refs/heads/{item.task_branch}", plan.target_ref)
        self.assertIsNone(plan.expected_remote_sha)
        self.assertEqual("c" * 40, plan.source_sha)
        self.assertFalse(plan.force)
        self.assertFalse(plan.delete)

        published_item = replace(item, last_published_sha="d" * 40)
        next_bundle = VerifiedBundle(
            "e" * 64, "f" * 40, "d" * 40, ("src/next.py",), 1, 1_024
        )
        next_plan = plan_publication(
            request=PublishRequest(published_item.work_item_id, next_bundle.head_sha),
            work_item=published_item,
            bundle=next_bundle,
            issue_allowed_paths=("src",),
            repository_allowed_paths=("src",),
        )
        self.assertEqual("d" * 40, next_plan.expected_remote_sha)

    def test_rejects_head_anchor_path_and_completed_work_item_conflicts(self) -> None:
        item = work_item()
        cases = (
            (PublishRequest(item.work_item_id, "d" * 40), bundle(), ("src",)),
            (
                PublishRequest(item.work_item_id, "c" * 40),
                VerifiedBundle(
                    "b" * 64, "c" * 40, "d" * 40, ("src/main.py",), 1, 1_024
                ),
                ("src",),
            ),
            (PublishRequest(item.work_item_id, "c" * 40), bundle(paths=("docs/a.md",)), ("src",)),
        )
        for request, verified, allowed in cases:
            with self.subTest(request=request, verified=verified):
                with self.assertRaises(PublicationError):
                    plan_publication(
                        request=request,
                        work_item=item,
                        bundle=verified,
                        issue_allowed_paths=allowed,
                        repository_allowed_paths=("src", "docs"),
                    )

        completed = item.transition_to(WorkItemState.RUNNING)
        completed = completed.transition_to(WorkItemState.REVIEW)
        completed = completed.transition_to(WorkItemState.COMPLETED)
        with self.assertRaisesRegex(PublicationError, "completed"):
            plan_publication(
                request=PublishRequest(completed.work_item_id, "c" * 40),
                work_item=completed,
                bundle=bundle(),
                issue_allowed_paths=("src",),
                repository_allowed_paths=("src",),
            )


if __name__ == "__main__":
    unittest.main()
