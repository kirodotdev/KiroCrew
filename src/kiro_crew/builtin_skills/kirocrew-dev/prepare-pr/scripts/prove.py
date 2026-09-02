#!/usr/bin/env python3
"""prove.py - prove a change's tests actually catch the bug the change fixes.

Reverts the change's PRODUCTION hunks, keeps its TEST hunks, and re-runs only
the changed test files.  A test that does not fail once the bug is back does not
test the fix, however green it looks.

This is not mutation testing.  No mutant is invented: the mutation is the exact
inverse of the diff under review, so a red run is evidence about *this* change
rather than about an arbitrary perturbation.  The cost is one extra pytest run
over one or two files.

Two properties make the verdict trustworthy:

* **It never touches the caller's tree.**  All work happens in a throwaway git
  worktree at HEAD, so there is nothing to restore and no uncommitted work to
  lose.  The alternative -- mutating in place and restoring afterwards -- has two
  ways to destroy work: ``git checkout --`` discards unrelated uncommitted edits
  in the same file, and a restore sequenced with ``&&`` runs only when pytest
  exits 0, i.e. only when the mutation did NOT do its job, leaving a sabotaged
  file behind.
* **It reads assertions, not exit status.**  Reverting production code can remove
  a symbol a test imports, and then every test fails at collection.  The exit
  code is non-zero and the run looks like proof while proving nothing.  So the
  verdict comes from pytest's own per-phase report and requires a failure at
  phase ``call``; a collection or setup fault is inconclusive.

Operates on committed state.  A dirty changed file is refused rather than
silently proving a snapshot the caller cannot see.

Portable: stdlib only; shells out to git and pytest via argument lists.

Usage:  python3 prove.py [--base <branch>] [--per-hunk]
Exit:   0 PROVEN | 20 NOT_PROVEN | 21 INCONCLUSIVE | 10 NOTHING_TO_PROVE
        | 30 BASELINE_RED | 2 environment error
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

# Emitted into the temp dir and put on PYTHONPATH so pytest reports its own
# counts.  Reading pytest's report objects is both safer and more precise than
# parsing a junit file: no XML parser is involved (the stdlib one resolves
# external entities, and this script must stay dependency-free), and `when`
# distinguishes a failed ASSERTION from a collection or setup fault directly
# rather than inferring it from element names.
_REPORTER_PLUGIN = """
import json
import os

import pytest

_counts = {"failures": 0, "errors": 0, "tests_run": 0}

# A failure at phase call is only evidence that an assertion observed the bug if
# the exception WAS an assertion.  Reverting production code can just as easily
# make the test raise AttributeError/TypeError mid-call -- nothing was asserted,
# so counting that would manufacture proof out of a broken call.  pytest.fail()
# and an unmet pytest.raises() both surface as Failed and ARE deliberate test
# judgements, so they count.
_ASSERT_EXC = tuple(
    t for t in (AssertionError, getattr(pytest.fail, "Exception", None)) if t is not None
)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call" and report.failed:
        excinfo = call.excinfo
        report.prove_assertion = bool(
            excinfo is not None and issubclass(excinfo.type, _ASSERT_EXC)
        )


def pytest_runtest_logreport(report):
    if report.when == "call":
        _counts["tests_run"] += 1
        # A strict xfail that unexpectedly PASSES is reported failed at phase
        # call, but nothing asserted the reverted bug -- counting it would
        # manufacture proof out of an xfail flip.
        if report.outcome == "failed" and not getattr(report, "wasxfail", None):
            if getattr(report, "prove_assertion", False):
                _counts["failures"] += 1
            else:
                _counts["errors"] += 1
    elif report.failed:
        # setup/teardown fault: the test never reached its assertion.
        _counts["errors"] += 1


def pytest_collectreport(report):
    if report.failed:
        _counts["errors"] += 1


def pytest_sessionfinish(session):
    with open(os.environ["PROVE_REPORT"], "w", encoding="utf-8") as fh:
        json.dump(_counts, fh)
