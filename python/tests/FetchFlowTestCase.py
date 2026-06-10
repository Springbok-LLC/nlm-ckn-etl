"""Tests for flows/fetch.py.

Covers the cache-marker functionality added to support reusing the external
API cache after a failed release:

- _git_short_commit: success (stripped hash) and failure ("unknown") paths
- _write_fetch_info: field shape, ISO-8601 UTC timestamp, file-size mapping
  (present → byte size, absent → None), run-name defaulting
- write_fetch_marker: writes fetch-info.json for the current run
- record_fetch_artifact: writes fetch-info.json and emits the Prefect artifact,
  tolerating a missing or corrupt fetch-status.json
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

# Make both python/src and python/src/flows importable (mirrors other tests).
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

from fetch import (  # noqa: E402
    _git_short_commit,
    _write_fetch_info,
    record_fetch_artifact,
    write_fetch_marker,
)


def _noop_logger():
    """Return a silent MagicMock that satisfies logger calls inside tasks."""
    m = MagicMock()
    m.info = lambda *a, **kw: None
    m.warning = lambda *a, **kw: None
    m.error = lambda *a, **kw: None
    return m


class GitShortCommitTestCase(unittest.TestCase):
    """Tests for the _git_short_commit helper."""

    def test_returns_stripped_hash(self):
        with patch("fetch.subprocess.check_output", return_value="abc1234\n"):
            self.assertEqual(_git_short_commit(), "abc1234")

    def test_returns_unknown_on_failure(self):
        with patch("fetch.subprocess.check_output", side_effect=OSError("no git")):
            self.assertEqual(_git_short_commit(), "unknown")


class WriteFetchInfoTestCase(unittest.TestCase):
    """Tests for the _write_fetch_info helper."""

    def test_writes_expected_fields_and_file_sizes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            # One present raw file; everything else (incl. transformed) absent.
            (ext / "cellxgene.json").write_text('{"a": 1}')

            with patch("fetch._git_short_commit", return_value="deadbee"):
                info = _write_fetch_info(ext, "myrun")

            # Returned dict and the written file agree.
            on_disk = json.loads((ext / "fetch-info.json").read_text())
            self.assertEqual(info, on_disk)

            self.assertEqual(info["commit"], "deadbee")
            self.assertEqual(info["run"], "myrun")
            # Present file → integer byte size; absent files → None.
            self.assertEqual(info["files"]["cellxgene.json"], len('{"a": 1}'))
            self.assertIsNone(info["files"]["gene.json"])
            self.assertIsNone(info["files"]["cellxgene_transformed.json"])

    def test_fetched_at_is_iso8601_utc(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("fetch._git_short_commit", return_value="x"):
                info = _write_fetch_info(Path(tmpdir), "r")
            parsed = datetime.fromisoformat(info["fetched_at"])
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))

    def test_run_defaults_to_env_then_full(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            with patch("fetch._git_short_commit", return_value="x"):
                # Empty run + CKN_RUN set → uses the env value.
                with patch.dict(os.environ, {"CKN_RUN": "envrun"}):
                    self.assertEqual(_write_fetch_info(ext, "")["run"], "envrun")
                # Empty run + CKN_RUN unset → falls back to "full".
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(_write_fetch_info(ext, "")["run"], "full")


class WriteFetchMarkerTestCase(unittest.TestCase):
    """Tests for the write_fetch_marker task."""

    def test_writes_marker_for_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            with patch("fetch.get_run_logger", return_value=_noop_logger()), \
                 patch("fetch._external_dir", return_value=ext), \
                 patch("fetch.REPO_ROOT", ext), \
                 patch("fetch._git_short_commit", return_value="cafe123"):
                write_fetch_marker.fn(run="some-run")

            info = json.loads((ext / "fetch-info.json").read_text())
            self.assertEqual(info["run"], "some-run")
            self.assertEqual(info["commit"], "cafe123")
            self.assertIn("fetched_at", info)


class RecordFetchArtifactTestCase(unittest.TestCase):
    """Tests for the record_fetch_artifact task."""

    def _run(self, ext: Path):
        with patch("fetch.get_run_logger", return_value=_noop_logger()), \
             patch("fetch._external_dir", return_value=ext), \
             patch("fetch.REPO_ROOT", ext), \
             patch("fetch._git_short_commit", return_value="abc"), \
             patch("fetch.S3_BUCKET", "my-bucket"), \
             patch("fetch.create_markdown_artifact") as mock_artifact:
            record_fetch_artifact.fn(run="rec-run")
        return mock_artifact

    def test_writes_fetch_info_and_emits_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            (ext / "gene.json").write_text('{"gene_entrez_ids": []}')
            mock_artifact = self._run(ext)

            info = json.loads((ext / "fetch-info.json").read_text())
            self.assertEqual(info["run"], "rec-run")
            self.assertEqual(info["commit"], "abc")
            self.assertEqual(
                info["files"]["gene.json"], len('{"gene_entrez_ids": []}')
            )
            mock_artifact.assert_called_once()
            self.assertEqual(
                mock_artifact.call_args.kwargs["key"], "fetch-summary"
            )

    def test_preserves_existing_fetched_at(self):
        """A refresh keeps the original fetch-completion timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            original = "2026-01-01T00:00:00+00:00"
            (ext / "fetch-info.json").write_text(
                json.dumps({"fetched_at": original, "commit": "old", "files": {}})
            )
            self._run(ext)
            info = json.loads((ext / "fetch-info.json").read_text())
            self.assertEqual(info["fetched_at"], original)
            self.assertEqual(info["commit"], "abc")  # commit still refreshed

    def test_tolerates_missing_fetch_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # No fetch-status.json present.
            mock_artifact = self._run(Path(tmpdir))
            mock_artifact.assert_called_once()

    def test_tolerates_corrupt_fetch_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            (ext / "fetch-status.json").write_text("not json {{{")
            mock_artifact = self._run(ext)
            mock_artifact.assert_called_once()

    def test_includes_source_outcomes_from_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ext = Path(tmpdir)
            (ext / "fetch-status.json").write_text(
                json.dumps(
                    {"cellxgene": {"last_outcome": "ok", "last_success_at": "2026-01-01"}}
                )
            )
            mock_artifact = self._run(ext)
            markdown = mock_artifact.call_args.kwargs["markdown"]
            self.assertIn("cellxgene", markdown)
            self.assertIn("2026-01-01", markdown)


if __name__ == "__main__":
    unittest.main()
