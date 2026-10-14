"""Tests for the graph/analyzer sidecar tasks in flows/pipeline.py.

A failed export or import must raise. Continuing would cache or promote a dump
without its named graphs and analyzers, or build the results graph without the
ontology edge definitions.
"""

import io
import json
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import pipeline  # noqa: E402


def _response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__.return_value = resp
    return resp


class ExportGraphsAndAnalyzersTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dump_dir = Path(self._tmp.name)
        (self.dump_dir / "db1").mkdir()
        (self.dump_dir / "db2").mkdir()
        self.addCleanup(self._tmp.cleanup)

    def _run(self, urlopen):
        with patch("urllib.request.urlopen", side_effect=urlopen):
            pipeline.export_graphs_and_analyzers.fn(self.dump_dir, "pw")

    def test_success_writes_both_sidecars(self):
        def urlopen(req, timeout=None):
            if "gharial" in req.full_url:
                return _response({"graphs": [{"name": "g"}]})
            return _response({"result": [{"name": "db::a"}, {"name": "builtin"}]})

        self._run(urlopen)

        for db in ("db1", "db2"):
            self.assertEqual(
                (self.dump_dir / db / "ckn-graphs.ndjson").read_text(),
                '{"name": "g"}\n',
            )
            self.assertEqual(
                (self.dump_dir / db / "ckn-analyzers.ndjson").read_text(),
                '{"name": "db::a"}\n',
            )

    def test_graph_export_failure_raises(self):
        def urlopen(req, timeout=None):
            if "gharial" in req.full_url:
                raise OSError("connection refused")
            return _response({"result": []})

        with self.assertRaises(RuntimeError) as ctx:
            self._run(urlopen)
        self.assertIn("graphs for db1", str(ctx.exception))

    def test_analyzer_export_failure_raises(self):
        def urlopen(req, timeout=None):
            if "analyzer" in req.full_url:
                raise OSError("timed out")
            return _response({"graphs": []})

        with self.assertRaises(RuntimeError) as ctx:
            self._run(urlopen)
        self.assertIn("analyzers for db1", str(ctx.exception))

    def test_every_database_is_attempted_before_raising(self):
        calls = []

        def urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise OSError("down")

        with self.assertRaises(RuntimeError) as ctx:
            self._run(urlopen)

        self.assertIn("db1", str(ctx.exception))
        self.assertIn("db2", str(ctx.exception))
        self.assertEqual(len(calls), 4)  # graphs and analyzers, both databases


class ImportGraphsFromSidecarTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dump_dir = Path(self._tmp.name)
        db_dir = self.dump_dir / "db1"
        db_dir.mkdir()
        (db_dir / "ckn-graphs.ndjson").write_text(
            json.dumps({"name": "g", "edgeDefinitions": []}) + "\n"
        )
        self.addCleanup(self._tmp.cleanup)

    def _http_error(self, code):
        return urllib.error.HTTPError(
            "http://x", code, "err", {}, io.BytesIO(b"detail")
        )

    def _run(self, urlopen):
        with patch("urllib.request.urlopen", side_effect=urlopen):
            pipeline.import_graphs_from_sidecar.fn(self.dump_dir, "pw")

    def test_created_graph_succeeds(self):
        self._run(lambda req, timeout=None: _response({}))

    def test_existing_graph_is_not_a_failure(self):
        def urlopen(req, timeout=None):
            raise self._http_error(409)

        self._run(urlopen)

    def test_other_http_error_raises(self):
        def urlopen(req, timeout=None):
            raise self._http_error(500)

        with self.assertRaises(RuntimeError) as ctx:
            self._run(urlopen)
        self.assertIn("db1/g", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
