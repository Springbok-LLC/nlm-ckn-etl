"""Every subprocess the flows launch receives the logging context.

``CORRELATION_ID`` and ``PHASE`` reach the worker scripts and Java through the
environment, so a run's lines join on one id and carry their phase.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

import _common  # noqa: E402
import fetch  # noqa: E402
import pipeline  # noqa: E402

_LOG_KEYS = ("CORRELATION_ID", "PHASE", "GIT_SHA")


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in _LOG_KEYS}
    return {**env, **extra}


class LogEnvTestCase(unittest.TestCase):
    def test_environment_correlation_id_wins(self):
        run = SimpleNamespace(id="flow-run-1")
        with patch.dict(os.environ, _clean_env(CORRELATION_ID="batch-1"), clear=True):
            with patch.object(_common, "flow_run", run):
                self.assertEqual(_common._log_env()["CORRELATION_ID"], "batch-1")

    def test_flow_run_id_used_when_environment_unset(self):
        run = SimpleNamespace(id="flow-run-1")
        with patch.dict(os.environ, _clean_env(), clear=True):
            with patch.object(_common, "flow_run", run):
                self.assertEqual(_common._log_env()["CORRELATION_ID"], "flow-run-1")

    def test_fallback_id_is_stable_within_a_process(self):
        run = SimpleNamespace(id=None)
        with patch.dict(os.environ, _clean_env(), clear=True):
            with patch.object(_common, "flow_run", run):
                first = _common._log_env()["CORRELATION_ID"]
                second = _common._log_env()["CORRELATION_ID"]
        self.assertEqual(first, second)
        self.assertEqual(len(first), 36)

    def test_git_sha_forwarded_only_when_set(self):
        run = SimpleNamespace(id="r")
        with patch.object(_common, "flow_run", run):
            with patch.dict(os.environ, _clean_env(GIT_SHA="abc123"), clear=True):
                self.assertEqual(_common._log_env()["GIT_SHA"], "abc123")
            with patch.dict(os.environ, _clean_env(), clear=True):
                self.assertNotIn("GIT_SHA", _common._log_env())

    def test_phase_set_when_given_and_left_alone_otherwise(self):
        run = SimpleNamespace(id="r")
        with patch.dict(os.environ, _clean_env(), clear=True):
            with patch.object(_common, "flow_run", run):
                self.assertEqual(_common._log_env("fetch")["PHASE"], "fetch")
                self.assertNotIn("PHASE", _common._log_env())


class LaunchEnvTestCase(unittest.TestCase):
    """The env handed to ``subprocess.run`` carries the id and the right phase."""

    def setUp(self):
        self.run = SimpleNamespace(id="flow-run-9")
        patcher = patch.dict(os.environ, _clean_env(GIT_SHA="sha9"), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(_common, "flow_run", self.run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(subprocess, "run")
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(pipeline, "get_run_logger", return_value=MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(fetch, "get_run_logger", return_value=MagicMock())
        patcher.start()
        self.addCleanup(patcher.stop)

    def _env(self):
        self.run_mock.assert_called_once()
        return self.run_mock.call_args.kwargs["env"]

    def _assert_context(self, phase):
        env = self._env()
        self.assertEqual(env["CORRELATION_ID"], "flow-run-9")
        self.assertEqual(env["GIT_SHA"], "sha9")
        self.assertEqual(env["PHASE"], phase)

    def test_run_python_script_forwards_phase(self):
        _common._run_python_script("X.py", "pw", phase="results")
        self._assert_context("results")

    def test_run_python_script_without_phase_sets_no_phase(self):
        _common._run_python_script("X.py", "pw")
        env = self._env()
        self.assertEqual(env["CORRELATION_ID"], "flow-run-9")
        self.assertNotIn("PHASE", env)

    def test_fetch_and_transform_are_phase_fetch(self):
        fetch.fetch_external_api_results.fn()
        self._assert_context("fetch")
        self.run_mock.reset_mock()
        fetch.transform_external_api_results.fn()
        self._assert_context("fetch")

    def test_ontology_launches_are_phase_ontology(self):
        # download_ontologies raises after the launch when no OWL files exist.
        with patch.object(Path, "glob", return_value=[Path("a.owl")]):
            pipeline.download_ontologies.fn("pw")
        self._assert_context("ontology")
        for task in (pipeline.slim_ontologies, pipeline.build_ontology_graph):
            self.run_mock.reset_mock()
            task.fn("pw")
            self._assert_context("ontology")

    def test_results_launches_are_phase_results(self):
        pipeline.write_tuples.fn("pw")
        self._assert_context("results")
        for task in (pipeline.build_results_graph, pipeline.build_induced_subgraph):
            self.run_mock.reset_mock()
            task.fn("pw")
            self._assert_context("results")


if __name__ == "__main__":
    unittest.main()
