import base64
import gzip
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from DataFetcher import (
    DataFetcher,
    CellxGeneFetcher,
    GeneFetcher,
    HuBMAPFetcher,
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
            raise RuntimeError(f"Simulated error for {id_value}")
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

        self.assertEqual(set(fetcher.fetched_ids), {"a", "b", "c"})
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


class DataFetcherCheckpointTestCase(unittest.TestCase):
    """Tests for JSONL checkpoint and resume logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_path = Path(self.tmpdir) / "results.json"
        self.checkpoint_path = Path(self.tmpdir) / "results.jsonl"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_checkpoint_removed_after_completion(self):
        """JSONL checkpoint is cleaned up after successful run."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a", "b"]}

        fetcher.run(context, force=False)

        self.assertTrue(self.output_path.exists())
        self.assertFalse(self.checkpoint_path.exists())

    def test_checkpoint_resume(self):
        """Run resumes from a JSONL checkpoint, skipping already-fetched IDs."""
        # Simulate an interrupted run by writing a partial checkpoint
        with open(self.checkpoint_path, "w") as fp:
            fp.write(json.dumps({"id": "a", "data": {"data": "a"}}) + "\n")

        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a", "b", "c"]}

        results = fetcher.run(context, force=False)

        # "a" was not re-fetched
        self.assertNotIn("a", fetcher.fetched_ids)
        # "b" and "c" were fetched
        self.assertEqual(set(fetcher.fetched_ids), {"b", "c"})
        # All results present
        self.assertEqual(results["a"], {"data": "a"})
        self.assertEqual(results["b"], {"data": "b"})
        self.assertEqual(results["c"], {"data": "c"})

    def test_force_clears_checkpoint(self):
        """Force flag removes stale checkpoint before re-fetching."""
        with open(self.checkpoint_path, "w") as fp:
            fp.write(json.dumps({"id": "a", "data": {"data": "stale"}}) + "\n")

        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path
        context = {"ids": ["a"]}

        results = fetcher.run(context, force=True)

        self.assertIn("a", fetcher.fetched_ids)
        self.assertEqual(results["a"], {"data": "a"})
        self.assertFalse(self.checkpoint_path.exists())


class DataFetcherRetryTestCase(unittest.TestCase):
    """Tests for _fetch_with_retry 429 handling."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.output_path = Path(self.tmpdir) / "results.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_retry_on_429(self):
        """_fetch_with_retry retries on 429 and succeeds."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path

        mock_response = MagicMock()
        mock_response.status_code = 429
        mock_response.headers = {"Retry-After": "0"}

        call_count = 0
        original_fetch = fetcher.fetch_one

        def flaky_fetch(id_value):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise requests.HTTPError(response=mock_response)
            return original_fetch(id_value)

        fetcher.fetch_one = flaky_fetch
        result = fetcher._fetch_with_retry("a")

        self.assertEqual(call_count, 2)
        self.assertEqual(result, {"data": "a"})

    def test_non_429_not_retried(self):
        """_fetch_with_retry does not retry on non-429 HTTP errors."""
        fetcher = FakeFetcher()
        fetcher.output_path = self.output_path

        mock_response = MagicMock()
        mock_response.status_code = 500

        def failing_fetch(id_value):
            raise requests.HTTPError(response=mock_response)

        fetcher.fetch_one = failing_fetch

        with self.assertRaises(requests.HTTPError):
            fetcher._fetch_with_retry("a")


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

    def test_get_ids_raises_on_missing_context(self):
        """get_ids raises ValueError when context is missing required key."""
        fetcher = CellxGeneFetcher()
        with self.assertRaises(ValueError):
            fetcher.get_ids({})

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
        """fetch_one raises HTTPError when dataset request fails."""
        mock_response = MagicMock(status_code=404)
        mock_response.raise_for_status.side_effect = requests.HTTPError("404")
        mock_get.return_value = mock_response

        fetcher = CellxGeneFetcher()
        with self.assertRaises(requests.HTTPError):
            fetcher.fetch_one("bad_id")


