#!/usr/bin/env python3
"""check_feature_map.py — keep ``docs/feature-map/README.md`` fresh.

The map is a lookup table from a user-facing dashboard feature to the page and
handler that own it. Its failure mode is silent: a feature lands, no row is
added, and the map degrades into something readers stop trusting. This gate
makes that specific omission visible.

## What it fails on, and why only that

A feature arrives or departs as a **file appearing or disappearing** under the
frontend pages tree or the dashboard handlers tree, or as a **route entry
appearing or disappearing** in the router. Edits to files that already exist are
how a feature *changes behavior*, which the map deliberately does not describe —
so an edit-only diff never trips this, with one carve-out: rewriting a ``<Route ``
line in the router reads as a destination swap, because that is what it almost
always is.

Drawing the line at structure rather than content is what keeps the gate worth
having. A gate that fired on every edit to a page would demand a map review on
every UI fix, and a map re-reviewed that often is one nobody reads. The judgment
half — is the row's content still true — belongs to the reviewer; this half only
guarantees somebody was asked.

## Usage

    # enforce against the merge base (exit 1 when a structural change skips the map)
    FEATURE_MAP_BASE_REF=origin/main python3 scripts/check_feature_map.py

    # no base ref: report the map's presence and size, enforce nothing (exit 0)
    python3 scripts/check_feature_map.py

    # self-test: one probe per trigger rule, each asserted
    python3 scripts/check_feature_map.py --test

## Fail-open on an unreadable diff

Unlike the brand and harness gates, this one exits 0 when git cannot produce a
diff. Those gates protect an invariant a bad line would carry into ``main``
forever; this one protects a documentation habit, and a shallow clone or a
missing base commit is not evidence that the habit was broken. Blocking every
PR on a git edge case would cost more than the freshness it buys, so the
verdict is reported and the build continues.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The map itself. A diff that touches this path has had the review this gate
# exists to prompt, whatever else it changed.
MAP_PATH = "docs/feature-map/README.md"

# Trees whose FILE SET is the feature inventory. A page module appearing under
# the first is a new destination; a handler module appearing under the second is
# a new backend surface. Both are directory prefixes, so a nested page
# (``pages/settings/BrowserPanel.tsx``) counts the same as a top-level one.
WATCHED_DIRS = (
    "website/src/pages/",
    "src/kiro_crew/dashboard/handlers/",
)

# Files under WATCHED_DIRS that are not features. Tests and helper modules come
# and go with refactors that add no destination, and treating their arrival as a
# feature would train reviewers to touch the map to silence a false positive —
# which is exactly how a gate stops meaning anything.
#
# Art is the same case and arrives the same way. An image is consumed BY a
# feature rather than being one: it adds no destination, no handler and no
# endpoint, so there is nothing for a map row to say about it beyond what the
# row for its consuming page already says. `website/src/pages/connections/logos/`
# is the live example — a provider gallery whose brand marks live beside the page
# that renders them, where adding one mark to an existing card is a change of
# appearance, not of inventory.
IGNORED_SUFFIXES = (
    ".test.tsx",
    ".test.ts",
    ".spec.tsx",
    ".spec.ts",
    ".snap",
    ".svg",
)
IGNORED_NAMES = (
    "__init__.py",
    "_shared.py",
    "index.ts",
)

# The router. Route entries are counted, not parsed: a `<Route ` occurrence is one
# registration, so the `+`/`-` lines carrying that token in the router's own diff
# are what say a destination arrived or left. The two directions are counted
# SEPARATELY — a swap adds one entry and removes another, and netting the pair to
# zero is exactly the staleness this gate used to wave through (see `classify`).
#
# Counting rather than extracting paths keeps the rule cheap and total. It costs a
# rewritten `<Route ` line reading as a swap even when the destination set is
# unchanged, which is the right side to err on: a rewritten route path is what
# invalidates the map's "how a user reaches it" column.
ROUTER_PATH = "website/src/App.tsx"
ROUTE_TOKEN = "<Route "


@dataclass(frozen=True)
class Verdict:
    """The gate's answer, and the evidence for it."""

    ok: bool
    added: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    routes_added: int = 0
    routes_removed: int = 0


def is_feature_file(path: str) -> bool:
    """Is this path one whose existence the map is expected to account for?"""
    if not path.startswith(WATCHED_DIRS):
        return False
    if path.endswith(IGNORED_SUFFIXES):
        return False
    return os.path.basename(path) not in IGNORED_NAMES


