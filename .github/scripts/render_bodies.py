#!/usr/bin/env python3
"""Render the markdown bodies used by .github/workflows/bump-ui-etl-version.yml.

    render_bodies.py bump-pr                <out.md>
    render_bodies.py release-failure-issue  <out.md>
    render_bodies.py release-annotation     <release.json> <patch.json>

Stdlib only, and deliberately so: this runs on the bare GitHub runner with no
setup-python step and no pip install. Do not add a dependency here without also
adding the install step to every job in the workflow that calls it.

-- Why a script and templates instead of a heredoc ---------------------------
The three bodies used to be assembled by python3 heredocs embedded in the
workflow's run: blocks. The prose now lives in .github/templates/*.md and the
logic lives here, which makes the prose reviewable as prose and lets the
rendering be unit tested (python/tests/RenderBodiesTestCase.py).

Most of the provenance in these bodies is optional - there may be no release,
no CloudWatch URL, no deployment at all on a manual dispatch - so a template
language would have to grow conditionals. It does not: every conditional
section is a single placeholder (e.g. {provenance_block}) whose value this
module assembles from small format strings. Templates stay declarative.

-- Untrusted input, and why str.format cannot be turned against us -----------
Everything event-controlled (the deployment description above all) reaches this
module through os.environ, never through the script text, and is defused by
code() / fenced() / link() before it is substituted. On top of that, the
substitution itself is injection-safe by construction:

  * Templates are rendered with str.format_map in EXACTLY ONE pass. format_map
    does not re-scan the values it substitutes, so a description of "{0}" or
    "{version}" lands in the output as those literal characters. It cannot be
    read as a placeholder, cannot index into anything, and cannot raise.
  * A value produced by this module is NEVER fed through format()/format_map()
    again. The conditional blocks are built with "...{}...".format(value) -
    also a single pass over a trusted format string - and the resulting block
    is then handed to the outer format_map as a value, so its braces are inert.
  * _render() checks that the template's placeholder set is exactly the set of
    slots supplied, so a stray "{" added to the prose is a loud error at render
    time rather than a silent KeyError or a swallowed brace.

The templates themselves are trusted repo content. Braces in template PROSE
must be doubled ({{ }}); the _render() check above is what catches a forgotten
one.

-- Block value convention ----------------------------------------------------
A placeholder that stands for an optional *section* (as opposed to an inline
value) carries its own trailing separator, so that an absent section leaves no
blank line behind. Those are called out at each build site below.
"""

import json
import os
import re
import string
import sys
from pathlib import Path

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

# Markers delimiting the annotate-release note. Kept here rather than in the
# template because the same two strings are also the regex that finds and
# replaces a previous note; one source of truth for both.
ANNOTATION_START = "<!-- nlm-ckn-etl:release-status:start -->"
ANNOTATION_END = "<!-- nlm-ckn-etl:release-status:end -->"

UPSTREAM_TAG_URL = "https://github.com/Springbok-LLC/nlm-ckn/releases/tag/"


# -- Rendering helpers --------------------------------------------------------
# Behaviour-preserving copies of the helpers that lived in the workflow's three
# heredocs (where they were maintained as three near-identical duplicates).


def clean(s):
    """Collapse arbitrary event text into one whitespace-normal line."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def code(s, empty="(none)"):
    """Inline code with backticks defused, so text cannot break out."""
    s = clean(s)
    return "`{}`".format(s.replace("`", "'") if s else empty)


def link(text, url, prefix="https://github.com/"):
    """A markdown link, or plain escaped text if the URL is not one of ours.

    Brackets in the label are escaped so the label cannot swallow the target.
    """
    label = clean(text).replace("[", "(").replace("]", ")")
    if isinstance(url, str) and url.startswith(prefix) and " " not in url:
        return "[{}]({})".format(label or url, url)
    return code(label)


def fenced(text, lang="text"):
    """Fence `text` with a backtick run longer than any inside it.

    A status description containing ``` therefore cannot close the block early
    and inject markdown (or an HTML comment marker) into the issue.
    """
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    bar = "`" * max(3, longest + 1)
    return [bar + lang, text, bar]


# -- Template loading ---------------------------------------------------------


