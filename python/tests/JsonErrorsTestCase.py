"""Tests for JsonErrors — the structured-JSON uncaught-exception hook.

Covers the pure formatter (``format_exception``), the ``sys.excepthook``
replacement (``_json_excepthook``), and ``install``.  The hook and default
hook are exercised directly (no subprocess) so the assertions stay fast and
deterministic; an end-to-end subprocess exit-code check is intentionally left
to the flow-level integration, since every ``__main__`` guard shares this one
code path.
"""
import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import JsonErrors


def _capture(exc):
    """Raise ``exc`` and return its ``(type, value, traceback)`` triple.

    Mirrors what CPython passes to ``sys.excepthook``.
    """
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001 - deliberately broad for capture
        return type(e), e, e.__traceback__


class JsonErrorsTestCase(unittest.TestCase):
    """Tests for JsonErrors.format_exception / _json_excepthook / install."""

    def setUp(self):
        # Preserve global state the tests mutate.
        self._orig_hook = sys.excepthook
        self._orig_argv0 = sys.argv[0]

    def tearDown(self):
        sys.excepthook = self._orig_hook
        sys.argv[0] = self._orig_argv0

    # ── format_exception ────────────────────────────────────────────────

    def test_format_exception_has_expected_fields(self):
        et, ev, tb = _capture(ValueError("bad value"))
        rec = json.loads(JsonErrors.format_exception(et, ev, tb))
        self.assertIs(rec["error"], True)
        self.assertEqual(rec["type"], "ValueError")
        self.assertEqual(rec["message"], "bad value")
        self.assertIn("ValueError: bad value", rec["traceback"])
        self.assertIn("timestamp", rec)
        self.assertIn("script", rec)

    def test_format_exception_is_single_physical_line(self):
        # A message with embedded newlines must not break the one-record-per-line
        # contract: json.dumps escapes them inside the string.
        et, ev, tb = _capture(RuntimeError("multi\nline\nmessage"))
        line = JsonErrors.format_exception(et, ev, tb)
        self.assertNotIn("\n", line)
        self.assertEqual(json.loads(line)["message"], "multi\nline\nmessage")

    def test_script_field_is_argv0_basename(self):
        sys.argv[0] = "/opt/nlm/python/src/DataFetcher.py"
        et, ev, tb = _capture(ValueError("x"))
        rec = json.loads(JsonErrors.format_exception(et, ev, tb))
        self.assertEqual(rec["script"], "DataFetcher.py")

    def test_chained_exception_preserved_in_traceback(self):
        try:
            try:
                raise FileNotFoundError("gene_transformed.json missing")
            except FileNotFoundError:
                raise KeyError("gene_entrez_ids")
        except BaseException as e:  # noqa: BLE001
            et, ev, tb = type(e), e, e.__traceback__
        rec = json.loads(JsonErrors.format_exception(et, ev, tb))
        self.assertEqual(rec["type"], "KeyError")
        self.assertIn("FileNotFoundError: gene_transformed.json missing", rec["traceback"])
        self.assertIn("KeyError", rec["traceback"])

    # ── install ─────────────────────────────────────────────────────────

    def test_install_sets_excepthook(self):
        JsonErrors.install()
        self.assertIs(sys.excepthook, JsonErrors._json_excepthook)

    # ── _json_excepthook ────────────────────────────────────────────────

    def test_hook_writes_one_json_line_to_stderr(self):
        et, ev, tb = _capture(ValueError("boom"))
        buf = io.StringIO()
        with patch.object(sys, "stderr", buf):
            JsonErrors._json_excepthook(et, ev, tb)
        out = buf.getvalue()
        self.assertEqual(out.count("\n"), 1)  # exactly one trailing newline
        rec = json.loads(out)
        self.assertEqual(rec["type"], "ValueError")
        self.assertEqual(rec["message"], "boom")

    def test_keyboard_interrupt_delegates_and_emits_no_json(self):
        et, ev, tb = _capture(KeyboardInterrupt())
        buf = io.StringIO()
        with patch.object(JsonErrors, "_default_excepthook") as default_hook, \
                patch.object(sys, "stderr", buf):
            JsonErrors._json_excepthook(et, ev, tb)
        default_hook.assert_called_once()
        self.assertEqual(buf.getvalue(), "")

    def test_hook_falls_back_to_default_when_formatting_fails(self):
        # Error-reporting must never mask the real error: if formatting blows
        # up, the default traceback hook is used instead.
        et, ev, tb = _capture(ValueError("x"))
        with patch.object(
            JsonErrors, "format_exception", side_effect=RuntimeError("kaboom")
        ), patch.object(JsonErrors, "_default_excepthook") as default_hook:
            JsonErrors._json_excepthook(et, ev, tb)
        default_hook.assert_called_once_with(et, ev, tb)


if __name__ == "__main__":
    unittest.main()
