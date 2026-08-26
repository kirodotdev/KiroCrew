"""Diff-scope — restrict a run to the files changed between two branches.

The self-improvement use case (point the app at its OWN feature branch): discovery
otherwise ranges over the WHOLE target package (``discover_defect_surfaces`` scans
``clone/src/kiro_crew``), so a bug run would fix all of Kiro Crew, not just the app.
Scoping the run to ``diff(base_ref, HEAD)`` confines it to exactly the change set the
branch introduced — i.e. the feature you are dogfooding — without any hand-maintained
path list (the diff self-updates as the branch evolves).

This module owns ONLY the pure "which files changed" computation; the profile consumes
the result to (a) filter ruff discovery surfaces and (b) tighten the edit allowlist so
the agent can edit ONLY scoped files (its own new RED test stays exempt — it is a new
file, not part of the base diff, but a reproducing test must be allowed to ship).

Target-agnostic: it shells only ``git`` (every profile's clone is a git repo); it names
no package path. ``None`` means "unscoped" (the historical whole-package behavior) — an
empty/unset ``base_ref`` or a git failure degrades safely to unscoped rather than
silently fixing nothing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def scoped_relpaths(clone: Path, base_ref: str, *, runner=subprocess.run) -> set[str] | None:
    """Return the set of repo-relative paths changed between ``base_ref`` and the clone's
    current HEAD (``git diff --name-only <base>...HEAD``), or ``None`` when scoping does
    not apply / cannot be computed.

    ``None`` (unscoped) is returned ONLY for a blank ``base_ref`` or a git error — the two
    cases where no scope could be computed at all. A SUCCESSFUL diff always returns a set,
    **including an empty one**.

    That distinction is load-bearing. An earlier revision did ``return files or None``, so a
    valid-but-empty diff (``scopeDiffBase=HEAD``) collapsed into "unscoped" and the edit fence
    silently widened from "what this branch changed" to the WHOLE REPOSITORY — the opposite of
    what setting a scope is for. Measured on a real repo: ``scopeDiffBase='HEAD'`` resolved
    fine, produced an empty diff, and returned ``None``. `set()` now means "scoped to nothing",
    which the gate enforces as "no file may be edited" — a run that can keep nothing, rather
    than one that may edit anything. Raised by the GPT review of this branch.

    ``...`` (three-dot) diffs HEAD against the merge-base of (base, HEAD) — i.e. only what
    THIS branch added, ignoring commits the base advanced past — which is exactly "the
    feature branch's own change set" even when the branch is behind base.
    """
    ref = (base_ref or "").strip()
    if not ref:
        return None
    try:
        proc = runner(
            [
                "git",
                "-C",
                str(clone),
                "-c",
                "core.quotePath=false",
                "diff",
                "--name-only",
                f"{ref}...HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:  # noqa: BLE001 — a git failure degrades to unscoped, never crashes the run
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    # NOT `files or None`: an empty set is a real, successful answer (see above).
    return {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}


def in_scope(relpath: str, scope: set[str] | None) -> bool:
    """True iff ``relpath`` is within ``scope`` (or scope is ``None`` = unscoped).

    The single membership predicate both the discovery filter and the edit allowlist use,
    so "what counts as in-scope" is defined in ONE place. Paths are compared verbatim
    (both sides are repo-relative, forward-slash, as ``git diff --name-only`` emits)."""
    if scope is None:
        return True
    return relpath in scope
