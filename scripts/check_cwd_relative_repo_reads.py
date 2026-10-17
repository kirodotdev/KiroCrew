#!/usr/bin/env python3
"""check_cwd_relative_repo_reads.py -- no test reaches the repository through the CWD.

## The rule this enforces

``docs/system-specs/common/testing-conventions.md`` says the process working
directory is per-PROCESS and, under pytest, is the repository root. A structural
test that asserts something about the repository's own source text therefore has
to name a file, and naming it as a bare relative path -- ``Path("src/kiro_crew/x.py")``
-- binds the test to whatever directory the process happens to start in.

That binding holds only while pytest is launched from the root. Launched from
anywhere else (a subdirectory, an editor's runner, a wrapper that changes
directory first) the same literal resolves somewhere that does not exist and the
read raises ``FileNotFoundError`` before a single assertion runs. The test reports
a failure that has nothing to do with the behaviour it pins, which costs a CI
round and teaches readers to re-run past it.

The remedy is to root the path at something the test already knows:

* Best -- the MODULE UNDER TEST, imported and asked where it lives::

      from kiro_crew.dashboard import ws
      source = Path(ws.__file__).read_text(encoding="utf-8")

  A module that moves or is renamed then fails at import, which no stale literal
  can survive, and the path cannot name a different file than the one the rest of
  the test exercises.

* Acceptable -- the REPOSITORY, resolved from the test file::

      _REPO = Path(__file__).resolve().parents[1]
      source = (_REPO / "src/kiro_crew/dashboard/ws.py").read_text(encoding="utf-8")

  Independent of the working directory, but it still spells the layout by hand, so
  moving the module leaves a literal that names nothing.

## What counts as a violation

A filesystem ACCESS -- a call in ``ACCESS_VERBS`` -- whose path expression bottoms
out at a bare relative string literal whose first segment names one of the
repository's own top-level entries (``src``, ``scripts``, ``.github``, ``setup.cfg``
and the rest, read from the repository itself rather than from a list kept here).

Those two halves are both necessary. A bare relative ``Path`` that is only ever
COMPARED, or joined onto a resolved root, is not a defect: the repository already
keeps allowlists of relative paths and checks them against ``path.relative_to(root)``,
and flagging those would make the gate wrong in the places it is most tempting to
write a relative literal. Equally, a relative read of something that is NOT a
repository entry -- ``Path("out.json")`` inside a test that has changed directory
into its own ``tmp_path`` -- is the working directory being used on purpose.

## Exemptions

A file containing ANY ``chdir`` call is skipped whole. Deliberately coarse: such a
file has taken the working directory into its own hands, and a relative path there
names the tree the test itself built rather than the checkout. This costs little --
54 of 2973 files -- and it removes the only false-positive class.

A ``# cwd-ok: <reason>`` comment on the offending line, or on the line above it,
exempts that one access. The reason is required and is not parsed; it is there so
the next reader learns why.

Whole-tree, not diff-scoped: the backlog is zero, so there is nothing to charge to
whoever pushes next.
"""

from __future__ import annotations

import argparse
import ast
import configparser
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: A path object is only working-directory-dependent once something reaches the
#: filesystem through it. Writes are included: a relative write lands IN the
#: checkout, which the conventions call out separately for the same reason.
ACCESS_VERBS = frozenset(
    {
        "exists",
        "glob",
        "is_dir",
        "is_file",
        "iterdir",
        "lstat",
        "mkdir",
        "open",
        "read_bytes",
        "read_text",
        "rglob",
        "samefile",
        "stat",
        "touch",
        "unlink",
        "walk",
        "write_bytes",
        "write_text",
    }
)

#: Recognised on the offending line or the one above it.
PRAGMA = "# cwd-ok:"

_EXCLUDED_PARTS = frozenset({"_vendor", "node_modules", "__pycache__", ".git"})


def repo_top_level(root: Path) -> set[str]:
    """The repository's own top-level entries, read from the repository.

    From git when it answers, so an untracked scratch directory in a working
    checkout cannot widen the rule. From the filesystem otherwise, which is what a
    checkout without ``.git`` (a source archive) offers.
    """
    try:
        completed = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return {entry.name for entry in root.iterdir()} - _EXCLUDED_PARTS
    entries = {line.strip() for line in completed.stdout.splitlines() if line.strip()}
    return entries - _EXCLUDED_PARTS


def parse_testpaths(cfg_text: str) -> list[str]:
    parser = configparser.ConfigParser()
    parser.read_string(cfg_text)
    raw = parser.get("tool:pytest", "testpaths", fallback="")
    return [part for part in raw.split() if part]


