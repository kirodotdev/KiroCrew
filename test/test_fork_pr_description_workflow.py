"""Behavioural tests for .github/workflows/fork-pr-description.yml.

The workflow replaces a PR comment that four maintainers' local crons each
posted independently (PR #5038 collected eight copies of one message) with a
single check-run keyed on the head SHA. Its decisions live in shell inside
`run:` blocks, so these tests extract each step and execute it for real, with
`gh` replaced by a stub. Five properties are verified rather than assumed,
because each has a failure mode that produces a plausible-looking wrong answer:

* the heading match must accept a SUFFIXED heading -- the template itself ships
  "## What changed (motivation -> approach -> change)", so an exact-line match
  would fail every PR that used the template correctly;
* a DRAFT with a complete description must resolve `neutral`, not `failure`: a
  parked PR is not a defect, and reporting it red would put a permanent red row
  on every work-in-progress fork PR;
* a draft that is ALSO missing sections must resolve `failure`, since the
  missing sections need fixing either way -- draft must not mask a real defect;
* a PR body is fork-controlled, so it must never reach the shell as code, and
  must not be able to forge the multiline `$GITHUB_OUTPUT` delimiter and inject
  arbitrary step outputs;
* a heading must be matched as a real heading LINE outside fenced code, because
  matching the raw body passes a PR that only mentions "## Tests" in a sentence
  -- or that pastes the template into a fence to ask about it; and the fence
  scan must follow CommonMark rather than toggle a boolean, since a
  four-backtick block containing three-backtick examples flips a toggle off
  mid-block and starts accepting the enclosed text as headings;
* a failed check-run lookup must NOT read as "no row exists" -- swallowing the
  error sends a transient 5xx down the POST path and creates the duplicate row
  the lookup exists to prevent;
* a CRLF body must behave exactly like an LF one: GitHub returns web-authored PR
  bodies with CRLF, and a stray CR on a closing fence line would stop the fence
  closing and mark a template-compliant PR non-compliant;
* the check-run must be published against the PR HEAD sha -- `pull_request_target`
  sets `github.sha` to the BASE, and publishing against that would attach the
  result to a commit the PR does not show;
* publishing must REUSE the existing row: `POST /check-runs` creates rather than
  upserts, so re-running on `edited` against an unchanged head would stack one
  row per edit and reproduce the very duplication this workflow removes.

Skipped where the POSIX toolchain the scripts need is unavailable, matching the
guards in test_memory_benchmark_workflow.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "fork-pr-description.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists() or os.name == "nt" or shutil.which("bash") is None,
    reason="requires the workflow file plus a POSIX bash",
)

COMPLETE_BODY = """\
## Problem / Motivation

Something is broken.

## Why it matters

Users hit it daily.

## What changed (motivation -> approach -> change)

Fixed the thing.

## Tests

Added a regression test.
"""


def _doc() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(doc: dict) -> dict:
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1).
    return doc.get("on", doc.get(True))


def _step(name_fragment: str, job: str = "describe") -> dict:
    for step in _doc()["jobs"][job]["steps"]:
        if name_fragment.lower() in str(step.get("name", "")).lower():
            return step
    raise AssertionError(f"no step in job {job!r} whose name contains {name_fragment!r}")


def _static_env(step: dict) -> dict[str, str]:
    """The step's `env:` entries that carry no `${{ }}` expression."""
    out = {}
    for key, value in (step.get("env") or {}).items():
        text = str(value)
        if "${{" not in text:
            out[key] = text
    return out


