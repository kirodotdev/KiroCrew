"""The `windows` block of the SHADOW verdict: the test files a diff needs on Windows.

SHADOW ONLY: `scripts/ci_test_selection.py` writes the block into its verdict and
`scripts/ci_test_selection_ledger.py` scores it against what failed on Windows.
No lane runs fewer tests because of it.

    "windows": {"verdict": "select" | "full", "reason": str | null,
                "run": [test file, ...], "would_run_share": float}

`run`, under `select`, is the union of
1. the test files the diff changes;
2. the bundle's `windows_targeting` test files the scorer put in tiers A to D;
3. the whole guard tier: it is there for what the map cannot see reach a test,
   and an OS effect is of that kind;
4. tiers A to D of the OS-sensitive changed files scored as a diff of their own:
   a changed `.py` file the bundle's `os_sensitive` table names, or one whose
   added lines hit a pattern of `ci_test_os_tables` (W1, W2 or W3). The added
   lines are matched as they are, comments included, and with no added lines
   to read every changed `.py` file counts: both err toward more tests;
minus the files Windows never collects (`test/windows-collect-ignore.txt`).

The verdict is `full`, first match wins, when
* the Linux verdict is `full` (`linux_full`);
* a changed path is in the `windows` bucket of ci.yml's `changes` job, read from
  that file so the list lives in one place (`windows_path`);
* the bundle lacks `os_sensitive` or `windows_targeting` (`table_absent`);
* the bundle's `built_at` is more than `STALE_DAYS` days before the head commit
  (now, for a diff with no head), or is missing or unreadable (`bundle_stale`);
* anything here raises (`error`).

`would_run_share` weighs each test file Windows collects by its node-id count
in the map (1 for a file the map lacks), as the Linux share does.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import ci_test_map as ctm
import ci_test_os_tables as ost

# A bundle older than this against the diff's head scores nothing: the tables
# describe a tree the diff no longer sits on. The nightly rebuild makes 7 a
# week of failed builds.
STALE_DAYS = 7
RUN_TIERS = ("A", "B", "C", "D")
TABLES = ("os_sensitive", "windows_targeting")
CI_WORKFLOW = ".github/workflows/ci.yml"
GIT_TIMEOUT_SECS = 30.0

Changes = list[tuple[str, list[str]]]


def full(reason: str) -> dict[str, Any]:
    """The block that runs every Windows test, and why."""
    return {"verdict": "full", "reason": reason, "run": [], "would_run_share": 1.0}


def path_bucket(root: Path) -> list[str]:
    """The globs of the `windows` bucket in ci.yml's `changes` job path filter.

    Read line by line (the scorer is stdlib only): the `windows:` key, then its
    quoted list items up to the next key. Anything else there raises, so a
    bucket this cannot read is never taken for an empty one.
    """
    globs: list[str] = []
    indent: int | None = None
    for line in (root / CI_WORKFLOW).read_text(encoding="utf-8").splitlines():
        body = line.strip()
        if indent is None:
            if body == "windows:":
                indent = len(line) - len(line.lstrip())
            continue
        if not body or body.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= indent:
            break
        item = re.fullmatch(r"-\s+'([^']+)'", body)
        if item is None:
            raise ValueError(f"{CI_WORKFLOW}: cannot read the windows filter line {body!r}")
        globs.append(item.group(1))
    if not globs:
        raise ValueError(f"{CI_WORKFLOW}: no windows path filter")
    return globs


def glob_pattern(glob: str) -> re.Pattern[str]:
    """A path filter glob as a regex: `**/` any folders, `**` anything, `*` within one name."""
    parts: list[str] = []
    at = 0
    while at < len(glob):
        for token, regex in (("**/", r"(?:.*/)?"), ("**", r".*"), ("*", r"[^/]*")):
            if glob.startswith(token, at):
                parts.append(regex)
                at += len(token)
                break
        else:
            parts.append(re.escape(glob[at]))
            at += 1
    return re.compile("".join(parts))


def added_lines(diff: str) -> dict[str, str]:
    """`git diff -U0` text -> {path: the lines the diff adds to it}."""
    found: dict[str, list[str]] = {}
    path: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif line.startswith("+") and path is not None:
            found.setdefault(path, []).append(line[1:])
    return {name: "\n".join(lines) + "\n" for name, lines in found.items()}


def git_added_lines(base: str, head: str, root: Path) -> dict[str, str]:
    """`added_lines` of the `.py` files `base..head` changes."""
    out = subprocess.run(
        ["git", "diff", "-U0", "--no-color", "--no-ext-diff", f"{base}..{head}", "--", "*.py"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=GIT_TIMEOUT_SECS,
    ).stdout
    return added_lines(out)


def commit_time(ref: str, root: Path) -> datetime:
    """When the commit `ref` names was committed."""
    out = subprocess.run(
        ["git", "show", "-s", "--format=%cI", f"{ref}^{{commit}}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=GIT_TIMEOUT_SECS,
    ).stdout
    return datetime.fromisoformat(out.strip())


def is_stale(built_at: object, at: datetime) -> bool:
    """Whether a bundle built at `built_at` is too old for a diff whose head is `at`.

    A bundle that does not say when it was built, in a form with a UTC offset,
    is stale: its age is unknown.
    """
    if not isinstance(built_at, str):
        return True
    try:
        built = datetime.fromisoformat(built_at)
        return (at - built).total_seconds() > STALE_DAYS * 86400
    except (ValueError, TypeError):
        return True


def os_sensitive_changes(
    changes: Changes, table: Mapping[str, Any], added: Mapping[str, str] | None
) -> Changes:
    """The changed `.py` files, tests apart, that can act differently on Windows."""
    picked: Changes = []
    for status, paths in changes:
        path = paths[-1] if paths else ""
        if not path.endswith(".py") or ctm.is_test_file(path):
            continue
        if path in table or added is None or ost.tier_of(added.get(path, "")) is not None:
            picked.append((status, paths))
    return picked


def block(
    changes: Changes,
    linux: Mapping[str, Any],
    bundle: Mapping[str, Any],
    universe: Iterable[str],
    guard: Iterable[str],
    *,
    added: Mapping[str, str] | None,
    bucket: Iterable[str],
    ignored: set[str],
    at: datetime,
    score: Callable[[Changes], Mapping[str, Any]],
) -> dict[str, Any]:
    """The `windows` block for one diff the scorer gave the verdict body `linux`.

    `guard` is the guard tier the scorer used. `score` tiers a diff as the scorer
    did this one; `added` is `added_lines` of the diff, None when there are none
    to read; `at` is the head's commit time.
    """
    if linux["verdict"] != "select":
        return full(f"linux_full: {linux['reason']}")
    patterns = [(glob, glob_pattern(glob)) for glob in bucket]
    for _status, paths in changes:
        for path in paths:
            hit = next((glob for glob, pattern in patterns if pattern.fullmatch(path)), None)
            if hit is not None:
                return full(f"windows_path: {path} is in the bucket ({hit})")
    for table in TABLES:
        if not bundle.get(table):
            return full(f"table_absent: {table}")
    if is_stale(bundle.get("built_at"), at):
        return full(f"bundle_stale: built_at is {bundle.get('built_at')!r}, the head is {at}")

    def run_tiers(body: Mapping[str, Any]) -> set[str]:
        return {test for tier in RUN_TIERS for test in body["tiers"][tier]}

    run = {paths[-1] for _status, paths in changes if paths and ctm.is_test_file(paths[-1])}
    run |= set(bundle["windows_targeting"]) & run_tiers(linux)
    run |= set(guard)
    sensitive = os_sensitive_changes(changes, bundle["os_sensitive"], added)
    if sensitive:
        scored = score(sensitive)
        if scored["verdict"] != "select":
            return full(f"linux_full: {scored['reason']}")
        run |= run_tiers(scored)
    run -= ignored
    counts: Mapping[str, int] = bundle.get("tests", {})
    collected = (set(universe) | run) - ignored
    total = sum(counts.get(test, 1) for test in collected)
    share = sum(counts.get(test, 1) for test in run) / total if total else 1.0
    return {"verdict": "select", "reason": None, "run": sorted(run), "would_run_share": share}
