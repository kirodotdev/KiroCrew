#!/usr/bin/env python3
"""Run only the test files a test-only PR touched, repeated, instead of the suite.

Why this reduction is sound when the general one is not
------------------------------------------------------
`run_scoped_tests.py` deliberately refuses to narrow WITHIN a surface, and its
docstring says why: answering "which tests reach this changed module?" needs a
real import graph, and six review rounds proved a text scan cannot enumerate the
ways a test can reach a module.

This script does NOT retry that. It answers two questions that are decidable
without an import graph, and it escalates to the full suite on anything else:

    1. Does any OTHER file depend on the test files this diff touched?
    2. Can this diff change the SET of test files, rather than only their contents?

Question 1 is a reverse lookup over a closed set of names, not an open-ended
reachability guess. Question 2 is the load-bearing one, and it is why this
version does not repeat the earlier failure: two independent reviewers found a
tree-scanning gate the first version's regex missed (`test_workflows_presence`
reaches the tree via `Path(__file__).resolve().parent` + `.glob(...)`, never the
`/ "test"` join it looked for). Enumerating join spellings is the same losing
game as enumerating import spellings, so the rule changed instead of the regex:

    A diff that only MODIFIES existing leaf test files cannot change the set of
    test files, so no gate that asserts on that SET can flip.

Adding, deleting or renaming a test file escalates to the full suite. That closes
the whole class -- presence gates, ignore-list assertions, "every module has a
referencing test" -- by construction, with no pattern to keep current.

What a modify-only leaf diff can still break, and how each is closed
--------------------------------------------------------------------
* Another test IMPORTS the changed module (`test_chat_backfill` imports
  `test_chat_slack`; `test_connections_handoff` imports autouse fixtures from
  `test_connections_warm`). Closed by the reverse-import scan.
* Another test reaches it DYNAMICALLY -- five files do
  `importlib.import_module("test_slack_golden_transcript")`, which no
  import-statement pattern sees. Closed by ALSO escalating when the changed
  module's name appears as a quoted string anywhere else in the tree. That is
  deliberately over-broad (a mention in a comment escalates too) because it is
  spelling-independent: it covers `importlib`, `__import__`,
  `pytest.importorskip`, a `sys.modules` patch, and whatever comes next.
* A CORPUS GATE reads the `test/` tree as DATA and asserts on its CONTENTS, so
  editing a test file can flip a gate that names none of them
  (`test_jsondecodeerror_redundancy_ratchet` carries `test/` in `SCAN_ROOTS`).
  Closed by always appending every test that scans a directory and reaches the
  test tree in ANY form -- the `/ "test"` join or a `__file__`-relative parent.
  Derived, not hardcoded, and an empty derivation forfeits the reduction, because
  a dead scan and a working one look identical.

Repeats, and what they are for
------------------------------
The CHANGED files run REPEAT times in separate pytest processes: a flaky test run
once in isolation usually passes, so a single green would prove nothing about the
fix it is gating, and separate processes re-roll per-process state and load order
(this repo has no pytest-repeat, and separate processes are stronger anyway).

The corpus gates run ONCE. They are deterministic content assertions -- repeating
them buys nothing and, at ~3 minutes a pass, would dominate the run.

This does NOT reproduce whole-suite conditions: a flake that needs a specific
xdist worker neighbour cannot appear here. That residual is the price of the
reduction, and it is bounded to test outcomes, since a modify-only leaf diff
changes no production code path. Force the full matrix with `ci-full-run`.

Usage
-----
    SCOPED_TESTS_BASE_REF="$(git merge-base HEAD origin/main)" \\
        python3 scripts/leaf_test_scope.py --plan
    ... --targets     # bare list for CI: changed files first, then gates
    ... --gates       # just the run-once gate list
    ... --run         # execute (gates once, changed files repeated)
    ... --repeat 5    # override the repeat count (default 3)
    ... --files test/test_a.py    # decide for an explicit MODIFIED list
    ... --test        # self-test

Exit codes: 0 eligible / run green, 1 tests failed, 2 usage or environment error,
3 NOT eligible -- the caller must run the full suite.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The selector's stdout becomes argv for pytest, so it is validated with the SAME
# helper `run_scoped_tests.py` uses rather than a second copy of the rule -- two
# spellings of one admission check drift, and this one would drift silently.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from run_scoped_tests import (  # noqa: E402  (path set immediately above)
    SelectionUntrustworthy,
    has_broad_impact,
    resolve_base,
    validated_targets,
)

NOT_ELIGIBLE = 3
DEFAULT_REPEAT = 3

# Only files matching this, directly under `test/`, can be leaves. Anything else
# in the tree -- conftest.py, a helper module, fixtures/, a .txt corpus, the
# workflows/ and metrics/ subtrees -- is shared input to other files' outcomes.
_LEAF_NAME = re.compile(r"^test_[A-Za-z0-9_]+\.py$")

# `from <stem> import ...` / `import <stem>`, at any indentation: several of this
# repo's cross-test imports sit inside a function body (`test_chat_fork_error_codes`
# does `import test_error_code_contract as gate` inside the test), so anchoring to
# column zero would call a non-leaf a leaf. This names the importer for the
# escalation message; `mentions_of` is what makes the check SAFE.
_IMPORT = re.compile(
    r"^[ \t]*(?:from[ \t]+([A-Za-z_][\w.]*)[ \t]+import|import[ \t]+([A-Za-z_][\w.]*))",
    re.M,
)

# Any call that enumerates a directory. Paired with `_REACHES_TEST_TREE` below --
# a test that globs `src/` is not a corpus gate for a test-file edit.
_SCANS_A_DIR = re.compile(r"\.(?:glob|rglob|iterdir|scandir|listdir)\(|os\.walk\(")

# Two ways a test under `test/` names the test tree. The `/ "test"` join is the
# obvious one; `__file__` is the one the first version missed, because for a file
# that already lives in `test/`, `Path(__file__).resolve().parent` IS the tree --
# no literal `"test"` appears anywhere. Matching the CONCEPT rather than one
# spelling is the point: a third spelling should not need a code change.
_REACHES_TEST_TREE = re.compile(r"""/[ \t]*['"]test['"]|__file__""")

