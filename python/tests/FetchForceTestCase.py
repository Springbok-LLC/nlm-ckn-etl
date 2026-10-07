import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "flows"))

import _common  # noqa: E402

_QUIET = lambda *args, **kwargs: None  # noqa: E731


class FetchForceTestCase(unittest.TestCase):
    """Tests for _common.should_force_fetch / _fetch_code_hash."""

    def _write_info(self, d, *, age_hours=1.0, code_hash="MATCH"):
        if code_hash == "MATCH":
            code_hash = _common._fetch_code_hash()
        ts = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
        (d / "fetch-info.json").write_text(
            json.dumps({"fetched_at": ts, "fetch_code_hash": code_hash})
        )

    def _decide(self, d, max_age=672.0):
        with patch.object(_common, "S3_BUCKET", ""), patch.object(
            _common, "_external_dir", return_value=d
        ):
            return _common.should_force_fetch("test-run", max_age)

    def test_reuse_when_fresh_and_code_unchanged(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._write_info(d, age_hours=1.0)
            self.assertFalse(self._decide(d))

    def test_force_when_code_changed(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._write_info(d, age_hours=1.0, code_hash="0000deadbeef0000")
            self.assertTrue(self._decide(d))

    def test_force_when_stale(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            self._write_info(d, age_hours=1000.0)  # > 672
            self.assertTrue(self._decide(d))

    def test_resume_when_missing(self):
        # No marker → resume from on-disk cache (do NOT force-wipe), so a run
        # that fetched data but failed validation isn't re-fetched from scratch.
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(self._decide(Path(t)))  # no fetch-info.json

    def test_resume_when_validated_false_but_fresh(self):
        # A validation failure records validated:false; a fresh, code-matching
        # marker should still resume (reuse on-disk data), not force-wipe.
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "fetch-info.json").write_text(json.dumps({
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "fetch_code_hash": _common._fetch_code_hash(),
                "validated": False,
            }))
            self.assertFalse(self._decide(d))

    def test_resume_when_fetched_at_corrupt(self):
        # A present-but-corrupt marker resumes rather than wiping.
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "fetch-info.json").write_text(json.dumps({
                "fetched_at": "not-a-timestamp",
                "fetch_code_hash": _common._fetch_code_hash(),
            }))
            self.assertFalse(self._decide(d))

    def test_fetch_code_hash_stable_and_16_char(self):
        h1 = _common._fetch_code_hash()
        h2 = _common._fetch_code_hash()
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        int(h1, 16)  # valid hex

    def test_record_fetch_artifact_writes_validated(self):
        # B: a validation failure records validated=False (not discarded).
        import fetch

        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            with patch("fetch.get_run_logger", return_value=MagicMock()), \
                 patch("fetch._external_dir", return_value=d), \
                 patch("fetch.REPO_ROOT", d), \
                 patch("fetch.create_markdown_artifact"):
                fetch.record_fetch_artifact.fn(run_name="x", validated=False)
            info = json.loads((d / "fetch-info.json").read_text())
        self.assertFalse(info["validated"])
        self.assertIn("fetch_code_hash", info)


if __name__ == "__main__":
    unittest.main()
