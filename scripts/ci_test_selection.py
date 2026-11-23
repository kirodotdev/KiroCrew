#!/usr/bin/env python3
"""SHADOW scorer: a skip tier for every test file of a diff, and a per-PR verdict.

SHADOW ONLY: nothing reads the verdict to skip a test. `scripts/ci_test_map.py
select` answers "these files, or everything"; this scorer says how much signal
ties each test file to the diff, so the risk of skipping it can be measured and
tuned per signal.

    python3 scripts/ci_test_selection.py --bundle MAP (--base B --head H |
        --name-status FILE) --out VERDICT [--rates FILE] [--timeout SECS]

Tiers, first match wins
-----------------------
    A  always run: the guard tier, a changed test file, or any `select` fall-back
       (which puts every test file in A and makes the verdict `full`)
    B  the map links it to a changed file reached by at most `narrow_reach_max`
       test files
    C  the map links it to a changed file reached by more
    D  it names a changed module (`git grep`: the dotted name, or
       `from <package> import <leaf>` with any spacing, a wrapped import
       confirmed by `ast`)
    E  it sits in the same folder as a changed file
    F  the bundle's `cochange` table links it to a changed file, or it is in the
       same package (`src/kiro_crew/<pkg>/...` vs `test_<pkg>*.py` or a `<pkg>/`
       folder)
    G  no signal

The bundle is a schema-1 test map, or the schema-2 gzip file `ci_test_map.py
bundle` writes, read as its map plus `co_change` (as `cochange`) and `guard`;
`tests`, `files` and `cochange` are the tables read. A missing table never makes
a file skippable: the files that would be G are held in F instead, and a table
the schema-2 file holds empty counts as missing. An optional `guard` table
(`[test file]`, `leaf_test_scope.corpus_gates` run when the bundle was built)
spares the live guard scan, which costs seconds; without it the scan runs. A
test the diff adds or edits is tier A or a fall-back either way.

`scripts/ci_test_selection_rates.json` gives each tier an upper-95% bound on its
diff-caused failure rate; a tier is skippable when that bound is below `y`.

The verdict (`--out`)
---------------------
    {"schema": 1, "base": SHA, "head": SHA, "bundle_commit": SHA | null,
     "verdict": "select" | "full", "reason": str | null,
     "tiers": {"A": [test file, ...], ..., "G": [...]},
     "would_skip": [test file, ...], "would_run_share": float}

`would_run_share` weighs each test file by its node-id count in the map (1 for a
file the map lacks). The test tree is read from the checkout, so a `--head` that
is not the checked-out commit, no bundle, an unreadable one, a table of the wrong
shape, an exception or `--timeout` (unfinished work is abandoned, not awaited) gives
verdict `full` with the reason. Exit 0 always, but 2 for bad arguments.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_test_map as ctm  # noqa: E402  (path set immediately above)

SCHEMA = 1
TIERS = ("A", "B", "C", "D", "E", "F", "G")
RATES = REPO_ROOT / "scripts" / "ci_test_selection_rates.json"
TABLES = ("tests", "files", "cochange")
TIMEOUT_SECS = 60.0
GIT_TIMEOUT_SECS = 30.0

Grep = Callable[[list[str], Path], set[str]]


def load_rates(path: Path) -> tuple[set[str], int]:
    """The skippable tiers and the narrow-reach threshold."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    y = float(doc["y"])
    skippable = {t for t in TIERS if float(doc["tiers"][t]["upper95"]) < y}
    return skippable, int(doc["narrow_reach_max"])


def load_bundle(path: Path) -> dict[str, Any]:
    """A schema-1 map, or a schema-2 `ci_test_map.py bundle` read as its tables."""
    doc = ctm._read_json(path)
    if isinstance(doc, dict) and doc.get("schema") == ctm.BUNDLE_SCHEMA:
        doc = _flatten_bundle(doc)
    if not isinstance(doc, dict) or doc.get("schema") != ctm.SCHEMA:
        raise ValueError(f"{path}: not a schema-{ctm.SCHEMA} bundle")
    check_tables(doc)
    return doc


