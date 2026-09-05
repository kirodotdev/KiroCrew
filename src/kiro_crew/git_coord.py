"""Git coordination for TaskRunner — per-step commits, worktree isolation, revert."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

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
        # A run persisted before repo_root was recorded: the path identity
        # established above is all there is to check.
        return True
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
    return current_branch == run.branch_name


def _orphaned_worktree_gitdir(path: str) -> str:
    """Return a missing worktree admin path named by a regular ``.git`` file.

    A deregistered linked worktree retains the ``.git`` pointer Git wrote into
    its checkout even after the pointed-to admin directory is gone.  That
    pointer is positive evidence of a stale checkout; a directory merely being
    non-Git is not.  Symlinks and live targets are rejected so an arbitrary
    replacement cannot borrow another file as proof of ownership.
    """
    dotgit = Path(path) / ".git"
    try:
        if not stat.S_ISREG(os.lstat(dotgit).st_mode):
            return ""
        with dotgit.open(encoding="utf-8") as stream:
            text = stream.read(4097)
    except (OSError, UnicodeError):
        return ""
    if len(text) > 4096:
        return ""
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith("gitdir: "):
        return ""
    target = Path(lines[0][len("gitdir: ") :])
    if not target.is_absolute():
        target = dotgit.parent / target
    try:
        if target.exists():
            return ""
    except OSError:
        return ""
    return str(target)


async def _leftover_dir_is_ours(run: Project, path: str | None = None) -> bool:
    """True only when *path* identifies this run's worktree.

    A live checkout must belong to the original repository and remain on the
    run's branch.  A broken checkout must retain Git's regular ``.git`` pointer
    to a now-missing admin entry under that repository's ``worktrees``
    directory.  An arbitrary non-Git directory satisfies neither proof and is
    never safe to remove.

    *path* defaults to ``run.worktree_path``. Pass it explicitly to judge a tree
    that has been renamed aside, where the saved path no longer names the
    directory in question -- the proof must hold for the tree actually about to
    be deleted, not merely for whatever occupied the saved path earlier.
    """
    path = path or run.worktree_path
    repo_root = run.repo_root
    if not path or not repo_root or not run.branch_name:
        return False
    if not await _is_git_repo(path):
        stale_gitdir = await asyncio.to_thread(_orphaned_worktree_gitdir, path)
        if not stale_gitdir:
            return False
        try:
            theirs = (await _git(repo_root, "rev-parse", "--git-common-dir")).strip()
        except (RuntimeError, OSError):
            return False
        if not theirs:
            return False
        return await asyncio.to_thread(
            _same_dir,
            str(Path(stale_gitdir).parent),
            str(Path(repo_root) / theirs / "worktrees"),
        )
    try:
        here = (await _git(path, "rev-parse", "--git-common-dir")).strip()
        theirs = (await _git(repo_root, "rev-parse", "--git-common-dir")).strip()
    except (RuntimeError, OSError):
        # It answered "is a repo" a moment ago but a follow-up probe just
        # failed outright -- treat as unidentifiable/foreign rather than
        # risk deleting something real.
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
        # CAPTURE, THEN DESTROY. The ownership proof above resolves
        # ``run.worktree_path``; every destructive step then used to re-resolve
        # that SAME path, with awaited ``git`` subprocesses in between. A
        # directory swapped onto the path during that window was the one that
        # got deleted -- the proof had validated a different tree.
        #
        # Renaming first collapses that gap. The single atomic ``os.rename`` is
        # the LAST operation to resolve the saved path, and everything
        # destructive afterwards addresses ``doomed`` -- a private name carrying
        # a random token, which nothing else can be holding. A later swap onto
        # the saved path can no longer redirect the delete; it merely loses the
        # race to the ``worktree add`` below, which then fails and returns False
        # rather than clobbering whatever arrived.
        #
        # Same-parent rename, so never cross-device. Fails CLOSED on anything
        # but the path having vanished on its own: when the leftover cannot be
        # captured (Windows holds a handle inside it, or permissions refuse),
        # recovery gives up instead of falling back to deleting by path.
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
            # The CAPTURE is what gets deleted, so ownership has to hold for the
            # captured tree -- not merely for whatever occupied the saved path
            # when the proof above ran. A directory swapped in between those two
            # points would otherwise be renamed aside and then destroyed on the
            # strength of a proof about a different tree, which is the whole
            # failure this capture exists to prevent. Proving it again here is
            # what closes that gap: after the rename the tree sits at a private
            # name nothing else knows, so this second proof and the delete
            # cannot disagree about which directory they mean.
            #
            # Put it back if it is not ours -- nothing has been destroyed yet,
            # and leaving a stranger's directory under a `.kirocrew-stale-*`
            # name would be a surprising side effect of a refusal. Say where it
            # ended up when the restore itself fails, so it is recoverable by
            # hand rather than merely lost.
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
        # Git's admin entry still names the ORIGINAL path, whose directory is
        # now gone -- renamed away just above, or already absent. That is
        # precisely the state ``worktree prune`` exists for, which is why the
        # former ``worktree remove --force`` is no longer needed: it was the
        # other operation that could destroy a replacement at the saved path,
        # and pruning deregisters a moved-away checkout just as well.
        try:
            await _git(run.repo_root, "worktree", "prune")
        except Exception:
            pass
        if doomed is not None:
            # Delete the CAPTURED tree, never a re-resolved path.
            #
            # ``rmtree_force``, not ``shutil.rmtree(..., ignore_errors=True)``:
            # this tree is a git checkout, and git writes its loose objects
            # read-only. Windows consults a file's own read-only attribute
            # rather than its parent directory's write bit, so those objects
            # refuse to unlink and ``ignore_errors`` would swallow every one of
            # those failures. Awaited in a thread because this coroutine runs on
            # the gateway event loop and deleting a full checkout is unbounded
            # blocking I/O.
            #
            # A failure here no longer blocks recovery -- the saved path was
            # freed by the rename, so ``worktree add`` below can proceed -- but
            # it does leave a tree on disk, so say so rather than dropping it
            # silently.
            if not await asyncio.to_thread(platform_compat.rmtree_force, doomed):
                logger.warning(
                    "run %s: could not fully delete the set-aside worktree at "
                    "%s; recovery continues but the directory remains on disk",
                    run.task_id,
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
