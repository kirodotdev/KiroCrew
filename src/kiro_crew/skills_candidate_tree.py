"""Reading a pending candidate's tree without following a link: the link screen, the top-level
layout both the verdict and the approve paths enforce, the script collection walks, and the
entry and depth budgets they share.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from kiro_crew import pinned_fs
from kiro_crew.platform_compat import PinnedDirectory, is_link_or_junction
from kiro_crew.skills_script_validator import MAX_SCRIPT_BYTES

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


# One script-entry population budget for the pending verdict and API reports.
# Reports may additionally retain ONE fixed truncation-summary entry.
_PENDING_SCRIPT_MAX_ENTRIES = 64
# Depth bound shared by BOTH pinned traversals of a pending candidate -- the
# verdict walk and the detail read. A candidate tree is written directly by an
# agent, so a nesting chain is free to produce; bounding well below Python's
# recursion limit keeps it from turning a read into a crash. One constant because
# the two walks must agree: a tree the verdict already declines to judge must not
# be one the detail read still descends.
_PENDING_SCRIPT_MAX_DEPTH = 8


def _candidate_has_symlink(pdir: Path) -> bool:
    """True if the candidate dir itself or any entry under it is a link —
    so the read/approve paths never follow an LLM-planted link to a
    sensitive file. (Scripts always require human review before going live;
    this is defense-in-depth, not the primary control.)

    "Link" is :func:`platform_compat.is_link_or_junction`, not
    ``os.path.islink``: a Windows directory junction is a reparse point
    ``islink`` reports as a plain directory, so the fence answered False for
    one AND ``os.walk`` descended through it -- the read path then reached
    whatever it pointed at, which is the exact escape this refuses.
    Directories are tested before files because a topdown walk offers a
    directory in ``dirs`` before descending into it, so answering there is
    what keeps this from ever walking THROUGH a link to reach its verdict.
    """
    if is_link_or_junction(pdir):
        return True
    for root, dirs, files in os.walk(pdir):
        for nm in list(dirs):
            if is_link_or_junction(os.path.join(root, nm)):
                return True
        for nm in files:
            if is_link_or_junction(os.path.join(root, nm)):
                return True
    return False


def _collect_scripts(sdir: Path) -> list[dict]:
    """Recursively collect ``{filename, content}`` for every regular file
    under ``sdir`` (relative filenames). Recursion + symlink-skip ensure a
    nested script (``scripts/nested/evil.py``) can't evade validation or
    review by hiding below the top level."""
    out: list[dict] = []
    if not sdir.is_dir():
        return out
    for root, _dirs, files in os.walk(sdir):
        for nm in sorted(files):
            fp = Path(root) / nm
            if fp.is_file() and not fp.is_symlink():
                try:
                    out.append(
                        {
                            "filename": str(fp.relative_to(sdir)),
                            "content": fp.read_text(encoding="utf-8"),
                        }
                    )
                except OSError:
                    continue
    return out


def _collect_scripts_pinned(
    self,
    pinned: PinnedDirectory,
    rel: tuple[str, ...],
    out: list[dict],
    budget: dict[str, int],
) -> bool:
    """Collect ``{filename, content}`` under *pinned*, refusing a link anywhere.

    Returns False when the tree is not readable as plain files and directories, or
    when it does not fit the budget, which the caller turns into a refusal of the
    whole candidate. Relative filenames carry the platform separator, because they
    are served through the API.

    Every bound here is the verdict walk's bound, deliberately: the SAME
    ``_PENDING_SCRIPT_MAX_ENTRIES`` entries and the same ``MAX_SCRIPT_BYTES`` per
    file. Those two ARE the bound on what this accumulates -- at most
    ``entries * per-file`` bytes -- so there is deliberately no third, aggregate
    check: with both of the above in force it could never fire, and a guard that
    cannot fire reads as protection while providing none. This runs per request
    against a tree an agent writes directly, so reusing the verdict's numbers is
    also what keeps the two from disagreeing about which candidates are readable.
    *budget* is threaded through the recursion rather than recreated per directory,
    or a planted tree of many small directories would each get a fresh allowance and
    the total would be unbounded again. Names are enumerated LAZILY against the
    entry cap for the same reason the verdict walk does it: an eager
    ``sorted(names())`` spends the allocation before any budget can refuse it.

    Depth is bounded by ``_PENDING_SCRIPT_MAX_DEPTH``, the same cap the verdict
    walk uses, and bounded the SAME WAY: that walk refuses to descend when the
    directory it is standing in is already at the cap, so it enumerates the level
    AT the cap. The comparison here is ``>`` rather than ``>=`` for exactly that
    reason -- one level tighter would refuse a tree the verdict judged fine, and
    the detail read would answer 404 for a candidate that is waiting for review. The tree is written directly by an agent, so a nesting chain deep
    enough to exhaust the interpreter's recursion limit is free to produce, and
    the resulting ``RecursionError`` would surface as a failed request rather
    than a refused candidate. Refusing at the cap also keeps the two traversals
    in step: a tree the verdict declines to judge is not one this still reads.
    """
    if len(rel) > _PENDING_SCRIPT_MAX_DEPTH:
        return False
    listed = pinned.names_bounded(_PENDING_SCRIPT_MAX_ENTRIES)
    if listed is None:
        return False
    for name in sorted(listed):
        if pinned.is_link(name):
            return False
        budget["entries"] += 1
        if budget["entries"] > _PENDING_SCRIPT_MAX_ENTRIES:
            # Directories spend the entry budget too, so a planted tree of many
            # nearly-empty ones cannot buy traversal the files could not.
            return False
        if pinned.is_dir(name):
            sub = pinned.child_if_real_dir(name)
            if sub is None:
                # The open refusing IS the protection: what is at the name is not
                # the real directory it screened as. Refuse the candidate.
                return False
            with sub:
                if not self._collect_scripts_pinned(sub, (*rel, name), out, budget):
                    return False
            continue
        try:
            text = pinned.read_text(name, max_bytes=MAX_SCRIPT_BYTES)
        except (OSError, UnicodeDecodeError):
            # ONE refusal policy, so nothing here reasons about error classes.
            # An entry that cannot be read as text is not shown to the reviewer,
            # and this API exists for INFORMED approval -- silently omitting it
            # would let a clean-looking detail stand for a candidate that approve
            # then refuses, or worse, promotes unreviewed. A hardlink, a
            # non-regular entry and one over the per-file cap arrive here too,
            # each refused by the read itself.
            return False
        out.append(
            {
                "filename": os.path.join(*rel, name) if rel else name,
                "content": text,
            }
        )
    return True


def _candidate_layout_findings_at(self, root_fd: int) -> list[str]:
    """Top-level layout check mirroring ``_candidate_layout_ok``.

    The approve path refuses a candidate whose ROOT layout is wrong — a
    symlinked ``SKILL.md``/``.meta.json``, a non-regular one (promotion
    reads their bytes, so a directory there is refused too), or any
    unexpected top-level entry — so a verdict that only inspects
    ``scripts/`` would read ``ok: true`` for a candidate approve is
    guaranteed to refuse, which is the predict-the-refusal contract this
    verdict exists to keep.

    Operates on the ALREADY-OPEN candidate-root descriptor the caller
    retains for the whole verdict — this function resolves no path at
    all. Every entry is stat-ed RELATIVE to that descriptor, so nothing
    a candidate does to the tree's names after the root was pinned can
    redirect the scan: the descriptor IS the directory, whatever any
    path now resolves to.

    This is deliberately NOT the recursive candidate-wide pre-walk the
    budgets removed: one capped scan of the top level plus a stat per
    allowed name — no byte is read, nothing recurses (``scripts/``
    internals stay the pinned walk's job). Findings use the same
    ``invalid layout:`` vocabulary as the walk so the frontend renders
    them identically.
    """
    names: list[str] = []
    try:
        with os.scandir(root_fd) as scanner:
            for entry in scanner:
                names.append(entry.name)
                if len(names) > 16:
                    # The valid top level has at most 3 entries; a
                    # planted crowd must not grow this per-poll
                    # scan without limit.
                    return ["invalid layout: too many top-level entries"]
    except OSError:
        return ["verdict unavailable: candidate unreadable"]
    findings: list[str] = []
    for nm in sorted(names):
        if nm not in self._ALLOWED_CANDIDATE_TOP:
            findings.append(f"invalid layout: unexpected candidate entry {nm!r}")
            continue
        est = pinned_fs.stat_at(root_fd, nm)
        if est is None:
            findings.append(f"invalid layout: {nm!r} unreadable")
        elif stat.S_ISLNK(est.st_mode):
            findings.append(f"invalid layout: {nm!r} is a symlink")
        elif nm != "scripts" and not stat.S_ISREG(est.st_mode):
            # Promotion reads these files' bytes; a directory (or
            # FIFO) named SKILL.md/.meta.json is refused at
            # approve time, so the verdict must not read clean.
            findings.append(f"invalid layout: {nm!r} is not a regular file")
    return findings


def _candidate_layout_ok(self, src: Path, name: str) -> bool:
    """Shared candidate-layout guard for BOTH approve paths.

    Rejects (a) any symlink anywhere in the candidate (defense-in-depth on
    top of the mandatory human review — promotion + chmod must only touch
    real files), and (b) any unexpected top-level entry: only ``SKILL.md``,
    ``.meta.json`` and a ``scripts`` DIRECTORY are allowed. An injected
    auxiliary file (dropped outside the validated set) would ride live
    WITHOUT validation or redaction; a regular file named ``scripts`` would
    skip the directory-only script validation + redaction walk. Returns True
    only when the layout is safe to promote.
    """
    if self._candidate_has_symlink(src):
        logger.warning("Refusing to approve %s: candidate contains a symlink", name)
        return False
    # One copy of the rule: the verdict's precheck reads this same set, so
    # an entry added here is automatically predicted by the badge.
    _allowed_top = self._ALLOWED_CANDIDATE_TOP
    for entry in src.iterdir():
        if entry.name not in _allowed_top:
            logger.warning(
                "Refusing to approve %s: unexpected candidate entry %r", name, entry.name
            )
            return False
        if entry.name == "scripts" and not entry.is_dir():
            logger.warning(
                "Refusing to approve %s: 'scripts' must be a directory, not a file", name
            )
            return False
    return True
