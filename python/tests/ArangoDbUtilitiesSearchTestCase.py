import json
from pathlib import Path
import sys
import tempfile
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
        self.db.collections.return_value = [
            {"name": name} for name in ["CL", "CS", "CSD", "NCBITaxon", "PUB"]
        ]
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
        self.assertEqual(
            set(properties["links"]), {"CL", "CS", "CSD", "NCBITaxon", "PUB"}
        )
        for link in properties["links"].values():
            self.assertEqual(link["fields"]["_search"], {"analyzers": ["norm-lower"]})

    def fields(self, collection, maps_name=COLLECTION_MAPS):
        adb.create_view("test-db", collection_maps_name=maps_name)
        properties = self.db.create_view.call_args.kwargs["properties"]
        return properties["links"][collection]["fields"]

    def analyzers(self, collection, field):
        return set(self.fields(collection)[field]["analyzers"])

    def test_create_view_links_fields_by_search_tier(self):
        text = {"text_en", "text_en_no_stem", "n-gram", "identity"}
        self.assertEqual(
            self.analyzers("CL", "label"), text | {"norm-lower", "wildcard-lower"}
        )
        self.assertEqual(self.analyzers("CL", "hasExactSynonym"), text | {"norm-lower"})
        self.assertEqual(self.analyzers("CL", "definition"), text)
        self.assertEqual(
            self.analyzers("NCBITaxon", "_key"), text | {"norm-lower", "wildcard-lower"}
        )

    def test_create_view_skips_fields_without_a_tier(self):
        self.assertNotIn("dataset_name", self.fields("CS"))
        self.assertIn("dataset_name", self.fields("CSD"))
        self.assertNotIn("cell_count", self.fields("CS"))
        self.assertNotIn("f_beta_score", self.fields("CS"))
        self.assertNotIn("year", self.fields("PUB"))

    def write_maps(self, edit):
        """Write the real maps, edited by edit(maps, CL label field), to a
        temporary file and return its name."""
        maps = json.loads(COLLECTION_MAPS.read_text())
        edit(maps, dict(maps["maps"])["CL"]["individual_fields"][0])
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        self.addCleanup(Path(tmp.name).unlink)
        with tmp:
            json.dump(maps, tmp)
        return tmp.name

    def test_create_view_rejects_unknown_tier(self):
        maps = self.write_maps(lambda m, f: f.update(search_tier="headline"))
        with self.assertRaisesRegex(ValueError, "CL.label"):
            self.fields("CL", maps)

    def test_create_view_rejects_missing_tier(self):
        maps = self.write_maps(lambda m, f: f.pop("search_tier"))
        with self.assertRaisesRegex(ValueError, "CL.label"):
            self.fields("CL", maps)

    def test_create_view_rejects_unknown_analyzer(self):
        maps = self.write_maps(lambda m, f: m["search_tiers"]["label"].append("nope"))
        with self.assertRaisesRegex(ValueError, "BGS.markers .*nope"):
            self.fields("CL", maps)

    def test_create_view_rejects_a_field_repeated_with_another_tier(self):
        maps = self.write_maps(
            lambda m, f: dict(m["maps"])["CL"]["individual_fields"].append(
                {**f, "search_tier": None}
            )
        )
        with self.assertRaisesRegex(ValueError, "CL.label has conflicting"):
            self.fields("CL", maps)

    def test_create_view_links_a_repeated_field_once(self):
        self.db.collections.return_value.append({"name": "PR"})
        self.assertEqual(self.analyzers("PR", "label"), self.analyzers("CL", "label"))

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

    def test_create_view_excludes_hsapdv_and_pato_for_cell_kn_phenotypes(self):
        """HsapDv and PATO are excluded from the induced phenotype graph like
        GO, so its search view must not link them either
        (Springbok-LLC/nlm-ckn-etl#155)."""
        self.db.collections.return_value = [
            {"name": "CL"},
            {"name": "HsapDv"},
            {"name": "PATO"},
        ]
        adb.create_view("Cell-KN-Phenotypes", collection_maps_name=COLLECTION_MAPS)
        properties = self.db.create_view.call_args.kwargs["properties"]
        self.assertNotIn("HsapDv", properties["links"])
        self.assertNotIn("PATO", properties["links"])
        self.assertIn("CL", properties["links"])


if __name__ == "__main__":
    unittest.main()