def _render(name, slots):
    """Substitute `slots` into the named template in a single format_map pass.

    The placeholder set in the template must match `slots` exactly. That check
    is the tripwire for a literal brace accidentally introduced into the prose
    (it would show up as an unexpected field name, or as a ValueError from
    parse()), and for a slot renamed on only one side.
    """
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    fields = {
        field for _, field, _, _ in string.Formatter().parse(text) if field is not None
    }
    if fields != set(slots):
        raise KeyError(
            "{}: template placeholders {} do not match supplied slots {}".format(
                name, sorted(fields), sorted(slots)
            )
        )
    return text.format_map(slots)


def _load_release(env):
    """The release object the lookup step cached, or {} if there is none.

    The step exports RELEASE_FILE and rm -f's the file when nothing matched, so
    an absent file means "no release found". A malformed file means the same:
    it must never fail a bump or suppress a failure report.
    """
    path = env.get("RELEASE_FILE") or ""
    if path and os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}
    return {}


def _release_bits(rel):
    """The ' · '-joined description of a resolved release."""
    bits = [
        link(rel.get("name") or rel.get("tag_name") or "release", rel.get("html_url"))
    ]
    if rel.get("tag_name"):
        bits.append("tag {}".format(code(rel.get("tag_name"))))
    if rel.get("published_at"):
        bits.append("published {}".format(clean(rel.get("published_at"))))
    if rel.get("prerelease"):
        bits.append("**pre-release**")
    return " · ".join(bits)


def _upstream(env):
    """(cleaned deployment description, upstream tag, upstream release URL).

    trigger-release.sh writes "nlm-ckn <TAG>", but description is free text on
    the API, so treat a non-match as simply unknown rather than trusting
    whatever is there to be a tag.
    """
    dep_desc = clean(env.get("DEPLOYMENT_DESCRIPTION"))
    m = re.match(r"^nlm-ckn[\s:]+(\S+)$", dep_desc)
    upstream = m.group(1) if m else ""
    upstream_url = ""
    if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$", upstream):
        upstream_url = UPSTREAM_TAG_URL + upstream
    return dep_desc, upstream, upstream_url


# -- bump-pr ------------------------------------------------------------------

# The dispatch path has no successful release behind it, so do not claim one -
# it is the sentence a reviewer reads first.
LEDE_DISPATCH = (
    "Raised by a manual dispatch of the bump workflow in **nlm-ckn-etl**, not "
    "by a release. It points this repo at the named ETL dataset."
)
LEDE_RELEASE = (
    "The **nlm-ckn-etl** production release finished successfully. This PR "
    "points this repo at the dataset it produced."
)

# Name whatever was actually searched for, so "not found" is actionable rather
# than just absent.
PR_NO_RELEASE = (
    "- **ETL release:** no release found for {} - either it was deleted, or "
    "this run was triggered by hand rather than by publishing a release."
)
PR_UPSTREAM_UNPARSED = (
    "- **Upstream nlm-ckn tag:** could not be read from the deployment "
    "description {} (expected `nlm-ckn <TAG>`)."
)
UPSTREAM_NOT_REPORTED = "- **Upstream nlm-ckn tag:** not reported on this deployment."
# There is no per-id web page for a deployment, so link the environment's
# activity log - the closest thing a human can open.
PR_DEPLOYMENT = (
    "- **Deployment:** id {} · "
    "[production activity log]({}/activity_log?environment=production)"
)
PR_NO_DEPLOYMENT = (
    "- **Deployment:** none - this PR was raised by a manual `workflow_dispatch` "
    "of the bump workflow, not by a deployment status."
)
PR_NO_LOGS = (
    "- **Pipeline logs (CloudWatch):** not reported on the success status - see "
    "`/batch/nlm-ckn-release`."
)