"""

EXIT_PROVEN = 0
EXIT_NOTHING_TO_PROVE = 10
EXIT_NOT_PROVEN = 20
EXIT_INCONCLUSIVE = 21
EXIT_BASELINE_RED = 30
EXIT_ENV = 2

# The REMOTE base, deliberately.  A local `main` that has fallen behind makes the
# diff include hunks already upstream; reverting those can turn some other
# commit's test red and report this change PROVEN on evidence it did not earn.
DEFAULT_BASE = "origin/main"

# Per-run wall-clock ceiling.  A proof is meant to cost seconds over one or two
# files; a run that reaches this bound is reported as an environment fault
# rather than silently read as a verdict.
PYTEST_TIMEOUT_SECS = 900

# A hunk header carries the post-image start line, which is what identifies the
# hunk to a human reading the report.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run git with a fixed argv, capturing both streams as text."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        # surrogateescape, not replace: this helper CAPTURES the diffs that
        # feed git apply below, and a payload must round-trip byte-exactly.
        errors="surrogateescape",
        check=False,
    )


def _git_ok(args: list[str], cwd: Path) -> str:
    """Run git and return stdout, or raise EnvironmentError with git's own message."""
    r = _git(args, cwd)
    if r.returncode != 0:
        raise EnvironmentError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def is_test_path(path: str) -> bool:
    """Whether *path* is a test file by this repo's layout and naming.

    Both the directory prefix and the filename shape are checked: tests live
    under ``test/`` here, but a helper named ``test_*.py`` elsewhere in the tree
    is still a test, and treating it as production would revert it.
    """
    p = path.replace(os.sep, "/")
    if p.startswith(("test/", "tests/")) or "/tests/" in p or "/test/" in p:
        return True
    name = p.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def is_doc_path(path: str) -> bool:
    """True for paths whose reversion no test could ever observe.

    Prose cannot change behaviour, so reverting a documentation hunk alone can
    never turn a test red.  Counting that as ``uncaught`` would make --per-hunk
    report NOT_PROVEN on any change that updates its own spec -- which this
    repo's AGENTS.md mandates in the same commit -- burying the real uncaught
    code hunks in doc noise.  Note this is narrower than "non-Python": JSON and
    config ARE reverted, because a limit or flag change is behaviour-bearing.
    """
    lowered = path.lower()
    return lowered.endswith((".md", ".rst", ".txt")) or lowered.startswith("docs/")


def classify(paths: list[str]) -> tuple[list[str], list[str]]:
    """Split changed paths into (production, test), keeping only Python files.

    Every non-test path counts as production, including JSON, config and non-Python
    source.  Restricting production to ``.py`` would leave part of the change
    standing during the mutation, so a mixed change could report PROVEN while a
    reverted config hunk was never exercised.  Only the tests actually RUN are
    restricted to Python, because pytest is what runs them.
    """
    prod: list[str] = []
    tests: list[str] = []
    for p in paths:
        if is_test_path(p):
            if p.endswith(".py"):
                tests.append(p)
        else:
            prod.append(p)
    return prod, tests


def split_hunks(patch: str) -> list[tuple[str, str]]:
    """Split a unified diff into one applyable patch per hunk.

    Returns (label, patch_text) pairs, where the label is ``path:line`` of the
    hunk's post-image start so a report can name which hunk went uncaught.  Each
    emitted patch repeats its file's header, because ``git apply`` needs the
    header to locate the target.
    """
    out: list[tuple[str, str]] = []
    header: list[str] = []
    path = ""
    hunk: list[str] = []
    label = ""

    def flush() -> None:
        if hunk and header:
            out.append((label, "".join(header) + "".join(hunk)))

    for line in patch.splitlines(keepends=True):
        if line.startswith("diff --git "):
            flush()
            hunk, header, label = [], [line], ""
            path = line.rstrip("\n").split(" b/")[-1]
        elif line.startswith("@@"):
            flush()
            hunk = [line]
            m = _HUNK_RE.match(line)
            label = f"{path}:{m.group(1)}" if m else path
        elif hunk:
            hunk.append(line)
        else:
            header.append(line)
    flush()
    return out