# The `/ "test"` join on its own is ALSO sufficient, without a directory scan: a
# gate can assert on the tree by checking named paths instead of enumerating them
# (`test_ci_surface_tests` asserts every name in `windows-collect-ignore.txt` is
# still present). Two independent sufficient conditions, unioned, because either
# one alone was demonstrably incomplete.
_JOINS_TEST_DIR = re.compile(r"""/[ \t]*['"]test['"]""")

_PY_SCAN_ROOTS = ("test", "scripts", "src")
_PY_SCAN_EXCLUDE = ("_vendor", "build", "node_modules", "__pycache__", ".venv")


def _rel_posix(path: Path, root: Path) -> str:
    """Repo-relative path with FORWARD slashes on every platform.

    `str(Path.relative_to(...))` yields `test\\test_x.py` on Windows, and that
    breaks this selector in two places at once: `run_scoped_tests._SAFE_TARGET`
    does not admit a backslash, so EVERY target is refused as "not a plain
    relative path", and the `test/` prefix rules in `classify` stop matching. Git
    reports paths with forward slashes on all platforms, so POSIX form is also
    what the diff side of the comparison already speaks.
    """
    return path.relative_to(root).as_posix()


def _iter_python(root: Path) -> list[Path]:
    out: list[Path] = []
    for base in _PY_SCAN_ROOTS:
        start = root / base
        if not start.is_dir():
            continue
        for path in start.rglob("*.py"):
            if any(part in _PY_SCAN_EXCLUDE for part in path.parts):
                continue
            out.append(path)
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        # Unreadable input cannot be cleared, and a reduction that silently
        # skipped a file it could not read would be exactly the wrong failure.
        raise SelectionUntrustworthy(f"cannot read {path} while classifying the diff") from None


