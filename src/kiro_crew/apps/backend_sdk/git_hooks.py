"""Git's config-defined hooks, turned off by name for one sandboxed git call (seam level 17).

Git 2.54 and later run a hook a repository names in its config (``hook.<name>.command``),
and ``core.hooksPath`` does not reach it. A repository's ``.git/config`` is written by
whoever edits the tree, so a git call over a tree an agent writes would run that
command. :func:`git_hook_disable_args` is the gateway's own answer
(``kiro_crew.git_config_hooks``): the ``-c hook.<name>.enabled=false`` entries for
every hook name git sees, the initialized submodules' included, then the pins that
stop submodule recursion. Put them before the subcommand.

The names are listed by running git, and that git runs inside the gateway's OS sandbox
at the tier the caller asks for, through the same chokepoint as :func:`run_sandboxed`,
so the scan reaches nothing the real call would not. The private module's unsandboxed
default runner, its ``runner`` and ``spawn_argv`` parameters and its ``git`` parameter
each choose what runs outside that chokepoint, which is the gateway's decision for its
own calls, so none of them is offered. Use these for a git call you run sandboxed at
the same ``mode`` and ``env``.
"""

from __future__ import annotations

import os

from kiro_crew import git_config_hooks, sandbox
from kiro_crew.apps.backend_sdk.spawn import SpawnPlan, _checked_mode

__all__ = ["GitHookScanError", "git_hook_disable_args", "git_hook_disable_args_async"]

#: Raised when the hook names could not be listed safely (git refused the listing, a
#: name cannot be passed as an override, or there are too many names or nested
#: repositories), so the git call must not run. ``git_stderr`` holds that git's own
#: stderr when it ran and refused. The gateway's own class.
GitHookScanError = git_config_hooks.ConfigHookScanError


def _prepare(argv: list[str], *, mode: str, env: dict[str, str] | None) -> SpawnPlan:
    # The chokepoint is read at call time, so a sandbox tightened later wraps the scan too.
    return sandbox.sandboxed_spawn_argv(argv, mode, env=env)


def git_hook_disable_args(
    cwd: str | os.PathLike[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
) -> list[str]:
    """The ``-c`` entries that turn off every config-defined hook git sees in *cwd*.

    ``kiro_crew.git_config_hooks.config_hook_disable_args_sandboxed`` with the
    gateway's sandbox chokepoint as the spawner: each listing git runs sandboxed at
    *mode* (``standard`` or ``strict``; anything else raises ``ValueError`` before a
    git starts) with *env* scrubbed. Pass the *mode* and *env* your real git call
    uses. A relative *cwd* is taken from this process's working directory, once, so
    the listing reads the repository your git call would. Empty of hook entries when
    there are none; the submodule recursion pins are always there. Raises
    :data:`GitHookScanError` when the names cannot be listed safely, and
    :data:`SandboxUnavailableError` when the host cannot sandbox the listing; refuse
    the git call on either. It blocks on the listing: call it from a worker thread, or
    use :func:`git_hook_disable_args_async` on an event loop.
    """
    return git_config_hooks.config_hook_disable_args_sandboxed(
        cwd, spawn_argv=_prepare, mode=_checked_mode(mode), env=env
    )


async def git_hook_disable_args_async(
    cwd: str | os.PathLike[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
) -> list[str]:
    """:func:`git_hook_disable_args` for an event loop.

    ``kiro_crew.git_config_hooks.config_hook_disable_args_sandboxed_async``: the
    listing runs on the gateway's subprocess worker pool, and each listing git's
    sandbox launcher is created and removed inside one worker call, so a cancelled
    caller cannot leave one behind. Raises as the sync form does.
    """
    return await git_config_hooks.config_hook_disable_args_sandboxed_async(
        cwd, spawn_argv=_prepare, mode=_checked_mode(mode), env=env
    )