def run_pytest(worktree: Path, test_files: list[str], report: Path) -> Optional[int]:
    """Run only *test_files* in *worktree*, writing a JSON count file to *report*.

    Returns pytest's exit code, or None if it exceeded the time bound.

    ``-n0`` and the addopts override are deliberate.  The repo's addopts carry
    ``-n auto --dist loadgroup`` for the full suite; across one or two files that
    buys nothing and costs determinism, and non-deterministic ordering would make
    per-hunk attribution unreproducible.  With xdist off, ``--dist`` has no
    meaning to preserve.

    ``PYTHONDONTWRITEBYTECODE`` is load-bearing, not hygiene.  Reverting a hunk
    frequently yields a file of the SAME byte length (``- 1`` becomes ``- 2``),
    and CPython invalidates a cached ``.pyc`` on (mtime, size).  When the revert
    lands in the same filesystem mtime tick as the baseline run that populated
    ``__pycache__``, both keys match and the stale bytecode is reused: the
    mutation never reaches the interpreter, the test passes, and a strong test is
    reported NOT_PROVEN.  Writing no bytecode at all removes the failure mode
    rather than racing it.
    """
    plugin_dir = report.parent
    (plugin_dir / "_prove_reporter.py").write_text(_REPORTER_PLUGIN, encoding="utf-8")
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "-p",
        "_prove_reporter",
        "-n0",
        "--override-ini=addopts=--ignore=build/private",
        *test_files,
    ]
    env = {
        **os.environ,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(plugin_dir),
        "PROVE_REPORT": str(report),
    }
    try:
        r = subprocess.run(
            cmd,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PYTEST_TIMEOUT_SECS,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None
    return r.returncode


def read_outcome(report: Path) -> tuple[int, int, int]:
    """Return (assertion_failures, errors, tests_run) from the reporter's JSON.

    The distinction is the whole point of asking pytest rather than reading its
    exit code: a failure recorded at phase ``call`` means a test ran and its
    assertion lost, which is the only evidence that the test observes the bug.  A
    collection or setup fault means the test never got that far -- which a broken
    revert produces in bulk and which says nothing about the assertion.
    """
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise EnvironmentError(f"pytest wrote no usable report at {report}: {exc}") from exc
    return int(data["failures"]), int(data["errors"]), int(data["tests_run"])


def dirty_among(changed: list[str], repo: Path) -> list[str]:
    """Changed paths that also carry uncommitted edits.

    Proving runs against committed state, so an uncommitted edit to a file under
    proof means the verdict describes code the caller is not looking at.  That is
    the same shape of false pass this script exists to prevent, so it is refused
    rather than warned about.
    """
    out = _git_ok(["status", "--porcelain", "--", *changed], repo) if changed else ""
    return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]


