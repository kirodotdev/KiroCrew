"""Running one fixed kiro-cli argv in the OS sandbox, bounded, and reaping its tree.

:func:`_run_process` is the single spawn this family makes for a probe or an auth call:
sandbox-wrapped, under the process-group supervisor, with captured output bounded and
the whole tree terminated on timeout or cancellation (Windows: a retained-handle tree,
spawned on a private loop). :func:`spawn_supervised_oneshot` is the same supervisor
for other modules' one-shot ``kiro-cli`` calls. :class:`ProcessResult` is what a run
reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import _process_group_supervisor, platform_compat
from kiro_crew.executors import kiro_spawn_executor
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    corroborate_launcher_refusal,
    create_subprocess_limited,
    launcher_refusal,
    resource_limit_supervisor_argv,
    sandboxed_spawn_argv,
    shielded_prepare_off_loop,
)

logger = logging.getLogger("kiro_crew.kiro_prerequisite")


_MAX_CAPTURED_OUTPUT = 64 * 1024


_TERMINATION_GRACE_SECS = 2.0
_WINDOWS_DESCENDANT_POLL_SECS = 0.05


_UNVERIFIED_SANDBOX_MODE = "strict"


_PROCESS_GROUP_SUPERVISOR = str(Path(__file__).with_name("_process_group_supervisor.py"))
_PROCESS_GROUP_SUPERVISOR_ERROR = "Kiro process-group supervisor is unavailable"
try:
    _PROCESS_GROUP_SUPERVISOR_CODE = Path(_PROCESS_GROUP_SUPERVISOR).read_text(encoding="utf-8")
except OSError:
    _PROCESS_GROUP_SUPERVISOR_CODE = ""


@functools.cache
def _host_can_reap() -> bool:
    """Whether the supervisor's ``--reap-survivors`` can act on this host.

    Probed here, in the gateway, with the supervisor's own check: the child runs
    on the same host and kernel, so the answer is the same there.
    """
    return platform_compat.IS_POSIX and _process_group_supervisor.can_reap()


async def spawn_supervised_oneshot(argv: list[str], **kwargs: Any) -> asyncio.subprocess.Process:
    """Spawn a one-shot ``kiro-cli`` call that cleans up what it leaves behind.

    For fixed-argv calls that return (``--list-models``, ``whoami``, the
    ``/usage`` scrape). A kiro-cli launcher wrapper can start a credential
    helper of about 140 threads for such a call and leave it running when the
    call returns. Here the call runs under the process-group supervisor in
    ``--reap-survivors`` mode, in its own session: the supervisor stays the
    group leader for the whole call and then ends what the command left in the
    group. Pass the already sandbox- and cgroup-wrapped *argv*; the supervisor
    goes outermost. Keyword arguments go to ``create_subprocess_limited``.

    The supervisor execs its target without a PATH lookup, so a wrapper that is
    not already absolute is resolved to a trusted system binary first, off the
    loop since a miss walks all of PATH. Where the supervisor is unavailable
    (Windows, or its source could not be captured) or the wrapper cannot be
    resolved, the call runs unsupervised, still in its own session on POSIX
    (Windows ignores ``start_new_session``). So does a
    host that cannot reap (no pidfd, e.g. macOS): there the supervisor would only
    wait for the leftovers instead of ending them, which would hold the call open.

    Every call is spawned with ``CREATE_NO_WINDOW`` (``0`` off Windows) unless
    the caller passes its own ``creationflags``: these are background helpers,
    and a console-subsystem child of the console-less gateway otherwise gets a
    console of its own that flashes on screen at each call.
    """
    supervised = False
    if _PROCESS_GROUP_SUPERVISOR_CODE and argv and _host_can_reap():
        target = argv
        if not os.path.isabs(target[0]):
            resolved = await asyncio.to_thread(platform_compat.trusted_system_bin, target[0])
            target = [resolved, *target[1:]] if resolved else []
            if not resolved:
                logger.debug("%s is not a trusted system binary; spawning unsupervised", argv[0])
        if target:
            supervised = True
            argv = [
                sys.executable,
                "-I",
                "-c",
                _PROCESS_GROUP_SUPERVISOR_CODE,
                "--reap-survivors",
                *target,
            ]
    if not supervised:
        _note_unsupervised_once()
    kwargs.setdefault("creationflags", platform_compat._SUBPROCESS_NO_WINDOW)
    return await create_subprocess_limited(*argv, start_new_session=True, **kwargs)


_unsupervised_noted = False


def _note_unsupervised_once() -> None:
    """Say once per process that one-shot calls run without leftover cleanup.

    Debug-level detail stays per call; this single INFO line is what makes a
    host that keeps leaking helpers diagnosable from the gateway log.
    """
    global _unsupervised_noted
    if _unsupervised_noted:
        return
    _unsupervised_noted = True
    logger.info(
        "One-shot kiro-cli calls run without the reaping supervisor on this host "
        "(no pidfd support, supervisor source unavailable, or wrapper not a trusted "
        "system binary); helpers they leave behind are not cleaned up"
    )


@dataclass
class ProcessResult:
    """Bounded result from one fixed child process."""

    ok: bool
    output: str = ""
    returncode: int | None = None
    timed_out: bool = False
    error: str = ""
    # ``(kind, detail, remedy)`` when the spawn was refused because the sandbox
    # could not be built. Set from exactly two structural sources and never
    # inferred from host capability or from the child's text: the typed
    # SandboxUnavailableError raised BEFORE the spawn, and -- when the launcher
    # refused AFTER the spawn -- a fresh run of the real launcher around a
    # trusted no-op (``sandbox.corroborate_launcher_refusal``), which the
    # candidate's stderr line merely triggers and whose own stderr is the
    # verdict. A trusted run that succeeds, times out, or refuses over host
    # state leaves this None, so an unrelated failure can never be misreported
    # as a sandbox problem, and a child cannot print its way into one.
    sandbox_failure: tuple[str, str, str] | None = None


ProcessRunner = Callable[..., Awaitable[ProcessResult]]


def _append_capped(current: str, chunk: str) -> str:
    combined = current + str(chunk or "").replace("\r", "")
    if len(combined) <= _MAX_CAPTURED_OUTPUT:
        return combined
    return combined[-_MAX_CAPTURED_OUTPUT:]


async def _terminate_process(
    proc: asyncio.subprocess.Process,
    windows_descendants: dict[int, int] | None = None,
) -> None:
    group_signalled = False
    if platform_compat.IS_POSIX:
        if proc.returncode is None:
            try:
                # Resolve the group through the still-live leader instead of
                # signalling a retained numeric PGID that the OS could reuse.
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGTERM,
                )
                group_signalled = True
            except (ProcessLookupError, OSError, ValueError):
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
    else:
        if proc.returncode is None:
            try:
                # asyncio retains the real Windows process handle, so this
                # targets the original process even if its PID is later reused.
                proc.terminate()
            except ProcessLookupError:
                pass

    leader_exited = proc.returncode is not None
    if not leader_exited:
        try:
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)
            leader_exited = True
        except asyncio.TimeoutError:
            pass

    if platform_compat.IS_POSIX:
        if group_signalled and not leader_exited:
            # The live group leader anchors the PGID identity. Once it exits,
            # never signal that retained integer because POSIX may reuse it.
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                await platform_compat.kill_process_tree_async(
                    proc.pid,
                    platform_compat.SIGKILL,
                )
        elif not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    else:
        # Retained Windows process handles refer to the original kernel process
        # objects, unlike PIDs, which may be recycled during a long operation.
        for handle in tuple((windows_descendants or {}).values()):
            with contextlib.suppress(ProcessLookupError, OSError, ValueError):
                platform_compat.terminate_process_handle(handle)
        if not leader_exited:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    if not leader_exited:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_TERMINATION_GRACE_SECS)


async def _track_windows_descendants(
    proc: asyncio.subprocess.Process,
    tracked: dict[int, int],
    primary_root_handle: int | None = None,
    initial_snapshot: asyncio.Future[None] | None = None,
    primary_terminal_snapshot: asyncio.Future[None] | None = None,
) -> None:
    """Retain the complete Windows tree until every observed anchor exits."""

    primary_snapshot_pending = primary_root_handle is not None
    primary_terminally_scanned = primary_root_handle is None and proc.returncode is not None
    terminally_scanned: set[int] = set()
    try:
        while True:
            descendant_roots: list[tuple[int, int, bool]] = []
            for pid, handle in tuple(tracked.items()):
                active_before = platform_compat.process_handle_active(handle)
                if active_before or pid not in terminally_scanned:
                    descendant_roots.append((pid, handle, active_before))
            primary_active = (
                platform_compat.process_handle_active(primary_root_handle)
                if primary_root_handle is not None
                else proc.returncode is None
            )
            primary_needs_scan = (
                not primary_terminally_scanned
                if primary_root_handle is not None
                else primary_active
            )
            roots = (
                [(proc.pid, primary_root_handle, True, primary_active)]
                if primary_needs_scan
                else []
            ) + [
                (root_pid, retained_handle, False, active_before)
                for root_pid, retained_handle, active_before in descendant_roots
            ]
            if not roots:
                return
            for (
                root_pid,
                retained_root_handle,
                is_primary_root,
                root_active_before,
            ) in roots:
                initial_primary_snapshot = is_primary_root and primary_snapshot_pending
                try:
                    discovered = await platform_compat.descendant_termination_handles_async(
                        root_pid,
                        tracked,
                        retained_root_handle,
                    )
                    # The platform snapshot validates each numeric Toolhelp
                    # parent edge against exact-handle creation/exit times. A
                    # root that exits during discovery can therefore contribute
                    # genuine children without admitting a recycled PID's tree.
                    tracked.update(discovered)
                except (OSError, ValueError) as exc:
                    if initial_primary_snapshot and initial_snapshot is not None:
                        if not initial_snapshot.done():
                            initial_snapshot.set_exception(exc)
                        return
                    if (
                        is_primary_root
                        and not root_active_before
                        and primary_terminal_snapshot is not None
                    ):
                        if not primary_terminal_snapshot.done():
                            primary_terminal_snapshot.set_exception(exc)
                        return
                    raise
                root_active = (
                    platform_compat.process_handle_active(retained_root_handle)
                    if retained_root_handle is not None
                    else proc.returncode is None
                )
                if is_primary_root:
                    primary_terminally_scanned = not root_active_before and not root_active
                    if (
                        primary_terminally_scanned
                        and primary_terminal_snapshot is not None
                        and not primary_terminal_snapshot.done()
                    ):
                        primary_terminal_snapshot.set_result(None)
                elif root_active or root_active_before:
                    terminally_scanned.discard(root_pid)
                else:
                    terminally_scanned.add(root_pid)
                if initial_primary_snapshot:
                    primary_snapshot_pending = False
                    if initial_snapshot is not None and not initial_snapshot.done():
                        initial_snapshot.set_result(None)
            await asyncio.sleep(_WINDOWS_DESCENDANT_POLL_SECS)
    finally:
        if initial_snapshot is not None and not initial_snapshot.done():
            initial_snapshot.cancel()
        if primary_terminal_snapshot is not None and not primary_terminal_snapshot.done():
            primary_terminal_snapshot.cancel()


async def _unlink_off_loop(path: str | None) -> None:
    """Remove a sandbox launcher/profile without blocking the event loop."""

    if not path:
        return

    def _unlink() -> None:
        with contextlib.suppress(OSError):
            os.unlink(path)

    await asyncio.to_thread(_unlink)


async def _prepare_sandboxed_spawn(
    argv: list[str],
    *,
    mode: str,
    env: dict[str, str],
    extra_hidden_dirs: tuple[str, ...],
    extra_visible_dirs: tuple[str, ...],
) -> tuple[list[str], dict[str, str], str | None]:
    """Prepare filesystem-heavy sandbox state on a worker thread.

    Delegates to the shared :func:`shielded_prepare_off_loop`, which owns the
    shield-and-recover pattern (including its repeat-cancellation semantics)
    for every async caller of the chokepoint.  The chokepoint call itself
    stays in this module so the ``mode``/``strip_python_env`` policy — and this
    module's own seam over ``sandboxed_spawn_argv`` — remain local.
    """
    return await shielded_prepare_off_loop(
        functools.partial(
            sandboxed_spawn_argv,
            argv,
            mode=mode,
            env=env,
            strip_python_env=True,
            extra_hidden_dirs=extra_hidden_dirs,
            extra_visible_dirs=extra_visible_dirs,
        )
    )


async def _run_on_private_loop(
    make_coro: Callable[[], Coroutine[Any, Any, ProcessResult]],
) -> ProcessResult:
    """Run one spawn on a PRIVATE event loop owned by a worker thread.

    Windows only, and it exists for one call: ``CreateProcess``. CPython performs
    it SYNCHRONOUSLY on whichever thread asks for the child, and on the Proactor
    loop there is no await point in between --
    ``ProactorEventLoop._make_subprocess_transport`` constructs
    ``_WindowsSubprocessTransport`` before its first ``await waiter``, that
    constructor calls ``self._start()``, and ``_start`` calls
    ``windows_utils.Popen`` -> ``subprocess.Popen._execute_child`` ->
    ``CreateProcess``. So the loop thread is inside ``CreateProcess`` for its
    whole duration, which on an image with an endpoint-protection filter driver
    is seconds, and nothing else on that loop advances.

    ``asyncio.to_thread(asyncio.create_subprocess_exec, ...)`` does NOT fix that,
    which is why the offload is a whole loop rather than one call: that is a
    coroutine FUNCTION, so the worker thread would only build a coroutine object
    and hand it back unawaited -- no child is spawned there at all, and awaiting
    the result on the gateway loop performs the identical on-loop
    ``CreateProcess``. Nothing narrower works either: asyncio offers no way to
    adopt an already-spawned ``Popen`` into a subprocess transport, so the only
    place the spawn can happen off the gateway loop is on another loop.

    A private loop rather than a rewrite to blocking ``Popen`` keeps the Windows
    retained-tree guarantee byte-for-byte: the descendant tracker, the exact-handle
    snapshots and the terminate path all still run against a real
    ``asyncio.subprocess.Process`` on a real Proactor loop, just not this one.
    ``asyncio.run`` off the main thread is fine on Windows -- it installs signal
    handlers only on the main thread -- and the loop is closed when the call
    returns, so none stays bound to the pooled worker. It is spelled through
    ``asyncio.Runner``, which is literally what ``asyncio.run`` uses internally
    (same loop creation, same task-cancellation and async-generator shutdown on
    close), because ``asyncio.run`` would trip test_spawn_audit: that scanner
    matches ``<spawn module>.<spawn attr>`` and carries ``asyncio`` as a module
    and ``run`` as an attr in order to catch ``subprocess.run``, so the name
    collides. Nothing here spawns a child -- the spawn is in ``_run_process``,
    which the audit already lists -- so allowlisting this function would put a
    non-spawn in a list of judged-benign spawns. Do not simplify it back.

    Residue, deliberate: a started ``run_in_executor`` future cannot be
    cancelled, so cancelling the awaiting task does not reach the worker. The
    child is then reaped by the body's own ``timeout_secs`` rather than at once,
    and the ``finally`` there still terminates the tree, closes every retained
    handle and removes the cleanup path -- so a cancel delays the reap, it does
    not leak a process. Making the cancel prompt needs a signal threaded through
    that teardown, which is its own change.
    """

    def _own_loop() -> ProcessResult:
        with asyncio.Runner() as runner:
            return runner.run(make_coro())

    return await asyncio.get_running_loop().run_in_executor(
        kiro_spawn_executor(),
        _own_loop,
    )


async def _run_process(
    command: str,
    args: list[str],
    *,
    env: dict[str, str],
    timeout_secs: float,
    sandbox_mode: str = _UNVERIFIED_SANDBOX_MODE,
    extra_hidden_dirs: tuple[str, ...] = (),
    extra_visible_dirs: tuple[str, ...] = (),
    _on_private_loop: bool = False,
) -> ProcessResult:
    """Run one fixed argv with bounded output, always inside the OS sandbox.

    There is no opt-out: every spawn this module makes is a probe or a Kiro auth
    call, and all of them are sandboxed. Nothing here may run a child
    unsandboxed.

    On Windows this re-enters itself once, on a private event loop owned by a
    pooled worker thread, because the spawn below reaches ``CreateProcess``
    synchronously on whatever loop asks for the child -- see
    :func:`_run_on_private_loop` for why that is the narrowest offload available
    and why the POSIX path needs none. ``_on_private_loop`` is that re-entry's
    own marker and is private: nothing outside this function may set it, and a
    caller that did would put the spawn back on its own loop.
    """

    if platform_compat.IS_WINDOWS and not _on_private_loop:
        return await _run_on_private_loop(
            functools.partial(
                _run_process,
                command,
                args,
                env=env,
                timeout_secs=timeout_secs,
                sandbox_mode=sandbox_mode,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
                _on_private_loop=True,
            )
        )

    if platform_compat.IS_POSIX and not _PROCESS_GROUP_SUPERVISOR_CODE:
        return ProcessResult(ok=False, error=_PROCESS_GROUP_SUPERVISOR_ERROR)

    output = ""
    cleanup_path: str | None = None
    creationflags = platform_compat.CREATE_NEW_PROCESS_GROUP
    if platform_compat.IS_WINDOWS:
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        spawn_argv = [command, *args]
        spawn_env = env
        if not platform_compat.IS_WINDOWS:
            # An absolute system `env` entrypoint prevents the generic sandbox
            # layer from mistaking an unverified candidate named `kiro-cli` for
            # the trusted provider spawn that may delegate to Kiro's internal
            # macOS sandbox. The candidate still executes inside the requested
            # outer sandbox with its original absolute path and argv.
            spawn_argv = ["/usr/bin/env", *spawn_argv]
            spawn_argv, spawn_env, cleanup_path = await _prepare_sandboxed_spawn(
                spawn_argv,
                mode=sandbox_mode,
                env=env,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
            if spawn_argv and not os.path.isabs(spawn_argv[0]):
                # Off the loop: a miss walks the whole ``PATH`` once per name to
                # report where the tool actually is, so a stalled network mount
                # anywhere on it would otherwise freeze the gateway rather than
                # just this probe.
                resolved_wrapper = await asyncio.to_thread(
                    platform_compat.trusted_system_bin, spawn_argv[0]
                )
                if not resolved_wrapper:
                    raise OSError(f"sandbox wrapper is unavailable: {spawn_argv[0]}")
                spawn_argv[0] = resolved_wrapper
        if platform_compat.IS_POSIX:
            # The immutable, gateway-captured supervisor is the outermost
            # process. Putting it inside the Linux namespace launcher makes the
            # two parent wait loops depend on each other; loading it from a
            # mutable package path would also let a same-UID agent replace code
            # immediately before an owner-triggered install.
            #
            # It also carries the resource limits (``--rlimits=``) rather than a
            # ``preexec_fn``. See resource_limit_supervisor_argv: a
            # preexec_fn forces a plain fork() of this multi-threaded gateway and
            # runs Python in the child before exec, which can wedge that child
            # in a futex with the fds it inherited pinned. The supervisor
            # applies the same setrlimits after exec, single-threaded, and the
            # exec'd child inherits them.
            spawn_argv = [
                sys.executable,
                "-I",
                "-c",
                _PROCESS_GROUP_SUPERVISOR_CODE,
                *resource_limit_supervisor_argv(),
                *spawn_argv,
            ]
        proc = await asyncio.create_subprocess_exec(
            *spawn_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=spawn_env,
            start_new_session=platform_compat.IS_POSIX,
            creationflags=creationflags,
        )
    except SandboxUnavailableError as exc:
        # The sandbox itself refused this spawn. Record it structurally so the
        # caller can report "present but unverifiable" without guessing from
        # host state — on Windows this wrap is skipped entirely, and the
        # allow_unsandboxed_exec opt-in bypasses it, so host capability is not
        # evidence about why THIS spawn failed.
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(
            ok=False, error=str(exc), sandbox_failure=(exc.kind, exc.detail, exc.remedy)
        )
    except (OSError, RuntimeError) as exc:
        await _unlink_off_loop(cleanup_path)
        return ProcessResult(ok=False, error=str(exc))

    windows_descendants: dict[int, int] = {}
    windows_root_handle: int | None = None
    descendant_task: asyncio.Task[None] | None = None
    initial_snapshot: asyncio.Future[None] | None = None
    primary_terminal_snapshot: asyncio.Future[None] | None = None
    if platform_compat.IS_WINDOWS:
        # Anchor the primary kernel object before yielding after spawn. Without
        # this handle, a launcher that exits immediately could leave helpers
        # behind while its numeric PID is recycled before the first snapshot.
        windows_root_handle = platform_compat.duplicate_asyncio_process_handle(proc)
        if windows_root_handle is None:
            await _terminate_process(proc)
            await _unlink_off_loop(cleanup_path)
            return ProcessResult(
                ok=False,
                returncode=proc.returncode,
                error="could not retain the Windows process tree",
            )
        initial_snapshot = asyncio.get_running_loop().create_future()
        primary_terminal_snapshot = asyncio.get_running_loop().create_future()
        descendant_task = asyncio.create_task(
            _track_windows_descendants(
                proc,
                windows_descendants,
                windows_root_handle,
                initial_snapshot,
                primary_terminal_snapshot,
            )
        )

    async def _capture(stream: asyncio.StreamReader | None) -> None:
        nonlocal output
        if stream is None:
            return
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            output = _append_capped(output, chunk.decode("utf-8", "replace"))

    stdout_task = asyncio.create_task(_capture(proc.stdout))
    stderr_task = asyncio.create_task(_capture(proc.stderr))
    operation_tasks = (stdout_task, stderr_task)
    wait_task = asyncio.create_task(proc.wait())
    cleanup_tasks = (*operation_tasks, wait_task)

    async def _wait_for_completion() -> None:
        completion_tasks: list[Awaitable[Any]] = list(cleanup_tasks)
        if descendant_task is not None:
            # A Windows launcher may exit after re-spawning its real work as a
            # child. Keep the retained-tree tracker in the success condition so a
            # zero exit cannot release live descendant handles while the child is
            # still running.
            # Shield the tracker from the operation timeout: the timeout path
            # still needs its latest retained handles while terminating the tree.
            completion_tasks.append(asyncio.shield(descendant_task))
        if initial_snapshot is not None:
            # A fast launcher and its readers cannot finish the operation until
            # the exact-object primary snapshot has settled.
            await initial_snapshot
        if primary_terminal_snapshot is not None:
            await asyncio.gather(primary_terminal_snapshot, *completion_tasks)
        else:
            await asyncio.gather(*completion_tasks)

    try:
        await asyncio.wait_for(
            _wait_for_completion(),
            timeout=timeout_secs,
        )
    except asyncio.TimeoutError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            timed_out=True,
            error="process timed out",
        )
    except asyncio.CancelledError:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        raise
    except Exception as exc:
        await _terminate_process(proc, windows_descendants)
        for task in cleanup_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*cleanup_tasks, return_exceptions=True)
        return ProcessResult(
            ok=False,
            output=output,
            returncode=proc.returncode,
            error=str(exc),
        )
    finally:
        if descendant_task is not None:
            descendant_task.cancel()
            await asyncio.gather(descendant_task, return_exceptions=True)
        for handle in windows_descendants.values():
            platform_compat.close_process_handle(handle)
        if windows_root_handle is not None:
            platform_compat.close_process_handle(windows_root_handle)
        await _unlink_off_loop(cleanup_path)

    if proc.returncode == 0:
        return ProcessResult(ok=True, output=output, returncode=0)
    # The launcher exits 1 with its refusal on stderr, exactly like a candidate
    # that exited 1 on its own -- and the candidate can print the launcher's
    # line: it is the unverified binary this spawn exists to verify. So the
    # candidate's line is never the verdict. It only decides whether to look
    # further, and the verdict comes from re-running the real launcher around a
    # trusted no-op under THIS spawn's own mode and hidden/visible dirs, whose
    # stderr no child wrote. Only where a launcher ran at all: Windows skips the
    # wrap, so a ``sandbox:`` line there could only be the candidate's own text.
    # Off the loop: the trusted run blocks on a subprocess, and a spawn-time
    # refusal must not stall the gateway.
    sandbox_failure = None
    if not platform_compat.IS_WINDOWS and launcher_refusal(output) is not None:
        sandbox_failure = await asyncio.to_thread(
            functools.partial(
                corroborate_launcher_refusal,
                output,
                mode=sandbox_mode,
                extra_hidden_dirs=extra_hidden_dirs,
                extra_visible_dirs=extra_visible_dirs,
            )
        )
    return ProcessResult(
        ok=False,
        output=output,
        returncode=proc.returncode,
        error=f"process exited with code {proc.returncode}",
        sandbox_failure=sandbox_failure,
    )
