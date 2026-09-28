#!/usr/bin/env python3
"""Check that a fix stayed inside the blast radius the conductor declared for it.

``verify_fix.py`` answers two questions about a finished fix: is the proof dead,
and is every legitimate operation still permitted. Neither of them asks the
question a security fix is most often rejected for by a human reviewer -- **did it
change more than it had to**. A fix for a cron seam that also adds a name to
``sandbox._AGENT_DENIED_ENV_KEYS`` strips that variable from every agent child, so
the operator loses their own env-configured governance ceiling. The proof still
stops reproducing, no golden path in the shipped corpus is a bash command that
notices, and the gate goes green on a change that made the product worse. That is
not a gap in the proof; it is a gap in SCOPE, and scope is knowable before the fix
is written rather than measured after.

So the conductor declares it. Before a fixer is dispatched it writes
``fix-contract.json`` into the worktree root::

    {
      "finding_ids": [16],
      "allowed_paths": ["src/kiro_crew/slack/gateway.py", "test/"],
      "forbidden_paths": ["src/kiro_crew/sandbox.py"],
      "max_changed_files": 3,
      "no_new_refusal_statement": "No command a maintainer runs today starts being refused."
    }

and this script asserts the fix against it::

    python3 check_fix_contract.py --worktree DIR --base origin/main [--contract PATH]

``allowed_paths`` and ``forbidden_paths`` are repository-relative PREFIXES, matched
as whole path segments: ``test/`` covers ``test/test_x.py`` and
``src/kiro_crew/sandbox.py`` covers only that file, never ``sandbox.py.orig``.
``forbidden_paths`` is checked as well as ``allowed_paths`` rather than left
implicit, because the two say different things to the fixer: one is the work it was
sent to do, and the other is the file whose edit was the last round's regression.
Naming it makes the refusal say so.

Exit codes, which are the interface::

    0   honoured    -- every changed path is allowed, none is forbidden, count <= max
    30  violated    -- at least one path is forbidden, outside the allowed set, or
                       the count is over the ceiling; every offending path is printed
    20  unreadable  -- the contract is absent, will not parse, or does not carry the
                       fields it must; or git could not answer what changed
    2   invalid input -- a bad argument, or a worktree that is not a git checkout

``30`` and ``20`` are the same codes ``verify_fix.py`` folds under, deliberately: a
violated contract is the actionable rejection (a named file to put back), and a
contract nobody could read is an unsettled check, never a pass. There is no exit
code for "no contract file" here -- a caller who names ``--contract`` is asserting
one exists, and an absent one is a broken dispatch. ``verify_fix.py`` owns the
"contract is optional" decision instead, by looking for the file before it runs
this at all.

**What is judged is the committed work plus every tracked change in the working
tree.** The committed range is ``<merge-base>..HEAD`` -- the fixer's own commits,
never main's -- and ``git diff HEAD`` adds what is staged AND what is merely saved.
Including the unstaged half is not strictness for its own sake: the proof of concept
and the behaviour rows execute the LIVE worktree, so an out-of-scope edit left
unstaged is code the verification RAN against and the commit does not carry. Judging
only the commit would report ``honoured`` for a fix whose verified behaviour is not
the fix being reviewed.

**An untracked ``*.py`` is judged too, and every other untracked path is not.** The
asymmetry is the failure each side prevents. A tracked fix that imports an UNTRACKED
module verifies green here and crashes on the merged checkout, which never carried the
module -- so a Python file git does not know about is part of the change whether the
fixer staged it or not. Everything else untracked is left alone because a test run
leaves ``.kiro/crew/``, ``.kirocrew.breadcrumb``, caches and logs behind: refusing
those would reject every fixer that ran the suite, which is the false rejection this
gate exists to prevent. Ignored paths are git's own answer (``--exclude-standard``),
and the untracked non-Python paths this gate skips are listed in ``ignored`` so the
skip is visible rather than silent.

**Two things are ignored, and both are noise this gate would otherwise report as a
violation.** ``fix-contract.json`` itself, because the conductor put it there. And an
UNTRACKED proof-of-concept copy -- ``test/test_s*_*.py`` or ``test/test_secaudit*.py``
that HEAD does not carry -- because ``verify_fix.py``'s own evaluator needs the audit
round's PoC file to resolve in the worktree, and that copy is scaffolding for the gate
rather than part of the fix. Two PoC-shaped paths are NOT ignored: one the fixer
committed, because once it is in the range it is the fix's own test; and one the
repository already tracks, because an edit to it is an edit to the project's test
suite and the shape of its name is not a licence.

``no_new_refusal_statement`` is carried through to the payload and printed, and it
is deliberately NOT machine-checked: it is the sentence the fixer must keep true
and the reviewer must read, and the check for it is the ``test`` rows of the golden
path corpus plus a human. A field that looked enforced but was not would be worse
than one that plainly is not.

The contract lives in the WORKTREE, which means a fixer could rewrite it -- so this
gate is not a fence against a dishonest fixer, and nothing here pretends
otherwise. It catches the failure that actually happens: a fixer that reaches one
file further than it was sent to, in good faith. What makes it reviewable is that
the payload ECHOES the contract it enforced, so the conductor reading the verdict
sees which allowed set produced it rather than assuming its own.

Reads one JSON file and runs ``git`` read-only. No network, and nothing out of the
contract is ever executed.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HONOURED = "honoured"
VIOLATED = "violated"
UNREADABLE = "unreadable"

EXIT_CODES = {HONOURED: 0, VIOLATED: 30, UNREADABLE: 20}
EXIT_INVALID = 2

#: The file the conductor writes into the worktree root, and the name
#: ``verify_fix.py`` looks for when it decides whether a contract applies at all.
CONTRACT_FILENAME = "fix-contract.json"

#: Ignored unconditionally: the conductor's own dispatch artefact, not the fix.
ALWAYS_IGNORED = (CONTRACT_FILENAME,)

#: Untracked paths a Kiro Crew test run leaves in the tree, exempt BY NAME rather than
#: by suffix. Kept as a short list so a reviewer can read it: everything else untracked is
#: part of the change, because a fix that depends on a file git does not know about
#: crashes the merged checkout whatever the file's extension is.
UNTRACKED_RESIDUE = (".kiro", ".kirocrew.breadcrumb")

#: A proof-of-concept copy, ignored only when it is UNTRACKED. These are the shapes
#: the audit rounds write (``test_s10_...`` for a surface, ``test_secaudit_...`` for a
#: round), and ``verify_fix.py``'s evaluator needs the file present in the worktree to
#: resolve the ledger's PoC nodeid -- so the untracked copy it drops there is
#: scaffolding. A PoC-shaped path that the repository already TRACKS is a different
#: thing entirely: an edit to it is an edit to the project's own test, and exempting it
#: because it was not committed in this range would let an out-of-scope change to
#: ``test/test_s10_*.py`` report honoured.
POC_PATTERNS = ("test/test_s*_*.py", "test/test_secaudit*.py")

#: The revision a fix is assumed to have branched from when neither the caller nor
#: the contract names one.
DEFAULT_BASE = "origin/main"

#: How long git gets to answer. Bounded because a hung child would hold the
#: conductor open with no verdict, which is worse than a named unreadable.
GIT_TIMEOUT = 60


def is_git_worktree(directory: Path) -> bool:
    """Is this a git checkout -- a clone (``.git/`` directory) or a linked worktree?

    The same screen ``verify_fix.py`` and ``verify_finding.py`` apply. A linked
    worktree carries a ``.git`` FILE holding a gitdir pointer, so an ``is_dir()``
    test alone would reject exactly the layout a fixer is dispatched into.
    """
    if not directory.is_dir():
        return False
    marker = directory / ".git"
    return marker.is_dir() or marker.is_file()


def load_contract(path: Path) -> tuple[dict[str, Any], list[str]]:
    """The contract, checked field by field. ``(contract, problems)``.

    Every problem is collected rather than raised at the first one, so a conductor
    that wrote two fields wrong is told both in one run. A contract with any problem
    is not partially applied: the caller reports ``unreadable`` and checks nothing,
    because a gate running on half a declaration is a gate whose allowed set nobody
    chose.
    """
    problems: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        reason = exc.strerror or exc.__class__.__name__
        return {}, [f"the fix contract is not readable: {path} ({reason})"]
    try:
        raw = json.loads(text)
    except ValueError as exc:
        return {}, [f"the fix contract does not parse as JSON: {path}: {exc}"]
    if not isinstance(raw, dict):
        return {}, [f"the fix contract must be a JSON object: {path}"]

    finding_ids = raw.get("finding_ids")
    # ``bool`` is excluded explicitly because it subclasses ``int``: ``[true]``
    # would otherwise be accepted and reported as finding 1.
    if (
        not isinstance(finding_ids, list)
        or not finding_ids
        or any(isinstance(item, bool) or not isinstance(item, int) for item in finding_ids)
    ):
        problems.append("finding_ids must be a non-empty list of integers")
        finding_ids = []

    allowed = _prefix_list(raw, "allowed_paths", problems, required=True)
    forbidden = _prefix_list(raw, "forbidden_paths", problems, required=False)

    max_files = raw.get("max_changed_files")
    if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
        problems.append("max_changed_files must be a positive integer")
        max_files = 0

    statement = raw.get("no_new_refusal_statement", "")
    if not isinstance(statement, str):
        problems.append("no_new_refusal_statement must be a string")
        statement = ""

    overlap = sorted(set(allowed) & set(forbidden))
    if overlap:
        # A prefix in both lists has no honest reading: the fix is sent to a file
        # it may not touch. Reported rather than resolved by precedence, because
        # either resolution would be this script silently choosing the scope.
        problems.append(
            "these prefixes are both allowed and forbidden, so the contract states"
            f" no scope: {', '.join(overlap)}"
        )

    contract = {
        "finding_ids": finding_ids,
        "allowed_paths": allowed,
        "forbidden_paths": forbidden,
        "max_changed_files": max_files,
        "no_new_refusal_statement": statement,
    }
    return contract, problems


def _prefix_list(
    raw: dict[str, Any], field: str, problems: list[str], *, required: bool
) -> list[str]:
    """One path-prefix list, normalised to POSIX separators and no leading ``./``."""
    value = raw.get(field, None)
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        problems.append(f"{field} must be a list of non-blank path prefixes")
        return []
    if required and not value:
        problems.append(f"{field} must name at least one path prefix")
        return []
    return [normalise_path(item) for item in value]


def normalise_path(path: str) -> str:
    """A repository-relative path in the one spelling comparisons are made in.

    Backslashes become ``/`` so a contract written on Windows matches the paths git
    reports, and a leading ``./`` is dropped. Trailing slashes are kept off, because
    :func:`under_prefix` re-adds the separator itself.
    """
    text = path.strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def under_prefix(path: str, prefix: str) -> bool:
    """Is *path* the prefix itself, or beneath it?

    Whole-segment matching, never a string ``startswith``: a forbidden
    ``src/kiro_crew/sandbox.py`` must not swallow ``src/kiro_crew/sandbox_pod.py``,
    and an allowed ``test`` must not permit ``testdata/secret``.
    """
    if not prefix:
        return False
    return path == prefix or path.startswith(prefix + "/")


def matches_any(path: str, prefixes: list[str]) -> bool:
    return any(under_prefix(path, prefix) for prefix in prefixes)


def is_poc_shaped(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in POC_PATTERNS)


def run_git(worktree: Path, args: list[str]) -> tuple[bool, str, str]:
    """One read-only git call. ``(ok, stdout, problem)``.

    A missing git, a non-zero status and a deadline all come back as one shape, so
    the caller has a single ``unreadable`` branch instead of three. ``-C`` is used
    rather than ``cwd`` so the invocation names the worktree it is asking about in
    its own argv, which is what a reviewer reads off a failure.
    """
    argv = ["git", "-C", str(worktree), *args]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
        )
    except OSError as exc:
        return False, "", f"git could not be run ({exc})"
    except subprocess.TimeoutExpired:
        return False, "", f"git did not answer within {GIT_TIMEOUT}s: {' '.join(args)}"
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        reason = detail[-1] if detail else f"exit {completed.returncode}"
        return False, "", f"git {' '.join(args)} failed: {reason}"
    return True, completed.stdout, ""


def changed_paths(worktree: Path, base: str) -> tuple[list[str], list[str], str]:
    """What the fix committed and what its working tree carries. ``(committed, working, problem)``.

    Two sets rather than one, because the ignore rule needs to tell them apart: a
    PoC copy that is only in the working tree is scaffolding, and the same file
    committed is the fix's own test.

    ``git diff --name-only HEAD`` is one call for both halves of the working tree,
    staged and unstaged. Asking ``--cached`` alone would leave out exactly the edit
    that is most dangerous -- one the proof ran against and the commit does not
    carry.

    ``--no-renames`` is load-bearing on both calls. With rename detection on, moving a
    file reports only the DESTINATION, so a forbidden file renamed under an allowed
    prefix would be judged by its new name alone and the contract would report honoured.
    Off, the same move is a delete plus an add, and the forbidden source path is in the
    judged set where it belongs.
    """
    ok, merge_base, problem = run_git(worktree, ["merge-base", base, "HEAD"])
    if not ok:
        return [], [], problem
    merge_base = merge_base.strip()
    if not merge_base:
        return [], [], f"git named no merge base for {base} and HEAD"
    ok, committed_text, problem = run_git(
        worktree, ["diff", "--name-only", "--no-renames", f"{merge_base}..HEAD"]
    )
    if not ok:
        return [], [], problem
    ok, working_text, problem = run_git(worktree, ["diff", "--name-only", "--no-renames", "HEAD"])
    if not ok:
        return [], [], problem
    committed = [normalise_path(line) for line in committed_text.splitlines() if line.strip()]
    working = [normalise_path(line) for line in working_text.splitlines() if line.strip()]
    return committed, working, ""


def partition(
    committed: list[str], working: list[str], untracked: list[str], tracked: set[str]
) -> tuple[list[str], list[str]]:
    """``(considered, ignored)`` -- the paths this gate judges, and the noise.

    See the module docstring for each exemption: the conductor's own contract file
    always, a PoC-shaped path the repository does not track, and an untracked path named
    in :data:`UNTRACKED_RESIDUE`. ``tracked`` is what HEAD carries, which is the test that
    tells an untracked scaffolding copy from an edit to the project's own test file.
    """
    committed_set = set(committed)
    considered: list[str] = []
    ignored: list[str] = []
    untracked_set = set(untracked)
    for path in sorted(committed_set | set(working) | untracked_set):
        if path in untracked_set and matches_any(path, list(UNTRACKED_RESIDUE)):
            ignored.append(path)
            continue
        if path in ALWAYS_IGNORED:
            ignored.append(path)
        elif is_poc_shaped(path) and path not in committed_set and path not in tracked:
            ignored.append(path)
        else:
            considered.append(path)
    return considered, ignored


def untracked_paths(worktree: Path) -> tuple[list[str], str]:
    """Untracked, non-ignored paths. ``(paths, problem)``.

    ``--exclude-standard`` means the ignore decision is git's, not a second list here
    that could disagree with ``.gitignore``. The caller splits these by suffix: see the
    module docstring for why a ``*.py`` is judged and a scratch data home is not.
    """
    ok, text, problem = run_git(worktree, ["ls-files", "--others", "--exclude-standard"])
    if not ok:
        return [], problem
    return [normalise_path(line) for line in text.splitlines() if line.strip()], ""


def tracked_paths(worktree: Path) -> tuple[set[str], str]:
    """Every path HEAD carries. ``(paths, problem)``.

    One call rather than a per-path query: the set is read once and asked about a
    handful of times, and a query per candidate would be a git process per PoC-shaped
    file. An unreadable answer is a problem the caller reports, never an empty set --
    an empty one would silently exempt every PoC-shaped path again.
    """
    ok, text, problem = run_git(worktree, ["ls-tree", "-r", "--name-only", "HEAD"])
    if not ok:
        return set(), problem
    return {normalise_path(line) for line in text.splitlines() if line.strip()}, ""


def judge(considered: list[str], contract: dict[str, Any]) -> dict[str, Any]:
    """The three ways a contract is violated, all reported together.

    One run tells the fixer everything that is wrong: a partial report costs a
    round per fix, and every round of a fixer lane is a dispatch a human approved.
    """
    forbidden = [p for p in considered if matches_any(p, contract["forbidden_paths"])]
    outside = [p for p in considered if not matches_any(p, contract["allowed_paths"])]
    over = len(considered) > int(contract["max_changed_files"])
    return {
        "forbidden": forbidden,
        "outside_allowed": outside,
        "count": (
            {"changed": len(considered), "max": int(contract["max_changed_files"])}
            if over
            else None
        ),
    }


def emit(payload: dict[str, Any]) -> int:
    """The one printer: JSON on stdout, the readable lines on stderr, the exit code."""
    print(json.dumps(payload, sort_keys=True))
    for note in payload["problems"]:
        print(f"unreadable: {note}", file=sys.stderr)
    violations = payload["violations"]
    for path in violations["forbidden"]:
        print(f"forbidden path changed: {path}", file=sys.stderr)
    for path in violations["outside_allowed"]:
        print(f"path outside the allowed set: {path}", file=sys.stderr)
    if violations["count"] is not None:
        print(
            "changed file count over the ceiling:"
            f" {violations['count']['changed']} > {violations['count']['max']}",
            file=sys.stderr,
        )
    statement = payload["contract"].get("no_new_refusal_statement")
    if statement:
        print(f"the fixer must keep this true (not machine-checked): {statement}", file=sys.stderr)
    return EXIT_CODES[payload["verdict"]]


def unreadable_payload(
    worktree: Path, base: str, contract_path: Path, problems: list[str]
) -> dict[str, Any]:
    """One payload shape on every path out, including the ones that checked nothing."""
    return {
        "verdict": UNREADABLE,
        "worktree": str(worktree),
        "base": base,
        "contract_path": str(contract_path),
        "contract": {},
        "changed": [],
        "ignored": [],
        "violations": {"forbidden": [], "outside_allowed": [], "count": None},
        "problems": problems,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check a fix against the conductor's declared blast radius"
    )
    parser.add_argument("--worktree", required=True)
    parser.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"the revision the fix branched from (default: {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--contract",
        default=None,
        help=f"path to the contract (default: <worktree>/{CONTRACT_FILENAME})",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    worktree = Path(args.worktree)
    if not worktree.is_dir():
        print(f"--worktree is not a directory: {worktree}", file=sys.stderr)
        return EXIT_INVALID
    if not is_git_worktree(worktree):
        print(
            f"--worktree is not a git checkout: {worktree};"
            " what changed is a question only a checkout can answer",
            file=sys.stderr,
        )
        return EXIT_INVALID
    contract_path = Path(args.contract) if args.contract else worktree / CONTRACT_FILENAME

    contract, problems = load_contract(contract_path)
    if problems:
        return emit(unreadable_payload(worktree, args.base, contract_path, problems))

    # The base is the CALLER's, and the contract has no say in it. A ``"base"`` key
    # in the file would be read from the tree under review, and naming ``HEAD`` there
    # empties the judged diff and makes every scope check trivially honoured.
    base = args.base

    committed, working, problem = changed_paths(worktree, base)
    if problem:
        return emit(unreadable_payload(worktree, base, contract_path, [problem]))

    tracked, problem = tracked_paths(worktree)
    if problem:
        return emit(unreadable_payload(worktree, base, contract_path, [problem]))

    untracked, problem = untracked_paths(worktree)
    if problem:
        return emit(unreadable_payload(worktree, base, contract_path, [problem]))

    considered, ignored = partition(committed, working, untracked, tracked)
    violations = judge(considered, contract)
    violated = bool(violations["forbidden"] or violations["outside_allowed"] or violations["count"])
    return emit(
        {
            "verdict": VIOLATED if violated else HONOURED,
            "worktree": str(worktree),
            "base": base,
            "contract_path": str(contract_path),
            "contract": contract,
            "changed": considered,
            "ignored": ignored,
            "violations": violations,
            "problems": [],
        }
    )


if __name__ == "__main__":
    sys.exit(main())
