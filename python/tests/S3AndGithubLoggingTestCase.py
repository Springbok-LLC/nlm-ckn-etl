"""S3 transfers, GitHub status and lookups in _common log structured events."""

import io
import os
import sys
import tarfile
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

from structlog.testing import capture_logs  # noqa: E402

import _common  # noqa: E402


def _events(logs, name):
    return [e for e in logs if e["event"] == name]


class FakeS3:
    """The few boto3 S3 client calls _common makes, backed by a dict."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})  # (bucket, key) -> bytes

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = Path(filename).read_bytes()

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])

    def copy_object(self, Bucket, CopySource, Key):
        self.objects[(Bucket, Key)] = self.objects[(CopySource["Bucket"], CopySource["Key"])]

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket, Prefix):
        yield {
            "Contents": [
                {"Key": key, "Size": len(body)}
                for (bucket, key), body in sorted(self.objects.items())
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }


class _S3Case(unittest.TestCase):
    def use_s3(self, fake, bucket="bkt", kms=""):
        for patcher in (
            patch.object(_common, "S3_BUCKET", bucket),
            patch.object(_common, "S3_KMS_KEY_ID", kms),
            patch.object(_common.boto3, "client", return_value=fake),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)


class TarTransferLoggingTestCase(_S3Case):
    def test_upload_logs_counts_and_leaves_archive_members_out(self):
        fake = FakeS3()
        self.use_s3(fake)
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "dump"
            (d / ".archive").mkdir(parents=True)
            (d / "a.json").write_text("aa")
            (d / "b.json").write_text("bbb")
            (d / ".archive" / "old.json").write_text("old")
            with capture_logs() as logs:
                stats = _common._s3_upload_tar(d, "s3://bkt/x/dump.tar.gz", "tuples")
        (event,) = _events(logs, "s3_upload_finished")
        self.assertEqual(stats["files"], 2)
        self.assertEqual(stats["members_skipped"], 1)
        self.assertEqual(event["files"], 2)
        self.assertEqual(event["members_skipped"], 1)
        self.assertEqual(event["bytes"], len(fake.objects[("bkt", "x/dump.tar.gz")]))
        self.assertEqual(event["s3_uri"], "s3://bkt/x/dump.tar.gz")
        self.assertEqual(event["artifact"], "tuples")
        self.assertFalse(event["kms_encrypted"])
        self.assertIsInstance(event["duration_ms"], int)

    def test_upload_reports_kms_but_never_logs_the_key_id(self):
        self.use_s3(FakeS3(), kms="arn:aws:kms:us-east-1:111:key/secret-key-id")
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "f").write_text("x")
            with capture_logs() as logs:
                _common._s3_upload_tar(Path(t), "s3://bkt/k.tar.gz")
        (event,) = _events(logs, "s3_upload_finished")
        self.assertTrue(event["kms_encrypted"])
        self.assertNotIn("artifact", event)
        self.assertNotIn("secret-key-id", repr(logs))

    def test_helpers_are_silent_no_ops_without_a_bucket(self):
        with patch.object(_common, "S3_BUCKET", ""):
            with capture_logs() as logs:
                up = _common._s3_upload_tar(Path("."), "s3://b/k")
                down = _common._s3_download_tar("s3://b/k", Path("."))
                sync = _common._s3_sync("s3://b/p/", "somewhere")
        self.assertEqual(logs, [])
        self.assertEqual(up["bytes"], 0)
        self.assertEqual(down["files"], 0)
        self.assertEqual(sync["objects_transferred"], 0)

    def _tar_with_unsafe_members(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            ok = tarfile.TarInfo("top/ok.txt")
            ok.size = 2
            tar.addfile(ok, io.BytesIO(b"ok"))
            link = tarfile.TarInfo("top/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
            evil = tarfile.TarInfo("top/../../evil.txt")
            evil.size = 4
            tar.addfile(evil, io.BytesIO(b"evil"))
        return buf.getvalue()

    def test_download_counts_extracted_and_warns_about_skipped_members(self):
        fake = FakeS3({("bkt", "dump.tar.gz"): self._tar_with_unsafe_members()})
        self.use_s3(fake)
        with tempfile.TemporaryDirectory() as t:
            target = Path(t) / "out"
            with capture_logs() as logs:
                stats = _common._s3_download_tar(
                    "s3://bkt/dump.tar.gz", target, "baseline_dump"
                )
            self.assertEqual((target / "ok.txt").read_text(), "ok")
            self.assertFalse((Path(t) / "evil.txt").exists())
            self.assertFalse((target / "link").exists())
        self.assertEqual(stats["files"], 1)
        (warn,) = _events(logs, "tar_members_skipped")
        self.assertEqual(warn["log_level"], "warning")
        self.assertEqual((warn["links"], warn["path_traversal"]), (1, 1))
        self.assertEqual(warn["artifact"], "baseline_dump")
        (done,) = _events(logs, "s3_download_finished")
        self.assertEqual(done["files"], 1)
        self.assertEqual(done["bytes"], len(fake.objects[("bkt", "dump.tar.gz")]))

    def test_clean_download_does_not_warn(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            info = tarfile.TarInfo("top/a.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"a"))
        self.use_s3(FakeS3({("bkt", "d.tar.gz"): buf.getvalue()}))
        with tempfile.TemporaryDirectory() as t:
            with capture_logs() as logs:
                _common._s3_download_tar("s3://bkt/d.tar.gz", Path(t))
        self.assertEqual(_events(logs, "tar_members_skipped"), [])


class SyncLoggingTestCase(_S3Case):
    def test_download_counts_transferred_and_unchanged(self):
        fake = FakeS3(
            {("bkt", "external/same.json"): b"123", ("bkt", "external/new.json"): b"12345"}
        )
        self.use_s3(fake)
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "same.json").write_text("abc")  # same size as S3
            with capture_logs() as logs:
                stats = _common._s3_sync("s3://bkt/external/", t, "external")
            self.assertEqual((Path(t) / "new.json").read_bytes(), b"12345")
        (event,) = _events(logs, "s3_sync_finished")
        self.assertEqual(stats, {"objects_transferred": 1, "objects_skipped": 1, "bytes": 5})
        self.assertEqual(event["direction"], "down")
        self.assertEqual(event["s3_uri"], "s3://bkt/external/")
        self.assertEqual(event["objects_transferred"], 1)
        self.assertEqual(event["objects_skipped"], 1)
        self.assertEqual(event["bytes"], 5)
        self.assertEqual(event["artifact"], "external")

    def test_upload_counts_transferred_and_unchanged(self):
        fake = FakeS3({("bkt", "p/same.json"): b"123"})
        self.use_s3(fake)
        with tempfile.TemporaryDirectory() as t:
            (Path(t) / "same.json").write_text("abc")
            (Path(t) / "sub").mkdir()
            (Path(t) / "sub" / "new.json").write_text("1234")
            with capture_logs() as logs:
                stats = _common._s3_sync(t, "s3://bkt/p/")
        (event,) = _events(logs, "s3_sync_finished")
        self.assertEqual(stats, {"objects_transferred": 1, "objects_skipped": 1, "bytes": 4})
        self.assertEqual(event["direction"], "up")
        self.assertEqual(event["s3_uri"], "s3://bkt/p/")
        self.assertIn(("bkt", "p/sub/new.json"), fake.objects)
        self.assertNotIn("artifact", event)

    def test_sync_tasks_name_their_artifact(self):
        self.use_s3(FakeS3())
        with tempfile.TemporaryDirectory() as t:
            with patch.object(_common, "_external_dir", return_value=Path(t)):
                with capture_logs() as logs:
                    _common.sync_external_from_s3.fn("r")
                    _common.sync_external_to_s3.fn("r")
                    _common.sync_external_to_s3_staging.fn("r")
        artifacts = [e["artifact"] for e in _events(logs, "s3_sync_finished")]
        self.assertEqual(artifacts, ["external", "external", "external_staging"])

    def test_promotion_logs_count_and_warns_when_empty(self):
        fake = FakeS3({("bkt", "runs/r/external-staging/a.json"): b"x"})
        self.use_s3(fake)
        with capture_logs() as logs:
            _common.promote_external_staging.fn("r")
        (event,) = _events(logs, "external_cache_promoted")
        self.assertEqual(event["objects_copied"], 1)
        self.assertEqual(event["src_prefix"], "runs/r/external-staging/")
        self.assertEqual(event["log_level"], "info")
        self.assertIn(("bkt", "external/a.json"), fake.objects)

        with capture_logs() as logs:
            _common.promote_external_staging.fn("other")
        (empty,) = _events(logs, "external_cache_promoted")
        self.assertEqual(empty["objects_copied"], 0)
        self.assertEqual(empty["log_level"], "warning")

    def test_sync_tasks_are_silent_without_a_bucket(self):
        with patch.object(_common, "S3_BUCKET", ""):
            with capture_logs() as logs:
                _common.sync_external_from_s3.fn("r")
                _common.sync_external_to_s3.fn("r")
                _common.sync_external_to_s3_staging.fn("r")
                _common.promote_external_staging.fn("r")
        self.assertEqual(logs, [])


class GithubStatusLoggingTestCase(unittest.TestCase):
    ENV = {
        "GITHUB_TOKEN": "ghp_supersecret",
        "GITHUB_REPOSITORY": "org/repo",
        "GITHUB_DEPLOYMENT_ID": "42",
    }

    def _post(self, env, urlopen=None):
        clean = {
            k: v for k, v in os.environ.items() if not k.startswith("GITHUB_")
        }
        with patch.dict(os.environ, {**clean, **env}, clear=True):
            with patch.object(_common.urllib.request, "urlopen", urlopen):
                with capture_logs() as logs:
                    _common.post_github_deployment_status(
                        state="success", description="done"
                    )
        return logs

    def test_missing_variables_are_named_not_valued(self):
        logs = self._post({"GITHUB_TOKEN": "ghp_supersecret"})
        (event,) = _events(logs, "github_status_skipped")
        self.assertEqual(event["missing_vars"], ["GITHUB_REPOSITORY", "GITHUB_DEPLOYMENT_ID"])
        self.assertEqual(event["log_level"], "warning")
        self.assertNotIn("ghp_supersecret", repr(logs))

    def test_success_logs_status_code(self):
        response = MagicMock(status=201)
        logs = self._post(self.ENV, urlopen=MagicMock(return_value=response))
        (event,) = _events(logs, "github_status_posted")
        self.assertEqual((event["state"], event["status_code"]), ("success", 201))
        self.assertFalse(event["log_url_attached"])
        self.assertNotIn("ghp_supersecret", repr(logs))

    def test_http_error_logs_reason_status_and_truncated_body(self):
        error = urllib.error.HTTPError(
            "https://api.github.com", 403, "Forbidden", {}, io.BytesIO(b"x" * 900)
        )
        logs = self._post(self.ENV, urlopen=MagicMock(side_effect=error))
        (event,) = _events(logs, "github_status_failed")
        self.assertEqual(event["reason"], "http_error")
        self.assertEqual(event["status_code"], 403)
        self.assertEqual(len(event["error"]), 500)
        self.assertNotIn("ghp_supersecret", repr(logs))

    def test_other_failure_logs_exception_type(self):
        logs = self._post(self.ENV, urlopen=MagicMock(side_effect=TimeoutError("slow")))
        (event,) = _events(logs, "github_status_failed")
        self.assertEqual(event["error_type"], "TimeoutError")
        self.assertEqual(event["error"], "slow")
        self.assertNotIn("reason", event)


class LookupLoggingTestCase(unittest.TestCase):
    def test_secrets_manager_failure_is_logged_then_raised(self):
        client = MagicMock()
        client.get_secret_value.side_effect = RuntimeError("no access")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("ARANGO_DB_PASSWORD", "PROJECT_NAME", "ENVIRONMENT")
        }
        env.update(PROJECT_NAME="nlm", ENVIRONMENT="dev")
        with patch.dict(os.environ, env, clear=True):
            with patch.object(_common.boto3, "client", return_value=client):
                with capture_logs() as logs:
                    with self.assertRaises(RuntimeError):
                        _common._get_or_create_arango_password()
        (event,) = _events(logs, "arango_password_fetch_failed")
        self.assertEqual(event["secret_id"], "/nlm/dev/secrets/arangodb-password")
        self.assertEqual((event["error_type"], event["error"]), ("RuntimeError", "no access"))
        self.assertEqual(event["log_level"], "error")

    def test_password_from_environment_logs_nothing(self):
        with patch.dict(os.environ, {"ARANGO_DB_PASSWORD": "pw"}):
            with capture_logs() as logs:
                self.assertEqual(_common._get_or_create_arango_password(), "pw")
        self.assertEqual(logs, [])

    def test_docker_unreachable_is_logged_and_returns_none(self):
        error = _common.docker_sdk.errors.DockerException("no daemon")
        with patch.object(_common.docker_sdk, "from_env", side_effect=error):
            with capture_logs() as logs:
                self.assertIsNone(_common._get_arangodb_id())
        (event,) = _events(logs, "docker_unreachable")
        self.assertEqual(event["error_type"], "DockerException")
        self.assertEqual(event["log_level"], "warning")


if __name__ == "__main__":
    unittest.main()
