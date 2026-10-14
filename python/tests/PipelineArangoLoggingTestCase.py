"""The first half of pipeline.py logs events: ArangoDB lifecycle, JAR, dumps."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from structlog.testing import capture_logs

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import pipeline  # noqa: E402


def _names(logs):
    return [e["event"] for e in logs]


def _one(logs, name):
    (event,) = [e for e in logs if e["event"] == name]
    return event


class _Exec:
    def __init__(self, code=0, output=b""):
        self.exit_code = code
        self.output = output


class StopAndWipeTestCase(unittest.TestCase):
    def test_unreachable_docker_warns(self):
        err = pipeline.docker_sdk.errors.DockerException("no socket")
        with patch.object(pipeline.docker_sdk, "from_env", side_effect=err):
            with capture_logs() as logs:
                pipeline.stop_arangodb.fn()
        event = _one(logs, "docker_unreachable")
        self.assertEqual(event["log_level"], "warning")
        self.assertEqual(event["error_type"], "DockerException")

    def test_each_removed_container_is_logged_with_prior_status(self):
        c = MagicMock(short_id="abc", status="running")
        client = MagicMock()
        client.containers.list.return_value = [c]
        with patch.object(pipeline.docker_sdk, "from_env", return_value=client):
            with capture_logs() as logs:
                pipeline.stop_arangodb.fn()
        c.stop.assert_called_once()
        event = _one(logs, "arangodb_container_removed")
        self.assertEqual((event["container_id"], event["status"]), ("abc", "running"))

    def test_bind_mount_wipe_logs_path(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "arango"
            home.mkdir()
            with patch.object(pipeline, "ARANGO_DB_HOST_HOME", ""):
                with patch.object(Path, "exists", lambda p: str(p) == str(home)):
                    with capture_logs() as logs:
                        pipeline._wipe_arangodb_data(str(home))
        event = _one(logs, "arangodb_data_wiped")
        self.assertEqual(event["kind"], "bind_mount")
        self.assertEqual(event["path"], str(home))

    def test_named_volume_removal_failure_warns_and_continues(self):
        err = pipeline.docker_sdk.errors.DockerException("busy")
        with patch.object(
            pipeline, "_arangodb_volume_source", return_value=("vol", True)
        ):
            with patch.object(pipeline.docker_sdk, "from_env", side_effect=err):
                with capture_logs() as logs:
                    pipeline._wipe_arangodb_data("x")
        event = _one(logs, "arangodb_volume_removal_failed")
        self.assertEqual(event["volume"], "vol")
        self.assertEqual(event["error_type"], "DockerException")


class StartAndRequireTestCase(unittest.TestCase):
    def test_reused_container_logs_port(self):
        container = MagicMock(ports={"8529/tcp": [{"HostPort": "9999"}]})
        client = MagicMock()
        client.containers.get.return_value = container
        with patch.object(pipeline, "_get_arangodb_id", return_value="cid"):
            with patch.object(pipeline.docker_sdk, "from_env", return_value=client):
                with capture_logs() as logs:
                    port = pipeline.start_arangodb.fn("h", "pw")
        self.assertEqual(port, 9999)
        event = _one(logs, "arangodb_container_reused")
        self.assertEqual((event["container_id"], event["port"]), ("cid", 9999))

    def test_port_lookup_failure_warns_and_falls_back(self):
        client = MagicMock()
        client.containers.get.side_effect = RuntimeError("gone")
        with patch.object(pipeline, "_get_arangodb_id", return_value="cid"):
            with patch.object(pipeline.docker_sdk, "from_env", return_value=client):
                with capture_logs() as logs:
                    port = pipeline.start_arangodb.fn("h", "pw")
        self.assertEqual(port, pipeline.ARANGO_DB_PORT)
        event = _one(logs, "arangodb_port_lookup_failed")
        self.assertEqual(event["error_type"], "RuntimeError")

    def test_new_container_logs_started_then_ready(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(pipeline, "_get_arangodb_id", return_value=None):
                with patch.object(pipeline, "_find_free_port", return_value=8600):
                    with patch.object(
                        pipeline, "_arangodb_volume_source", return_value=(d, False)
                    ):
                        with patch.object(pipeline.docker_sdk, "from_env"):
                            with patch.object(pipeline, "_wait_for_arangodb_ready"):
                                with capture_logs() as logs:
                                    pipeline.start_arangodb.fn(d, "pw")
        self.assertEqual(_names(logs), ["arangodb_container_started", "arangodb_ready"])
        self.assertEqual(logs[0]["volume_kind"], "bind_mount")
        self.assertEqual(logs[0]["port"], 8600)

    def test_remote_endpoint(self):
        with patch.object(pipeline, "ARANGO_DB_IS_LOCAL", False):
            with capture_logs() as logs:
                pipeline.require_arangodb.fn()
        self.assertEqual(_one(logs, "arangodb_endpoint_resolved")["mode"], "remote")

    def test_local_endpoint_names_the_container(self):
        with patch.object(pipeline, "ARANGO_DB_IS_LOCAL", True):
            with patch.object(pipeline, "_get_arangodb_id", return_value="cid"):
                with capture_logs() as logs:
                    pipeline.require_arangodb.fn()
        event = _one(logs, "arangodb_endpoint_resolved")
        self.assertEqual((event["mode"], event["container_id"]), ("local", "cid"))


class JarTestCase(unittest.TestCase):
    def _run(self, root, bucket):
        with patch.object(pipeline, "REPO_ROOT", root):
            with patch.object(pipeline, "S3_BUCKET", bucket):
                with patch.object(pipeline, "_jar_key", return_value="k1"):
                    with capture_logs() as logs:
                        key = pipeline.ensure_jar.fn()
        return key, logs

    def test_cached_jar(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            jar = root / pipeline.CLASSPATH
            jar.parent.mkdir(parents=True, exist_ok=True)
            jar.write_bytes(b"12345")
            key, logs = self._run(root, "")
        self.assertEqual(key, "k1")
        event = _one(logs, "jar_resolved")
        self.assertEqual(
            (event["source"], event["bytes"], event["jar_key"]), ("cache", 5, "k1")
        )

    def test_maven_build(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            jar = root / pipeline.CLASSPATH

            def build(*a, **k):
                jar.parent.mkdir(parents=True, exist_ok=True)
                jar.write_bytes(b"123")

            with patch.object(pipeline, "_run_logged", side_effect=build):
                _, logs = self._run(root, "")
        self.assertEqual(_one(logs, "jar_resolved")["source"], "maven")

    def test_s3_download(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            jar = root / pipeline.CLASSPATH

            def download(bucket, key, dest):
                Path(dest).write_bytes(b"1234")

            s3 = MagicMock()
            s3.download_file.side_effect = download
            with patch.object(pipeline.boto3, "client", return_value=s3):
                _, logs = self._run(root, "bkt")
        event = _one(logs, "jar_resolved")
        self.assertEqual((event["source"], event["bytes"]), ("s3", 4))


class OntologyTestCase(unittest.TestCase):
    def test_downloaded_logs_count(self):
        with patch.object(pipeline, "_run_logged"):
            with patch.object(Path, "glob", return_value=[Path("a"), Path("b")]):
                with capture_logs() as logs:
                    pipeline.download_ontologies.fn("pw")
        event = _one(logs, "ontologies_downloaded")
        self.assertEqual((event["records_out"], event["phase"]), (2, "ontology"))

    def test_no_owl_files_logs_error_then_raises(self):
        with patch.object(pipeline, "_run_logged"):
            with patch.object(Path, "glob", return_value=[]):
                with capture_logs() as logs:
                    with self.assertRaises(FileNotFoundError):
                        pipeline.download_ontologies.fn("pw")
        event = _one(logs, "ontologies_download_failed")
        self.assertEqual(event["reason"], "no_owl_files")
        self.assertEqual(event["log_level"], "error")


def _container(result, tar_bytes=None):
    c = MagicMock()
    c.exec_run.return_value = result
    c.get_archive.return_value = (iter(()), None)
    return c


class DumpRestoreTestCase(unittest.TestCase):
    def _dump(self, result, label="baseline"):
        container = _container(result)
        client = MagicMock()
        client.containers.get.return_value = container
        with tempfile.TemporaryDirectory() as d:
            dump_dir = Path(d) / "dump"
            dump_dir.mkdir()
            (dump_dir / "a.json").write_bytes(b"abc")
            sub = dump_dir / "db"
            sub.mkdir()
            (sub / "b.json").write_bytes(b"de")
            with patch.object(pipeline, "_get_arangodb_id", return_value="cid"):
                with patch.object(pipeline.docker_sdk, "from_env", return_value=client):
                    with patch.object(pipeline.tarfile, "open"):
                        with patch.object(pipeline, "REPO_ROOT", Path(d)):
                            with capture_logs() as logs:
                                try:
                                    pipeline.dump_arangodb.fn(
                                        dump_dir, "secret-pw", label=label
                                    )
                                    err = None
                                except RuntimeError as exc:
                                    err = exc
        return logs, err

    def test_dump_success_logs_counts_and_size(self):
        logs, err = self._dump(_Exec(0, b"done"))
        self.assertIsNone(err)
        event = _one(logs, "arangodump_finished")
        self.assertEqual(event["dump_label"], "baseline")
        self.assertEqual((event["file_count"], event["bytes"]), (2, 5))
        self.assertEqual(event["rc"], 0)
        self.assertEqual(_one(logs, "arangodump_output")["log_level"], "debug")

    def test_dump_failure_logs_error_with_output(self):
        logs, err = self._dump(_Exec(2, b"boom"))
        self.assertIsNotNone(err)
        event = _one(logs, "arangodump_failed")
        self.assertEqual((event["reason"], event["rc"]), ("nonzero_exit", 2))
        self.assertEqual(event["output"], "boom")

    def test_no_event_carries_the_password(self):
        logs, _ = self._dump(_Exec(2, b"boom"))
        self.assertNotIn("secret-pw", repr(logs))

    def _restore(self, result):
        container = MagicMock()
        container.exec_run.return_value = result
        client = MagicMock()
        client.containers.get.return_value = container
        with tempfile.TemporaryDirectory() as d:
            dump_dir = Path(d) / "dump"
            dump_dir.mkdir()
            with patch.object(pipeline, "_get_arangodb_id", return_value="cid"):
                with patch.object(pipeline.docker_sdk, "from_env", return_value=client):
                    with capture_logs() as logs:
                        try:
                            pipeline.restore_arangodb.fn(dump_dir, "pw")
                            err = None
                        except RuntimeError as exc:
                            err = exc
        return logs, err

    def test_restore_success(self):
        logs, err = self._restore(_Exec(0, b"Processed 3 collection(s)"))
        self.assertIsNone(err)
        self.assertEqual(_one(logs, "arangorestore_finished")["rc"], 0)
        self.assertNotIn("arangorestore_reported_errors", _names(logs))

    def test_restore_exit_zero_with_error_lines_warns(self):
        out = b"ok\n2026 [1] ERROR [2] could not create view\nfine\n"
        logs, err = self._restore(_Exec(0, out))
        self.assertIsNone(err)
        event = _one(logs, "arangorestore_reported_errors")
        self.assertEqual(event["log_level"], "warning")
        self.assertEqual(event["error_lines"], 1)
        self.assertIn("could not create view", event["sample"][0])

    def test_restore_failure(self):
        logs, err = self._restore(_Exec(1, b"nope"))
        self.assertIsNotNone(err)
        self.assertEqual(_one(logs, "arangorestore_failed")["rc"], 1)


class SidecarTestCase(unittest.TestCase):
    def test_export_logs_one_summary_per_database(self):
        with tempfile.TemporaryDirectory() as d:
            dump_dir = Path(d)
            (dump_dir / "db1").mkdir()

            class Resp:
                def __init__(self, body):
                    self.body = body

                def read(self):
                    return self.body

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            def urlopen(req, timeout):
                if "gharial" in req.full_url:
                    return Resp(b'{"graphs": [{"name": "g"}]}')
                return Resp(b'{"result": [{"name": "db::a"}, {"name": "text"}]}')

            with patch("urllib.request.urlopen", urlopen):
                with capture_logs() as logs:
                    pipeline.export_graphs_and_analyzers.fn(dump_dir, "pw")
        event = _one(logs, "graph_sidecars_exported")
        self.assertEqual(
            (event["database"], event["graph_count"], event["analyzer_count"]),
            ("db1", 1, 1),
        )

    def test_export_failure_is_error_and_still_raises(self):
        with tempfile.TemporaryDirectory() as d:
            dump_dir = Path(d)
            (dump_dir / "db1").mkdir()
            with patch("urllib.request.urlopen", side_effect=OSError("down")):
                with capture_logs() as logs:
                    with self.assertRaises(RuntimeError):
                        pipeline.export_graphs_and_analyzers.fn(dump_dir, "pw")
        failed = [e for e in logs if e["event"] == "graph_sidecar_export_failed"]
        self.assertEqual({e["kind"] for e in failed}, {"graphs", "analyzers"})
        self.assertNotIn("graph_sidecars_exported", _names(logs))

    def _import(self, urlopen):
        with tempfile.TemporaryDirectory() as d:
            dump_dir = Path(d)
            (dump_dir / "db1").mkdir()
            (dump_dir / "db1" / "ckn-graphs.ndjson").write_text('{"name": "g"}\n')
            with patch("urllib.request.urlopen", urlopen):
                with capture_logs() as logs:
                    try:
                        pipeline.import_graphs_from_sidecar.fn(dump_dir, "pw")
                        err = None
                    except RuntimeError as exc:
                        err = exc
        return logs, err

    def _http_error(self, code):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            "u", code, "msg", {}, io.BytesIO(b"detail text")
        )

    def test_recreated(self):
        resp = MagicMock()
        resp.__enter__.return_value = resp
        logs, err = self._import(lambda *a, **k: resp)
        self.assertIsNone(err)
        self.assertEqual(_one(logs, "graph_recreated")["graph"], "g")

    def test_conflict_is_already_present(self):
        def urlopen(*a, **k):
            raise self._http_error(409)

        logs, err = self._import(urlopen)
        self.assertIsNone(err)
        self.assertEqual(_one(logs, "graph_already_present")["database"], "db1")

    def test_other_http_error_is_logged_and_raises(self):
        def urlopen(*a, **k):
            raise self._http_error(500)

        logs, err = self._import(urlopen)
        self.assertIsNotNone(err)
        event = _one(logs, "graph_recreate_failed")
        self.assertEqual((event["reason"], event["status_code"]), ("http_error", 500))
        self.assertEqual(event["error"], "detail text")


if __name__ == "__main__":
    unittest.main()
