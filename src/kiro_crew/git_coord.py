"""Git coordination for TaskRunner — per-step commits, worktree isolation, revert."""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import stat
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import TYPE_CHECKING

try:  # Windows-only module; absent on POSIX.
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

from kiro_crew import platform_compat
from kiro_crew.sandbox import (
    create_subprocess_limited,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)

if TYPE_CHECKING:
    from kiro_crew.taskrunner import Project, Task

logger = logging.getLogger(__name__)


async def init_workspace(run: Project) -> None:
    """Set up git isolation for a run when the workspace is a git repo.

    The task runner is a general coding tool, so it must NOT assume the target
    is (or should become) a git repo:

    - **Git repo** (including a nested subdirectory of one — ``--show-toplevel``
      resolves the root): create an isolated worktree on a task branch and run
      there, leaving the user's working tree untouched.
    - **Non-git folder**: run in place. We do NOT ``git init`` — imposing version
      control on a folder the user didn't set up as a repo is surprising. Git
      isolation (per-step commit / revert / diff-review) is simply disabled for
      the run via ``git_enabled = False``.
    """
    orig_dir = run.work_dir
    branch = f"kirocrew/task/{run.task_id}"

    if not await _is_git_repo(orig_dir):
        # General coding task on a non-git folder — run directly in it, no git.
        run.git_enabled = False
        return

    run.base_branch = (await _git(orig_dir, "rev-parse", "--abbrev-ref", "HEAD")).strip()
    repo_root = (await _git(orig_dir, "rev-parse", "--show-toplevel")).strip()
    wt_dir = str(Path(repo_root).parent / ".kirocrew-work" / run.task_id)
    await _git(orig_dir, "worktree", "add", wt_dir, "-b", branch)
    run.work_dir = wt_dir
    run.worktree_path = wt_dir
    run.repo_root = repo_root
    run.branch_name = branch
    run.git_enabled = True


async def commit_step(run: Project, step: Task) -> str:
    """Stage all changes and commit. Returns sha or empty string.

    No-op (returns "") when the run has no git workspace.
    """
    if not run.git_enabled:
        return ""
    await _git(run.work_dir, "add", "-A")
    head_tree = (await _git(run.work_dir, "rev-parse", "HEAD^{tree}")).strip()
    idx_tree = (await _git(run.work_dir, "write-tree")).strip()
    if head_tree == idx_tree:
        return ""
    msg = f"step {step.index}: {step.title}"
    await _git(run.work_dir, "commit", "-m", msg)
    sha = (await _git(run.work_dir, "rev-parse", "HEAD")).strip()
    run.commit_hashes.append(sha)
    return sha


async def revert_step(run: Project) -> None:
    """Revert the last commit (failed step). No-op if git-disabled or nothing to revert."""
    if not run.git_enabled or not run.commit_hashes:
        return
    try:
        await _git(run.work_dir, "reset", "--hard", "HEAD~1")
        run.commit_hashes.pop()
    except Exception:
        logger.debug("git revert failed", exc_info=True)


async def get_state_summary(run: Project) -> str:
    """Build context from git log + diff stat. Empty when git-disabled."""
    if not run.git_enabled:
        return ""
    try:
        log = await _git(run.work_dir, "log", "--oneline", f"{run.base_branch}..HEAD")
        stat = await _git(run.work_dir, "diff", "--stat", run.base_branch)
    except Exception:
        return ""
    parts = []
    if log.strip():
        parts.append(f"## Git Log (changes so far)\n```\n{log.strip()}\n```")
    if stat.strip():
        parts.append(f"## Files Changed\n```\n{stat.strip()}\n```")
    return "\n\n".join(parts)


async def get_step_diff(run: Project) -> str:
    """Get the diff of the last commit (for review). Empty when git-disabled."""
    if not run.git_enabled:
        return ""
    try:
        return await _git(run.work_dir, "diff", "HEAD~1")
    except Exception:
        return ""


async def finalize(run: Project) -> str:
    """Clean up worktree if used. Return branch name."""
    if run.worktree_path:
        try:
            await _git(run.repo_root, "worktree", "remove", run.worktree_path, "--force")
        except Exception:
            logger.debug("worktree cleanup failed", exc_info=True)
    return run.branch_name


