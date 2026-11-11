"""release.py logs events and runs fetch and pipeline under one correlation id."""

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import structlog
from prefect import flow
from prefect.testing.utilities import prefect_test_harness
from structlog.testing import capture_logs

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import _common  # noqa: E402
import fetch  # noqa: E402
import pipeline  # noqa: E402
import release  # noqa: E402

_LOG_KEYS = ("CORRELATION_ID", "PHASE", "GIT_SHA", "AWS_BATCH_JOB_ID")


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


def _names(logs):
    return [e["event"] for e in logs]


class _FakeProc:
    """A finished child with no output, standing in for ``subprocess.Popen``."""

    def __init__(self):
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def wait(self):
        return 0


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _LOG_KEYS}
    return {**env, **extra}


class CorrelationIdTestCase(unittest.TestCase):
    def setUp(self):
        structlog.contextvars.clear_contextvars()
        self.addCleanup(structlog.contextvars.clear_contextvars)

    def _run_flow(self, env):
        """Run the release flow with fake subflows that report the id they see."""
        seen = {}

        def fake(name):
            def _sub(**kwargs):
                seen[name] = (
                    _common._log_env()["CORRELATION_ID"],
                    structlog.contextvars.get_contextvars().get("correlation_id"),
                )

            return _sub

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env, clear=True))
            stack.enter_context(patch.object(release, "nlm_ckn_fetch", fake("fetch")))
            stack.enter_context(patch.object(release, "nlm_ckn_etl", fake("etl")))
            for name in (
                "extract_release_tarball",
                "sync_release_dir_to_s3",
                "promote_results_to_latest",
                "post_github_deployment_status",
            ):
                stack.enter_context(patch.object(release, name))
            stack.enter_context(
                patch.object(release, "resolve_fetch_force", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    release, "_read_release_json", return_value={"hubmap_urls": ["u"]}
                )
            )
            release.nlm_ckn_release.fn("v1", ncbi_email="e", ncbi_api_key="k")
        return seen

    def test_both_subflows_log_under_the_same_id(self):
        seen = self._run_flow(_clean_env())
        self.assertEqual(set(seen), {"fetch", "etl"})
        self.assertEqual(seen["fetch"], seen["etl"])
        self.assertEqual(seen["fetch"][0], seen["fetch"][1])

    def test_environment_id_wins(self):
        seen = self._run_flow(_clean_env(CORRELATION_ID="batch-7"))
        self.assertEqual(seen["fetch"][0], "batch-7")
        self.assertEqual(seen["etl"][1], "batch-7")

    def test_an_id_already_bound_is_kept(self):
        structlog.contextvars.bind_contextvars(correlation_id="bound-3")
        seen = self._run_flow(_clean_env())
        self.assertEqual(seen["fetch"][0], "bound-3")

    def test_id_is_unbound_after_the_flow(self):
        self._run_flow(_clean_env())
        self.assertNotIn("correlation_id", structlog.contextvars.get_contextvars())

    def test_subprocesses_of_both_subflows_get_the_release_id(self):
        """The id survives the last hop, into the env handed to ``Popen``.

        The subflows are stand-ins that launch through the production fetch
        and ETL tasks, so only what reaches ``subprocess.Popen`` is checked.
        """
        children = {}
        bound = {}

        def launching(name, launch):
            def _sub(**kwargs):
                with patch.object(
                    subprocess, "Popen", side_effect=lambda *a, **k: _FakeProc()
                ) as popen:
                    launch()
                children[name] = popen.call_args.kwargs["env"]
                bound[name] = structlog.contextvars.get_contextvars()[
                    "correlation_id"
                ]

            return _sub

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, _clean_env(), clear=True))
            for module in (fetch, pipeline):
                stack.enter_context(
                    patch.object(
                        module, "get_run_logger", MagicMock(), create=True
                    )
                )
            stack.enter_context(
                patch.object(
                    release,
                    "nlm_ckn_fetch",
                    launching("fetch", fetch.fetch_external_api_results.fn),
                )
            )
            stack.enter_context(
                patch.object(
                    release,
                    "nlm_ckn_etl",
                    launching("etl", lambda: pipeline.write_tuples.fn("pw")),
                )
            )
            for name in (
                "extract_release_tarball",
                "sync_release_dir_to_s3",
                "promote_results_to_latest",
                "post_github_deployment_status",
            ):
                stack.enter_context(patch.object(release, name))
            stack.enter_context(
                patch.object(release, "resolve_fetch_force", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    release, "_read_release_json", return_value={"hubmap_urls": ["u"]}
                )
            )
            release.nlm_ckn_release.fn("v1", ncbi_email="e", ncbi_api_key="k")

        self.assertEqual(set(children), {"fetch", "etl"})
        for name in ("fetch", "etl"):
            self.assertEqual(children[name]["CORRELATION_ID"], bound[name])
        self.assertEqual(bound["fetch"], bound["etl"])
        self.assertEqual(children["fetch"]["PHASE"], "fetch")
        self.assertEqual(children["etl"]["PHASE"], "results")

    def test_real_prefect_subflows_see_the_bound_id(self):
        seen = {}

        @flow
        def sub(name):
            seen[name] = structlog.contextvars.get_contextvars().get("correlation_id")

        @flow
        def outer():
            with structlog.contextvars.bound_contextvars(correlation_id="rel-1"):
                sub("a")
                sub("b")

        with prefect_test_harness():
            outer()
        self.assertEqual(seen, {"a": "rel-1", "b": "rel-1"})


