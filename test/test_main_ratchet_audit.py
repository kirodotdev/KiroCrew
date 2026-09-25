"""The Main Ratchet Audit must measure every gate, and describe only main.

The audit's concurrency group is keyed on the SHA, so audits for DIFFERENT
commits run concurrently and finish in whatever order their runners take. The
per-commit verdict is exactly what that buys, and it stays sound. The single
tracking issue is not per-commit though -- it is shared mutable state -- so a
run that finishes after a newer push must not write its verdict there:

* A slow **green** run would close the drift record a newer push just opened,
  turning a live ratchet failure on main into no record at all. That is the same
  false all-clear the whole workflow exists to close, arriving through the
  reporter instead of through an empty diff scope.
* A slow **drifting** run would open or refresh a record against a tree that has
  since been fixed, so main reads as dirty when it is clean.

Two further ways the lane could report a verdict that means less than it looks
are pinned at the bottom of this module: a gate ADDED to ``ci.yml`` or to
``fast-gate.yml`` (the split-out cheap blocking gates ci.yml waits on) and not
mirrored here would never be measured on the integrated tree, and a gate step
left on the default ``success()`` condition would skip every later gate as soon
as one drifted.

These are behavioural properties of a shell step, not of its prose, so they are
pinned by running the step itself against a stubbed ``gh``. The alternative --
grepping the workflow for a comparison -- passes on a step that compares the two
SHAs and then closes the issue anyway.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "main-ratchet-audit.yml"

_CURRENT = "a" * 40
_NEWER = "b" * 40

# `gh issue list --json number,body` shape: one open issue this workflow owns,
# recognised by the same hidden marker the step filters on.
_MARKER = "<!-- main-ratchet-audit-tracking-issue -->"
_OPEN_ONE = json.dumps([{"number": 4242, "body": f"{_MARKER}\ndrift"}])

_STUB_GH = """#!/usr/bin/env bash
# Records every invocation on ONE line -- an issue body is multi-line, and a
# raw dump would split one call across several log entries -- then answers the
# reads the step performs.
printf '%s\\n' "${*//$'\\n'/<NL>}" >> "$GH_LOG"
case "$1" in
  api)
    if [ "$STUB_HEAD_SHA" = "UNREADABLE" ]; then
      echo "stub: head unreadable" >&2
      exit 1
    fi
    printf '%s\\n' "$STUB_HEAD_SHA"
    ;;
  label) ;;
  issue)
    case "$2" in
      list) printf '%s' "$STUB_OPEN_JSON" ;;
      create) printf 'https://github.example/o/r/issues/4243\\n' ;;
    esac
    ;;
esac
exit 0
"""

# A stand-in for `C:\Windows\System32\bash.exe` with no WSL distribution
# installed: an ASCII sentinel on stdout so the harness's own diagnostic can be
# checked, and the launcher's real UTF-16LE complaint on stderr.
_SHELL_THAT_RUNS_NOTHING = (
    "import sys\n"
    "sys.stdout.write('shell-refused-to-run')\n"
    "sys.stderr.buffer.write("
    "'Windows Subsystem for Linux has no installed distributions.'.encode('utf-16-le'))\n"
    "sys.exit(1)\n"
)


def _posix_shell() -> str | None:
    """An ABSOLUTE path to a shell that can run the reporter step, or None.

    Absolute, never the bare name: on Windows ``CreateProcess`` searches
    ``C:\\Windows\\System32`` BEFORE PATH, so a bare ``bash`` resolves to the WSL
    launcher living there even when the PATH entry a probe approved is Git Bash.
    That launcher answers a UTF-16LE "Windows Subsystem for Linux has no
    installed distributions" on stderr and exits 1, so the stub's call log stays
    empty -- and an empty log is exactly what some assertions below look for, so
    one wrong binary scores part of this module green and reds the rest with a
    message about itself.

    None on Windows outright, which is why resolving is not enough on its own.
    The step under test is a ``runs-on: ubuntu-latest`` shell block, and running
    it needs the extensionless ``#!``-shebang ``gh`` stub reachable on PATH --
    an execute bit ``os.chmod`` cannot grant there. Every sibling module that
    executes a workflow's shell against a stubbed ``gh`` carries the same
    precondition.
    """
    if os.name == "nt":
        return None
    return shutil.which("bash")


_BASH = _posix_shell()

# Applied per class rather than module-wide: the workflow-parity classes at the
# bottom only read YAML, so they stay measured on every platform.
_needs_posix_shell = pytest.mark.skipif(
    _BASH is None or shutil.which("jq") is None,
    reason="the reporter step is shell and needs a POSIX bash + jq, as its runner has",
)


def _report_script() -> str:
    """The reporter step's own shell, read out of the workflow."""
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["report"]["steps"]
    runs = [step["run"] for step in steps if "run" in step]
    assert len(runs) == 1, f"expected one run block in the report job, got {len(runs)}"
    return runs[0]


