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

It also prints the one line a ``fix:`` pull request pastes under ``## Tests`` --
``Proof: PROVEN <tests that fail without the fix> (head <sha12>)``, or ``Proof: not
provable -- <reason>`` -- and writes ``<git-dir>/prepare-pr-prove.json``, which
``diff_signals.py --check-body`` compares a ``PROVEN`` line with, for the same tree.
The named tests are those of the changed test files that fail with the production
hunks reverted; a control test that passes both ways is not named and stays valid.

Exit 11 is for a change whose only tests are frontend tests AND whose production files are
all under ``website/``. A backend fix that carries a frontend test exits 10 with no line:
"prove.py runs Python only" is not why its Python change goes unproven.

Usage:  python3 prove.py [--base <branch>] [--per-hunk]
Exit:   0 PROVEN | 20 NOT_PROVEN | 21 INCONCLUSIVE | 10 NOTHING_TO_PROVE
        | 11 FRONTEND_TESTS | 30 BASELINE_RED | 2 environment error
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
from typing import Any, Optional

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

_counts = {"failures": 0, "errors": 0, "tests_run": 0, "failed_ids": []}

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
                _counts["failed_ids"].append(report.nodeid)
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
EXIT_FRONTEND_TESTS = 11
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

# The record ``diff_signals.py --check-body`` reads, inside the git directory.
RECORD_FILENAME = "prepare-pr-prove.json"

# Most tests a Proof line names; the record keeps them all.
MAX_NAMED_TESTS = 5

REASON_FRONTEND = "frontend test (prove.py runs Python only)"
REASON_TEST_ONLY = "test-only change"
REASON_DOCS_OR_CONFIG = "docs or config only"
REASON_TIMED_OUT = "prove.py timed out"

# A hunk header carries the post-image start line, which is what identifies the
# hunk to a human reading the report.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


# Variables that point git at a repository other than the one in ``cwd``. A parent git
# hook exports some of them; left in place they retarget every call below.
_GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_COMMON_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def _without_git_location(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k not in _GIT_LOCATION_VARS}


# The two exec vectors pinned off on EVERY git call here, as argv options. ``-c`` is the
# highest-precedence source: it beats the repository, global and system config, the
# ``GIT_CONFIG_COUNT`` form, and a ``GIT_CONFIG_PARAMETERS`` inherited from a parent
# ``git -c core.hooksPath=<dir> ...``. ``git worktree add`` runs ``post-checkout`` and the
# throwaway proof worktree must run no hook.
GIT_PINS = ("-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false")


def _git_argv(args: list[str]) -> list[str]:
    return ["git", *GIT_PINS, *args]


