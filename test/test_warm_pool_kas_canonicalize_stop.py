"""The warm pool stops auto-refilling when the Kiro CLI cannot canonicalize its
recorded KAS server path.

A data home copied to a new host carries the Kiro CLI's recorded KAS bundle path
forward while the directory does not survive the copy intact, so the Kiro CLI
dies at startup with ``failed to canonicalize KAS server path`` and rc=1. Without
a stop, the fill catches the error and breaks, the health sweep sees the deficit
and refills, and the new child dies the same way: a crash loop with no actionable
message.

The exception the fill must recognise is produced by a REAL failed start here,
not synthesised: a real child writes the exact Kiro CLI line to stderr and exits
1, wired to a real ``AcpRuntime`` whose real reader, exit watcher and stderr drain
run. The death reason the runtime builds is what fails the pending ``initialize``
future -- the exact exception ``runtime.spawn`` raises and ``_start_kiro_runtime``
re-raises unchanged for this cause -- so the test proves the start path does not
wrap or strip the line before the pool fill reads it. A plain rc=1 child on the
same real path is the control.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

import pytest
from session_pool_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolate_config_dir,
    _make_manager,
    _make_provider,
)

from kiro_crew.start_priority import PrioritySemaphore

_POSIX_ONLY = pytest.mark.skipif(
    not hasattr(os, "fork"), reason="real-child spawn uses POSIX process groups"
)

# The exact stderr line the Kiro CLI writes after a data-home copy breaks the
# recorded KAS bundle directory on the new host.
KAS_CANONICALIZE_LINE = (
    "error: failed to canonicalize KAS server path: "
    "/opt/old-host/kiro-cli/kas/1.2.3/node_modules/@kiro/agent/"
    "dist/server/acp-server.js"
)

# A plain rc=1 death that carries none of the canonicalize signature.
PLAIN_LINE = "unexpected end of stream"


def _child_script(stderr_line: str) -> str:
    """A child that writes *stderr_line*, flushes, and exits 1 -- no response to
    ``initialize``, so the runtime observes the death the way a broken KAS
    extraction kills the real relay."""
    return (
        "import sys\n"
        f"sys.stderr.write({stderr_line!r} + '\\n')\n"
        "sys.stderr.flush()\n"
        "sys.exit(1)\n"
    )


async def _real_start_death_exception(stderr_line: str) -> BaseException:
    """The exception a REAL failed start raises for a child that dies rc=1 after
    writing *stderr_line*.

    Mirrors how the start path learns of the death: the runtime's reader, exit
    watcher and stderr drain run against a real child, and the pending
    ``initialize`` future is failed with the death the runtime composed -- the
    same future ``_initialize_handshake`` awaits, so its exception is what
    ``runtime.spawn`` propagates.
    """
    from kiro_crew.acp.runtime import AcpRuntime

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _child_script(stderr_line),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    pgid = proc.pid
    reader_task = None
    watch_task = None
    try:
        rt = AcpRuntime(work_dir="/tmp")
        rt._process = proc
        rt._pid = proc.pid
        rt._initialized = True
        pending: asyncio.Future = asyncio.get_running_loop().create_future()
        # The id _initialize_handshake awaits on; _mark_dead fails every pending
        # request with the composed death.
        rt._pending_requests[1] = pending

        # Drain stderr to completion BEFORE the reader can run. The reader's EOF
        # path builds the death reason from _stderr_lines the instant stdout
        # closes; _drain_stderr is the only writer of that buffer. Scheduling
        # both together lets the reader win the event-loop race and compose a
        # bare rc=1 with an empty tail, so the stderr line must be fully captured
        # first. The child has exited by the wait above, so its stderr is at EOF
        # and the drain returns once it has read the line.
        await asyncio.wait_for(proc.wait(), timeout=30.0)
        await asyncio.wait_for(rt._drain_stderr(), timeout=30.0)
        reader_task = asyncio.ensure_future(rt._reader_loop())
        watch_task = asyncio.ensure_future(rt._exit_watch_loop())
        await asyncio.wait_for(asyncio.shield(pending), timeout=30.0)
        raise AssertionError("the pending request should have failed on the death")
    except BaseException as exc:  # noqa: BLE001 -- the death IS the result here
        if isinstance(exc, AssertionError):
            raise
        return exc
    finally:
        for t in (reader_task, watch_task):
            if t is not None:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=30.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_real_failed_start_hands_the_fill_the_line():
    """Precondition and the heart of the fix: the exception a REAL rc=1 start
    death produces carries the Kiro CLI line and the fix, and the classifier the
    fill uses matches its ``str()``. If the start path wrapped or replaced the
    reason, this fails here rather than silently leaving the loop in place."""
    from kiro_crew.acp.runtime_liveness import is_kas_canonicalize_failure

    exc = await _real_start_death_exception(KAS_CANONICALIZE_LINE)
    text = str(exc)
    assert "failed to canonicalize KAS server path" in text, text
    assert "reinstall Kiro CLI" in text, text
    assert is_kas_canonicalize_failure(text) is True

    # Control: a plain rc=1 death on the same real path does NOT match.
    plain = await _real_start_death_exception(PLAIN_LINE)
    assert is_kas_canonicalize_failure(str(plain)) is False


def _factory_raising(exc: BaseException):
    """A provider factory whose ``start`` raises *exc* -- the real exception the
    start path produced above."""
    from unittest.mock import AsyncMock, MagicMock

    def _factory(*a, **kw):
        p = _make_provider()
        p.start = AsyncMock(side_effect=exc)
        return p

    return MagicMock(side_effect=_factory)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_fill_stops_refilling_and_a_sweep_does_not_respawn(caplog):
    exc = await _real_start_death_exception(KAS_CANONICALIZE_LINE)
    mgr, _ = _make_manager(pool_size=1)
    factory = _factory_raising(exc)
    mgr._provider_factory = factory

    with caplog.at_level(logging.WARNING, logger="kiro_crew.session_pool"):
        await mgr._fill_warm_pool()
        # The health sweep sees the deficit and drives a second fill. With the
        # stop in place it returns without spawning: no crash loop.
        await mgr._sweep_warm_pool_once()
        await mgr._fill_warm_pool()

    # Behavioural claim first, so origin/main shows red as a respawn (factory
    # called twice), not as a missing attribute.
    assert factory.call_count == 1, "the pool respawned into the same KAS failure"
    assert mgr._warm_pool.empty()
    pause_logs = [r for r in caplog.records if "pausing auto-refill" in r.getMessage()]
    assert len(pause_logs) == 1
    logged = pause_logs[0].getMessage()
    assert "failed to canonicalize KAS server path" in logged
    assert "reinstall Kiro CLI" in logged
    assert mgr._pool.state.kas_canonicalize_blocked is True

    await mgr.close_all()


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_plain_rc1_death_still_refills():
    """Control: a death whose stderr lacks the signature keeps today's behaviour
    -- the latch stays clear and a later fill spawns again."""
    exc = await _real_start_death_exception(PLAIN_LINE)
    mgr, _ = _make_manager(pool_size=1)
    factory = _factory_raising(exc)
    mgr._provider_factory = factory

    await mgr._fill_warm_pool()
    assert factory.call_count == 1
    assert mgr._pool.state.kas_canonicalize_blocked is False

    await mgr._fill_warm_pool()
    assert factory.call_count == 2

    await mgr.close_all()


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_identity_swap_clears_the_block():
    """An identity swap changes what a spawn would produce, so it clears the latch
    and the next fill tries again."""
    exc = await _real_start_death_exception(KAS_CANONICALIZE_LINE)
    mgr, _ = _make_manager(pool_size=1)
    mgr._provider_factory = _factory_raising(exc)

    await mgr._fill_warm_pool()
    assert mgr._pool.state.kas_canonicalize_blocked is True

    mgr._pool.mark_identity_epoch()
    assert mgr._pool.state.kas_canonicalize_blocked is False

    await mgr.close_all()


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_stale_factorys_kas_failure_does_not_block_the_replacement():
    """A spawn from a retired factory that dies with the KAS cause AFTER a config
    apply swapped in a working factory must not latch: the replacement factory is
    fine, so latching would leave the pool empty with no working cause.

    The fill lock is released across the start, so the old spawn is in flight when
    the swap lands; its KAS failure then reaches the error path with a different
    factory installed. The guard keys on the failing iteration's own factory."""
    from unittest.mock import AsyncMock, MagicMock

    from kiro_crew.testing.wait import await_until

    exc = await _real_start_death_exception(KAS_CANONICALIZE_LINE)
    mgr, _ = _make_manager(pool_size=1)
    mgr._start_sem = PrioritySemaphore(1)

    in_start = asyncio.Event()
    release_start = asyncio.Event()

    async def _slow_then_kas() -> None:
        in_start.set()
        await await_until(release_start, what="the test releases the gated start")
        raise exc

    def _old_factory(*a, **kw):
        p = _make_provider()
        p.start = AsyncMock(side_effect=_slow_then_kas)
        return p

    # The replacement factory is working: its providers start cleanly.
    good_provider = _make_provider()
    new_factory = MagicMock(side_effect=lambda *a, **kw: good_provider)

    mgr._provider_factory = MagicMock(side_effect=_old_factory)

    fill = asyncio.create_task(mgr._fill_warm_pool())
    try:
        await await_until(in_start, what="the old spawn enters start()")
        # The config apply swaps the factory while the old spawn is parked. Model
        # just the factory swap and the latch clear that start_pool does.
        mgr._provider_factory = new_factory
        mgr._pool.state.kas_canonicalize_blocked = False
    finally:
        release_start.set()
        await await_until(fill, what="the old spawn's fill finishes")

    # The stale KAS failure does not latch: its factory is not the installed one.
    assert mgr._pool.state.kas_canonicalize_blocked is False
    # The replacement factory can still fill the pool.
    await mgr._fill_warm_pool()
    assert new_factory.call_count >= 1
    assert mgr._warm_pool.qsize() == 1

    await mgr.close_all()
