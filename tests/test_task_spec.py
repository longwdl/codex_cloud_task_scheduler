from __future__ import annotations

import unittest

from codex_dispatcher.task_spec import (
    TaskSpecError,
    is_path_allowed,
    parse_acceptance_criteria,
    parse_task_spec,
)


BODY = """## 目标
安全地实现解析器
## 背景
输入来自不可信 Issue
## 范围
仅 src 与 tests
## 非目标
不联网
## 验收条件
- [ ] 测试通过
## 允许修改路径
- src/codex_dispatcher
- tests
## 验证命令
~~~bash
python -m unittest
~~~
## 阻塞条件
需求冲突
## 部署限制
不得部署 production
"""


class TaskSpecTests(unittest.TestCase):
    def test_parses_all_required_nonempty_sections(self) -> None:
        spec = parse_task_spec(BODY)
        self.assertEqual("安全地实现解析器", spec.objective)
        self.assertEqual(("src/codex_dispatcher", "tests"), spec.allowed_paths)

    def test_rejects_missing_or_empty_section(self) -> None:
        with self.assertRaisesRegex(TaskSpecError, "背景"):
            parse_task_spec(BODY.replace("## 背景\n输入来自不可信 Issue\n", ""))
        with self.assertRaisesRegex(TaskSpecError, "目标"):
            parse_task_spec(BODY.replace("安全地实现解析器", ""))

    def test_path_policy_rejects_escape_glob_and_hard_denylist(self) -> None:
        allowed = parse_task_spec(BODY).allowed_paths
        self.assertTrue(is_path_allowed("src/codex_dispatcher/module.py", allowed))
        unsafe_paths = (
            "../src/a.py",
            "src\\a.py",
            "/src/a.py",
            "src/*/a.py",
            ".env",
            "private.key",
            "id_rsa.pub",
            ".github/workflows/a.yml",
            "infra/production/main.tf",
        )
        for unsafe in unsafe_paths:
            self.assertFalse(is_path_allowed(unsafe, allowed), unsafe)

    def test_path_policy_rejects_control_characters(self) -> None:
        for path in ("docs/new\nfile.md", "docs/tab\tfile.md", "docs/del\x7ffile.md"):
            with self.subTest(path=repr(path)):
                self.assertFalse(is_path_allowed(path, ("docs",)))

    def test_allowlist_is_component_bounded(self) -> None:
        self.assertFalse(
            is_path_allowed(
                "src/codex_dispatcher_evil/a.py", ("src/codex_dispatcher",)
            )
        )

    def test_acceptance_parser_preserves_prose_and_recognizes_strict_predicates(self) -> None:
        criteria = parse_acceptance_criteria(
            """- [AC-1] required-check: tests
- [AC-2] changed-paths-within-allowed
- [AC-3] task-head-published
- [ ] 人工检查错误信息
"""
        )

        self.assertEqual(
            [
                "required-check",
                "changed-paths-within-allowed",
                "task-head-published",
                "manual",
            ],
            [item.predicate for item in criteria],
        )
        self.assertEqual("tests", criteria[0].argument)
        self.assertEqual("人工检查错误信息", criteria[3].description)
        self.assertTrue(criteria[3].criterion_id.startswith("AC-TEXT-004-"))

    def test_audit_and_explicit_manual_predicates_require_descriptions(self) -> None:
        criteria = parse_acceptance_criteria(
            "- [AC-1] audit: inspect edge-case behavior\n"
            "- [AC-2] manual: verify the external approval"
        )
        self.assertEqual(["audit", "manual"], [item.predicate for item in criteria])
        self.assertEqual(
            ["inspect edge-case behavior", "verify the external approval"],
            [item.argument for item in criteria],
        )
        for content in ("- [AC-1] audit", "- [AC-1] manual"):
            with self.subTest(content=content):
                with self.assertRaises(TaskSpecError):
                    parse_acceptance_criteria(content)
    def test_acceptance_parser_rejects_typos_unknown_predicates_and_duplicate_ids(self) -> None:
        invalid = (
            "- [AC-0] required-check: tests",
            "- [AC-1] required-check",
            "- [AC-1] invented-predicate",
            "- [AC-1] task-head-published: extra",
            "- [AC-1] task-head-published\n- [AC-1] changed-paths-within-allowed",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(TaskSpecError):
                parse_acceptance_criteria(value)
        self.assertFalse(
            is_path_allowed(
                "src/codex_dispatcher/private/a.py",
                ("src",),
                ("src/codex_dispatcher/private",),
            )
        )


if __name__ == "__main__":
    unittest.main()
