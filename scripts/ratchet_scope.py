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

Two gate families share this module and differ ONLY in how the base is named.
The merge-ref family above discovers the checkout shape itself
(``changed_paths()`` / ``added_lines()``). ``check_brand_name.py``,
``check_harness_parity.py`` and ``check_focus_cue.py`` are handed an explicit
base ref instead (``*_BASE_REF``, resolved inside Actions to the PR's
``base.sha`` so a run never picks up base moves landing after it started); they
use the ``*_at`` entry points below (``resolve_base`` / ``changed_paths_at`` /
``added_lines_at``), which keep that env-provided base semantics while sharing
the diff PARSING — N private parsers meant the same added line could be judged
differently by different gates, and a scope fix to one copy left the others
wrong.

``changed_paths`` names each checkout shape it tries and reports the winner,
because they fail in ways that look alike and an earlier version of the black
gate silently fell back to whole-tree scope on CI. ``added_lines`` reads the diff
endpoints that SAME answer named, so both descriptions are of one diff -- an
added-line set computed against a different base than the changed-file set is
worse than no added-line set at all.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: Set to a non-empty value to make :func:`changed_paths` answer "whole tree".
#:
#: A diff scope is the right question on a pull request and the WRONG one on a
#: push to a branch that is already the base: there ``origin/main...HEAD`` is
#: empty, so every consuming ratchet judges zero files and reports green
#: whatever the tree holds. A caller that means to measure an integrated tree
#: says so here rather than relying on a diff that happens to be empty.
WHOLE_TREE_ENV = "RATCHET_SCOPE_WHOLE_TREE"

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


def _git_strict(*args: str) -> str:
    """Run git, returning stdout and RAISING ``CalledProcessError`` on failure.

    The explicit-base entry points use this instead of :func:`_git` because
    their callers fail CLOSED: an env-base gate that cannot see its base must
    refuse to pass, and the exception carries git's stderr so each gate can
    fold it into its own gate-named message. ``errors="replace"`` for the same
    reason as everywhere else here: ``--text`` makes git emit the content of a
    file that is not valid UTF-8, and a strict decode would raise inside
    ``subprocess`` — a traceback instead of a verdict.
    """
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.stdout


def _first_parent_is_base() -> bool:
    """True when HEAD's FIRST parent is the base branch's commit.

    Two checkout shapes answer "HEAD is a merge", and they need opposite diffs:

    * CI's ``pull_request`` merge ref puts the BASE first, so ``HEAD^1..HEAD``
      is exactly this change.
    * A local ``git merge origin/main`` on a feature branch puts the FEATURE
      tip first, so ``HEAD^1..HEAD`` is only what main brought in and the
      branch's own commits are invisible -- every consuming ratchet then
      under-scopes, and a local run passes a gate CI goes on to fail.

    Which shape this is is decided by which parent the base branch can reach:
    on the CI shape ``HEAD^1`` IS a commit of the base. The base refs tried
    here are the same pair, in the same order, as the three-dot fallback's, so
    a checkout with only a local ``main`` (a merge made ON main has its prior
    tip as ``HEAD^1``, which ``main`` reaches) still recognises the shape. A
    failing git -- neither ref resolvable -- answers False, so an
    unrecognisable merge falls through to the three-dot attempts rather than
    trusting a parent order nothing verified.
    """
    for base in ("origin/main", "main"):
        code, _ = _git("merge-base", "--is-ancestor", "HEAD^1", base)
        if code == 0:
            return True
    return False


def changed_paths() -> tuple[set[str] | None, str]:
    """This change's paths plus how they were determined, for the log.

    Several checkout shapes have to work and they fail in ways that look alike, so
    each attempt is named and the winner is printed. Guessing silently is what let
    an earlier version of the black gate fall back to whole-tree scope on CI
    without saying so, and then report a file the base branch had merged.

    * A ``pull_request`` checkout leaves HEAD as the MERGE commit, whose tree is
      the base tree plus this change. So ``diff HEAD^1 HEAD`` is exactly this
      change -- but only once ``_first_parent_is_base`` confirms, against a
      resolvable base ref, that ``HEAD^1`` really is the base: a local
      ``git merge origin/main`` is ALSO a merge at HEAD, with the parents the
      other way around, and taking ``HEAD^1..HEAD`` there would scope to what
      main brought in instead of this change.
    * ``diff HEAD^1 HEAD^2`` is equivalent but needs BOTH parents' trees, which a
      shallow clone may not have.
    * Locally HEAD is the branch tip -- or an unrecognised merge -- so the
      three-dot diff against the base branch is the right question.

    None means undeterminable, and the caller must then judge the whole tree
    rather than nothing: a scope that fails open disables the gate exactly when
    its inputs are unusual.

    ``WHOLE_TREE_ENV`` short-circuits to that same None, for a caller whose
    question really is about a whole tree. It is checked FIRST so the answer
    cannot depend on which checkout shape a run happens to have -- an empty
    diff is indistinguishable from a clean change, and that ambiguity is what
    the override exists to remove.
    """
    if os.environ.get(WHOLE_TREE_ENV, "").strip():
        return None, f"whole tree ({WHOLE_TREE_ENV} set)"
    code, out = _git("rev-list", "--parents", "-n", "1", "HEAD")
    is_merge = code == 0 and len(out.split()) >= 3
    attempts: list[tuple[str, list[str]]] = []
    if is_merge and _first_parent_is_base():
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


