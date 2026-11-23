#!/usr/bin/env python3
"""Per-test runtime map of the backend suite, and the SHADOW selector that reads it.

SHADOW ONLY: no CI lane reads this script's output. `test-map.yml` builds the
map nightly on main; the selector prints what it WOULD run so its safety can be
measured (`scripts/ci_test_map_shadow.py`) before anything acts on it. Every
test still runs on every pull request.

Why a runtime map
-----------------
A static import graph selects the whole suite for nearly every source change,
because lazy imports tie most of `src/` into one cycle. Recording, per test,
which files it really executed (`scripts/ci_test_file_map.py`) links a change
to the test files that touched it.

    build   merge the plugin's `part-*.json` files into the map
    bundle  add the co-change history and the guard tier to a map and write
            the gzip selection file the scorer reads (below)
    check   describe a bundle file in one line (commit, build time, size, rows
            per table) and exit 1 when it lacks a table: `test-map.yml`
            publishes a bundle only after this passes
    select  given a diff (`--base B --head H`, or `--name-status FILE`) and a
            map or bundle, print the test files it would run, or fall back to
            the full suite and say why
    guard-tier
            print the guard tier, one test file per line, so the tier runs as
            one pytest invocation:
                python -m pytest $(python3 scripts/ci_test_map.py guard-tier)
            Only files git tracks, in code point order, as POSIX paths: the
            same lines on every platform, whatever else a working tree holds.
    core    given a map, rewrite `scripts/ci_test_map_core.txt`: the `src/`
            files linked to more than half of its test files, whose functions
            the recorder then maps one by one

The map
-------
    {"schema": 1, "commit": SHA, "tests": {test file: node ids run},
     "files": {executed file: [index into sorted(tests)]},
     "functions": {listed file: {qualname: [first line, last line,
                                            [index into sorted(tests)]]}}}

Paths are repository-relative POSIX paths. Files are the unit CI shards and
runs, so a node id counts for its test FILE. `functions` holds the files in
`scripts/ci_test_map_core.txt` when the map was recorded, less every function
the recorder saw run during an import (its parts' `<import time>` list).

The bundle (gzip JSON)
----------------------
The map, with the two tables `scripts/ci_test_selection.py` reads beside it:

    "cochange": {src file: [test file, ...]},  # changed together in one of the
                                               # last N first-parent commits
    "guard": [test file, ...]                  # leaf_test_scope.corpus_gates

two tables the verdict's `windows` block is chosen from
(`scripts/ci_test_os_tables.py`, `scripts/ci_test_selection_windows.py`):

    "os_sensitive": {src file: "W1" | "W2"},
    "windows_targeting": [test file, ...]

and when the `bundle` command wrote the file, which says how old those tables are:

    "built_at": ISO 8601 time, UTC

A table that cannot be built, or that comes out empty, is left out of the file,
never written empty, so a reader can tell "no data" from "no links". A shallow
clone cannot build `cochange` unless it holds `--commits` commits and the one
before them: a part of the window would read as the whole of it.

What the selector falls back on (prints nothing, exit 3)
--------------------------------------------------------
* a file added, deleted, renamed or copied: the set of files changes, which a
  map of the old set cannot describe;
* any `conftest.py`: it reaches every test below it;
* a non-`.py` file anywhere, or a `.py` file outside `src/` and `test/`
  (workflows, scripts, config, docs): tests read those as data, and the plugin
  records only code that ran;
* a `.py` file the map never saw executed in a test, unless it is itself a
  test file;
* an empty always-run guard tier (`leaf_test_scope.corpus_gates`): a dead scan
  and a working one look the same, so an empty one forfeits the reduction.

Otherwise the selection is: every changed test file, every test file the map
links to a changed file, and the guard tier (tests that read the test tree as
data), which is always added.

Function level
--------------
For a modified file with a `functions` table, and a diff given as `--base` and
`--head`, the links are the test files that ran the functions the diff touches:
each line a hunk removes is placed in the base file's functions and each line
it adds in the head file's, both found with `ast` and named as `co_qualname`
names them. A function's decorators, signature and docstring are read without
calling it (a tool registry, `inspect`), so they count as code around it, and
so does all of a one-line `def` and of a def whose decorator caches its result
(`cache`, `lru_cache`, `cached_property`), since later callers read that result
without running the body. The file keeps its file-level links when a changed
line is outside every function body (module level, a class body, those lines),
when the map has no entry for a touched function (renamed, new, never run, or
run during an import), or when either side does not parse.

A test that reads what a function kept from an earlier test without running
it is not linked to it: a memo the function fills itself, a helper only a
cached function calls, a session fixture's value, a module imported before the
recorder starts. It is the same
first-run bias the recorder has for module bodies, and the shadow report
(`ci_test_map_shadow.py`) measures it with the same function-level links.

Exit codes: 0 selection printed, 3 full suite (reason on stderr), 2 usage.
"""

