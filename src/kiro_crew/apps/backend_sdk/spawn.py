"""Sandboxed child processes: the gateway's spawn chokepoint, with no widening knob.

:func:`sandboxed_argv` is ``kiro_crew.sandbox.sandboxed_spawn_argv`` limited to the
choices that keep or tighten the sandbox: the ``standard`` tier (credential homes
hidden, git-over-SSH and the AWS CLI usable) or the ``strict`` one (those hidden
too). The private chokepoint's other parameters lift a mask, open a write window
inside a sealed parent or reclassify the child, which is the gateway's decision
for its own spawns and never an app's, so they are not offered. The one mask option
offered, ``extra_hidden_dirs`` (seam level 18), only adds: it hides more of the disk
from the child and lifts nothing the tier hides.

:func:`run_sandboxed` and :func:`run_sandboxed_async` prepare, run and clean up in
one call, with the gateway's resource limits applied after ``exec``; prefer them.
A caller that needs the child's stdio as a stream prepares with
:func:`sandboxed_argv` instead, runs the returned argv with the returned
environment, and removes the returned cleanup path, when it is not ``None``, once
the child has exited.

:func:`minimal_env` is the base environment the gateway gives its own install and
build children: this process's location hints and nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat, sandbox
from kiro_crew.apps.registry_pipeline import subprocess_env

#: Raised when the gateway could not, or will not, sandbox the child. No child was
#: started. The gateway's own class, so ``except SandboxUnavailableError`` catches
#: what it raises.
SandboxUnavailableError = sandbox.SandboxUnavailableError

#: The tiers an app may ask for. ``off`` and every other value are refused.
SANDBOX_MODES = ("standard", "strict")

#: Bytes one step of a capped drain reads from a pipe. It bounds one allocation, so a
#: child is stopped within one step of passing its cap.
_PIPE_CHUNK_BYTES = 64 * 1024


class OutputLimitExceeded(subprocess.SubprocessError):
    """A :func:`run_sandboxed_async` child wrote more than its ``max_output_bytes``.

    Raised once the child and its process group were killed and the child reaped.
    ``limit`` is the cap, and ``stdout`` and ``stderr`` are what was kept, at most
    ``limit`` bytes of each. ``cmd`` is the argv as the caller passed it; the
    message does not quote it.
    """

    def __init__(self, cmd: list[str], limit: int, stdout: bytes, stderr: bytes) -> None:
        super().__init__(f"the child wrote more than {limit} bytes to a pipe and was stopped")
        self.cmd = cmd
        self.limit = limit
        self.stdout = stdout
        self.stderr = stderr


class _PipeOverflow(Exception):
    """Internal: a pipe passed the cap *limit*. Carries what each pipe kept."""

    def __init__(self, limit: int, stdout: bytes, stderr: bytes) -> None:
        super().__init__()
        self.limit = limit
        self.stdout = stdout
        self.stderr = stderr


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


def _checked_output_cap(cap: int | None) -> int | None:
    # None keeps the uncapped read; any other value must be a positive int, so the cap
    # can only lower what the caller receives.
    if cap is None:
        return None
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError(f"max_output_bytes must be a positive int or None, not {cap!r}")
    return cap


def _ceiling_targets() -> tuple[str, ...]:
    """The data home's read-only leaves, as absolute paths, in each spelling the sandbox seals.

    ``sandbox._CREW_READONLY_TARGETS`` under the home directory (both data-home
    spellings) and ``sandbox._relocated_crew_targets`` for a data home moved out from
    under it: the governance ceilings (``security_policy.json``, ``profiles``,
    ``admission_policy.json``, ``computer_use.json``) and the gateway-managed records
    in-sandbox code reads.
    """
    home = str(Path.home())
    return (
        *(os.path.join(home, rel) for rel in sandbox._CREW_READONLY_TARGETS),
        *sandbox._relocated_crew_targets(tuple(sandbox._CREW_READONLY_LEAVES)),
    )


def _spellings(path: str) -> set[str]:
    return {os.path.normcase(os.path.normpath(path)), os.path.normcase(os.path.realpath(path))}


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent.rstrip(os.sep) + os.sep)


def _ceiling_overlap(path: str) -> str | None:
    """The read-only leaf *path* is, holds or sits in, or ``None`` when it touches none."""
    mine = _spellings(path)
    for target in _ceiling_targets():
        for theirs in _spellings(target):
            if any(_within(m, theirs) or _within(theirs, m) for m in mine):
                return target
    return None


def _normalized_hidden_dirs(dirs: Iterable[str | os.PathLike[str]] | None) -> tuple[str, ...]:
    """*dirs* as absolute paths, each made absolute once here, or a ``TypeError``/``ValueError``.

    A relative entry is resolved against this process's working directory, the one
    place it is resolved, so the overlap check and the chokepoint read the same path.
    """
    if dirs is None:
        return ()
    if isinstance(dirs, (str, bytes, os.PathLike)):
        raise TypeError("extra_hidden_dirs takes a list of paths, not one path")
    hidden: list[str] = []
    for entry in dirs:
        if not isinstance(entry, (str, os.PathLike)):
            raise TypeError(
                f"an extra_hidden_dirs entry must be a str or os.PathLike, not {type(entry).__name__}"
            )
        path = os.fspath(entry)
        if not isinstance(path, str) or not path or "\x00" in path:
            raise ValueError("an extra_hidden_dirs entry must be a non-empty str path")
        hidden.append(os.path.abspath(path))
    return tuple(dict.fromkeys(hidden))


def _refuse_unsafe_hidden_dirs(hidden: tuple[str, ...], mode: str) -> None:
    """Refuse *hidden* when it would remove a ceiling, or when this spawn would drop it.

    An entry that is, holds or sits in one of the data home's read-only leaves raises
    ``ValueError``: hiding a ceiling from the process that enforces it reads as the
    permissive default, so that mask would remove the ceiling rather than protect it.
    ``sandbox.credential_mask_applies`` names the outcomes that hand back a child the
    masks never reach (no backend, a spawn already inside a Kiro Crew sandbox); those
    raise :data:`SandboxUnavailableError`, so the child is not started with the paths
    readable. It resolves paths and can probe the backend, so it blocks.
    """
    for path in hidden:
        ceiling = _ceiling_overlap(path)
        if ceiling is not None:
            raise ValueError(
                f"extra_hidden_dirs may not hide {path}: it overlaps {ceiling}, "
                "which the sandbox keeps readable and never writable"
            )
    if hidden and not sandbox.credential_mask_applies(mode):
        raise SandboxUnavailableError(
            "extra_hidden_dirs cannot be applied to a child on this host, so no child was "
            "started",
            kind="no_backend",
            detail="the sandbox would not carry extra_hidden_dirs for this spawn",
        )


def _hidden_dirs_for(dirs: Iterable[str | os.PathLike[str]] | None, mode: str) -> tuple[str, ...]:
    hidden = _normalized_hidden_dirs(dirs)
    if hidden:
        _refuse_unsafe_hidden_dirs(hidden, mode)
    return hidden


async def _hidden_dirs_off_loop(
    dirs: Iterable[str | os.PathLike[str]] | None, mode: str
) -> tuple[str, ...]:
    hidden = _normalized_hidden_dirs(dirs)
    if hidden:
        await asyncio.to_thread(_refuse_unsafe_hidden_dirs, hidden, mode)
    return hidden


def _hidden_option(hidden: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
    return {"extra_hidden_dirs": hidden} if hidden else {}


def minimal_env(**extra: str) -> dict[str, str]:
    """This process's environment cut to location hints, plus *extra* (seam level 17).

    The ``kiro_crew.apps.registry`` facade's ``minimal_env``, looked up at call time on
    its owner ``registry_pipeline.subprocess_env``, which a write through the facade
    also reaches; importing the facade would load the whole App Registry into a
    spawned backend. It is the allowlist the gateway gives its own install and build
    children (``PATH``, ``HOME``, the locale, the temp and per-user directories, the
    SSH agent socket and a few tool homes, with their Windows spellings), so API keys,
    tokens and cloud credentials in this process's environment are not passed on. It
    only removes variables this process already has; *extra* is added as given. Pass
    it as ``env`` to :func:`run_sandboxed`, which scrubs it again.
    """
    return subprocess_env.minimal_env(**extra)


def sandboxed_argv(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: Iterable[str | os.PathLike[str]] = (),
) -> SpawnPlan:
    """``(wrapped argv, scrubbed environment, cleanup path)`` for running *argv*.

    *env* is the base environment to scrub (this process's own when ``None``);
    ``strip_python_env`` drops ``PYTHONPATH`` and ``PYTHONHOME`` so a foreign Python
    child does not inherit the gateway's interpreter paths. Raises
    :data:`SandboxUnavailableError` when the host refuses the spawn and
    ``ValueError`` for a *mode* outside :data:`SANDBOX_MODES`.

    *extra_hidden_dirs* (seam level 18) are more paths to hide from the child, on top
    of everything the tier hides; nothing the tier hides can be lifted by them. Each
    is made absolute once, against this process's working directory. An entry that
    overlaps one of the data home's read-only leaves (the governance ceilings among
    them) raises ``ValueError``, and a spawn that would not carry the paths raises
    :data:`SandboxUnavailableError`; nothing is prepared on either.
    """
    checked = _checked_mode(mode)
    hidden = _hidden_dirs_for(extra_hidden_dirs, checked)
    return sandbox.sandboxed_spawn_argv(
        argv, checked, env=env, strip_python_env=strip_python_env, **_hidden_option(hidden)
    )


async def sandboxed_argv_async(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: Iterable[str | os.PathLike[str]] = (),
) -> SpawnPlan:
    """:func:`sandboxed_argv` prepared off the event loop.

    ``kiro_crew.sandbox.sandboxed_spawn_argv_async``: the preparation does
    filesystem work, so it runs on a worker thread and is shielded from
    cancellation, which would otherwise leave its cleanup path behind.
    *extra_hidden_dirs* is checked as :func:`sandboxed_argv` checks it, off the loop.
    """
    checked = _checked_mode(mode)
    hidden = await _hidden_dirs_off_loop(extra_hidden_dirs, checked)
    return await sandbox.sandboxed_spawn_argv_async(
        argv, checked, env=env, strip_python_env=strip_python_env, **_hidden_option(hidden)
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


async def _stop_capped(proc: "asyncio.subprocess.Process", group: int | None) -> None:
    """:func:`_kill_tree` for a capped run: the same kills, then a reap that keeps nothing.

    ``kill_and_reap`` reaps through ``communicate()``, which keeps everything still
    written to the pipes, and a descendant that ``setsid()``-ed out of the group survives
    the kill holding them. So the capped run's cleanup sends the same signals (the
    group, the tree, the child), then drains both pipes in bounded reads that are
    discarded and waits for the child (``platform_compat._discard_output_until_exit``),
    under the same reap deadline, in the same shielded task. Past the deadline nothing
    reads the pipes, so a writer still alive there is held back by the pipe's flow
    control, not by this process's memory.
    """

    async def _cleanup() -> None:
        if group is not None:
            with contextlib.suppress(Exception):
                platform_compat.kill_process_group(group, platform_compat.SIGKILL)
        if proc.returncode is None and not platform_compat._shares_own_process_group(proc.pid):
            with contextlib.suppress(Exception):
                await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGKILL)
        with contextlib.suppress(Exception):
            proc.kill()
        with contextlib.suppress(Exception, asyncio.TimeoutError):
            await asyncio.wait_for(
                platform_compat._discard_output_until_exit(proc),
                timeout=platform_compat.REAP_TIMEOUT_SECS,
            )

    await platform_compat._run_cleanup_shielded(_cleanup())


def run_sandboxed(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    extra_hidden_dirs: Iterable[str | os.PathLike[str]] = (),
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
    child that leaves work running in the background. *extra_hidden_dirs* (seam level
    18) is checked as :func:`sandboxed_argv` checks it, before anything is prepared.
    """
    forwarded = _checked_run_kwargs(run_kwargs)
    checked = _checked_mode(mode)
    hidden = _hidden_dirs_for(extra_hidden_dirs, checked)
    wrapped, scrubbed, cleanup = sandbox.sandboxed_spawn_argv(
        argv, checked, env=env, strip_python_env=strip_python_env, **_hidden_option(hidden)
    )
    try:
        return sandbox.run_limited(wrapped, env=scrubbed, **forwarded)
    finally:
        _remove_cleanup(cleanup)


