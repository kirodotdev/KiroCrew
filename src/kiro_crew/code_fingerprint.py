"""One token that names WHICH Kiro Crew code a process is running.

Two processes that must speak the same wire protocol -- the gateway and the
MCP gateway daemon it spawns, the daemon and the pooled MCP backends it hands
out -- have no other way to tell they were started from different checkouts.
The launcher script ``.venv/bin/kirocrew`` is a byte-identical shim across every
commit of an editable install, so hashing it (the MCP stub's ``binary_version``)
says nothing about the code underneath; the package version string changes only
at a release. A daemon that outlived a ``git pull`` therefore kept answering for
a gateway on new code, and the two disagreed about the shape of a control frame
(``/api/session-directive``) for a day before anyone could see why.

The fingerprint is derived from the package tree itself, in this order:

1. **Git.** When ``kiro_crew/`` sits inside a git worktree, the HEAD commit plus
   ``+<digest of the tracked diff>`` when tracked files differ. A bare dirty
   bit is not enough: two edits to the same HEAD would read as the same code,
   and a daemon from the first edit would be adopted by a gateway on the
   second -- the exact miss this module exists to prevent, in the exact
   setting (an editable install being hacked on) where it matters most.
2. **Source mtimes.** Otherwise the newest ``st_mtime_ns`` over every ``*.py``
   under the package, mixed with the installed distribution version. A wheel
   install is immutable until it is replaced, and replacing it moves the mtimes.

The value is opaque; the only contract is *equal iff the same code*. It is
computed once per process and cached. The computation runs a ``git``
subprocess or walks the package tree, so an async caller warms the cache with
:func:`warm_code_fingerprint` (``asyncio.to_thread``) before the first
synchronous read on its event loop; every later ``code_fingerprint()`` is a
dictionary hit.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import os
import subprocess
from pathlib import Path

from kiro_crew.platform_compat import trusted_git_bin
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_PACKAGE_ROOT = Path(__file__).resolve().parent

#: Bound on the mtime walk. The package holds well under this; the cap only
#: stops a pathological tree (a symlink loop planted under site-packages) from
#: turning a startup probe into a scan.
_MAX_FILES_WALKED = 20_000


def _git_env() -> dict[str, str]:
    """An environment in which git reads no configuration anyone could plant.

    This runs in the GATEWAY process, outside the sandbox, on a tree an agent
    can write to. A ``diff.external`` / ``core.pager`` / hook in a repo,
    global or system config, or a ``GIT_*`` variable in the inherited
    environment, would have git run whatever it names with the gateway's
    privileges. Repo-level config cannot be disabled by environment, so the
    argv also pins the two settings a diff can execute (``--no-ext-diff``,
    ``--no-textconv``) and the pager.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git_fingerprint(root: Path) -> str | None:
    """``<HEAD sha>[+<diff digest>]`` when *root* is inside a git worktree, else None."""
    # Resolved from fixed system directories, never PATH: the gateway's PATH
    # can lead with agent-writable directories, and a planted ``git`` shim
    # would run with the gateway's privileges on every fingerprint. No trusted
    # git means no git branch at all; the mtime rule answers instead.
    git = trusted_git_bin()
    if git is None:
        return None
    env = _git_env()
    try:
        head = subprocess.run(
            [git, "-C", str(root), "-c", "core.pager=cat", "rev-parse", "HEAD"],
            capture_output=True,
            timeout=5,
            check=False,
            env=env,
            **UTF8_TEXT,
        )
        if head.returncode != 0 or not head.stdout.strip():
            return None
        sha = head.stdout.strip()
        # Tracked-file changes only; an untracked scratch file is not code
        # the process runs differently for. The diff TEXT is digested, not
        # just its presence: two different uncommitted states of one HEAD
        # must not compare equal.
        dirty = subprocess.run(
            [
                git,
                "-C",
                str(root),
                "-c",
                "core.pager=cat",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "HEAD",
                "--",
                str(root),
            ],
            capture_output=True,
            timeout=5,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if dirty.returncode != 0:
        # git errored rather than answering; fall back rather than claim clean.
        return None
    if not dirty.stdout:
        return sha
    return f"{sha}+{hashlib.sha256(dirty.stdout).hexdigest()[:16]}"


def _mtime_fingerprint(root: Path) -> str:
    """Distribution version mixed with the newest source mtime under *root*."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            dist = version("kiro-crew")
        except PackageNotFoundError:
            dist = "unknown"
    except Exception:  # pragma: no cover - importlib.metadata is stdlib
        dist = "unknown"
    newest = 0
    walked = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            walked += 1
            if walked > _MAX_FILES_WALKED:
                break
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            if st.st_mtime_ns > newest:
                newest = st.st_mtime_ns
        if walked > _MAX_FILES_WALKED:
            break
    digest = hashlib.sha256(f"{dist}|{newest}".encode("utf-8")).hexdigest()[:24]
    return f"mtime:{digest}"


@functools.lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """The fingerprint of the ``kiro_crew`` package THIS process imported."""
    return _git_fingerprint(_PACKAGE_ROOT) or _mtime_fingerprint(_PACKAGE_ROOT)


async def warm_code_fingerprint() -> str:
    """Compute :func:`code_fingerprint` off the event loop and return it.

    The first computation runs ``git`` or walks the package tree; an async
    caller awaits this once at startup so the synchronous reads that follow on
    its loop (a ping reply, an adoption check) hit the cache.
    """
    return await asyncio.to_thread(code_fingerprint)


def fingerprint_of(root: Path) -> str:
    """Uncached fingerprint of an arbitrary package root; for tests."""
    return _git_fingerprint(root) or _mtime_fingerprint(root)
