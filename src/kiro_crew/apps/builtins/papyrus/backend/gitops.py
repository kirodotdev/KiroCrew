"""Papyrus — git operations for a paper checked out from a remote.

Papers are often already in a git repository (a hosted LaTeX service exposes one,
and lab papers live in a normal repo), so the app can clone one as a project and
push commits back. Every git invocation here follows the same three rules as
:mod:`.latex`:

* **Never on the event loop.** ``git clone`` and ``git push`` are network
  operations that can take tens of seconds; they are spawned with
  :func:`asyncio.create_subprocess_exec` and awaited under a timeout.
* **Sandbox chokepoint.** The repository is agent- and user-influenced content
  whose local hooks and config can execute code, so every spawn routes through
  :func:`kiro_crew.sandbox.sandboxed_spawn_argv` (OS isolation + scrubbed env)
  and spawns through :func:`kiro_crew.sandbox.create_subprocess_limited`.
* **No argument smuggling.** The clone URL is matched against a scheme allowlist
  and passed after ``--``, so a value like ``--upload-pack=...`` can never be
  read as an option. The project directory is produced by
  :func:`.store.safe_project_dir`, never taken from the client verbatim.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.sandbox import create_subprocess_limited, sandboxed_spawn_argv
from kiro_crew.sel import sel

logger = logging.getLogger("kirocrew.app.papyrus")

#: A clone URL must match a known transport. Modern git already refuses an
#: option-shaped URL, but the check is free and closes the door on every version.
GIT_URL_RE = re.compile(r"^(?:https?|git|ssh)://[^\s]+$|^git@[\w.-]+:[^\s]+$")

#: Wall-clock ceilings, by operation cost.
CLONE_TIMEOUT_SEC = 120.0
NETWORK_TIMEOUT_SEC = 60.0
LOCAL_TIMEOUT_SEC = 15.0

#: Default message when the client sends none.
DEFAULT_COMMIT_MESSAGE = "Update from Papyrus"

#: How many recent commits the status endpoint reports.
RECENT_COMMITS = 5

#: Stash label used by the pull autostash, so a leftover stash is identifiable.
_STASH_LABEL = "papyrus-pull-autostash"

#: Substrings that identify an authentication failure across remote types. git's
#: exit code is non-zero for every push failure and the wording varies by
#: transport, so the UI needs this to distinguish "log in" from "something broke".
_AUTH_MARKERS = (
    "authentication",
    "could not read username",
    "permission denied",
    "403 forbidden",
    "terminal prompts disabled",
)

#: Cap on the git output echoed back to the client.
MAX_OUTPUT_CHARS = 4000


class GitError(Exception):
    """A git invocation failed. ``output`` carries the (bounded) git message."""

    def __init__(self, message: str, *, output: str = "", auth: bool = False) -> None:
        super().__init__(message)
        self.output = output[:MAX_OUTPUT_CHARS]
        self.auth = auth


class GitConflict(GitError):
    """A pull could not be applied because of a real conflict."""


@dataclass
class GitStatus:
    """The toolbar's view of a project's git state."""

    is_git: bool
    branch: str = ""
    dirty: bool = False
    has_remote: bool = False
    ahead: int = 0
    behind: int = 0
    changes: list[str] = field(default_factory=list)
    recent_commits: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        if not self.is_git:
            return {"is_git": False}
        return {
            "is_git": True,
            "branch": self.branch,
            "dirty": self.dirty,
            "has_remote": self.has_remote,
            "ahead": self.ahead,
            "behind": self.behind,
            "changes": self.changes,
            "recent_commits": self.recent_commits,
        }


def is_git_repo(project: Path) -> bool:
    """True when *project* holds a git repository. Synchronous — offload it."""
    return (project / ".git").exists()


def git_available() -> bool:
    """True when a ``git`` binary is on PATH. Synchronous — offload it."""
    return shutil.which("git") is not None


def _audit(operation: str, target: str, outcome: str, *, error: str = "") -> None:
    """SEL event for every git spawn. Fire-and-forget."""
    sel().log_api_access(
        caller="core:papyrus",
        operation=f"papyrus.git_{operation}",
        outcome=outcome,
        source="builtin-app",
        resources=target[:200],
        error=error[:200] if error else "",
    )


async def _git(
    args: list[str], *, cwd: Path, timeout: float = LOCAL_TIMEOUT_SEC
) -> tuple[int, str, str]:
    """Run ``git <args>`` in *cwd* off the event loop.

    Returns ``(returncode, stdout, stderr)``. A timeout kills the process tree
    and surfaces as :class:`GitError` rather than a silent empty result.
    """
    argv = ["git", *args]
    wrapped, env, cleanup = sandboxed_spawn_argv(argv)
    # A push/pull must never block on an interactive credential prompt: the
    # gateway has no terminal, so the child would hang until the timeout.
    env["GIT_TERMINAL_PROMPT"] = "0"
    proc: asyncio.subprocess.Process | None = None
    try:
        # `create_subprocess_limited`, not `create_subprocess_exec` +
        # `preexec_fn`: a post-fork preexec forks the threaded gateway and runs
        # Python in the child before exec. The shim applies the same limits
        # AFTER exec, where the process is single-threaded.
        proc = await create_subprocess_limited(
            *wrapped,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=platform_compat.CREATE_NEW_PROCESS_GROUP,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        if proc is not None and proc.returncode is None:
            try:
                await platform_compat.kill_process_tree_async(
                    proc.pid, platform_compat.SIGKILL
                )
            except (ProcessLookupError, OSError, ValueError):
                logger.debug("papyrus: git %s already gone before kill", args[:1])
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:  # pragma: no cover - defensive
                logger.warning("papyrus: git did not exit after SIGKILL")
        _audit(args[0] if args else "run", str(cwd), "failure", error="timeout")
        raise GitError(f"git {args[0] if args else ''} timed out") from exc
    except FileNotFoundError as exc:
        _audit(args[0] if args else "run", str(cwd), "failure", error="git not found")
        raise GitError("git is not installed on this host") from exc
    finally:
        if cleanup:
            Path(cleanup).unlink(missing_ok=True)
    code = proc.returncode or 0
    _audit(args[0] if args else "run", str(cwd), "ok" if code == 0 else "failure")
    return (
        code,
        (stdout or b"").decode("utf-8", "replace"),
        (stderr or b"").decode("utf-8", "replace"),
    )


def derive_project_name(url: str) -> str:
    """Derive a project name from a clone URL's last path segment."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail.lower()


