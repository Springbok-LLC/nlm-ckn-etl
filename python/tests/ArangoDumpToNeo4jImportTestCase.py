"""Tests for ArangoDumpToNeo4jImport, the arangodump -> neo4j-admin import CSV converter.

Covers:
- node_id: _id with its first '/' replaced by ':'
- edge_provenance: the Biolink slots export_kgx derives from Source
- read_dump: collections and their types; rejects the envelope and VPack
  formats, and missing or unexpected data files
- convert: end to end on a small dump -- one CSV per collection, labels and
  category, the document's own id kept as oboInOwl_id, a field that is a list
  in any document becoming a string[], '|' inside list elements, newlines,
  quotes and empty strings kept, absent fields left absent, parallel edges
  kept, relationship types, provenance, import.args and counts.env
- values the importer cannot represent, and name collisions, fail loudly
- the command line used by src/main/docker/neo4j/Dockerfile
"""

import csv
import gzip
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from ArangoDumpToNeo4jImport import (  # noqa: E402
    Collection,
    convert,
    edge_provenance,
    node_id,
    read_dump,
    write_collection,
)

US = "\x1f"
NT = "biolink:NamedThing"


def _write_dump(path, collections, meta=None):
    """Write a plain-format arangodump directory: {name: (type, [documents])}."""
    path.mkdir(parents=True, exist_ok=True)
    meta = {"database": "Test", "useEnvelope": False, "useVPack": False} if meta is None else meta
    (path / "dump.json").write_text(json.dumps(meta))
    (path / "ENCRYPTION").write_text("none")
    for i, (name, (kind, docs)) in enumerate(collections.items()):
        prefix = f"{name}_{i:032x}"
        (path / f"{prefix}.structure.json").write_text(
            json.dumps({"indexes": [], "parameters": {"name": name, "type": kind}})
        )
        with gzip.open(path / f"{prefix}.data.json.gz", "wt") as f:
            f.writelines(json.dumps(doc) + "\n" for doc in docs)
    return path


def _node(collection, key, **fields):
    return {"_key": key, "_id": f"{collection}/{key}", "_rev": "_rev1", **fields}


def _edge(collection, key, src, dst, **fields):
    return {**_node(collection, key), "_from": src, "_to": dst, **fields}


def _read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


class NodeIdTestCase(unittest.TestCase):
    def test_first_slash_only(self):
        self.assertEqual(node_id("HP/0012871"), "HP:0012871")
        self.assertEqual(node_id("GS/a/b"), "GS:a/b")


class EdgeProvenanceTestCase(unittest.TestCase):
    """edge_provenance sets the slots as export_kgx does."""

    def test_known_source(self):
        self.assertEqual(
            edge_provenance("MONDO"),
            {
                "aggregator_knowledge_source": "infores:nlm-ckn",
                "primary_knowledge_source": "infores:mondo",
                "knowledge_source": "infores:mondo",
                "supporting_data_source": None,
            },
        )

    def test_no_curie(self):
        """CELLxGENE has no CURIE: NLM-CKN is the knowledge source, with no primary source."""
        for source in ("CELLxGENE", "", None, []):
            with self.subTest(source=source):
                slots = edge_provenance(source)
                self.assertIsNone(slots["primary_knowledge_source"])
                self.assertEqual(slots["knowledge_source"], "infores:nlm-ckn")
                self.assertIsNone(slots["supporting_data_source"])

    def test_list_source(self):
        """The first CURIE is primary; the rest, deduplicated, are supporting."""
        slots = edge_provenance(["CELLxGENE", "MONDO", "HP", "mondo", "UBERON"])
        self.assertEqual(slots["primary_knowledge_source"], "infores:mondo")
        self.assertEqual(slots["knowledge_source"], "infores:mondo")
        self.assertEqual(slots["supporting_data_source"], ["infores:hpo", "infores:uberon"])


class ReadDumpTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def test_collections(self):
        dump = _write_dump(
            self.tmp / "db",
            {"HP-HP": (3, []), "HP": (2, []), "_system": (2, [])},
        )
        self.assertEqual(
            [(c.name, c.is_edge) for c in read_dump(dump)],
            [("HP", False), ("HP-HP", True)],
        )

    def test_rejects_envelope_and_vpack(self):
        for meta in (
            {"useEnvelope": True, "useVPack": False},
            {"useEnvelope": False, "useVPack": True},
            {"useEnvelope": False},
        ):
            with self.subTest(meta=meta):
                dump = _write_dump(self.tmp / "db", {"HP": (2, [])}, meta=meta)
                with self.assertRaisesRegex(ValueError, "only false is supported"):
                    read_dump(dump)

    def test_rejects_missing_data_file(self):
        dump = _write_dump(self.tmp / "db", {"HP": (2, [])})
        next(dump.glob("*.data.json.gz")).unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            read_dump(dump)

    def test_rejects_unexpected_data_file(self):
        """E.g. a dump split into several files, or written uncompressed."""
        dump = _write_dump(self.tmp / "db", {"HP": (2, [])})
        (dump / "HP_x.data.json").write_text("")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            read_dump(dump)

    def test_rejects_unknown_type(self):
        dump = _write_dump(self.tmp / "db", {"HP": (4, [])})
        with self.assertRaisesRegex(ValueError, "unknown collection type"):
            read_dump(dump)


