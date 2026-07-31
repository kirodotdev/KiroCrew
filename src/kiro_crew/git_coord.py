"""Git coordination for TaskRunner — per-step commits, worktree isolation, revert."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv

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


async def _is_git_repo(path: str) -> bool:
    # git runs against an agent-selected repo whose local hooks and config can
    # execute code, so route through the sandbox chokepoint (OS isolation +
    # credential-scrubbed env). See Talos finding 92e24570.
    argv, env, cleanup = sandboxed_spawn_argv(["git", "rev-parse", "--is-inside-work-tree"])
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
    except FileNotFoundError:
        # No ``git`` binary on this host. The task runner is git-optional, so a
        # host without git simply has no git repos — treat the workspace as
        # non-git (run in place, git_enabled=False) instead of crashing.
        logger.debug("git binary not found; treating workspace as non-git", exc_info=True)
        return False
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)


async def _git(work_dir: str, *args: str) -> str:
    # Agent-influenced git invocation: sandbox + scrubbed env (Talos 92e24570).
    argv, env, cleanup = sandboxed_spawn_argv(["git", *args])
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
