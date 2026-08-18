"""Port for mechanically verifying an exported Runner bundle in quarantine."""

from __future__ import annotations

from typing import Protocol

from codex_dispatcher.publisher import VerifiedBundle
from codex_dispatcher.runner_transport import RunnerExportReply
from codex_dispatcher.work_items import WorkItem


class BundleVerifier(Protocol):
    def verify(
        self,
        artifact: bytes,
        *,
        manifest: RunnerExportReply,
        work_item: WorkItem,
    ) -> VerifiedBundle: ...