def prove(repo: Path, base: str, per_hunk: bool) -> dict:
    """Run the proof and return a result record; see module docstring for exits."""
    # "--" is required: without it a base like "--octopus" is parsed as a git
    # option, merge-base returns HEAD, and the empty diff that follows reports
    # NOTHING_TO_PROVE for a change that has plenty to prove.
    merge_base = _git_ok(["merge-base", "--", base, "HEAD"], repo).strip()
    if not merge_base:
        raise EnvironmentError(f"no merge base between {base} and HEAD")

    changed = [
        p for p in _git_ok(["diff", "--name-only", merge_base, "HEAD"], repo).splitlines() if p
    ]
    prod, tests = classify(changed)

    if not tests:
        return {
            "verdict": "NOTHING_TO_PROVE",
            "exit": EXIT_NOTHING_TO_PROVE,
            "reason": "the change adds or edits no test file",
        }
    if not prod:
        return {
            "verdict": "NOTHING_TO_PROVE",
            "exit": EXIT_NOTHING_TO_PROVE,
            "reason": "test-only change: no production hunk to revert",
        }

    stale = dirty_among(prod + tests, repo)
    if stale:
        raise EnvironmentError(
            "uncommitted edits in files under proof; commit or stash them first: "
            + ", ".join(stale)
        )

    patch = _git_ok(["diff", merge_base, "HEAD", "--", *prod], repo)
    if not patch.strip():
        return {
            "verdict": "NOTHING_TO_PROVE",
            "exit": EXIT_NOTHING_TO_PROVE,
            "reason": "production diff is empty",
        }

    mutations = split_hunks(patch) if per_hunk else [("all production hunks", patch)]
    if per_hunk and not mutations:
        raise EnvironmentError("could not split the production diff into hunks")

    tmp = Path(tempfile.mkdtemp(prefix="kc-prove-"))
    worktree = tmp / "wt"
    try:
        r = _git(["worktree", "add", "-q", "--detach", str(worktree), "HEAD"], repo)
        if r.returncode != 0:
            raise EnvironmentError(f"worktree add failed: {r.stderr.strip()}")

        baseline = run_pytest(worktree, tests, tmp / "baseline.json")
        if baseline is None:
            raise EnvironmentError(f"baseline run exceeded {PYTEST_TIMEOUT_SECS}s")
        if baseline != 0:
            return {
                "verdict": "BASELINE_RED",
                "exit": EXIT_BASELINE_RED,
                "reason": "the changed tests do not pass before mutation, "
                "so a red mutated run cannot be attributed",
                "tests": tests,
            }

        results = []
        for idx, (label, mutation) in enumerate(mutations):
            # Per-hunk only: a documentation hunk is unfalsifiable, so reverting
            # it would always read "uncaught". Bucket it separately instead of
            # letting prose drive a NOT_PROVEN verdict.
            if per_hunk and is_doc_path(label.rsplit(":", 1)[0]):
                results.append(
                    {
                        "hunk": label,
                        "outcome": "unprovable",
                        "failures": 0,
                        "errors": 0,
                        "tests_run": 0,
                    }
                )
                continue
            applied = subprocess.run(
                ["git", "apply", "-R", "-"],
                cwd=str(worktree),
                input=mutation,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
            )
            if applied.returncode != 0:
                raise EnvironmentError(f"could not revert {label}: {applied.stderr.strip()}")

            report = tmp / f"mutated-{idx}.json"
            code = run_pytest(worktree, tests, report)
            if code is None:
                raise EnvironmentError(f"mutated run exceeded {PYTEST_TIMEOUT_SECS}s")

            failures, errors, total = read_outcome(report)
            if failures:
                outcome = "caught"
            elif errors:
                outcome = "inconclusive"
            else:
                outcome = "uncaught"
            results.append(
                {
                    "hunk": label,
                    "outcome": outcome,
                    "failures": failures,
                    "errors": errors,
                    "tests_run": total,
                }
            )

            # Re-apply so each hunk is measured against the unmutated tree.
            forward = subprocess.run(
                ["git", "apply", "-"],
                cwd=str(worktree),
                input=mutation,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
            )
            if forward.returncode != 0:
                raise EnvironmentError(
                    f"could not restore {label} in the worktree: {forward.stderr.strip()}"
                )

        uncaught = [r for r in results if r["outcome"] == "uncaught"]
        inconclusive = [r for r in results if r["outcome"] == "inconclusive"]
        if uncaught:
            verdict, code = "NOT_PROVEN", EXIT_NOT_PROVEN
            reason = "the tests still pass with the bug reintroduced"
        elif inconclusive:
            verdict, code = "INCONCLUSIVE", EXIT_INCONCLUSIVE
            reason = (
                "the mutated run only errored at collection, so no assertion " "observed the bug"
            )
        else:
            verdict, code = "PROVEN", EXIT_PROVEN
            reason = "an assertion failed with the bug reintroduced"
        return {
            "verdict": verdict,
            "exit": code,
            "reason": reason,
            "tests": tests,
            "results": results,
        }
    finally:
        _git(["worktree", "remove", "--force", str(worktree)], repo)
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--base", default=DEFAULT_BASE, help=f"base ref to diff against (default: {DEFAULT_BASE})"
    )
    ap.add_argument(
        "--per-hunk",
        action="store_true",
        help="revert one production hunk at a time and name the uncaught ones",
    )
    args = ap.parse_args()

    if not shutil.which("git"):
        print("prove: git not found on PATH", file=sys.stderr)
        return EXIT_ENV

    repo_root = _git(["rev-parse", "--show-toplevel"], Path.cwd())
    if repo_root.returncode != 0:
        print("prove: not inside a git repository", file=sys.stderr)
        return EXIT_ENV
    repo = Path(repo_root.stdout.strip())

    try:
        result = prove(repo, args.base, args.per_hunk)
    except EnvironmentError as exc:
        print(f"prove: {exc}", file=sys.stderr)
        return EXIT_ENV

    print(f"{result['verdict']}: {result['reason']}")
    for row in result.get("results", []):
        if row["outcome"] != "caught":
            print(f"  {row['outcome']}: {row['hunk']}")
    return int(result["exit"])


if __name__ == "__main__":
    sys.exit(main())