def _run_git(argv: list[str]) -> str:
    proc = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0:
        raise SelectionUntrustworthy(f"{' '.join(argv[:3])} failed: {proc.stderr.strip()}")
    return proc.stdout


def changed_with_status(base_sha: str) -> dict[str, str]:
    """Map changed path -> one-letter git status, for committed AND dirty work.

    `run_scoped_tests.changed_files` returns names only, and this selector needs
    the STATUS: an added or deleted test file changes the SET of test files, which
    is what tree-scanning gates assert on, so only 'M' can qualify. Read
    NUL-delimited so a path git would otherwise C-quote cannot be misread, and
    with `--no-renames` so a rename arrives as the delete plus the add it is
    (both of which escalate) rather than a single 'R' that looks like an edit.
    """
    status: dict[str, str] = {}

    fields = _run_git(["git", "diff", "--name-status", "--no-renames", "-z", f"{base_sha}...HEAD"])
    parts = [p for p in fields.split("\0") if p != ""]
    for code, path in zip(parts[0::2], parts[1::2]):
        status[path] = code[:1].upper()

    dirty = _run_git(
        ["git", "status", "--porcelain", "--untracked-files=all", "--no-renames", "-z"]
    )
    for record in dirty.split("\0"):
        if len(record) > 3:
            path = record[3:]
            if not path:
                continue
            # Porcelain XY: '??' is untracked (a new file), and a staged-or-
            # unstaged letter in either column is that letter. An uncommitted
            # add must read as 'A', never as a modification.
            code = record[:2]
            status[path] = "A" if code.strip() == "??" else (code.strip()[:1].upper())
    return status


def importers_of(stems: set[str], root: Path) -> dict[str, str]:
    """Map each stem another file IMPORTS by statement to that importer.

    The reverse direction of the question `run_scoped_tests` refuses: who names
    THIS module, over the finite set of import statements in the tree. This is for
    the message; `mentions_of` is the safety net that does not depend on spelling.
    """
    if not stems:
        return {}
    found: dict[str, str] = {}
    for path in _iter_python(root):
        text = _read(path)
        for match in _IMPORT.finditer(text):
            module = (match.group(1) or match.group(2) or "").split(".")[0]
            if module in stems and module != path.stem:
                found.setdefault(module, _rel_posix(path, root))
    return found


def mentions_of(stems: set[str], root: Path) -> dict[str, str]:
    """Map each stem that appears as a QUOTED STRING in another file to that file.

    This is the spelling-independent half of the dependency check, and it exists
    because import syntax is not the only way to reach a module: five files in
    this repo do `importlib.import_module("test_slack_golden_transcript")`, which
    `_IMPORT` cannot see, and a purely syntactic scan would call that heavily
    depended-on module a leaf.

    Deliberately over-broad -- a stem named in a comment or a data list escalates
    too. That is the safe direction, and it is cheap here: it costs an occasional
    unnecessary full suite, never a missed dependency.
    """
    if not stems:
        return {}
    found: dict[str, str] = {}
    quoted = {stem: (f'"{stem}"', f"'{stem}'") for stem in stems}
    for path in _iter_python(root):
        text = _read(path)
        for stem, forms in quoted.items():
            if stem in found or path.stem == stem:
                continue
            if any(form in text for form in forms):
                found[stem] = _rel_posix(path, root)
    return found


