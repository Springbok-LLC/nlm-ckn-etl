"""The external-cache helpers log structured events, not free text."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

from structlog.testing import capture_logs  # noqa: E402

import _common  # noqa: E402

_RAW = ("cellxgene", "opentargets", "gene", "uniprot")


def _events(logs, name):
    return [e for e in logs if e["event"] == name]


class FetchCacheDecisionTestCase(unittest.TestCase):
    def _write_info(self, d, *, age_hours=1.0, code_hash="MATCH", fetched_at=None):
        if code_hash == "MATCH":
            code_hash = _common._fetch_code_hash()
        if fetched_at is None:
            fetched_at = (
                datetime.now(timezone.utc) - timedelta(hours=age_hours)
            ).isoformat()
        (d / "fetch-info.json").write_text(
            json.dumps({"fetched_at": fetched_at, "fetch_code_hash": code_hash})
        )

    def _decide(self, d, max_age=672.0):
        with capture_logs() as logs:
            with patch.object(_common, "S3_BUCKET", ""), patch.object(
                _common, "_external_dir", return_value=d
            ):
                result = _common.should_force_fetch("test-run", max_age)
        return result, logs

    def _decision(self, logs):
        (event,) = _events(logs, "fetch_cache_decision")
        return event

    def test_fresh_marker_resumes(self):
        with tempfile.TemporaryDirectory() as t:
            self._write_info(Path(t), age_hours=2.0)
            result, logs = self._decide(Path(t))
        event = self._decision(logs)
        self.assertFalse(result)
        self.assertEqual(event["decision"], "resume")
        self.assertEqual(event["reason"], "fresh")
        self.assertEqual(event["source"], "local")
        self.assertEqual(event["threshold_hours"], 672.0)
        self.assertAlmostEqual(event["age_hours"], 2.0, delta=0.1)
        self.assertEqual(event["log_level"], "info")

    def test_expired_marker_forces(self):
        with tempfile.TemporaryDirectory() as t:
            self._write_info(Path(t), age_hours=1000.0)
            result, logs = self._decide(Path(t))
        event = self._decision(logs)
        self.assertTrue(result)
        self.assertEqual((event["decision"], event["reason"]), ("force", "expired"))
        self.assertGreater(event["age_hours"], event["threshold_hours"])

    def test_changed_code_forces_and_reports_both_hashes(self):
        with tempfile.TemporaryDirectory() as t:
            self._write_info(Path(t), code_hash="0000deadbeef0000")
            result, logs = self._decide(Path(t))
        event = self._decision(logs)
        self.assertTrue(result)
        self.assertEqual(event["reason"], "code_changed")
        self.assertEqual(event["cached_hash"], "0000deadbeef0000")
        self.assertEqual(event["current_hash"], _common._fetch_code_hash())

    def test_missing_marker_resumes(self):
        with tempfile.TemporaryDirectory() as t:
            result, logs = self._decide(Path(t))
        event = self._decision(logs)
        self.assertFalse(result)
        self.assertEqual((event["decision"], event["reason"]), ("resume", "no_marker"))
        self.assertEqual(_events(logs, "fetch_info_unreadable"), [])

    def test_malformed_timestamp_resumes_at_warn(self):
        with tempfile.TemporaryDirectory() as t:
            self._write_info(Path(t), fetched_at="not-a-timestamp")
            result, logs = self._decide(Path(t))
        event = self._decision(logs)
        self.assertFalse(result)
        self.assertEqual(event["reason"], "invalid_marker")
        self.assertEqual(event["log_level"], "warning")

    def test_unparseable_marker_logs_unreadable_and_resumes(self):
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "fetch-info.json").write_text("{not json")
            result, logs = self._decide(Path(t))
        (unreadable,) = _events(logs, "fetch_info_unreadable")
        self.assertFalse(result)
        self.assertEqual(unreadable["source"], "local")
        self.assertEqual(unreadable["error_type"], "JSONDecodeError")
        self.assertEqual(self._decision(logs)["reason"], "no_marker")

    def test_s3_read_failure_logs_unreadable_with_s3_source(self):
        client = MagicMock()
        client.download_file.side_effect = RuntimeError("denied")
        with capture_logs() as logs:
            with patch.object(_common, "S3_BUCKET", "bkt"), patch.object(
                _common.boto3, "client", return_value=client
            ):
                result = _common.should_force_fetch("test-run")
        (unreadable,) = _events(logs, "fetch_info_unreadable")
        self.assertFalse(result)
        self.assertEqual(unreadable["source"], "s3")
        self.assertEqual(unreadable["error"], "denied")
        self.assertEqual(self._decision(logs)["source"], "s3")

    def test_exactly_one_decision_line_per_call(self):
        with tempfile.TemporaryDirectory() as t:
            self._write_info(Path(t), code_hash="0000deadbeef0000")
            _, logs = self._decide(Path(t))
        self.assertEqual(len(_events(logs, "fetch_cache_decision")), 1)


class CleanExternalFilesLoggingTestCase(unittest.TestCase):
    def _clean(self, d):
        with capture_logs() as logs:
            with patch.object(_common, "_external_dir", return_value=d):
                _common.clean_empty_external_files.fn("x")
        return logs

    def test_empty_file_removed_and_counted(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "gene.json").write_text("")
            logs = self._clean(d)
            self.assertFalse((d / "gene.json").exists())
        (removed,) = _events(logs, "cache_file_removed")
        self.assertEqual((removed["file"], removed["reason"]), ("gene.json", "empty"))
        self.assertEqual(removed["log_level"], "warning")
        (summary,) = _events(logs, "external_cache_cleaned")
        self.assertEqual(summary["records_out"], 1)

    def test_missing_sentinel_removed_with_key(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "uniprot.json").write_text(json.dumps({"other": 1}))
            logs = self._clean(d)
        (removed,) = _events(logs, "cache_file_removed")
        self.assertEqual(removed["reason"], "missing_sentinel")
        self.assertEqual(removed["key"], "protein_accessions")

    def test_corrupt_non_empty_file_is_reported_not_silent(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "gene.json").write_text("{broken")
            logs = self._clean(d)
            self.assertTrue((d / "gene.json").exists())
        (event,) = _events(logs, "cache_file_unreadable")
        self.assertEqual(event["file"], "gene.json")
        self.assertEqual(event["error_type"], "JSONDecodeError")

    def test_clean_directory_still_logs_a_zero_summary(self):
        with tempfile.TemporaryDirectory() as t:
            logs = self._clean(Path(t))
        (summary,) = _events(logs, "external_cache_cleaned")
        self.assertEqual(summary["records_out"], 0)
        self.assertEqual(_events(logs, "cache_file_removed"), [])


class ValidateExternalFilesLoggingTestCase(unittest.TestCase):
    def _populate(self, d, empty=()):
        for stem in _RAW:
            for name in (f"{stem}.json", f"{stem}_transformed.json"):
                body = {} if name in empty else {"k": "v"}
                (d / name).write_text(json.dumps(body))

    def _validate(self, d):
        with capture_logs() as logs:
            with patch.object(_common, "_external_dir", return_value=d):
                try:
                    _common.validate_external_files.fn("x")
                    error = None
                except RuntimeError as exc:
                    error = exc
        return logs, error

    def test_valid_files_log_one_summary(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._populate(d)
            logs, error = self._validate(d)
            expected_bytes = sum(p.stat().st_size for p in d.iterdir())
        self.assertIsNone(error)
        (summary,) = _events(logs, "external_files_validated")
        self.assertEqual(summary["files_ok"], 8)
        self.assertEqual(summary["bytes"], expected_bytes)

    def test_empty_source_is_an_error_event_but_does_not_raise(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._populate(d, empty=("gene_transformed.json",))
            logs, error = self._validate(d)
        self.assertIsNone(error)
        (event,) = _events(logs, "external_source_empty")
        self.assertEqual(event["file"], "gene_transformed.json")
        self.assertEqual(event["source"], "gene")
        self.assertEqual(event["log_level"], "error")
        (summary,) = _events(logs, "external_files_validated")
        self.assertEqual(summary["files_ok"], 7)

    def test_missing_and_invalid_files_log_an_error_before_raising(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._populate(d)
            (d / "cellxgene.json").unlink()
            (d / "gene.json").write_text("{broken")
            (d / "uniprot.json").write_text("")
            logs, error = self._validate(d)
        self.assertIsInstance(error, RuntimeError)
        (invalid,) = _events(logs, "external_files_invalid")
        self.assertEqual(invalid["log_level"], "error")
        problems = {p["file"]: p["reason"] for p in invalid["files"]}
        self.assertEqual(
            problems,
            {
                "cellxgene.json": "not_found",
                "gene.json": "parse_failed",
                "uniprot.json": "empty",
            },
        )
        self.assertEqual(_events(logs, "external_files_validated"), [])


if __name__ == "__main__":
    unittest.main()