def _same_dir(a: str, b: str) -> bool:
    """True if two path strings name the same directory.

    Compared as resolved, normalized paths rather than as raw strings, because
    the two sides come from different producers and disagree textually while
    naming one directory: git prints ``--show-toplevel`` with forward slashes
    even on Windows and resolves symlinks (so a macOS ``/var/...`` temp dir
    comes back as ``/private/var/...``), while ``run.worktree_path`` holds
    whatever unresolved, natively-separated string was stored when the
    worktree was created. ``normcase`` additionally folds case and separators
    on Windows, where two spellings of one path are the same directory.
    """
    try:
        ra, rb = Path(a).resolve(), Path(b).resolve()
    except OSError:
        return False
    return os.path.normcase(str(ra)) == os.path.normcase(str(rb))


async def workspace_is_valid(run: Project) -> bool:
    """True if a run's git worktree, when it has one, is still THAT worktree.

    A run with no worktree (``git_enabled`` False from the start -- the
    ordinary non-git-folder case) is trivially valid, since there is nothing
    to check here. This exists to distinguish an ACTUALLY broken worktree
    (still present on disk after an interrupted ``finalize()``, or removed
    out from under the run entirely -- ``git worktree remove`` deregisters
    and deletes in separate steps, so a failure between them can leave the
    directory behind but already deregistered) from that ordinary case.

    Repo-ness alone is NOT sufficient and answers the wrong question: once the
    worktree has been deregistered but its directory survives, an ordinary
    directory nested anywhere inside another repository still reports as being
    inside a work tree -- git simply walks up to the ENCLOSING repository. The
    run would then be judged valid and every remaining step would commit into
    somebody else's checkout while reporting success. So require the repository
    git resolves from the run's directory to BE the run's own worktree.
    """
    if not run.branch_name:
        return True
    expected = run.worktree_path or run.work_dir
    if not expected:
        return False
    try:
        toplevel = (await _git(run.work_dir, "rev-parse", "--show-toplevel")).strip()
    except (RuntimeError, OSError):
        # Non-zero exit (not a repo at all) or the directory being gone
        # outright -- both mean there is no worktree here to resume against.
        # Broad on purpose, and safe in this direction: False here means
        # "not valid", which routes into recovery. (Contrast `_is_git_repo`,
        # where False means "no git isolation needed" and a transient error
        # must NOT be swallowed.)
        return False
    if not toplevel or not await asyncio.to_thread(_same_dir, toplevel, expected):
        return False
    if not run.repo_root:
        # A run persisted before repo_root was recorded cannot be fully
        # verified (no repository identity, no branch/history anchor) and
        # cannot be recovered either -- reinit_workspace_for_retry needs the
        # original repository. Path identity alone would accept a DIFFERENT
        # repository sitting at the expected path, and resuming there commits
        # the run's remaining steps into someone else's history. Fail closed:
        # the retry is refused with a clear error instead of resuming on a
        # workspace whose identity cannot be established.
        return False
    # Matching paths are still not proof of identity -- a DIFFERENT repository
    # created at the same path satisfies the check above, and resuming into it
    # would commit the run's remaining steps outside its own repository. A
    # linked worktree SHARES its main repository's git directory, so require
    # the common git dir seen from the run's worktree to be the very one
    # `run.repo_root` uses. That is what makes this a worktree OF this repo
    # rather than merely a repository sitting at the expected path.
    try:
        here = (await _git(run.work_dir, "rev-parse", "--git-common-dir")).strip()
        theirs = (await _git(run.repo_root, "rev-parse", "--git-common-dir")).strip()
    except (RuntimeError, OSError):
        return False
    if not here or not theirs:
        return False
    # `--git-common-dir` may answer relatively (to its own cwd); anchoring each
    # to the directory it was resolved from normalizes that, and an already
    # absolute answer wins the join unchanged.
    if not await asyncio.to_thread(
        _same_dir,
        str(Path(run.work_dir) / here),
        str(Path(run.repo_root) / theirs),
    ):
        return False
    # Same repository is still not the same WORKTREE: a linked worktree of
    # this very repo, checked out on some OTHER branch, would satisfy every
    # check above -- same path, same repo -- and retried steps would then
    # commit onto the wrong branch while reporting success. A worktree is
    # only "this run's own" if it is also on the run's branch.
    try:
        current_branch = (await _git(run.work_dir, "branch", "--show-current")).strip()
    except (RuntimeError, OSError):
        return False
    if current_branch != run.branch_name:
        return False
    # Same branch NAME is still not the same HISTORY: a force-moved branch
    # keeps its name while dropping the run's earlier commits, and resuming on
    # it would leave those steps marked passed although their work is gone --
    # every remaining step would then build on the wrong tree and report
    # success. The run's own commit ledger is the ground truth: its last
    # recorded commit must still be in the branch's history.
    return await _branch_history_intact(run.work_dir, run)