class OpenTargetsFetcherTestCase(unittest.TestCase):
    """Tests for OpenTargetsFetcher."""

    def test_get_ids(self):
        """get_ids returns gene Ensembl IDs from context."""
        fetcher = OpenTargetsFetcher()
        context = {"gene_data": {"gene_ensembl_ids": ["ENSG001", "ENSG002"]}}
        self.assertEqual(fetcher.get_ids(context), ["ENSG001", "ENSG002"])

    def test_get_ids_raises_on_missing_context(self):
        """get_ids raises ValueError when context is missing required key."""
        fetcher = OpenTargetsFetcher()
        with self.assertRaises(ValueError):
            fetcher.get_ids({})

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

    def test_get_ids_raises_on_missing_context(self):
        """get_ids raises ValueError when context is missing required key."""
        fetcher = GeneFetcher()
        with self.assertRaises(ValueError):
            fetcher.get_ids({})

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
    def test_fetch_one_raises_on_failure(self, mock_fetch):
        """fetch_one raises RuntimeError when fetch returns None."""
        mock_fetch.return_value = None

        fetcher = GeneFetcher()
        with self.assertRaises(RuntimeError):
            fetcher.fetch_one("bad_id")

    @patch("DataFetcher.fetch_xml_for_gene_id")
    def test_fetch_one_extracts_uniprot_name(self, mock_fetch):
        """fetch_one extracts UniProt accession from Gene XML."""
        xml_with_uniprot = (
            "<Entrezgene>"
            "<Other-source_url>"
            "https://www.uniprot.org/uniprot/P24941"
            "</Other-source_url>"
            "</Entrezgene>"
        )
        mock_fetch.return_value = xml_with_uniprot

        fetcher = GeneFetcher()
        result = fetcher.fetch_one("896")

        self.assertEqual(result["UniProt_name"], "P24941")

    @patch("DataFetcher.fetch_xml_for_gene_id")
    def test_fetch_one_extracts_uniprotkb_name(self, mock_fetch):
        """fetch_one handles the newer /uniprotkb/ URL format."""
        xml_with_uniprotkb = (
            "<Entrezgene>"
            "<Other-source_url>"
            "https://www.uniprot.org/uniprotkb/O14757"
            "</Other-source_url>"
            "</Entrezgene>"
        )
        mock_fetch.return_value = xml_with_uniprotkb

        fetcher = GeneFetcher()
        result = fetcher.fetch_one("1080")

        self.assertEqual(result["UniProt_name"], "O14757")

    @patch("DataFetcher.fetch_xml_for_gene_id")
    def test_fetch_one_no_uniprot_url(self, mock_fetch):
        """fetch_one sets UniProt_name to None when XML has no UniProt URL."""
        mock_fetch.return_value = "<Entrezgene><summary>test</summary></Entrezgene>"

        fetcher = GeneFetcher()
        result = fetcher.fetch_one("999")

        self.assertIsNone(result["UniProt_name"])
        self.assertIn("xml_gz_b64", result)

    def test_before_dump_stores_ids(self):
        """before_dump stores gene_entrez_ids in results."""
        fetcher = GeneFetcher()
        results = {}
        fetcher.before_dump(results, ["896"])
        self.assertEqual(results["gene_entrez_ids"], ["896"])


class UniProtFetcherTestCase(unittest.TestCase):
    """Tests for UniProtFetcher."""

    def test_get_ids_raises_on_missing_context(self):
        """get_ids raises ValueError when context is missing required key."""
        fetcher = UniProtFetcher()
        with self.assertRaises(ValueError):
            fetcher.get_ids({})

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
        """fetch_one raises HTTPError on non-200 response."""
        mock_response = MagicMock(status_code=404)
        mock_response.raise_for_status.side_effect = requests.HTTPError("404")
        mock_get.return_value = mock_response

        fetcher = UniProtFetcher()
        with self.assertRaises(requests.HTTPError):
            fetcher.fetch_one("BAD")


