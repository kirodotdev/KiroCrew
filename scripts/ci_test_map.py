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
    bundle  wrap a map, the co-change history and the guard tier into one
            gzip selection artifact (schema 2, below)
    select  given a diff (`--base B --head H`, or `--name-status FILE`) and a
            map or bundle, print the test files it would run, or fall back to
            the full suite and say why
    guard-tier
            print the guard tier, one test file per line, so the tier runs as
            one pytest invocation:
                python -m pytest $(python3 scripts/ci_test_map.py guard-tier)
            Only files git tracks, in code point order, as POSIX paths: the
            same lines on every platform, whatever else a working tree holds.

The map
-------
    {"schema": 1, "commit": SHA, "tests": {test file: node ids run},
     "files": {executed file: [index into sorted(tests)]}}

Paths are repository-relative POSIX paths. Files are the unit CI shards and
runs, so a node id counts for its test FILE.

The bundle (gzip JSON)
----------------------
    {"schema": 2, "commit": SHA, "built_at": ISO,
     "map": <the schema-1 map, unchanged>,
     "co_change": {src file: {test file: count}},  # last N first-parent commits
     "co_change_window": {"asked": N, "read": commits read, "shallow": bool},
     "guard": [test file, ...]}                    # leaf_test_scope.corpus_gates

A table that cannot be built, or that comes out empty, is left out of the file,
never written empty, so a reader can tell "no data" from "no links". A shallow
clone cannot build `co_change` unless it holds `--commits` commits and the one
before them: a part of the window would read as the whole of it. The window is
written beside the table, and a reader takes the table as data only when
`window_is_whole` says so.

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
import gzip
import json
import subprocess
import sys
import zlib
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA = 1
BUNDLE_SCHEMA = 2
FULL = 3
CO_CHANGE_COMMITS = 2000
GIT_LOG_TIMEOUT_SECS = 600.0
BUNDLE_TABLES = ("co_change", "guard")
NO_GUARD = "corpus_gates found no guard test"
NO_PAIR = "no commit in the window changed a src file and a test file together"

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


def _read_json(path: Path) -> Any:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except (EOFError, zlib.error) as exc:
            raise ValueError(f"{path}: damaged gzip data: {exc}") from exc
    return json.loads(raw.decode("utf-8"))


def load_bundle(path: Path) -> dict[str, Any]:
    """Read a schema-2 bundle; its map is checked like a schema-1 map file."""
    doc = _read_json(path)
    if not isinstance(doc, dict) or doc.get("schema") != BUNDLE_SCHEMA:
        raise ValueError(f"{path}: not a schema-{BUNDLE_SCHEMA} selection bundle")
    _check_map(doc.get("map"), path)
    return doc


def load_map(path: Path) -> dict[str, Any]:
    """Read and check a map file, or the map inside a bundle; a wrong schema is a usage error."""
    doc = _read_json(path)
    if isinstance(doc, dict) and doc.get("schema") == BUNDLE_SCHEMA:
        return _check_map(doc.get("map"), path)
    return _check_map(doc, path)


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


def co_change(commits: Iterable[list[str]]) -> dict[str, dict[str, int]]:
    """`src/` file -> {test file: how many commits changed both}."""
    counts: dict[str, dict[str, int]] = {}
    for paths in commits:
        tests = sorted({p for p in paths if is_test_file(p)})
        for src in sorted({p for p in paths if p.startswith("src/") and not is_test_file(p)}):
            row = counts.setdefault(src, {})
            for test in tests:
                row[test] = row.get(test, 0) + 1
    return {src: row for src, row in sorted(counts.items()) if row}


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
) -> tuple[dict[str, dict[str, int]], dict[str, Any]]:
    """`co_change` over the last `commits` first-parent commits of `ref`, and its window.

    Raises when a shallow clone does not also hold the commit before the window:
    the window's oldest commit would then have no parent to be compared with.
    """
    held = git_first_parent_files(root, ref, commits + 1, timeout)
    shallow = is_shallow(root, timeout)
    if shallow and len(held) <= commits:
        raise ValueError(f"shallow clone holds {len(held)} of the {commits + 1} commits needed")
    window = {"asked": commits, "read": min(len(held), commits), "shallow": shallow}
    return co_change(held[:commits]), window


def window_is_whole(window: object) -> bool:
    """Whether a bundle's `co_change_window` says its table covers what was asked.

    Whole is every commit asked for, or fewer from a clone that holds the whole
    history. Anything else, and a window that is missing or of another shape,
    is a part of the answer at best.
    """
    if not isinstance(window, dict):
        return False
    asked, read, shallow = (window.get(k) for k in ("asked", "read", "shallow"))
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in (asked, read)):
        return False
    return read == asked or (read < asked and shallow is False)


def bundle(
    test_map: dict[str, Any],
    root: Path,
    ref: str = "HEAD",
    commits: int = CO_CHANGE_COMMITS,
    timeout: float | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """The schema-2 bundle, and why each table left out of it could not be built."""
    doc: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "commit": test_map.get("commit", ""),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "map": test_map,
    }
    absent: dict[str, str] = {}
    failures = (OSError, ValueError, subprocess.SubprocessError)
    try:
        pairs, window = co_change_window(root, ref, commits, timeout)
    except failures as exc:
        absent["co_change"] = f"history read failed: {exc}"
    else:
        if pairs:
            doc["co_change"], doc["co_change_window"] = pairs, window
        else:
            absent["co_change"] = NO_PAIR
    try:
        gates = guard_tier(root)
    except failures as exc:
        absent["guard"] = f"corpus_gates failed: {exc}"
    else:
        if gates:
            doc["guard"] = gates
        else:
            absent["guard"] = NO_GUARD
    return doc, absent


def _cmd_bundle(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    doc, absent = bundle(load_map(Path(args.map)), root, args.ref, args.commits)
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True)
    data = gzip.compress(text.encode("utf-8"), mtime=0)
    Path(args.out).write_bytes(data)
    tables = [t for t in BUNDLE_TABLES if t in doc]
    print(f"bundle: tables {', '.join(tables) or 'none'}, {len(data) / 1e6:.2f} MB gzip")
    for table, why in sorted(absent.items()):
        print(f"::warning::{table} left out: {why}")
    return 0


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
    pack = sub.add_parser("bundle", help="map JSON -> schema-2 gzip selection bundle")
    pack.add_argument("--map", required=True, help="schema-1 map (or a bundle) to wrap")
    pack.add_argument("--out", required=True, help="gzip JSON bundle to write")
    pack.add_argument("--ref", default="HEAD", help="branch whose first parents feed co_change")
    pack.add_argument(
        "--commits", type=int, default=CO_CHANGE_COMMITS, help="first-parent commits to read"
    )
    pick = sub.add_parser("select", help="print the test files a diff would run")
    pick.add_argument("--map", required=True, help="schema-1 map or schema-2 bundle")
    pick.add_argument("--base")
    pick.add_argument("--head")
    pick.add_argument("--name-status", help="file holding `git diff --name-status -M` output")
    pick.add_argument("--json", action="store_true", help="print one JSON verdict line")
    sub.add_parser("guard-tier", help="print the guard tier, one test file per line")
    args = parser.parse_args(argv)
    commands = {
        "build": _cmd_build,
        "bundle": _cmd_bundle,
        "select": _cmd_select,
        "guard-tier": _cmd_guard_tier,
    }
    try:
        return commands[args.cmd](args)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
