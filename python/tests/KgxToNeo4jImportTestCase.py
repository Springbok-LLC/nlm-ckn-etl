"""Tests for KgxToNeo4jImport, the KGX TSV -> neo4j-admin import CSV converter.

Covers:
- node_header / edge_header: id, category, subject and object become import
  fields; ':' in other column names becomes '_'; :LABEL / :TYPE appended
- node_labels: biolink:NamedThing plus one label per category, deduped, sorted
- convert: end to end on a small export -- labels, relationship types, empty
  cells left absent, values containing '"', ',' or '|' kept verbatim, and
  cells longer than the csv module's default field limit
- the command line used by src/main/docker/neo4j/Dockerfile
"""

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from KgxToNeo4jImport import (  # noqa: E402
    convert,
    edge_header,
    node_header,
    node_labels,
)

DB = "Cell-KN-Test"

NODE_COLUMNS = ["id", "category", "name", "provided_by", "OBO foundry unique label", "biolink:xref"]
EDGE_COLUMNS = ["id", "subject", "predicate", "object", "Label", "knowledge_source"]


def _write_tsv(path, columns, rows):
    """Write a TSV exactly as the KGX TSV sink does: a plain tab join, no quoting."""
    lines = ["\t".join(columns)] + ["\t".join(row) for row in rows]
    path.write_text("\n".join(lines) + "\n")


def _read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


class HeaderTestCase(unittest.TestCase):
    """Tests for node_header and edge_header."""

    def test_node_header(self):
        """id is the ID field, category a string array, and :LABEL is appended."""
        self.assertEqual(
            node_header(NODE_COLUMNS),
            [
                "id:ID",
                "category:string[]",
                "name",
                "provided_by",
                "OBO foundry unique label",
                "biolink_xref",
                ":LABEL",
            ],
        )

    def test_edge_header(self):
        """subject and object are the endpoints, and :TYPE is appended."""
        self.assertEqual(
            edge_header(EDGE_COLUMNS),
            ["id", ":START_ID", "predicate", ":END_ID", "Label", "knowledge_source", ":TYPE"],
        )

    def test_colons_never_reach_the_import_header(self):
        """A ':' would be read as a type suffix, so none survive outside the added fields."""
        columns = ["id", "category", "a:b", "c:d:e"]
        self.assertEqual(node_header(columns)[2:4], ["a_b", "c_d_e"])
        self.assertEqual(edge_header(["subject", "predicate", "object", "x:y"])[3], "x_y")


class NodeLabelsTestCase(unittest.TestCase):
    """Tests for node_labels."""

    def test_named_thing_added(self):
        self.assertEqual(node_labels("biolink:Cell"), "biolink:Cell|biolink:NamedThing")

    def test_multiple_categories_sorted_and_deduped(self):
        self.assertEqual(
            node_labels("biolink:Gene|biolink:NamedThing|biolink:Cell|biolink:Gene"),
            "biolink:Cell|biolink:Gene|biolink:NamedThing",
        )

    def test_empty_category(self):
        """An uncategorised node (or stray '|') still gets biolink:NamedThing, and no empty label."""
        self.assertEqual(node_labels(""), "biolink:NamedThing")
        self.assertEqual(node_labels("|biolink:Cell|"), "biolink:Cell|biolink:NamedThing")