# ---------------------------------------------------------------------------
# Explicit-base entry points, for the env-base gate family
# ---------------------------------------------------------------------------


def parse_added_lines(diff_text: str, *, anchor_deletions: bool = False) -> set[int]:
    """1-based post-image line numbers the hunk headers in ``diff_text`` mark.

    For ONE file's ``--unified=0`` diff: only ``@@`` headers are read, so the
    caller must have scoped the diff to a single path
    (``git diff <frm> -- <path>``). There is deliberately no ``+++``
    attribution here — git QUOTES a path holding a non-ASCII byte on those
    lines, and a ``+++ b/`` parser silently drops that file's hunks. Path
    discovery belongs to :func:`changed_paths_at`, whose ``-z`` output is
    never quoted.

    A deletion-only hunk reads ``+<start>,0``: the change removed lines and
    added none, so by default it contributes nothing (``range(start, start)``
    is empty). ``anchor_deletions=True`` records ``start`` instead, for a gate
    that must see WHERE lines were removed — the focus-cue gate exists to
    catch a deleted cue line, which is invisible to the pure added set.
    """
    lines: set[int] = set()
    for raw in diff_text.splitlines():
        match = _HUNK_RE.match(raw)
        if match is None:
            continue
        start = int(match.group(1))
        count = int(match.group(2)) if match.group(2) is not None else 1
        if count == 0:
            if anchor_deletions:
                lines.add(start)
            continue
        lines.update(range(start, start + count))
    return lines


def resolve_base(base: str) -> str:
    """The commit an env-provided base ref measures against.

    ``merge-base`` is the honest divergence point, but a shallow CI clone
    fetches the base commit as its own tip with no shared history, so it often
    has none — the base tip is then the fallback. This is resolution, not
    parsing: it is the one step the env-base family does differently from the
    resolver above, so it stays a separate function the gates call once per
    run.
    """
    code, out = _git("merge-base", base, "HEAD")
    return out.strip() if code == 0 else base


def changed_paths_at(frm: str) -> list[str]:
    """Paths the diff from ``frm`` to the WORKING TREE touches, in git's order.

    The explicit-base counterpart of :func:`changed_paths`: the caller names
    the base (CI passes the PR's ``base.sha`` through ``*_BASE_REF``, so a run
    never picks up base moves landing after it started) and applies its own
    scope filter to the returned paths. ``-z`` is what makes the answer
    trustworthy: without it git quotes any path holding an unusual byte, and a
    parser reading quoted output silently drops that file — a gate that skips
    a changed file is worse than no gate. ``--diff-filter=d`` drops deletions:
    a removed file has no lines to judge.

    A list, not a set, because git emits the names path-sorted and the
    consuming gates' reports inherit that order — a set would make a
    violation listing nondeterministic. A failing git raises
    ``subprocess.CalledProcessError`` so each caller can fail CLOSED with its
    own gate-named message; unlike :func:`changed_paths`, which degrades to
    whole-tree scope, an env-base gate that cannot see its base must refuse to
    pass, not widen.
    """
    out = _git_strict("diff", "--name-only", "-z", "--diff-filter=d", frm)
    return [p for p in out.split("\0") if p]


def added_lines_at(frm: str, path: str, *, anchor_deletions: bool = False) -> set[int]:
    """1-based line numbers the diff from ``frm`` to the working tree adds.

    Base-to-working-tree, so a local run sees edits that are not committed
    yet, which is the only form in which a local run is useful; CI checks out
    a clean tree where that equals base-to-HEAD. ``--text`` forces hunks even
    for a path ``.gitattributes`` marks ``-diff``: git would otherwise report
    only "Binary files differ", leaving nothing to scan and passing the file
    silently. Per PATH rather than whole-diff, matching how the env-base gates
    consume it — see :func:`parse_added_lines` for why single-path diffs need
    no ``+++`` attribution.
    """
    diff = _git_strict("diff", "--unified=0", "--no-color", "--text", frm, "--", path)
    return parse_added_lines(diff, anchor_deletions=anchor_deletions)