class ConvertTestCase(unittest.TestCase):
    """convert, end to end on a small dump."""

    COMMENT = '"vaginal" refers to the tunica vaginalis of the testis, not to the vagina.'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        dump = _write_dump(
            Path(tmp.name) / "Cell-KN-Test",
            {
                "HP": (2, [
                    _node("HP", "0012871", id="HP:0012871", comment=self.COMMENT,
                          hasExactSynonym="Vaginal varicocele", label="Varicocele"),
                    _node("HP", "1", hasExactSynonym=["a|b", "c"], definition="one\ntwo",
                          **{"oboInOwl:hasOBONamespace": "x"}),
                    _node("HP", "2", label=""),
                ]),
                "CL": (2, [
                    _node("CL", "0020036", id="CL:9900001", label="oRGC1"),
                    _node("CL", "0020041", id="CL:9900001", label="oRGC4"),
                ]),
                "CL-CL": (3, [
                    _edge("CL-CL", "0020041-RO:0002103-0020036", "CL/0020041", "CL/0020036",
                          Label="SYNAPSED_BY", Source="CL"),
                    _edge("CL-CL", "0020041-RO:0002120-0020036", "CL/0020041", "CL/0020036",
                          Label="SYNAPSED_TO", Source="CELLxGENE"),
                ]),
                "CL-HP": (3, [
                    _edge("CL-HP", "k", "CL/0020036", "HP/1", Label="HAS_PHENOTYPE",
                          Source=["MONDO", "HP"], Score="0.5"),
                ]),
                "HP-HP": (3, []),
            },
        )
        self.dst = Path(tmp.name) / "import"
        self.totals = convert(dump, self.dst)
        self.dst = self.dst.resolve()

    def test_files(self):
        self.assertEqual(
            sorted(str(p.relative_to(self.dst)) for p in self.dst.rglob("*") if p.is_file()),
            [
                "counts.env",
                "import.args",
                "nodes/CL.csv",
                "nodes/HP.csv",
                "relationships/CL-CL.csv",
                "relationships/CL-HP.csv",
                "relationships/HP-HP.csv",
            ],
        )

    def test_import_args(self):
        """The importer's options, then one --nodes / --relationships per file."""
        self.assertEqual(
            (self.dst / "import.args").read_text().splitlines(),
            [
                "--array-delimiter=U+001F",
                "--multiline-fields=true",
                f"--nodes={self.dst}/nodes/CL.csv",
                f"--relationships={self.dst}/relationships/CL-CL.csv",
                f"--relationships={self.dst}/relationships/CL-HP.csv",
                f"--nodes={self.dst}/nodes/HP.csv",
                f"--relationships={self.dst}/relationships/HP-HP.csv",
            ],
        )

    def test_counts(self):
        self.assertEqual(self.totals, (5, 3))
        self.assertEqual((self.dst / "counts.env").read_text(), "NODES=5\nRELATIONSHIPS=3\n")

    def test_node_header(self):
        """Generated columns first; the document's id becomes oboInOwl_id; ':' becomes '_';
        a field that is a list in any document is a string[]."""
        self.assertEqual(
            _read_csv(self.dst / "nodes/HP.csv")[0],
            [
                "id:ID",
                ":LABEL",
                "category:string[]",
                "provided_by",
                "comment",
                "definition",
                "hasExactSynonym:string[]",
                "label",
                "oboInOwl_hasOBONamespace",
                "oboInOwl_id",
            ],
        )

    def test_node_rows(self):
        labels = f"HP{US}{NT}"
        self.assertEqual(
            _read_csv(self.dst / "nodes/HP.csv")[1:],
            [
                ["HP:0012871", labels, labels, "infores:nlm-ckn", self.COMMENT, "",
                 "Vaginal varicocele", "Varicocele", "", "HP:0012871"],
                ["HP:1", labels, labels, "infores:nlm-ckn", "", "one\ntwo",
                 f"a|b{US}c", "", "x", ""],
                ["HP:2", labels, labels, "infores:nlm-ckn", "", "", "", "", "", ""],
            ],
        )

    def test_empty_string_quoted_absent_field_not(self):
        """The importer stores "" as an empty string and skips an unquoted empty field."""
        last = (self.dst / "nodes/HP.csv").read_text().splitlines()[-1]
        self.assertEqual(last, f'"HP:2","HP{US}{NT}","HP{US}{NT}","infores:nlm-ckn",,,,"",,')

    def test_node_ids_come_from_arango_id(self):
        """Two nodes sharing a stale id annotation stay two nodes."""
        rows = _read_csv(self.dst / "nodes/CL.csv")
        self.assertEqual(rows[0][-2:], ["label", "oboInOwl_id"])
        self.assertEqual(
            [(r[0], r[-2], r[-1]) for r in rows[1:]],
            [("CL:0020036", "oRGC1", "CL:9900001"), ("CL:0020041", "oRGC4", "CL:9900001")],
        )

    def test_relationships(self):
        """Parallel edges that differ only by Label are both kept; the type is the collection."""
        rows = _read_csv(self.dst / "relationships/CL-CL.csv")
        self.assertEqual(
            rows[0],
            [":START_ID", ":END_ID", ":TYPE", "id", "aggregator_knowledge_source",
             "primary_knowledge_source", "knowledge_source", "supporting_data_source:string[]",
             "Label", "Source"],
        )
        self.assertEqual(
            rows[1:],
            [
                ["CL:0020041", "CL:0020036", "CL-CL", "CL-CL/0020041-RO:0002103-0020036",
                 "infores:nlm-ckn", "infores:cl", "infores:cl", "", "SYNAPSED_BY", "CL"],
                ["CL:0020041", "CL:0020036", "CL-CL", "CL-CL/0020041-RO:0002120-0020036",
                 "infores:nlm-ckn", "", "infores:nlm-ckn", "", "SYNAPSED_TO", "CELLxGENE"],
            ],
        )

    def test_list_source(self):
        rows = _read_csv(self.dst / "relationships/CL-HP.csv")
        self.assertEqual(rows[0][8:], ["Label", "Score", "Source:string[]"])
        self.assertEqual(
            rows[1],
            ["CL:0020036", "HP:1", "CL-HP", "CL-HP/k", "infores:nlm-ckn", "infores:mondo",
             "infores:mondo", "infores:hpo", "HAS_PHENOTYPE", "0.5", f"MONDO{US}HP"],
        )

    def test_empty_collection(self):
        """An empty collection still gets a file, with the generated columns only."""
        self.assertEqual(len(_read_csv(self.dst / "relationships/HP-HP.csv")), 1)


