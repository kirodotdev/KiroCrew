"""Regression tests for the PR quality gates.

These pin the behaviours that are easy to break silently by editing YAML:
the triggers a gate needs to be fixable without a code push, the
added-lines-only scoping that keeps a gate from blaming a PR for
pre-existing code, the advisory-vs-blocking contract of each lane, and --
most importantly -- that `pr-readiness.yml` does not force-pass a failing
Design Review.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class TestScreenshotEvidence:
    """The gate must be satisfiable by editing the PR body alone."""

    def test_reruns_on_body_edit_and_label_change(self):
        # Without `edited`, a contributor who adds the screenshots to the
        # description cannot turn the check green without a no-op push.
        # Without `labeled`, the escape hatch has the same problem.
        wf = _read("screenshot-evidence.yml")
        types_line = next(ln for ln in wf.splitlines() if "types:" in ln)
        for needed in ("edited", "labeled", "unlabeled"):
            assert needed in types_line, f"missing '{needed}' trigger"

    def test_has_escape_hatch_label(self):
        # A gate with no exemption path forces contributors to paste a
        # meaningless screenshot to get green, which defeats the purpose.
        assert "no-screenshots" in _read("screenshot-evidence.yml")

    def test_excludes_non_visual_frontend_paths(self):
        # Tests, type declarations and locale catalogues change constantly
        # with no visual delta; gating on them trains bad habits.
        wf = _read("screenshot-evidence.yml")
        for excluded in (
            ":(exclude)website/src/**/*.test.tsx",
            ":(exclude)website/src/test/**",
            ":(exclude)website/src/**/*.d.ts",
        ):
            assert excluded in wf, f"should exclude {excluded}"

    def test_body_is_only_pattern_matched(self):
        # The PR body is untrusted author input. It must never be eval'd or
        # interpolated into a shell command.
        wf = _read("screenshot-evidence.yml")
        assert 'body="$(gh api' in wf
        assert "eval" not in wf

    def test_has_fork_friendly_body_marker(self):
        # Fork contributors cannot add labels, so the body marker must exist
        # as a self-service waiver alongside the label.
        wf = _read("screenshot-evidence.yml")
        assert "<!-- no-visual-delta -->" in wf
        # The marker is attacker-controlled text: fixed-string match only.
        assert "grep -qF -- '<!-- no-visual-delta -->'" in wf

    def test_marker_requires_justification(self):
        # A bare marker is a silent bypass; the waiver must carry a reviewable
        # claim and fail loudly without one.
        wf = _read("screenshot-evidence.yml")
        assert "why no screenshots?" in wf.lower()
        assert "marker without a justification" in wf

    def test_marker_waiver_warns_instead_of_passing_silently(self):
        # A reviewer scanning the run log must see that evidence was waived.
        wf = _read("screenshot-evidence.yml")
        assert (
            "::warning::'<!-- no-visual-delta -->' marker present" in wf
        ), "waiver must emit a warning annotation naming the marker"

    def test_remediation_sends_evidence_through_gh_attach_not_the_tree(self):
        # Evidence is a GitHub attachment on the description, uploaded with
        # `gh pr create|edit --attach` (or dragged into the web editor); the
        # remediation must name that procedure, with the local-path convention
        # the description uses and the size limits, and must not send an
        # author to commit files or to pin a raw URL to a commit.
        wf = _read("screenshot-evidence.yml")
        assert "gh pr edit $PR --body-file <body.md> --attach" in wf
        # The create-time hint names the subcommand without spelling out the
        # full command: test_workflow_pr_create_handoff.py treats any run block
        # containing that literal as a step that opens pull requests.
        assert "works the same way on the \\`create\\` subcommand" in wf
        assert "gh >= 2.99" in wf
        assert "![alt](./evidence/after.png)" in wf
        assert "![](./evidence/demo.mp4)" in wf
        assert "https://github.com/user-attachments/assets/... URL" in wf
        assert "Dragging the file into the" in wf
        assert "10 MB per image/GIF, 100 MB per video" in wf
        assert "raw/<sha>" not in wf
        assert "Commit the images under" not in wf
        # The accepted-evidence check itself is unchanged: an attachment URL,
        # a markdown image, an HTML tag and a still-linked committed path all
        # satisfy the gate.
        assert (
            "'!\\[[^]]*\\]\\([^)]+\\)|<img[[:space:]]|<video[[:space:]]|temp-screenshots/|user-attachments/'"
            in wf
        )

    def test_body_reaches_grep_via_here_strings_not_pipes(self):
        # Under `set -uo pipefail` a `printf '%s' "$body" | grep -q` pipeline
        # can report 141: grep -q exits on the first match, the printf writer
        # dies of SIGPIPE, and the `if` reads false even though the pattern
        # matched -- real evidence misreported as missing. A here-string has
        # no writer process to kill, so the status is grep's alone.
        wf = yaml.safe_load(_read("screenshot-evidence.yml"))
        steps = wf["jobs"]["screenshot-evidence"]["steps"]
        step = next(
            (s for s in steps if s.get("name") == "Require visual evidence in the PR body"),
            None,
        )
        assert step is not None, "step 'Require visual evidence in the PR body' not found"
        # The rationale comments legitimately name the forbidden form, so only
        # code lines are scanned. The invariant is positive: every grep in the
        # step reads from a here-string and none sits behind a pipe, which a
        # substring test for "| grep" cannot pin (`|grep`, `| /bin/grep`, and
        # `| LC_ALL=C grep` would all slip past it).
        code = [ln for ln in step["run"].splitlines() if not ln.lstrip().startswith("#")]
        grep_lines = [ln for ln in code if re.search(r"\bgrep\b", ln)]
        assert len(grep_lines) == 3, (
            "expected exactly three body checks (evidence, marker, justification), got: "
            f"{grep_lines}"
        )
        # A grep pattern may legitimately contain literal `|` alternation, so
        # the pipe test targets "a pipe feeding grep" (with or without spacing,
        # a path prefix, or interposed env assignments), not any `|` at all.
        piped_grep = re.compile(r"\|\s*(?:[\w./=-]+\s+)*(?:[\w./-]+/)?grep\b")
        for ln in grep_lines:
            assert not piped_grep.search(
                ln
            ), f"the PR body must never reach grep through a pipe: {ln!r}"
            assert re.search(
                r'<<<\s*"\$\{?body\}?"', ln
            ), f"each body check must read from a here-string: {ln!r}"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the evidence step runs under bash on ubuntu-latest",
)
class TestScreenshotEvidenceBodyLogic:
    """Execute the real evidence step against fixture PR bodies.

    Textual pins cannot prove the branch logic; this extracts the actual
    ``run:`` script from the YAML and runs it with ``gh`` stubbed to return a
    fixture body, so the waiver semantics are locked by behavior.
    """

    def _run_step(
        self,
        tmp_path: Path,
        body: str,
        exempt: str = "false",
        gh_status: int = 0,
        fail_first: int = 0,
    ):
        wf = yaml.safe_load(_read("screenshot-evidence.yml"))
        steps = wf["jobs"]["screenshot-evidence"]["steps"]
        step = next(
            (s for s in steps if s.get("name") == "Require visual evidence in the PR body"),
            None,
        )
        assert step is not None, "step 'Require visual evidence in the PR body' not found"
        body_file = tmp_path / "body.txt"
        body_file.write_text(body, encoding="utf-8")
        # `gh api ... --jq '.body // ""'` prints the raw body: stub it with cat.
        # The sentinel proves the stub (not a real gh on PATH) served the call.
        # `cat` is the last command, so a failed read is the stub's own exit
        # status -- which the step now reports as a read failure instead of
        # silently treating the empty body as a description carrying no
        # evidence. That distinction is what keeps a broken harness from
        # reporting itself as the gate's verdict.
        sentinel = tmp_path / "gh-stub-invoked"
        gh = tmp_path / "gh"
        attempts = tmp_path / "gh-attempts"
        stub = f'#!/bin/sh\ntouch "{sentinel}"\nprintf x >> "{attempts}"\n'
        if gh_status:
            # Stand in for an API failure on every attempt (5xx, rate limit).
            stub += f'echo "gh: could not reach the API" >&2\nexit {gh_status}\n'
        elif fail_first:
            # Transient: fail the first N attempts, then serve the body. The
            # attempt tape doubles as the counter.
            stub += (
                f'if [ "$(wc -c < "{attempts}")" -le {fail_first} ]; then\n'
                '  echo "gh: temporarily unavailable" >&2\n'
                "  exit 1\n"
                "fi\n"
                f'cat "{body_file}"\n'
            )
        else:
            stub += f'cat "{body_file}"\n'
        gh.write_text(stub, encoding="utf-8", newline="\n")
        gh.chmod(0o755)
        self._attempts_file = attempts
        summary = tmp_path / "summary.md"
        summary.touch()
        # HERMETIC env, not `**os.environ`. The step's outcome is decided entirely
        # by environment variables, and inheriting the ambient one made that
        # outcome depend on whatever else the process had been doing: `BASH_ENV`
        # would make `bash -c` source a file before the script runs, and a stray
        # `PATH`, `EXEMPT`, `LC_*` or `GH_*` value reaches the same branches the
        # assertions read. Enumerating what the script needs is also self-documenting
        # -- anything absent here is something the step must not depend on.
        env = {
            # tmp_path first so the `gh` stub wins; the system dirs follow because
            # the script needs printf/grep/cat and the stub's shell.
            "PATH": f"{tmp_path}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            # Starve any real gh of credentials so a stub-resolution failure
            # can never turn into a live API call.
            "GH_TOKEN": "",
            "GITHUB_TOKEN": "",
            # The patterns are ASCII and every fixture body is ASCII, so pin the
            # collation rather than inheriting a locale that changes what `grep -i`
            # and the `[[:space:]]` class match.
            "LC_ALL": "C",
            "EXEMPT": exempt,
            "REPO": "example/repo",
            "PR": "1",
            "GITHUB_STEP_SUMMARY": str(summary),
            # A here-string larger than the pipe buffer is backed by a temp
            # file; keep that file under the test's own directory instead of
            # the shared /tmp.
            "TMPDIR": str(tmp_path),
        }
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            # Every write the script performs is at an absolute path, but the
            # child must still not inherit pytest's CWD (the repo root): any
            # future relative write belongs under the test's own directory.
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if exempt != "true":
            # The label path exits before reading the body; every other path
            # must have gone through the stub.
            assert sentinel.exists(), "gh stub was never invoked"
        return result

    def test_marker_with_justification_passes_with_warning(self, tmp_path):
        body = (
            "<!-- no-visual-delta -->\n"
            "**Why no screenshot:** internal string builder change, rendered\n"
            "output is byte-identical.\n"
        )
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout
        assert "<!-- no-visual-delta -->" in result.stdout

    def test_marker_alone_fails_with_explanation(self, tmp_path):
        result = self._run_step(tmp_path, "<!-- no-visual-delta -->\njust trust me\n")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "marker without a justification" in result.stdout

    def test_empty_justification_does_not_waive(self, tmp_path):
        # A justification label with nothing after the colon is still a bare
        # marker: the claim must carry content.
        body = "<!-- no-visual-delta -->\n**Why no screenshot:**\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 1, result.stdout + result.stderr
        # Naming the branch is what makes this test mean anything. Exit 1 alone
        # is also what a body that never arrived produces, so the bare returncode
        # assertion held whether or not the marker was ever seen and rejected.
        assert "marker without a justification" in result.stdout, result.stdout

    def test_emphasis_opening_justification_waives(self, tmp_path):
        # A reason that opens with markdown emphasis is still a reason.
        body = "<!-- no-visual-delta -->\n**Why no screenshot:** *pure rename*, no delta.\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "::warning::" in result.stdout

    def test_image_beats_marker(self, tmp_path):
        # Real evidence satisfies the gate outright: a body carrying both a
        # screenshot and an unjustified marker passes on the screenshot.
        body = "<!-- no-visual-delta -->\n![shot](https://example.test/x.png)\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Visual evidence found" in result.stdout

    def test_no_marker_no_image_still_fails(self, tmp_path):
        result = self._run_step(tmp_path, "A visual change with no evidence.\n")
        assert result.returncode == 1, result.stdout + result.stderr
        # Assert the sentence, not just `::error::`: the step has three error
        # branches (unreadable body, marker without justification, no evidence)
        # and only the last one is the verdict under test.
        assert "carries no screenshot or recording" in result.stdout, result.stdout
        # The remediation the author reads is the attach procedure, with this
        # PR's number already in the command.
        summary = (tmp_path / "summary.md").read_text(encoding="utf-8")
        assert "gh pr edit 1 --body-file <body.md> --attach" in summary, summary
        assert "temp-screenshots/<feature>/" not in summary, summary

    def test_unreadable_body_is_not_reported_as_missing_evidence(self, tmp_path):
        # A failed API read is not an absent screenshot. Discarding both gh's
        # status and its stderr makes a transient failure leave the body empty,
        # so the run tells the author their description carries no evidence,
        # sending them to fix a description that is already
        # correct. It must still fail closed (this gate is required) while
        # naming the read as the cause.
        body = "![shot](https://example.test/x.png)\n"
        result = self._run_step(tmp_path, body, gh_status=1)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Could not read this PR's description" in result.stdout, result.stdout
        assert "carries no screenshot or recording" not in result.stdout, result.stdout
        # Pin the retry budget: a read that never succeeds is attempted three
        # times and then gives up, rather than once or forever.
        assert self._attempts_file.read_bytes() == b"xxx", self._attempts_file.read_bytes()

    def test_transient_read_failure_is_absorbed_by_the_retry(self, tmp_path):
        # The failure class this gate trips on is transient, so a first-attempt
        # 5xx must not cost the author a manual re-run: the retry reads the
        # body on a later attempt and the gate judges the real description.
        body = "![shot](https://example.test/x.png)\n"
        result = self._run_step(tmp_path, body, fail_first=1)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Visual evidence found" in result.stdout, result.stdout
        assert self._attempts_file.read_bytes() == b"xx", self._attempts_file.read_bytes()

    def test_image_in_body_still_passes(self, tmp_path):
        result = self._run_step(tmp_path, "![shot](https://example.test/x.png)\n")
        assert result.returncode == 0, result.stdout + result.stderr

    def test_oversized_body_with_early_evidence_passes(self, tmp_path):
        # Regression pin for the SIGPIPE misreport: with `printf | grep -q`
        # under pipefail, a body larger than the pipe buffer whose evidence
        # sits in the first chunk makes grep exit before the writer finishes,
        # the writer dies of SIGPIPE, and the pipeline reports 141 -- real
        # evidence read as absent. The here-string form must pass this.
        body = "![shot](https://example.test/x.png)\n" + "x" * (1 << 20) + "\n"
        result = self._run_step(tmp_path, body)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Visual evidence found" in result.stdout

    def test_label_waiver_unchanged(self, tmp_path):
        # The marker is an additional path; the label path must keep working.
        result = self._run_step(tmp_path, "no evidence at all", exempt="true")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "'no-screenshots' label present" in result.stdout


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the detection step runs under bash on ubuntu-latest",
)
class TestScreenshotEvidenceSurfaceDetection:
    """Execute the real surface-detection step with ``git`` stubbed.

    This step decides whether the evidence step runs at all, so a wrong
    ``visual=false`` is a silent bypass of a required gate rather than a
    visible failure. The cases below pin which of the two empty results --
    "nothing visual changed" and "the diff could not be computed" -- produced
    the answer.
    """

    def _run_detect(self, tmp_path: Path, git_stdout: str = "", git_status: int = 0):
        wf = yaml.safe_load(_read("screenshot-evidence.yml"))
        steps = wf["jobs"]["screenshot-evidence"]["steps"]
        step = next(
            (s for s in steps if s.get("name") == "Detect user-visible frontend changes"),
            None,
        )
        assert step is not None, "step 'Detect user-visible frontend changes' not found"
        git = tmp_path / "git"
        if git_status:
            body = f'echo "fatal: bad object" >&2\nexit {git_status}\n'
        else:
            body = f'printf %s "{git_stdout}"\n'
        git.write_text(f"#!/bin/sh\n{body}", encoding="utf-8", newline="\n")
        git.chmod(0o755)
        outputs = tmp_path / "outputs.txt"
        outputs.touch()
        env = {
            # tmp_path first so the `git` stub wins the lookup.
            "PATH": f"{tmp_path}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            "LC_ALL": "C",
            "BASE_SHA": "1111111111111111111111111111111111111111",
            "HEAD_SHA": "2222222222222222222222222222222222222222",
            "GITHUB_OUTPUT": str(outputs),
            "TMPDIR": str(tmp_path),
        }
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return result, outputs.read_text(encoding="utf-8")

    def test_visual_path_sets_visual_true(self, tmp_path):
        result, outputs = self._run_detect(tmp_path, git_stdout="website/src/components/Foo.tsx\n")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "visual=true" in outputs, outputs

    def test_no_visual_path_sets_visual_false(self, tmp_path):
        result, outputs = self._run_detect(tmp_path, git_stdout="")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "visual=false" in outputs, outputs

    def test_uncomputable_diff_fails_instead_of_skipping_the_gate(self, tmp_path):
        # The failure this pins: a failed `git diff` swallowed into the same
        # empty string as "nothing visual changed" makes the step write
        # visual=false, the evidence step's `if:` go false, and a REQUIRED
        # check report green having examined nothing. Failing open on a gate
        # is worse than a false red, so the diff failure must surface.
        result, outputs = self._run_detect(tmp_path, git_status=128)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Could not compute the diff" in result.stdout, result.stdout
        assert "visual=false" not in outputs, outputs


class TestCrossPlatform:
    """Findings must be confined to lines the PR actually adds."""

    def test_scans_added_lines_only(self):
        wf = _read("cross-platform.yml")
        assert "grep -E '^\\+'" in wf
        assert "grep -vE '^\\+\\+\\+'" in wf

    def test_filters_prose_before_matching(self):
        # A docstring quoting ``shell=True`` to explain why it is avoided must
        # not fail the gate.
        wf = _read("cross-platform.yml")
        assert "grep -vE '^\\+[[:space:]]*#'" in wf
        assert "grep -vF '``'" in wf

    def test_no_encoding_rule(self):
        # A line regex cannot decide this: nested calls truncate the lookahead
        # and multi-line calls split `encoding=` onto another line. Both give
        # FALSE failures on correct code,
        # so the rule is deliberately absent and its absence is documented.
        wf = _read("cross-platform.yml")
        assert "deliberately NO" in wf, "the absence must stay documented"
        # No rule may actually grep for the encoding kwarg.
        rule_lines = [ln for ln in wf.splitlines() if ln.lstrip().startswith("hits=")]
        assert rule_lines, "expected at least one scan rule"
        for ln in rule_lines:
            assert "encoding" not in ln, f"encoding rule reintroduced: {ln.strip()[:80]}"

    def test_excludes_vendor_and_compat_module(self):
        wf = _read("cross-platform.yml")
        assert ":(exclude)src/kiro_crew/_vendor/**" in wf
        assert ":(exclude)src/kiro_crew/platform_compat.py" in wf

    def test_has_escape_hatch_label(self):
        assert "posix-only-approved" in _read("cross-platform.yml")


class TestPrScope:
    """Scope breadth is advisory: it must never fail the build."""

    def test_never_exits_nonzero(self):
        wf = _read("pr-scope.yml")
        assert "exit 1" not in wf, "PR Scope must stay advisory"

    def test_requires_both_thresholds(self):
        # Breadth alone or size alone is legitimately self-contained; only the
        # combination reviews badly.
        wf = _read("pr-scope.yml")
        assert '-gt "$MAX_AREAS" ] && [' in wf
        assert "MAX_LINES" in wf

    def test_excludes_vendor_and_screenshots(self):
        wf = _read("pr-scope.yml")
        assert ":(exclude)src/kiro_crew/_vendor/**" in wf
        assert ":(exclude)temp-screenshots/**" in wf


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="the measure step runs under bash on ubuntu-latest",
)
class TestPrScopeMeasureLogic:
    """Execute the real scope-measurement step with ``git`` stubbed.

    The step is advisory by contract (it never exits nonzero), which is
    exactly why a swallowed read failure is invisible: a failed ``git diff``
    collapses onto the same empty string as "no files changed", and
    the step reports a verdict -- "No reviewable files changed." -- about a
    diff it never obtained. These cases pin which of the two empty results
    produced the answer, without loosening the advisory contract.
    """

    def _run_measure(
        self,
        tmp_path: Path,
        files_out: str = "",
        numstat_out: str = "",
        git_status: int = 0,
        numstat_status: int = 0,
    ):
        wf = yaml.safe_load(_read("pr-scope.yml"))
        steps = wf["jobs"]["pr-scope"]["steps"]
        step = next((s for s in steps if s.get("name") == "Measure diff breadth"), None)
        assert step is not None, "step 'Measure diff breadth' not found"
        files_file = tmp_path / "files.txt"
        files_file.write_text(files_out, encoding="utf-8")
        numstat_file = tmp_path / "numstat.txt"
        numstat_file.write_text(numstat_out, encoding="utf-8")
        git = tmp_path / "git"
        if git_status:
            body = f'echo "fatal: bad object" >&2\nexit {git_status}\n'
        else:
            # The step reads the same range twice (`--name-only`, then
            # `--numstat`); serve each call the matching fixture.
            numstat_body = (
                f'echo "fatal: bad object" >&2; exit {numstat_status}'
                if numstat_status
                else f'cat "{numstat_file}"'
            )
            body = (
                'case "$*" in\n'
                f"  *--numstat*) {numstat_body} ;;\n"
                f'  *) cat "{files_file}" ;;\n'
                "esac\n"
            )
        git.write_text(f"#!/bin/sh\n{body}", encoding="utf-8", newline="\n")
        git.chmod(0o755)
        summary = tmp_path / "summary.md"
        summary.touch()
        env = {
            # tmp_path first so the `git` stub wins the lookup.
            "PATH": f"{tmp_path}{os.pathsep}/usr/local/bin{os.pathsep}/usr/bin{os.pathsep}/bin",
            "LC_ALL": "C",
            "BASE_SHA": "1111111111111111111111111111111111111111",
            "HEAD_SHA": "2222222222222222222222222222222222222222",
            # Thresholds come from the step's own env stanza so the test
            # exercises the values the workflow actually ships.
            "MAX_AREAS": str(step["env"]["MAX_AREAS"]),
            "MAX_LINES": str(step["env"]["MAX_LINES"]),
            "GITHUB_STEP_SUMMARY": str(summary),
            "TMPDIR": str(tmp_path),
        }
        result = subprocess.run(
            ["bash", "-c", step["run"]],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        return result, summary.read_text(encoding="utf-8")

    def test_measured_diff_reports_scope(self, tmp_path):
        result, summary = self._run_measure(
            tmp_path,
            files_out="src/kiro_crew/session/state.py\n",
            numstat_out="10\t2\tsrc/kiro_crew/session/state.py\n",
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "distinct areas: 1" in result.stdout, result.stdout
        assert "### PR scope" in summary, summary

    def test_empty_diff_is_a_real_verdict(self, tmp_path):
        # A diff that READS successfully and is empty keeps its existing
        # meaning: nothing reviewable changed.
        result, _ = self._run_measure(tmp_path, files_out="", numstat_out="")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "No reviewable files changed." in result.stdout, result.stdout

    def test_uncomputable_diff_refuses_the_verdict_but_stays_advisory(self, tmp_path):
        # The failure this pins: a failed `git diff` swallowed into the same
        # empty string as "no files changed" makes the step claim
        # "No reviewable files changed." having measured nothing. The step must
        # refuse to report any scope claim -- while still exiting 0,
        # because this gate's advisory contract (test_never_exits_nonzero)
        # is deliberate.
        result, summary = self._run_measure(tmp_path, git_status=128)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "read failure" in result.stdout, result.stdout
        assert "::warning::" in result.stdout, result.stdout
        assert "No reviewable files changed." not in result.stdout, result.stdout
        assert "Scope looks self-contained." not in result.stdout, result.stdout
        assert "NOT measured" in summary, summary

    def test_uncomputable_line_count_also_refuses(self, tmp_path):
        # The second read of the same range has the same failure mode, and its
        # old form additionally piped the failure into `awk`, which summed
        # nothing into a legitimate-looking 0.
        result, summary = self._run_measure(
            tmp_path,
            files_out="src/kiro_crew/session/state.py\n",
            numstat_status=128,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "read failure" in result.stdout, result.stdout
        assert "Scope looks self-contained." not in result.stdout, result.stdout
        assert "NOT measured" in summary, summary


class TestDesignReviewBlocks:
    """A BLOCK verdict must reach the required `PR Readiness` status.

    Design, UX and First Principles all gate now: a real BLOCK on any of the
    three fails its own check, which `pr-readiness.yml` folds into the required
    `PR Readiness` status. None may be force-passed into the advisory bucket.
    """

    def test_readiness_blocks_every_opinion_lane(self):
        # The whole point of the promotion: no advisory bucket force-passes UX
        # and First Principles (or Design), so a red opinion lane produces a
        # red PR Readiness.
        wf = _read("pr-readiness.yml")
        assert (
            'passed+=("$label (advisory)")' not in wf
        ), "no opinion lane may be force-passed; a BLOCK must reach readiness"
        assert 'failed+=("$label (BLOCK)")' in wf

    def test_all_three_lanes_share_the_one_blocking_branch(self):
        # Both readers -- the fork check-run reader and the same-repo
        # workflow-run reader -- must route all three lanes through the
        # BLOCK-only failing branch, so the wiring cannot drift for one lane.
        wf = _read("pr-readiness.yml")
        branch = (
            '[ "$label" = "Design Review" ] || [ "$label" = "UX Review" ] '
            '|| [ "$label" = "First Principles Review" ]'
        )
        assert wf.count(branch) == 2, "both readiness readers must block all three lanes"

    @pytest.mark.parametrize("name", ["design-review.yml", "fork-design-review.yml"])
    def test_prompt_no_longer_claims_block_is_advisory(self, name):
        # The prompt must not tell the model "BLOCK does NOT block the merge",
        # which would teach it to under-use the verdict that actually gates.
        wf = _read(name)
        assert "does NOT block the merge" not in wf
        assert "BLOCK (advisory)" not in wf
        assert "blocks PR readiness" in wf

    @pytest.mark.parametrize("name", ["design-review.yml", "fork-design-review.yml"])
    def test_falsification_step_and_block_budget(self, name):
        # Raising the stakes of BLOCK requires a matching precision bar.
        wf = _read(name)
        assert "FALSIFY BEFORE YOU BLOCK" in wf
        assert "at most 1 BLOCK per" in wf

    def test_same_repo_gate_fails_only_on_block(self):
        # The readiness wiring is only safe because every non-BLOCK outcome --
        # including an errored or throttled run -- exits 0.
        wf = _read("design-review.yml")
        gate = wf.split("Design review status (gates on BLOCK)")[1]
        assert "PASS|CONCERNS)" in gate
        assert "exit 1 ;;" in gate
        # The wildcard (errored / no verdict) branch must not fail.
        tail = gate.split("BLOCK)")[1]
        assert "exit 0" in tail, "an incomplete review must never block"


REVIEW_PROMPTS = Path(__file__).resolve().parents[1] / ".github" / "review-prompts"

UX_LANES = ["ux-review.yml", "fork-ux-review.yml"]
DESIGN_LANES = ["design-review.yml", "fork-design-review.yml"]
FP_CONTRACT = "first-principles.md"


def _read_prompt(name: str) -> str:
    return (REVIEW_PROMPTS / name).read_text(encoding="utf-8")


def _flat(text: str) -> str:
    """Collapse whitespace so an assertion survives prompt re-wrapping."""
    return " ".join(text.split())


class TestAdvisoryLanesStateTheirRealAuthority:
    """A lane told its verdict is inert calibrates every borderline case down.

    Each of these prompts once opened by disclaiming authority the workflow
    does in fact grant. A reviewer that believes a BLOCK changes nothing has no
    reason to spend one, so decidable defects settle on CONCERNS -- the tier
    nothing gates on. Design, UX and First Principles now ALL fail `PR
    Readiness` on a BLOCK, so every one of these prompts must state that
    authority plainly rather than disclaim it.
    """

    @pytest.mark.parametrize("name", UX_LANES + DESIGN_LANES)
    def test_workflow_prompt_does_not_disclaim_its_own_authority(self, name):
        wf = _flat(_read(name))
        assert "Nothing you emit blocks the merge" not in wf
        assert "do not gate" not in wf
        assert "does NOT block the merge" not in wf

    def test_first_principles_contract_does_not_disclaim_its_authority(self):
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "Nothing you emit blocks the merge" not in contract
        assert "do not gate" not in contract
        assert "does NOT block the merge" not in contract

    @pytest.mark.parametrize("name", DESIGN_LANES)
    def test_design_prompt_says_a_block_reaches_readiness(self, name):
        assert "blocks PR readiness" in _flat(_read(name))

    @pytest.mark.parametrize("name", UX_LANES)
    def test_ux_prompt_says_a_block_reaches_readiness(self, name):
        # UX was promoted: the prompt must state the real authority a BLOCK now
        # carries, and must not keep the old "does not gate" disclaimer that
        # calibrated borderline calls down to CONCERNS.
        wf = _flat(_read(name))
        assert "blocks PR readiness" in wf
        assert "does not by itself gate PR readiness" not in wf

    def test_first_principles_says_a_block_reaches_readiness(self):
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "blocks PR readiness" in contract
        assert "does not by itself gate PR readiness" not in contract

    def test_first_principles_routes_a_block_grade_subtraction_to_blockers(self):
        # The observed failure: a conclusion meeting this lane's own strongest
        # BLOCK criterion ("an item's zero option costs nobody anything") was
        # written into `### Subtractions`, which carries no verdict, so the
        # verdict stayed CONCERNS and nothing was required to act on it.
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "belongs under Blockers" in contract


class TestDecidableFindingsExitTheTieBreaker:
    """`prefer CONCERNS` is a one-way ratchet until something exits it.

    Preferring the lower tier is right for a matter of taste and wrong for a
    fact read off the diff. With no exception, a mechanically decidable defect
    lands on the advisory tier exactly like a preference does, and the two
    become indistinguishable to whoever reads the verdict.
    """

    @pytest.mark.parametrize("name", UX_LANES)
    def test_ux_tie_breaker_carries_a_closed_exception_list(self, name):
        wf = _flat(_read(name))
        assert "Tie-breaker: when torn between BLOCK and CONCERNS" in wf
        # Five decidable exits: an evidence gap (a control no supplied
        # screenshot shows -- the lane cannot evaluate it, so CONCERNS would
        # misreport an unreached verdict as a mild one), the two notice rules,
        # a primary control the blind reader could not use and a hard element
        # swap. Each is read off the screenshot list, the blind-read report or
        # the diff, not judged.
        assert "The tie-breaker does NOT apply to the five below" in wf
        assert "An evidence gap (lens 12 or 13)" in wf
        assert "cannot evaluate: missing" in wf
        assert "hedges about state the code already holds" in wf
        assert "assert what happened" in wf
        assert "A primary control (lens 12) the blind reader misread" in wf
        assert "A hard swap (lens 13)" in wf

    @pytest.mark.parametrize("name", UX_LANES + DESIGN_LANES)
    def test_every_mandated_block_carries_a_falsification_step(self, name):
        # The design lanes established the precedent that raising the stakes of
        # BLOCK requires a matching precision bar. An exception list that
        # MANDATES a BLOCK raises them for that path, so it owes the same step:
        # a rule admitted for being readable off the diff has to be read off the
        # diff, or it becomes a licence to spend the verdict on a resemblance.
        assert "FALSIFY BEFORE YOU BLOCK" in _flat(_read(name))

    def test_first_principles_exception_carries_a_falsification_step(self):
        assert "FALSIFY BEFORE YOU BLOCK" in _flat(_read_prompt(FP_CONTRACT))

    def test_first_principles_tie_breaker_exempts_the_rider_combination(self):
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "Tie-breaker: when torn between BLOCK and CONCERNS" in contract
        # Three combinations are settled by reading, not by degree: (a) an
        # unverified premise on a core availability path, (b) the rider, (c) a
        # product-shape change with no non-draft RFC on the base and no
        # maintainer override -- which is also the lane's only "cannot
        # evaluate": the recorded decision is the one piece of evidence it
        # requires and cannot produce.
        assert "Three combinations are settled by reading the diff" in contract
        assert "PRODUCT-SHAPE CHANGE WITHOUT A RECORDED DECISION" in contract
        assert "This is the one `cannot evaluate` this lane has" in contract
        assert "CANNOT EVALUATE -- REQUIRED EVIDENCE ABSENT" not in contract
        assert "product-shape change without accepted RFC" in contract
        assert "UNVERIFIED PREMISE ON A CORE AVAILABILITY PATH" in contract
        assert "an item is riding along" in contract
        assert "When all four hold the defect is already" in contract

    def test_first_principles_lower_the_concern_names_the_exception(self):
        # `When unsure, LOWER the concern` sits far from the tie-breaker and
        # would otherwise re-impose the ratchet the exception just lifted.
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "When unsure, LOWER the concern" in contract
        assert "The three exceptions are named at the" in contract
        assert "there is no fourth" in contract


FP_LANES = ["first-principles-review.yml", "fork-first-principles-review.yml"]


class TestMissingEvidenceIsABlockNotAConcern:
    """A lane that cannot evaluate must say so with the verdict that has teeth.

    A UI change with no admissible screenshot leaves the UX blind read
    unperformed; a CONCERNS on it scores green in pr-readiness. A verdict the
    lane could not reach must not read as "looked and found little".
    """

    @pytest.mark.parametrize("name", UX_LANES)
    def test_ux_lane_blocks_on_an_evidence_gap(self, name):
        wf = _flat(_read(name))
        assert "cannot evaluate: missing" in wf
        assert "the verdict cannot be PASS" not in wf
        assert "or the evidence is incomplete" not in wf
        # A recording gap is a gap like any other, and a description image
        # the evidence step did not admit does not close one.
        assert "a BLOCK like every gap" in wf
        assert "the evidence step did not admit" in wf

    def test_fork_ux_lane_keeps_its_own_limitation_out_of_the_block(self):
        # The fork lane has no blind reader. That is the LANE's limitation, not
        # an evidence gap the author can close, so it must cap at CONCERNS
        # rather than block a fork contributor for something they cannot fix.
        wf = _flat(_read("fork-ux-review.yml"))
        assert "this lane's limitation, not the author's gap" in wf
        assert "The absent blind read is not this exit" in wf

    @pytest.mark.parametrize("name", DESIGN_LANES)
    def test_design_lane_blocks_when_it_has_not_seen_the_surface(self, name):
        wf = _flat(_read(name))
        assert "CANNOT EVALUATE -- REQUIRED EVIDENCE MISSING" in wf
        assert "cannot evaluate: missing <screenshot or recording of X>" in wf
        # Evidence must be of THIS revision: an image hosted off a commit
        # outside the PR, or one that depicts another PR, is not evidence.
        assert "hosted off a commit outside this PR" in wf

    @pytest.mark.parametrize("name", DESIGN_LANES)
    def test_design_lane_reads_evidence_presence_off_a_fetched_list_not_the_description(self, name):
        """The Design reviewer has no shell to fetch with, so a bare URL in the
        description would count as evidence whether or not it renders -- a
        fabricated or dead user-attachments URL would bypass the trigger. Both
        lanes therefore run the same allowlisted fetch the UX lanes source and
        hand the reviewer one evidence file; the prompt names that file, not
        the description, as the predicate."""
        raw = _read(name)
        wf = _flat(raw)
        assert "- name: Collect rendered evidence" in raw
        assert "pr-attachment-evidence.sh" in raw
        assert "EVIDENCE: ${{ runner.temp }}/design-evidence.txt" in raw
        assert "Read ${{ runner.temp }}/design-evidence.txt" in wf
        assert "the evidence list the workflow wrote (RENDERED EVIDENCE above)" in wf
        assert "a URL that did not download is not evidence" in wf
        assert "is read off the evidence list and the diff, not judged" in wf
        assert "the evidence list naming no downloaded attachment" in wf
        # The old predicate -- presence read off the description's text -- is gone.
        assert "no screenshot or recording attached to its description" not in wf
        assert "is read off the description and the diff" not in wf
        # A transport failure is presence unconfirmed, capped at CONCERNS, never
        # a BLOCK: the UX lane fails its run on the same failure.
        assert "presence is unconfirmed, not absent" in wf
        # The sourced script is PR-controlled on a same-repo pull request, so
        # the step runs before any Bedrock credential exists in the job.
        steps = yaml.safe_load(raw)["jobs"][name[: -len(".yml")]]["steps"]
        evidence_at = next(
            i for i, s in enumerate(steps) if s.get("name") == "Collect rendered evidence"
        )
        creds_at = next(
            i for i, s in enumerate(steps) if "configure-aws-credentials" in s.get("uses", "")
        )
        review_at = next(
            i for i, s in enumerate(steps) if s.get("name") == "Design review (Fable 5)"
        )
        assert evidence_at < creds_at < review_at
        if name == "fork-design-review.yml":
            # The fork lane fetches from the trusted base checkout and must be
            # allowed to reach the asset host user-attachments redirects to.
            assert '"$GITHUB_WORKSPACE/.github/scripts/pr-attachment-evidence.sh"' in raw
            # Whole allowlist tokens, so a host that merely contains the name
            # as a substring cannot satisfy the check.
            harden = next(
                s
                for s in yaml.safe_load(raw)["jobs"]["fork-design-review"]["steps"]
                if s.get("name") == "Harden runner (egress allowlist)"
            )
            endpoints = set(harden["with"]["allowed-endpoints"].split())
            assert {
                "github.com:443",
                "github-production-user-asset-6210df.s3.amazonaws.com:443",
            } <= endpoints, sorted(endpoints)
        else:
            # The same-repo lane also lists committed images present at HEAD.
            assert 'git cat-file -e "HEAD:$path"' in raw
            assert "committed images this revision adds or changes: $k" in raw

    def test_first_principles_reads_rfc_status_from_the_base_commit(self):
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "PRODUCT SHAPE NEEDS A RECORDED DECISION" in contract
        # A PR that flips `status:` or ships the RFC beside the change has
        # proposed a decision, not recorded one.
        assert "never from the checkout or the diff" in contract
        assert "/ai-review override first-principles <head sha>" in contract
        for name in FP_LANES:
            wf = _read(name)
            # The list is produced by the same step, from the same base sha, as
            # the contract -- the PR cannot edit either.
            assert "RFC_STATUS: ${{ runner.temp }}/rfc-status.txt" in wf
            assert "git grep -E '^status:[[:space:]]*[A-Za-z-]+' \"$BASE_SHA\"" in wf
            assert "':(exclude)docs/request-for-change/README.md'" in wf
            assert "rfc-status.txt" in _flat(wf)

    def test_first_principles_product_shape_gate_is_not_a_request_for_a_document(self):
        # The lane may not ask for an RFC to be written; lens 9 reports that a
        # record is absent and names the two ways it gets made. Both sentences
        # must coexist or the gate contradicts the anti-noise bar.
        contract = _flat(_read_prompt(FP_CONTRACT))
        assert "Do NOT ask for a written artifact" in contract
        assert "Lens 9 is not a way around this" in contract