class ConvertTestCase(unittest.TestCase):
    """Tests for convert, on a small export written as the KGX TSV sink writes it."""

    LONG = "x" * (256 * 1024)

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.src = Path(tmp.name) / "kgx"
        self.dst = Path(tmp.name) / "import"
        self.src.mkdir()
        self.dst.mkdir()
        _write_tsv(
            self.src / f"{DB}_nodes.tsv",
            NODE_COLUMNS,
            [
                ["CL:0000001", "biolink:Cell", "cell, primitive", "infores:cl", "", ""],
                ["GS:A", "biolink:Gene|biolink:GeneSet", "", "", "", "HGNC:1|HGNC:2"],
                ["X:1", "", '"quoted" start', "", '"unbalanced quote', self.LONG],
            ],
        )
        _write_tsv(
            self.src / f"{DB}_edges.tsv",
            EDGE_COLUMNS,
            [
                ["e1", "CL:0000001", "CL-GS", "GS:A", "HAS_MARKER", "infores:nlm-ckn"],
                ["e2", "GS:A", "GS-X", "X:1", "", ""],
            ],
        )
        convert(self.src, self.dst, DB)
        self.nodes = _read_csv(self.dst / "nodes.csv")
        self.edges = _read_csv(self.dst / "edges.csv")

    def test_writes_only_the_two_import_files(self):
        self.assertEqual(sorted(p.name for p in self.dst.iterdir()), ["edges.csv", "nodes.csv"])

    def test_nodes(self):
        """One row per node, the TSV cells unchanged, labels appended."""
        self.assertEqual(self.nodes[0], node_header(NODE_COLUMNS))
        self.assertEqual(
            self.nodes[1:],
            [
                ["CL:0000001", "biolink:Cell", "cell, primitive", "infores:cl", "", "",
                 "biolink:Cell|biolink:NamedThing"],
                ["GS:A", "biolink:Gene|biolink:GeneSet", "", "", "", "HGNC:1|HGNC:2",
                 "biolink:Gene|biolink:GeneSet|biolink:NamedThing"],
                ["X:1", "", '"quoted" start', "", '"unbalanced quote', self.LONG,
                 "biolink:NamedThing"],
            ],
        )

    def test_edges(self):
        """One row per edge; the relationship type is the predicate."""
        self.assertEqual(self.edges[0], edge_header(EDGE_COLUMNS))
        self.assertEqual(
            self.edges[1:],
            [
                ["e1", "CL:0000001", "CL-GS", "GS:A", "HAS_MARKER", "infores:nlm-ckn", "CL-GS"],
                ["e2", "GS:A", "GS-X", "X:1", "", "", "GS-X"],
            ],
        )

    def test_empty_cells_written_unquoted(self):
        """neo4j-admin import skips an unquoted empty field but stores "" as an empty string."""
        lines = (self.dst / "edges.csv").read_text().splitlines()
        self.assertEqual(lines[2], "e2,GS:A,GS-X,X:1,,,GS-X")

    def test_quotes_are_data_not_csv_quoting(self):
        """KGX never quotes, so a leading '"' is kept rather than parsed away.

        Read with csv's default quoting, '"quoted" start' would lose its quotes and
        '"unbalanced quote' would swallow the cells after it.
        """
        self.assertEqual(len(self.nodes), 4)
        self.assertEqual(self.nodes[3][2], '"quoted" start')
        self.assertEqual(self.nodes[3][4], '"unbalanced quote')


class CommandLineTestCase(unittest.TestCase):
    """The src_dir dst_dir db_name command line the Dockerfile runs."""

    SCRIPT = _SRC / "KgxToNeo4jImport.py"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_converts(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = Path(tmp) / "kgx", Path(tmp) / "import"
            src.mkdir()
            dst.mkdir()
            _write_tsv(src / f"{DB}_nodes.tsv", ["id", "category"], [["A:1", "biolink:Cell"]])
            _write_tsv(src / f"{DB}_edges.tsv", ["subject", "predicate", "object"], [["A:1", "P", "A:1"]])
            result = self._run(str(src), str(dst), DB)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                (dst / "nodes.csv").read_text().splitlines(),
                ["id:ID,category:string[],:LABEL", "A:1,biolink:Cell,biolink:Cell|biolink:NamedThing"],
            )
            self.assertEqual(
                (dst / "edges.csv").read_text().splitlines(),
                [":START_ID,predicate,:END_ID,:TYPE", "A:1,P,A:1,P"],
            )

    def test_missing_export_fails(self):
        """A missing TSV (e.g. a wrong db_name) is an error, not an empty import."""
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, tmp, "No-Such-Db")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No-Such-Db_nodes.tsv", result.stderr)

    def test_usage_error(self):
        result = self._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