def corpus_gates(root: Path) -> list[str]:
    """Tests that read the `test/` tree as data, so any test edit can flip them.

    Derived from the CONCEPT (enumerates a directory AND names the test tree)
    rather than one join spelling, which is what the first version got wrong.
    Over-inclusion is the safe direction and is what this buys: ~158 files against
    1,886, at roughly 3 minutes for one pass. A missed gate silently skips a check
    the diff really can break, which is the one outcome this reduction must avoid.
    """
    gates: list[str] = []
    test_dir = root / "test"
    if not test_dir.is_dir():
        return gates
    for path in sorted(test_dir.glob("test_*.py")):
        text = _read(path)
        scans_tree = _SCANS_A_DIR.search(text) and _REACHES_TEST_TREE.search(text)
        if scans_tree or _JOINS_TEST_DIR.search(text):
            gates.append(_rel_posix(path, root))
    return gates


def classify(
    status: dict[str, str], root: Path = REPO_ROOT
) -> tuple[tuple[list[str], list[str]] | None, str]:
    """Return ((changed, gates), reason), or (None, reason) to run the full suite."""
    paths = sorted(status)
    if not paths:
        return None, "full suite: diff is empty against the base"

    broad = has_broad_impact(paths)
    if broad:
        return None, f"full suite: broad-impact change {broad}"

    outside = [p for p in paths if not p.startswith("test/")]
    if outside:
        return None, (
            f"full suite: the diff is not test-only ({len(outside)} path(s) outside "
            f"test/, e.g. {outside[0]})"
        )

    non_leaf_shape = [p for p in paths if not _LEAF_NAME.match(p[len("test/") :])]
    if non_leaf_shape:
        return None, (
            f"full suite: {non_leaf_shape[0]} is shared test input (a helper, fixture, "
            "conftest or data file), not a leaf test module, so other files' outcomes "
            "depend on it"
        )

    # The load-bearing rule. Anything other than an in-place edit changes the SET
    # of test files, which is what a presence/ignore-list/referencing-test gate
    # asserts on -- and those cannot be enumerated by scanning for a spelling.
    set_changing = sorted(p for p, code in status.items() if code != "M")
    if set_changing:
        code = status[set_changing[0]]
        return None, (
            f"full suite: {set_changing[0]} is {code!r}, not a modification -- adding, "
            "deleting or renaming a test file changes the SET of test files, which the "
            "tree-scanning gates assert on"
        )

    stems = {Path(p).stem for p in paths}
    imported = importers_of(stems, root)
    if imported:
        stem, importer = sorted(imported.items())[0]
        return None, (
            f"full suite: test/{stem}.py is imported by {importer}, so it is shared "
            "input rather than a leaf (this repo has 21 such files, several exporting "
            "autouse fixtures)"
        )

    mentioned = mentions_of(stems, root)
    if mentioned:
        stem, mentioner = sorted(mentioned.items())[0]
        return None, (
            f"full suite: the name {stem!r} appears as a string in {mentioner}, so "
            "something may reach it dynamically (five files in this repo use "
            "importlib.import_module on a test module)"
        )

    missing = [p for p in paths if not (root / p).is_file()]
    if missing:
        return None, f"full suite: {missing[0]} is not on disk, so it cannot be run"

    gates = corpus_gates(root)
    if not gates:
        return None, (
            "full suite: the corpus-gate derivation found no test that scans the "
            "test/ tree, which cannot be true in this repo -- treating the scan as "
            "broken rather than trusting an empty result"
        )

    gates = [g for g in gates if g not in set(paths)]
    return (paths, gates), (
        f"leaf tests: {len(paths)} modified leaf test file(s) repeated, plus "
        f"{len(gates)} corpus gate(s) that read the test/ tree, run once"
    )


def plan(base: str) -> tuple[tuple[list[str], list[str]] | None, str]:
    base_sha = resolve_base(base)
    try:
        status = changed_with_status(base_sha)
    except SelectionUntrustworthy as exc:
        return None, f"full suite: {exc}"
    return classify(status)


def pytest_argv(targets: list[str]) -> list[str]:
    """Match CI's reduced lane: no coverage (a subset's number is not comparable).

    `--` ends option parsing so nothing after it can be read as a flag, and
    `validated_targets` is the real protection either way.
    """
    return [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--no-cov",
        "--",
        *validated_targets(targets, REPO_ROOT),
    ]


