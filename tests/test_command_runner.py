from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from codex_dispatcher.command_runner import run_binary_command, run_command


class CommandRunnerTests(unittest.TestCase):
    def test_rejects_non_argv_and_nul_injection(self) -> None:
        with self.assertRaises(TypeError):
            run_command("echo hello")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            run_command(["echo", "bad\x00argument"])

    def test_captures_nonzero_and_truncates_output(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import sys; print('x' * 100); "
                "sys.stderr.write('e' * 100); raise SystemExit(7)",
            ],
            max_output_bytes=10,
        )
        self.assertEqual(7, result.returncode)
        self.assertTrue(result.stdout_truncated)
        self.assertTrue(result.stderr_truncated)
        self.assertIn("[output truncated]", result.stdout)

    def test_timeout_and_exception_output_are_redacted(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import time; print('token=leak', flush=True); time.sleep(2)",
            ],
            timeout_seconds=0.05,
            secrets=["leak"],
        )
        self.assertTrue(result.timed_out)
        self.assertEqual("command timed out", result.error)
        self.assertNotIn("leak", result.stdout)

    def test_environment_is_minimal_and_can_be_overridden(self) -> None:
        result = run_command(
            [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('ONLY_ME', 'missing')); "
                "print(os.environ.get('HOME', 'missing'))",
            ],
            env={"ONLY_ME": "yes"},
        )
        self.assertEqual("yes\nmissing\n", result.stdout)

    def test_shell_metacharacters_are_only_argv_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "should-not-exist"
            result = run_command(
                [sys.executable, "-c", "import sys; print(sys.argv[1])", f";touch {marker}"]
            )
            self.assertEqual(f";touch {marker}\n", result.stdout)
            self.assertFalse(marker.exists())

    def test_binary_runner_preserves_bytes_and_bounds_output(self) -> None:
        result = run_binary_command(
            [
                sys.executable,
                "-c",
                "import sys; data=sys.stdin.buffer.read(); "
                "sys.stdout.buffer.write(data + b'\\x00\\xff')",
            ],
            input_bytes=b"frame\x00bytes",
            max_output_bytes=100,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual(b"frame\x00bytes\x00\xff", result.stdout)

        truncated = run_binary_command(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 100)"],
            max_output_bytes=10,
        )
        self.assertEqual(b"x" * 10, truncated.stdout)
        self.assertTrue(truncated.stdout_truncated)

    @unittest.skipUnless(os.name == "posix", "exact process-group kill requires POSIX")
    def test_started_hook_can_kill_only_its_exact_session_leader(self) -> None:
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        observed = {}

        def interrupt(process) -> None:
            observed["pid"] = process.pid
            self.assertEqual(tuple(argv), process.argv)
            self.assertEqual(process.pid, process.process_group_id)
            self.assertEqual(process.pid, process.session_id)
            with self.assertRaisesRegex(RuntimeError, "mismatched"):
                process.kill_exact_process_group(
                    expected_argv=(*argv, "unexpected"),
                    expected_pid=process.pid,
                )
            self.assertFalse(process.termination_requested)
            process.kill_exact_process_group(
                expected_argv=argv,
                expected_pid=process.pid,
            )
            self.assertTrue(process.termination_requested)

        result = run_binary_command(argv, started_hook=interrupt)

        self.assertEqual(-signal.SIGKILL, result.returncode)
        self.assertFalse(result.timed_out)
        self.assertIsNone(result.error)
        self.assertGreater(observed["pid"], 0)

    def test_started_hook_failure_is_ambiguous_without_immediate_kill(self) -> None:
        result = run_binary_command(
            [sys.executable, "-c", "pass"],
            started_hook=lambda process: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        self.assertEqual(0, result.returncode)
        self.assertFalse(result.timed_out)
        self.assertEqual("command start hook failed", result.error)

    def test_started_hook_proof_budget_does_not_consume_command_timeout(self) -> None:
        result = run_binary_command(
            [sys.executable, "-c", "import time; time.sleep(0.5)"],
            timeout_seconds=0.4,
            started_hook=lambda process: time.sleep(0.2),
        )

        self.assertEqual(0, result.returncode)
        self.assertFalse(result.timed_out)

    def test_stdout_line_hook_observes_complete_lines_in_order(self) -> None:
        observed = []
        result = run_binary_command(
            [
                sys.executable,
                "-c",
                "import os,time; "
                "parts=(b'fir',b'st\\n\\nsec',b'ond\\nfin',b'al'); "
                "[(os.write(1, part), time.sleep(0.01)) for part in parts]",
            ],
            stdout_line_hook=observed.append,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"first\n\nsecond\nfinal", result.stdout)
        self.assertEqual([b"first", b"second", b"final"], observed)
        self.assertIsNone(result.error)

    def test_stdout_hook_receives_short_flushed_line_before_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            acknowledgement = Path(temp_dir) / "observed"

            def acknowledge(line: bytes) -> None:
                if line == b"thread.started":
                    acknowledgement.write_bytes(b"observed")

            result = run_binary_command(
                [
                    sys.executable,
                    "-c",
                    "import pathlib,sys,time\n"
                    "ack=pathlib.Path(sys.argv[1])\n"
                    "print('thread.started', flush=True)\n"
                    "deadline=time.monotonic()+3\n"
                    "while not ack.exists() and time.monotonic()<deadline:\n"
                    "    time.sleep(0.01)\n"
                    "sys.exit(0 if ack.exists() else 7)\n",
                    str(acknowledgement),
                ],
                timeout_seconds=5,
                stdout_line_hook=acknowledge,
            )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"thread.started\n", result.stdout)
        self.assertIsNone(result.error)
        self.assertFalse(result.timed_out)

    def test_stdout_line_hook_failure_does_not_stop_drain_or_process(self) -> None:
        observed = []

        def fail_once(line: bytes) -> None:
            observed.append(line)
            raise RuntimeError("must not escape or be reported")

        result = run_binary_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'one\\ntwo\\nthree')",
            ],
            stdout_line_hook=fail_once,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"one\ntwo\nthree", result.stdout)
        self.assertEqual([b"one"], observed)
        self.assertFalse(result.timed_out)
        self.assertEqual("command stdout hook failed", result.error)

    def test_stdout_line_hook_unterminated_line_is_bounded(self) -> None:
        observed = []
        result = run_binary_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 100000)",
            ],
            max_output_bytes=17,
            stdout_line_hook=observed.append,
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual(b"x" * 17, result.stdout)
        self.assertTrue(result.stdout_truncated)
        self.assertEqual([b"x" * 17], observed)

    def test_stdout_line_hook_timeout_error_takes_precedence(self) -> None:
        result = run_binary_command(
            [
                sys.executable,
                "-c",
                "import sys,time; print('one', flush=True); time.sleep(30)",
            ],
            timeout_seconds=0.05,
            stdout_line_hook=lambda line: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        self.assertTrue(result.timed_out)
        self.assertEqual("command timed out", result.error)


if __name__ == "__main__":
    unittest.main()