def _run(
    tmp_path: Path,
    *,
    head_sha: str,
    ratchet: str,
    frontend: str = "success",
    bundle: str = "success",
    open_json: str = _OPEN_ONE,
    shell: list[str] | None = None,
) -> tuple[int, str, list[str]]:
    """Execute the reporter step with a stubbed ``gh``; return rc, output, calls.

    ``shell`` replaces the interpreter argv, so a test can hand the harness a
    shell that runs nothing and assert it says so.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gh"
    stub.write_text(_STUB_GH, encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "gh.log"
    log.write_text("", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "GH_LOG": str(log),
            "STUB_HEAD_SHA": head_sha,
            "STUB_OPEN_JSON": open_json,
            "GH_TOKEN": "stub",
            "REPO": "o/r",
            "SERVER_URL": "https://github.example",
            "RUN_ID": "1",
            "COMMIT_SHA": _CURRENT,
            "RATCHET_RESULT": ratchet,
            "FRONTEND_RESULT": frontend,
            # Every lane the reporter reads must be supplied: the step runs under
            # `set -u`, so one missing lane aborts it before its first call and
            # the empty call log reads as a reporter that chose to do nothing.
            "BUNDLE_RESULT": bundle,
        }
    )
    if shell is None:
        assert _BASH is not None, "the POSIX-shell precondition mark did not fire"
        shell = [_BASH, "-s"]

    proc = subprocess.run(
        shell,
        input=_report_script(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        # A shell that is not the one asked for answers in its own encoding --
        # the WSL launcher uses UTF-16LE -- so decode defensively: a raised
        # UnicodeDecodeError here would destroy the diagnosis below rather than
        # report it.
        errors="replace",
        cwd=tmp_path,
        env=env,
    )
    calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
    output = proc.stdout + proc.stderr
    # The step's first action is always `gh label create`, so an EMPTY call log
    # means the shell never ran the step -- not a reporter that chose to do
    # nothing. Some assertions below are SATISFIED by an empty log, so this is
    # what keeps an unusable shell from being scored as a verdict.
    assert calls, f"the shell ran no part of the reporter step (rc={proc.returncode}): {output!r}"
    return proc.returncode, output, calls


def _closed_issues(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("issue close")]


def _commented_issues(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("issue comment")]


class TestABrokenShellIsNotAQuietReporter:
    """An empty call log means the shell never ran, and is never a verdict.

    Some assertions in this module are satisfied by an empty log, so a shell that
    cannot run the step at all reads as "the reporter correctly did nothing" for
    those while it reds the rest with a complaint about itself -- a split verdict
    on the shell rather than on the step. A resolution that yields the wrong
    binary (a bare ``bash`` on Windows lands on the WSL launcher) is therefore
    only half the guard: the harness must also refuse the observation rather
    than score it.
    """

    def test_a_shell_that_runs_nothing_fails_the_harness(self, tmp_path: Path) -> None:
        # Not gated on the host: the shell is supplied here rather than
        # resolved, so the guard is measured on every platform, including the
        # one whose shim it describes.
        with pytest.raises(AssertionError, match="ran no part of the reporter step") as excinfo:
            _run(
                tmp_path,
                head_sha=_CURRENT,
                ratchet="success",
                shell=[sys.executable, "-c", _SHELL_THAT_RUNS_NOTHING],
            )

        # The shell's own output must reach the message, or the next reader gets
        # "no calls" with nothing to say which binary answered.
        assert "shell-refused-to-run" in str(excinfo.value)


@_needs_posix_shell
class TestSupersededReporter:
    """A reporter whose commit is not main's head leaves the issue alone."""

    def test_a_green_run_at_main_head_closes_the_tracking_issue(self, tmp_path: Path) -> None:
        # The baseline the guard must not break: the self-heal still fires when
        # this commit really is the tree the green verdict describes.
        rc, out, calls = _run(tmp_path, head_sha=_CURRENT, ratchet="success")

        assert rc == 0, out
        assert _closed_issues(calls) == ["issue close 4242 --repo o/r --reason completed"], out

    def test_a_superseded_green_run_does_not_close_newer_drift(self, tmp_path: Path) -> None:
        # The finding itself: a slow green audit for an older commit must not
        # erase the drift record a newer push opened. Losing it means a live
        # ratchet failure on main has no durable artifact at all.
        rc, out, calls = _run(tmp_path, head_sha=_NEWER, ratchet="success")

        assert rc == 0, out
        assert _closed_issues(calls) == [], out
        assert _commented_issues(calls) == [], out

    def test_a_superseded_drifting_run_does_not_touch_the_issue(self, tmp_path: Path) -> None:
        # The other direction of the same class: a stale drift verdict must not
        # refresh a record against a tree that has since been fixed. The run
        # still fails, so the verdict stays visible on its own commit.
        rc, out, calls = _run(tmp_path, head_sha=_NEWER, ratchet="failure")

        assert rc == 1, out
        assert _closed_issues(calls) == [], out
        assert _commented_issues(calls) == [], out
        assert [c for c in calls if c.startswith("issue create")] == [], out

    def test_a_drifting_run_at_main_head_still_refreshes_the_issue(self, tmp_path: Path) -> None:
        # The baseline for the drift branch: the guard must not have disarmed
        # the lane's actual job of recording drift on the current head.
        rc, out, calls = _run(tmp_path, head_sha=_CURRENT, ratchet="failure")

        assert rc == 1, out
        assert len(_commented_issues(calls)) == 1, out
        assert "Still drifting" in _commented_issues(calls)[0], out