async def clone(url: str, destination: Path) -> None:
    """Shallow-clone *url* into *destination*.

    *destination* must NOT exist and must already have been produced by
    :func:`.store.safe_project_dir` — this function does not validate it. The URL
    is checked against :data:`GIT_URL_RE` and passed after ``--``. A failed clone
    removes the partial directory so a retry is not blocked by it.
    """
    if not GIT_URL_RE.match(url or ""):
        raise GitError("url must be http(s)://, git://, ssh://, or git@host:path")
    try:
        code, _out, err = await _git(
            ["clone", "--depth", "1", "--", url, str(destination)],
            cwd=destination.parent,
            timeout=CLONE_TIMEOUT_SEC,
        )
    except GitError:
        await _remove_tree(destination)
        raise
    if code != 0:
        await _remove_tree(destination)
        raise GitError("git clone failed", output=err)


async def _remove_tree(path: Path) -> None:
    """Remove a partially-cloned directory off the event loop."""
    if await asyncio.to_thread(path.is_dir):
        await asyncio.to_thread(shutil.rmtree, path, True)


async def status(project: Path) -> GitStatus:
    """Collect the project's git state, or ``is_git=False`` when it is not a repo."""
    if not await asyncio.to_thread(is_git_repo, project):
        return GitStatus(is_git=False)

    _c, porcelain, _e = await _git(["status", "--porcelain"], cwd=project)
    _c, branch_out, _e = await _git(["branch", "--show-current"], cwd=project)
    _c, log_out, _e = await _git(
        ["log", f"-{RECENT_COMMITS}", "--oneline", "--no-color"], cwd=project
    )
    _c, remote_out, _e = await _git(["remote"], cwd=project)

    ahead = behind = 0
    code, counts, _e = await _git(
        ["rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cwd=project
    )
    if code == 0:
        parts = counts.strip().split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            ahead, behind = int(parts[0]), int(parts[1])

    changed = [line for line in porcelain.strip().splitlines() if line]
    return GitStatus(
        is_git=True,
        branch=branch_out.strip(),
        dirty=bool(changed),
        has_remote=bool(remote_out.strip()),
        ahead=ahead,
        behind=behind,
        changes=changed[:200],
        recent_commits=[line for line in log_out.strip().splitlines() if line],
    )


async def commit(project: Path, message: str) -> str:
    """Stage everything and commit. "Nothing to commit" is a success, not an error."""
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")
    await _git(["add", "-A"], cwd=project)
    code, out, err = await _git(["commit", "-m", message or DEFAULT_COMMIT_MESSAGE], cwd=project)
    if code == 0:
        return out.strip()
    if "nothing to commit" in out.lower():
        return "nothing to commit"
    raise GitError("git commit failed", output=err or out)


async def push(project: Path) -> str:
    """Push the current branch. Raises with ``auth=True`` on a credential failure."""
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")
    code, out, err = await _git(["push"], cwd=project, timeout=NETWORK_TIMEOUT_SEC)
    if code == 0:
        return (out or err).strip()
    combined = (err + out).lower()
    if any(marker in combined for marker in _AUTH_MARKERS):
        raise GitError("authentication failed", output=err or out, auth=True)
    raise GitError("git push failed", output=err or out)


async def pull(project: Path) -> tuple[str, bool]:
    """Rebase-pull, autostashing local work. Returns ``(output, stashed)``.

    A dirty working tree — typically compiler artifacts that are not in
    ``.gitignore`` — would otherwise refuse the rebase outright, so uncommitted
    work (including untracked files) is stashed first and popped afterwards. On a
    real conflict the rebase is aborted and the stash restored, so the tree comes
    back exactly as it was. If the pop itself conflicts, the stash is deliberately
    LEFT in place — silently discarding the user's edits to finish an operation
    would be the worse outcome — and that is reported.

    Every failure path after the stash restores it, including the ones that raise
    from inside ``_git`` itself (a network pull that exceeds
    ``NETWORK_TIMEOUT_SEC``, or ``git`` disappearing mid-operation). Without that,
    a timed-out pull would return the user to an apparently-clean tree with their
    work parked in a stash they were never told about — which reads as "my edits
    vanished", the exact outcome the docstring above promises cannot happen.
    """
    if not await asyncio.to_thread(is_git_repo, project):
        raise GitError("not a git repository")

    _c, porcelain, _e = await _git(["status", "--porcelain"], cwd=project)
    stashed = False
    if porcelain.strip():
        code, out, err = await _git(
            ["stash", "push", "--include-untracked", "-m", _STASH_LABEL], cwd=project
        )
        stashed = code == 0 and "no local changes" not in (out + err).lower()

    try:
        code, out, err = await _git(
            ["pull", "--rebase"], cwd=project, timeout=NETWORK_TIMEOUT_SEC
        )
    except GitError:
        # The pull never produced an exit code (timeout / git vanished). Put the
        # tree back before surfacing the error; a best-effort pop, because failing
        # to restore must not mask the original cause.
        if stashed:
            try:
                await _git(["stash", "pop"], cwd=project)
            except GitError:  # pragma: no cover - defensive
                logger.warning(
                    "papyrus: could not restore the autostash after a failed pull; "
                    "it is kept as '%s'",
                    _STASH_LABEL,
                )
        raise

    if code != 0:
        combined = out + err
        if "CONFLICT" in combined:
            await _git(["rebase", "--abort"], cwd=project)
            if stashed:
                await _git(["stash", "pop"], cwd=project)
            raise GitConflict("pull conflicts with upstream", output=combined)
        if stashed:
            await _git(["stash", "pop"], cwd=project)
        raise GitError("git pull failed", output=err or out)

    if stashed:
        pop_code, pop_out, pop_err = await _git(["stash", "pop"], cwd=project)
        if pop_code != 0:
            raise GitConflict(
                "pulled, but your local changes conflict with upstream — the stash "
                "was kept so nothing is lost",
                output=pop_out + pop_err,
            )
    return out.strip(), stashed