def _git_env() -> dict[str, str]:
    """The environment for every git call here.

    Location variables are removed, and ``GIT_CONFIG_PARAMETERS`` too: it is what a parent
    ``git -c key=value`` exports to its children, so keeping it would carry that caller's
    one-off config (a hooks path, say) into the proof. The rest of the caller's config is
    kept: it may hold ``safe.directory`` or an identity. The exec vectors are pinned in
    argv by ``_git_argv``, not here.
    """
    env = _without_git_location(dict(os.environ))
    env.pop("GIT_CONFIG_PARAMETERS", None)
    return env


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run git with a fixed argv, capturing both streams as text."""
    return subprocess.run(
        _git_argv(args),
        cwd=str(cwd),
        env=_git_env(),
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


_FRONTEND_TEST_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_CONFIG_SUFFIXES = (".json", ".yml", ".yaml", ".toml", ".ini", ".cfg")


def is_frontend_test_path(path: str) -> bool:
    """True for a website test file, which pytest cannot run (a vitest runner can)."""
    p = path.replace(os.sep, "/")
    return p.startswith("website/") and (
        _FRONTEND_TEST_RE.search(p) is not None or "/__tests__/" in p
    )


def is_docs_or_config(path: str) -> bool:
    """True for prose and configuration, which no Python test observes."""
    return is_doc_path(path) or path.lower().endswith(_CONFIG_SUFFIXES)


def is_frontend_only(changed: list[str]) -> bool:
    """True when every production path in *changed* is under ``website/``.

    A path is production unless it is a frontend test, prose or configuration. A backend
    fix that happens to carry a frontend test is not frontend-only, so "prove.py runs
    Python only" would not be the reason its Python change goes unproven.
    """
    production = [p for p in changed if not is_frontend_test_path(p) and not is_docs_or_config(p)]
    return all(p.replace(os.sep, "/").startswith("website/") for p in production)


class ProveTimeout(EnvironmentError):
    """A pytest run reached ``PYTEST_TIMEOUT_SECS``; still an environment fault (exit 2)."""


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

    ``-p no:platformdirs`` is a consequence of the addopts override above, and it
    is what keeps a verdict about the change from depending on the host's
    installed packages.  ``platformdirs`` declares a ``pytest11`` entry point, so
    pytest imports it at startup; an entry point that raises on import aborts the
    run before collection.  This child runs with ``addopts`` replaced and often
    in a throwaway worktree, so neither the repo config nor the target repo's own
    config can refuse that plugin on its behalf.  A child killed that way exits
    non-zero having asserted nothing, which ``prove`` cannot tell from a genuine
    baseline failure -- so it would report BASELINE_RED for a reason that has
    nothing to do with the diff under test.
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
        "no:platformdirs",
        "-p",
        "_prove_reporter",
        "-n0",
        "--override-ini=addopts=--ignore=build/private",
        *test_files,
    ]
    env = {
        **_without_git_location(dict(os.environ)),  # the tests run in the worktree, not the repo
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


def read_failed_ids(report: Path) -> list[str]:
    """Node ids of the tests whose assertion failed in the run that wrote *report*."""
    try:
        data = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    ids = data.get("failed_ids", [])
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []


def escape_name(name: str) -> str:
    """A test id as it is printed in a PR body.

    A parametrized id can carry ``<!--`` (a generated marker), which the body parser
    reads as an HTML comment and strips. ``&#33;`` is ``!`` to a reader and to
    ``proof_line_check.py``, which decodes it before comparing with the record.
    """
    return name.replace("<!--", "<&#33;--")


def proof_line(result: dict, head: str) -> Optional[str]:
    """The ``Proof:`` line to paste under ``## Tests``, or None when no line is honest.

    NOT_PROVEN, INCONCLUSIVE and BASELINE_RED have none: the author fixes the tests
    first. An unnamed reason (a change with no test file and code in it) has none
    either; the author writes ``other: <20+ characters>`` and says why.
    """
    if result["verdict"] == "PROVEN":
        named = result.get("named") or []
        if not named:
            return None
        names = ", ".join(escape_name(n) for n in named[:MAX_NAMED_TESTS])
        return "Proof: PROVEN {} (head {})".format(names, head[:12])
    reason = result.get("proof_reason")
    return "Proof: not provable -- {}".format(reason) if reason else None


def pin_commit(repo: Path) -> tuple[str, str]:
    """(commit id, tree id) of HEAD, resolved once before any test runs.

    A proof takes minutes. If the author commits meanwhile, HEAD moves; the run
    still judged the commit it started from, so that commit's ids, not HEAD's at
    the end, go into the printed line and the record.
    """
    commit = _git_ok(["rev-parse", "--verify", "HEAD^{commit}"], repo).strip()
    tree = _git_ok(["rev-parse", "--verify", commit + "^{tree}"], repo).strip()
    return commit, tree


def write_record(
    repo: Path, result: dict, line: Optional[str], commit: str, tree: str
) -> Optional[Path]:
    """Write ``<git-dir>/prepare-pr-prove.json`` for the commit and tree just judged.

    The record names the tree (not only the commit), so a message-only amend keeps
    it valid and any change of code makes it stale. ``commit`` and ``tree`` are the
    ids pinned before proving began. Returns the path, or None when git cannot say
    where its directory is.
    """
    gitdir = _git(["rev-parse", "--absolute-git-dir"], repo)
    if gitdir.returncode != 0:
        return None
    record = {
        "version": 1,
        "tree": tree,
        "head": commit,
        "verdict": result["verdict"],
        "exit": result["exit"],
        "tests": result.get("named", []),
        "line": line,
    }
    path = Path(gitdir.stdout.strip()) / RECORD_FILENAME
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return path


def dirty_among(changed: list[str], repo: Path) -> list[str]:
    """Changed paths that also carry uncommitted edits.

    Proving runs against committed state, so an uncommitted edit to a file under
    proof means the verdict describes code the caller is not looking at.  That is
    the same shape of false pass this script exists to prevent, so it is refused
    rather than warned about.
    """
    out = _git_ok(["status", "--porcelain", "--", *changed], repo) if changed else ""
    return [ln[3:].strip() for ln in out.splitlines() if ln.strip()]


def prove(repo: Path, base: str, per_hunk: bool, commit: str = "HEAD") -> dict:
    """Run the proof on *commit* and return a result record; see module docstring for exits.

    *commit* is diffed against the merge base and checked out in the throwaway
    worktree, so a HEAD that moves during the run changes nothing here.
    """
    # "--" is required: without it a base like "--octopus" is parsed as a git
    # option, merge-base returns HEAD, and the empty diff that follows reports
    # NOTHING_TO_PROVE for a change that has plenty to prove.
    merge_base = _git_ok(["merge-base", "--", base, commit], repo).strip()
    if not merge_base:
        raise EnvironmentError(f"no merge base between {base} and HEAD")

    changed = [
        p for p in _git_ok(["diff", "--name-only", merge_base, commit], repo).splitlines() if p
    ]
    prod, tests = classify(changed)

    if not tests:
        if any(is_frontend_test_path(p) for p in changed) and is_frontend_only(changed):
            return {
                "verdict": "FRONTEND_TESTS",
                "exit": EXIT_FRONTEND_TESTS,
                "reason": "the changed tests are frontend tests; prove.py runs Python only",
                "proof_reason": REASON_FRONTEND,
            }
        result = {
            "verdict": "NOTHING_TO_PROVE",
            "exit": EXIT_NOTHING_TO_PROVE,
            "reason": "the change adds or edits no test file",
        }
        if changed and all(is_docs_or_config(p) for p in changed):
            result["proof_reason"] = REASON_DOCS_OR_CONFIG
        return result
    if not prod:
        return {
            "verdict": "NOTHING_TO_PROVE",
            "exit": EXIT_NOTHING_TO_PROVE,
            "reason": "test-only change: no production hunk to revert",
            "proof_reason": REASON_TEST_ONLY,
        }

    stale = dirty_among(prod + tests, repo)
    if stale:
        raise EnvironmentError(
            "uncommitted edits in files under proof; commit or stash them first: "
            + ", ".join(stale)
        )

    patch = _git_ok(["diff", merge_base, commit, "--", *prod], repo)
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
        r = _git(["worktree", "add", "-q", "--detach", str(worktree), commit], repo)
        if r.returncode != 0:
            raise EnvironmentError(f"worktree add failed: {r.stderr.strip()}")

        baseline = run_pytest(worktree, tests, tmp / "baseline.json")
        if baseline is None:
            raise ProveTimeout(f"baseline run exceeded {PYTEST_TIMEOUT_SECS}s")
        if baseline != 0:
            return {
                "verdict": "BASELINE_RED",
                "exit": EXIT_BASELINE_RED,
                "reason": "the changed tests do not pass before mutation, "
                "so a red mutated run cannot be attributed",
                "tests": tests,
            }

        results: list[dict[str, Any]] = []
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
                _git_argv(["apply", "-R", "-"]),
                cwd=str(worktree),
                input=mutation,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=_git_env(),
            )
            if applied.returncode != 0:
                raise EnvironmentError(f"could not revert {label}: {applied.stderr.strip()}")

            report = tmp / f"mutated-{idx}.json"
            code = run_pytest(worktree, tests, report)
            if code is None:
                raise ProveTimeout(f"mutated run exceeded {PYTEST_TIMEOUT_SECS}s")

            failures, errors, total = read_outcome(report)
            failed = read_failed_ids(report)
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
                    "failed": failed,
                }
            )

            # Re-apply so each hunk is measured against the unmutated tree.
            forward = subprocess.run(
                _git_argv(["apply", "-"]),
                cwd=str(worktree),
                input=mutation,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=_git_env(),
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
        named: list[str] = []
        for row in results:
            for test_id in row.get("failed", []):
                if test_id not in named:
                    named.append(test_id)
        return {
            "verdict": verdict,
            "exit": code,
            "reason": reason,
            "tests": tests,
            "named": named,
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
        commit, tree = pin_commit(repo)
        result = prove(repo, args.base, args.per_hunk, commit)
    except ProveTimeout as exc:
        print(f"prove: {exc}", file=sys.stderr)
        result = {
            "verdict": "TIMED_OUT",
            "exit": EXIT_ENV,
            "reason": str(exc),
            "proof_reason": REASON_TIMED_OUT,
        }
    except EnvironmentError as exc:
        print(f"prove: {exc}", file=sys.stderr)
        return EXIT_ENV

    print(f"{result['verdict']}: {result['reason']}")
    for row in result.get("results", []):
        if row["outcome"] != "caught":
            print(f"  {row['outcome']}: {row['hunk']}")
    line = proof_line(result, commit)
    write_record(repo, result, line, commit, tree)
    if line:
        print(line)
    return int(result["exit"])


if __name__ == "__main__":
    sys.exit(main())