class RejectTestCase(unittest.TestCase):
    """What the importer cannot represent, or would silently merge, fails loudly."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def _write(self, name, is_edge, docs):
        data = self.tmp / f"{name}.data.json.gz"
        with gzip.open(data, "wt") as f:
            f.writelines(json.dumps(doc) + "\n" for doc in docs)
        return write_collection(Collection(name, is_edge, data), self.tmp / f"{name}.csv")

    def test_name_collisions(self):
        for is_edge, fields in (
            (False, {"category": "x"}),
            (False, {"a:b": "x", "a_b": "y"}),
            (False, {"oboInOwl_id": "x", "id": "y"}),
            (True, {"id": "x"}),
            (True, {"knowledge_source": "x"}),
        ):
            with self.subTest(fields=fields):
                doc = _edge("E", "1", "A/1", "A/2", **fields) if is_edge else _node("A", "1", **fields)
                with self.assertRaisesRegex(ValueError, "already taken"):
                    self._write("E" if is_edge else "A", is_edge, [doc])

    def test_unrepresentable_values(self):
        for value, message in (
            (1, "expected a string"),
            (None, "expected a string"),
            ({"a": "b"}, "expected a string"),
            ([], "empty list"),
            (["a", ""], "empty list element"),
            ([f"a{US}b"], "array delimiter"),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, f"A document A/1: .*{message}"):
                    self._write("A", False, [_node("A", "1", f=value)])

    def test_scalar_empty_string_in_a_list_field(self):
        """[""] would import as absent, so a "" in a string[] field fails too."""
        with self.assertRaisesRegex(ValueError, "A/2: .*empty list element"):
            self._write("A", False, [_node("A", "1", f=["x"]), _node("A", "2", f="")])


class CommandLineTestCase(unittest.TestCase):
    """The dump_dir dst_dir command line the Dockerfile runs."""

    SCRIPT = _SRC / "ArangoDumpToNeo4jImport.py"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_converts(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = _write_dump(
                Path(tmp) / "db",
                {"A": (2, [_node("A", "1")]), "A-A": (3, [_edge("A-A", "1", "A/1", "A/1")])},
            )
            result = self._run(str(dump), str(Path(tmp) / "import"))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("1 nodes and 1 relationships", result.stdout)
            self.assertEqual(
                (Path(tmp) / "import/nodes/A.csv").read_text(),
                '"id:ID",":LABEL","category:string[]","provided_by"\n'
                f'"A:1","A{US}{NT}","A{US}{NT}","infores:nlm-ckn"\n',
            )

    def test_missing_dump_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run(tmp, str(Path(tmp) / "import"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("dump.json", result.stderr)

    def test_usage_error(self):
        result = self._run()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)


if __name__ == "__main__":
    unittest.main()
