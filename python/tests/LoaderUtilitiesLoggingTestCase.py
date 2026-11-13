"""LoaderUtilities.py logs events: file discovery, UBERON roots, gene mapping."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
from botocore.exceptions import ClientError, NoCredentialsError
from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import LoaderUtilities as lu  # noqa: E402

# conftest patches this for the whole session, once collection is over.
_real_get_gene_mapping = lu.get_gene_names_and_ensembl_and_entrez_ids


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


class ParseTermLoggingTestCase(unittest.TestCase):
    def test_skipped_terms_are_debug_events(self):
        with capture_logs() as logs:
            result = lu.parse_term("http://purl.obolibrary.org/obo/GOREL_0000001")
        self.assertEqual(result, (None, None, None, None, None))
        event = _one(logs, "ontology_term_skipped")
        self.assertEqual((event["reason"], event["log_level"]), ("invalid_ontology_id", "debug"))


class UberonLoggingTestCase(unittest.TestCase):
    def setUp(self):
        lu._ORGANS_WITHOUT_ROOT.clear()
        self.addCleanup(lu._ORGANS_WITHOUT_ROOT.clear)

    def test_file_without_a_root_term(self):
        with tempfile.TemporaryDirectory() as d:
            pd.DataFrame(
                {"obo_id": ["UBERON:1"], "label": ["x"], "level": ["descendant"]}
            ).to_csv(Path(d) / "uberon_heart.csv", index=False)
            with capture_logs() as logs:
                root_map = lu.get_uberon_root_map(d)
        self.assertEqual(root_map, {})
        self.assertEqual(_one(logs, "uberon_root_missing")["path"], "uberon_heart.csv")

    def test_organ_without_a_root_is_reported_once(self):
        with capture_logs() as logs:
            for _ in range(3):
                self.assertIsNone(lu.resolve_root_uberon_term("spleenless", [], {}))
        event = _one(logs, "uberon_root_missing")
        self.assertEqual((event["organ"], event["log_level"]), ("spleenless", "warning"))

    def test_unknown_tissue_terms_are_one_line(self):
        root_map = {
            "heart": {"roots": [("UBERON:1", "heart")], "terms": {"UBERON:1", "UBERON:2"}}
        }
        with capture_logs() as logs:
            root = lu.resolve_root_uberon_term(
                "heart", ["UBERON:2", "UBERON:8", "UBERON:9"], root_map
            )
        self.assertEqual(root, "UBERON:1")
        event = _one(logs, "tissue_terms_unknown")
        self.assertEqual(
            (event["organ"], event["n_terms_unknown"], event["terms"]),
            ("heart", 2, ["UBERON:8", "UBERON:9"]),
        )

    def test_summary_without_an_organ(self):
        summary = pd.DataFrame({"dataset_version_id": ["d"]})
        with capture_logs() as logs:
            self.assertIsNone(lu.resolve_summary_root_uberon_term(summary, {}))
        self.assertEqual(_one(logs, "summary_organ_missing")["log_level"], "warning")


class DatasetFilesLoggingTestCase(unittest.TestCase):
    def _paths(self, files, manifest_rows=None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in files:
                (root / name).write_text("x")
            if manifest_rows is not None:
                pd.DataFrame({"filename": manifest_rows}).to_csv(
                    root / "master_s3_manifest.csv", index=False
                )
            with capture_logs() as logs:
                lu.get_dataset_file_paths(root)
        return logs

    def test_results_without_a_summary(self):
        logs = self._paths(["results_ensg_a.csv"])
        event = _one(logs, "summary_missing")
        self.assertEqual(event["results_file"], "results_ensg_a.csv")

    def test_results_listed_in_the_manifest_but_absent(self):
        logs = self._paths(
            ["results_ensg_a.csv", "master_dataset_summary_a.csv"],
            manifest_rows=["results_ensg_a.csv", "results_ensg_b.csv"],
        )
        event = _one(logs, "results_missing_vs_manifest")
        self.assertEqual(event["file"], "results_ensg_b.csv")
        self.assertEqual(_all(logs, "summary_missing"), [])

    def test_unreadable_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "results_ensg_a.csv").write_text("x")
            (root / "master_dataset_summary_a.csv").write_text("x")
            (root / "master_s3_manifest.csv").write_text("no_filename_column\n1\n")
            with capture_logs() as logs:
                lu.get_dataset_file_paths(root)
        event = _one(logs, "manifest_check_failed")
        self.assertEqual((event["error_type"], event["log_level"]), ("KeyError", "warning"))

    def test_mapping_file_without_dataset_version_ids(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            pd.DataFrame({"cluster_name": ["a"]}).to_csv(
                root / "cluster_cid_mapping_a.csv", index=False
            )
            with capture_logs() as logs:
                self.assertEqual(lu.get_reference_dataset_version_ids(root), set())
        event = _one(logs, "mapping_dataset_version_id_missing")
        self.assertEqual(event["file"], "cluster_cid_mapping_a.csv")


class ResultsLoggingTestCase(unittest.TestCase):
    def test_uuid_column_added_once(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "r.csv"
            pd.DataFrame({"a": [1, 2]}).to_csv(path, index=False)
            with capture_logs() as logs:
                lu.load_results(path)
                lu.load_results(path)
        event = _one(logs, "results_uuid_added")
        self.assertEqual((event["file"], event["rows"]), ("r.csv", 2))

    def test_gene_names_collected(self):
        with (
            patch.object(lu, "load_results", return_value=pd.DataFrame({"clusterName": ["c"]})),
            patch.object(lu, "collect_unique_gene_names", return_value=["A", "B"]),
            patch.object(lu, "collect_unique_gene_ensembl_ids", return_value=[]),
            patch.object(lu, "collect_unique_gene_entrez_ids", return_value=[]),
            capture_logs() as logs,
        ):
            lu.get_unique_gene_names_and_ids([Path("a.csv"), Path("b.csv")])
        event = _one(logs, "gene_names_collected")
        self.assertEqual((event["n_files"], event["records_out"]), (2, 2))


class GeneMappingLoggingTestCase(unittest.TestCase):
    """get_gene_names_and_ensembl_and_entrez_ids, with the network patched out."""

    MAPPING = pd.DataFrame(
        {
            "external_gene_name": ["A", "B"],
            "ensembl_gene_id": ["ENSG1", "ENSG2"],
            "entrezgene_id": [1, 2],
        }
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "gene_mapping.csv"

    def _run(self, bucket="", s3=None, biomart=None):
        patches = [
            patch.object(lu, "GENE_MAPPING_PATH", self.path),
            patch.object(lu, "BIOMART_DIRPATH", self.dir),
            patch.object(lu, "_S3_BUCKET", bucket),
            patch.object(lu, "sleep"),
        ]
        if s3 is not None:
            patches.append(patch.object(lu.boto3, "client", return_value=s3))
        if biomart is not None:
            patches.append(
                patch.object(lu.sc.queries, "biomart_annotations", biomart)
            )
        with capture_logs() as logs:
            for p in patches:
                p.start()
            try:
                result = _real_get_gene_mapping()
            finally:
                for p in patches:
                    p.stop()
        return logs, result

    def _write_mapping(self, age_hours=0):
        self.MAPPING.to_csv(self.path)
        t = time.time() - age_hours * 3600
        os.utime(self.path, (t, t))

    def test_fresh_local_file(self):
        self._write_mapping(age_hours=2)
        logs, _ = self._run()
        event = _one(logs, "gene_mapping_loaded")
        self.assertEqual(
            (event["source"], event["records_out"], event["age_hours"]), ("local", 2, 2.0)
        )
        self.assertNotIn("reason", event)

    def test_expired_local_file_falls_through_to_biomart(self):
        # Older than the limit, which #163 changes back.
        self._write_mapping(age_hours=10_000)
        biomart = MagicMock(return_value=self.MAPPING)
        logs, result = self._run(biomart=biomart)
        event = _one(logs, "gene_mapping_loaded")
        self.assertEqual((event["source"], event["reason"]), ("biomart", "expired"))
        self.assertEqual(event["records_out"], 2)
        self.assertIsInstance(event["duration_ms"], int)
        self.assertEqual(list(result["entrezgene_id"]), ["1", "2"])

    def test_biomart_retries_are_warnings(self):
        biomart = MagicMock(side_effect=[RuntimeError("down"), self.MAPPING])
        logs, _ = self._run(biomart=biomart)
        retry = _one(logs, "biomart_retry")
        self.assertEqual(
            (retry["attempt"], retry["max_retries"], retry["wait_s"], retry["log_level"]),
            (1, 3, 30, "warning"),
        )
        self.assertEqual(retry["error"], "down")
        self.assertEqual(_one(logs, "gene_mapping_loaded")["source"], "biomart")

    def test_mapping_in_s3_is_loaded_from_there(self):
        s3 = MagicMock()
        s3.head_object.return_value = {
            "LastModified": pd.Timestamp.now(tz="UTC").to_pydatetime()
        }
        s3.download_file.side_effect = lambda *a: self.MAPPING.to_csv(self.path)
        logs, _ = self._run(bucket="bkt", s3=s3)
        event = _one(logs, "gene_mapping_loaded")
        self.assertEqual((event["source"], event["records_out"]), ("s3", 2))
        self.assertTrue(event["uri"].startswith("s3://bkt/"))

    def test_mapping_missing_from_s3_is_the_reason_for_biomart(self):
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "no"}}, "HeadObject"
        )
        logs, _ = self._run(bucket="bkt", s3=s3, biomart=MagicMock(return_value=self.MAPPING))
        self.assertEqual(_one(logs, "gene_mapping_loaded")["reason"], "not_found")
        self.assertEqual(_all(logs, "gene_mapping_s3_error"), [])
        self.assertEqual(_one(logs, "gene_mapping_cached")["uri"].split("/")[2], "bkt")

    def test_s3_errors_are_warnings(self):
        s3 = MagicMock()
        s3.head_object.side_effect = NoCredentialsError()
        logs, _ = self._run(bucket="bkt", s3=s3, biomart=MagicMock(return_value=self.MAPPING))
        event = _one(logs, "gene_mapping_s3_error")
        self.assertEqual((event["error_type"], event["log_level"]), ("NoCredentialsError", "warning"))

    def test_cache_upload_failure_is_a_warning(self):
        s3 = MagicMock()
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "404", "Message": "no"}}, "HeadObject"
        )
        s3.upload_file.side_effect = RuntimeError("denied")
        logs, _ = self._run(bucket="bkt", s3=s3, biomart=MagicMock(return_value=self.MAPPING))
        event = _one(logs, "gene_mapping_cache_failed")
        self.assertEqual((event["error"], event["log_level"]), ("denied", "warning"))
        self.assertEqual(_all(logs, "gene_mapping_cached"), [])


class GeneIdMapsLoggingTestCase(unittest.TestCase):
    def test_a_miss_is_a_debug_event(self):
        names = pd.DataFrame({"ensembl_gene_id": ["E1"]}, index=["A"])
        with capture_logs() as logs:
            self.assertEqual(lu.map_gene_name_to_ensembl_ids("Z", names), [])
        event = _one(logs, "gene_id_unmapped")
        self.assertEqual(
            (event["reason"], event["id"], event["log_level"]),
            ("no_ensembl_id", "Z", "debug"),
        )

    def test_ids_collected_counts_unmapped_names(self):
        with capture_logs() as logs:
            ids = lu.collect_unique_gene_ensembl_ids(["BRCA1", "NOSUCHGENE", "ENSG0001.2"])
        self.assertEqual(ids, ["ENSG00000012048", "ENSG0001"])
        event = _one(logs, "gene_ids_collected")
        self.assertEqual(
            (event["kind"], event["records_in"], event["records_out"], event["records_rejected"]),
            ("ensembl", 3, 2, 1),
        )

    def test_entrez_ids_collected(self):
        with capture_logs() as logs:
            ids = lu.collect_unique_gene_entrez_ids(["TP53", "NOSUCHGENE"])
        self.assertEqual(ids, ["7157"])
        event = _one(logs, "gene_ids_collected")
        self.assertEqual((event["kind"], event["records_rejected"]), ("entrez", 1))


if __name__ == "__main__":
    unittest.main()