from __future__ import annotations

import argparse
import ast
import gzip
import importlib
import json
import subprocess
import sys
import zlib
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

# The sibling under the same package this module was loaded from: `scripts.` in
# the tests, top-level when run as a script, so no path edit and no second copy.
_os_tables_module = importlib.import_module(
    f"{__package__ + '.' if __package__ else ''}ci_test_os_tables"
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1
FULL = 3
CO_CHANGE_COMMITS = 2000
GIT_LOG_TIMEOUT_SECS = 600.0
BUNDLE_TABLES = ("cochange", "guard", "os_sensitive", "windows_targeting")
NO_GUARD = "corpus_gates found no guard test"

# Where pytest collects (setup.cfg `testpaths`). A changed test file outside
# these is not a test pytest would run, so it falls back like any other file.
TESTPATHS = ("test/", "src/kiro_crew/apps/builtins/")

# Shared with the recorder (`scripts/ci_test_file_map.py`), which names the same two.
CORE_LIST = "scripts/ci_test_map_core.txt"
IMPORT_TIME = "<import time>"
CORE_REACH = 0.5
# A def with one of these decorators runs once; later callers read the kept result.
CACHES = frozenset({"cache", "lru_cache", "cached_property"})


class Selection(NamedTuple):
    """The verdict for one diff: test files, or `full=True` with the reasons."""

    full: bool
    tests: list[str]
    reasons: list[str]


def merge_parts(parts: Iterable[Mapping[str, Any]], commit: str) -> dict[str, Any]:
    """Fold plugin parts (`{test file: {"nodes": N, "files": [...], "functions": [...]}}`)
    into the map. One qualname run from two places (a property's getter and
    setter) keeps the span of both; one any part ran during an import is left out."""
    nodes: dict[str, int] = {}
    links: dict[str, set[str]] = {}
    functions: dict[str, dict[str, tuple[int, int, set[str]]]] = {}
    imported: set[tuple[str, str]] = set()
    for part in parts:
        imported.update((path, name) for path, name in part.get(IMPORT_TIME, ()))
        for test_file, entry in part.items():
            if test_file == IMPORT_TIME:
                continue
            nodes[test_file] = nodes.get(test_file, 0) + int(entry["nodes"])
            for executed in entry["files"]:
                links.setdefault(executed, set()).add(test_file)
            for path, qualname, first, last in entry.get("functions", ()):
                table = functions.setdefault(path, {})
                low, high, ran = table.get(qualname, (first, last, set()))
                table[qualname] = (min(low, first), max(high, last), ran | {test_file})
    tests = sorted(nodes)
    index = {name: i for i, name in enumerate(tests)}
    return {
        "schema": SCHEMA,
        "commit": commit,
        "tests": {name: nodes[name] for name in tests},
        "files": {path: sorted(index[t] for t in linked) for path, linked in sorted(links.items())},
        "functions": {
            path: kept
            for path, table in sorted(functions.items())
            if (
                kept := {
                    name: [first, last, sorted(index[t] for t in ran)]
                    for name, (first, last, ran) in sorted(table.items())
                    if (path, name) not in imported
                }
            )
        },
    }


def read_parts(directory: Path) -> list[dict[str, Any]]:
    paths = sorted(directory.glob("**/part-*.json"))
    if not paths:
        raise ValueError(f"{directory}: no part-*.json files")
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def read_json(path: Path) -> Any:
    """The JSON in a plain or gzip file; damaged gzip data is a `ValueError`."""
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (EOFError, zlib.error) as exc:
            raise ValueError(f"{path}: damaged gzip data: {exc}") from exc
    return json.loads(raw.decode("utf-8"))


def load_map(path: Path) -> dict[str, Any]:
    """Read and check a map file, plain or gzip; a wrong schema is a usage error."""
    return _check_map(read_json(path), path)


def _check_map(doc: Any, path: Path) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a schema-{SCHEMA} test map")
    if not isinstance(doc.get("tests"), dict) or not isinstance(doc.get("files"), dict):
        raise ValueError(f"{path}: map has no tests/files tables")
    return doc


def parse_name_status(text: str) -> list[tuple[str, list[str]]]:
    """`git diff --name-status -M` lines -> `[(status letter, [path, ...])]`."""
    rows: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        status, *paths = line.split("\t")
        rows.append((status[:1], paths))
    return rows


def _git(root: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    ).stdout


def git_name_status(base: str, head: str, root: Path) -> str:
    """Name-status of `base..head` with rename detection, as git prints it."""
    return _git(root, "diff", "--name-status", "-M", f"{base}..{head}")


def parse_hunks(diff: str) -> list[tuple[range, range]]:
    """The base lines each hunk of a `git diff -U0` removes and the head lines it adds."""
    hunks = []
    for line in diff.splitlines():
        if line.startswith("@@ "):
            removed, added = (_lines(side[1:]) for side in line.split(" ")[1:3])
            hunks.append((removed, added))
    return hunks


def _lines(side: str) -> range:
    start, _, count = side.partition(",")
    return range(int(start), int(start) + int(count or "1"))


def function_bodies(source: str) -> list[tuple[str, int, int]]:
    """`(qualname, first body line, last line)` of every `def`, the body starting
    after the docstring (see "Function level" in the module docstring)."""
    bodies: list[tuple[str, int, int]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(_decorator_name(d) in CACHES for d in child.decorator_list):
                    continue
                name = prefix + child.name
                body = child.body[1:] if ast.get_docstring(child) is not None else child.body
                if body and body[0].lineno > child.lineno:
                    bodies.append((name, body[0].lineno, child.end_lineno or 0))
                visit(child, name + ".<locals>.")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(ast.parse(source), "")
    return bodies


def _decorator_name(node: ast.expr) -> str:
    """`cache` for `@cache`, `@functools.cache` and `@functools.lru_cache(maxsize=8)`."""
    target = node.func if isinstance(node, ast.Call) else node
    if isinstance(target, ast.Attribute):
        return target.attr
    return target.id if isinstance(target, ast.Name) else ""


def touched_functions(base: str, head: str, hunks: list[tuple[range, range]]) -> set[str] | None:
    """The innermost function holding each changed line of `base` and `head`, or
    None when a line is outside every function body or nothing changed."""
    names: set[str] = set()
    for source, side in ((base, 0), (head, 1)):
        bodies = function_bodies(source)
        for hunk in hunks:
            for line in hunk[side]:
                holding = [b for b in bodies if b[1] <= line <= b[2]]
                if not holding:
                    return None
                names.add(max(holding, key=lambda b: b[1])[0])
    return names or None


def changed_functions(
    base: str,
    head: str,
    changes: list[tuple[str, list[str]]],
    test_map: Mapping[str, Any],
    root: Path,
) -> dict[str, set[str] | None]:
    """`touched_functions` of each modified file the map has a `functions` table for."""
    tables = test_map.get("functions", {})
    touched: dict[str, set[str] | None] = {}
    for status, paths in changes:
        path = paths[-1] if paths else ""
        if status != "M" or path not in tables:
            continue
        try:
            hunks = parse_hunks(_git(root, "diff", "-U0", f"{base}..{head}", "--", path))
            sides = (_git(root, "show", f"{base}:{path}"), _git(root, "show", f"{head}:{path}"))
            touched[path] = touched_functions(*sides, hunks)
        except (SyntaxError, ValueError):  # not Python at one end, or not UTF-8
            touched[path] = None
    return touched


def linked_tests(test_map: Mapping[str, Any], path: str, functions: set[str] | None) -> list[int]:
    """Indices of the test files that ran one of `functions` of `path`, when the
    map's `functions` table has them all; otherwise of those that ran the file."""
    table = test_map.get("functions", {}).get(path, {})
    if functions and functions <= table.keys():
        return sorted({i for name in functions for i in table[name][2]})
    return list(test_map["files"].get(path, ()))


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(TESTPATHS) and name.startswith("test_") and name.endswith(".py")


def select(
    changes: list[tuple[str, list[str]]],
    test_map: Mapping[str, Any],
    guard_tier: list[str],
    functions: Mapping[str, set[str] | None] | None = None,
) -> Selection:
    """Apply the fall-back rules in the module docstring to one diff; `functions`
    is `changed_functions` of it, when the diff has a base and a head."""
    reasons: list[str] = []
    tests = sorted(test_map["tests"])
    files: Mapping[str, list[int]] = test_map["files"]
    chosen: set[str] = set()
    for status, paths in changes:
        path = paths[-1] if paths else ""
        if status != "M":
            reasons.append(f"{status} {' -> '.join(paths)}: file set changed")
        elif path.rsplit("/", 1)[-1] == "conftest.py":
            reasons.append(f"{path}: conftest reaches every test below it")
        elif not path.endswith(".py") or not path.startswith(("src/", "test/")):
            reasons.append(f"{path}: not recorded by the map")
        elif path in files or is_test_file(path):
            if is_test_file(path):
                chosen.add(path)
            touched = (functions or {}).get(path)
            chosen.update(tests[i] for i in linked_tests(test_map, path, touched))
        else:
            reasons.append(f"{path}: no test executed it")
    if not guard_tier:
        reasons.append("guard tier is empty: refusing to reduce")
    if reasons:
        return Selection(True, [], reasons)
    return Selection(False, sorted(chosen | set(guard_tier)), [])


def guard_tier(root: Path) -> list[str]:
    """The always-run tier: tests that read the test tree as data."""
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from leaf_test_scope import corpus_gates  # noqa: E402  (reused, not copied)

    return corpus_gates(root)


def parse_ls_files(out: bytes) -> set[str]:
    """The paths in `git ls-files -z` output."""
    return {name for name in out.decode("utf-8", "surrogateescape").split("\0") if name}


def tracked_tests(root: Path) -> set[str] | None:
    """The files under `test/` in git's index, or None for a tree with no `.git`."""
    if not (root / ".git").exists():
        return None
    listed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--", "test"],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return parse_ls_files(listed.stdout)


def tier_lines(tier: Iterable[str], tracked: set[str] | None) -> list[str]:
    """*tier* as the `guard-tier` command prints it: tracked paths, in code point order."""
    lines = sorted(path for path in tier if tracked is None or path in tracked)
    if not lines:
        raise ValueError("guard tier is empty: refusing to print it")
    return lines


def core_files(test_map: Mapping[str, Any]) -> list[str]:
    """The `src/` files the map links to more than `CORE_REACH` of its test files."""
    floor = CORE_REACH * len(test_map["tests"])
    return sorted(
        path
        for path, linked in test_map["files"].items()
        if path.startswith("src/") and len(linked) > floor
    )


def share(selection: Selection, test_map: Mapping[str, Any]) -> float:
    """Share of the map's test node ids the selection would run (full = 1.0)."""
    counts: Mapping[str, int] = test_map["tests"]
    total = sum(counts.values())
    if selection.full or not total:
        return 1.0
    return sum(counts.get(t, 0) for t in selection.tests) / total


def git_first_parent_files(
    root: Path, ref: str, commits: int, timeout: float | None = None
) -> list[list[str]]:
    """The paths each of the last `commits` first-parent commits of `ref` touched.

    `-z` prints every path raw and NUL-terminated: without it git quotes a path
    with non-ASCII or special characters, which would then miss the `src/` prefix.
    A root commit, and the boundary commit of a shallow clone, lists no paths:
    diffed against the empty tree it would pair every `src/` file with every test.
    `timeout` defaults to `GIT_LOG_TIMEOUT_SECS`, read at call time.
    """
    out = subprocess.run(
        ["git", "-c", "log.showRoot=false", "log", "-z", f"-n{commits}"]
        + ["--first-parent", "--diff-merges=first-parent"]
        + ["--format=%x01", "--name-only", ref],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_LOG_TIMEOUT_SECS if timeout is None else timeout,
    ).stdout
    found: list[list[str]] = []
    for token in out.split("\x00"):
        # A commit's header token is the marker, then its first path after a newline.
        if token.lstrip("\n").startswith("\x01"):
            found.append([])
            token = token.lstrip("\n")[1:].lstrip("\n")
        elif token.startswith("\n"):
            token = token[1:]
        if token and found:
            found[-1].append(token)
    return found


def co_change(commits: Iterable[list[str]]) -> dict[str, list[str]]:
    """`src/` file -> the test files some commit changed together with it."""
    links: dict[str, set[str]] = {}
    for paths in commits:
        tests = {p for p in paths if is_test_file(p)}
        if not tests:
            continue
        for src in paths:
            if src.startswith("src/") and not is_test_file(src):
                links.setdefault(src, set()).update(tests)
    return {src: sorted(tests) for src, tests in sorted(links.items())}


def is_shallow(root: Path, timeout: float | None = None) -> bool:
    """Whether the clone at `root` lacks the older part of its history."""
    out = subprocess.run(
        ["git", "rev-parse", "--is-shallow-repository"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_LOG_TIMEOUT_SECS if timeout is None else timeout,
    ).stdout
    return out.strip() == "true"


def co_change_window(
    root: Path, ref: str, commits: int, timeout: float | None = None
) -> dict[str, list[str]]:
    """`co_change` over the last `commits` first-parent commits of `ref`.

    Raises when a shallow clone does not also hold the commit before the window:
    the window's oldest commit would then have no parent to be compared with.
    """
    held = git_first_parent_files(root, ref, commits + 1, timeout)
    if len(held) <= commits and is_shallow(root, timeout):
        raise ValueError(f"shallow clone holds {len(held)} of the {commits + 1} commits needed")
    return co_change(held[:commits])


def bundle(
    test_map: dict[str, Any],
    root: Path,
    ref: str = "HEAD",
    commits: int = CO_CHANGE_COMMITS,
    timeout: float | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The map with the tables that could be built, and why each other one could not.

    A table the map already carries is not kept: it is built here or left out.
    """
    doc: dict[str, Any] = {k: v for k, v in test_map.items() if k not in BUNDLE_TABLES}
    absent: dict[str, str] = {}
    tables: tuple[tuple[str, Callable[[], Any], str, str], ...] = (
        (
            "cochange",
            lambda: co_change_window(root, ref, commits, timeout),
            "history read failed",
            "no commit in the window changed a src file and a test file together",
        ),
        ("guard", lambda: guard_tier(root), "corpus_gates failed", NO_GUARD),
        (
            "os_sensitive",
            lambda: _os_tables_module.os_sensitive(root),
            "src scan failed",
            "the scan found no OS-sensitive src file",
        ),
        (
            "windows_targeting",
            lambda: _os_tables_module.windows_targeting(root),
            "test scan failed",
            "the scan found no test file that targets Windows",
        ),
    )
    for name, build, failed, empty in tables:
        try:
            table = build()
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            absent[name] = f"{failed}: {exc}"
            continue
        if table:
            doc[name] = table
        else:
            absent[name] = empty
    return doc, absent


def _cmd_bundle(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    doc, absent = bundle(load_map(Path(args.map)), root, args.ref, args.commits)
    doc["built_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    data = gzip.compress(text.encode("utf-8"), mtime=0)
    Path(args.out).write_bytes(data)
    tables = [t for t in BUNDLE_TABLES if t in doc]
    print(f"bundle: tables {', '.join(tables) or 'none'}, {len(data) / 1e6:.2f} MB gzip")
    for table, why in sorted(absent.items()):
        print(f"::warning::{table} left out: {why}")
    return 0


def bundle_rows(doc: Mapping[str, Any]) -> dict[str, int]:
    """Rows in each table of a bundle; a table that is absent or empty has none."""
    tables = {name: doc.get(name) for name in ("tests", "files", *BUNDLE_TABLES)}
    return {n: len(t) if isinstance(t, (dict, list)) else 0 for n, t in tables.items()}


def _cmd_check(args: argparse.Namespace) -> int:
    path = Path(args.bundle)
    doc = load_map(path)
    rows = bundle_rows(doc)
    counts = ", ".join(f"{name} {count}" for name, count in rows.items())
    built = f", built_at {doc['built_at']}" if "built_at" in doc else ""
    size = path.stat().st_size
    print(f"bundle: commit `{doc.get('commit', '')}`{built}, {size} bytes, rows: {counts}")
    missing = [name for name in BUNDLE_TABLES if not rows[name]]
    for name in missing:
        print(f"::error::the bundle has no {name} table")
    return 1 if missing else 0


def _cmd_build(args: argparse.Namespace) -> int:
    doc = merge_parts(read_parts(Path(args.parts)), args.commit)
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    nodes = sum(doc["tests"].values())
    print(
        f"map: {len(doc['files'])} executed files, {len(doc['tests'])} test files, "
        f"{nodes} test node ids, {len(text.encode()) / 1e6:.2f} MB"
    )
    if not doc["files"]:
        print("::error::the parts recorded no executed file")
        return 1
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    test_map = load_map(Path(args.map))
    functions = None
    if args.name_status:
        changes = parse_name_status(Path(args.name_status).read_text(encoding="utf-8"))
    elif args.base and args.head:
        changes = parse_name_status(git_name_status(args.base, args.head, root))
        functions = changed_functions(args.base, args.head, changes, test_map, root)
    else:
        print("select needs --base and --head, or --name-status", file=sys.stderr)
        return 2
    result = select(changes, test_map, guard_tier(root), functions)
    if args.json:
        print(json.dumps({**result._asdict(), "share": share(result, test_map)}))
        return FULL if result.full else 0
    if result.full:
        for reason in result.reasons:
            print(f"full suite: {reason}", file=sys.stderr)
        return FULL
    print("\n".join(result.tests))
    return 0


def _cmd_core(args: argparse.Namespace) -> int:
    test_map = load_map(Path(args.map))
    listed = core_files(test_map)
    header = (
        f"# The src files the test map at {test_map.get('commit') or '?'} links to more\n"
        f"# than {CORE_REACH:.0%} of its test files; the recorder maps their functions.\n"
        "# Generated: python3 scripts/ci_test_map.py core --map <test-map.json>\n"
    )
    out = Path(args.root) / CORE_LIST
    out.write_text(header + "".join(f"{path}\n" for path in listed), encoding="utf-8")
    print(f"core: {len(listed)} src files written to {CORE_LIST}")
    return 0


def _cmd_guard_tier(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    print("\n".join(tier_lines(guard_tier(root), tracked_tests(root))))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", default=str(REPO_ROOT), help="repository root")
    sub = parser.add_subparsers(dest="cmd", required=True)
    build = sub.add_parser("build", help="plugin parts -> map JSON")
    build.add_argument("--parts", required=True, help="directory holding part-*.json")
    build.add_argument("--out", required=True, help="map JSON to write")
    build.add_argument("--commit", default="", help="commit the parts were recorded on")
    pack = sub.add_parser("bundle", help="map JSON -> gzip selection bundle")
    pack.add_argument("--map", required=True, help="map to add the tables to")
    pack.add_argument("--out", required=True, help="gzip JSON bundle to write")
    pack.add_argument("--ref", default="HEAD", help="branch whose first parents feed cochange")
    pack.add_argument(
        "--commits", type=int, default=CO_CHANGE_COMMITS, help="first-parent commits to read"
    )
    check = sub.add_parser("check", help="describe a bundle; exit 1 when a table is missing")
    check.add_argument("--bundle", required=True, help="gzip JSON bundle to read")
    pick = sub.add_parser("select", help="print the test files a diff would run")
    pick.add_argument("--map", required=True, help="map or bundle, plain or gzip")
    pick.add_argument("--base")
    pick.add_argument("--head")
    pick.add_argument("--name-status", help="file holding `git diff --name-status -M` output")
    pick.add_argument("--json", action="store_true", help="print one JSON verdict line")
    sub.add_parser("guard-tier", help="print the guard tier, one test file per line")
    core = sub.add_parser("core", help=f"map -> {CORE_LIST}")
    core.add_argument("--map", required=True)
    args = parser.parse_args(argv)
    commands = {
        "build": _cmd_build,
        "bundle": _cmd_bundle,
        "check": _cmd_check,
        "select": _cmd_select,
        "guard-tier": _cmd_guard_tier,
        "core": _cmd_core,
    }
    try:
        return commands[args.cmd](args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
