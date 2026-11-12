"""DataFetcher.py logs events: the fetch loop, HuBMAP, and main()."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from structlog.testing import capture_logs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import DataFetcher  # noqa: E402
from DataFetcher import DataFetcher as Base  # noqa: E402
from DataFetcher import HuBMAPFetcher  # noqa: E402


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _all(logs, name):
    return [e for e in logs if e["event"] == name]


class _Fetcher(Base):
    name = "fake"
    max_per_second = 1000
    batch_size = 2

    def __init__(self, errors=None):
        self.errors = errors or {}

    def get_ids(self, context):
        return context.get("ids", ["a"])

    def fetch_one(self, id_value):
        if id_value in self.errors:
            raise self.errors[id_value]
        return {"data": id_value}


class FetchLoopLoggingTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / "fake.json"

    def _run(self, fetcher, ids, **kwargs):
        fetcher.output_path = self.output
        with capture_logs() as logs:
            fetcher.run({"ids": ids}, **kwargs)
        return logs

    def test_plan_and_finished(self):
        logs = self._run(_Fetcher(), ["a", "b", "c"])
        plan = _one(logs, "fetch_plan")
        self.assertEqual(
            (plan["source"], plan["ids_total"], plan["ids_cached"], plan["ids_pending"]),
            ("fake", 3, 0, 3),
        )
        done = _one(logs, "fetch_finished")
        self.assertEqual(done["outcome"], "ok")
        self.assertEqual(
            (done["records_in"], done["records_out"], done["records_rejected"]),
            (3, 3, 0),
        )
        self.assertIsInstance(done["duration_ms"], int)
        self.assertEqual([e["n_ok"] for e in _all(logs, "fetch_batch_done")], [2, 1])
        self.assertTrue(all(e["log_level"] == "debug" for e in _all(logs, "fetch_batch_done")))

    def test_second_run_is_all_cached(self):
        self._run(_Fetcher(), ["a", "b"])
        logs = self._run(_Fetcher(), ["a", "b"])
        plan = _one(logs, "fetch_plan")
        self.assertEqual((plan["ids_cached"], plan["ids_pending"]), (2, 0))
        self.assertEqual(_one(logs, "fetch_cache_loaded")["records_loaded"], 2)
        self.assertEqual(_one(logs, "fetch_finished")["outcome"], "ok")

    def test_failed_item_is_a_warning_and_the_run_is_partial(self):
        fetcher = _Fetcher(errors={"b": RuntimeError("boom")})
        logs = self._run(fetcher, ["a", "b"])
        failed = _one(logs, "fetch_item_failed")
        self.assertEqual(
            (failed["id"], failed["error_type"], failed["error"], failed["log_level"]),
            ("b", "RuntimeError", "boom", "warning"),
        )
        done = _one(logs, "fetch_finished")
        self.assertEqual(done["outcome"], "partial")
        self.assertEqual((done["records_out"], done["records_rejected"]), (1, 1))
        self.assertEqual(done["n_retryable"], 1)
        batch = _one(logs, "fetch_batch_done")
        self.assertEqual((batch["n_ok"], batch["n_failed"]), (1, 1))

    def test_unexpected_error_is_an_error_event(self):
        fetcher = _Fetcher(errors={"a": TypeError("odd")})
        logs = self._run(fetcher, ["a"])
        event = _one(logs, "fetch_item_unexpected_error")
        self.assertEqual((event["error_type"], event["log_level"]), ("TypeError", "error"))
        self.assertEqual(_all(logs, "fetch_item_failed"), [])

    def test_resumed_checkpoint_is_logged(self):
        fetcher = _Fetcher()
        fetcher.output_path = self.output
        fetcher.checkpoint_path.write_text(json.dumps({"id": "a", "data": {}}) + "\n")
        with capture_logs() as logs:
            fetcher._load()
        event = _one(logs, "fetch_checkpoint_resumed")
        self.assertEqual(event["records_loaded"], 1)

    def test_rate_limit_is_a_warning(self):
        response = MagicMock(status_code=429, headers={"Retry-After": "0"})
        fetcher = _Fetcher(errors={"a": requests.HTTPError(response=response)})
        with patch.object(DataFetcher, "sleep"):
            with capture_logs() as logs:
                with self.assertRaises(requests.HTTPError):
                    fetcher._fetch_with_retry("a")
        events = _all(logs, "fetch_rate_limited")
        self.assertEqual([e["attempt"] for e in events], [1, 2, 3])
        self.assertEqual(events[0]["max_retries"], 3)
        self.assertEqual(events[0]["log_level"], "warning")


class HuBMAPLoggingTestCase(unittest.TestCase):
    def _run(self, files, status_code=200):
        with tempfile.TemporaryDirectory() as d:
            hubmap_dir = Path(d) / "hubmap"
            hubmap_dir.mkdir()
            for name in files:
                (hubmap_dir / name).write_text("{}")
            fetcher = HuBMAPFetcher()
            fetcher.output_path = hubmap_dir
            urls = [("heart", "1.0", "https://x/v1.0/graph.json")]
            with patch.object(HuBMAPFetcher, "_get_hubmap_json_urls", return_value=urls):
                with patch.object(
                    DataFetcher.requests,
                    "get",
                    return_value=MagicMock(status_code=status_code, text="{}"),
                ):
                    with capture_logs() as logs:
                        fetcher.run({})
        return logs

    def test_download_is_summarized(self):
        done = _one(self._run([]), "fetch_finished")
        self.assertEqual(
            (done["source"], done["n_downloaded"], done["records_rejected"]),
            ("hubmap", 1, 0),
        )

    def test_present_table_is_counted(self):
        done = _one(self._run(["heart-v1.0.json"]), "fetch_finished")
        self.assertEqual((done["n_present"], done["n_downloaded"]), (1, 0))

    def test_older_version_is_archived(self):
        done = _one(self._run(["heart-v0.9.json"]), "fetch_finished")
        self.assertEqual((done["n_archived"], done["n_downloaded"]), (1, 1))

    def test_failed_download_is_an_error(self):
        logs = self._run([], status_code=500)
        event = _one(logs, "hubmap_download_failed")
        self.assertEqual(
            (event["organ"], event["reason"], event["status_code"], event["log_level"]),
            ("heart", "http_error", 500, "error"),
        )
        self.assertEqual(_one(logs, "fetch_finished")["records_rejected"], 1)

    def test_older_version_removed_when_archive_fails(self):
        with patch.object(DataFetcher.shutil, "move", side_effect=OSError("no")):
            logs = self._run(["heart-v0.9.json"])
        event = _one(logs, "hubmap_table_removed")
        self.assertEqual((event["reason"], event["log_level"]), ("archive_failed", "warning"))

    def test_urls_unresolved_and_none_configured(self):
        good = MagicMock(status_code=200, text="see https://x/v1.0/graph.json")
        with patch.object(DataFetcher.requests, "get", return_value=good):
            with capture_logs() as logs:
                found = HuBMAPFetcher._get_hubmap_json_urls(
                    ["ftp://nope", "https://h/asct-b/heart/latest"]
                )
        self.assertEqual(len(found), 1)
        event = _one(logs, "hubmap_urls_unresolved")
        self.assertEqual((event["n_failed"], event["log_level"]), (1, "warning"))
        with capture_logs() as logs:
            self.assertEqual(HuBMAPFetcher._get_hubmap_json_urls([]), [])
        self.assertEqual(_one(logs, "hubmap_no_urls_configured")["log_level"], "warning")


class FetchStatusLoggingTestCase(unittest.TestCase):
    def test_unreadable_status_warns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "fetch-status.json"
            path.write_text("{x")
            with capture_logs() as logs:
                self.assertEqual(DataFetcher._load_fetch_status(path), {})
        event = _one(logs, "fetch_status_unreadable")
        self.assertEqual((event["path"], event["error_type"]), ("fetch-status.json", "JSONDecodeError"))


class _FakeRun:
    run_name = "r"

    def __init__(self, root):
        self.external_dir = Path(root) / "external"
        self.results_dir = Path(root) / "results"
        self.hubmap_urls = []


class MainLoggingTestCase(unittest.TestCase):
    """main() with the data readers patched out."""

    def _main(self, fetcher, argv, status=None, file_paths=None, paths_error=None):
        with tempfile.TemporaryDirectory() as d:
            run = _FakeRun(d)
            run.external_dir.mkdir()
            if status is not None:
                (run.external_dir / "fetch-status.json").write_text(json.dumps(status))
            fetcher.output_path = run.external_dir / f"{fetcher.name}.json"
            with (
                patch.object(sys, "argv", ["DataFetcher.py", *argv]),
                patch.object(DataFetcher, "FETCHER_REGISTRY", [fetcher]),
                patch.object(DataFetcher, "set_current_run"),
                patch.object(DataFetcher, "get_current_run", return_value=run),
                patch.object(DataFetcher, "configure_logging"),
                patch.object(
                    DataFetcher,
                    "get_dataset_file_paths",
                    side_effect=paths_error,
                    return_value=file_paths or {"nsforest_paths": ["x.csv"]},
                ),
                patch.object(DataFetcher, "get_dataset_version_id_lists", return_value=[]),
                patch.object(DataFetcher, "get_unique_gene_names_and_ids", return_value=None),
                capture_logs() as logs,
            ):
                try:
                    DataFetcher.main()
                    error = None
                except Exception as exc:
                    error = exc
        return logs, error

    def test_run_lifecycle(self):
        logs, error = self._main(_Fetcher(), [])
        self.assertIsNone(error)
        started = _one(logs, "run_started")
        self.assertEqual(
            (started["script"], started["run_name"], started["forced"]),
            ("DataFetcher", "r", []),
        )
        self.assertEqual(_one(logs, "run_finished")["outcome"], "ok")

    def test_forced_sources_are_listed(self):
        logs, _ = self._main(_Fetcher(), ["--force-fake"])
        self.assertEqual(_one(logs, "run_started")["forced"], ["fake"])

    def test_fresh_source_is_skipped(self):
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        logs, _ = self._main(
            _Fetcher(),
            ["--max-source-age-hours", "24"],
            status={"fake": {"last_outcome": "ok", "last_success_at": recent}},
        )
        event = _one(logs, "fetch_skipped_fresh")
        self.assertEqual((event["source"], event["age_hours"], event["max_age_hours"]), ("fake", 2.0, 24.0))

    def test_missing_context_without_cache_is_an_error_and_fails_the_run(self):
        class NeedsContext(_Fetcher):
            def get_ids(self, context):
                raise ValueError("needs gene_data")

        logs, error = self._main(NeedsContext(), [])
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(_one(logs, "fetch_context_missing")["error"], "needs gene_data")
        self.assertEqual(_one(logs, "run_failed")["error_type"], "RuntimeError")

    def test_missing_context_with_cache_uses_it(self):
        class NeedsContext(_Fetcher):
            def get_ids(self, context):
                raise ValueError("needs gene_data")

        fetcher = NeedsContext()
        with patch.object(NeedsContext, "_load", return_value={"a": {}}):
            logs, error = self._main(fetcher, [])
        self.assertIsNone(error)
        event = _one(logs, "fetch_using_cache")
        self.assertEqual((event["reason"], event["records_loaded"]), ("missing_context", 1))

    def test_fetcher_failure_is_an_error_with_traceback(self):
        class Broken(_Fetcher):
            def run(self, context, force=False):
                raise RuntimeError("down")

        logs, error = self._main(Broken(), [])
        self.assertIsInstance(error, RuntimeError)
        event = _one(logs, "fetch_failed")
        self.assertEqual((event["source"], event["error"], event["exc_info"]), ("fake", "down", True))

    def test_unreadable_results_dir_is_an_error(self):
        logs, _ = self._main(_Fetcher(), [], paths_error=OSError("gone"))
        event = _one(logs, "results_dir_unreadable")
        self.assertEqual((event["error_type"], event["log_level"]), ("OSError", "error"))

    def test_no_nsforest_results_is_an_error(self):
        logs, _ = self._main(_Fetcher(), [], file_paths={"nsforest_paths": []})
        self.assertEqual(_one(logs, "no_nsforest_results")["results_dir"], "results")


if __name__ == "__main__":
    unittest.main()
