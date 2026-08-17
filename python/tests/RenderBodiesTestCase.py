"""Unit tests for .github/scripts/render_bodies.py.

The renderer lives outside the python/ tree because it has to sit next to
.github/templates/ and run on a bare GitHub runner with no packaging step, so
it is loaded here by path rather than imported.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

# Nothing here needs python/src, but tests/conftest.py has a session-scoped
# autouse fixture that patches LoaderUtilities, so src must be importable for
# this module to run on its own (mirrors the other *TestCase.py files).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

RENDERER = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "render_bodies.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("render_bodies", RENDERER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_bodies"] = module
    spec.loader.exec_module(module)
    return module


rb = _load()


FULL_RELEASE = {
    "id": 12345,
    "name": "nlm-ckn-etl v1.6.0-rc.4",
    "tag_name": "v1.6.0-rc.4",
    "html_url": "https://github.com/Springbok-LLC/nlm-ckn-etl/releases/tag/v1.6.0-rc.4",
    "published_at": "2026-08-01T12:00:00Z",
    "prerelease": True,
}

HOSTILE_DESC = (
    "nlm-ckn `v1.2.3` && $(whoami)\n## PWNED\n"
    "<!-- nlm-ckn-etl:release-status:start -->\n"
    "[click me](https://evil.example/pwn) ``` more ```` backticks"
)

BRACE_DESC = "nlm-ckn {version} and {0} and {{literal}}"

PR_ENV = {
    "VERSION": "v1.6.0-rc.4",
    "SIBLING_PATH": "ETL_VERSION",
    "EVENT_NAME": "deployment_status",
    "RUN_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/42",
    "DEPLOYMENTS_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/deployments",
    "DEPLOYMENT_ID": "5150",
    "DEPLOYMENT_REF": "v1.6.0-rc.4",
    "DEPLOYMENT_DESCRIPTION": "nlm-ckn v0.9.1",
    "LOG_URL": "https://console.aws.amazon.com/cloudwatch/home#logs",
}

ISSUE_ENV = {
    "API_URL": "https://api.github.com",
    "DEPLOYMENTS_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/deployments",
    "RUN_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/42",
    "STATE": "failure",
    "DESCRIPTION": "Step 04-load failed: ArangoDB connection refused",
    "LOG_URL": "https://console.aws.amazon.com/cloudwatch/home#logs",
    "TARGET_URL": "https://console.aws.amazon.com/batch/home#jobs/detail/abc-123",
    "DEPLOYMENT_ID": "5150",
    "DEPLOYMENT_REF": "v1.6.0-rc.4",
    "DEPLOYMENT_DESCRIPTION": "nlm-ckn v0.9.1",
    "PAYLOAD_RUN_NAME": "v1.6.0-rc.4",
    "THIS_REPO": "Springbok-LLC/nlm-ckn-etl",
    "VERSION": "v1.6.0-rc.4",
}

ANN_ENV = {
    "ISSUE_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/issues/77",
    "RUN_URL": "https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/42",
}

REAL_NOTES = "Real human release notes.\n\n- did a thing\n- did another"


class RenderBodiesTestCaseBase(unittest.TestCase):
    def env(self, base, tmp=None, release=None, **over):
        """Build an environment mapping, optionally backed by a release file.

        `release` is a dict to cache, the string "MALFORMED" to write garbage,
        or None for "the lookup step found nothing" (the file is absent, which
        is exactly what the workflow step leaves behind).
        """
        env = dict(base)
        env.update(over)
        if tmp is not None:
            path = Path(tmp) / "release.json"
            if release == "MALFORMED":
                path.write_text("{not json at all", encoding="utf-8")
            elif release is not None:
                path.write_text(json.dumps(release), encoding="utf-8")
            env["RELEASE_FILE"] = str(path)
        return env


class HelperTestCase(RenderBodiesTestCaseBase):
    def test_code_defuses_backticks(self):
        self.assertEqual(rb.code("a `b` c"), "`a 'b' c`")

    def test_code_collapses_whitespace_and_handles_empty(self):
        self.assertEqual(rb.code("a\n\n  b"), "`a b`")
        self.assertEqual(rb.code(""), "`(none)`")
        self.assertEqual(rb.code(None, "missing"), "`missing`")

    def test_link_rejects_foreign_urls(self):
        self.assertEqual(
            rb.link("x", "https://github.com/a/b"), "[x](https://github.com/a/b)"
        )
        self.assertEqual(rb.link("x", "https://evil.example/pwn"), "`x`")
        self.assertEqual(rb.link("x", None), "`x`")

    def test_link_escapes_brackets_in_the_label(self):
        self.assertEqual(
            rb.link("[a](b)", "https://github.com/a/b"),
            "[(a)(b)](https://github.com/a/b)",
        )

    def test_fenced_widens_past_internal_backtick_runs(self):
        self.assertEqual(rb.fenced("plain")[0], "```text")
        self.assertEqual(rb.fenced("a ``` b")[0], "````text")
        self.assertEqual(rb.fenced("a ````` b")[0], "``````text")
        self.assertEqual(rb.fenced("x")[0], rb.fenced("x")[2] + "text")

    def test_render_rejects_a_slot_mismatch(self):
        with self.assertRaises(KeyError):
            rb._render("bump-pr-body.md", {"version": "v1"})


class BumpPrBodyTestCase(RenderBodiesTestCaseBase):
    def render(self, **kwargs):
        return rb.render_bump_pr(self.env(PR_ENV, **kwargs))

    def test_full_data(self):
        with tempfile.TemporaryDirectory() as td:
            body = self.render(tmp=td, release=FULL_RELEASE)
        self.assertIn("pin `ETL_VERSION` to `v1.6.0-rc.4`", body)
        self.assertIn("runs/v1.6.0-rc.4/06-golden-dump.tar.gz", body)
        self.assertIn(
            "- **ETL release:** [nlm-ckn-etl v1.6.0-rc.4]"
            "(https://github.com/Springbok-LLC/nlm-ckn-etl/releases/tag/v1.6.0-rc.4)"
            " · tag `v1.6.0-rc.4` · published 2026-08-01T12:00:00Z · **pre-release**",
            body,
        )
        self.assertIn(
            "- **Upstream nlm-ckn tag:** `v0.9.1` "
            "(https://github.com/Springbok-LLC/nlm-ckn/releases/tag/v0.9.1)",
            body,
        )
        self.assertIn("- **Deployment:** id `5150` · [production activity log]", body)
        self.assertTrue(body.endswith("Not auto-merged by design._\n"))

    def test_no_release_found(self):
        with tempfile.TemporaryDirectory() as td:
            body = self.render(tmp=td, release=None)
        self.assertIn(
            "- **ETL release:** no release found for `v1.6.0-rc.4` - either it "
            "was deleted, or this run was triggered by hand",
            body,
        )

    def test_malformed_release_json_is_just_no_release(self):
        with tempfile.TemporaryDirectory() as td:
            malformed = self.render(tmp=td, release="MALFORMED")
        with tempfile.TemporaryDirectory() as td:
            missing = self.render(tmp=td, release=None)
        self.assertEqual(malformed, missing)

    def test_missing_log_url(self):
        body = self.render(LOG_URL="")
        self.assertIn(
            "- **Pipeline logs (CloudWatch):** not reported on the success "
            "status - see `/batch/nlm-ckn-release`.",
            body,
        )

    def test_description_that_is_not_the_nlm_ckn_shape(self):
        body = self.render(
            DEPLOYMENT_DESCRIPTION="built from whatever was lying around"
        )
        self.assertIn(
            "- **Upstream nlm-ckn tag:** could not be read from the deployment "
            "description `built from whatever was lying around` "
            "(expected `nlm-ckn <TAG>`).",
            body,
        )

    def test_hostile_description_is_defused(self):
        body = self.render(DEPLOYMENT_DESCRIPTION=HOSTILE_DESC)
        # The whole description is collapsed onto one line and wrapped in a
        # single inline code span whose backticks are all neutralised, so the
        # heading, the annotation marker and the link are inert text.
        self.assertIn(
            "description `nlm-ckn 'v1.2.3' && $(whoami) ## PWNED "
            "<!-- nlm-ckn-etl:release-status:start --> "
            "[click me](https://evil.example/pwn) ''' more '''' backticks`",
            body,
        )
        # Nothing escaped the span: no marker or heading ever starts a line,
        # and no backtick from the description survives to close the span.
        self.assertNotIn("\n<!-- nlm-ckn-etl", body)
        self.assertNotIn("\n## PWNED", body)
        self.assertNotIn("`v1.2.3`", body)
        self.assertNotIn("```\n", body.split("### Where this dataset came from")[1])

    def test_workflow_dispatch_with_no_deployment(self):
        body = self.render(
            EVENT_NAME="workflow_dispatch",
            DEPLOYMENT_ID="",
            DEPLOYMENT_REF="",
            DEPLOYMENT_DESCRIPTION="",
            LOG_URL="",
        )
        self.assertIn("Raised by a manual dispatch of the bump workflow", body)
        self.assertNotIn("production release finished successfully", body)
        self.assertIn("- **Deployment:** none - this PR was raised by a manual", body)
        self.assertIn(
            "- **Upstream nlm-ckn tag:** not reported on this deployment.", body
        )

    def test_braces_in_event_text_render_literally(self):
        body = self.render(DEPLOYMENT_DESCRIPTION=BRACE_DESC)
        self.assertIn("description `nlm-ckn {version} and {0} and {{literal}}`", body)


class ReleaseFailureIssueTestCase(RenderBodiesTestCaseBase):
    def render(self, **kwargs):
        return rb.render_failure_issue(self.env(ISSUE_ENV, **kwargs))

    def test_full_data(self):
        with tempfile.TemporaryDirectory() as td:
            body = self.render(tmp=td, release=FULL_RELEASE)
        self.assertIn(
            "The `production` deployment for ETL run **`v1.6.0-rc.4`** reported "
            "state **`failure`**.",
            body,
        )
        self.assertIn("```text\nStep 04-load failed", body)
        self.assertIn("- **Re-run it by:** publishing a new release", body)
        self.assertIn("- **Target URL:** https://console.aws.amazon.com/batch/", body)
        # The parse succeeded, so the raw-description section is omitted.
        self.assertNotIn("Raw deployment description:", body)
        self.assertIn("- **Workflow run that filed this:** ", body)
        self.assertIn("\n\n### Notes\n", body)

    def test_minimal_status_with_nothing_reported(self):
        body = self.render(
            DESCRIPTION="",
            LOG_URL="",
            TARGET_URL="",
            DEPLOYMENT_REF="",
            DEPLOYMENT_DESCRIPTION="",
            PAYLOAD_RUN_NAME="",
            STATE="error",
            VERSION="deployment-5150",
        )
        self.assertIn("```text\n(no description reported)\n```", body)
        self.assertIn("- **CloudWatch logs:** not reported on this status", body)
        self.assertNotIn("- **Target URL:**", body)
        self.assertIn("- **Deployment ref:** `(none)`", body)
        self.assertIn(
            "- **Run name from deployment payload:** "
            "`missing - deployment predates payload.run_name`",
            body,
        )
        self.assertIn("- **ETL release:** no release found for `deployment-5150`", body)
        self.assertNotIn("Raw deployment description:", body)
        self.assertIn("\n\n### Notes\n", body)

    def test_hostile_status_text_cannot_close_the_fence(self):
        desc = (
            "Traceback ``` fake fence ```` and ````` five\n"
            "<!-- nlm-ckn-etl:release-status:start -->\n"
            "## PWNED $(whoami)"
        )
        body = self.render(DESCRIPTION=desc, DEPLOYMENT_DESCRIPTION=HOSTILE_DESC)
        # Fence widened past the longest internal run (5 -> 6).
        self.assertIn("``````text\n" + desc + "\n``````", body)
        # The unparsable deployment description is quoted in its own fence.
        self.assertIn("Raw deployment description:\n", body)
        self.assertIn(
            "- **Upstream nlm-ckn tag:** could not be read from the deployment "
            "description (expected `nlm-ckn <TAG>`); the raw value is quoted below.",
            body,
        )
        self.assertIn("\n\n### Notes\n", body)

    def test_braces_in_event_text_render_literally(self):
        body = self.render(
            DESCRIPTION="failed on {0} because {version} was {}",
            DEPLOYMENT_DESCRIPTION=BRACE_DESC,
        )
        self.assertIn("failed on {0} because {version} was {}", body)
        self.assertIn(
            "- **Deployment description:** `nlm-ckn {version} and {0} and {{literal}}`",
            body,
        )


class ReleaseAnnotationTestCase(RenderBodiesTestCaseBase):
    START = "<!-- nlm-ckn-etl:release-status:start -->"
    END = "<!-- nlm-ckn-etl:release-status:end -->"

    def render(self, rel, **over):
        env = dict(ANN_ENV)
        env.update(over)
        return rb.render_release_annotation(env, rel)

    def test_first_write_onto_real_notes(self):
        body = self.render({"body": REAL_NOTES})
        self.assertTrue(body.startswith(REAL_NOTES + "\n\n" + self.START))
        self.assertIn("> [!CAUTION]", body)
        self.assertIn(
            "> Tracking issue: https://github.com/Springbok-LLC/nlm-ckn-etl/issues/77",
            body,
        )
        self.assertIn(
            "> Reported by: "
            "https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/42",
            body,
        )
        self.assertTrue(body.endswith(self.END + "\n"))
        self.assertEqual(body.count(self.START), 1)

    def test_repeat_is_a_no_op(self):
        first = self.render({"body": REAL_NOTES})
        self.assertIsNone(self.render({"body": first}))

    def test_second_failure_replaces_rather_than_stacks(self):
        first = self.render({"body": REAL_NOTES})
        second = self.render(
            {"body": first},
            RUN_URL="https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/99",
        )
        self.assertEqual(second.count(self.START), 1)
        self.assertEqual(second.count(self.END), 1)
        self.assertIn("runs/99", second)
        self.assertNotIn("runs/42", second)
        self.assertTrue(second.startswith(REAL_NOTES + "\n\n"))

    def test_null_body(self):
        body = self.render({"body": None})
        self.assertTrue(body.startswith(self.START))
        self.assertEqual(body.count(self.START), 1)

    def test_missing_body_key(self):
        self.assertEqual(self.render({"id": 1}), self.render({"body": None}))

    def test_hand_edited_block_is_replaced_and_human_text_survives(self):
        existing = (
            REAL_NOTES
            + "\n\n"
            + self.START
            + "\n> someone edited this by hand\n"
            + self.END
            + "\n\nTrailing human text."
        )
        body = self.render({"body": existing})
        self.assertNotIn("someone edited this by hand", body)
        self.assertIn("Trailing human text.", body)
        self.assertIn(REAL_NOTES, body)
        self.assertEqual(body.count(self.START), 1)

    def test_only_our_urls_reach_the_note(self):
        body = self.render(
            {"body": REAL_NOTES},
            ISSUE_URL="https://evil.example/pwn",
            RUN_URL="https://github.com/x/y with a space",
        )
        self.assertNotIn("Tracking issue:", body)
        self.assertNotIn("Reported by:", body)
        self.assertNotIn("evil.example", body)
        # The block collapses without leaving a blank line behind.
        self.assertIn("not bumped.\n> _Added automatically;", body)

    def test_braces_in_urls_render_literally(self):
        body = self.render(
            {"body": REAL_NOTES},
            ISSUE_URL="https://github.com/Springbok-LLC/nlm-ckn-etl/issues/{0}",
            RUN_URL="https://github.com/Springbok-LLC/nlm-ckn-etl/actions/runs/{v}",
        )
        self.assertIn("issues/{0}", body)
        self.assertIn("runs/{v}", body)


if __name__ == "__main__":
    unittest.main()