async def _communicate_capped(
    proc: "asyncio.subprocess.Process", input: bytes | None, limit: int
) -> tuple[bytes, bytes]:
    """``proc.communicate(input)``, keeping at most *limit* bytes of each pipe.

    Both pipes are drained at once, as ``communicate`` drains them, so a child that
    fills one pipe while the other is read cannot deadlock. Raises
    :class:`_PipeOverflow` once either pipe passes *limit*, after stopping the other
    reads, so the caller's reap is the only reader left on the pipes.
    """
    kept = (bytearray(), bytearray())

    async def drain(stream: asyncio.StreamReader | None, buf: bytearray) -> None:
        if stream is None:
            return
        while chunk := await stream.read(_PIPE_CHUNK_BYTES):
            room = limit - len(buf)
            buf += chunk[:room]
            if len(chunk) > room:
                raise _PipeOverflow(limit, bytes(kept[0]), bytes(kept[1]))

    async def feed() -> None:
        stdin = proc.stdin
        if stdin is None:
            return
        try:
            if input:
                stdin.write(input)
                await stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child stopped reading its input; its output is still collected
        finally:
            stdin.close()

    tasks = [
        asyncio.ensure_future(drain(proc.stdout, kept[0])),
        asyncio.ensure_future(drain(proc.stderr, kept[1])),
        asyncio.ensure_future(feed()),
    ]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    await proc.wait()
    return bytes(kept[0]), bytes(kept[1])


