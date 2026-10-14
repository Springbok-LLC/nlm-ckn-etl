"""Tests for CLI validation and release-config seeding (flow CLI audit P1).

Covers the Q6 (negative-age rejection) and Q2 (--release-config seeding)
behaviors, and pins the renamed flag/env/key names so a future rename breaks a
test rather than slipping through:

- Negative ``--max-*-age-hours`` is rejected at every CLI entry point
  (release.py / fetch.py ``_parse_args``, DataFetcher.py ``main``) and at the
  flow level (``nlm_ckn_release`` / ``nlm_ckn_fetch``), the latter covering
  Prefect-deployment runs that bypass argparse.
- ``_load_release_json`` seeds ``os.environ`` from a given config file, pinning
  the ``NLM_CKN_TAG`` env var and the ``nlm_ckn_tag`` key after the rename.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Make both python/src and python/src/flows importable.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import DataFetcher
import fetch
import release

_AGE_ENV = ("NLM_CKN_TAG", "GITHUB_REPO", "TAR_SOURCE", "MAX_FETCH_AGE_HOURS")


def _noop_logger():
    class _L:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    return _L()


class NegativeAgeCliTestCase(unittest.TestCase):
    """Negative ``--max-*-age-hours`` is rejected at each CLI entry point."""

    def test_release_cli_rejects_negative(self):
        with self.assertRaises(SystemExit):
            release._parse_args(["--nlm-ckn-tag", "vX", "--max-fetch-age-hours", "-1"])

    def test_release_cli_accepts_valid(self):
        args = release._parse_args(
            ["--nlm-ckn-tag", "vX", "--max-fetch-age-hours", "24"]
        )
        self.assertEqual(args.nlm_ckn_tag, "vX")
        self.assertEqual(args.max_fetch_age_hours, 24.0)

    def test_fetch_cli_rejects_negative(self):
        with self.assertRaises(SystemExit):
            fetch._parse_args(
                [
                    "--ncbi-email", "a@b.c",
                    "--ncbi-api-key", "k",
                    "--max-source-age-hours", "-1",
                ]
            )

    def test_datafetcher_cli_rejects_negative(self):
        argv = ["DataFetcher.py", "--max-source-age-hours", "-1"]
        with patch.object(sys, "argv", argv), self.assertRaises(SystemExit):
            DataFetcher.main()


class NegativeAgeFlowTestCase(unittest.TestCase):
    """Flow-level guards reject negatives even when the CLI is bypassed."""

    def test_release_flow_rejects_negative(self):
        with self.assertRaises(ValueError):
            release.nlm_ckn_release.fn("vX", max_fetch_age_hours=-1.0)

    def test_fetch_flow_rejects_negative(self):
        with patch("fetch.get_run_logger", return_value=_noop_logger()):
            with self.assertRaises(ValueError):
                fetch.nlm_ckn_fetch.fn(
                    ncbi_email="a@b.c", ncbi_api_key="k", max_source_age_hours=-1.0
                )


class LoadReleaseJsonSeedingTestCase(unittest.TestCase):
    """``_load_release_json`` seeds env defaults from the given config file."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _AGE_ENV}

    def tearDown(self):
        for k in _AGE_ENV:
            os.environ.pop(k, None)
            if self._saved.get(k) is not None:
                os.environ[k] = self._saved[k]

    def test_seeds_env_from_nlm_ckn_tag_key(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(
                {"nlm_ckn_tag": "v9.9.9", "github_repo": "Org/repo",
                 "hubmap_urls": ["x"]},
                f,
            )
            path = f.name
        try:
            release._load_release_json(path)
            self.assertEqual(os.environ["NLM_CKN_TAG"], "v9.9.9")
            self.assertEqual(os.environ["GITHUB_REPO"], "Org/repo")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
