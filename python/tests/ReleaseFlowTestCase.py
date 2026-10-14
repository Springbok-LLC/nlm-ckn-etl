"""Tests for flows/release.py.

Covers:
- _read_release_json: local file, missing file, S3 temp-file cleanup
- resolve_fetch_force: no cache, fresh cache, stale cache, S3 temp-file cleanup
- extract_release_tarball: flat extraction, hubmap URLs, prefix filter,
  directory/symlink skip, path-traversal filter
"""

import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make both python/src and python/src/flows importable.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import _common  # noqa: E402
import release as _release_module
from release import (
    _read_release_json,
    extract_release_tarball,
    resolve_fetch_force,
)


def _noop_logger():
    """Return a silent MagicMock that satisfies logger calls inside tasks."""
    m = MagicMock()
    m.info = lambda *a, **kw: None
    m.warning = lambda *a, **kw: None
    m.error = lambda *a, **kw: None
    return m


class ReadReleaseJsonTestCase(unittest.TestCase):
    """Tests for the _read_release_json helper."""

    def test_reads_local_file(self):
        """Parses a local JSON file and returns its contents."""
        data = {"nlm_ckn_tag": "v2026-04", "hubmap_urls": ["https://example.com"]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = f.name
        try:
            result = _read_release_json(tmp)
        finally:
            os.unlink(tmp)
        self.assertEqual(result["nlm_ckn_tag"], "v2026-04")

    def test_returns_empty_dict_for_missing_file(self):
        """Returns {} when the local path does not exist."""
        result = _read_release_json("/nonexistent/path/release.json")
        self.assertEqual(result, {})

    def test_s3_temp_file_cleaned_up_on_success(self):
        """Temp file is removed after a successful S3 read."""
        data = {"nlm_ckn_tag": "v2026-04"}
        created_paths = []

        def fake_download(bucket, key, dest):
            created_paths.append(Path(dest))
            Path(dest).write_text(json.dumps(data))

        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = fake_download

        with patch("release.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = _read_release_json("s3://my-bucket/release.json")

        self.assertEqual(result["nlm_ckn_tag"], "v2026-04")
        self.assertEqual(len(created_paths), 1)
        self.assertFalse(created_paths[0].exists(), "Temp file must be cleaned up on success")

    def test_s3_temp_file_cleaned_up_on_parse_error(self):
        """Temp file is removed even when the downloaded content is invalid JSON."""
        created_paths = []

        def fake_download(bucket, key, dest):
            created_paths.append(Path(dest))
            Path(dest).write_bytes(b"not valid json {{")

        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = fake_download

        with patch("release.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            with self.assertRaises(Exception):
                _read_release_json("s3://my-bucket/release.json")

        self.assertEqual(len(created_paths), 1)
        self.assertFalse(created_paths[0].exists(), "Temp file must be cleaned up on error")


class ResolveFetchForceTestCase(unittest.TestCase):
    """Tests for resolve_fetch_force (thin wrapper over _common.should_force_fetch)."""

    def _call(self, **kwargs):
        """Invoke the task's underlying function, bypassing the Prefect runtime."""
        return resolve_fetch_force.fn(**kwargs)

    def _fresh_info(self, hours_old):
        """fetch-info.json with the CURRENT code hash so the age branch is exercised."""
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat()
        return {"fetched_at": ts, "fetch_code_hash": _common._fetch_code_hash()}

    def test_returns_false_when_no_local_fetch_info(self):
        """Returns False (resume from on-disk cache) when fetch-info.json is absent —
        a fetch that failed validation must not be discarded and re-fetched."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("_common.S3_BUCKET", ""), \
                 patch("_common._external_dir", return_value=Path(tmpdir)):
                result = self._call(run_name="test-run", max_fetch_age_hours=48.0)
        self.assertFalse(result)

    def test_returns_false_when_local_cache_fresh(self):
        """Returns False when fresh AND the fetch code is unchanged."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "fetch-info.json").write_text(json.dumps(self._fresh_info(10)))
            with patch("_common.S3_BUCKET", ""), \
                 patch("_common._external_dir", return_value=Path(tmpdir)):
                result = self._call(run_name="test-run", max_fetch_age_hours=48.0)
        self.assertFalse(result)

    def test_returns_true_when_local_cache_stale(self):
        """Returns True when older than the threshold (code unchanged)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "fetch-info.json").write_text(json.dumps(self._fresh_info(72)))
            with patch("_common.S3_BUCKET", ""), \
                 patch("_common._external_dir", return_value=Path(tmpdir)):
                result = self._call(run_name="test-run", max_fetch_age_hours=48.0)
        self.assertTrue(result)

    def test_returns_true_when_fetch_code_changed(self):
        """Returns True when the fetch_code_hash differs, even if the cache is fresh."""
        with tempfile.TemporaryDirectory() as tmpdir:
            info = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "fetch_code_hash": "0000deadbeef0000",
            }
            (Path(tmpdir) / "fetch-info.json").write_text(json.dumps(info))
            with patch("_common.S3_BUCKET", ""), \
                 patch("_common._external_dir", return_value=Path(tmpdir)):
                result = self._call(run_name="test-run", max_fetch_age_hours=48.0)
        self.assertTrue(result)

    def test_s3_temp_file_cleaned_up_when_json_invalid(self):
        """Temp file is removed even when the S3 fetch-info.json contains bad JSON."""
        created_paths = []

        def fake_download(bucket, key, dest):
            created_paths.append(Path(dest))
            Path(dest).write_text("not json {{{")

        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = fake_download

        with patch("_common.S3_BUCKET", "my-bucket"), \
             patch("_common.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            # Bad JSON is caught -> no marker -> resume from on-disk cache.
            result = resolve_fetch_force.fn(run_name="test-run", max_fetch_age_hours=48.0)

        self.assertFalse(result, "Unreadable fetch-info.json should resume, not force")
        self.assertEqual(len(created_paths), 1, "download must have been attempted")
        self.assertFalse(created_paths[0].exists(), "Temp file must be cleaned up")

    def test_s3_temp_file_cleaned_up_on_success(self):
        """Temp file is removed after a successful S3 fetch-info.json read."""
        fetch_info = self._fresh_info(5)
        created_paths = []

        def fake_download(bucket, key, dest):
            created_paths.append(Path(dest))
            Path(dest).write_text(json.dumps(fetch_info))

        mock_s3 = MagicMock()
        mock_s3.download_file.side_effect = fake_download

        with patch("_common.S3_BUCKET", "my-bucket"), \
             patch("_common.boto3") as mock_boto3:
            mock_boto3.client.return_value = mock_s3
            result = resolve_fetch_force.fn(run_name="test-run", max_fetch_age_hours=48.0)

        self.assertFalse(result)
        self.assertEqual(len(created_paths), 1)
        self.assertFalse(created_paths[0].exists(), "Temp file must be cleaned up on success")


class ExtractReleaseTarballTestCase(unittest.TestCase):
    """Tests for the extract_release_tarball Prefect task."""

    _PREFIX = "data/prod/"

    def _make_tar(self, members: dict, dest: Path) -> Path:
        """Create a .tar.gz at *dest* mapping arcname → bytes content."""
        with tarfile.open(dest, "w:gz") as tf:
            for arcname, content in members.items():
                raw = content.encode() if isinstance(content, str) else content
                info = tarfile.TarInfo(name=arcname)
                info.size = len(raw)
                tf.addfile(info, io.BytesIO(raw))
        return dest

    def _call(self, tar_source, run_name, hubmap_urls, repo_root):
        with patch("release.REPO_ROOT", repo_root):
            return extract_release_tarball.fn(tar_source, run_name, hubmap_urls)

    def test_files_extracted_flat(self):
        """Files nested under data/prod/ land flat in results_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar = self._make_tar(
                {
                    "data/prod/organ_a/results_ensg_lung.csv": "col1,col2\n1,2",
                    "data/prod/organ_b/results_ensg_heart.csv": "col1,col2\n3,4",
                },
                root / "release.tar.gz",
            )
            results_dir = self._call(str(tar), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_lung.csv").exists())
            self.assertTrue((results_dir / "results_ensg_heart.csv").exists())

    def test_hubmap_urls_written(self):
        """hubmap_urls.txt is created with one URL per line."""
        urls = ["https://hubmap1.example.com", "https://hubmap2.example.com"]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar = self._make_tar(
                {"data/prod/results_ensg_lung.csv": "a,b\n1,2"},
                root / "release.tar.gz",
            )
            results_dir = self._call(str(tar), "test-run", urls, root)

            txt = (results_dir / "hubmap_urls.txt").read_text()
            for url in urls:
                self.assertIn(url, txt)

    def test_files_outside_prefix_skipped(self):
        """Files not under data/prod/ are not extracted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar = self._make_tar(
                {
                    "data/prod/results_ensg_good.csv": "a,b\n1,2",
                    "README.txt": "should be skipped",
                    "data/other/skipped.csv": "x,y\n1,2",
                },
                root / "release.tar.gz",
            )
            results_dir = self._call(str(tar), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_good.csv").exists())
            self.assertFalse((results_dir / "README.txt").exists())
            self.assertFalse((results_dir / "skipped.csv").exists())

    def test_directory_entries_skipped(self):
        """Directory entries in the tarball are not created in results_dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar_path = root / "release.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tf:
                dir_info = tarfile.TarInfo(name="data/prod/subdir/")
                dir_info.type = tarfile.DIRTYPE
                tf.addfile(dir_info)
                data = b"col\n1"
                file_info = tarfile.TarInfo(name="data/prod/subdir/results_ensg_file.csv")
                file_info.size = len(data)
                tf.addfile(file_info, io.BytesIO(data))
            results_dir = self._call(str(tar_path), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_file.csv").exists())
            self.assertFalse((results_dir / "subdir").exists())

    def test_symlinks_skipped(self):
        """Symlink entries in the tarball are not followed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar_path = root / "release.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tf:
                data = b"col\n1"
                file_info = tarfile.TarInfo(name="data/prod/results_ensg_real.csv")
                file_info.size = len(data)
                tf.addfile(file_info, io.BytesIO(data))

                sym_info = tarfile.TarInfo(name="data/prod/results_ensg_link.csv")
                sym_info.type = tarfile.SYMTYPE
                sym_info.linkname = "/etc/passwd"
                tf.addfile(sym_info)
            results_dir = self._call(str(tar_path), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_real.csv").exists())
            self.assertFalse((results_dir / "results_ensg_link.csv").exists())

    def test_path_traversal_via_prefix_blocked(self):
        """A member whose name starts with data/prod/ but traverses upward is blocked."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar_path = root / "release.tar.gz"
            with tarfile.open(tar_path, "w:gz") as tf:
                good_data = b"good"
                good_info = tarfile.TarInfo(name="data/prod/results_ensg_good.csv")
                good_info.size = len(good_data)
                tf.addfile(good_info, io.BytesIO(good_data))

                # Traversal: after stripping the prefix the member name resolves
                # outside results_dir via the path-traversal check.
                evil_data = b"evil"
                evil_info = tarfile.TarInfo(name="data/prod/../../outside.txt")
                evil_info.size = len(evil_data)
                tf.addfile(evil_info, io.BytesIO(evil_data))

            results_dir = self._call(str(tar_path), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_good.csv").exists())
            # The evil file must not appear anywhere outside results_dir.
            self.assertFalse((root / "outside.txt").exists())
            self.assertFalse((root / "data" / "outside.txt").exists())

    def test_existing_results_dir_cleared(self):
        """If results_dir already exists, it is wiped before extraction."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stale_file = root / "data" / "results-test-run" / "stale.csv"
            stale_file.parent.mkdir(parents=True)
            stale_file.write_text("old data")

            tar = self._make_tar(
                {"data/prod/results_ensg_fresh.csv": "new,data\n1,2"},
                root / "release.tar.gz",
            )
            results_dir = self._call(str(tar), "test-run", [], root)

            self.assertTrue((results_dir / "results_ensg_fresh.csv").exists())
            self.assertFalse((results_dir / "stale.csv").exists())

    def test_missing_results_ensg_raises(self):
        """A tarball with no results_ensg_*.csv fails loudly rather than
        silently producing an empty results_dir.  Guards against an outdated
        NSForest naming convention (pre results_ensg/symbols split)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar = self._make_tar(
                {"data/prod/organ_a/old_results.csv": "col1,col2\n1,2"},
                root / "release.tar.gz",
            )
            with self.assertRaises(FileNotFoundError):
                self._call(str(tar), "test-run", [], root)

    def test_per_organ_manifests_unioned(self):
        """The per-organ master_s3_manifest.csv files are unioned into one
        complete top-level manifest rather than colliding on a single basename
        (which would leave only the last-extracted organ's rows)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            tar = self._make_tar(
                {
                    "data/prod/organ_a/results_ensg_a.csv": "c\n1",
                    "data/prod/organ_a/master_s3_manifest.csv": (
                        "filename,s3_path\n"
                        "results_ensg_a.csv,s3://bucket/a\n"
                    ),
                    "data/prod/organ_b/results_ensg_b.csv": "c\n2",
                    "data/prod/organ_b/master_s3_manifest.csv": (
                        "filename,s3_path\n"
                        "results_ensg_b.csv,s3://bucket/b\n"
                    ),
                },
                root / "release.tar.gz",
            )
            results_dir = self._call(str(tar), "test-run", [], root)

            manifest = (results_dir / "master_s3_manifest.csv").read_text()
            # Both organs' rows survive — not just the last-extracted one.
            self.assertIn("results_ensg_a.csv", manifest)
            self.assertIn("results_ensg_b.csv", manifest)


if __name__ == "__main__":
    unittest.main()