class ReleaseEventsTestCase(unittest.TestCase):
    def _run(self, *, fetch_error=None, promote_error=None, env=None, tag="v1",
             hubmap_urls=("u1", "u2"), **kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, _clean_env(**(env or {})), clear=True))
            stack.enter_context(patch.object(release, "nlm_ckn_fetch", side_effect=fetch_error))
            stack.enter_context(patch.object(release, "nlm_ckn_etl"))
            stack.enter_context(patch.object(release, "extract_release_tarball"))
            stack.enter_context(patch.object(release, "sync_release_dir_to_s3"))
            stack.enter_context(
                patch.object(release, "promote_results_to_latest", side_effect=promote_error)
            )
            self.status = stack.enter_context(
                patch.object(release, "post_github_deployment_status")
            )
            stack.enter_context(
                patch.object(release, "resolve_fetch_force", return_value=True)
            )
            stack.enter_context(
                patch.object(
                    release,
                    "_read_release_json",
                    return_value={"hubmap_urls": list(hubmap_urls)},
                )
            )
            with capture_logs() as logs:
                try:
                    release.nlm_ckn_release.fn(
                        tag, ncbi_email="e", ncbi_api_key="k", **kwargs
                    )
                    err = None
                except Exception as exc:
                    err = exc
        return logs, err

    def test_success_logs_started_then_finished_with_step_durations(self):
        logs, err = self._run(github_repo="o/r")
        self.assertIsNone(err)
        names = _names(logs)
        self.assertLess(names.index("release_started"), names.index("release_finished"))
        started = _one(logs, "release_started")
        self.assertEqual(started["release_tag"], "v1")
        self.assertEqual(started["run_name"], "1")
        self.assertEqual(started["hubmap_url_count"], 2)
        self.assertEqual(started["github_repo"], "o/r")
        self.assertEqual(
            started["tar_source"],
            "https://github.com/o/r/releases/download/v1/prod-data-v1.tar.gz",
        )
        self.assertNotIn("batch_job_id", started)
        self.assertNotIn("cloudwatch_log_url", started)
        finished = _one(logs, "release_finished")
        self.assertTrue(finished["force_fetch"])
        self.assertEqual(
            set(finished["step_durations_ms"]), {"extract", "fetch", "etl", "promote"}
        )
        self.assertEqual([e for e in logs if e["log_level"] == "error"], [])

    def test_batch_job_id_and_log_url_logged_once_on_batch(self):
        logs, _ = self._run(env={"AWS_BATCH_JOB_ID": "job-9"})
        started = _one(logs, "release_started")
        self.assertEqual(started["batch_job_id"], "job-9")
        self.assertIn("/batch/nlm-ckn-release".replace("/", "$252F"), started["cloudwatch_log_url"])

    def test_step_failure_logs_once_and_still_posts_status(self):
        logs, err = self._run(fetch_error=RuntimeError("api down"))
        self.assertIsInstance(err, RuntimeError)
        event = _one(logs, "release_step_failed")
        self.assertEqual(event["step"], 2)
        self.assertEqual(event["error_type"], "RuntimeError")
        self.assertEqual(event["error"], "api down")
        self.assertEqual(
            event["retry_command"], "poetry run src/flows/release.py --nlm-ckn-tag v1"
        )
        self.assertNotIn("release_finished", _names(logs))
        states = [c.kwargs["state"] for c in self.status.call_args_list]
        self.assertEqual(states[-1], "failure")

    def test_retry_command_carries_every_effective_input(self):
        logs, _ = self._run(
            fetch_error=RuntimeError("x"),
            run_name="rc 2",
            github_repo="fork/nlm-ckn",
            release_config="s3://b/release.json",
        )
        self.assertEqual(
            _one(logs, "release_step_failed")["retry_command"],
            "poetry run src/flows/release.py --nlm-ckn-tag v1 --run-name 'rc 2' "
            "--github-repo fork/nlm-ckn --release-config s3://b/release.json",
        )
        logs, _ = self._run(
            fetch_error=RuntimeError("x"), tar_source="/tmp/local.tar.gz"
        )
        self.assertEqual(
            _one(logs, "release_step_failed")["retry_command"],
            "poetry run src/flows/release.py --nlm-ckn-tag v1 "
            "--tar-source /tmp/local.tar.gz",
        )

    def test_promotion_failure_has_its_own_event(self):
        logs, err = self._run(promote_error=OSError("denied"))
        self.assertIsInstance(err, OSError)
        event = _one(logs, "release_promotion_failed")
        self.assertEqual((event["release_tag"], event["error_type"]), ("v1", "OSError"))
        self.assertNotIn("release_finished", _names(logs))

    def test_validation_failure_is_a_config_step_failure(self):
        logs, err = self._run(hubmap_urls=())
        self.assertIsInstance(err, ValueError)
        event = _one(logs, "release_step_failed")
        self.assertEqual(event["step"], "config")
        self.assertNotIn("release_started", _names(logs))

    def test_placeholder_tag_is_a_config_step_failure(self):
        logs, err = self._run(tag="v0.0.0-alpha")
        self.assertIsInstance(err, ValueError)
        self.assertEqual(_one(logs, "release_step_failed")["step"], "config")

    def test_negative_age_is_a_config_step_failure(self):
        logs, err = self._run(max_fetch_age_hours=-1.0)
        self.assertIsInstance(err, ValueError)
        self.assertEqual(_one(logs, "release_step_failed")["step"], "config")

    def test_config_saved_event(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(release, "REPO_ROOT", Path(d)):
                with capture_logs() as logs:
                    release._save_release_json({"nlm_ckn_tag": "v1"})
        self.assertEqual(
            _one(logs, "release_config_saved")["path"], str(Path(d) / "release.json")
        )


class TarballEventsTestCase(unittest.TestCase):
    def _tar(self, root):
        path = root / "r.tar.gz"
        with tarfile.open(path, "w:gz") as tf:
            def add(name, text):
                raw = text.encode()
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tf.addfile(info, io.BytesIO(raw))

            add("data/prod/a/results_ensg_x.csv", "a,b")
            add("data/prod/a/master_s3_manifest.csv", "filename,s3_path\nf1,s3://b/f1\n")
            add("data/prod/b/master_s3_manifest.csv", "filename,s3_path\nf2,s3://b/f2\n")
            link = tarfile.TarInfo("data/prod/ln")
            link.type = tarfile.SYMTYPE
            link.linkname = "x"
            tf.addfile(link)
        return path

    def test_local_tarball_events(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tar = self._tar(root)
            with patch.object(release, "REPO_ROOT", root):
                with capture_logs() as logs:
                    release.extract_release_tarball.fn(str(tar), "t", ["u1"])
            size = tar.stat().st_size
        fetched = _one(logs, "release_tarball_fetched")
        self.assertEqual(
            (fetched["source_kind"], fetched["path"], fetched["bytes"]),
            ("local", str(tar), size),
        )
        self.assertEqual(_one(logs, "release_manifest_merged")["records_out"], 2)
        skipped = _one(logs, "tar_members_skipped")
        self.assertEqual((skipped["links"], skipped["path_traversal"]), (1, 0))
        extracted = _one(logs, "release_tarball_extracted")
        self.assertEqual(
            (
                extracted["nsforest_files"],
                extracted["hubmap_urls"],
                extracted["manifest_rows"],
                extracted["files_skipped"],
            ),
            (1, 1, 2, 1),
        )

    def test_s3_tarball_reports_uri(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            tar = self._tar(root)

            s3 = MagicMock()
            s3.download_file.side_effect = lambda b, k, dest: Path(dest).write_bytes(
                tar.read_bytes()
            )
            with patch.object(release, "REPO_ROOT", root):
                (root / "data").mkdir()
                with patch.object(release.boto3, "client", return_value=s3):
                    with capture_logs() as logs:
                        release.extract_release_tarball.fn("s3://bkt/k.tar.gz", "t", ["u"])
        fetched = _one(logs, "release_tarball_fetched")
        self.assertEqual((fetched["source_kind"], fetched["uri"]), ("s3", "s3://bkt/k.tar.gz"))


class PromoteTestCase(unittest.TestCase):
    def _run(self, count):
        with patch.object(release, "S3_BUCKET", "b"):
            with patch.object(release, "_s3_copy_prefix", return_value=count):
                with capture_logs() as logs:
                    release.promote_results_to_latest.fn("r")
        return _one(logs, "results_promoted_to_latest")

    def test_count_logged_at_info(self):
        event = self._run(5)
        self.assertEqual((event["objects_copied"], event["log_level"]), (5, "info"))

    def test_zero_objects_warns(self):
        self.assertEqual(self._run(0)["log_level"], "warning")

    def test_sync_passes_artifact_name(self):
        with patch.object(release, "S3_BUCKET", "b"):
            with patch.object(release, "_s3_sync") as sync:
                release.sync_release_dir_to_s3.fn("r")
        self.assertEqual(sync.call_args.kwargs["artifact"], "release_results")


if __name__ == "__main__":
    unittest.main()
