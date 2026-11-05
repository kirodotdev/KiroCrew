"""One owner for reclaiming git's orphaned temporary pack files.

``git fetch`` (and ``git clone``) receive their pack through ``git
index-pack --stdin``, which writes ``.git/objects/pack/tmp_pack_XXXXXX`` and
renames it into place only when the WHOLE pack arrived. When the fetching
process is killed before that — a subprocess timeout is exactly such a kill
(``subprocess.run`` on Windows uses TerminateProcess, which gives git no
chance to unlink the file; a killed fetch every few minutes can fill a disk
with hundreds of GB of ``tmp_pack_*``) — the file is simply left behind. Git
itself never goes back for it, so this module is the one reclamation path,
called from the fetches that run PERIODICALLY into long-lived clones, where
orphans would pile up without bound — the auto-improvement
clone-setup/commit/driver sites and the dev-fleet status refresher. The PR
watcher's base-ref fetch does not call it: its clone is rebuilt right before
the fetch, so nothing there can be older than the threshold. The tree's remaining fetch sites do NOT call it
(``cli_server``, ``ops_mission_control``'s ledger sync, ``md_notebook``,
``registry_pipeline``, the sync/rebase fetches inside ``dev_fleet`` itself,
and the ``kirocrew-prepare-pr`` builtin scripts): dev-fleet's two land in the
operator's checkout whose object store the refresher sweeps every cycle
regardless, and the rest are one-shot, user-triggered paths a reader should
revisit if they ever gain a periodic caller.

Why an mtime threshold, and why this number
-------------------------------------------
A live fetch touches its temp pack continuously while receiving, so an
in-flight pack's mtime is always recent; a killed one's is frozen at the
kill. Almost every calling fetch runs under a total-duration bound —
``clone_setup._run`` defaults to 120s with a 300s ceiling, ``commit._git``
runs at 60s, the dev-fleet fetches at 90s — so any
``tmp_pack_*`` untouched for longer than :data:`TMP_PACK_MAX_AGE_S`, twice
that largest bound, belongs to a fetch of ours that cannot be making
progress. The exception is ``spine/driver_git._git``, whose ``subprocess.run``
passes NO timeout: there the threshold vouches only for a fetch that has
been inert for ten minutes, and a mistaken reaping costs one wasted transfer,
not repository damage (on Windows the unlink of a file a live git still holds
open fails and is swallowed; on POSIX git keeps writing to its now-unlinked
fd and its final rename fails, failing that one fetch). The threshold IS the
in-flight guard for the bounded callers: no caller has to track concurrent
fetches, and a pack from a concurrently running fetch (this process's, or the
operator's own git in the same checkout) is younger than the threshold for
its whole first ten minutes and is skipped. The cost of the conservatism is a
bounded backlog: at the refresher's pace, orphans pile up for at most
~10 minutes before the next cycle reaps them, instead of forever.

What it deliberately does NOT do
--------------------------------
No git is spawned (the reaper runs right after a fetch that may have just
been killed by a timeout — the tree's git may be wedged), nothing younger
than the threshold is touched, directories named ``tmp_pack_*`` are skipped,
and every failure (unreadable dir, vanished entry, an unlink blocked because
a live process on Windows still holds the file open) is swallowed: reaping is
a janitor, and a janitor that raises is worse than none. Callers get the
reaped paths back only so a test (and a curious log line) can assert the work
happened.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: What git names an in-progress received pack (see ``index-pack``/``unpack
#: -objects`` in git's own sources; only ``index-pack`` writes this one).
TMP_PACK_PREFIX = "tmp_pack_"

#: A ``tmp_pack_*`` older than this is orphaned. Twice the largest total-time
#: bound any CALLING fetch runs under (``clone_setup._run``'s 300s; the other
#: bounded callers run 90-120s) — see the module docstring for why the number
#: is the safety argument, not bookkeeping.
TMP_PACK_MAX_AGE_S = 600.0


def _pack_dirs(clone: Path) -> list[Path]:
    """Every ``objects/pack`` directory *clone* could hold a temp pack in.

    Resolved from the filesystem, not by spawning ``git rev-parse --git-path``:
    the layouts that matter here are fixed (plain checkout, bare repo, linked
    worktree whose ``.git`` is a gitfile pointing at a per-worktree gitdir that
    shares the common object store). Nonexistent candidates are dropped;
    anything weirder than these three simply yields no reaping this call, which
    is the correct failure mode for a janitor.
    """
    candidates: list[Path] = []
    dot_git = clone / ".git"
    if not dot_git.exists():
        # Bare repo only. A checkout's own ``objects/pack`` is project data,
        # so this branch runs only when there is no ``.git`` and the folder
        # carries a bare repo's own metadata.
        if _looks_bare(clone):
            candidates.append(clone / "objects" / "pack")
    elif dot_git.is_dir():
        candidates.append(dot_git / "objects" / "pack")
    elif dot_git.is_file():
        gitdir = _gitfile_target(dot_git, clone)
        if gitdir is not None:
            candidates.append(gitdir / "objects" / "pack")
            common = _commondir_target(gitdir)
            if common is not None:
                # A linked worktree's gitdir holds no objects: they live in the
                # common dir, and that is where its fetches write temp packs.
                candidates.append(common / "objects" / "pack")
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        real = os.path.normpath(str(candidate))
        if real in seen or not os.path.isdir(real):
            continue
        seen.add(real)
        unique.append(Path(real))
    return unique


def _looks_bare(repo: Path) -> bool:
    """True when *repo* holds a bare repo's own files: ``HEAD``, ``config``, ``refs/``, ``objects/``."""
    try:
        return (
            (repo / "HEAD").is_file()
            and (repo / "config").is_file()
            and (repo / "refs").is_dir()
            and (repo / "objects").is_dir()
        )
    except OSError:
        return False


def _gitfile_target(dot_git: Path, clone: Path) -> Path | None:
    """The gitdir a ``.git`` FILE points at (``gitdir: <path>``), or None."""
    try:
        first = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not first.startswith("gitdir:"):
        return None
    target = Path(first[len("gitdir:") :].strip())
    if not target.is_absolute():
        target = clone / target
    return target


def _commondir_target(gitdir: Path) -> Path | None:
    """The common dir a linked worktree's ``commondir`` file names, or None."""
    try:
        common = (gitdir / "commondir").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not common:
        return None
    target = Path(common)
    if not target.is_absolute():
        target = gitdir / target
    return target


def reap_stale_tmp_packs(
    clone: str | os.PathLike,
    *,
    max_age_s: float = TMP_PACK_MAX_AGE_S,
    now: float | None = None,
) -> list[str]:
    """Unlink every stale ``tmp_pack_*`` under *clone*'s object store.

    Best-effort and silent by design — see the module docstring for the safety
    argument behind :data:`TMP_PACK_MAX_AGE_S`. Returns the absolute paths it
    reaped, so a test can assert the work without this module ever logging.
    """
    if now is None:
        now = time.time()
    reaped: list[str] = []
    for pack_dir in _pack_dirs(Path(clone)):
        try:
            entries = list(os.scandir(pack_dir))
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(TMP_PACK_PREFIX):
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                age = now - entry.stat(follow_symlinks=False).st_mtime
            except OSError:
                continue  # vanished / unreadable mid-scan: skip, never raise
            if age < max_age_s:
                continue  # possibly still in flight — the threshold is the guard
            try:
                os.unlink(entry.path)
            except OSError:
                continue  # gone already, or a live process still holds it open
            reaped.append(entry.path)
    return reaped