def run(changed: list[str], gates: list[str], repeat: int) -> int:
    """Gates once, changed files `repeat` times. See the module docstring."""
    if gates:
        argv = pytest_argv(gates)
        print(f"leaf_test_scope: corpus gates ({len(gates)} file(s), once)", flush=True)
        rc = subprocess.run(
            argv, cwd=str(REPO_ROOT), check=False
        ).returncode  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        if rc != 0:
            print(f"leaf_test_scope: FAILED in the corpus gates (rc={rc}).", file=sys.stderr)
            return 1

    argv = pytest_argv(changed)
    for attempt in range(1, repeat + 1):
        print(f"leaf_test_scope: changed files, pass {attempt}/{repeat}", flush=True)
        rc = subprocess.run(
            argv, cwd=str(REPO_ROOT), check=False
        ).returncode  # noqa: E501  # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-tainted-env-args.dangerous-subprocess-use-tainted-env-args
        if rc != 0:
            print(
                f"leaf_test_scope: FAILED on pass {attempt}/{repeat} (rc={rc}). A flake "
                "that only fails sometimes is still failing -- do not re-run to green.",
                file=sys.stderr,
            )
            return 1
    print(f"leaf_test_scope: {repeat} consecutive passes over {len(changed)} changed file(s).")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--plan", action="store_true", help="print the verdict and targets")
    mode.add_argument("--targets", action="store_true", help="changed files, then gates")
    mode.add_argument("--gates", action="store_true", help="print the run-once gate list")
    mode.add_argument("--run", action="store_true", help="run the narrow set")
    mode.add_argument("--test", action="store_true", help="run this script's self-test")
    parser.add_argument("--repeat", type=int, default=DEFAULT_REPEAT)
    parser.add_argument("--files", nargs="*", help="classify this list, treated as MODIFICATIONS")
    args = parser.parse_args(argv)

    if args.test:
        return _self_test()
    if args.repeat < 1:
        parser.error("--repeat must be >= 1")

    try:
        if args.files is not None:
            resolved, reason = classify({p: "M" for p in args.files})
        else:
            resolved, reason = plan(os.environ.get("SCOPED_TESTS_BASE_REF", ""))
    except ValueError as exc:
        print(f"leaf_test_scope: {exc}", file=sys.stderr)
        return 2
    except SelectionUntrustworthy as exc:
        print(f"leaf_test_scope: full suite: {exc}", file=sys.stderr)
        return NOT_ELIGIBLE

    if resolved is None:
        if not (args.targets or args.gates):
            print(f"leaf_test_scope: {reason}")
        return NOT_ELIGIBLE

    changed, gates = resolved
    if args.targets:
        print("\n".join(changed + gates))
        return 0
    if args.gates:
        print("\n".join(gates))
        return 0

    print(f"leaf_test_scope: {reason}")
    for target in changed:
        print(f"  - {target}  (repeated)")
    if args.run:
        try:
            return run(changed, gates, args.repeat)
        except SelectionUntrustworthy as exc:
            print(f"leaf_test_scope: refusing to run: {exc}", file=sys.stderr)
            return NOT_ELIGIBLE
    return 0