class HuBMAPFetcherTestCase(unittest.TestCase):
    """Tests for HuBMAPFetcher."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.hubmap_dir = Path(self.tmpdir) / "hubmap"
        self.hubmap_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def _make_fetcher(self):
        fetcher = HuBMAPFetcher()
        return fetcher

    @patch("DataFetcher.requests.get")
    def test_get_hubmap_json_urls_parses_organ_and_version(self, mock_get):
        """_get_hubmap_json_urls extracts organ, version, and URL from response."""
        html = '<a href="https://cdn.humanatlas.io/digital-objects/asct-b/kidney/v2.1/graph.json">link</a>'
        mock_get.return_value = MagicMock(status_code=200, text=html)

        with patch("DataFetcher.HUBMAP_LATEST_URLS", [
            "https://lod.humanatlas.io/asct-b/kidney/latest/",
        ]):
            urls = HuBMAPFetcher._get_hubmap_json_urls()

        self.assertEqual(len(urls), 1)
        org, ver, url = urls[0]
        self.assertEqual(org, "kidney")
        self.assertEqual(ver, 2.1)
        self.assertIn("v2.1/graph.json", url)

    @patch("DataFetcher.requests.get")
    def test_get_hubmap_json_urls_raises_on_bad_status(self, mock_get):
        """_get_hubmap_json_urls raises when latest URL returns non-200."""
        mock_get.return_value = MagicMock(status_code=500)

        with patch("DataFetcher.HUBMAP_LATEST_URLS", [
            "https://lod.humanatlas.io/asct-b/kidney/latest/",
        ]):
            with self.assertRaises(Exception, msg="Could not get HuBMAP latest URL"):
                HuBMAPFetcher._get_hubmap_json_urls()

    @patch("DataFetcher.requests.get")
    def test_get_hubmap_json_urls_raises_on_no_url_match(self, mock_get):
        """_get_hubmap_json_urls raises when response has no matching URL."""
        mock_get.return_value = MagicMock(status_code=200, text="no link here")

        with patch("DataFetcher.HUBMAP_LATEST_URLS", [
            "https://lod.humanatlas.io/asct-b/kidney/latest/",
        ]):
            with self.assertRaises(Exception, msg="Could not find HuBMAP JSON URL or version"):
                HuBMAPFetcher._get_hubmap_json_urls()

    @patch("DataFetcher.requests.get")
    @patch.object(HuBMAPFetcher, "_get_hubmap_json_urls")
    def test_run_downloads_new_file(self, mock_urls, mock_get):
        """run() downloads a new file when it does not exist."""
        mock_urls.return_value = [("kidney", 2.1, "https://example.com/v2.1/graph.json")]
        mock_get.return_value = MagicMock(status_code=200, text='{"data": "test"}')

        fetcher = self._make_fetcher()
        with patch("DataFetcher.HUBMAP_DIRPATH", self.hubmap_dir):
            fetcher.run({})

        filepath = self.hubmap_dir / "kidney-v2.1.json"
        self.assertTrue(filepath.exists())
        with open(filepath) as fp:
            self.assertEqual(json.load(fp), {"data": "test"})

    @patch.object(HuBMAPFetcher, "_get_hubmap_json_urls")
    def test_run_skips_existing_file(self, mock_urls):
        """run() skips download when the file already exists."""
        mock_urls.return_value = [("kidney", 2.1, "https://example.com/v2.1/graph.json")]

        # Pre-create the file
        filepath = self.hubmap_dir / "kidney-v2.1.json"
        filepath.write_text('{"existing": true}')

        fetcher = self._make_fetcher()
        with patch("DataFetcher.HUBMAP_DIRPATH", self.hubmap_dir):
            fetcher.run({})

        # File content unchanged (no download occurred)
        with open(filepath) as fp:
            self.assertEqual(json.load(fp), {"existing": True})

    @patch("DataFetcher.requests.get")
    @patch.object(HuBMAPFetcher, "_get_hubmap_json_urls")
    def test_run_archives_old_version(self, mock_urls, mock_get):
        """run() moves old version files to .archive/ before downloading."""
        mock_urls.return_value = [("kidney", 2.1, "https://example.com/v2.1/graph.json")]
        mock_get.return_value = MagicMock(status_code=200, text='{"new": true}')

        # Pre-create an older version
        old_file = self.hubmap_dir / "kidney-v1.5.json"
        old_file.write_text('{"old": true}')

        fetcher = self._make_fetcher()
        with patch("DataFetcher.HUBMAP_DIRPATH", self.hubmap_dir):
            fetcher.run({})

        # Old file archived
        self.assertFalse(old_file.exists())
        archived = self.hubmap_dir / ".archive" / "kidney-v1.5.json"
        self.assertTrue(archived.exists())

        # New file downloaded
        new_file = self.hubmap_dir / "kidney-v2.1.json"
        self.assertTrue(new_file.exists())

    @patch("DataFetcher.requests.get")
    @patch.object(HuBMAPFetcher, "_get_hubmap_json_urls")
    def test_run_no_file_on_download_failure(self, mock_urls, mock_get):
        """run() does not create a file when download returns non-200."""
        mock_urls.return_value = [("kidney", 2.1, "https://example.com/v2.1/graph.json")]
        mock_get.return_value = MagicMock(status_code=500)

        fetcher = self._make_fetcher()
        with patch("DataFetcher.HUBMAP_DIRPATH", self.hubmap_dir):
            fetcher.run({})

        filepath = self.hubmap_dir / "kidney-v2.1.json"
        self.assertFalse(filepath.exists())
