"""Structured-JSON reporting of uncaught exceptions for standalone scripts.

Every Python script in this package is launched as its own subprocess (see
``flows/_common._run_python_script``).  When one raises an uncaught exception,
CPython's default hook writes a multi-line traceback to ``stderr`` — readable
by a human, but awkward for the log aggregators (CloudWatch, Prefect) that
collect the ETL's output.

Calling :func:`install` from a script's ``if __name__ == "__main__":`` guard
replaces ``sys.excepthook`` so that any exception which escapes ``main()`` is
emitted as a single-line JSON object on ``stderr`` instead, e.g.::

    {"error": true, "timestamp": "2026-07-23T18:30:00.123456+00:00",
     "script": "GeneTupleWriter.py", "type": "FileNotFoundError",
     "message": "gene_transformed.json missing", "traceback": "Traceback ..."}

The process still exits non-zero (CPython uses status 1 after the hook runs),
so ``subprocess.run(..., check=True)`` continues to raise ``CalledProcessError``
exactly as before — only the *format* of the error text changes.

Design notes
------------
* Zero third-party dependencies, so importing this module never drags heavy
  scientific packages into a script that does not otherwise need them.
* ``KeyboardInterrupt`` (Ctrl-C) is passed through to the default hook so an
  interactive interrupt still exits cleanly with the conventional status 130
  and is not misreported as a pipeline error.
* The hook is defensive: if JSON serialisation itself fails, it falls back to
  the default traceback rather than masking the original error.
"""

import json
import sys
import traceback as _traceback
from datetime import datetime, timezone
from pathlib import Path

__all__ = ["install", "format_exception"]

# Preserved so the fallback path (and any caller that wants the original
# behaviour) can still reach CPython's default handler.
_default_excepthook = sys.excepthook


def format_exception(exc_type, exc_value, exc_tb) -> str:
    """Return a one-line JSON string describing an exception.

    Exposed separately from the hook so callers can format an exception they
    caught themselves (e.g. inside a ``try/except`` that logs and re-raises).
    """
    payload = {
        "error": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        # sys.argv[0] is the script path; fall back to "<unknown>" if unset.
        "script": Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "<unknown>",
        "type": getattr(exc_type, "__name__", str(exc_type)),
        "message": str(exc_value),
        "traceback": "".join(
            _traceback.format_exception(exc_type, exc_value, exc_tb)
        ),
    }
    # Compact separators keep the whole record on a single physical line so
    # each log event is one parseable line; the traceback's newlines are
    # escaped inside the JSON string by json.dumps.
    return json.dumps(payload, separators=(",", ":"))


def _json_excepthook(exc_type, exc_value, exc_tb) -> None:
    """``sys.excepthook`` replacement that emits JSON to stderr."""
    # Let Ctrl-C behave normally (default hook prints nothing and exits 130).
    if issubclass(exc_type, KeyboardInterrupt):
        _default_excepthook(exc_type, exc_value, exc_tb)
        return
    try:
        print(format_exception(exc_type, exc_value, exc_tb), file=sys.stderr, flush=True)
    except Exception:
        # Never let error-reporting mask the real error: fall back to the
        # default traceback if anything above goes wrong.
        _default_excepthook(exc_type, exc_value, exc_tb)


def install() -> None:
    """Install the JSON exception hook as ``sys.excepthook``.

    Idempotent — calling it more than once is harmless.  Intended to be the
    first statement inside a script's ``if __name__ == "__main__":`` guard.
    """
    sys.excepthook = _json_excepthook
