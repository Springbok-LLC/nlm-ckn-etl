"""fetch.py logs events: config, phase boundaries, cache cleanup, source status."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from structlog.testing import capture_logs

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import fetch  # noqa: E402


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


class RetryEmptyTestCase(unittest.TestCase):
    def _run(self, files):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            for name, content in files.items():
                (d / name).write_text(content)
            with patch.object(fetch, "_external_dir", return_value=d):
                with capture_logs() as logs:
                    fetch.retry_failed_cache_entries.fn("x")
            kept = {n: (d / n).read_text() for n in files}
        return logs, kept

    def test_counts_removed_entries_and_files_touched(self):
        data = {"a": {}, "b": {"x": 1}, "gene_entrez_ids": {}, "c": {}}
        logs, kept = self._run({"one.json": json.dumps(data), "two.json": "{}"})
        event = _one(logs, "cache_entries_cleared")
        self.assertEqual((event["records_out"], event["files_touched"]), (2, 1))
        self.assertEqual(set(json.loads(kept["one.json"])), {"b", "gene_entrez_ids"})

    def test_unreadable_file_warns_and_continues(self):
        logs, _ = self._run({"bad.json": "{not json", "ok.json": '{"a": {}}'})
        event = _one(logs, "cache_file_unreadable")
        self.assertEqual(event["file"], "bad.json")
        self.assertEqual(event["error_type"], "JSONDecodeError")
        self.assertEqual(_one(logs, "cache_entries_cleared")["records_out"], 1)

    def test_missing_directory_logs_zero(self):
        with patch.object(fetch, "_external_dir", return_value=Path("/no/such/dir")):
            with capture_logs() as logs:
                fetch.retry_failed_cache_entries.fn("x")
        self.assertEqual(_one(logs, "cache_entries_cleared")["records_out"], 0)


class RecordArtifactTestCase(unittest.TestCase):
    def _run(self, status=None, status_text=None):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "cellxgene.json").write_text("{}")
            if status is not None:
                (d / "fetch-status.json").write_text(json.dumps(status))
            if status_text is not None:
                (d / "fetch-status.json").write_text(status_text)
            with patch.object(fetch, "_external_dir", return_value=d):
                with patch.object(fetch, "REPO_ROOT", d):
                    with patch.object(fetch, "_git_commit", return_value="abc"):
                        with patch.object(fetch, "create_markdown_artifact"):
                            with capture_logs() as logs:
                                outcomes = fetch.record_fetch_artifact.fn("x", True)
        return logs, outcomes

    def test_fetch_info_written_counts_files(self):
        logs, _ = self._run()
        event = _one(logs, "fetch_info_written")
        self.assertEqual((event["files_ok"], event["files_missing"]), (1, 7))
        self.assertEqual((event["validated"], event["commit"]), (True, "abc"))

    def test_one_status_line_per_source_with_age_and_level(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        status = {
            "cellxgene": {"last_outcome": "ok", "last_success_at": recent},
            "gene": {"last_outcome": "failed"},
            "uniprot": {"last_outcome": "partial"},
            "opentargets": {"last_outcome": "skipped"},
        }
        logs, outcomes = self._run(status)
        by_source = {e["source"]: e for e in _all(logs, "fetch_source_status")}
        self.assertEqual(set(by_source), set(fetch._SOURCE_NAMES))
        self.assertEqual(by_source["cellxgene"]["log_level"], "info")
        self.assertAlmostEqual(by_source["cellxgene"]["age_hours"], 2.0, delta=0.2)
        self.assertEqual(by_source["gene"]["log_level"], "warning")
        self.assertEqual(by_source["uniprot"]["log_level"], "warning")
        self.assertEqual(by_source["opentargets"]["log_level"], "info")
        self.assertEqual(by_source["hubmap"]["outcome"], "unknown")
        self.assertEqual(by_source["hubmap"]["log_level"], "warning")
        self.assertEqual(outcomes["gene"], "failed")

    def test_unreadable_status_warns_with_error_type(self):
        logs, outcomes = self._run(status_text="{broken")
        event = _one(logs, "fetch_status_unreadable")
        self.assertEqual(event["error_type"], "JSONDecodeError")
        self.assertEqual(set(outcomes.values()), {"unknown"})

    def test_source_version_logged_only_when_present(self):
        status = {"opentargets": {"last_outcome": "ok", "source_version": "25.06"}}
        logs, _ = self._run(status)
        event = _one(logs, "source_versions_resolved")
        self.assertEqual(
            (event["source"], event["source_version"]), ("opentargets", "25.06")
        )


class GitCommitTestCase(unittest.TestCase):
    def test_failure_warns_and_returns_unknown(self):
        with patch.object(fetch.subprocess, "check_output", side_effect=OSError("x")):
            with capture_logs() as logs:
                self.assertEqual(fetch._git_commit(), "unknown")
        self.assertEqual(_one(logs, "git_commit_unavailable")["error_type"], "OSError")


class FlowTestCase(unittest.TestCase):
    def _patches(self, outcomes=None, validate_error=None):
        outcomes = outcomes or {n: "ok" for n in fetch._SOURCE_NAMES}
        names = (
            "sync_results_from_s3 validate_release_dir sync_external_from_s3 "
            "clean_empty_external_files retry_failed_cache_entries "
            "fetch_external_api_results transform_external_api_results "
            "sync_external_to_s3_staging promote_external_staging"
        ).split()
        stack = [patch.object(fetch, n) for n in names]
        stack.append(
            patch.object(fetch, "validate_external_files", side_effect=validate_error)
        )
        stack.append(patch.object(fetch, "record_fetch_artifact", return_value=outcomes))
        stack.append(patch.object(fetch, "_git_commit", return_value="abc"))
        stack.append(patch.object(fetch, "_fetch_code_hash", return_value="h" * 16))
        stack.append(patch.object(fetch, "S3_BUCKET", ""))
        return stack

    def _run(self, outcomes=None, validate_error=None, **kwargs):
        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in self._patches(outcomes, validate_error):
                stack.enter_context(p)
            with capture_logs() as logs:
                try:
                    fetch.nlm_ckn_fetch.fn(
                        ncbi_email="e", ncbi_api_key="k", run_name="x", **kwargs
                    )
                    err = None
                except Exception as exc:
                    err = exc
        return logs, err

    def test_success_logs_config_then_phase_boundaries_with_counts(self):
        outcomes = {
            "cellxgene": "ok",
            "opentargets": "ok",
            "gene": "partial",
            "uniprot": "skipped",
            "hubmap": "failed",
        }
        logs, err = self._run(outcomes, force=True)
        self.assertIsNone(err)
        names = [e["event"] for e in logs]
        self.assertLess(names.index("config_resolved"), names.index("phase_started"))
        config = _one(logs, "config_resolved")
        self.assertEqual(config["run_name"], "x")
        self.assertFalse(config["s3_enabled"])
        self.assertTrue(config["force"])
        self.assertEqual(config["fetch_code_hash"], "h" * 16)
        done = _one(logs, "phase_finished")
        self.assertEqual(done["phase"], "fetch")
        self.assertEqual(
            (
                done["sources_ok"],
                done["sources_partial"],
                done["sources_skipped"],
                done["sources_failed"],
            ),
            (2, 1, 1, 1),
        )
        self.assertIsInstance(done["duration_ms"], int)
        self.assertEqual(_all(logs, "run_failed"), [])

    def test_validation_failure_logs_run_failed_and_reraises(self):
        logs, err = self._run(validate_error=RuntimeError("bad cache"))
        self.assertIsInstance(err, RuntimeError)
        event = _one(logs, "run_failed")
        self.assertEqual(event["phase"], "fetch")
        self.assertEqual(event["error_type"], "RuntimeError")
        self.assertEqual(event["error"], "bad cache")
        self.assertEqual(_all(logs, "phase_finished"), [])

    def test_missing_credentials_log_run_failed(self):
        with patch.dict("os.environ", {"NCBI_EMAIL": "", "NCBI_API_KEY": ""}):
            with capture_logs() as logs:
                with self.assertRaises(RuntimeError):
                    fetch.nlm_ckn_fetch.fn()
        self.assertEqual(_one(logs, "run_failed")["error_type"], "RuntimeError")


if __name__ == "__main__":
    unittest.main()
