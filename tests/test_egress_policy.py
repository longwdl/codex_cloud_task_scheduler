from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.egress_policy import (
    EgressPolicyError,
    main,
    validate_egress_policy,
)


def policy(path: Path, text: str) -> None:
    path.write_text(text, encoding="ascii")
    path.chmod(0o640)


class EgressPolicyTests(unittest.TestCase):
    def test_accepts_exact_and_narrow_suffix_domains_and_exact_addresses(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowlist = root / "allowlist"
            blocklist = root / "blocklist"
            policy(
                allowlist,
                "# reviewed\nauth.openai.com\n.objects.githubusercontent.com\n",
            )
            policy(blocklist, "192.0.2.1/32\n2001:db8::1/128\n")

            with patch(
                "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                allowlist.stat().st_uid,
            ):
                self.assertEqual((2, 2), validate_egress_policy(allowlist, blocklist))

    def test_rejects_wildcards_urls_ips_broad_suffixes_and_duplicates(self) -> None:
        invalid_rules = (
            "*.example.com",
            "https://example.com",
            "127.0.0.1",
            ".example.com",
            "localhost",
            "EXAMPLE.com",
            "example.com:443",
            "example.com/path",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowlist = root / "allowlist"
            blocklist = root / "blocklist"
            policy(blocklist, "192.0.2.1/32\n")
            for rule in invalid_rules:
                with self.subTest(rule=rule):
                    policy(allowlist, f"{rule}\n")
                    with patch(
                        "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                        allowlist.stat().st_uid,
                    ), self.assertRaisesRegex(EgressPolicyError, "allowlist"):
                        validate_egress_policy(allowlist, blocklist)

            policy(allowlist, "example.com\nexample.com\n")
            with patch(
                "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                allowlist.stat().st_uid,
            ), self.assertRaisesRegex(EgressPolicyError, "duplicate"):
                validate_egress_policy(allowlist, blocklist)

    def test_rejects_empty_broad_noncanonical_and_replaceable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowlist = root / "allowlist"
            blocklist = root / "blocklist"
            policy(allowlist, "example.com\n")
            for rule in ("192.0.2.0/24", "192.0.2.1", "2001:0db8::1/128"):
                with self.subTest(rule=rule):
                    policy(blocklist, f"{rule}\n")
                    with patch(
                        "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                        allowlist.stat().st_uid,
                    ), self.assertRaisesRegex(EgressPolicyError, "blocklist"):
                        validate_egress_policy(allowlist, blocklist)

            policy(blocklist, "192.0.2.1/32\n")
            allowlist.chmod(0o666)
            with patch(
                "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                allowlist.stat().st_uid,
            ), self.assertRaisesRegex(EgressPolicyError, "protected"):
                validate_egress_policy(allowlist, blocklist)

            allowlist.unlink()
            allowlist.symlink_to(blocklist)
            with patch(
                "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                blocklist.stat().st_uid,
            ), self.assertRaisesRegex(EgressPolicyError, "protected"):
                validate_egress_policy(allowlist, blocklist)

    def test_cli_reports_only_counts_not_policy_contents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            allowlist = root / "allowlist"
            blocklist = root / "blocklist"
            policy(allowlist, "sensitive-destination.example\n")
            policy(blocklist, "192.0.2.1/32\n")
            output = io.StringIO()
            error = io.StringIO()

            with patch(
                "codex_dispatcher.egress_policy._REQUIRED_OWNER_UID",
                allowlist.stat().st_uid,
            ), redirect_stdout(output), redirect_stderr(error):
                result = main(
                    [
                        "--allowlist",
                        str(allowlist),
                        "--blocklist",
                        str(blocklist),
                    ]
                )

            self.assertEqual(0, result)
            self.assertEqual("codex-egress-policy: valid allow=1 block=1\n", output.getvalue())
            self.assertEqual("", error.getvalue())
            self.assertNotIn("sensitive-destination", output.getvalue())


if __name__ == "__main__":
    unittest.main()
