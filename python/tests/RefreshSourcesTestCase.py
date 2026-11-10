"""Tests for refreshing single external sources and resuming a cached fetch.

Covers:
- parse_refresh_sources: lists, comma-separated strings, validation
- fetch_external_api_results / transform_external_api_results: the flags passed
- nlm_ckn_release: fetch_mode and refresh_sources
- _load_release_json and the release CLI: the release.json keys
"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

# Make both python/src and python/src/flows importable.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import fetch as _fetch_module  # noqa: E402
import release as _release_module  # noqa: E402
from _common import parse_refresh_sources  # noqa: E402


class ParseRefreshSourcesTestCase(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_refresh_sources(None), [])
        self.assertEqual(parse_refresh_sources(""), [])
        self.assertEqual(parse_refresh_sources([]), [])

    def test_comma_separated_string(self):
        self.assertEqual(
            parse_refresh_sources("cellxgene, gene,,cellxgene"), ["cellxgene", "gene"]
        )

    def test_list(self):
        self.assertEqual(parse_refresh_sources(["uniprot", "opentargets"]), ["uniprot", "opentargets"])

    def test_unknown_source_is_rejected(self):
        with self.assertRaises(ValueError):
            parse_refresh_sources("cellxgene,hubmap")


class SourceFlagsTestCase(unittest.TestCase):
    def _fetch_args(self, **kwargs):
        with patch.object(_fetch_module, "_run_python_script") as run:
            _fetch_module.fetch_external_api_results.fn(**kwargs)
        self.assertEqual(run.call_args[0][0], "DataFetcher.py")
        return run.call_args[1]["extra_args"]

    def _transform_runs(self, **kwargs):
        """Return the arguments of each DataTransformer.py run."""
        with patch.object(_fetch_module, "_run_python_script") as run:
            _fetch_module.transform_external_api_results.fn(**kwargs)
        for call in run.call_args_list:
            self.assertEqual(call[0][0], "DataTransformer.py")
        return [call[1]["extra_args"] for call in run.call_args_list]

    def test_fetch_forces_only_the_named_sources(self):
        args = self._fetch_args(force_sources=["cellxgene"], run_name="r")
        self.assertEqual(args, ["--force-cellxgene", "--run-name", "r"])

    def test_fetch_force_all_wins(self):
        args = self._fetch_args(force=True, force_sources=["cellxgene"])
        self.assertEqual(args, ["--force-all"])

    def test_fetch_without_sources_is_unchanged(self):
        self.assertIsNone(self._fetch_args())

    def test_transform_forces_the_named_sources_and_checks_the_rest(self):
        # Naming sources restricts a DataTransformer.py run to them, so the
        # others must be transformed by a run of their own.
        runs = self._transform_runs(force_sources=["cellxgene"], run_name="r")
        self.assertEqual(
            runs,
            [
                ["opentargets", "gene", "uniprot", "--run-name", "r"],
                ["cellxgene", "--force", "--run-name", "r"],
            ],
        )

    def test_transform_of_every_source_forced_needs_no_other_run(self):
        runs = self._transform_runs(
            force_sources=["cellxgene", "opentargets", "gene", "uniprot"]
        )
        self.assertEqual(
            runs, [["cellxgene", "opentargets", "gene", "uniprot", "--force"]]
        )

    def test_transform_force_all_wins(self):
        self.assertEqual(
            self._transform_runs(force=True, force_sources=["cellxgene"]), [["--force"]]
        )

    def test_transform_without_sources_is_unchanged(self):
        self.assertEqual(self._transform_runs(), [None])


class _Stop(Exception):
    """Ends a release run once the fetch step has been exercised."""


class ReleaseFlowFetchModeTestCase(unittest.TestCase):
    def _run(self, **kwargs):
        """Run the release flow up to the end of the fetch step."""
        rel = _release_module
        with patch.object(rel, "_read_release_json", return_value={"hubmap_urls": ["u"]}), \
             patch.object(rel, "post_github_deployment_status") as status, \
             patch.object(rel, "extract_release_tarball"), \
             patch.object(rel, "sync_release_dir_to_s3"), \
             patch.object(rel, "resolve_fetch_force") as resolve, \
             patch.object(rel, "nlm_ckn_fetch") as fetch, \
             patch.object(rel, "nlm_ckn_etl", side_effect=_Stop):
            resolve.return_value = kwargs.pop("auto_force", False)
            with self.assertRaises(_Stop):
                rel.nlm_ckn_release.fn(
                    nlm_ckn_tag="v1.0.0-rc.1", tar_source="x.tar.gz", **kwargs
                )
        return resolve, fetch, status

    def test_auto_decides_from_the_cache(self):
        resolve, fetch, _ = self._run(auto_force=True)

        resolve.assert_called_once()
        self.assertTrue(fetch.call_args[1]["force"])
        self.assertFalse(fetch.call_args[1]["retry_empty"])
        self.assertEqual(fetch.call_args[1]["refresh_sources"], [])

    def test_resume_skips_the_hash_check(self):
        resolve, fetch, status = self._run(
            fetch_mode="resume", refresh_sources="cellxgene", auto_force=True
        )

        resolve.assert_not_called()
        self.assertFalse(fetch.call_args[1]["force"])
        self.assertTrue(fetch.call_args[1]["retry_empty"])
        self.assertEqual(fetch.call_args[1]["refresh_sources"], ["cellxgene"])
        descriptions = [c[1]["description"] for c in status.call_args_list]
        self.assertIn("[2/3] Fetching external APIs (incremental, refreshing cellxgene)", descriptions)

    def test_unknown_fetch_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            _release_module.nlm_ckn_release.fn(
                nlm_ckn_tag="v1.0.0-rc.1", fetch_mode="skip"
            )

    def test_unknown_refresh_source_is_rejected(self):
        with self.assertRaises(ValueError):
            _release_module.nlm_ckn_release.fn(
                nlm_ckn_tag="v1.0.0-rc.1", refresh_sources="hubmap"
            )


class ReleaseConfigTestCase(unittest.TestCase):
    def _seed(self, data):
        keys = ("FETCH_MODE", "REFRESH_SOURCES", "NLM_CKN_TAG", "GITHUB_REPO", "TAR_SOURCE")
        with patch.dict(os.environ, {}, clear=False):
            for k in keys:
                os.environ.pop(k, None)
            with patch.object(_release_module, "_read_release_json", return_value=data):
                _release_module._load_release_json()
            return {k: os.environ.get(k) for k in ("FETCH_MODE", "REFRESH_SOURCES")}

    def test_release_json_seeds_the_defaults(self):
        seeded = self._seed({"fetch_mode": "resume", "refresh_sources": ["cellxgene", "gene"]})
        self.assertEqual(seeded, {"FETCH_MODE": "resume", "REFRESH_SOURCES": "cellxgene,gene"})

    def test_release_json_without_the_keys_seeds_nothing(self):
        self.assertEqual(self._seed({}), {"FETCH_MODE": None, "REFRESH_SOURCES": None})

    def test_cli_flags(self):
        with patch.dict(os.environ, {"NLM_CKN_TAG": "v1"}):
            args = _release_module._parse_args(
                ["--fetch-mode", "resume", "--refresh-sources", "cellxgene,uniprot"]
            )
        self.assertEqual(args.fetch_mode, "resume")
        self.assertEqual(args.refresh_sources, ["cellxgene", "uniprot"])

    def test_cli_rejects_unknown_values(self):
        with patch.dict(os.environ, {"NLM_CKN_TAG": "v1"}):
            with self.assertRaises(SystemExit):
                _release_module._parse_args(["--refresh-sources", "hubmap"])
            with self.assertRaises(SystemExit):
                _release_module._parse_args(["--fetch-mode", "skip"])


if __name__ == "__main__":
    unittest.main()