@_needs_posix_shell
class TestUnreadableHead:
    """An unknown head resolves toward keeping drift VISIBLE, in both branches."""

    def test_an_unreadable_head_still_records_drift(self, tmp_path: Path) -> None:
        # Failing the other way would let one transient API error swallow a
        # drift record entirely -- a likelier loss than the race being guarded.
        rc, out, calls = _run(tmp_path, head_sha="UNREADABLE", ratchet="failure")

        assert rc == 1, out
        assert len(_commented_issues(calls)) == 1, out

    def test_an_unreadable_head_declines_to_close_on_green(self, tmp_path: Path) -> None:
        # The opposite default, for the opposite risk: an unconfirmed head must
        # not authorise closing a record, because that loss is not recoverable
        # by the next run, whereas a record left open closes on the next green
        # push. The two branches disagree deliberately.
        rc, out, calls = _run(tmp_path, head_sha="UNREADABLE", ratchet="success")

        assert rc == 0, out
        assert _closed_issues(calls) == [], out


@_needs_posix_shell
class TestConvergenceStaysExempt:
    """Duplicate reconciliation is ordering-independent, so it is not guarded."""

    def test_a_superseded_run_still_collapses_a_split_record(self, tmp_path: Path) -> None:
        # Closing a redundant copy while keeping the lowest-numbered one can
        # never remove the last record, so a superseded run may still self-heal
        # the split that the per-SHA concurrency group makes possible.
        two_open = json.dumps(
            [
                {"number": 4242, "body": f"{_MARKER}\ndrift"},
                {"number": 4250, "body": f"{_MARKER}\ndrift"},
            ]
        )
        rc, out, calls = _run(tmp_path, head_sha=_NEWER, ratchet="success", open_json=two_open)

        assert rc == 0, out
        closed = _closed_issues(calls)
        assert closed == ["issue close 4250 --repo o/r --reason not planned"], out


