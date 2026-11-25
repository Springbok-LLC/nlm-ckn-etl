"""Every event the code emits has a row in docs/logging-conventions.md.

The vocabulary is a contract with whoever reads the logs, so a new event name
goes into the doc's tables in the PR that adds it.  This reads the event names
out of the Python and Java sources and fails on one the doc does not list.  It
checks emitted against documented only: the tables' first column also holds
field values (``reason`` codes and the like), so a documented name that no code
emits is not an error.
"""

import ast
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
_DOC = _REPO / "docs" / "logging-conventions.md"
_PY_SRC = _REPO / "python" / "src"
_JAVA_SRC = _REPO / "src" / "main" / "java"

# conftest.py patches LoaderUtilities for the session, so src must be importable.
sys.path.insert(0, str(_PY_SRC))

_NAME = re.compile(r"[a-z][a-z0-9_]*$")
_LEVELS = {"debug", "info", "warning", "error", "critical", "exception"}

# Call sites whose event name is not a literal, so it cannot be checked here.
# Each is a (file, "function: call") pair, so a second dynamic call in the same
# file is not let through by the first; a new one has to be added on purpose,
# with its names documented by hand.
_DYNAMIC_PYTHON = {
    ("TupleWriterUtilities.py", "log: _events.warning(event)"),
}


def documented_events(text: str) -> set[str]:
    """Names in the first column of the doc's tables.

    A row can document several events at once (``a_failed``, ``b_failed``), so
    every backticked name in the first cell counts.
    """
    names: set[str] = set()
    for line in text.splitlines():
        if line.startswith("|"):
            first_cell = line.split("|")[1]
            names.update(re.findall(r"`([a-z][a-z0-9_]*)`", first_cell))
    return names


def _loggers(tree: ast.AST) -> set[str]:
    """Names bound to ``structlog.get_logger(...)``, and aliases of their methods."""
    loggers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            func = node.value.func
            if isinstance(func, ast.Attribute) and func.attr == "get_logger":
                loggers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return loggers