def render_bump_pr(env):
    rel = _load_release(env)
    dep_desc, upstream, upstream_url = _upstream(env)
    version = env["VERSION"]

    # Each provenance line is emitted only when its value exists, and has a
    # plain-language fallback when the value is one a reviewer would otherwise
    # go looking for. {provenance_block} sits between two fixed lines, so it
    # needs no trailing separator.
    lines = []
    if rel.get("html_url"):
        lines.append("- **ETL release:** {}".format(_release_bits(rel)))
    else:
        lines.append(PR_NO_RELEASE.format(code(env.get("DEPLOYMENT_REF") or version)))

    if upstream and upstream_url:
        lines.append(
            "- **Upstream nlm-ckn tag:** {} ({})".format(code(upstream), upstream_url)
        )
    elif upstream:
        lines.append("- **Upstream nlm-ckn tag:** {}".format(code(upstream)))
    elif dep_desc:
        lines.append(PR_UPSTREAM_UNPARSED.format(code(dep_desc)))
    else:
        lines.append(UPSTREAM_NOT_REPORTED)

    if env.get("DEPLOYMENT_ID"):
        lines.append(
            PR_DEPLOYMENT.format(code(env.get("DEPLOYMENT_ID")), env["DEPLOYMENTS_URL"])
        )
    elif env.get("EVENT_NAME") == "workflow_dispatch":
        lines.append(PR_NO_DEPLOYMENT)

    if env.get("LOG_URL"):
        lines.append("- **Pipeline logs (CloudWatch):** {}".format(env["LOG_URL"]))
    else:
        lines.append(PR_NO_LOGS)

    lines.append("- **Bump workflow run:** {}".format(env["RUN_URL"]))

    return _render(
        "bump-pr-body.md",
        {
            "version": version,
            "sibling_path": env["SIBLING_PATH"],
            "lede": LEDE_DISPATCH
            if env.get("EVENT_NAME") == "workflow_dispatch"
            else LEDE_RELEASE,
            "dump_key": "runs/{}/06-golden-dump.tar.gz".format(version),
            "provenance_block": "\n".join(lines),
        },
    )


# -- release-failure-issue ----------------------------------------------------

ISSUE_RERUN = (
    "- **Re-run it by:** publishing a new release, or dispatching "
    "`trigger-release.yml` with the same upstream tag. Re-publishing an "
    "existing release does not re-fire `on-release.yml`."
)
ISSUE_NO_RELEASE = (
    "- **ETL release:** no release found for {} - either it was deleted, or "
    "this run was started by hand rather than by publishing a release."
)
ISSUE_UPSTREAM_UNPARSED = (
    "- **Upstream nlm-ckn tag:** could not be read from the deployment "
    "description (expected `nlm-ckn <TAG>`); the raw value is quoted below."
)
ISSUE_NO_LOGS = (
    "- **CloudWatch logs:** not reported on this status - check "
    "`/batch/nlm-ckn-release` in CloudWatch."
)