def _flatten_bundle(packed: Mapping[str, Any]) -> Any:
    """The schema-2 bundle's map, with its `co_change` and `guard` tables beside it.

    The writer leaves out a table it could not build, so one that is missing,
    null or empty is no data: it is not carried over as a table with no links.
    """
    doc = packed.get("map")
    if not isinstance(doc, dict):
        return doc
    doc = dict(doc)
    co_change = packed.get("co_change")
    if co_change not in (None, {}):
        if not isinstance(co_change, dict) or not all(
            isinstance(r, dict) for r in co_change.values()
        ):
            raise ValueError("bundle table `co_change` is not {file: {test file: count}}")
        doc["cochange"] = {src: sorted(row) for src, row in co_change.items()}
    if packed.get("guard") not in (None, []):
        doc["guard"] = packed["guard"]
    return doc


def check_tables(bundle: Mapping[str, Any]) -> None:
    """A present table of the wrong shape is corrupt, never a quietly lost signal."""

    def is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    tests = bundle.get("tests", {})
    if not isinstance(tests, dict) or not all(is_int(n) and n >= 0 for n in tests.values()):
        raise ValueError("bundle table `tests` is not {test file: node count}")
    files = bundle.get("files", {})
    if not isinstance(files, dict) or not all(
        isinstance(links, list) and all(is_int(i) and 0 <= i < len(tests) for i in links)
        for links in files.values()
    ):
        raise ValueError("bundle table `files` is not {file: [index into tests]}")
    cochange = bundle.get("cochange", {})
    if not isinstance(cochange, dict) or not all(
        isinstance(links, list) and all(isinstance(t, str) for t in links)
        for links in cochange.values()
    ):
        raise ValueError("bundle table `cochange` is not {file: [test file]}")
    guard = bundle.get("guard", [])
    if not isinstance(guard, list) or not all(isinstance(t, str) for t in guard):
        raise ValueError("bundle table `guard` is not [test file]")


def module_name(path: str) -> str | None:
    """`src/kiro_crew/a/b.py` -> `kiro_crew.a.b`; test helpers import bare."""
    if not path.endswith(".py"):
        return None
    stem = path[: -len(".py")].removesuffix("/__init__")
    for prefix in ("src/", "test/"):
        stem = stem.removeprefix(prefix)
    return stem.replace("/", ".")


def name_patterns(
    modules: Iterable[str],
) -> tuple[list[str], re.Pattern[str], re.Pattern[str]]:
    """Fixed strings for `git grep -F`, the regex that confirms a hit, and the
    regex for a `from <package>` line whose leaf sits on a later line.

    A regex `git grep` over the test tree costs over a second per module; the
    fixed strings (each module and its package, no keyword spacing assumed)
    prefilter it and the regexes run on the matching lines only.
    """
    fixed: set[str] = set()
    forms: list[str] = []
    parents: set[str] = set()
    for name in sorted(set(modules)):
        fixed.add(name)
        forms.append(rf"(?<![\w.]){re.escape(name)}(?!\w)")
        if "." in name:
            parent, leaf = name.rsplit(".", 1)
            fixed.add(parent)
            parents.add(re.escape(parent))
            forms.append(
                rf"\bfrom[ \t]+{re.escape(parent)}[ \t]+import(?!\w).*(?<![\w.]){re.escape(leaf)}(?!\w)"
            )
    never = r"(?!)"
    # `from <package> import (` left open (a comment may follow), or a backslash
    # continuation.
    wrapped = (
        rf"\bfrom[ \t]+(?:{'|'.join(sorted(parents))})[ \t]*"
        r"(\\\s*$|[ \t]import(?!\w)[^)#]*(\([^)]*|\\\s*)$)"
    )
    return (
        sorted(fixed),
        re.compile("|".join(forms) or never),
        re.compile(wrapped if parents else never),
    )


def git_grep(modules: list[str], root: Path) -> set[str]:
    """Tracked files under the test paths that name one of `modules`."""
    fixed, confirm, wrapped = name_patterns(modules)
    if not fixed:
        return set()
    # A module anywhere; its package only on a line that also names the leaf or
    # wraps, which keeps `kiro_crew` (every top-level module's package) from
    # matching every line of the tree.
    names = sorted(set(modules))
    terms = [["-e", name] for name in names]
    for name in names:
        if "." in name:
            parent, leaf = name.rsplit(".", 1)
            terms.append(["-e", parent, "--and", "-e", "import", "--and", "-e", leaf])
    for parent in sorted(set(fixed) - set(names)):
        terms.append(["-e", parent, "--and", "-e", "import", "--and", "-e", "("])
        terms.append(["-e", parent, "--and", "-e", "\\"])
    argv = ["git", "grep", "--null", "-I", "-F", *terms[0]]
    for term in terms[1:]:
        argv += ["--or", *term]
    done = subprocess.run(
        [*argv, "--", *ctm.TESTPATHS],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_SECS,
    )
    if done.returncode not in (0, 1):  # 1: no match
        raise subprocess.CalledProcessError(done.returncode, argv, done.stdout, done.stderr)
    hits: set[str] = set()
    continued: set[str] = set()
    for row in done.stdout.splitlines():
        path, _, line = row.partition("\0")
        if path in hits:
            continue
        if confirm.search(line):
            hits.add(path)
        elif wrapped.search(line):
            continued.add(path)
    pairs = {tuple(m.rsplit(".", 1)) for m in modules if "." in m}
    for path in sorted(continued - hits):
        if _imports_any((root / path).read_text(encoding="utf-8", errors="replace"), pairs):
            hits.add(path)
    return hits