def _aliases(tree: ast.AST, loggers: set[str]) -> set[str]:
    """Names assigned a logger's method, as in ``log = _events.info if n else _events.warning``."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        uses_method = any(
            isinstance(sub, ast.Attribute)
            and sub.attr in _LEVELS
            and isinstance(sub.value, ast.Name)
            and sub.value.id in loggers
            for sub in ast.walk(node.value)
        )
        if uses_method:
            aliases.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return aliases


def _calls(tree: ast.AST):
    """Every call in ``tree`` with the name of the function it sits in."""

    def walk(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = node.name
        if isinstance(node, ast.Call):
            yield node, scope
        for child in ast.iter_child_nodes(node):
            yield from walk(child, scope)

    yield from walk(tree, "<module>")


def python_events(source: str) -> tuple[set[str], list[str]]:
    """Event names logged in ``source``, and the calls whose name is not a literal.

    A call that is not a literal is reported as ``"function: call"``, naming the
    function it is in and its event argument.
    """
    tree = ast.parse(source)
    loggers = _loggers(tree)
    aliases = _aliases(tree, loggers)
    names: set[str] = set()
    dynamic: list[str] = []
    for node, scope in _calls(tree):
        func = node.func
        is_method = (
            isinstance(func, ast.Attribute)
            and func.attr in _LEVELS
            and isinstance(func.value, ast.Name)
            and func.value.id in loggers
        )
        if not (is_method or (isinstance(func, ast.Name) and func.id in aliases)):
            continue
        first = node.args[0] if node.args else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
        else:
            arg = ast.unparse(first) if first is not None else ""
            dynamic.append(f"{scope}: {ast.unparse(func)}({arg})")
    return names, dynamic


# A string literal, a character literal, or a comment: whichever starts first.
_JAVA_SKIP = re.compile(
    r'"(?:\\.|[^"\\\n])*"' r"|'(?:\\.|[^'\\\n])*'" r"|//[^\n]*|/\*.*?\*/",
    re.DOTALL,
)


def _strip_java_comments(source: str) -> str:
    """``source`` without comments; text inside string literals is kept."""
    return _JAVA_SKIP.sub(lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", source)


# The event argument is a whole literal (a ``,`` or ``)`` follows it), or else it
# is reported as not a literal: ``"a" + suffix`` is not the event ``a``.
_JAVA_CALL = re.compile(
    r"\bEvents\s*\.\s*(?:info|warn|error|debug)\s*\(\s*\w+\s*,\s*"
    r"(\"[^\"]*\"(?=\s*[,)])|[^,)]+)"
    r"|\bEvents\s*\.\s*failure\s*\(\s*\w+\s*,\s*"
    r"(\"[^\"]*\"(?=\s*[,)])|[^,)]+)"
)


def java_events(source: str) -> tuple[set[str], list[str]]:
    """Event names logged in ``source``, and the calls whose name is not a literal."""
    names: set[str] = set()
    dynamic: list[str] = []
    for match in _JAVA_CALL.finditer(_strip_java_comments(source)):
        arg = (match.group(1) or match.group(2)).strip()
        if re.fullmatch(r'"[^"]*"', arg):
            names.add(arg[1:-1])
        else:
            dynamic.append(arg)
    return names, dynamic


def _scan_python() -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    by_event: dict[str, set[str]] = {}
    dynamic: list[tuple[str, str]] = []
    for path in sorted(_PY_SRC.rglob("*.py")):
        if "_deprecated" in path.parts:
            continue
        names, dyn = python_events(path.read_text())
        for name in names:
            by_event.setdefault(name, set()).add(path.name)
        dynamic.extend((path.name, call) for call in dyn)
    return by_event, dynamic


def _scan_java() -> tuple[dict[str, set[str]], list[tuple[str, str]]]:
    by_event: dict[str, set[str]] = {}
    dynamic: list[tuple[str, str]] = []
    for path in sorted(_JAVA_SRC.rglob("*.java")):
        if path.name == "Events.java":
            continue
        names, dyn = java_events(path.read_text())
        for name in names:
            by_event.setdefault(name, set()).add(path.name)
        dynamic.extend((path.name, call) for call in dyn)
    return by_event, dynamic


class VocabularyTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.documented = documented_events(_DOC.read_text())
        cls.python, cls.python_dynamic = _scan_python()
        cls.java, cls.java_dynamic = _scan_java()

    def _undocumented(self, emitted: dict[str, set[str]]) -> list[str]:
        return sorted(
            f"{name} ({', '.join(sorted(files))})"
            for name, files in emitted.items()
            if name not in self.documented
        )

    def test_python_events_are_documented(self):
        self.assertEqual(
            self._undocumented(self.python),
            [],
            "add a row to docs/logging-conventions.md for each event",
        )

    def test_java_events_are_documented(self):
        self.assertEqual(
            self._undocumented(self.java),
            [],
            "add a row to docs/logging-conventions.md for each event",
        )

    def test_event_names_are_snake_case(self):
        for emitted in (self.python, self.java):
            for name in emitted:
                self.assertRegex(name, _NAME, f"{name!r} is not snake_case")

    def test_dynamic_python_calls_are_known(self):
        self.assertEqual(
            sorted(set(self.python_dynamic) - _DYNAMIC_PYTHON),
            [],
            "an event name that is not a literal cannot be checked; "
            "use a literal, or list the call in _DYNAMIC_PYTHON and document its names",
        )

    def test_java_event_names_are_literals(self):
        self.assertEqual(self.java_dynamic, [])

    def test_the_scan_finds_the_events_it_should(self):
        # Guards against the scan going blind and the tests above passing empty.
        self.assertGreater(len(self.python), 80)
        self.assertGreater(len(self.java), 40)
        for name in ("phase_finished", "run_failed", "fetch_source_status"):
            self.assertIn(name, self.python)
        for name in ("run_started", "run_finished", "vertices_inserted"):
            self.assertIn(name, self.java)


class ExtractionTestCase(unittest.TestCase):
    def test_python_literal_and_alias_calls(self):
        source = (
            "import structlog\n"
            "_events = structlog.get_logger(__name__)\n"
            "def f(ok):\n"
            "    _events.info('a_started', x=1)\n"
            "    log = _events.info if ok else _events.warning\n"
            "    log('b_done')\n"
            "    other.info('not_a_logger')\n"
        )
        self.assertEqual(python_events(source), ({"a_started", "b_done"}, []))

    def test_python_dynamic_call_is_reported(self):
        source = (
            "import structlog\n"
            "_events = structlog.get_logger(__name__)\n"
            "_events.warning(event, x=1)\n"
        )
        self.assertEqual(python_events(source), (set(), ["<module>: _events.warning(event)"]))

    def test_python_dynamic_call_is_identified_by_function_and_argument(self):
        source = (
            "import structlog\n"
            "_events = structlog.get_logger(__name__)\n"
            "def log():\n"
            "    _events.warning(event)\n"
            "def other():\n"
            "    _events.warning(event)\n"
            "    _events.warning(second)\n"
        )
        _, dynamic = python_events(source)
        self.assertEqual(
            dynamic,
            [
                "log: _events.warning(event)",
                "other: _events.warning(event)",
                "other: _events.warning(second)",
            ],
        )
        # Only the one on the allowlist would be let through.
        allowed = {("x.py", "log: _events.warning(event)")}
        self.assertEqual(
            sorted({("x.py", d) for d in dynamic} - allowed),
            [
                ("x.py", "other: _events.warning(event)"),
                ("x.py", "other: _events.warning(second)"),
            ],
        )

    def test_java_literal_multiline_and_failure_calls(self):
        source = (
            'Events.info(LOG, "a_started", "k", v);\n'
            "Events.warn(\n"
            '        LOG,\n'
            '        "b_skipped", "k", v);\n'
            'Events.failure(LOG, "c_failed", e, "k", v);\n'
        )
        self.assertEqual(java_events(source), ({"a_started", "b_skipped", "c_failed"}, []))

    def test_java_concatenated_name_is_not_a_literal(self):
        source = 'Events.info(LOG, "run_started" + suffix, "k", v);'
        names, dynamic = java_events(source)
        self.assertEqual(names, set())
        self.assertEqual(len(dynamic), 1)

    def test_java_comments_are_ignored(self):
        source = (
            '// Events.info(LOG, "line_comment");\n'
            '/* Events.warn(LOG, "block_comment"); */\n'
            '/*\n * Events.warn(LOG, "multiline_comment");\n */\n'
            'String s = "// not a comment"; Events.info(LOG, "kept", "k", s);\n'
            "Events.warn(\n    LOG,\n    \"multiline_call\");\n"
        )
        self.assertEqual(java_events(source), ({"kept", "multiline_call"}, []))

    def test_java_non_literal_name_is_reported(self):
        self.assertEqual(java_events("Events.info(LOG, name, 1);"), (set(), ["name"]))

    def test_documented_events_reads_the_first_column(self):
        text = (
            "| `a_event` | INFO | `x` |\n"
            "| `b_failed`, `c_failed` | ERROR | `y` |\n"
            "| plain | row | `not_an_event` |\n"
            "prose `d_event`\n"
        )
        self.assertEqual(documented_events(text), {"a_event", "b_failed", "c_failed"})


if __name__ == "__main__":
    unittest.main()
