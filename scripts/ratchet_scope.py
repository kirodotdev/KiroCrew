#!/usr/bin/env python3
"""ratchet_scope.py -- one answer to "which files and lines does THIS change touch".

The merge-ref ratchets (``check_black_formatting.py``,
``check_subprocess_encoding.py``, ``check_agent_sdk_boundary.py``,
``check_sync_io_in_async.py``) record a pre-existing violation set as legacy and
then judge only what the change in front of them adds. All four need the same pair
of answers -- the changed-file set and the added-line set -- and they must agree:
a scope fix applied to one private copy and not the others would make the same
added line red under one gate and green under another, for no reason a
contributor could see.

Scope note, so this docstring does not overclaim: the pair here is the
MERGE-REF resolver, used by those four gates. ``check_brand_name.py``,
``check_harness_parity.py`` and ``check_focus_cue.py`` parse added lines too, but
against an env-provided base ref (``*_BASE_REF``, resolved inside Actions) rather
than by discovering the checkout shape, so folding them in is a larger change
than a move and is deliberately not attempted here.

``changed_paths`` names each checkout shape it tries and reports the winner,
because they fail in ways that look alike and an earlier version of the black
gate silently fell back to whole-tree scope on CI. ``added_lines`` reads the diff
endpoints that SAME answer named, so both descriptions are of one diff -- an
added-line set computed against a different base than the changed-file set is
worse than no added-line set at all.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _git(*args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, proc.stdout


def changed_paths() -> tuple[set[str] | None, str]:
    """This change's paths plus how they were determined, for the log.

    Several checkout shapes have to work and they fail in ways that look alike, so
    each attempt is named and the winner is printed. Guessing silently is what let
    an earlier version of the black gate fall back to whole-tree scope on CI
    without saying so, and then report a file the base branch had merged.

    * A ``pull_request`` checkout leaves HEAD as the MERGE commit, whose tree is
      the base tree plus this change. So ``diff HEAD^1 HEAD`` is exactly this
      change and needs no merge base -- only HEAD and its first parent.
    * ``diff HEAD^1 HEAD^2`` is equivalent but needs BOTH parents' trees, which a
      shallow clone may not have.
    * Locally HEAD is the branch tip, so the three-dot diff against the base
      branch is the right question.

    None means undeterminable, and the caller must then judge the whole tree
    rather than nothing: a scope that fails open disables the gate exactly when
    its inputs are unusual.
    """
    code, out = _git("rev-list", "--parents", "-n", "1", "HEAD")
    is_merge = code == 0 and len(out.split()) >= 3
    attempts: list[tuple[str, list[str]]] = []
    if is_merge:
        attempts.append(("merge HEAD^1..HEAD", ["diff", "--name-only", "HEAD^1", "HEAD"]))
        attempts.append(("merge parents", ["diff", "--name-only", "HEAD^1", "HEAD^2"]))
    for base in ("origin/main", "main"):
        attempts.append((f"{base}...HEAD", ["diff", "--name-only", f"{base}...HEAD"]))
    for label, args in attempts:
        code, out = _git(*args)
        if code == 0:
            return {line.strip() for line in out.splitlines() if line.strip()}, label
    return None, "undeterminable (judging the whole tree)"


def added_lines(scope_label: str) -> dict[str, set[int]] | None:
    """Repo-relative path -> line numbers this change ADDED, or None.

    Uses the diff endpoints named by ``changed_paths``' label, so the added set
    and the changed-file set always describe the same diff. An unknown label (or
    a failing git) degrades to None -- the added-line rule is then skipped
    rather than guessed, and a caller's count rules still apply.
    """
    if scope_label == "merge HEAD^1..HEAD":
        args = ["diff", "--unified=0", "HEAD^1", "HEAD"]
    elif scope_label == "merge parents":
        args = ["diff", "--unified=0", "HEAD^1", "HEAD^2"]
    elif scope_label.endswith("...HEAD"):
        args = ["diff", "--unified=0", scope_label]
    else:
        return None
    proc = subprocess.run(
        ["git", "--no-pager", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return None
    added: dict[str, set[int]] = {}
    current: str | None = None
    for line in proc.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("+++ "):
            current = None  # /dev/null or unusual prefix
        elif current is not None:
            match = _HUNK_RE.match(line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2)) if match.group(2) is not None else 1
                added.setdefault(current, set()).update(range(start, start + count))
    return added
