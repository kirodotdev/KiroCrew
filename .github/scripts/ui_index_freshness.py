#!/usr/bin/env python3
"""CI's find_ui index freshness check: fail a PR only when the PR made it stale.

``src/kiro_crew/docs/ui-index.generated.json`` and
``website/src/uiLocations/guidePlans.gen.ts`` are committed and regenerated from
the dashboard source plus the 12 catalogs. Every PR is checked fresh at its OWN
head, but merges onto a ``main`` that has moved since (branch protection does not
require an up-to-date branch), so ``main`` itself goes stale. A whole-tree check
on the merge ref then fails every open PR for a staleness none of them caused.

So this step checks two trees:

- the tree under test (the merge ref on ``pull_request``, the group head on
  ``merge_group``, the pushed commit otherwise);
- on ``pull_request`` / ``merge_group``, when that tree is stale, its BASE (the
  commit the merge ref was computed against).

=====================  ===========  =====================================
tree under test        base         result
=====================  ===========  =====================================
fresh                  (not run)    pass
stale                  fresh        FAIL: this PR changed index inputs
stale                  stale        pass with a warning: main is stale
stale                  unreadable   FAIL: cannot tell, so do not pass
stale, on a push/cron  (not run)    pass with a warning
generator problem      (not run)    FAIL, exactly as before
=====================  ===========  =====================================

The generator says "stale" with exit 3 (``STALE_EXIT`` in
``website/scripts/lib/ui-index-freshness.mjs``). A base from before that exit
code existed exits 1 for both stale and broken, so a 1 whose stderr carries
the stale message and no problem list also reads as stale.

Release builds do not rely on this step: release.yml sets
``KC_UI_INDEX_STRICT=1`` on its build lanes, which makes ``npm run build``
itself fail on a stale index.

    python3 .github/scripts/ui_index_freshness.py
    # env: EVENT_NAME, BASE_SHA (empty when there is no base), GITHUB_WORKSPACE
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

STALE_EXIT = 3
FRESH, STALE, BROKEN = "fresh", "stale", "broken"
_PR_EVENTS = frozenset({"pull_request", "merge_group"})
_STALE_LINE = re.compile(r"gen-ui-index: .* is (stale|missing)[.;]")
_PROBLEM_LINE = re.compile(r"gen-ui-index: \d+ problem\(s\)")
FIX = "run `npm run gen:ui` in website/ and commit the result"


@dataclass(frozen=True)
class Run:
    code: int
    stderr: str


@dataclass(frozen=True)
class Verdict:
    exit_code: int
    level: str  # "notice" | "warning" | "error"
    message: str


def classify(run: Run) -> str:
    """Read one ``gen-ui-index.mjs --check`` run as fresh, stale or broken."""
    if run.code == 0:
        return FRESH
    if run.code == STALE_EXIT:
        return STALE
    if run.code == 1 and _STALE_LINE.search(run.stderr) and not _PROBLEM_LINE.search(run.stderr):
        return STALE
    return BROKEN


def decide(event: str, head: str, base: Optional[str], base_sha: str) -> Verdict:
    """The table in the module docstring. ``base`` is None when it was not checked."""
    if head == FRESH:
        return Verdict(0, "notice", "find_ui index is up to date.")
    if head == BROKEN:
        return Verdict(1, "error", "gen-ui-index --check failed; see its output above.")
    if event not in _PR_EVENTS:
        return Verdict(
            0,
            "warning",
            f"The find_ui index on this commit is stale; {FIX}. Release builds fail on this.",
        )
    if not base_sha:
        return Verdict(
            1,
            "error",
            f"The find_ui index is stale and this {event} run has no base commit to compare; {FIX}.",
        )
    if base == FRESH:
        return Verdict(
            1,
            "error",
            f"This change makes the find_ui index stale: its base {base_sha[:12]} is fresh and the merge result is not. "
            f"This PR changed index inputs; {FIX}.",
        )
    if base == STALE:
        return Verdict(
            0,
            "warning",
            f"The find_ui index is stale, but its base {base_sha[:12]} is already stale, so this PR did not cause it. "
            "Main needs a regen commit (`npm run gen:ui`); nothing to do here.",
        )
    return Verdict(
        1,
        "error",
        f"The find_ui index is stale and its base {base_sha[:12]} could not be checked, so this step cannot tell whether "
        f"this PR caused it; {FIX}, or re-run once the base is checkable.",
    )


Runner = Callable[[Path], Run]


def run_check(website: Path) -> Run:
    proc = subprocess.run(
        ["node", "scripts/gen-ui-index.mjs", "--check"],
        cwd=website,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return Run(proc.returncode, proc.stderr)


def checkout_base(workspace: Path, base_sha: str, dest: Path) -> Optional[Path]:
    """A detached worktree of ``base_sha`` sharing this checkout's node_modules, or None."""
    git = ["git", "-C", str(workspace)]
    for cmd in (
        git + ["fetch", "--no-tags", "--depth=1", "origin", base_sha],
        git + ["worktree", "add", "--detach", str(dest), base_sha],
    ):
        if subprocess.run(cmd, check=False).returncode != 0:
            return None
    modules = workspace / "website" / "node_modules"
    link = dest / "website" / "node_modules"
    if not link.exists():
        link.symlink_to(modules, target_is_directory=True)
    return dest / "website"


def main(
    env: dict[str, str],
    runner: Runner = run_check,
    base_checkout: Callable[[Path, str, Path], Optional[Path]] = checkout_base,
) -> int:
    workspace = Path(env.get("GITHUB_WORKSPACE") or Path.cwd())
    event = env.get("EVENT_NAME", "")
    base_sha = env.get("BASE_SHA", "").strip()
    head = classify(runner(workspace / "website"))
    base: Optional[str] = None
    if head == STALE and event in _PR_EVENTS and base_sha:
        print(
            f"Checking the base {base_sha} to see whether this change caused the staleness...",
            flush=True,
        )
        dest = Path(env.get("RUNNER_TEMP") or "/tmp") / "ui-index-base"
        site = base_checkout(workspace, base_sha, dest)
        base = classify(runner(site)) if site is not None else BROKEN
    verdict = decide(event, head, base, base_sha)
    print(f"::{verdict.level}::{verdict.message}")
    return verdict.exit_code


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
