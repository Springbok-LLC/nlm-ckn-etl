"""Under ``LOG_FORMAT=json`` every line a worker logs is one JSON object.

Runs real processes, so what is checked is what CloudWatch would receive: no
stray text between the records, and the core fields on each.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
# conftest.py patches LoaderUtilities for the session, so src must be importable.
sys.path.insert(0, str(_SRC))
_CORE = ("timestamp", "level", "service", "correlation_id", "logger", "message", "release")
_LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "CRITICAL"}

_SCRIPT = """
import logging, sys
import structlog
from logging_setup import configure_logging
configure_logging("etl-test")
structlog.get_logger("contract").info("structlog_event", n=1)
logging.getLogger("third.party").warning("stdlib record")
try:
    1 / 0
except ZeroDivisionError:
    structlog.get_logger("contract").exception("failed_event")
"""


def _env():
    env = {k: v for k, v in os.environ.items() if k not in ("PHASE", "GIT_SHA")}
    return {**env, "LOG_FORMAT": "json", "CORRELATION_ID": "contract-1", "PYTHONPATH": str(_SRC)}


def _records(text):
    records = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            record = json.loads(line)
        except ValueError:
            raise AssertionError(f"line {number} is not JSON: {line!r}") from None
        if not isinstance(record, dict):
            raise AssertionError(f"line {number} is not an object: {line!r}")
        records.append(record)
    return records


class LogContractTestCase(unittest.TestCase):
    def _assert_contract(self, records):
        self.assertTrue(records, "no log lines")
        for record in records:
            for key in _CORE:
                self.assertIn(key, record, f"{key} missing from {record}")
            self.assertIn(record["level"], _LEVELS)
            self.assertTrue(record["timestamp"].endswith("Z"))
            self.assertEqual(record["correlation_id"], "contract-1")

    def test_configured_process_logs_only_json(self):
        done = subprocess.run(
            [sys.executable, "-c", _SCRIPT],
            capture_output=True, text=True, env=_env(), cwd=_SRC, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stderr, "")
        records = _records(done.stdout)
        self._assert_contract(records)
        self.assertEqual(
            [r["message"] for r in records],
            ["structlog_event", "stdlib record", "failed_event"],
        )
        self.assertIn("ZeroDivisionError", records[-1]["exception"])

    def test_validator_logs_only_json_on_stderr(self):
        with tempfile.TemporaryDirectory() as data_dir:
            done = subprocess.run(
                [
                    sys.executable, str(_SRC / "ProductionDataValidator.py"),
                    "--data-dir", data_dir, "--no-annotations",
                ],
                capture_output=True, text=True, env=_env(), timeout=60,
            )
        self.assertEqual(done.returncode, 1, done.stderr)  # an empty tree is an error
        self.assertEqual(done.stdout, "")
        records = _records(done.stderr)
        self._assert_contract(records)
        messages = [r["message"] for r in records]
        self.assertEqual(messages[-1], "validation_finished")
        self.assertIn("validation_finding", messages)

    def test_validator_annotations_are_the_only_other_stderr_lines(self):
        # Under GitHub Actions the validator adds ``::error`` annotations beside
        # the JSON; they are meant for the runner, so they are the one exception.
        with tempfile.TemporaryDirectory() as data_dir:
            done = subprocess.run(
                [sys.executable, str(_SRC / "ProductionDataValidator.py"), "--data-dir", data_dir],
                capture_output=True, text=True, env={**_env(), "GITHUB_ACTIONS": "true"}, timeout=60,
            )
        lines = done.stderr.splitlines()
        annotations = [x for x in lines if x.startswith("::")]
        self.assertTrue(annotations, done.stderr)
        self._assert_contract(_records("\n".join(x for x in lines if not x.startswith("::"))))


if __name__ == "__main__":
    unittest.main()