def _imports_any(source: str, pairs: set[tuple[str, ...]]) -> bool:
    """Whether a `from parent import leaf` of `pairs` appears, however it is wrapped."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True  # unreadable: keep the signal rather than lose it
    return any(
        (node.module, alias.name) in pairs
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    )


def git_test_files(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *ctm.TESTPATHS],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_SECS,
    ).stdout
    return [p for p in out.splitlines() if ctm.is_test_file(p)]


def rev_parse(ref: str, root: Path) -> str:
    """The commit SHA `ref` names."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_SECS,
    ).stdout.strip()


def check_checkout(head: str, root: Path) -> None:
    """The test tree is read from the checkout, so it must be `head`'s."""
    if rev_parse("HEAD", root) != head:
        raise ValueError(f"the checkout is not --head {head}, so its test tree is not head's")


def _package(path: str) -> str | None:
    if not path.startswith("src/kiro_crew/"):
        return None
    first = path.removeprefix("src/kiro_crew/").split("/")[0]
    return None if first == "__init__.py" else first.removesuffix(".py")


def _in_package(test: str, package: str) -> bool:
    dirs, stem = test.split("/")[:-1], test.rsplit("/", 1)[-1][len("test_") : -len(".py")]
    return package in dirs or stem == package or stem.startswith(package + "_")


def score(
    changes: list[tuple[str, list[str]]],
    bundle: Mapping[str, Any],
    universe: Iterable[str],
    guard: list[str],
    *,
    skippable: set[str],
    narrow_reach_max: int,
    grep: Callable[[list[str]], set[str]],
) -> dict[str, Any]:
    """Tier every test file of `universe` for one diff; the verdict without base/head."""
    tables = {t: bundle[t] for t in TABLES if isinstance(bundle.get(t), dict)}
    missing = [t for t in TABLES if t not in tables]
    counts: Mapping[str, int] = tables.get("tests", {})
    tests = sorted(set(universe) | set(guard))
    test_map = {"tests": counts, "files": tables.get("files", {})}
    fallback = ctm.select(changes, test_map, guard)
    if fallback.full:
        return _verdict(
            {t: tests if t == "A" else [] for t in TIERS},
            [],
            counts,
            "fallback: " + "; ".join(fallback.reasons),
        )

    paths = [p for _status, ps in changes for p in ps[-1:]]
    indexed = sorted(counts)
    narrow: set[str] = set()
    broad: set[str] = set()
    for path in paths:
        linked = {indexed[i] for i in tables.get("files", {}).get(path, ())}
        (narrow if len(linked) <= narrow_reach_max else broad).update(linked)
    named = grep(sorted({m for p in paths if (m := module_name(p))}))
    changed_tests = {p for p in paths if ctm.is_test_file(p)}
    # An edited test file is tier A itself; it ties none of its siblings to the diff.
    folders = {p.rsplit("/", 1)[0] for p in paths if "/" in p and p not in changed_tests}
    cochange = {t for p in paths for t in tables.get("cochange", {}).get(p, ())}
    packages = {pkg for p in paths if (pkg := _package(p))}

    tiers: dict[str, list[str]] = {t: [] for t in TIERS}
    held: set[str] = set()
    for test in tests:
        if test in changed_tests or test in guard:
            tier = "A"
        elif test in narrow:
            tier = "B"
        elif test in broad:
            tier = "C"
        elif test in named:
            tier = "D"
        elif test.rsplit("/", 1)[0] in folders:
            tier = "E"
        elif test in cochange or any(_in_package(test, pkg) for pkg in packages):
            tier = "F"
        elif missing:
            tier = "F"
            held.add(test)
        else:
            tier = "G"
        tiers[tier].append(test)
    skip = sorted(t for tier in skippable for t in tiers[tier] if t not in held)
    return _verdict(tiers, skip, counts, None)