async def run_sandboxed_async(
    argv: list[str],
    *,
    mode: str = "standard",
    env: dict[str, str] | None = None,
    strip_python_env: bool = False,
    input: bytes | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    max_output_bytes: int | None = None,
    extra_hidden_dirs: Iterable[str | os.PathLike[str]] = (),
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

    *max_output_bytes* (seam level 17) caps what is kept of each of stdout and
    stderr. ``None``, the default, keeps everything, as before. Once either pipe
    passes the cap, the child is stopped and :class:`OutputLimitExceeded` is raised
    with what was kept, so a child that floods its pipes cannot grow this process.
    No exit of a capped run reads without a bound: past the cap, a timeout or a
    cancel, the cleanup kills as the uncapped one does but drains what is left in
    bounded reads and discards it. The cap only lowers what is returned: anything but
    a positive int raises ``ValueError`` before anything is prepared.

    *extra_hidden_dirs* (seam level 18) is checked as :func:`sandboxed_argv` checks
    it, off the loop and before anything is prepared.
    """
    cap = _checked_output_cap(max_output_bytes)
    checked = _checked_mode(mode)
    hidden = await _hidden_dirs_off_loop(extra_hidden_dirs, checked)
    wrapped, scrubbed, cleanup = await sandbox.sandboxed_spawn_argv_async(
        argv, checked, env=env, strip_python_env=strip_python_env, **_hidden_option(hidden)
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
        if cap is None:
            read = proc.communicate(input)
            stop = _kill_tree
        else:
            read = _communicate_capped(proc, input, cap)
            stop = _stop_capped
        try:
            stdout, stderr = await asyncio.wait_for(read, timeout)
        except asyncio.TimeoutError:
            await stop(proc, group)
            raise subprocess.TimeoutExpired(argv, timeout or 0.0) from None
        except _PipeOverflow as overflow:
            await stop(proc, group)
            raise OutputLimitExceeded(
                argv, overflow.limit, overflow.stdout, overflow.stderr
            ) from None
        except BaseException:
            await stop(proc, group)
            raise
        return subprocess.CompletedProcess(argv, proc.returncode or 0, stdout, stderr)
    finally:
        _remove_cleanup(cleanup)
