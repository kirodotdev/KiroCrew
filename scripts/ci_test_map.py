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
    select  given a diff (`--base B --head H`, or `--name-status FILE`) and a
            map, print the test files it would run, or fall back to the full
            suite and say why
    guard-tier
            print the guard tier, one test file per line, so the tier runs as
            one pytest invocation:
                python -m pytest $(python3 scripts/ci_test_map.py guard-tier)
            Only files git tracks, in code point order, as POSIX paths: the
            same lines on every platform, whatever else a working tree holds.

The map
-------
    {"schema": 1, "commit": SHA, "tests": {test file: node ids run},
     "files": {executed file: [index into sorted(tests)]},
     "os_sensitive": {src file: tier}, "windows_targeting": [test file, ...]}

The last two are computed from the tree `--root` names at build time, and are
absent when they cannot be built (`scripts/ci_test_os_tables.py`).

Paths are repository-relative POSIX paths. Files are the unit CI shards and
runs, so a node id counts for its test FILE.

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

Exit codes: 0 selection printed, 3 full suite (reason on stderr), 2 usage.
"""

from __future__ import annotations

import argparse
import importlib
import json
import subprocess
import sys
from collections.abc import Iterable, Mapping
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

# Where pytest collects (setup.cfg `testpaths`). A changed test file outside
# these is not a test pytest would run, so it falls back like any other file.
TESTPATHS = ("test/", "src/kiro_crew/apps/builtins/")


class Selection(NamedTuple):
    """The verdict for one diff: test files, or `full=True` with the reasons."""

    full: bool
    tests: list[str]
    reasons: list[str]


def merge_parts(parts: Iterable[Mapping[str, Any]], commit: str) -> dict[str, Any]:
    """Fold plugin parts (`{test file: {"nodes": N, "files": [...]}}`) into the map."""
    nodes: dict[str, int] = {}
    links: dict[str, set[str]] = {}
    for part in parts:
        for test_file, entry in part.items():
            nodes[test_file] = nodes.get(test_file, 0) + int(entry["nodes"])
            for executed in entry["files"]:
                links.setdefault(executed, set()).add(test_file)
    tests = sorted(nodes)
    index = {name: i for i, name in enumerate(tests)}
    return {
        "schema": SCHEMA,
        "commit": commit,
        "tests": {name: nodes[name] for name in tests},
        "files": {path: sorted(index[t] for t in linked) for path, linked in sorted(links.items())},
    }


def read_parts(directory: Path) -> list[dict[str, Any]]:
    paths = sorted(directory.glob("**/part-*.json"))
    if not paths:
        raise ValueError(f"{directory}: no part-*.json files")
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


def load_map(path: Path) -> dict[str, Any]:
    """Read and check a map file; a wrong schema is a usage error, not a guess."""
    doc = json.loads(path.read_text(encoding="utf-8"))
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


def git_name_status(base: str, head: str, root: Path) -> str:
    """Name-status of `base..head` with rename detection, as git prints it."""
    return subprocess.run(
        ["git", "diff", "--name-status", "-M", f"{base}..{head}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    ).stdout


def is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return path.startswith(TESTPATHS) and name.startswith("test_") and name.endswith(".py")


def select(
    changes: list[tuple[str, list[str]]], test_map: Mapping[str, Any], guard_tier: list[str]
) -> Selection:
    """Apply the fall-back rules in the module docstring to one diff."""
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
            chosen.update(tests[i] for i in files.get(path, ()))
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


def share(selection: Selection, test_map: Mapping[str, Any]) -> float:
    """Share of the map's test node ids the selection would run (full = 1.0)."""
    counts: Mapping[str, int] = test_map["tests"]
    total = sum(counts.values())
    if selection.full or not total:
        return 1.0
    return sum(counts.get(t, 0) for t in selection.tests) / total


def _cmd_build(args: argparse.Namespace) -> int:
    doc = merge_parts(read_parts(Path(args.parts)), args.commit)
    doc.update(_os_tables_module.os_tables(Path(args.root).resolve()))
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    nodes = sum(doc["tests"].values())
    print(
        f"map: {len(doc['files'])} executed files, {len(doc['tests'])} test files, "
        f"{nodes} test node ids, {len(text.encode()) / 1e6:.2f} MB"
    )
    print(
        f"os tables: {len(doc.get('os_sensitive', {}))} os-sensitive src files, "
        f"{len(doc.get('windows_targeting', []))} windows-targeting test files"
    )
    if not doc["files"]:
        print("::error::the parts recorded no executed file")
        return 1
    return 0


def _cmd_select(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    test_map = load_map(Path(args.map))
    if args.name_status:
        text = Path(args.name_status).read_text(encoding="utf-8")
    elif args.base and args.head:
        text = git_name_status(args.base, args.head, root)
    else:
        print("select needs --base and --head, or --name-status", file=sys.stderr)
        return 2
    result = select(parse_name_status(text), test_map, guard_tier(root))
    if args.json:
        print(json.dumps({**result._asdict(), "share": share(result, test_map)}))
        return FULL if result.full else 0
    if result.full:
        for reason in result.reasons:
            print(f"full suite: {reason}", file=sys.stderr)
        return FULL
    print("\n".join(result.tests))
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
    pick = sub.add_parser("select", help="print the test files a diff would run")
    pick.add_argument("--map", required=True)
    pick.add_argument("--base")
    pick.add_argument("--head")
    pick.add_argument("--name-status", help="file holding `git diff --name-status -M` output")
    pick.add_argument("--json", action="store_true", help="print one JSON verdict line")
    sub.add_parser("guard-tier", help="print the guard tier, one test file per line")
    args = parser.parse_args(argv)
    commands = {"build": _cmd_build, "select": _cmd_select, "guard-tier": _cmd_guard_tier}
    try:
        return commands[args.cmd](args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