def _verdict(
    tiers: dict[str, list[str]], skip: list[str], counts: Mapping[str, int], reason: str | None
) -> dict[str, Any]:
    everything = [t for tier in TIERS for t in tiers[tier]]
    total = sum(counts.get(t, 1) for t in everything)
    skipped = sum(counts.get(t, 1) for t in skip)
    return {
        "verdict": "full" if reason else "select",
        "reason": reason,
        "tiers": tiers,
        "would_skip": skip,
        "would_run_share": 1.0 if not total else (total - skipped) / total,
    }


def full_verdict(reason: str) -> dict[str, Any]:
    return {
        "verdict": "full",
        "reason": reason,
        "tiers": {t: [] for t in TIERS},
        "would_skip": [],
        "would_run_share": 1.0,
    }


T = TypeVar("T")


def bounded(work: Callable[[], T], timeout: float) -> T:
    """`work()`, or TimeoutError once `timeout` seconds pass with it unfinished.

    It runs on a daemon thread, so an unfinished read, scan or git call is
    abandoned: the caller writes its verdict and the process exits.
    """
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = work()
        except BaseException as exc:  # noqa: BLE001  (re-raised on the caller's thread)
            box["error"] = exc

    thread = threading.Thread(target=target, name="ci-test-selection-score", daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"scoring did not finish within {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _score_args(args: argparse.Namespace, grep: Grep) -> tuple[dict[str, Any], dict[str, Any]]:
    """`(base, head and bundle_commit, the verdict body)` for one diff."""
    root = Path(args.root).resolve()
    meta: dict[str, Any] = {"base": None, "head": None}
    for key in meta:
        if getattr(args, key):
            meta[key] = rev_parse(getattr(args, key), root)
    bundle = load_bundle(Path(args.bundle))
    skippable, narrow_reach_max = load_rates(Path(args.rates))
    if meta["head"]:
        check_checkout(meta["head"], root)
    if args.name_status:
        text = Path(args.name_status).read_text(encoding="utf-8")
    else:
        text = ctm.git_name_status(meta["base"], meta["head"], root)
    body = score(
        ctm.parse_name_status(text),
        bundle,
        git_test_files(root),
        bundle["guard"] if "guard" in bundle else ctm.guard_tier(root),
        skippable=skippable,
        narrow_reach_max=narrow_reach_max,
        grep=lambda modules: grep(modules, root),
    )
    return {**meta, "bundle_commit": bundle.get("commit") or None}, body


def run(args: argparse.Namespace, grep: Grep = git_grep) -> dict[str, Any]:
    """Score one diff; any failure or the time-out becomes a `full` verdict with its reason.

    `base` and `head` are the commit SHAs the revisions name; on a failure they
    are the revisions as given, since resolving them may be what failed.
    """
    meta: dict[str, Any] = {"base": args.base, "head": args.head, "bundle_commit": None}
    try:
        resolved, body = bounded(lambda: _score_args(args, grep), args.timeout)
        meta.update(resolved)
    except Exception as exc:  # noqa: BLE001  (the scorer never fails a job)
        body = full_verdict(f"{type(exc).__name__}: {exc}")
    return {"schema": SCHEMA, **meta, **body}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--root", default=str(ctm.REPO_ROOT), help="repository root")
    parser.add_argument("--bundle", required=True, help="schema-1 test map / bundle JSON")
    parser.add_argument("--rates", default=str(RATES), help="per-tier failure rates JSON")
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--name-status", help="file holding `git diff --name-status -M` output")
    parser.add_argument("--out", required=True, help="verdict JSON to write")
    parser.add_argument("--timeout", type=float, default=TIMEOUT_SECS, help="seconds")
    args = parser.parse_args(argv)
    if not args.name_status and not (args.base and args.head):
        print("score needs --base and --head, or --name-status", file=sys.stderr)
        return 2
    verdict = run(args)
    try:
        Path(args.out).write_text(json.dumps(verdict, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 0
    print(
        f"verdict: {verdict['verdict']}, would skip {len(verdict['would_skip'])} test files, "
        f"run share {verdict['would_run_share']:.3f}, reason: {verdict['reason']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
