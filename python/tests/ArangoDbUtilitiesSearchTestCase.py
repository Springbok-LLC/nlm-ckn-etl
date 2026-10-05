from pathlib import Path
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import ArangoDbUtilities as adb

COLLECTION_MAPS = (
    Path(__file__).parent.parent.parent / "data" / "nlm-ckn-collection-maps.json"
)


class ArangoDbUtilitiesSearchTestCase(unittest.TestCase):
    """Tests the analyzer and view configuration for the _search field
    against a mocked database, so no ArangoDB instance is required."""

    def setUp(self):
        self.db = MagicMock()
        self.db.collections.return_value = [{"name": "CL"}, {"name": "CS"}]
        patcher = patch.object(adb, "create_or_get_database", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_create_analyzers_includes_norm_lower(self):
        adb.create_analyzers("test-db")
        created = {c.kwargs["name"]: c.kwargs for c in self.db.create_analyzer.mock_calls}
        self.assertIn("norm-lower", created)
        self.assertEqual(created["norm-lower"]["analyzer_type"], "norm")
        self.assertEqual(created["norm-lower"]["properties"]["case"], "lower")

    def test_delete_analyzers_includes_norm_lower(self):
        adb.delete_analyzers("test-db")
        deleted = [c.args[0] for c in self.db.delete_analyzer.call_args_list]
        self.assertIn("test-db::norm-lower", deleted)

    def test_create_analyzers_includes_wildcard_lower(self):
        adb.create_analyzers("test-db")
        created = {c.kwargs["name"]: c.kwargs for c in self.db.create_analyzer.mock_calls}
        self.assertIn("wildcard-lower", created)
        self.assertEqual(created["wildcard-lower"]["analyzer_type"], "wildcard")
        inner = created["wildcard-lower"]["properties"]["analyzer"]
        self.assertEqual(inner["type"], "norm")
        self.assertEqual(inner["properties"]["case"], "lower")

    def test_delete_analyzers_includes_wildcard_lower(self):
        adb.delete_analyzers("test-db")
        deleted = [c.args[0] for c in self.db.delete_analyzer.call_args_list]
        self.assertIn("test-db::wildcard-lower", deleted)

    def test_create_view_indexes_search_with_norm_lower(self):
        adb.create_view("test-db", collection_maps_name=COLLECTION_MAPS)
        properties = self.db.create_view.call_args.kwargs["properties"]
        self.assertEqual(set(properties["links"]), {"CL", "CS"})
        for link in properties["links"].values():
            self.assertEqual(link["fields"]["_search"], {"analyzers": ["norm-lower"]})

    def test_create_view_keeps_per_field_indexing(self):
        adb.create_view("test-db", collection_maps_name=COLLECTION_MAPS)
        properties = self.db.create_view.call_args.kwargs["properties"]
        self.assertEqual(
            properties["links"]["CL"]["fields"]["label"],
            {"analyzers": ["text_en", "text_en_no_stem", "n-gram", "identity"]},
        )

    def test_create_view_excludes_go_for_cell_kn_phenotypes(self):
        """GO is excluded from the induced phenotype graph itself
        (InducedSubgraphFinder.IGNORED_VERTEX_COLLECTIONS), so its search view
        must not link a GO collection even when one exists in the database
        (Springbok-LLC/nlm-ckn-etl#119)."""
        self.db.collections.return_value = [{"name": "CL"}, {"name": "GO"}]
        adb.create_view("Cell-KN-Phenotypes", collection_maps_name=COLLECTION_MAPS)
        properties = self.db.create_view.call_args.kwargs["properties"]
        self.assertNotIn("GO", properties["links"])
        self.assertIn("CL", properties["links"])


if __name__ == "__main__":
    unittest.main()