def classify(
    added: list[str],
    deleted: list[str],
    routes_added: int,
    routes_removed: int,
    map_touched: bool,
) -> Verdict:
    """The whole rule, as a pure function of five facts.

    Kept free of git so the trigger matrix can be pinned by tests that build no
    repository: every branch here is reachable from plain arguments.

    Route additions and removals are counted SEPARATELY rather than netted. A
    diff that removes one route and adds another leaves the router's total
    unchanged, but the map's row for the old destination is now wrong and the new
    one has no row at all — netting would read that as "nothing structural
    happened" and let the staleness through.
    """
    features_added = tuple(p for p in added if is_feature_file(p))
    features_deleted = tuple(p for p in deleted if is_feature_file(p))
    structural = bool(features_added or features_deleted or routes_added or routes_removed)
    return Verdict(
        ok=map_touched or not structural,
        added=features_added,
        deleted=features_deleted,
        routes_added=routes_added,
        routes_removed=routes_removed,
    )


# ---------------------------------------------------------------------------
# Git plumbing
# ---------------------------------------------------------------------------


class DiffUnreadable(Exception):
    """Git could not answer. The caller reports and exits 0 — see the module docstring."""


def git(args: list[str]) -> str:
    """Run git, raising :class:`DiffUnreadable` on any failure.

    ``errors="replace"`` because ``git show`` emits file content, which may not
    be valid UTF-8; a strict decode would raise inside ``subprocess`` and turn a
    verdict into a traceback. Everything parsed here (name-status records,
    NUL-separated paths) is ASCII.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            encoding="utf-8",
            errors="replace",
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        raise DiffUnreadable(str(exc)) from exc
    return proc.stdout


_SCOPE_MODULE = None


def _scope():
    """The shared diff plumbing (``scripts/ratchet_scope.py``).

    Loaded by path rather than imported: ``scripts/`` is not a package, so a
    plain import would resolve only by accident of ``sys.path[0]``, and not at
    all when a test loads this gate by path. Shared with the other env-base
    gates so they cannot come to disagree about what a change touched.
    """
    global _SCOPE_MODULE
    if _SCOPE_MODULE is None:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ratchet_scope.py")
        spec = importlib.util.spec_from_file_location("ratchet_scope", script)
        if spec is None or spec.loader is None:
            raise DiffUnreadable(f"cannot load {script}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SCOPE_MODULE = module
    return _SCOPE_MODULE


def diff_base(base: str) -> str:
    """The commit to measure against (``ratchet_scope.resolve_base``)."""
    return _scope().resolve_base(base)


def name_status(frm: str) -> tuple[list[str], list[str], list[str]]:
    """``(added, deleted, modified)`` paths between ``frm`` and the working tree.

    ``--name-status -z`` rather than the shared ``changed_paths_at`` because that
    helper drops deletions (``--diff-filter=d``) — and a deleted page is half of
    what this gate watches for. ``-z`` also keeps a path holding an unusual byte
    from being quoted, which a naive parser silently skips: a gate that misses
    the one file that mattered is worse than no gate.

    ``-M`` so a pure rename reads as one ``R`` record instead of an add plus a
    delete, which is what lets the source and destination be told apart. The
    source is returned as a DELETION and the destination as an ADDITION, so a
    renamed page fires the gate: the map's page column cites the filename, so that
    row now points at a file which no longer exists while the file that does exist
    has no row. Rename detection therefore changes the reporting rather than the
    verdict — without it the same change arrives as a delete plus an add and fires
    too. A copy adds its destination and leaves its source in place.

    UNTRACKED files count as additions. ``git diff`` cannot see them at all, so
    without this a local pre-commit run reports clean on exactly the change the
    gate exists to catch — a brand-new page file — and only CI would ever fail.
    CI checks out a committed tree where the untracked set is empty, so this
    widens the local answer without changing the enforced one.
    """
    raw = git(["diff", "--name-status", "-z", "-M", frm])
    fields = [f for f in raw.split("\0") if f]
    added: list[str] = []
    deleted: list[str] = []
    modified: list[str] = []
    i = 0
    while i < len(fields):
        code = fields[i]
        # Rename and copy records carry TWO paths (old, new); everything else one.
        # A rename is structurally a delete plus an add — the map's row for the old
        # filename is now wrong and the new one has no row — so the two paths are
        # classified separately rather than both landing in `modified`, which would
        # read a renamed page as an edit and let the staleness through. A copy adds
        # its destination and leaves its source in place.
        if code[:1] in ("R", "C"):
            if i + 2 >= len(fields):
                break
            if code[:1] == "R":
                deleted.append(fields[i + 1])
            added.append(fields[i + 2])
            i += 3
            continue
        if i + 1 >= len(fields):
            break
        path = fields[i + 1]
        if code[:1] == "A":
            added.append(path)
        elif code[:1] == "D":
            deleted.append(path)
        else:
            modified.append(path)
        i += 2
    untracked = git(["ls-files", "--others", "--exclude-standard", "-z"])
    added.extend(p for p in untracked.split("\0") if p and p not in added)
    return added, deleted, modified


def router_delta(frm: str) -> tuple[int, int]:
    """``(routes_added, routes_removed)`` in the router since ``frm``.

    Read from the router's own unified diff rather than by differencing two file
    totals: a swap adds one entry and removes another, which a total-count
    difference reports as zero change while both of the map's affected rows are
    wrong.
    """
    patch = git(["diff", "--unified=0", frm, "--", ROUTER_PATH])
    routes_added = routes_removed = 0
    for line in patch.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+") and ROUTE_TOKEN in line:
            routes_added += 1
        elif line.startswith("-") and ROUTE_TOKEN in line:
            routes_removed += 1
    return routes_added, routes_removed


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report(verdict: Verdict) -> int:
    if verdict.ok:
        print(f"feature-map gate: {MAP_PATH} accounts for this change ✓")
        return 0

    print(
        "::error::feature-map gate: this change adds or removes a dashboard "
        f"feature but leaves {MAP_PATH} untouched."
    )
    for path in verdict.added:
        print(f"  added    {path}")
    for path in verdict.deleted:
        print(f"  deleted  {path}")
    if verdict.routes_added or verdict.routes_removed:
        # Both numbers, always, whenever either is non-zero: `+1 / -1` is what
        # makes a swap legible, and it is the shape a single netted figure hid.
        print(
            f"  router   +{verdict.routes_added} / -{verdict.routes_removed} "
            f"route entries in {ROUTER_PATH}"
        )
    print(
        f"\nUpdate {MAP_PATH}: add a row for each new feature (name, one-line "
        "what-it-is, how a user reaches it, frontend page, backend handler, key "
        "endpoints) and delete the row for each feature that went away.\n"
        "\nIf nothing user-facing changed — a helper module, a split, an internal "
        "route — say so in the row the new file belongs to, or add the file's "
        "suffix to IGNORED_SUFFIXES in scripts/check_feature_map.py when it can "
        "never be a feature.\n"
        "\nThe rule is structural on purpose: only files APPEARING or "
        "DISAPPEARING under "
        f"{', '.join(WATCHED_DIRS)} trip this. Editing an existing page or "
        "handler never does."
    )
    return 1


def enforce(base: str) -> int:
    frm = diff_base(base)
    added, deleted, _modified = name_status(frm)
    map_touched = MAP_PATH in added or MAP_PATH in deleted or MAP_PATH in _modified
    routes_added, routes_removed = router_delta(frm)
    return report(classify(added, deleted, routes_added, routes_removed, map_touched))


def survey() -> int:
    """Non-enforcing: confirm the map exists and report its size."""
    try:
        with open(os.path.join(REPO_ROOT, MAP_PATH), encoding="utf-8") as fh:
            lines = fh.read().count("\n") + 1
    except OSError:
        print(f"::error::feature-map gate: {MAP_PATH} is missing.")
        return 1
    print(
        f"::notice::feature-map gate report: {MAP_PATH} is present "
        f"({lines} lines). Not enforced here; set FEATURE_MAP_BASE_REF to gate "
        "a change."
    )
    return 0


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

# (label, added, deleted, routes_added, routes_removed, map_touched, expect_ok)
#
# The two route counts are separate columns because a swap — one entry out, one in
# — is a real structural change that a single netted figure reported as zero. The
# `route-swapped-*` pair below is the regression that costs the most to lose.
PROBES: tuple[tuple[str, list[str], list[str], int, int, bool, bool], ...] = (
    ("page-added-no-map", ["website/src/pages/NewPage.tsx"], [], 0, 0, False, False),
    ("page-added-with-map", ["website/src/pages/NewPage.tsx"], [], 0, 0, True, True),
    ("nested-page-added", ["website/src/pages/settings/NewPanel.tsx"], [], 0, 0, False, False),
    ("page-deleted-no-map", [], ["website/src/pages/OldPage.tsx"], 0, 0, False, False),
    (
        "handler-added-no-map",
        ["src/kiro_crew/dashboard/handlers/new_thing.py"],
        [],
        0,
        0,
        False,
        False,
    ),
    (
        "handler-deleted-no-map",
        [],
        ["src/kiro_crew/dashboard/handlers/old_thing.py"],
        0,
        0,
        False,
        False,
    ),
    ("route-added-no-map", [], [], 1, 0, False, False),
    ("route-removed-no-map", [], [], 0, 1, False, False),
    # The swap. Both of the map's affected rows are wrong — the retired
    # destination's row still claims it exists, and the new one has no row at all.
    ("route-swapped-no-map", [], [], 1, 1, False, False),
    ("route-swapped-with-map", [], [], 1, 1, True, True),
    ("route-added-with-map", [], [], 1, 0, True, True),
    # Edits only. `name_status` puts modifications in neither list, so an
    # edit-only diff reaches classify() with both empty.
    ("edit-only", [], [], 0, 0, False, True),
    # Non-features under a watched tree.
    ("test-file-added", ["website/src/pages/NewPage.test.tsx"], [], 0, 0, False, True),
    ("snapshot-added", ["website/src/pages/__snapshots__/x.snap"], [], 0, 0, False, True),
    # Art beside the page that renders it. A brand mark is consumed by a feature
    # rather than being one, so its arrival owes the map nothing.
    ("page-asset-added", ["website/src/pages/connections/logos/gitlab.svg"], [], 0, 0, False, True),
    ("handler-init-added", ["src/kiro_crew/dashboard/handlers/__init__.py"], [], 0, 0, False, True),
    ("pages-index-added", ["website/src/pages/overview/index.ts"], [], 0, 0, False, True),
    # Outside the watched trees entirely.
    ("component-added", ["website/src/components/NewThing.tsx"], [], 0, 0, False, True),
    ("unrelated-module-added", ["src/kiro_crew/memory.py"], [], 0, 0, False, True),
    ("doc-added", ["docs/guides/new.md"], [], 0, 0, False, True),
)


def self_test() -> int:
    failures = 0
    for label, added, deleted, r_added, r_removed, touched, expect_ok in PROBES:
        got = classify(added, deleted, r_added, r_removed, touched)
        if got.ok != expect_ok:
            want = "pass" if expect_ok else "fail"
            print(f"  FAIL {label}: expected {want}, got {'pass' if got.ok else 'fail'}")
            failures += 1
        else:
            print(f"  ok   {label}")

    # The failure report must NAME what triggered it. A gate that fails without
    # saying which path caused it sends the reader back to the diff.
    triggered = classify(["website/src/pages/A.tsx"], ["website/src/pages/B.tsx"], 2, 1, False)
    if triggered.added != ("website/src/pages/A.tsx",):
        print(f"  FAIL evidence-added: got {triggered.added}")
        failures += 1
    elif triggered.deleted != ("website/src/pages/B.tsx",):
        print(f"  FAIL evidence-deleted: got {triggered.deleted}")
        failures += 1
    elif triggered.routes_added != 2:
        print(f"  FAIL evidence-routes-added: got {triggered.routes_added}")
        failures += 1
    elif triggered.routes_removed != 1:
        print(f"  FAIL evidence-routes-removed: got {triggered.routes_removed}")
        failures += 1
    else:
        print("  ok   evidence-carried")

    # The map's own path must be watched by the touch test, not by is_feature_file:
    # it lives under neither watched tree, so it can never be its own trigger.
    if is_feature_file(MAP_PATH):
        print(f"  FAIL map-not-a-feature: {MAP_PATH} classified as a feature file")
        failures += 1
    else:
        print("  ok   map-not-a-feature")

    print("self-test passed" if not failures else f"self-test FAILED ({failures})")
    return 1 if failures else 0


def force_utf8_output() -> None:
    """Print UTF-8 whatever the console default is.

    The clean verdict ends in a check mark and a violation can name a non-ASCII
    path. Windows consoles default to cp1252 and raise ``UnicodeEncodeError`` on
    either, turning a pass into a traceback.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    force_utf8_output()
    if "--test" in argv:
        return self_test()

    base = os.environ.get("FEATURE_MAP_BASE_REF", "").strip()
    if not base:
        return survey()

    try:
        return enforce(base)
    except DiffUnreadable as exc:
        # Fail OPEN: see the module docstring. A git edge case is not evidence
        # that the map went stale, and this gate guards a habit, not an invariant.
        print(
            f"::notice::feature-map gate: cannot diff against {base}, so the map "
            f"was not checked. Passing rather than blocking on a git edge case.\n"
            f"  {exc}"
        )
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
