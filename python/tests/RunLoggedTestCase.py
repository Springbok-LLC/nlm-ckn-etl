"""_run_logged logs a subprocess's lifecycle and forwards its output."""

import io
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

import _common  # noqa: E402
import logging_setup  # noqa: E402

_LOG_KEYS = ("CORRELATION_ID", "PHASE", "GIT_SHA", "LOG_FORMAT")


def _py(code):
    return [sys.executable, "-c", code]


class RunLoggedTestCase(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items() if k not in _LOG_KEYS}
        patcher = patch.dict(os.environ, {**env, "CORRELATION_ID": "run-x"}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.events = io.StringIO()
        logging_setup.configure_logging("etl-test", stream=self.events)
        self.addCleanup(structlog.contextvars.clear_contextvars)
        self.addCleanup(structlog.reset_defaults)
        # Pass-through lines are written to sys.stdout, apart from the events.
        self.passthrough = io.StringIO()
        patcher = patch.object(sys, "stdout", self.passthrough)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _events(self):
        return [json.loads(x) for x in self.events.getvalue().splitlines()]

    def _by_message(self, message):
        return [e for e in self._events() if e["message"] == message]

    def test_success_logs_started_and_finished(self):
        _common._run_logged(_py("pass"), phase="results")
        (started,) = self._by_message("subprocess_started")
        (finished,) = self._by_message("subprocess_finished")
        self.assertEqual(started["phase"], "results")
        self.assertEqual(started["argv"][-1], "pass")
        self.assertEqual(finished["rc"], 0)
        self.assertIsInstance(finished["duration_ms"], int)
        self.assertEqual(finished["correlation_id"], "run-x")

    def test_structured_child_line_passes_through_unchanged(self):
        line = '{"message": "tuples_written", "level": "INFO", "records_out": 3}'
        _common._run_logged(_py(f"print({line!r})"))
        self.assertEqual(self.passthrough.getvalue(), line + "\n")
        self.assertEqual(self._by_message("subprocess_output"), [])

    def test_plain_stdout_becomes_info_event(self):
        _common._run_logged(_py("print('hello from child')"))
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(event["level"], "INFO")
        self.assertEqual(event["stream"], "stdout")
        self.assertEqual(event["line"], "hello from child")

    def test_output_events_carry_the_correlation_id(self):
        # Reader threads start with an empty context; the id must be copied in.
        _common._run_logged(_py("print('x')"), phase="fetch")
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(event["correlation_id"], "run-x")

    def test_stderr_becomes_warn_event(self):
        _common._run_logged(
            _py("import sys; print('Exception in thread main', file=sys.stderr)")
        )
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(event["level"], "WARN")
        self.assertEqual(event["stream"], "stderr")

    def test_json_without_level_is_wrapped_not_passed_through(self):
        _common._run_logged(_py("print('{\"just\": \"data\"}')"))
        self.assertEqual(self.passthrough.getvalue(), "")
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(event["line"], '{"just": "data"}')

    def test_blank_lines_are_dropped(self):
        _common._run_logged(_py("print(); print('x'); print('   ')"))
        self.assertEqual(len(self._by_message("subprocess_output")), 1)

    def test_nonzero_exit_logs_error_and_raises(self):
        with self.assertRaises(subprocess.CalledProcessError) as ctx:
            _common._run_logged(_py("import sys; sys.exit(3)"), phase="ontology")
        self.assertEqual(ctx.exception.returncode, 3)
        (failed,) = self._by_message("subprocess_failed")
        self.assertEqual(failed["level"], "ERROR")
        self.assertEqual(failed["reason"], "nonzero_exit")
        self.assertEqual(failed["rc"], 3)
        self.assertEqual(failed["phase"], "ontology")
        self.assertNotIn("oom_suspected", failed)
        self.assertEqual(self._by_message("subprocess_finished"), [])

    @unittest.skipIf(os.name == "nt", "needs POSIX signals")
    def test_sigkill_flags_suspected_out_of_memory(self):
        with self.assertRaises(subprocess.CalledProcessError):
            _common._run_logged(_py("import os; os.kill(os.getpid(), 9)"))
        (failed,) = self._by_message("subprocess_failed")
        self.assertTrue(failed["oom_suspected"])

    def test_child_receives_logging_context(self):
        _common._run_logged(
            _py(
                "import os; print(os.environ['CORRELATION_ID'], os.environ['PHASE'])"
            ),
            phase="fetch",
        )
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(event["line"], "run-x fetch")

    def test_large_output_on_both_streams_does_not_deadlock(self):
        code = (
            "import sys\n"
            "for i in range(5000):\n"
            "    print('o' * 200)\n"
            "    print('e' * 200, file=sys.stderr)\n"
        )
        _common._run_logged(_py(code))
        events = self._by_message("subprocess_output")
        self.assertEqual(len(events), 10000)
        self.assertEqual(sum(e["stream"] == "stderr" for e in events), 5000)

    def test_overlong_line_is_truncated(self):
        _common._run_logged(_py("print('x' * 40000)"))
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(len(event["line"]), _common._MAX_CHILD_LINE)
        self.assertTrue(event["truncated"])

    def test_line_of_exactly_the_limit_is_not_truncated(self):
        n = _common._MAX_CHILD_LINE
        _common._run_logged(_py(f"print('x' * {n})"))
        (event,) = self._by_message("subprocess_output")
        self.assertEqual(len(event["line"]), n)
        self.assertNotIn("truncated", event)

    def test_overlong_line_is_drained_and_the_next_line_survives(self):
        _common._run_logged(_py("print('x' * 100000); print('after')"))
        lines = [e["line"] for e in self._by_message("subprocess_output")]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "after")

    def test_overlong_structured_line_is_truncated_not_passed_through(self):
        code = (
            "import json\n"
            "print(json.dumps({'message': 'm', 'level': 'INFO',"
            " 'blob': 'y' * 50000}))\n"
        )
        _common._run_logged(_py(code))
        self.assertEqual(self.passthrough.getvalue(), "")
        (event,) = self._by_message("subprocess_output")
        self.assertTrue(event["truncated"])
        self.assertEqual(len(event["line"]), _common._MAX_CHILD_LINE)

    def test_descendant_holding_the_pipes_does_not_block_the_flow(self):
        code = (
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c',"
            " 'import time; time.sleep(4)'])\n"
        )
        with patch.object(_common, "_READER_GRACE_S", 0.3):
            started = time.monotonic()
            _common._run_logged(_py(code))
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 3)
        self.assertEqual(len(self._by_message("subprocess_pipes_held")), 1)
        self.assertEqual(len(self._by_message("subprocess_finished")), 1)

    def test_console_mode_leaves_child_output_alone(self):
        with patch.dict(os.environ, {"LOG_FORMAT": "console"}):
            with patch.object(subprocess, "Popen") as popen:
                popen.return_value.wait.return_value = 0
                _common._run_logged(["true"])
        kwargs = popen.call_args.kwargs
        self.assertIsNone(kwargs["stdout"])
        self.assertIsNone(kwargs["stderr"])

    def test_interrupt_kills_the_child(self):
        proc = MagicMock()
        proc.stdout = io.StringIO()
        proc.stderr = io.StringIO()
        proc.wait.side_effect = [KeyboardInterrupt, 0]
        with patch.object(subprocess, "Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                _common._run_logged(["sleep", "60"])
        proc.kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
