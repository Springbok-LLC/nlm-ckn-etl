"""configure_logging emits single-line JSON with the core schema fields."""

import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

import structlog  # noqa: E402

import logging_setup  # noqa: E402

_ENV_KEYS = ("CORRELATION_ID", "PHASE", "GIT_SHA", "LOG_FORMAT")


class LoggingSetupTestCase(unittest.TestCase):
    def setUp(self):
        self._root_handlers = logging.getLogger().handlers[:]
        self._root_level = logging.getLogger().level

    def tearDown(self):
        root = logging.getLogger()
        root.handlers = self._root_handlers
        root.setLevel(self._root_level)
        structlog.contextvars.clear_contextvars()
        structlog.reset_defaults()
        for name in logging_setup._QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.NOTSET)

    def _emit(self, env=None, emit=None):
        """Configure under ``env`` and return the lines written to the stream."""
        clean = {k: v for k, v in os.environ.items() if k not in _ENV_KEYS}
        stream = io.StringIO()
        with patch.dict(os.environ, {**clean, **(env or {})}, clear=True):
            logging_setup.configure_logging("etl-test", stream=stream)
            if emit is None:
                structlog.get_logger("my.module").info("event happened", records_out=3)
            else:
                emit()
        return stream.getvalue().splitlines()

    def test_core_fields(self):
        (line,) = self._emit(
            {"CORRELATION_ID": "run-1", "GIT_SHA": "abc123"}
        )
        rec = json.loads(line)
        self.assertEqual(rec["message"], "event happened")
        self.assertEqual(rec["level"], "INFO")
        self.assertEqual(rec["service"], "etl-test")
        self.assertEqual(rec["correlation_id"], "run-1")
        self.assertEqual(rec["logger"], "my.module")
        self.assertEqual(rec["release"], "abc123")
        self.assertEqual(rec["records_out"], 3)
        self.assertRegex(rec["timestamp"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertNotIn("event", rec)
        self.assertNotIn("phase", rec)

    def test_level_is_uppercase(self):
        lines = self._emit(
            emit=lambda: (
                structlog.get_logger("m").warning("w"),
                structlog.get_logger("m").error("e"),
            )
        )
        self.assertEqual([json.loads(x)["level"] for x in lines], ["WARN", "ERROR"])

    def test_correlation_id_falls_back_to_uuid(self):
        (a,) = self._emit()
        (b,) = self._emit()
        cid_a = json.loads(a)["correlation_id"]
        cid_b = json.loads(b)["correlation_id"]
        self.assertNotIn(cid_a, ("", "unknown"))
        self.assertEqual(len(cid_a), 36)
        self.assertNotEqual(cid_a, cid_b)

    def test_release_defaults_to_unknown(self):
        (line,) = self._emit()
        self.assertEqual(json.loads(line)["release"], "unknown")

    def test_phase_bound_from_environment(self):
        (line,) = self._emit({"PHASE": "fetch"})
        self.assertEqual(json.loads(line)["phase"], "fetch")

    def test_stdlib_logger_gets_core_fields(self):
        (line,) = self._emit(
            {"CORRELATION_ID": "run-2"},
            emit=lambda: logging.getLogger("prefect.flow").error("boom"),
        )
        rec = json.loads(line)
        self.assertEqual(rec["message"], "boom")
        self.assertEqual(rec["level"], "ERROR")
        self.assertEqual(rec["logger"], "prefect.flow")
        self.assertEqual(rec["correlation_id"], "run-2")
        self.assertEqual(rec["service"], "etl-test")

    def test_exception_is_a_single_line_field(self):
        def emit():
            try:
                raise ValueError("bad")
            except ValueError:
                structlog.get_logger("m").exception("failed")

        (line,) = self._emit(emit=emit)
        rec = json.loads(line)
        self.assertIn("ValueError: bad", rec["exception"])

    def test_console_format_is_not_json(self):
        (line,) = self._emit({"LOG_FORMAT": "console"})
        self.assertIn("event happened", line)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(line)

    def test_http_client_chatter_is_quiet(self):
        def emit():
            logging.getLogger("httpx").info("HTTP Request: GET http://x")
            logging.getLogger("httpx").warning("slow")

        lines = self._emit(emit=emit)
        self.assertEqual([json.loads(x)["message"] for x in lines], ["slow"])

    def test_reconfigure_does_not_duplicate_lines(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"CORRELATION_ID": "r"}):
            logging_setup.configure_logging("etl-test", stream=stream)
            logging_setup.configure_logging("etl-test", stream=stream)
            structlog.get_logger("m").info("once")
        self.assertEqual(len(stream.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
