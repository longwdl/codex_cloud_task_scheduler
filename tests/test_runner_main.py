from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_dispatcher.runner_main import (
    RunnerConfigurationError,
    build_runner_service,
    load_runner_configuration,
    run,
)


def protected_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700 if executable else 0o600)


def config(root: Path) -> Path:
    git = root / "git"
    codex = root / "codex"
    schema = root / "schema.json"
    protected_file(git, "#!/bin/sh\nexit 0\n", executable=True)
    protected_file(codex, "#!/bin/sh\nexit 0\n", executable=True)
    protected_file(schema, "{}\n")
    codex_home = root / "codex-home"
    codex_home.mkdir(mode=0o700, exist_ok=True)
    codex_home.chmod(0o700)
    work_items = root / "work-items"
    work_items.mkdir(mode=0o700, exist_ok=True)
    work_items.chmod(0o700)
    run = root / "run"
    run.mkdir(mode=0o700, exist_ok=True)
    run.chmod(0o700)
    config_path = root / "config.json"
    protected_file(
        config_path,
        json.dumps(
            {
                "version": 1,
                "git_path": str(git),
                "codex_path": str(codex),
                "codex_home": str(codex_home),
                "output_schema": str(schema),
                "work_items_root": str(work_items),
                "active_lock_path": str(run / "active.lock"),
                "git_timeout_seconds": 10,
                "codex_timeout_seconds": 20,
            }
        ),
    )
    return config_path


class RunnerMainTests(unittest.TestCase):
    def test_loads_only_exact_protected_fixed_path_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            loaded = load_runner_configuration(config(root))
            self.assertEqual((root / "git").resolve(), loaded.git_path)
            self.assertEqual((root / "codex-home").resolve(), loaded.codex_home)
            self.assertEqual((root / "work-items").resolve(), loaded.work_items_root)
            self.assertEqual(20.0, loaded.codex_timeout_seconds)
            self.assertIsNotNone(build_runner_service(loaded))

    def test_rejects_writable_duplicate_and_relative_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            path.chmod(0o666)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected"):
                load_runner_configuration(path)

            path.chmod(0o600)
            path.write_text('{"version":1,"version":1}', encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "malformed"):
                load_runner_configuration(path)

            path = config(root)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["work_items_root"] = "relative"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "absolute"):
                load_runner_configuration(path)

    def test_rejects_unprotected_shared_codex_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            (root / "codex-home").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            (root / "codex-home").chmod(0o700)
            link = root / "codex-home-link"
            link.symlink_to(root / "codex-home", target_is_directory=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["codex_home"] = str(link)
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

    def test_rejects_untrusted_files_and_unprotected_mutable_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            with patch(
                "codex_dispatcher.runner_main.os.geteuid",
                return_value=path.stat().st_uid + 1,
            ):
                with self.assertRaisesRegex(RunnerConfigurationError, "trusted"):
                    load_runner_configuration(path)

            path = config(root)
            (root / "work-items").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            path = config(root)
            (root / "run").chmod(0o755)
            with self.assertRaisesRegex(RunnerConfigurationError, "protected directory"):
                load_runner_configuration(path)

            path = config(root)
            active_lock = root / "run" / "active.lock"
            active_lock.write_text("", encoding="utf-8")
            active_lock.chmod(0o644)
            with self.assertRaisesRegex(RunnerConfigurationError, "active_lock_path"):
                load_runner_configuration(path)

    def test_rejects_executable_below_a_replaceable_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = config(root)
            tools = root / "mutable-tools"
            tools.mkdir(mode=0o700)
            codex = tools / "codex"
            protected_file(codex, "#!/bin/sh\nexit 0\n", executable=True)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["codex_path"] = str(codex)
            path.write_text(json.dumps(payload), encoding="utf-8")
            tools.chmod(0o770)

            with self.assertRaisesRegex(RunnerConfigurationError, "parent directories"):
                load_runner_configuration(path)

    def test_forced_command_failure_is_generic_and_emits_no_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = io.BytesIO()
            error = io.BytesIO()
            result = run(
                config_path=config(root),
                input_stream=io.BytesIO(b"malformed secret=do-not-echo"),
                output_stream=output,
                error_stream=error,
            )
            self.assertEqual(2, result)
            self.assertEqual(b"", output.getvalue())
            self.assertEqual(b"codex-runner-v1: request rejected\n", error.getvalue())
            self.assertNotIn(b"do-not-echo", error.getvalue())


if __name__ == "__main__":
    unittest.main()