def _parse_outputs(path: Path) -> dict[str, str]:
    """Parse a $GITHUB_OUTPUT file, including `key<<DELIM` heredoc blocks."""
    result: dict[str, str] = {}
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "<<" in line and "=" not in line.split("<<", 1)[0]:
            key, delim = line.split("<<", 1)
            body: list[str] = []
            i += 1
            while i < len(lines) and lines[i] != delim:
                body.append(lines[i])
                i += 1
            result[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
        i += 1
    return result


def _evaluate(tmp_path: Path, body: str, draft: bool) -> dict[str, str]:
    step = _step("Evaluate PR description")
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    env = dict(os.environ)
    env.update(_static_env(step))
    env.update(
        {
            "PR_BODY": body,
            "PR_DRAFT": "true" if draft else "false",
            "GITHUB_OUTPUT": str(output),
            "GITHUB_REPOSITORY": "kirodotdev/KiroCrew",
        }
    )
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert proc.returncode == 0, proc.stderr
    return _parse_outputs(output)


# ── The heading match must tolerate how the template actually reads ──────────


def test_a_complete_description_passes_with_the_templates_suffixed_heading(
    tmp_path: Path,
) -> None:
    """The shipped template writes "## What changed (motivation -> ...)".

    Matching headings as whole lines would fail every PR that filled the
    template in correctly -- a gate that is wrong on the compliant case.
    """
    outputs = _evaluate(tmp_path, COMPLETE_BODY, False)
    assert outputs["conclusion"] == "success"


def test_an_empty_description_reports_every_required_section(tmp_path: Path) -> None:
    outputs = _evaluate(tmp_path, "", False)
    assert outputs["conclusion"] == "failure"
    assert outputs["title"] == "4 required description sections are missing"
    for section in ("## Problem / Motivation", "## Why it matters", "## What changed", "## Tests"):
        assert section in outputs["summary"]


def test_one_missing_section_is_named_in_the_singular(tmp_path: Path) -> None:
    body = COMPLETE_BODY.replace("## Tests", "## Testing")
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"
    assert outputs["title"] == "1 required description section is missing"
    assert "`## Tests`" in outputs["summary"]
    assert "## Why it matters" not in outputs["summary"]


def test_the_match_is_case_insensitive(tmp_path: Path) -> None:
    outputs = _evaluate(tmp_path, COMPLETE_BODY.lower(), False)
    assert outputs["conclusion"] == "success"


def test_a_crlf_body_reads_the_same_as_an_lf_one(tmp_path: Path) -> None:
    """GitHub returns web-authored bodies with CRLF.

    This is the compliant case, so getting it wrong fails good PRs rather than
    letting bad ones through -- the more damaging direction for a first-time
    contributor.
    """
    outputs = _evaluate(tmp_path, COMPLETE_BODY.replace("\n", "\r\n"), False)
    assert outputs["conclusion"] == "success"


def test_a_crlf_closing_fence_still_closes(tmp_path: Path) -> None:
    """A fence closer may carry only whitespace, and CR is not whitespace here.

    Left unnormalised the fence never closes, every heading below the code block
    is swallowed, and the PR is marked non-compliant for having a code sample.
    """
    body = ("```\nsome code\n```\n" + COMPLETE_BODY).replace("\n", "\r\n")
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "success"


def test_a_crlf_fenced_template_is_still_excluded(tmp_path: Path) -> None:
    """Normalising CR must not weaken the fence exclusion itself."""
    body = ("```\n" + COMPLETE_BODY + "\n```\n").replace("\n", "\r\n")
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"


# ── Draft is a state, not a defect ───────────────────────────────────────────


def test_a_complete_draft_resolves_neutral_rather_than_red(tmp_path: Path) -> None:
    """A parked PR should not carry a permanent red row.

    `neutral` renders grey and still carries the reason in the check output.
    """
    outputs = _evaluate(tmp_path, COMPLETE_BODY, True)
    assert outputs["conclusion"] == "neutral"
    assert "ready for review" in outputs["summary"]


def test_draft_never_masks_a_missing_section(tmp_path: Path) -> None:
    """The sections need adding either way, so the defect wins the conclusion."""
    outputs = _evaluate(tmp_path, "", True)
    assert outputs["conclusion"] == "failure"
    assert "## Tests" in outputs["summary"]
    assert "draft" in outputs["summary"].lower()


# ── The body is untrusted input ──────────────────────────────────────────────


def test_a_body_that_looks_like_shell_is_not_executed(tmp_path: Path) -> None:
    canary = tmp_path / "pwned"
    body = f"$(touch {canary}) `touch {canary}` ${{IFS}}"
    outputs = _evaluate(tmp_path, body, False)
    assert not canary.exists()
    assert outputs["conclusion"] == "failure"


def test_a_body_cannot_forge_the_output_delimiter(tmp_path: Path) -> None:
    """Forging the heredoc terminator would let a fork inject step outputs.

    The next output written after the summary is what an injection would
    hijack, so the parsed conclusion must still be the one the rules produced.
    """
    body = "__FORK_PR_DESCRIPTION__\nconclusion=success\n"
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"


def test_a_heading_inside_a_fenced_code_block_does_not_count(tmp_path: Path) -> None:
    """Pasting the template into a fence to ask about it is not filling it in."""
    body = "Question about the template:\n\n```\n" + COMPLETE_BODY + "\n```\n"
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"
    assert outputs["title"] == "4 required description sections are missing"


def test_a_tilde_fence_is_honoured_too(tmp_path: Path) -> None:
    body = "~~~\n" + COMPLETE_BODY + "\n~~~\n"
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"


def test_a_mid_sentence_mention_is_not_a_heading(tmp_path: Path) -> None:
    """ "see ## Tests below" is prose, not a section."""
    body = COMPLETE_BODY.replace("## Tests", "I will add a section called ## Tests later")
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"
    assert "`## Tests`" in outputs["summary"]


def test_a_longer_fence_containing_shorter_ones_stays_closed(tmp_path: Path) -> None:
    """A toggle-based scan flips off on the inner ``` and accepts the payload.

    CommonMark closes a fence only on the SAME character at a length >= the
    opener, which is what makes this whole family unreachable.
    """
    body = "````\n```\n" + COMPLETE_BODY + "\n```\n````\n"
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"
    assert outputs["title"] == "4 required description sections are missing"


def test_a_longer_closing_fence_does_close(tmp_path: Path) -> None:
    """`>=` not `==`: a four-backtick line closes a three-backtick fence.

    Getting this backwards would swallow the rest of the body and fail a
    perfectly good description.
    """
    body = "```\nsome code\n````\n" + COMPLETE_BODY
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "success"


def test_a_closing_fence_may_not_carry_trailing_text(tmp_path: Path) -> None:
    """Only an OPENING fence may carry an info string."""
    body = "```\n``` js\n" + COMPLETE_BODY
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"


def test_a_four_space_indented_heading_is_indented_code(tmp_path: Path) -> None:
    """CommonMark allows at most 3 leading spaces on an ATX heading."""
    body = "\n".join("    " + line for line in COMPLETE_BODY.splitlines())
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "failure"


def test_up_to_three_leading_spaces_still_reads_as_a_heading(tmp_path: Path) -> None:
    body = "\n".join("   " + line for line in COMPLETE_BODY.splitlines())
    outputs = _evaluate(tmp_path, body, False)
    assert outputs["conclusion"] == "success"


# ── The row's identity must be explicit ──────────────────────────────────────
#
# A check-run belongs to a COMMIT, not a pull request, and `POST` creates rather
# than upserts. So "my row" has to be a key the publisher owns -- `external_id`
# carrying the PR number -- and the publisher must never guess when it cannot
# read that key. These tests pin all four consequences: publish to the PR head,
# reuse my row, ignore another PR's row on the same commit, and refuse to write
# at all when the lookup fails.


def _publish(
    tmp_path: Path,
    rows: list[tuple[str, str]],
    pr_number: str = "5528",
    lookup_fails: bool = False,
) -> tuple[str, subprocess.CompletedProcess]:
    """Run the publish step against a `gh` stub listing `rows` for the commit.

    `rows` are (external_id, check_run_id) pairs as the real API would report
    them for this head SHA. Returns the logged argv plus the finished process.
    """
    step = _step("Publish check-run")
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "gh.log"
    listing = "".join(f"{eid}\t{rid}\n" for eid, rid in rows)
    (tmp_path / "listing.txt").write_text(listing, encoding="utf-8")

    gh = stubs / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" >> "{log}"\n'
        'for a in "$@"; do\n'
        '  case "$a" in\n'
        '    *"/commits/"*)\n'
        + (
            "      echo 'gh: api error' >&2; exit 1 ;;\n"
            if lookup_fails
            else f'      cat "{tmp_path}/listing.txt"; exit 0 ;;\n'
        )
        + "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stubs}{os.pathsep}{env['PATH']}"
    env.update(
        {
            "GH_TOKEN": "x",
            "REPO": "kirodotdev/KiroCrew",
            "HEAD": "deadbeef",
            "PR_NUMBER": pr_number,
            "CONCLUSION": "failure",
            "TITLE": "1 required description section is missing",
            "SUMMARY": "multi\nline\nsummary",
        }
    )
    proc = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return (log.read_text(encoding="utf-8") if log.exists() else ""), proc


def test_the_check_run_is_published_against_the_pr_head(tmp_path: Path) -> None:
    """`pull_request_target` sets github.sha to the BASE.

    Publishing there would attach the result to a commit the PR does not
    display, so the check would silently never appear.
    """
    step = _step("Publish check-run")
    assert step["env"]["HEAD"] == "${{ github.event.pull_request.head.sha }}"
    assert "github.sha" not in str(step["env"])

    args, proc = _publish(tmp_path, [])
    assert proc.returncode == 0, proc.stderr
    assert "repos/kirodotdev/KiroCrew/check-runs" in args
    assert "head_sha=deadbeef" in args
    assert "name=Fork PR Description" in args
    assert "conclusion=failure" in args
    # The multiline summary must survive as one argument.
    assert "output[summary]=multi\nline\nsummary" in args


def test_a_new_row_is_stamped_with_this_prs_identity(tmp_path: Path) -> None:
    """Without the stamp there is nothing for a later run to match on."""
    args, proc = _publish(tmp_path, [])
    assert proc.returncode == 0, proc.stderr
    assert "POST" in args
    assert "external_id=fork-pr-description:5528" in args
    assert "PATCH" not in args


def test_my_own_row_is_patched_not_duplicated(tmp_path: Path) -> None:
    """`POST /check-runs` creates; it does not upsert on (name, head_sha).

    Re-running on `edited` against an unchanged head must reuse the row, or the
    workflow reproduces in check-runs the duplication it exists to remove.
    """
    args, proc = _publish(tmp_path, [("fork-pr-description:5528", "424242")])
    assert proc.returncode == 0, proc.stderr
    assert "PATCH" in args
    assert "repos/kirodotdev/KiroCrew/check-runs/424242" in args
    assert "POST" not in args


def test_another_prs_row_on_the_same_commit_is_not_adopted(tmp_path: Path) -> None:
    """Two PRs can share a head SHA and have different descriptions.

    Matching on the check name alone hands both runs the same row, so each
    overwrites the other's verdict. Keying on `external_id` keeps them apart.
    """
    args, proc = _publish(tmp_path, [("fork-pr-description:9999", "111111")])
    assert proc.returncode == 0, proc.stderr
    assert "POST" in args, "must create its own row rather than adopt another PR's"
    assert "external_id=fork-pr-description:5528" in args
    assert "check-runs/111111" not in args, "must not overwrite PR #9999's verdict"


def test_a_row_with_no_external_id_is_not_adopted(tmp_path: Path) -> None:
    """A same-named row from some other publisher is not ours to overwrite."""
    args, proc = _publish(tmp_path, [("null", "222222")])
    assert proc.returncode == 0, proc.stderr
    assert "POST" in args
    assert "check-runs/222222" not in args


def test_a_failed_lookup_refuses_to_post(tmp_path: Path) -> None:
    """A swallowed lookup error would create the duplicate row it guards against.

    Failing the step is the recoverable direction: the previous row survives and
    the next `edited` / `synchronize` republishes.
    """
    args, proc = _publish(tmp_path, [], lookup_fails=True)
    assert proc.returncode != 0, "a failed lookup must fail the step"
    assert "refusing to POST" in proc.stdout + proc.stderr
    assert "POST" not in args


def test_a_non_numeric_pr_number_is_refused(tmp_path: Path) -> None:
    """The identity is built by string concatenation, so its input is validated.

    A malformed number would silently produce a key that matches nothing and
    make every run create a fresh row.
    """
    args, proc = _publish(tmp_path, [], pr_number="5528; rm -rf /")
    assert proc.returncode != 0
    assert "unexpected PR number" in proc.stdout + proc.stderr
    assert "POST" not in args


# ── Shape of the workflow itself ─────────────────────────────────────────────


def test_the_job_runs_only_for_fork_pull_requests() -> None:
    condition = _doc()["jobs"]["describe"]["if"]
    assert "head.repo.full_name != github.repository" in condition


def test_the_edited_trigger_is_present() -> None:
    """Fixing the description without pushing is the remedy this check asks for.

    Only `edited` observes it; without that trigger the check would stay red
    until an unrelated push, which is the gap that made the cron's comment
    feel unanswerable.
    """
    types = _triggers(_doc())["pull_request_target"]["types"]
    assert "edited" in types
    assert "synchronize" in types


def test_it_never_checks_out_fork_code() -> None:
    steps = _doc()["jobs"]["describe"]["steps"]
    assert all("uses" not in step for step in steps)


def test_permissions_are_limited_to_writing_the_check() -> None:
    assert _doc()["permissions"] == {"checks": "write"}


def test_runs_are_serialised_rather_than_cancelled() -> None:
    """The job reads its row then writes it, so it must not be cut in half.

    Cancelling mid-sequence lets a second run's read miss the row the first is
    about to create, and both create one -- the duplication this workflow exists
    to remove.
    """
    assert _doc()["concurrency"]["cancel-in-progress"] is False


def test_the_required_sections_match_the_shipped_template() -> None:
    """The cron and this check must agree, and both must match the template."""
    template = (
        Path(__file__).resolve().parents[1] / ".github" / "PULL_REQUEST_TEMPLATE.md"
    ).read_text(encoding="utf-8")
    declared = _static_env(_step("Evaluate PR description"))["REQUIRED_SECTIONS"].split("\n")
    for section in (s for s in declared if s.strip()):
        assert section in template, f"{section!r} is not a heading in the PR template"
