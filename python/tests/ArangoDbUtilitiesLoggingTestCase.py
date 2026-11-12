"""ArangoDbUtilities.py logs events, against mocked ArangoDB handles."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ArangoDbUtilities as adb  # noqa: E402

COLLECTION_MAPS = (
    Path(__file__).resolve().parents[2] / "data" / "nlm-ckn-collection-maps.json"
)


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _names(logs):
    return [e["event"] for e in logs]


def _named(name):
    mock = MagicMock()
    mock.name = name
    return mock


class DatabaseLoggingTestCase(unittest.TestCase):
    def _create(self, exists):
        sys_db = MagicMock()
        sys_db.has_database.return_value = exists
        with patch.object(adb, "_sys_db", return_value=sys_db), patch.object(
            adb, "_client"
        ):
            with capture_logs() as logs:
                adb.create_or_get_database("kn")
        return logs

    def test_new_database_is_logged(self):
        event = _one(self._create(exists=False), "database_created")
        self.assertEqual((event["database"], event["log_level"]), ("kn", "info"))

    def test_existing_database_logs_nothing(self):
        self.assertEqual(self._create(exists=True), [])

    def test_dropping_a_database_is_a_warning(self):
        sys_db = MagicMock()
        sys_db.has_database.return_value = True
        with patch.object(adb, "_sys_db", return_value=sys_db):
            with capture_logs() as logs:
                adb.delete_database("kn")
        event = _one(logs, "database_dropped")
        self.assertEqual((event["database"], event["log_level"]), ("kn", "warning"))
        sys_db.delete_database.assert_called_once_with("kn")

    def test_dropping_a_missing_database_logs_nothing(self):
        sys_db = MagicMock()
        sys_db.has_database.return_value = False
        with patch.object(adb, "_sys_db", return_value=sys_db):
            with capture_logs() as logs:
                adb.delete_database("kn")
        self.assertEqual(logs, [])


class GraphLoggingTestCase(unittest.TestCase):
    def test_graph_created_then_reused(self):
        db = _named("kn")
        db.has_graph.return_value = False
        with capture_logs() as logs:
            adb.create_or_get_graph(db, "g")
        event = _one(logs, "graph_created")
        self.assertEqual((event["database"], event["graph"]), ("kn", "g"))
        db.has_graph.return_value = True
        with capture_logs() as logs:
            adb.create_or_get_graph(db, "g")
        self.assertEqual(logs, [])

    def test_graph_dropped_is_a_warning(self):
        db = _named("kn")
        db.has_graph.return_value = True
        with capture_logs() as logs:
            adb.delete_graph(db, "g")
        event = _one(logs, "graph_dropped")
        self.assertEqual((event["graph"], event["log_level"]), ("g", "warning"))

    def test_collection_creation_is_debug_and_deletion_a_warning(self):
        graph = _named("g")
        graph.has_vertex_collection.return_value = False
        graph.has_edge_definition.return_value = False
        with capture_logs() as logs:
            adb.create_or_get_vertex_collection(graph, "CL")
            adb.create_or_get_edge_collection(graph, "CL", "GO")
        self.assertEqual(_names(logs), ["vertex_collection_created", "edge_definition_created"])
        self.assertEqual({e["log_level"] for e in logs}, {"debug"})
        self.assertEqual(logs[1]["collection"], "CL-GO")

        graph.has_vertex_collection.return_value = True
        graph.has_edge_definition.return_value = True
        with capture_logs() as logs:
            adb.delete_vertex_collection(graph, "CL")
            adb.delete_edge_collection(graph, "CL-GO")
        self.assertEqual(_names(logs), ["vertex_collection_deleted", "edge_definition_deleted"])
        self.assertEqual({e["log_level"] for e in logs}, {"warning"})


class SummaryLoggingTestCase(unittest.TestCase):
    def test_counts_and_totals(self):
        db = MagicMock()
        db.collections.return_value = [
            {"name": "_users", "system": True, "type": "document"},
            {"name": "CL", "system": False, "type": "document"},
            {"name": "GO", "system": False, "type": "document"},
            {"name": "CL-GO", "system": False, "type": "edge"},
        ]
        counts = {"CL": 10, "GO": 5, "CL-GO": 7}
        db.collection.side_effect = lambda n: MagicMock(count=lambda: counts[n])
        with patch.object(adb, "create_or_get_database", return_value=db):
            with capture_logs() as logs:
                summary = adb.print_summary("kn")
        self.assertEqual(summary, {"vertex": {"CL": 10, "GO": 5}, "edge": {"CL-GO": 7}})
        counted = [
            (e["collection"], e["kind"], e["records_out"], e["database"])
            for e in logs
            if e["event"] == "collection_counted"
        ]
        self.assertEqual(
            counted,
            [("CL", "vertex", 10, "kn"), ("GO", "vertex", 5, "kn"), ("CL-GO", "edge", 7, "kn")],
        )
        total = _one(logs, "database_summary")
        self.assertEqual((total["vertex_total"], total["edge_total"]), (15, 7))

    def test_vertex_examples_are_debug_events(self):
        db = MagicMock()
        db.collections.return_value = [
            {"name": "_users", "type": "document"},
            {"name": "CL", "type": "document"},
        ]
        collection = MagicMock()
        collection.keys.return_value = ["a"]
        collection.count.return_value = 1
        collection.get.return_value = {"_key": "a"}
        with (
            patch.object(adb, "create_or_get_database", return_value=db),
            patch.object(adb, "create_or_get_graph"),
            patch.object(adb, "create_or_get_vertex_collection", return_value=collection),
        ):
            with capture_logs() as logs:
                adb.print_vertex_examples("kn", "g")
        event = _one(logs, "vertex_example")
        self.assertEqual(
            (event["collection"], event["document"], event["log_level"]),
            ("CL", {"_key": "a"}, "debug"),
        )


class ViewLoggingTestCase(unittest.TestCase):
    def test_a_link_without_a_collection_is_a_warning(self):
        db = MagicMock()
        db.collections.return_value = [{"name": "CL"}]
        with patch.object(adb, "create_or_get_database", return_value=db):
            with capture_logs() as logs:
                adb.create_view("kn", collection_maps_name=COLLECTION_MAPS)
        skipped = [e for e in logs if e["event"] == "view_link_skipped"]
        self.assertTrue(skipped)
        self.assertEqual({e["database"] for e in skipped}, {"kn"})
        self.assertNotIn("CL", {e["collection"] for e in skipped})
        self.assertEqual({e["log_level"] for e in skipped}, {"warning"})


if __name__ == "__main__":
    unittest.main()