def _resolve_roots(cfg_text: str) -> list[str]:
    roots = parse_testpaths(cfg_text)
    if not roots:
        raise SystemExit(
            "setup.cfg has no [tool:pytest] testpaths; the gate cannot tell which "
            "trees pytest collects, so it fails closed rather than scanning nothing"
        )
    return roots


def is_test_file(path: Path) -> bool:
    """What pytest collects: anything under a ``test``/``tests`` directory, or named so.

    Helper modules under ``test/`` count. They are imported by the tests that read
    the repository and carry exactly the same relative literals.
    """
    if path.suffix != ".py":
        return False
    parts = set(path.parts)
    if parts & _EXCLUDED_PARTS:
        return False
    if parts & {"test", "tests"}:
        return True
    name = path.name
    return name.startswith("test_") or name.endswith("_test.py")


def collect_files(root: Path, testpaths: list[str]) -> list[Path]:
    found: set[Path] = set()
    for rel in testpaths:
        base = root / rel
        if not base.exists():
            continue
        for candidate in base.rglob("*.py"):
            if is_test_file(candidate.relative_to(root)):
                found.add(candidate)
    return sorted(found)


def _string(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _path_literal(node: ast.AST) -> str | None:
    """``Path("x")`` or ``pathlib.Path("x")`` -> ``"x"``."""
    if not isinstance(node, ast.Call) or not node.args:
        return None
    func = node.func
    if isinstance(func, ast.Name):
        name = func.id
    elif isinstance(func, ast.Attribute):
        name = func.attr
    else:
        return None
    if name != "Path":
        return None
    return _string(node.args[0])


def _rooted_literal(node: ast.AST, bound: dict[str, str]) -> str | None:
    """The bare relative literal a path expression bottoms out at, if any.

    Walks back through ``/`` joins and non-accessing calls, so
    ``Path("src/x") / "y"`` and ``Path("src/x").resolve()`` are still seen as rooted
    at the literal. Returns None as soon as the chain bottoms out at anything else,
    which is what exempts ``_REPO / "src/x"`` and ``Path(mod.__file__)``.
    """
    for _ in range(64):
        literal = _path_literal(node)
        if literal is not None:
            return literal
        if isinstance(node, ast.Name):
            return bound.get(node.id)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            node = node.left
            continue
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node = node.func.value
            continue
        if isinstance(node, ast.Attribute):
            node = node.value
            continue
        return None
    return None


def names_bound_to_relative_paths(tree: ast.AST) -> dict[str, str]:
    """``P = Path("src/x")`` -> ``{"P": "src/x"}``, so a later ``P.read_text()`` is seen.

    File-wide rather than per-scope: a module-level constant accessed inside a
    method is the shape this pattern actually takes.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            literal = _path_literal(node.value)
            if isinstance(target, ast.Name) and literal is not None:
                bound[target.id] = literal
    return bound


def is_repo_relative(literal: str, top_level: set[str]) -> bool:
    if not literal or literal.startswith(("/", "~")):
        return False
    if len(literal) > 1 and literal[1] == ":":  # a Windows drive
        return False
    first = literal.replace("\\", "/").split("/")[0]
    return first in top_level


def changes_directory(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if attr == "chdir":
                return True
    return False


def _exempt_lines(source: str) -> set[int]:
    """Line numbers a ``# cwd-ok:`` comment covers: its own, and the one after it."""
    exempt: set[int] = set()
    for number, line in enumerate(source.splitlines(), start=1):
        if PRAGMA in line:
            exempt.add(number)
            exempt.add(number + 1)
    return exempt


def find_violations(source: str, top_level: set[str]) -> list[tuple[int, str, str]]:
    """``(line, literal, verb)`` for every working-directory-rooted access."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if changes_directory(tree):
        return []
    bound = names_bound_to_relative_paths(tree)
    exempt = _exempt_lines(source)
    violations: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or node.lineno in exempt:
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "open" and node.args:
            literal = _string(node.args[0]) or _rooted_literal(node.args[0], bound)
            verb = "open()"
        elif isinstance(func, ast.Attribute) and func.attr in ACCESS_VERBS:
            literal = _rooted_literal(func.value, bound)
            verb = f".{func.attr}()"
        else:
            continue
        if literal and is_repo_relative(literal, top_level):
            violations.append((node.lineno, literal, verb))
    return violations


def scan(root: Path, files: list[Path], top_level: set[str]) -> list[str]:
    reported: list[str] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line, literal, verb in find_violations(source, top_level):
            reported.append(f"{path.relative_to(root)}:{line}: {verb} on relative {literal!r}")
    return reported


_PROBES: tuple[tuple[str, str, bool], ...] = (
    (
        "a bare relative read",
        'from pathlib import Path\nPath("src/kiro_crew/x.py").read_text()\n',
        True,
    ),
    (
        "the same read through the module alias",
        'import pathlib\npathlib.Path("src/kiro_crew/x.py").read_text(encoding="utf-8")\n',
        True,
    ),
    ("a builtin open", 'open("src/kiro_crew/x.py").read()\n', True),
    (
        "a relative path bound to a name first",
        'from pathlib import Path\nP = Path("src/kiro_crew/x.py")\nP.read_text()\n',
        True,
    ),
    (
        "a non-reading verb in the access set",
        'from pathlib import Path\nassert Path("scripts/x.py").exists()\n',
        True,
    ),
    (
        "a relative write into the checkout",
        'from pathlib import Path\nPath("src/kiro_crew/x.py").write_text("")\n',
        True,
    ),
    (
        "a join onto the literal",
        'from pathlib import Path\n(Path("src") / "kiro_crew" / "x.py").read_text()\n',
        True,
    ),
    (
        "a relative literal only compared, never read",
        'from pathlib import Path\nALLOWED = {Path("src/kiro_crew/x.py")}\n'
        'assert Path("a/b") in ALLOWED\n',
        False,
    ),
    (
        "the repository resolved from the test file",
        "from pathlib import Path\n_REPO = Path(__file__).resolve().parents[1]\n"
        '(_REPO / "src/kiro_crew/x.py").read_text()\n',
        False,
    ),
    (
        "the module under test asked where it lives",
        "from pathlib import Path\nfrom kiro_crew.dashboard import ws\n"
        "Path(ws.__file__).read_text()\n",
        False,
    ),
    (
        "a relative name that is not a repository entry",
        'from pathlib import Path\nPath("out.json").read_text()\n',
        False,
    ),
    (
        "an absolute path",
        'from pathlib import Path\nPath("/srv/src/kiro_crew/x.py").read_text()\n',
        False,
    ),
    (
        "a file that manages the working directory itself",
        "import os\nfrom pathlib import Path\n"
        'os.chdir("/somewhere")\nPath("src/kiro_crew/x.py").read_text()\n',
        False,
    ),
    (
        "an access carrying the pragma",
        "from pathlib import Path\n"
        '# cwd-ok: reads the fixture tree this test just built\nPath("src/x.py").read_text()\n',
        False,
    ),
)

_SELFTEST_TOP_LEVEL = {"src", "scripts", "setup.cfg", ".github"}


def selftest() -> int:
    """One probe per rule family, so a rule that stops matching fails here.

    Both directions on purpose: a gate that only proves it catches things can be
    weakened into catching everything, and a blocking gate that fires on a legitimate
    relative read is worse than no gate.
    """
    failures: list[str] = []
    for label, source, should_flag in _PROBES:
        flagged = bool(find_violations(source, _SELFTEST_TOP_LEVEL))
        if flagged != should_flag:
            want = "flagged" if should_flag else "clean"
            failures.append(f"{label}: expected {want}, got {'flagged' if flagged else 'clean'}")
    for failure in failures:
        print(f"selftest: {failure}", file=sys.stderr)
    if failures:
        print(f"selftest: {len(failures)} of {len(_PROBES)} probes wrong", file=sys.stderr)
        return 1
    print(f"selftest: {len(_PROBES)} probes correct")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse a test that reaches the repo via the CWD")
    parser.add_argument(
        "--test",
        action="store_true",
        help="run the gate's own probes instead of scanning the tree",
    )
    args = parser.parse_args(argv)
    if args.test:
        return selftest()

    top_level = repo_top_level(ROOT)
    testpaths = _resolve_roots((ROOT / "setup.cfg").read_text(encoding="utf-8"))
    files = collect_files(ROOT, testpaths)
    if not files:
        raise SystemExit(f"no test files found under {testpaths}; the gate would pass vacuously")
    violations = scan(ROOT, files, top_level)
    if violations:
        print(
            "A test reaches the repository through the process working directory.\n"
            "Root the path at the module under test instead:\n"
            "    from kiro_crew.<pkg> import <mod>\n"
            '    source = Path(<mod>.__file__).read_text(encoding="utf-8")\n'
            "or, when no module owns the file, at the repository resolved from the\n"
            "test file: Path(__file__).resolve().parents[1] / <relative path>.\n"
            "See docs/system-specs/common/testing-conventions.md on the working directory.\n",
            file=sys.stderr,
        )
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(f"\n{len(violations)} violation(s) over {len(files)} test files.", file=sys.stderr)
        return 1
    print(f"clean: {len(files)} test files, none reaching the repository through the CWD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