@_needs_posix_shell
class TestIssueBodyRendering:
    """The durable artifact is the most-read output, so its Markdown must render."""

    def test_the_created_body_carries_no_literal_backslashes(self, tmp_path: Path) -> None:
        # The body is built by a single-quoted printf, where a backslash-escaped
        # backtick is NOT unescaped by bash and reaches the issue verbatim.
        rc, out, calls = _run(tmp_path, head_sha=_CURRENT, ratchet="failure", open_json="[]")

        assert rc == 1, out
        created = [c for c in calls if c.startswith("issue create")]
        assert len(created) == 1, out
        assert "\\`" not in created[0], created[0]
        assert f"tree at `{_CURRENT}`" in created[0], created[0]


class TestGateParityWithCi:
    """A ``scripts/check_*.py`` gate CI blocks on is measured here or accounted for.

    "CI" is two workflow files since the Fast Gate split: ci.yml keeps the heavy
    matrix, and fast-gate.yml holds the eleven cheap blocking gates that ci.yml's
    ``await-fast-gate`` job now waits on. Both are read below. Reading only
    ci.yml would have left eight of the enumerated gates outside the scan the
    moment they moved -- the subset assertion would still pass, while the
    accounting sets it checks against silently stopped describing anything.

    Scoped to script-invoking gates on purpose. The lane's two pytest-side
    ratchets (the redactor census and the config baseline) are an ORIGINAL
    selection, not a transcription: ci.yml runs them only as part of the sharded
    full suite and enumerates no "ratchet test" set anywhere, so there is no
    counterpart to diff against. Pinning them by a source heuristic instead was
    measured and rejected -- scanning `test/` for a module-level baseline
    constant matches five modules for the two that are ratchets, so the pin would
    need a hand-maintained exclusion list: the same bookkeeping with an extra
    mechanism and more confidence than it earns. A third pytest-side ratchet
    should bring an explicit registry with it.
    """

    # A gate script cannot merely be absent from this lane; it must be absent for
    # a RECORDED reason. Scoping the pin to ci.yml's `backend-lint` would have
    # missed the repo's dominant shape -- one gate per standalone job -- so the
    # whole of ci.yml AND of fast-gate.yml is read and every gate lands in
    # exactly one of the three sets below. That is what makes a gate added tomorrow a red test rather than
    # a silent omission.

    # No whole-tree question exists to ask on main: each of these judges a CHANGE
    # against a base ref (`*_BASE_REF`, the lines a PR adds), diffs the base
    # branch's own file, or consumes a coverage artifact this lane never produces.
    _PR_ONLY_BY_CONSTRUCTION: frozenset[str] = frozenset(
        {
            "check_brand_name.py",
            "check_comment_history.py",
            "check_focus_cue.py",
            "check_harness_parity.py",
            # Only added lines are enforced; the whole-tree backlog is a report.
            "check_memory_store_seam.py",
            "check_changelog_history.py",
            # Compares the base ref's ledger entries with the head's, so main's
            # own tree has nothing to be judged against.
            "check_decisions_history.py",
            "check_per_file_coverage.py",
        }
    )

    # Cheap whole-tree content assertions that DO share the evicted-verdict cause
    # this lane exists to fix, and are outside its declared ratchet / ceiling /
    # baseline scope rather than outside the problem. Named here so the boundary
    # is a reviewed decision instead of an invisible gap; moving one into
    # `ratchet-gates` is a step plus a deletion from this set.
    _DEFERRED_WHOLE_TREE_GATES: frozenset[str] = frozenset(
        {
            "check_feature_map.py",
            "check_builtin_skill_scope.py",
            "check_loop_bound_locks.py",
            "check_testpaths_coverage.py",
        }
    )

    @staticmethod
    def _gate_scripts(job: dict) -> set[str]:
        found = set()
        for step in job.get("steps") or []:
            found.update(re.findall(r"scripts/(check_[A-Za-z0-9_]+\.py)", step.get("run") or ""))
        return found

    def test_every_ci_gate_is_mirrored_or_recorded(self) -> None:
        # The silent direction: a renamed script errors its step loudly, but a
        # gate ADDED to CI and not mirrored here just never runs on main --
        # and its drift then surfaces on an unrelated PR -- the failure mode
        # reproduced for every future gate. Both blocking workflows
        # count: fast-gate.yml is where the cheap gates live, and ci.yml blocks
        # on it through `await-fast-gate`, so a gate added to either is a gate CI
        # enforces.
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

        in_ci: set[str] = set()
        for name in ("ci.yml", "fast-gate.yml"):
            workflow = yaml.safe_load(
                (_REPO_ROOT / ".github" / "workflows" / name).read_text("utf-8")
            )
            for job in workflow["jobs"].values():
                in_ci |= self._gate_scripts(job)
        # The scan must actually see the moved gates: if a rename or another split
        # empties it, the subset assertion below passes by measuring nothing.
        assert self._DEFERRED_WHOLE_TREE_GATES <= in_ci, (
            "gate script(s) recorded as deferred are no longer run by ci.yml or "
            f"fast-gate.yml: {sorted(self._DEFERRED_WHOLE_TREE_GATES - in_ci)}. Either they "
            "moved to a third workflow this scan must read, or the record is stale."
        )
        measured = self._gate_scripts(audit["jobs"]["ratchet-gates"])
        classified = measured | self._PR_ONLY_BY_CONSTRUCTION | self._DEFERRED_WHOLE_TREE_GATES

        assert in_ci <= classified, (
            "CI runs gate script(s) this lane neither measures nor accounts for: "
            f"{sorted(in_ci - classified)}. Mirror the step into ratchet-gates, or record "
            "the reason in _PR_ONLY_BY_CONSTRUCTION (it judges a diff, not a tree) or "
            "_DEFERRED_WHOLE_TREE_GATES. An unclassified gate is never measured on main's "
            "integrated tree and its drift surfaces on an unrelated PR."
        )

    def test_the_audit_mirrors_the_whole_backend_lint_ratchet_set(self) -> None:
        # The set this lane claims outright, kept separate from the accounting
        # above: an omission recorded in either set must never quietly cover a
        # backend-lint ratchet, which is the set the workflow says it mirrors.
        ci = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

        expected = self._gate_scripts(ci["jobs"]["backend-lint"])
        actual = self._gate_scripts(audit["jobs"]["ratchet-gates"])

        assert expected <= actual, (
            "ci.yml backend-lint runs ratchet gate(s) the Main Ratchet Audit does not: "
            f"{sorted(expected - actual)}. This is the set the audit mirrors outright, so "
            "it must be measured, not excused."
        )


