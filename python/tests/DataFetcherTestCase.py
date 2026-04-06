import base64
import gzip
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from DataFetcher import (
    DataFetcher,
    CellxGeneFetcher,
    GeneFetcher,
    OpenTargetsFetcher,
    UniProtFetcher,
)


# --- Fake subclass for testing DataFetcher logic ---


class FakeFetcher(DataFetcher):
    """Minimal fetcher for testing the base class batch loop."""

    name = "fake"
    output_path = None  # Set in setUp

    def __init__(self, fetch_results=None, fetch_errors=None):
        self.fetch_results = fetch_results or {}
        self.fetch_errors = fetch_errors or set()
        self.fetched_ids = []

    def get_ids(self, context):
        return context["ids"]

    def fetch_one(self, id_value):
        self.fetched_ids.append(id_value)
        if id_value in self.fetch_errors:
            raise Exception(f"Simulated error for {id_value}")
        return self.fetch_results.get(id_value, {"data": id_value})

    def before_dump(self, results, ids):
        results["_ids"] = ids


class DataFetcherTestCase(unittest.TestCase):
    """Tests for DataFetcher batch loop logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_path = Path(self.tmpdir) / "results.json"

    def test_fetches_all_ids(self):
        """All IDs are fetched when no prior results exist."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a", "b", "c"]}

        results = fetcher.run(context, force=False)

        self.assertEqual(fetcher.fetched_ids, ["a", "b", "c"])
        self.assertEqual(results["a"], {"data": "a"})
        self.assertEqual(results["b"], {"data": "b"})
        self.assertEqual(results["c"], {"data": "c"})

    def test_skips_existing_results(self):
        """IDs already in results are not re-fetched."""
        # Pre-populate results file
        existing = {"a": {"data": "old_a"}, "_ids": ["a", "b"]}
        with open(self.output_path, "w") as fp:
            json.dump(existing, fp)

        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a", "b"]}

        results = fetcher.run(context, force=False)

        self.assertNotIn("a", fetcher.fetched_ids)
        self.assertIn("b", fetcher.fetched_ids)
        self.assertEqual(results["a"], {"data": "old_a"})

    def test_force_clears_existing(self):
        """Force flag causes all IDs to be re-fetched."""
        existing = {"a": {"data": "old_a"}, "_ids": ["a"]}
        with open(self.output_path, "w") as fp:
            json.dump(existing, fp)

        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a"]}

        results = fetcher.run(context, force=True)

        self.assertIn("a", fetcher.fetched_ids)
        self.assertEqual(results["a"], {"data": "a"})

    def test_on_fetch_error(self):
        """Errors in fetch_one store the on_fetch_error fallback."""
        fetcher = FakeFetcher(fetch_errors={"b"})
        fetcher.output_path = self.output_path
        context = {"ids": ["a", "b", "c"]}

        results = fetcher.run(context, force=False)

        self.assertEqual(results["a"], {"data": "a"})
        self.assertEqual(results["b"], {})
        self.assertEqual(results["c"], {"data": "c"})

    def test_before_dump_called(self):
        """before_dump stores metadata in results."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a"]}

        results = fetcher.run(context, force=False)

        self.assertEqual(results["_ids"], ["a"])

    def test_results_saved_to_disk(self):
        """Results are written to the output path."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a"]}

        fetcher.run(context, force=False)

        self.assertTrue(self.output_path.exists())
        with open(self.output_path) as fp:
            saved = json.load(fp)
        self.assertIn("a", saved)

    def test_batch_size_triggers_dump(self):
        """Results are dumped when batch_size is reached."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        fetcher.batch_size = 2
        context = {"ids": ["a", "b", "c", "d", "e"]}

        fetcher.run(context, force=False)

        # Verify file was written (multiple dumps occurred)
        self.assertTrue(self.output_path.exists())
        with open(self.output_path) as fp:
            saved = json.load(fp)
        self.assertEqual(len(fetcher.fetched_ids), 5)
        # All results present in final save
        for id_val in ["a", "b", "c", "d", "e"]:
            self.assertIn(id_val, saved)


# --- Fetcher subclass tests ---


class CellxGeneFetcherTestCase(unittest.TestCase):
    """Tests for CellxGeneFetcher."""

    def test_get_ids_flattens_lists(self):
        """get_ids flattens nested dataset_version_id_lists."""
        fetcher = CellxGeneFetcher()
        context = {
            "dataset_version_id_lists": [["id1", "id2"], ["id3"]],
        }
        ids = fetcher.get_ids(context)
        self.assertEqual(ids, ["id1", "id2", "id3"])

    @patch("DataFetcher.requests.get")
    def test_fetch_one_success(self, mock_get):
        """fetch_one returns dataset and collection JSON on success."""
        dataset_json = {"collection_id": "coll1", "title": "Test"}
        collection_json = {"publisher_metadata": {"authors": []}}

        mock_responses = [
            MagicMock(status_code=200, json=MagicMock(return_value=dataset_json)),
            MagicMock(status_code=200, json=MagicMock(return_value=collection_json)),
        ]
        mock_get.side_effect = mock_responses

        fetcher = CellxGeneFetcher()
        result = fetcher.fetch_one("test_version_id")

        self.assertEqual(result["dataset_json"], dataset_json)
        self.assertEqual(result["collection_json"], collection_json)
        self.assertEqual(mock_get.call_count, 2)

    @patch("DataFetcher.requests.get")
    def test_fetch_one_dataset_failure(self, mock_get):
        """fetch_one returns empty dict when dataset request fails."""
        mock_get.return_value = MagicMock(status_code=404)

        fetcher = CellxGeneFetcher()
        result = fetcher.fetch_one("bad_id")

        self.assertEqual(result, {})


class OpenTargetsFetcherTestCase(unittest.TestCase):
    """Tests for OpenTargetsFetcher."""

    def test_get_ids(self):
        """get_ids returns gene Ensembl IDs from context."""
        fetcher = OpenTargetsFetcher()
        context = {"gene_data": {"gene_ensembl_ids": ["ENSG001", "ENSG002"]}}
        self.assertEqual(fetcher.get_ids(context), ["ENSG001", "ENSG002"])

    @patch("DataFetcher.requests.post")
    def test_fetch_one_success(self, mock_post):
        """fetch_one returns raw GraphQL data on success."""
        response_data = {"data": {"target": {"id": "ENSG001"}}}
        mock_post.return_value = MagicMock(
            status_code=200,
            text=json.dumps(response_data),
            raise_for_status=MagicMock(),
        )

        fetcher = OpenTargetsFetcher()
        result = fetcher.fetch_one("ENSG001")

        self.assertEqual(result, {"target": {"id": "ENSG001"}})

    def test_on_fetch_error_structure(self):
        """on_fetch_error returns empty target and empty resources."""
        fetcher = OpenTargetsFetcher()
        result = fetcher.on_fetch_error("ENSG001")

        self.assertIn("target", result)
        self.assertEqual(result["target"], {})
        for resource in [
            "diseases", "drugs", "interactions", "pharmacogenetics",
            "tractability", "expression", "depmap",
        ]:
            self.assertIn(resource, result)

    def test_before_dump_stores_ids(self):
        """before_dump stores gene_ensembl_ids in results."""
        fetcher = OpenTargetsFetcher()
        results = {}
        fetcher.before_dump(results, ["ENSG001"])
        self.assertEqual(results["gene_ensembl_ids"], ["ENSG001"])


class GeneFetcherTestCase(unittest.TestCase):
    """Tests for GeneFetcher."""

    def test_get_ids(self):
        """get_ids returns gene Entrez IDs from context."""
        fetcher = GeneFetcher()
        context = {"gene_data": {"gene_entrez_ids": ["896", "1080"]}}
        self.assertEqual(fetcher.get_ids(context), ["896", "1080"])

    @patch("DataFetcher.fetch_xml_for_gene_id")
    def test_fetch_one_compresses_xml(self, mock_fetch):
        """fetch_one returns gzip-compressed base64-encoded XML."""
        mock_fetch.return_value = "<xml>test data</xml>"

        fetcher = GeneFetcher()
        result = fetcher.fetch_one("896")

        self.assertIn("xml_gz_b64", result)
        # Verify round-trip
        decoded = gzip.decompress(
            base64.b64decode(result["xml_gz_b64"])
        ).decode("utf-8")
        self.assertEqual(decoded, "<xml>test data</xml>")

    @patch("DataFetcher.fetch_xml_for_gene_id")
    def test_fetch_one_returns_empty_on_failure(self, mock_fetch):
        """fetch_one returns empty dict when fetch returns None."""
        mock_fetch.return_value = None

        fetcher = GeneFetcher()
        result = fetcher.fetch_one("bad_id")

        self.assertEqual(result, {})

    def test_before_dump_stores_ids(self):
        """before_dump stores gene_entrez_ids in results."""
        fetcher = GeneFetcher()
        results = {}
        fetcher.before_dump(results, ["896"])
        self.assertEqual(results["gene_entrez_ids"], ["896"])


class UniProtFetcherTestCase(unittest.TestCase):
    """Tests for UniProtFetcher."""

    def test_get_ids_derives_accessions(self):
        """get_ids extracts unique sorted protein accessions from gene results."""
        fetcher = UniProtFetcher()
        context = {
            "gene_results": {
                "896": {"UniProt_name": "P24941"},
                "1080": {"UniProt_name": "O14757"},
                "gene_entrez_ids": ["896", "1080"],
            },
        }
        ids = fetcher.get_ids(context)
        self.assertEqual(ids, ["O14757", "P24941"])

    def test_get_ids_skips_empty(self):
        """get_ids skips entries with no gene data."""
        fetcher = UniProtFetcher()
        context = {
            "gene_results": {
                "896": {"UniProt_name": "P24941"},
                "bad": {},
                "gene_entrez_ids": ["896", "bad"],
            },
        }
        ids = fetcher.get_ids(context)
        self.assertEqual(ids, ["P24941"])

    @patch("DataFetcher.requests.get")
    def test_fetch_one_success(self, mock_get):
        """fetch_one returns raw UniProt JSON on success."""
        response_json = {"primaryAccession": "P24941"}
        mock_get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value=response_json),
        )

        fetcher = UniProtFetcher()
        result = fetcher.fetch_one("P24941")

        self.assertEqual(result, response_json)

    @patch("DataFetcher.requests.get")
    def test_fetch_one_failure(self, mock_get):
        """fetch_one returns empty dict on non-200 response."""
        mock_get.return_value = MagicMock(status_code=404)

        fetcher = UniProtFetcher()
        result = fetcher.fetch_one("BAD")

        self.assertEqual(result, {})
