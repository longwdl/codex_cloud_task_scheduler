from __future__ import annotations

import unittest

from codex_dispatcher.redaction import redact_text


class RedactionTests(unittest.TestCase):
    def test_redacts_common_credentials_and_explicit_secrets(self) -> None:
        result = redact_text(
            "Authorization: Bearer abc token=xyz password: hello "
            "private key=secret key value",
            ["also-secret"],
        )
        self.assertNotIn("abc", result)
        self.assertNotIn("xyz", result)
        self.assertNotIn("hello", result)
        self.assertNotIn("secret", result)
        self.assertNotIn("value", result)
        self.assertEqual("[REDACTED]", redact_text("also-secret", ["also-secret"]))

    def test_redacts_bare_provider_tokens(self) -> None:
        token = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        self.assertEqual("failed: [REDACTED]", redact_text(f"failed: {token}"))


if __name__ == "__main__":
    unittest.main()