async def _branch_history_intact(git_cwd: str, run: Project) -> bool:
    """True when the run's branch still carries the run's recorded commits.

    ``run.commit_hashes`` is the run's own ledger of step commits, appended by
    ``commit_step`` and popped by ``revert_step``, so its last entry is the
    commit the branch tip must still descend from. A branch that was
    force-moved after those steps ran keeps its NAME but not that history;
    resuming on it would leave earlier steps marked passed while their commits
    are absent. With no recorded commits there is nothing to verify -- that is
    a run which never committed a step, not a rewritten branch.

    Fails closed: a recorded commit that no longer resolves (rewritten then
    garbage-collected) and any git error both answer False.
    """
    if not run.commit_hashes:
        return True
    try:
        await _git(
            git_cwd,
            "merge-base",
            "--is-ancestor",
            run.commit_hashes[-1],
            f"refs/heads/{run.branch_name}",
        )
    except (RuntimeError, OSError):
        return False
    return True


_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)  # 0 on Windows (constant absent)


def _is_link(st: os.stat_result) -> bool:
    """POSIX symlink OR Windows reparse point (symlink/junction)."""
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)  # absent on POSIX -> 0
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _windows_open_reparse_handle(filepath: str) -> int:
    """Open *filepath* on Windows WITHOUT following a reparse point.

    ``CreateFileW`` with ``FILE_FLAG_OPEN_REPARSE_POINT`` opens the reparse
    point ITSELF: a symlink or junction leaf is never dereferenced, so a link
    to a UNC target performs no outbound authentication. This is atomic --
    there is no check-then-open window in which the file can be swapped for a
    link. The caller validates the opened handle's attributes via
    ``os.fstat`` and refuses a reparse point or non-regular file.
    """
    if msvcrt is None:  # pragma: no cover - unreachable off-Windows
        raise OSError("msvcrt unavailable: not a Windows platform")

    generic_read = 0x80000000
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    flag_open_reparse_point = 0x00200000

    kernel32 = getattr(ctypes, "windll").kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(
        filepath,
        generic_read,
        share_all,
        None,
        open_existing,
        flag_open_reparse_point,
        None,
    )
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(f"cannot open worktree metadata file: {filepath}")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY)  # type: ignore[attr-defined]
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_nofollow_text(filepath: str):  # type: ignore[no-untyped-def]
    """Open for reading without following a final-component link.

    THE RECOVERY-PATH INVARIANT: no filesystem operation on the
    worktree-recovery path ever follows a link. POSIX: ``O_NOFOLLOW`` makes
    the open itself fail ELOOP on a symlinked leaf. Windows: the file is
    opened with reparse-point-safe handle semantics
    (``FILE_FLAG_OPEN_REPARSE_POINT`` -- the link is opened, never followed).
    Both opens are ATOMIC -- no check-then-open window -- and the OPENED
    handle is then validated: a reparse point or non-regular file is refused
    after the fact, from the handle's own attributes, not from a separate
    path probe a concurrent swap could invalidate.
    """
    if _O_NOFOLLOW:
        fd = os.open(filepath, os.O_RDONLY | _O_NOFOLLOW)
    else:
        fd = _windows_open_reparse_handle(filepath)
    try:
        st = os.fstat(fd)
        if _is_link(st) or not stat.S_ISREG(st.st_mode):
            raise OSError("refusing linked/non-regular worktree metadata file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, encoding="utf-8", errors="replace")


def _run_worktree_admin_dir(worktrees_dir: str, worktree_path: str) -> str:
    """Return the admin entry under *worktrees_dir* registering *worktree_path*.

    Git maps a linked worktree back to its checkout through the ``gitdir``
    file inside each entry of the repository's ``worktrees`` directory: its
    one line is the checkout's ``.git`` path. The entry for a given checkout
    is found by that recorded path, not by directory name -- git suffixes
    names on collision, so the basename is not reliable. Returns "" when no
    entry registers the path.

    Every consumed component obeys the recovery-path invariant (never follow
    a link): the ``worktrees`` directory and each entry are ``lstat``-checked
    and a symlink/junction is skipped, and the ``gitdir`` file is opened
    no-follow. A planted link anywhere in the span yields "" -- it is never
    dereferenced.
    """
    expected = os.path.normcase(os.path.normpath(os.path.join(worktree_path, ".git")))
    try:
        if _is_link(os.lstat(worktrees_dir)):
            return ""
        entries = os.listdir(worktrees_dir)
    except OSError:
        return ""
    for name in entries:
        entry = os.path.join(worktrees_dir, name)
        try:
            if _is_link(os.lstat(entry)):
                continue
            with _open_nofollow_text(os.path.join(entry, "gitdir")) as stream:
                recorded = stream.readline().strip()
        except OSError:
            continue
        if recorded and os.path.normcase(os.path.normpath(recorded)) == expected:
            return entry
    return ""


def _orphaned_worktree_gitdir(path: str, worktrees_dir: str) -> str:
    """Return a missing worktree admin path named by a regular ``.git`` file.

    A deregistered linked worktree retains the ``.git`` pointer Git wrote into
    its checkout even after the pointed-to admin directory is gone.  That
    pointer is evidence of a stale checkout; a directory merely being non-Git
    is not.  Symlinks and live targets are rejected so an arbitrary
    replacement cannot borrow another file as proof of ownership.

    The pointer text is attacker-writable content inside the leftover tree,
    so nothing from it may reach a filesystem probe until it is proven,
    LEXICALLY, to name an entry directly under this repository's own
    ``worktrees`` directory (*worktrees_dir*).  On Windows, merely calling
    ``exists()`` on a UNC target (``\\\\host\\share``) performs an outbound
    SMB authentication -- a credential exposure that happens before any
    ``OSError`` is caught.  So UNC and device targets are rejected on the RAW
    string, before any normalization or syscall, and containment is decided
    by pure string comparison; only a target already proven local and
    repo-owned is probed for existence.
    """
    dotgit = Path(path) / ".git"
    try:
        if not stat.S_ISREG(os.lstat(dotgit).st_mode):
            return ""
        # No-follow open closes the lstat->open TOCTOU: a leaf swapped for a
        # link between the check and the read is refused by the open itself.
        with _open_nofollow_text(str(dotgit)) as stream:
            text = stream.read(4097)
    except (OSError, UnicodeError):
        return ""
    if len(text) > 4096:
        return ""
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        return ""
    raw = lines[0][len("gitdir: ") :].strip()
    # UNC and device paths ('\\\\host\\share', '//host/share', '\\\\.\\',
    # '\\\\?\\', and separator-mixed spellings) all begin with two path
    # separators. Reject them on the raw text -- no Path object, no syscall.
    if len(raw) >= 2 and raw[0] in ("\\", "/") and raw[1] in ("\\", "/"):
        return ""
    target = Path(raw)
    if not target.is_absolute():
        target = dotgit.parent / target
    # Lexical containment: the target's PARENT must be the repository's own
    # worktrees directory. Both accepted spellings of that directory -- as
    # given and fully resolved -- are computed from the repo-owned path only;
    # the attacker-influenced target is never resolved, only normalized as a
    # string. Only after this proof does any syscall touch the target.
    target_parent = os.path.normcase(os.path.dirname(os.path.normpath(str(target))))
    allowed = {
        os.path.normcase(os.path.normpath(worktrees_dir)),
        os.path.normcase(os.path.realpath(worktrees_dir)),
    }
    if target_parent not in allowed:
        return ""
    try:
        # ``lexists`` stats the final component WITHOUT following it: a leaf
        # symlink/junction (even one pointing at a UNC target) is seen as the
        # link itself -- no dereference, no outbound authentication. A link
        # leaf reads as "exists" and is therefore rejected as evidence, which
        # is the correct, safe outcome.
        if os.path.lexists(str(target)):
            return ""
    except OSError:
        return ""
    return str(target)


async def _leftover_dir_is_ours(run: Project, path: str | None = None) -> bool:
    """True only when *path* identifies this run's worktree.

    A live checkout must belong to the original repository (same
    ``--git-common-dir``) and remain on the run's branch.  A broken checkout
    must retain Git's regular ``.git`` pointer to a now-missing admin entry
    directly under that repository's ``worktrees`` directory -- proven
    lexically against the repo-owned path before the pointer target is
    touched at all (see ``_orphaned_worktree_gitdir``).  An arbitrary non-Git
    directory satisfies neither proof and is never safe to move.

    Either proof authorizes only CAPTURE (setting the tree aside so recovery
    can proceed); nothing in recovery deletes a leftover tree.

    *path* defaults to ``run.worktree_path``. Pass it explicitly to judge a
    tree that has been renamed aside, where the saved path no longer names
    the directory in question.
    """
    path = path or run.worktree_path
    repo_root = run.repo_root
    if not path or not repo_root or not run.branch_name:
        return False
    if not await _is_git_repo(path):
        try:
            theirs = (await _git(repo_root, "rev-parse", "--git-common-dir")).strip()
        except (RuntimeError, OSError):
            return False
        if not theirs:
            return False
        worktrees_dir = str(Path(repo_root) / theirs / "worktrees")
        stale_gitdir = await asyncio.to_thread(_orphaned_worktree_gitdir, path, worktrees_dir)
        return bool(stale_gitdir)
    try:
        here = (await _git(path, "rev-parse", "--git-common-dir")).strip()
        theirs = (await _git(repo_root, "rev-parse", "--git-common-dir")).strip()
    except (RuntimeError, OSError):
        # It answered "is a repo" a moment ago but a follow-up probe just
        # failed outright -- treat as unidentifiable/foreign rather than
        # risk moving something real.
        return False
    if not here or not theirs:
        return False
    if not await asyncio.to_thread(
        _same_dir, str(Path(path) / here), str(Path(repo_root) / theirs)
    ):
        return False
    try:
        branch = (await _git(path, "branch", "--show-current")).strip()
    except (RuntimeError, OSError):
        return False
    return branch == run.branch_name


async def reinit_workspace_for_retry(run: Project) -> bool:
    """Recreate a lost worktree before resuming a retried run.

    ``init_workspace()`` cannot simply be called again here: it overwrites
    ``run.work_dir`` with the worktree path on its original call, so a second
    call would check git-repo-ness of the now-DEAD WORKTREE rather than the
    original repo, and silently disable git (``git_enabled = False``) instead
    of recovering. This uses ``run.repo_root`` instead -- set once by the
    original ``init_workspace()`` and never overwritten -- and reuses the
    run's EXISTING branch: ``finalize()`` only ever removes the worktree,
    never the branch, so creating a fresh branch of the same name would fail
    with "already exists".

    Returns True if the workspace is valid to resume against afterward;
    False means the caller must not resume and should fail the run instead
    of continuing against a broken workspace.
    """
    if not run.repo_root or not await _is_git_repo(run.repo_root):
        return False
    if not run.branch_name:
        return False
    if not await _branch_history_intact(run.repo_root, run):
        # Re-adding the branch by NAME would silently resume on a rewritten
        # history: the run's earlier steps would stay marked passed although
        # their commits are gone. Refuse before touching anything on disk --
        # the leftover tree, whatever it is, stays exactly where it was.
        logger.warning(
            "refusing to recover run %s: branch %s no longer carries the "
            "run's last recorded commit",
            run.task_id,
            run.branch_name,
        )
        return False
    if run.worktree_path:
        path_exists = await asyncio.to_thread(os.path.exists, run.worktree_path)
        if path_exists and not await _leftover_dir_is_ours(run):
            logger.warning(
                "refusing to recover run %s: an unowned directory now "
                "occupies its worktree path %s",
                run.task_id,
                run.worktree_path,
            )
            return False
        # CAPTURE, THEN PARK. The ownership proof above resolves
        # ``run.worktree_path``; acting on a re-resolved path later -- with
        # awaited ``git`` subprocesses in between -- would let a directory
        # swapped onto the path be the one acted on, while the proof had
        # validated a different tree.
        #
        # Renaming first collapses that gap. The single atomic ``os.rename`` is
        # the LAST operation to resolve the saved path, and everything
        # afterwards addresses ``doomed`` -- a private name carrying a random
        # token, which nothing else can be holding. A later swap onto the
        # saved path can no longer redirect anything; it merely loses the race
        # to the ``worktree add`` below, which then fails and returns False
        # rather than clobbering whatever arrived.
        #
        # Same-parent rename, so never cross-device. Fails CLOSED on anything
        # but the path having vanished on its own: when the leftover cannot be
        # captured (Windows holds a handle inside it, or permissions refuse),
        # recovery gives up instead of falling back to acting by path.
        doomed: str | None = None
        if path_exists:
            doomed = f"{run.worktree_path}.kirocrew-stale-{uuid.uuid4().hex[:12]}"
            try:
                await asyncio.to_thread(os.rename, run.worktree_path, doomed)
            except FileNotFoundError:
                doomed = None
            except OSError:
                logger.warning(
                    "refusing to recover run %s: could not set aside the "
                    "leftover directory at %s",
                    run.task_id,
                    run.worktree_path,
                    exc_info=True,
                )
                return False
        if doomed is not None and not await _leftover_dir_is_ours(run, path=doomed):
            # The CAPTURE is what gets parked and re-added over, so ownership
            # has to hold for the captured tree -- not merely for whatever
            # occupied the saved path when the proof above ran. A directory
            # swapped in between those two points would otherwise be renamed
            # aside on the strength of a proof about a different tree, which
            # is the whole failure this capture exists to prevent. Proving it
            # again here is what closes that gap: after the rename the tree
            # sits at a private name nothing else knows, so this second proof
            # and everything after it cannot disagree about which directory
            # they mean.
            #
            # Put it back if it is not ours -- nothing has been moved
            # anywhere unexpected yet, and leaving a stranger's directory
            # under a `.kirocrew-stale-*` name would be a surprising side
            # effect of a refusal. Say where it ended up when the restore
            # itself fails, so it is recoverable by hand rather than merely
            # lost.
            restored = True
            try:
                await asyncio.to_thread(os.rename, doomed, run.worktree_path)
            except OSError:
                restored = False
            logger.warning(
                "refusing to recover run %s: the directory captured from %s is "
                "not this run's worktree%s",
                run.task_id,
                run.worktree_path,
                "" if restored else f"; it remains at {doomed}",
            )
            return False
        # Git may still hold an admin entry registering the run's ORIGINAL
        # path, whose directory is now gone -- renamed away just above, or
        # already absent -- and ``worktree add`` refuses a path git believes
        # is a missing-but-registered worktree. A repository-wide ``worktree
        # prune`` is the WRONG tool for that: it deregisters EVERY worktree
        # whose directory is currently missing, including unrelated checkouts
        # on a temporarily unmounted share or removable drive, corrupting
        # them for when they come back. So remove ONLY this run's entry:
        # ``_run_worktree_admin_dir`` walks the repo-owned ``worktrees``
        # directory and returns the single entry whose recorded ``gitdir``
        # names the run's own worktree path -- every input is repo-owned, no
        # leftover-tree content is consulted -- and only that directory is
        # removed. Other runs' and other users' registrations are untouched.
        try:
            theirs = (await _git(run.repo_root, "rev-parse", "--git-common-dir")).strip()
        except (RuntimeError, OSError):
            theirs = ""
        if theirs:
            worktrees_dir = str(Path(run.repo_root) / theirs / "worktrees")
            admin_dir = await asyncio.to_thread(
                _run_worktree_admin_dir, worktrees_dir, run.worktree_path
            )
            if admin_dir and not await asyncio.to_thread(platform_compat.rmtree_force, admin_dir):
                logger.warning(
                    "run %s: could not remove its stale worktree registration "
                    "at %s; the re-add below may fail",
                    run.task_id,
                    admin_dir,
                )
        if doomed is not None:
            # The captured tree is PARKED, never deleted. Recovery's ownership
            # proofs top out at "this run's own leftover" -- and for the
            # orphaned-checkout case this function exists for, the only
            # available evidence is a plain-text ``.git`` pointer file that a
            # forgery reproduces exactly, so no leftover that reaches this
            # point carries evidence strong enough to authorize destruction.
            # The rename above already freed the saved path for the re-add
            # below; the tree stays on disk under the private set-aside name,
            # and the warning says where it went so a human can inspect or
            # remove it by hand.
            logger.warning(
                "run %s: the leftover from %s has been parked at %s -- "
                "inspect and remove it by hand if it is not needed",
                run.task_id,
                run.worktree_path,
                doomed,
            )
    wt_dir = run.worktree_path or str(Path(run.repo_root).parent / ".kirocrew-work" / run.task_id)
    try:
        await _git(run.repo_root, "worktree", "add", wt_dir, run.branch_name)
    except Exception:
        logger.debug("worktree re-add on retry failed", exc_info=True)
        return False
    run.work_dir = wt_dir
    run.worktree_path = wt_dir
    run.git_enabled = True
    # Validate the worktree that ``worktree add`` actually produced, not the
    # one the prechecks reasoned about. Every check so far ran BEFORE the add,
    # so a branch force-updated inside that window would be checked out here
    # with the run's earlier commits gone -- and those steps would stay marked
    # passed. ``workspace_is_valid`` re-proves path, repository, branch and
    # history identity on the freshly added checkout; on mismatch, deregister
    # the just-added worktree (plain ``worktree remove``, which refuses a
    # dirty tree rather than destroying anything unexpected) and fail closed.
    if not await workspace_is_valid(run):
        logger.warning(
            "refusing to recover run %s: the freshly added worktree at %s "
            "failed revalidation (branch moved during recovery?)",
            run.task_id,
            wt_dir,
        )
        try:
            await _git(run.repo_root, "worktree", "remove", wt_dir)
        except Exception:
            logger.warning(
                "run %s: could not remove the failed recovery worktree at %s; "
                "it remains on disk",
                run.task_id,
                wt_dir,
                exc_info=True,
            )
        run.git_enabled = False
        return False
    return True


async def _is_git_repo(path: str) -> bool:
    # git runs against an agent-selected repo whose local hooks and config can
    # execute code, so route through the sandbox chokepoint (OS isolation +
    # credential-scrubbed env).
    argv, env, cleanup = await sandboxed_spawn_argv_async(
        ["git", "rev-parse", "--is-inside-work-tree"], _prepare=sandboxed_spawn_argv
    )
    try:
        proc = await create_subprocess_limited(
            *argv,
            cwd=path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        await proc.communicate()
        return proc.returncode == 0
    except (FileNotFoundError, NotADirectoryError):
        # Something in the invocation is NOT THERE, which is genuinely
        # answerable as "no git repo here": either no ``git`` binary on the
        # host (the task runner is git-optional, so such a host simply has no
        # git repos), or *path* itself no longer exists -- exactly the
        # lost-worktree state the retry path probes for. The platforms spell
        # the missing-cwd case differently: POSIX raises ``FileNotFoundError``
        # from the spawn, Windows raises ``NotADirectoryError`` (WinError 267)
        # out of ``CreateProcess``, so both names are needed.
        #
        # Deliberately NOT a blanket ``except OSError``. A transient spawn
        # failure in a perfectly valid repo -- ``EMFILE``, ``ENOMEM``,
        # ``EAGAIN`` -- is also an ``OSError``, and answering False to those
        # would report a real repo as non-git: ``init_workspace`` would then
        # set ``git_enabled = False`` and every step would run directly
        # against the user's own checkout with no worktree isolation and no
        # per-step commits. Those must propagate and fail the run instead.
        logger.debug("git unavailable for %s; treating as non-git", path, exc_info=True)
        return False
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


async def _git(work_dir: str, *args: str) -> str:
    # Agent-influenced git invocation: sandbox + scrubbed env.
    argv, env, cleanup = await sandboxed_spawn_argv_async(
        ["git", *args], _prepare=sandboxed_spawn_argv
    )
    try:
        proc = await create_subprocess_limited(
            *argv,
            cwd=work_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode()}")
    return stdout.decode()