def _self_test() -> int:
    """Prove every escalation fires. A reducer trusted without these is a guess."""
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        if not cond:
            failures.append(name)

    def verdict(paths: list[str], code: str = "M") -> object:
        return classify({p: code for p in paths})[0]

    # The derivations must resolve against the real tree. An empty result and a
    # broken scan are indistinguishable, which is how a dead path survived four
    # review rounds in run_scoped_tests.py.
    gates = corpus_gates(REPO_ROOT)
    check("corpus-gate derivation is non-empty", bool(gates))
    for known in (
        "test/test_coverage_omit_contract.py",
        "test/test_jsondecodeerror_redundancy_ratchet.py",
        "test/test_ci_surface_tests.py",
        # The gate the first version MISSED: it reaches the tree via
        # `Path(__file__).resolve().parent` + `.glob(...)`, never a `/ "test"` join.
        "test/test_workflows_presence.py",
    ):
        check(f"corpus gate derived: {known}", known in gates)
        check(f"corpus gate exists on disk: {known}", (REPO_ROOT / known).is_file())

    # The dependency checks must find this repo's real cross-test reaches.
    for stem, why in {
        "test_chat_slack": "test_chat_backfill imports _make_slack_app",
        "test_connections_warm": "test_connections_handoff imports autouse fixtures",
        "test_error_code_contract": "imported inside a function body",
    }.items():
        check(f"reverse-import finds {stem} ({why})", stem in importers_of({stem}, REPO_ROOT))
    check(
        "dynamic-mention finds an importlib.import_module consumer",
        "test_slack_golden_transcript" in mentions_of({"test_slack_golden_transcript"}, REPO_ROOT),
    )
    # A leaf named as a STRING here would be found by `mentions_of` (which scans
    # `scripts/` too) and would stop qualifying -- the check would then fail on its
    # own fixture. Discover one instead, which also proves a clean leaf exists.
    gate_set_now = set(gates)
    leaf = ""
    for candidate in sorted((REPO_ROOT / "test").glob("test_*.py")):
        rel = _rel_posix(candidate, REPO_ROOT)
        if rel in gate_set_now:
            continue
        if importers_of({candidate.stem}, REPO_ROOT) or mentions_of({candidate.stem}, REPO_ROOT):
            continue
        leaf = rel
        break
    check("a clean leaf exists in this repo", bool(leaf))

    # Escalations.
    check("empty diff escalates", verdict([]) is None)
    check("source change escalates", verdict(["src/kiro_crew/session.py"]) is None)
    check("conftest escalates", verdict(["test/conftest.py"]) is None)
    check("shared helper escalates", verdict(["test/source_corpus.py"]) is None)
    check("fixture tree escalates", verdict(["test/fixtures/npm-audit/x.json"]) is None)
    check("data corpus escalates", verdict(["test/windows-expected-failures.txt"]) is None)
    check("nested subtree escalates", verdict(["test/workflows/x.py"]) is None)
    check("imported test escalates", verdict(["test/test_chat_slack.py"]) is None)
    check(
        "dynamically-reached test escalates",
        verdict(["test/test_slack_golden_transcript.py"]) is None,
    )
    check(
        "mixed leaf + source escalates",
        verdict(["test/test_ask_question_roundtrip.py", "src/kiro_crew/agent.py"]) is None,
    )
    check("workflow change escalates", verdict([".github/workflows/ci.yml"]) is None)

    # The set-changing rule: only 'M' qualifies.
    for code in ("A", "D"):
        check(
            f"a {code!r} leaf escalates (the set of test files changed)",
            verdict([leaf], code) is None,
        )

    accepted = verdict([leaf])
    check("a MODIFIED leaf test file is accepted", accepted is not None)
    if isinstance(accepted, tuple):
        changed, gate_set = accepted
        check("accepted set carries the changed file", leaf in changed)
        check("the changed file is not duplicated into the gates", leaf not in gate_set)
        check("accepted set carries the corpus gates", bool(gate_set))
        check(
            "accepted set is a real reduction",
            len(changed) + len(gate_set) < len(list((REPO_ROOT / "test").glob("test_*.py"))) / 5,
        )
        try:
            validated_targets(changed + gate_set, REPO_ROOT)
        except SelectionUntrustworthy as exc:
            failures.append(f"validated_targets rejected the accepted set: {exc}")

    for name in failures:
        print(f"leaf_test_scope self-test FAILED: {name}", file=sys.stderr)
    if failures:
        return 1
    print("leaf_test_scope self-test: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
