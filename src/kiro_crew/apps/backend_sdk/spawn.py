"""Sandboxed child processes: the gateway's spawn chokepoint, with no widening knob.

:func:`sandboxed_argv` is ``kiro_crew.sandbox.sandboxed_spawn_argv`` limited to the
choices that keep or tighten the sandbox: the ``standard`` tier (credential homes
hidden, git-over-SSH and the AWS CLI usable) or the ``strict`` one (those hidden
too). The private chokepoint's other parameters lift a mask, open a write window
inside a sealed parent or reclassify the child, which is the gateway's decision
for its own spawns and never an app's, so they are not offered.

:func:`run_sandboxed` and :func:`run_sandboxed_async` prepare, run and clean up in
one call, with the gateway's resource limits applied after ``exec``; prefer them.
A caller that needs the child's stdio as a stream prepares with
:func:`sandboxed_argv` instead, runs the returned argv with the returned
environment, and removes the returned cleanup path, when it is not ``None``, once
the child has exited.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from typing import Any

from kiro_crew import platform_compat, sandbox

#: Raised when the gateway could not, or will not, sandbox the child. No child was
#: started. The gateway's own class, so ``except SandboxUnavailableError`` catches
#: what it raises.
SandboxUnavailableError = sandbox.SandboxUnavailableError

#: The tiers an app may ask for. ``off`` and every other value are refused.
SANDBOX_MODES = ("standard", "strict")

SpawnPlan = tuple[list[str], dict[str, str], str | None]

#: The ``subprocess.run`` keywords :func:`run_sandboxed` forwards. Each one shapes
#: the child's stdio, its working directory or how its result is reported. Every
#: other keyword is refused, because ``run_limited`` and ``subprocess.run`` also take
#: ones that add reach: ``profile`` picks the resource limits (``"none"`` lifts them),
#: ``executable`` replaces the program the sandbox wrapper starts, and ``pass_fds`` /
#: ``close_fds`` hand the child this process's descriptors beyond its stdio.
RUN_KWARGS = frozenset(
    {
        "input",
        "capture_output",
        "timeout",
        "check",
        "cwd",
        "stdin",
        "stdout",
        "stderr",
        "text",
        "encoding",
        "errors",
        "universal_newlines",
        "bufsize",
    }
)


def _checked_mode(mode: str) -> str:
    if mode not in SANDBOX_MODES:
        raise ValueError(f"sandbox mode must be one of {SANDBOX_MODES}, not {mode!r}")
    return mode


def _checked_run_kwargs(run_kwargs: dict[str, Any]) -> dict[str, Any]:
    refused = sorted(set(run_kwargs) - RUN_KWARGS)
    if refused:
        raise TypeError(
            f"run_sandboxed does not accept {', '.join(refused)}; "
            f"it forwards only {', '.join(sorted(RUN_KWARGS))}"
        )
    return run_kwargs


def sandboxed_argv(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
) -> SpawnPlan:
    """``(wrapped argv, scrubbed environment, cleanup path)`` for running *argv*.

    *env* is the base environment to scrub (this process's own when ``None``);
    ``strip_python_env`` drops ``PYTHONPATH`` and ``PYTHONHOME`` so a foreign Python
    child does not inherit the gateway's interpreter paths. Raises
    :data:`SandboxUnavailableError` when the host refuses the spawn and
    ``ValueError`` for a *mode* outside :data:`SANDBOX_MODES`.
    """
    return sandbox.sandboxed_spawn_argv(
        argv, _checked_mode(mode), env=env, strip_python_env=strip_python_env
    )


async def sandboxed_argv_async(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
) -> SpawnPlan:
    """:func:`sandboxed_argv` prepared off the event loop.

    ``kiro_crew.sandbox.sandboxed_spawn_argv_async``: the preparation does
    filesystem work, so it runs on a worker thread and is shielded from
    cancellation, which would otherwise leave its cleanup path behind.
    """
    return await sandbox.sandboxed_spawn_argv_async(
        argv, _checked_mode(mode), env=env, strip_python_env=strip_python_env
    )


def _remove_cleanup(cleanup: str | None) -> None:
    if cleanup is not None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(cleanup)


async def _kill_tree(proc: "asyncio.subprocess.Process", group: int | None) -> None:
    """Kill *proc*'s process group *group* (when it has one), then the child itself.

    *group* was read while the child was alive, so it cannot name another group
    while any member of this one lives. Signalling it reaches a descendant that
    outlived the child (``sh -c "work &"``), which ``kill_and_reap`` does not
    group-kill once the child has been reaped.
    """
    if group is not None:
        with contextlib.suppress(Exception):
            platform_compat.kill_process_group(group, platform_compat.SIGKILL)
    await platform_compat.kill_and_reap(proc)


def run_sandboxed(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    **run_kwargs: Any,
) -> "subprocess.CompletedProcess[Any]":
    """Run *argv* to completion inside the sandbox, under the gateway's resource limits.

    :func:`sandboxed_argv`, then ``kiro_crew.sandbox.run_limited`` with the scrubbed
    environment, then the cleanup path removed whatever the outcome. *run_kwargs*
    are the ``subprocess.run`` keywords in :data:`RUN_KWARGS` (``capture_output``,
    ``timeout``, ``cwd``, ``input``, ...); the child always gets the scrubbed
    environment. Any other keyword raises ``TypeError`` before anything is prepared.
    Past ``timeout`` the child is killed (its whole tree on Windows); a POSIX
    descendant that outlives it is not, so use :func:`run_sandboxed_async` for a
    child that leaves work running in the background.
    """
    forwarded = _checked_run_kwargs(run_kwargs)
    wrapped, scrubbed, cleanup = sandbox.sandboxed_spawn_argv(
        argv, _checked_mode(mode), env=env, strip_python_env=strip_python_env
    )
    try:
        return sandbox.run_limited(wrapped, env=scrubbed, **forwarded)
    finally:
        _remove_cleanup(cleanup)


async def run_sandboxed_async(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    input: bytes | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
) -> "subprocess.CompletedProcess[bytes]":
    """:func:`run_sandboxed` for an event loop, with output captured.

    The spawn is prepared off the loop (``sandboxed_spawn_argv_async``), started
    through ``kiro_crew.sandbox.create_subprocess_limited`` with the scrubbed
    environment and piped stdio, in a process group of its own on POSIX, and given
    *input*. Past *timeout*, or when the caller is cancelled, that group is killed,
    reaching a descendant that outlived the child, then the child is killed and
    reaped (``platform_compat.kill_and_reap``, its whole tree on Windows), and
    ``subprocess.TimeoutExpired`` (or the cancellation) propagates. A descendant
    that ``setsid()``-ed into a session of its own is outside the group. The
    cleanup path is removed on every outcome.
    """
    wrapped, scrubbed, cleanup = await sandbox.sandboxed_spawn_argv_async(
        argv, _checked_mode(mode), env=env, strip_python_env=strip_python_env
    )
    try:
        proc = await sandbox.create_subprocess_limited(
            *wrapped,
            env=scrubbed,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if input is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Its own process group on POSIX, so the timeout and cancel cleanup can
            # kill the child's descendants too.
            start_new_session=platform_compat.IS_POSIX,
        )
        group = platform_compat._isolated_group_of_live_child(
            proc.pid, reaped=proc.returncode is not None
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(input), timeout)
        except asyncio.TimeoutError:
            await _kill_tree(proc, group)
            raise subprocess.TimeoutExpired(argv, timeout or 0.0) from None
        except BaseException:
            await _kill_tree(proc, group)
            raise
        return subprocess.CompletedProcess(argv, proc.returncode or 0, stdout, stderr)
    finally:
        _remove_cleanup(cleanup)
