"""Tests for post_github_deployment_status in flows/_common.py.

The deployment statuses release.py posts drive everything downstream of a
release (bump-ui-etl-version.yml, build-neo4j-image.yml), so a rejected token
must be reported as an ERROR that says what was lost and how to re-post it.

Covers:
- 401 / 403: ERROR naming the lost status, its listeners, and the exact
  ``gh api`` command to re-post it; never raises
- any other HTTP error: WARNING only
- missing env vars: no request at all
"""

import io
import os
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

# Make both python/src and python/src/flows importable.
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC / "flows"))
sys.path.insert(0, str(_SRC))

from _common import post_github_deployment_status  # noqa: E402

REPO = "Springbok-LLC/nlm-ckn-etl"
DEPLOYMENT_ID = "6333642120"
ENV = {"GITHUB_TOKEN": "ghp_x", "GITHUB_REPOSITORY": REPO, "GITHUB_DEPLOYMENT_ID": DEPLOYMENT_ID}


def _http_error(code, body):
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", {}, io.BytesIO(body.encode())
    )


class PostGithubDeploymentStatusTestCase(unittest.TestCase):

    def _post(self, error=None, env=ENV, state="success"):
        """Post one status with urlopen failing with ``error``; return (logs, urlopen mock)."""
        with patch.dict(os.environ, env, clear=True), \
             patch("_common._cloudwatch_log_url", return_value=""), \
             patch("_common.urllib.request.urlopen", side_effect=error) as urlopen, \
             self.assertLogs("_common", level="INFO") as logs:
            post_github_deployment_status(state=state, description="done")
        return logs, urlopen

    def _levels(self, logs, level):
        return [r.getMessage() for r in logs.records if r.levelname == level]

    def test_rejected_token_is_an_error_with_recovery(self):
        """A 401 names the lost status, what listens for it, and how to re-post it."""
        logs, _ = self._post(_http_error(401, '{"message": "Bad credentials"}'))
        errors = self._levels(logs, "ERROR")
        self.assertEqual(len(errors), 1)
        message = errors[0]
        self.assertIn("Bad credentials", message)
        self.assertIn("'success' status was NOT posted", message)
        self.assertIn("build-neo4j-image.yml", message)
        self.assertIn("bump-ui-etl-version.yml", message)
        self.assertIn(
            f"gh api repos/{REPO}/deployments/{DEPLOYMENT_ID}/statuses "
            "-f state=success -f environment=production",
            message,
        )

    def test_forbidden_is_also_an_error(self):
        """A token that lost access to the repo is as fatal to the listeners as a dead one."""
        logs, _ = self._post(_http_error(403, '{"message": "Resource not accessible"}'), state="failure")
        self.assertIn("-f state=failure", self._levels(logs, "ERROR")[0])

    def test_other_http_errors_stay_warnings(self):
        logs, _ = self._post(_http_error(502, "Bad gateway"))
        self.assertEqual(self._levels(logs, "ERROR"), [])
        self.assertEqual(len(self._levels(logs, "WARNING")), 1)

    def test_missing_env_makes_no_request(self):
        """Local runs (no deployment) never call GitHub."""
        _, urlopen = self._post(env={"GITHUB_REPOSITORY": REPO})
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