def render_failure_issue(env):
    rel = _load_release(env)
    dep_desc, upstream, upstream_url = _upstream(env)
    desc = (env.get("DESCRIPTION") or "").strip() or "(no description reported)"
    run_name = env.get("PAYLOAD_RUN_NAME") or ""

    release_lines = []
    if rel.get("html_url"):
        release_lines.append("- **ETL release:** {}".format(_release_bits(rel)))
        release_lines.append(ISSUE_RERUN)
    else:
        release_lines.append(
            ISSUE_NO_RELEASE.format(
                code(env.get("DEPLOYMENT_REF") or run_name or env.get("VERSION"))
            )
        )
    if upstream and upstream_url:
        release_lines.append(
            "- **Upstream nlm-ckn tag:** {} ({})".format(code(upstream), upstream_url)
        )
    elif upstream:
        release_lines.append("- **Upstream nlm-ckn tag:** {}".format(code(upstream)))
    elif dep_desc:
        release_lines.append(ISSUE_UPSTREAM_UNPARSED)
    else:
        release_lines.append(UPSTREAM_NOT_REPORTED)

    where = []
    if env.get("LOG_URL"):
        where.append("- **CloudWatch logs:** {}".format(env["LOG_URL"]))
    else:
        where.append(ISSUE_NO_LOGS)
    if env.get("TARGET_URL"):
        where.append("- **Target URL:** {}".format(env["TARGET_URL"]))
    where += [
        # There is no per-id web page for a deployment, so link the
        # environment's activity log (human) and the API object (machine).
        "- **Deployment activity log:** {}/activity_log?environment=production".format(
            env["DEPLOYMENTS_URL"]
        ),
        "- **Deployment id:** `{}` (API: {}/repos/{}/deployments/{})".format(
            env["DEPLOYMENT_ID"],
            env["API_URL"],
            env["THIS_REPO"],
            env["DEPLOYMENT_ID"],
        ),
        # code() rather than a bare `{}`: ref and description are strings a
        # caller controls, and a backtick in either would otherwise close the
        # span and let the rest render as markdown.
        "- **Deployment ref:** {}".format(code(env.get("DEPLOYMENT_REF"))),
        "- **Deployment description:** {}".format(
            code(env.get("DEPLOYMENT_DESCRIPTION"))
        ),
        "- **Run name from deployment payload:** {}".format(
            code(run_name, "missing - deployment predates payload.run_name")
        ),
        "- **Workflow run that filed this:** {}".format(env["RUN_URL"]),
    ]

    # Only worth the space when the parse above failed: the reader needs to see
    # the exact string to work out what wrote it. Carries its own trailing
    # blank line so that omitting it leaves no gap (see the header).
    raw_block = ""
    if dep_desc and not upstream:
        raw_block = (
            "Raw deployment description:\n" + "\n".join(fenced(dep_desc)) + "\n\n"
        )

    return _render(
        "release-failure-issue.md",
        {
            "version": env["VERSION"],
            "state": env["STATE"],
            "status_fence": "\n".join(fenced(desc)),
            "release_block": "\n".join(release_lines),
            "where_block": "\n".join(where),
            "raw_description_block": raw_block,
        },
    )


# -- release-annotation -------------------------------------------------------


def _safe_url(u):
    """Only our own URLs go into the note, but validate anyway: the note is
    written into a public artifact, so nothing unvalidated belongs in it."""
    u = (u or "").strip()
    ok = u.startswith("https://github.com/") and not re.search(r"\s", u)
    return u if ok else ""


def render_release_annotation(env, rel):
    """Return the new release body, or None when it already matches."""
    body = rel.get("body") or ""

    # Each link is its own complete line; the block is newline-terminated so
    # that an absent link leaves no blank line (see the header).
    links = ""
    issue_url = _safe_url(env.get("ISSUE_URL"))
    run_url = _safe_url(env.get("RUN_URL"))
    if issue_url:
        links += "> Tracking issue: {}\n".format(issue_url)
    if run_url:
        links += "> Reported by: {}\n".format(run_url)

    # rstrip: the template file ends with a newline, but the note is spliced
    # into the middle of a body here and gets its separators added below.
    note = _render(
        "release-annotation.md",
        {
            "marker_start": ANNOTATION_START,
            "marker_end": ANNOTATION_END,
            "links_block": links,
        },
    ).rstrip("\n")

    # Replace any previous block rather than appending, so repeated failure
    # statuses for one run leave exactly one note behind.
    pattern = re.compile(
        r"\n*"
        + re.escape(ANNOTATION_START)
        + r".*?"
        + re.escape(ANNOTATION_END)
        + r"\n*",
        re.DOTALL,
    )
    stripped = pattern.sub("\n", body).rstrip()
    new_body = (stripped + "\n\n" + note + "\n") if stripped else (note + "\n")
    return None if new_body == body else new_body


# -- Entry point --------------------------------------------------------------


def main(argv):
    target = argv[1]
    if target in ("bump-pr", "release-failure-issue"):
        render = render_bump_pr if target == "bump-pr" else render_failure_issue
        # No trailing newline added: the template file supplies exactly one.
        with open(argv[2], "w", encoding="utf-8") as fh:
            fh.write(render(os.environ))
        return 0

    if target == "release-annotation":
        with open(argv[2], encoding="utf-8") as fh:
            rel = json.load(fh)
        new_body = render_release_annotation(os.environ, rel)
        if new_body is None:
            print("unchanged", file=sys.stderr)
            return 3  # distinct code: caller treats this as "skip"
        with open(argv[3], "w", encoding="utf-8") as fh:
            json.dump({"body": new_body}, fh)
        return 0

    print("unknown target: {}".format(target), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
