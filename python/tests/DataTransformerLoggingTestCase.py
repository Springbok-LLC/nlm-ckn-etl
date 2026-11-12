"""DataTransformer.py logs events: the transform run, rejects, and main()."""

import base64
import gzip
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import DataTransformer  # noqa: E402
from DataTransformer import (  # noqa: E402
    CellxGeneTransformer,
    GeneTransformer,
    OpenTargetsTransformer,
)


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


def _gz(text):
    return base64.b64encode(gzip.compress(text.encode())).decode()


class TransformRunLoggingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _transformer(self, cls, raw):
        transformer = cls()
        transformer.input_path = self.dir / f"{cls.name}.json"
        transformer.input_path.write_text(json.dumps(raw))
        return transformer

    def test_cellxgene_rejects_entries_missing_data(self):
        raw = {
            "ok": {"dataset_json": {"title": "t"}, "collection_json": {"name": "c"}},
            "bad": {"dataset_json": {}, "collection_json": None},
        }
        transformer = self._transformer(CellxGeneTransformer, raw)
        with capture_logs() as logs:
            transformer.run()
        rejected = _one(logs, "records_rejected")
        self.assertEqual(
            (rejected["source"], rejected["reason"], rejected["records_rejected"]),
            ("cellxgene", "missing_dataset_or_collection", 1),
        )
        self.assertEqual(rejected["sample"], ["bad"])
        self.assertEqual(rejected["log_level"], "warning")
        done = _one(logs, "transform_finished")
        self.assertEqual(
            (done["records_in"], done["records_out"], done["records_rejected"]),
            (2, 1, 1),
        )
        self.assertEqual(done["input_path"], str(transformer.input_path))
        self.assertIsInstance(done["duration_ms"], int)

    def test_gene_parse_failure_and_empty_entries(self):
        raw = {
            "gene_entrez_ids": ["1", "2", "3"],
            "1": {"xml_gz_b64": _gz("<not-gene-xml>")},
            "2": {},
            "3": {"xml_gz_b64": "!!!not base64"},
        }
        transformer = self._transformer(GeneTransformer, raw)
        with capture_logs() as logs, patch.object(
            DataTransformer, "parse_xml_for_gene_id", side_effect=ValueError("bad xml")
        ):
            transformer.run()
        failed = _all(logs, "gene_parse_failed")
        self.assertEqual({e["gene_id"] for e in failed}, {"1", "3"})
        self.assertEqual(failed[0]["log_level"], "warning")
        reasons = {e["reason"]: e["records_rejected"] for e in _all(logs, "records_rejected")}
        self.assertEqual(reasons, {"parse_failed": 2, "no_data": 1})
        done = _one(logs, "transform_finished")
        # The id list is not a record.
        self.assertEqual(
            (done["records_in"], done["records_out"], done["records_rejected"]),
            (3, 0, 3),
        )

    def test_clean_run_logs_no_rejects(self):
        raw = {"gene_ensembl_ids": ["E1"], "E1": {"target": {"id": "E1"}}}
        transformer = self._transformer(OpenTargetsTransformer, raw)
        with capture_logs() as logs:
            transformer.run()
        self.assertEqual(_all(logs, "records_rejected"), [])
        done = _one(logs, "transform_finished")
        self.assertEqual((done["records_in"], done["records_out"]), (1, 1))

    def test_reject_state_resets_between_runs(self):
        raw = {"bad": {"dataset_json": {}, "collection_json": None}}
        transformer = self._transformer(CellxGeneTransformer, raw)
        with capture_logs():
            transformer.run()
        with capture_logs() as logs:
            transformer.run(force=True)
        self.assertEqual(_one(logs, "records_rejected")["records_rejected"], 1)

    def test_up_to_date_output_is_skipped(self):
        raw = {"E1": {"target": {"id": "E1"}}}
        transformer = self._transformer(OpenTargetsTransformer, raw)
        with capture_logs():
            transformer.run()
        later = time.time() + 10
        os.utime(transformer.output_path, (later, later))
        with capture_logs() as logs:
            transformer.run()
        event = _one(logs, "transform_skipped_up_to_date")
        self.assertEqual(event["source"], "opentargets")
        self.assertLess(event["input_mtime"], event["output_mtime"])
        self.assertEqual(_all(logs, "transform_finished"), [])


class NoH5adAssetLoggingTestCase(unittest.TestCase):
    def test_non_h5ad_assets_warn(self):
        raw = {
            "dv": {
                "dataset_json": {"assets": [{"filetype": "RDS", "url": "u"}]},
                "collection_json": {"name": "c"},
            }
        }
        with capture_logs() as logs:
            CellxGeneTransformer().transform(raw)
        event = _one(logs, "no_h5ad_asset")
        self.assertEqual((event["dataset_version_id"], event["filetypes"]), ("dv", ["RDS"]))
        self.assertEqual(event["log_level"], "warning")


class MainLoggingTestCase(unittest.TestCase):
    def _main(self, argv, transformers):
        class Run:
            run_name = "r"

        with (
            patch.object(sys, "argv", ["DataTransformer.py", *argv]),
            patch.object(DataTransformer, "TRANSFORMER_REGISTRY", transformers),
            patch.object(DataTransformer, "set_current_run"),
            patch.object(DataTransformer, "get_current_run", return_value=Run()),
            patch.object(DataTransformer, "configure_logging"),
            capture_logs() as logs,
        ):
            try:
                DataTransformer.main()
                error = None
            except Exception as exc:
                error = exc
        return logs, error

    def test_run_lifecycle(self):
        class Fake:
            name = "fake"

            def run(self, force=False):
                return {}

        logs, error = self._main(["--force"], [Fake()])
        self.assertIsNone(error)
        started = _one(logs, "run_started")
        self.assertEqual(
            (started["script"], started["run_name"], started["sources"], started["force"]),
            ("DataTransformer", "r", ["fake"], True),
        )
        self.assertEqual(_one(logs, "run_finished")["outcome"], "ok")

    def test_failure_logs_run_failed_and_reraises(self):
        class Broken:
            name = "broken"

            def run(self, force=False):
                raise FileNotFoundError("no raw results")

        logs, error = self._main([], [Broken()])
        self.assertIsInstance(error, FileNotFoundError)
        event = _one(logs, "run_failed")
        self.assertEqual((event["script"], event["error_type"]), ("DataTransformer", "FileNotFoundError"))


if __name__ == "__main__":
    unittest.main()
