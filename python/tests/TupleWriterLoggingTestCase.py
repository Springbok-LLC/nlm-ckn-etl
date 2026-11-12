"""The tuple writers and TupleWriterPipeline log events, not prints."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import CellxGeneTupleWriter  # noqa: E402
import GeneTupleWriter  # noqa: E402
import MappingTupleWriter  # noqa: E402
import OpenTargetsTupleWriter  # noqa: E402
import TupleWriterPipeline  # noqa: E402
import TupleWriterUtilities as utilities  # noqa: E402
import UniProtTupleWriter  # noqa: E402
from TupleWriterUtilities import RejectTally  # noqa: E402


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


class RejectTallyTestCase(unittest.TestCase):
    def test_one_line_per_reason_with_a_capped_sample(self):
        tally = RejectTally("gene")
        for i in range(8):
            tally.reject("no_data", str(i))
        tally.reject("unmapped_name", "x")
        tally.flag("deprecated_term", "CL_1")
        with capture_logs() as logs:
            tally.log()
        by_reason = {e["reason"]: e for e in _all(logs, "records_rejected")}
        self.assertEqual(by_reason["no_data"]["records_rejected"], 8)
        self.assertEqual(len(by_reason["no_data"]["sample"]), utilities.REJECT_SAMPLE_SIZE)
        self.assertEqual(by_reason["no_data"]["source"], "gene")
        self.assertEqual(by_reason["unmapped_name"]["records_rejected"], 1)
        flagged = _one(logs, "records_flagged")
        self.assertEqual((flagged["reason"], flagged["records_flagged"]), ("deprecated_term", 1))
        self.assertTrue(all(e["log_level"] == "warning" for e in logs))
        self.assertEqual(tally.n_rejected, 9)

    def test_nothing_is_logged_when_nothing_was_recorded(self):
        with capture_logs() as logs:
            RejectTally("gene").log()
        self.assertEqual(logs, [])


class UtilitiesLoggingTestCase(unittest.TestCase):
    def test_require_input_logs_before_raising(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "gene_transformed.json"
            with capture_logs() as logs:
                with self.assertRaises(FileNotFoundError) as ctx:
                    utilities.require_input(path, "Gene results", "gene_transformed")
        self.assertIn(f"Gene results not found at {path}", str(ctx.exception))
        event = _one(logs, "input_missing")
        self.assertEqual(
            (event["input"], event["path"], event["log_level"]),
            ("gene_transformed", str(path), "error"),
        )

    def test_require_input_passes_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.json"
            path.write_text("{}")
            with capture_logs() as logs:
                utilities.require_input(path, "X", "x")
        self.assertEqual(logs, [])

    def test_write_tuples_logs_and_returns_the_count(self):
        tuples = [("a", "b", "c")]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.json"
            with capture_logs() as logs:
                n = utilities.write_tuples(tuples, path)
            written = json.loads(path.read_text())["tuples"]
        self.assertEqual((n, len(written)), (1, 1))
        event = _one(logs, "tuples_written")
        self.assertEqual(
            (event["records_out"], event["deduped_removed"], event["output_path"]),
            (1, 0, str(path)),
        )
        self.assertIsInstance(event["duration_ms"], int)

    def test_unparseable_string_list_warns(self):
        with capture_logs() as logs:
            self.assertEqual(utilities.parse_string_list("['a'"), [])
        event = _one(logs, "string_list_parse_failed")
        self.assertEqual((event["value"], event["log_level"]), ("['a'", "warning"))
        self.assertEqual(event["error_type"], "SyntaxError")


class WriterRejectsTestCase(unittest.TestCase):
    def test_uniprot_without_data(self):
        tally = RejectTally("uniprot")
        UniProtTupleWriter.create_tuples(
            {"protein_accessions": ["P1"], "P1": {}}, tally
        )
        self.assertEqual(tally._rejected, {"no_data": ["P1"]})

    def test_gene_without_data_or_name(self):
        tally = RejectTally("gene")
        with patch.object(
            GeneTupleWriter,
            "get_gene_entrez_id_to_names_map",
            return_value=pd.DataFrame(
                {"external_gene_name": []}, index=pd.Index([], name="entrezgene_id")
            ),
        ):
            GeneTupleWriter.create_tuples(
                {"gene_entrez_ids": ["1", "2"], "1": {}, "2": {"Gene_ID": "2"}}, tally
            )
        self.assertEqual(tally._rejected, {"no_data": ["1"], "unmapped_name": ["2"]})

    def test_cellxgene_dataset_in_no_summary(self):
        tally = RejectTally("cellxgene")
        CellxGeneTupleWriter.create_tuples(
            {"dv-old": {"Dataset_name": "n"}}, {"dv-new": {"heart"}}, None, tally
        )
        self.assertEqual(tally._rejected, {"not_in_summary": ["dv-old"]})

    def test_mondo_deprecated_term_is_a_reject(self):
        tally = RejectTally("opentargets")
        with patch.object(OpenTargetsTupleWriter, "DEPRECATED_TERMS", {"MONDO_1"}):
            term = OpenTargetsTupleWriter.get_mondo_term("MONDO_1", None, tally)
        self.assertIsNone(term)
        self.assertEqual(tally._rejected, {"deprecated_term": ["MONDO_1"]})

    def test_opentargets_unmapped_ids(self):
        tally = RejectTally("opentargets")
        with (
            patch.object(
                OpenTargetsTupleWriter,
                "get_gene_ensembl_id_to_names_map",
                return_value=pd.DataFrame(
                    {"external_gene_name": ["G1"]}, index=["E1"]
                ),
            ),
            patch.object(
                OpenTargetsTupleWriter,
                "get_gene_name_to_entrez_ids_map",
                return_value=pd.DataFrame({"entrezgene_id": []}, index=[]),
            ),
            patch.object(OpenTargetsTupleWriter, "get_efo_to_mondo_map"),
        ):
            OpenTargetsTupleWriter.create_tuples(
                {"gene_ensembl_ids": ["E1", "E2"]}, {}, {}, tally
            )
        self.assertEqual(
            tally._rejected,
            {"unmapped_entrez": ["G1"], "unmapped_ensembl": ["E2"]},
        )

    def _mapping_rows(self, **overrides):
        row = {
            "dataset_version_id": "dv-001",
            "cluster_name": "T-Cell",
            "skos": "skos:broadMatch",
            "manual_mapped_cid": "CL:0000084",
            "cell_ontology_id": "CL:0000236",
            "clusterName": "T-Cell",
            "clusterSize": 1718,
            "NSForest_markers": "['TP53']",
            "binary_genes": "['TP53']",
            "uuid": "abc",
        }
        row.update(overrides)
        return pd.DataFrame([row])

    def _mapping(self, rows):
        tally = RejectTally("mapping")
        with patch.object(
            MappingTupleWriter,
            "get_gene_ensembl_id_to_names_map",
            return_value=pd.DataFrame({"external_gene_name": []}, index=[]),
        ):
            tuples = MappingTupleWriter.create_tuples(rows, pd.DataFrame(), None, None, tally)
        return tuples, tally

    def test_mapping_cluster_without_cl_id(self):
        _, tally = self._mapping(
            self._mapping_rows(manual_mapped_cid="", cell_ontology_id="")
        )
        self.assertEqual(tally._rejected, {"no_cl_id": ["T-Cell"]})

    def test_mapping_unexpected_cl_curie(self):
        _, tally = self._mapping(self._mapping_rows(manual_mapped_cid="CL:12"))
        self.assertEqual(tally._rejected, {"bad_cl_curie": ["CL:12"]})

    def test_mapping_deprecated_term_is_kept_and_flagged(self):
        with patch.object(MappingTupleWriter, "DEPRECATED_TERMS", {"CL_0000084"}):
            tuples, tally = self._mapping(self._mapping_rows())
        self.assertGreater(len(tuples), 0)
        self.assertEqual(tally._flagged, {"deprecated_term": ["CL_0000084"]})
        self.assertEqual(tally.n_rejected, 0)


class WriterMainLoggingTestCase(unittest.TestCase):
    def test_uniprot_main_logs_start_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            external = Path(d) / "external"
            external.mkdir()
            (external / "uniprot_transformed.json").write_text(
                json.dumps({"protein_accessions": ["P1", "P2"], "P1": {}, "P2": {}})
            )
            run = SimpleNamespace(external_dir=external)
            with patch.object(UniProtTupleWriter, "get_current_run", return_value=run):
                with capture_logs() as logs:
                    n = UniProtTupleWriter.main()
        self.assertEqual(n, 0)
        started = _one(logs, "tuple_writer_started")
        self.assertEqual((started["source"], started["records_in"]), ("uniprot", 2))
        self.assertEqual(_one(logs, "records_rejected")["records_rejected"], 2)
        self.assertEqual(_all(logs, "tuples_written"), [])


class PipelineLoggingTestCase(unittest.TestCase):
    class _Writer:
        def __init__(self, n=0, error=None):
            self.n, self.error = n, error

        def main(self):
            if self.error:
                raise self.error
            return self.n

    def _main(self, writers):
        with (
            patch.object(sys, "argv", ["TupleWriterPipeline.py"]),
            patch.object(TupleWriterPipeline, "WRITERS", writers),
            patch.object(TupleWriterPipeline, "set_current_run"),
            patch.object(
                TupleWriterPipeline,
                "get_current_run",
                return_value=SimpleNamespace(run_name="r"),
            ),
            patch.object(TupleWriterPipeline, "configure_logging"),
            capture_logs() as logs,
        ):
            try:
                TupleWriterPipeline.main()
                error = None
            except Exception as exc:
                error = exc
        return logs, error

    def test_lifecycle_and_per_writer_counts(self):
        logs, error = self._main(
            [("a", self._Writer(3)), ("b", self._Writer(None))]
        )
        self.assertIsNone(error)
        started = _one(logs, "run_started")
        self.assertEqual(
            (started["script"], started["run_name"], started["writers"]),
            ("TupleWriterPipeline", "r", ["a", "b"]),
        )
        self.assertEqual(_one(logs, "tuple_writers_finished")["tuples_out"], {"a": 3, "b": 0})
        self.assertEqual(_one(logs, "run_finished")["outcome"], "ok")

    def test_a_failing_writer_fails_the_run(self):
        logs, error = self._main(
            [("a", self._Writer(error=FileNotFoundError("gone")))]
        )
        self.assertIsInstance(error, FileNotFoundError)
        event = _one(logs, "run_failed")
        self.assertEqual((event["script"], event["error_type"]), ("TupleWriterPipeline", "FileNotFoundError"))
        self.assertEqual(_all(logs, "tuple_writers_finished"), [])

    def test_the_writers_run_in_the_documented_order(self):
        self.assertEqual(
            [name for name, _ in TupleWriterPipeline.WRITERS],
            ["nsforest", "mapping", "cellxgene", "opentargets", "gene", "uniprot"],
        )


if __name__ == "__main__":
    unittest.main()