class TestFrontendCeilingParityWithCi:
    """The frontend ceilings are ``.mjs``, and a ``check_*.py`` scan cannot see them.

    Every pin above matches ``scripts/check_<name>.py``: a Python filename, with
    an underscore. The frontend ceilings are ``website/scripts/check-<name>.mjs``
    -- a different extension and a hyphen -- so they fall outside that pattern
    entirely. A ceiling can therefore be blocking on every pull request, share the
    evicted-verdict cause this workflow exists to close, and still be absent here
    while the Python parity pins stay green, because nothing they scan for is
    spelled that way.

    That is the gap this class closes, for the same reason the Python pins exist:
    a gate CI blocks on that this lane does not mirror is never measured on main's
    integrated tree, and its drift surfaces on an unrelated pull request instead,
    where the cheapest way out is to raise the ceiling.

    Scoped to ci.yml's ``bundle-size`` job against this lane's ``bundle-ceiling``
    job, which is the pair the workflow says it mirrors outright -- the same shape
    as the ``backend-lint`` -> ``ratchet-gates`` pin. The wider frontend gate
    surface (the ``npm run lint:*`` lanes) is deliberately NOT claimed here: those
    run through package.json script aliases rather than a direct ``node
    scripts/...`` invocation, so pinning them needs a different scan and a
    reviewed per-gate decision, not this regex.
    """

    @staticmethod
    def _mjs_gate_scripts(job: dict) -> set[str]:
        found = set()
        for step in job.get("steps") or []:
            found.update(re.findall(r"scripts/(check-[A-Za-z0-9-]+\.mjs)", step.get("run") or ""))
        return found

    def test_the_audit_mirrors_ci_bundle_size_gates(self) -> None:
        ci = yaml.safe_load((_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

        expected = self._mjs_gate_scripts(ci["jobs"]["bundle-size"])
        # A scan that matches nothing would pass the subset assertion by
        # measuring nothing, which is the failure this class is about.
        assert expected, (
            "found no check-*.mjs gate in ci.yml's bundle-size job; the scan no longer "
            "sees the gates it is meant to pin, so the assertion below proves nothing"
        )

        actual = self._mjs_gate_scripts(audit["jobs"]["bundle-ceiling"])

        assert expected <= actual, (
            "ci.yml's bundle-size job runs frontend ceiling gate(s) the Main Ratchet Audit "
            f"does not: {sorted(expected - actual)}. Mirror the step into bundle-ceiling; "
            "an unmirrored ceiling is measured on pull requests only, so main drifts past "
            "it and the breach lands on the next contributor."
        )

    def test_the_bundle_lane_builds_the_report_the_gates_read(self) -> None:
        # The gates read dist/bundle-report.json, which only an analyze-mode build
        # writes. Mirroring the gate steps without that build gives a lane that
        # cannot measure anything, and a gate with no input is the false green
        # this workflow exists to close.
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
        runs = " ".join(s.get("run") or "" for s in audit["jobs"]["bundle-ceiling"]["steps"])

        assert "--mode analyze" in runs, (
            "bundle-ceiling runs the size gates without an analyze-mode build, so "
            "dist/bundle-report.json is never written and the gates have no input"
        )

    def test_the_bundle_lane_is_wired_into_the_reporter(self) -> None:
        # Structural half, readable on every platform: the reporter can only
        # report on a lane it depends on and can read. The behavioural half --
        # that a bundle-only drift actually reaches the issue under its own name
        # -- runs the step itself, below.
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
        report = audit["jobs"]["report"]

        assert "bundle-ceiling" in report["needs"], (
            "the reporter does not depend on bundle-ceiling, so a bundle ceiling breach "
            "never reaches the tracking issue"
        )

        env = next(step["env"] for step in report["steps"] if "env" in step)
        assert "BUNDLE_RESULT" in env, "the reporter cannot read the bundle lane's result"


@_needs_posix_shell
class TestBundleDriftReachesTheRecord:
    """A bundle-only drift fails the run and names itself in the tracking issue.

    The measurement is only half the fix. If the ceiling is breached on main and
    the durable record does not say so -- or says only that "a ceiling" drifted --
    the reader is back to opening run logs, which is the state this workflow
    exists to replace. Driven through the step itself rather than by matching the
    script's text, because a script can mention a lane and still never render it.
    """

    def test_a_bundle_only_drift_is_recorded_and_fails_the_run(self, tmp_path: Path) -> None:
        rc, out, calls = _run(
            tmp_path, head_sha=_CURRENT, ratchet="success", frontend="success", bundle="failure"
        )

        assert rc == 1, out
        commented = _commented_issues(calls)
        assert len(commented) == 1, out
        # Its own name, and the other two lanes absent: a reader must be able to
        # tell WHICH ceiling drifted straight from the record.
        assert "bundle-ceiling" in commented[0], commented[0]
        assert "frontend-ceiling" not in commented[0], commented[0]
        assert "ratchet-gates" not in commented[0], commented[0]

    def test_a_bundle_only_drift_opens_an_issue_when_none_is_open(self, tmp_path: Path) -> None:
        rc, out, calls = _run(
            tmp_path,
            head_sha=_CURRENT,
            ratchet="success",
            bundle="failure",
            open_json="[]",
        )

        assert rc == 1, out
        created = [c for c in calls if c.startswith("issue create")]
        assert len(created) == 1, out
        assert "bundle-ceiling" in created[0], created[0]

    def test_all_three_lanes_green_still_closes_the_issue(self, tmp_path: Path) -> None:
        # The new lane must not make the all-green branch unreachable: a third
        # `needs` that never reads `success` would leave the record open forever
        # and the self-heal would be dead.
        rc, out, calls = _run(
            tmp_path, head_sha=_CURRENT, ratchet="success", frontend="success", bundle="success"
        )

        assert rc == 0, out
        assert _closed_issues(calls) == ["issue close 4242 --repo o/r --reason completed"], out

    def test_a_skipped_bundle_lane_counts_as_drift(self, tmp_path: Path) -> None:
        # `skipped` and `cancelled` are unknown verdicts, not green ones. The
        # reporter treats anything other than an explicit success as drift, and
        # that has to hold for this lane too, or an evicted bundle measurement
        # closes the record it should have kept open.
        rc, out, calls = _run(tmp_path, head_sha=_CURRENT, ratchet="success", bundle="skipped")

        assert rc == 1, out
        assert _closed_issues(calls) == [], out
        assert len(_commented_issues(calls)) == 1, out


class TestEveryGateReports:
    """One drifting gate must not skip the gates after it."""

    # Every job that holds more than one gate step. A lane with a single gate has
    # nothing after it to skip, so the condition buys it nothing; a lane with two
    # or more is where the default silently costs a verdict, and ci.yml's own
    # bundle lane shows it -- on the one push to main where its size gate failed,
    # its cycle gate read `skipped`.
    _JOBS_WITH_GATES = ("ratchet-gates", "bundle-ceiling")

    @staticmethod
    def _gate_steps(job: dict) -> list[dict]:
        steps = job.get("steps") or []
        return [
            s
            for s in steps
            if any(
                token in (s.get("run") or "")
                for token in ("scripts/check_", "scripts/check-", "pytest")
            )
        ]

    def test_gate_steps_do_not_stop_at_the_first_failure(self) -> None:
        # A step defaults to `if: success()`, so the first drifting gate would
        # skip the rest and the run would name only the gate that happens to be
        # listed first -- a partial verdict that looks like a full one, for as
        # long as that one drift stayed unfixed.
        audit = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))

        for job_name in self._JOBS_WITH_GATES:
            gate_steps = self._gate_steps(audit["jobs"][job_name])
            assert len(gate_steps) > 1, (
                f"expected {job_name!r} to hold several gate steps; found "
                f"{len(gate_steps)}, so this pin no longer measures what it describes"
            )

            for step in gate_steps:
                condition = str(step.get("if", "")).replace(" ", "")
                assert "!cancelled()" in condition, (
                    f"gate step {step.get('name')!r} in {job_name!r} runs on the default "
                    "success() condition, so an earlier drifting gate skips it and its own "
                    "drift goes unmeasured"
                )
