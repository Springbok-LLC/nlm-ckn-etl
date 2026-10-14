"""The second half of pipeline.py logs events: validation, artifacts, phases."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import pipeline  # noqa: E402


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


def _names(logs):
    return [e["event"] for e in logs]


class ValidationTestCase(unittest.TestCase):
    def test_release_dir_validated_counts_nsforest_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            results = root / "data" / "results-r"
            results.mkdir(parents=True)
            (results / "results_ensg_a.csv").write_text("x")
            (results / "results_ensg_b.csv").write_text("x")
            (results / "hubmap_urls.txt").write_text("u")
            with patch.object(pipeline, "REPO_ROOT", root):
                with capture_logs() as logs:
                    pipeline.validate_release_dir.fn("r")
        self.assertEqual(_one(logs, "release_dir_validated")["records_out"], 2)

    def _tuples(self, files):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tuples = root / "data" / "tuples-r"
            tuples.mkdir(parents=True)
            for name, text in files.items():
                (tuples / name).write_text(text)
            with patch.object(pipeline, "REPO_ROOT", root):
                with capture_logs() as logs:
                    pipeline.validate_tuple_files.fn("r")
        return logs

    def test_tuple_files_validated_with_totals(self):
        logs = self._tuples({"a.json": "[1, 2, 3]", "b.json": "[4, 5]"})
        event = _one(logs, "tuple_files_validated")
        self.assertEqual((event["records_out"], event["bytes"]), (2, 15))
        self.assertEqual(_all(logs, "tuple_file_empty"), [])

    def test_empty_tuple_files_warn(self):
        logs = self._tuples({"a.json": "[1, 2, 3]", "empty.json": "[]", "none.json": ""})
        warned = {e["file"]: e for e in _all(logs, "tuple_file_empty")}
        self.assertEqual(set(warned), {"empty.json", "none.json"})
        self.assertEqual(warned["empty.json"]["log_level"], "warning")
        self.assertEqual(_one(logs, "tuple_files_validated")["records_out"], 3)


class ArtifactSyncTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(pipeline, "S3_BUCKET", "bkt")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_artifacts_are_named_on_transfers(self):
        with tempfile.TemporaryDirectory() as d:
            absent = Path(d) / "absent"
            with patch.object(pipeline, "_s3_upload_tar") as up, patch.object(
                pipeline, "_s3_download_tar"
            ) as down, patch.object(pipeline, "_s3_sync") as sync, patch.object(
                pipeline, "REPO_ROOT", Path(d)
            ):
                pipeline.sync_tuples_to_s3.fn("r")
                pipeline.sync_baseline_dump_to_s3.fn(absent, "k")
                pipeline.sync_results_dump_to_s3.fn(absent, "k", "r")
                pipeline.sync_baseline_dump_from_s3.fn(absent, "k")
                pipeline.sync_results_dump_from_s3.fn(absent, "k", "r")
                pipeline.sync_results_from_s3.fn("r")
        self.assertEqual(
            [c.kwargs["artifact"] for c in up.call_args_list],
            ["tuples", "baseline_dump", "results_dump"],
        )
        self.assertEqual(
            [c.kwargs["artifact"] for c in down.call_args_list],
            ["baseline_dump", "results_dump"],
        )
        self.assertEqual(sync.call_args.kwargs["artifact"], "release_results")

    def test_local_dump_is_a_cache_hit(self):
        with tempfile.TemporaryDirectory() as d:
            baseline = Path(d) / "arangodump-baseline-k"
            results = Path(d) / "arangodump-results-k-r"
            baseline.mkdir()
            results.mkdir()
            with patch.object(pipeline, "_s3_download_tar") as down:
                with capture_logs() as logs:
                    pipeline.sync_baseline_dump_from_s3.fn(baseline, "k")
                    pipeline.sync_results_dump_from_s3.fn(results, "k", "r")
        down.assert_not_called()
        hits = {e["artifact"]: e for e in _all(logs, "artifact_cache_hit")}
        self.assertEqual(set(hits), {"baseline_dump", "results_dump"})
        self.assertEqual(hits["results_dump"]["run_name"], "r")
        self.assertEqual(hits["baseline_dump"]["jar_key"], "k")

    def test_local_mode_is_silent(self):
        with patch.object(pipeline, "S3_BUCKET", ""):
            with capture_logs() as logs:
                pipeline.sync_tuples_to_s3.fn("r")
                pipeline.sync_baseline_dump_from_s3.fn(Path("x"), "k")
                pipeline.sync_results_from_s3.fn("r")
        self.assertEqual(logs, [])


class AnalyzersTestCase(unittest.TestCase):
    def _run(self, delete_error=None):
        adb = MagicMock()
        adb.delete_view.__name__ = "delete_view"
        adb.delete_analyzers.__name__ = "delete_analyzers"
        adb.delete_view.side_effect = delete_error
        with patch.dict(sys.modules, {"ArangoDbUtilities": adb}):
            with capture_logs() as logs:
                pipeline.create_analyzers_and_views.fn("pw", "db1")
        return adb, logs

    def test_created_event(self):
        adb, logs = self._run()
        event = _one(logs, "analyzers_and_views_created")
        self.assertEqual(event["database"], "db1")
        self.assertIsInstance(event["duration_ms"], int)
        adb.create_view.assert_called_once()

    def test_delete_failure_is_debug_with_error_type(self):
        _, logs = self._run(delete_error=ConnectionError("down"))
        event = _one(logs, "analyzers_delete_skipped")
        self.assertEqual(event["log_level"], "debug")
        self.assertEqual(event["error_type"], "ConnectionError")
        self.assertEqual(event["step"], "delete_view")
        self.assertIn("analyzers_and_views_created", _names(logs))


class PromoteTestCase(unittest.TestCase):
    def _run(self, *, obo=False, external=False, fetch_info=None):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            golden = root / "golden"
            golden.mkdir()
            ext = root / "data" / "external-r"
            if external:
                ext.mkdir(parents=True)
            if fetch_info is not None:
                ext.mkdir(parents=True, exist_ok=True)
                (ext / "fetch-info.json").write_text(fetch_info)
            if obo:
                (root / "data" / "obo").mkdir(parents=True)
            with ExitStack() as stack:
                stack.enter_context(patch.object(pipeline, "S3_BUCKET", "bkt"))
                stack.enter_context(patch.object(pipeline, "S3_KMS_KEY_ID", ""))
                stack.enter_context(patch.object(pipeline, "REPO_ROOT", root))
                stack.enter_context(patch.object(pipeline, "_external_dir", return_value=ext))
                stack.enter_context(patch.object(pipeline, "_git_commit", return_value="abc"))
                up = stack.enter_context(
                    patch.object(
                        pipeline,
                        "_s3_upload_tar",
                        return_value={"files": 1, "members_skipped": 0, "bytes": 100},
                    )
                )
                s3 = stack.enter_context(patch.object(pipeline.boto3, "client"))
                with capture_logs() as logs:
                    pipeline.promote_to_production.fn(golden, "pw", "jk", "r")
            size = (root / "build-info.txt").stat().st_size
        return logs, up, s3, size

    def test_missing_directories_warn_and_are_skipped(self):
        logs, up, _, size = self._run()
        skipped = {e["artifact"]: e for e in _all(logs, "artifact_upload_skipped")}
        self.assertEqual(set(skipped), {"obo", "external"})
        self.assertEqual(skipped["obo"]["reason"], "missing_directory")
        self.assertEqual(skipped["obo"]["log_level"], "warning")
        done = _one(logs, "production_promotion_finished")
        self.assertEqual((done["objects"], done["bytes"]), (2, 100 + size))
        self.assertEqual(done["run_name"], "r")
        self.assertEqual(
            [c.kwargs["artifact"] for c in up.call_args_list], ["golden"]
        )

    def test_everything_present_uploads_all_artifacts(self):
        logs, up, _, size = self._run(obo=True, external=True)
        self.assertEqual(_all(logs, "artifact_upload_skipped"), [])
        self.assertEqual(
            [c.kwargs["artifact"] for c in up.call_args_list],
            ["golden", "obo", "external"],
        )
        done = _one(logs, "production_promotion_finished")
        self.assertEqual((done["objects"], done["bytes"]), (4, 300 + size))

    def test_unreadable_fetch_info_warns_and_continues(self):
        logs, _, _, _ = self._run(fetch_info="{broken")
        event = _one(logs, "fetch_info_unreadable")
        self.assertEqual(
            (event["source"], event["error_type"]), ("local", "JSONDecodeError")
        )
        self.assertIn("production_promotion_finished", _names(logs))

    def test_missing_version_files_warn_per_component(self):
        logs, _, _, _ = self._run()
        components = {e["component"] for e in _all(logs, "build_version_unavailable")}
        self.assertEqual(components, {"java", "python"})


class FlowTestCase(unittest.TestCase):
    _TASKS = (
        "stop_arangodb start_arangodb require_arangodb download_ontologies "
        "slim_ontologies build_ontology_graph dump_arangodb "
        "export_graphs_and_analyzers import_graphs_from_sidecar restore_arangodb "
        "validate_release_dir sync_results_from_s3 write_tuples sync_tuples_to_s3 "
        "validate_tuple_files sync_baseline_dump_to_s3 sync_baseline_dump_from_s3 "
        "sync_results_dump_to_s3 sync_results_dump_from_s3 build_results_graph "
        "build_induced_subgraph create_analyzers_and_views promote_to_production "
        "sync_external_from_s3 validate_external_files post_github_deployment_status "
        "_wipe_arangodb_data _set_arango_port"
    ).split()

    def _run(self, root, *, overrides=None, **flags):
        mocks = {}
        with ExitStack() as stack:
            for name in self._TASKS:
                mocks[name] = stack.enter_context(patch.object(pipeline, name))
            for name, value in (overrides or {}).items():
                mocks[name].side_effect = value
            stack.enter_context(patch.object(pipeline, "REPO_ROOT", root))
            stack.enter_context(patch.object(pipeline, "ARANGO_DB_IS_LOCAL", True))
            stack.enter_context(patch.object(pipeline, "ensure_jar", return_value="jk"))
            stack.enter_context(
                patch.object(pipeline, "_get_or_create_arango_password", return_value="pw")
            )
            stack.enter_context(patch.object(pipeline, "_git_commit", return_value="abc"))
            stack.enter_context(
                patch.object(pipeline, "_fetch_code_hash", return_value="h" * 16)
            )
            stack.enter_context(patch.object(pipeline, "S3_BUCKET", ""))
            with capture_logs() as logs:
                try:
                    pipeline.nlm_ckn_etl.fn(run_name="r", **flags)
                    err = None
                except Exception as exc:
                    err = exc
        return logs, err

    def test_ontology_phase_logs_config_then_started_and_finished(self):
        with tempfile.TemporaryDirectory() as d:
            logs, err = self._run(Path(d), run_ontology=True)
        self.assertIsNone(err)
        names = _names(logs)
        self.assertLess(names.index("config_resolved"), names.index("phase_started"))
        config = _one(logs, "config_resolved")
        self.assertEqual(config["run_name"], "r")
        self.assertEqual(config["jar_key"], "jk")
        self.assertTrue(config["run_ontology"])
        self.assertFalse(config["run_results"])
        self.assertEqual(config["git_commit"], "abc")
        self.assertNotIn("batch_job_id", config)
        started = _one(logs, "phase_started")
        done = _one(logs, "phase_finished")
        self.assertEqual((started["phase"], done["phase"]), ("ontology", "ontology"))
        self.assertEqual(done["jar_key"], "jk")
        self.assertEqual(done["dump"], "arangodump-baseline-jk")
        self.assertIsInstance(done["duration_ms"], int)
        finished = _one(logs, "run_finished")
        self.assertEqual(finished["outcome"], "success")
        self.assertEqual(finished["phases_run"], ["ontology"])
        self.assertEqual(finished["phases_skipped"], [])
        self.assertEqual(set(finished["phase_durations_ms"]), {"ontology"})

    def test_batch_job_id_logged_in_config(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict("os.environ", {"AWS_BATCH_JOB_ID": "job-1"}):
                logs, _ = self._run(Path(d), run_ontology=True)
        self.assertEqual(_one(logs, "config_resolved")["batch_job_id"], "job-1")

    def test_existing_dumps_skip_their_phases(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data = root / "data"
            (data / "arangodump-baseline-jk").mkdir(parents=True)
            (data / "arangodump-results-jk-r").mkdir()
            (data / "arangodump-golden-r").mkdir()
            logs, err = self._run(
                root, run_ontology=True, run_results=True, run_archive=True
            )
        self.assertIsNone(err)
        skipped = {e["phase"]: e for e in _all(logs, "phase_skipped")}
        self.assertEqual(set(skipped), {"ontology", "results", "archive"})
        self.assertEqual(skipped["results"]["reason"], "dump_exists")
        self.assertFalse(skipped["results"]["force"])
        self.assertEqual(_all(logs, "phase_started"), [])
        finished = _one(logs, "run_finished")
        self.assertEqual(finished["phases_run"], [])
        self.assertEqual(finished["phases_skipped"], ["ontology", "results", "archive"])

    def test_results_phase_reports_tuple_counts(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "data" / "arangodump-baseline-jk").mkdir(parents=True)

            def write_tuples(*a, **k):
                (root / "data" / "tuples-r" / "a.json").write_text("[1, 2]")

            logs, err = self._run(
                root, run_results=True, overrides={"write_tuples": write_tuples}
            )
        self.assertIsNone(err)
        done = _one(logs, "phase_finished")
        self.assertEqual(done["phase"], "results")
        self.assertEqual((done["tuple_files"], done["tuple_bytes"]), (1, 6))

    def test_failure_logs_run_failed_with_phase_and_no_finish(self):
        with tempfile.TemporaryDirectory() as d:
            # Results phase without a baseline dump raises inside the phase.
            logs, err = self._run(Path(d), run_results=True)
        self.assertIsInstance(err, RuntimeError)
        event = _one(logs, "run_failed")
        self.assertEqual(event["phase"], "results")
        self.assertEqual(event["error_type"], "RuntimeError")
        self.assertIn("Baseline dump not found", event["error"])
        self.assertIsInstance(event["duration_ms"], int)
        self.assertEqual(_all(logs, "phase_finished"), [])
        self.assertEqual(_all(logs, "run_finished"), [])

    def test_no_stage_flags_logs_run_failed_without_a_phase(self):
        with tempfile.TemporaryDirectory() as d:
            logs, err = self._run(Path(d))
        self.assertIsInstance(err, ValueError)
        event = _one(logs, "run_failed")
        self.assertIsNone(event["phase"])
        self.assertEqual(_all(logs, "config_resolved"), [])


class HelpersTestCase(unittest.TestCase):
    def test_failing_task_names_the_innermost_function_in_the_module(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(pipeline, "REPO_ROOT", Path(d)):
                try:
                    pipeline.validate_tuple_files.fn("r")
                except FileNotFoundError as exc:
                    self.assertEqual(pipeline._failing_task(exc), "validate_tuple_files")

    def test_failing_task_is_none_for_an_error_raised_elsewhere(self):
        try:
            json.loads("{")
        except ValueError as exc:
            self.assertIsNone(pipeline._failing_task(exc))

    def test_source_versions_logged_only_when_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            ext = Path(d)
            (ext / "fetch-status.json").write_text(
                json.dumps(
                    {
                        "uniprot": {"last_outcome": "ok", "source_version": "2025_03"},
                        "gene": {"last_outcome": "ok"},
                    }
                )
            )
            with patch.object(pipeline, "_external_dir", return_value=ext):
                with capture_logs() as logs:
                    pipeline._log_source_versions("r")
        event = _one(logs, "source_versions_resolved")
        self.assertEqual((event["source"], event["source_version"]), ("uniprot", "2025_03"))

    def test_unreadable_status_warns(self):
        with tempfile.TemporaryDirectory() as d:
            ext = Path(d)
            (ext / "fetch-status.json").write_text("{x")
            with patch.object(pipeline, "_external_dir", return_value=ext):
                with capture_logs() as logs:
                    pipeline._log_source_versions("r")
        self.assertEqual(_one(logs, "fetch_status_unreadable")["error_type"], "JSONDecodeError")

    def test_dir_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sub").mkdir()
            (root / "a").write_bytes(b"123")
            (root / "sub" / "b").write_bytes(b"45")
            self.assertEqual(pipeline._dir_bytes(root), 5)
            self.assertEqual(pipeline._dir_bytes(root / "missing"), 0)


if __name__ == "__main__":
    unittest.main()
