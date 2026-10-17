"""Tests for the periodic persistence watchdog.

The boot preflight catches a data home that cannot persist AT boot; this
watchdog catches one that passed boot and broke later (a seccomp filter whose
sandbox was torn down mid-run). It must: re-run the probe periodically,
re-confirm a single failure before acting, exit non-zero on a confirmed
restart-curable failure so a supervisor restarts, stay up on a failure a
restart cannot cure, and exit 0 on an operator stop -- including one that lands
while the confirming probe is running.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiro_crew.dashboard import persistence_watchdog as pw
from kiro_crew.testing.wait import async_wait_until, await_until


class _Event:
    """A minimal shutdown signal matching the watchdog's Protocol."""

    def __init__(self) -> None:
        self._e = asyncio.Event()

    def is_set(self) -> bool:
        return self._e.is_set()

    def set(self) -> None:
        self._e.set()

    async def wait(self) -> bool:
        await self._e.wait()
        return True


def _ok() -> pw.ProbeResult | None:
    return None


def _enosys(reason: str = "ENOSYS from a basic file syscall") -> pw.ProbeResult:
    return pw.ProbeResult(reason, restartable=True)


def _enospc(reason: str = "cannot write files: [Errno 28] No space left") -> pw.ProbeResult:
    return pw.ProbeResult(reason, restartable=False)


def _probe_sequence(values: list[pw.ProbeResult | None]):
    """An async probe that yields ``values`` in order, then repeats the last."""
    calls = {"n": 0}

    async def _probe() -> pw.ProbeResult | None:
        i = min(calls["n"], len(values) - 1)
        calls["n"] += 1
        return values[i]

    _probe.calls = calls  # type: ignore[attr-defined]
    return _probe


@pytest.mark.asyncio
async def test_exits_nonzero_on_confirmed_restartable_failure() -> None:
    shutdown = _Event()
    # Fails (ENOSYS), re-confirm also fails -> fire.
    probe = _probe_sequence([_enosys()])
    fired = await pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=0.0)
    assert fired is True
    assert shutdown.is_set()
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    done.set_result(True)
    assert pw.shutdown_exit_code(done) == pw.PERSISTENCE_LOST_EXIT_CODE
    assert pw.PERSISTENCE_LOST_EXIT_CODE != 0
    # Must be a RESTARTED exit, never the terminal live-holder code (78), which
    # the Linux unit exempts from Restart= via RestartPreventExitStatus. 72
    # (EX_OSFILE) is restarted like 69 and 75.
    from kiro_crew.gateway_lock import LIVE_HOLDER_EXIT_CODE

    assert pw.PERSISTENCE_LOST_EXIT_CODE != LIVE_HOLDER_EXIT_CODE


@pytest.mark.asyncio
async def test_non_restartable_failure_keeps_serving() -> None:
    """A full disk (ENOSPC) fails the probe but a restart cannot cure it, so the
    watchdog logs loudly and keeps running rather than crash-looping."""
    shutdown = _Event()
    # Confirmed non-restartable twice, then stop so the test ends.
    probe = _probe_sequence([_enospc(), _enospc(), _enospc()])

    async def _stop_after_two_cycles() -> None:
        await async_wait_until(
            lambda: probe.calls["n"] >= 4,  # type: ignore[attr-defined]
            describe=lambda: "two full failed+confirmed cycles",
        )
        shutdown.set()

    stopper = asyncio.ensure_future(_stop_after_two_cycles())
    fired = await await_until(
        asyncio.ensure_future(
            pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=0.0)
        ),
        what="watchdog to exit after operator stop",
    )
    await stopper
    assert fired is False  # never self-terminated on a non-restartable fault
    # It ran several failed+confirmed cycles without exiting on its own; the
    # only thing that stopped it was the operator stop the stopper set.
    assert probe.calls["n"] >= 4  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_persistent_non_restartable_fault_is_announced_once_per_reason(
    monkeypatch,
) -> None:
    """Consecutive ticks failing with the same non-restartable reason announce it
    once; a new reason, or the same reason after a healthy probe, announces again."""
    announced: list[str] = []
    monkeypatch.setattr(pw, "_announce_unrecoverable", announced.append)
    shutdown = _Event()
    full = _enospc("disk full")
    ro = _enospc("read-only")
    # Each tick probes twice while failing (first + confirm), once when healthy.
    values = [full, full, full, full, full, full, ro, ro, ro, ro, _ok(), full, full, _ok()]
    calls = {"n": 0}

    async def _probe() -> pw.ProbeResult | None:
        i = calls["n"]
        calls["n"] += 1
        if i >= len(values) - 1:
            shutdown.set()
        return values[min(i, len(values) - 1)]

    fired = await await_until(
        asyncio.ensure_future(
            pw.run_persistence_watchdog(shutdown, _probe, interval=0.0, confirm_delay=0.0)
        ),
        what="watchdog to exit after the scripted probes",
    )
    assert fired is False
    assert announced == ["disk full", "read-only", "disk full"]


@pytest.mark.asyncio
async def test_transient_failure_recovers_without_shutdown() -> None:
    shutdown = _Event()
    # First probe fails, the re-confirm probe passes -> keep running.
    probe = _probe_sequence([_enosys("transient glitch"), _ok(), _ok()])

    async def _stop_soon() -> None:
        await async_wait_until(
            lambda: probe.calls["n"] >= 2,  # type: ignore[attr-defined]
            describe=lambda: "first failed+recovered cycle",
        )
        shutdown.set()

    stopper = asyncio.ensure_future(_stop_soon())
    fired = await await_until(
        asyncio.ensure_future(
            pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=0.0)
        ),
        what="watchdog to exit after operator stop",
    )
    await stopper
    assert fired is False  # the re-confirm cleared the one-off failure


@pytest.mark.asyncio
async def test_operator_stop_during_the_confirm_window_exits_without_firing() -> None:
    """A single failure opens a confirmation window; an operator stop landing in
    that window ends the watchdog cleanly (exit False, not a self-termination)
    and the second confirming probe never runs."""
    shutdown = _Event()
    probe = _probe_sequence([_enosys(), _enosys()])

    task: asyncio.Future[bool] = asyncio.ensure_future(
        pw.run_persistence_watchdog(shutdown, probe, interval=0.0, confirm_delay=30.0)
    )
    # Wait until the first probe has run and the watchdog is in the confirm wait.
    await async_wait_until(
        lambda: probe.calls["n"] >= 1,  # type: ignore[attr-defined]
        describe=lambda: "the first probe to run",
    )
    shutdown.set()
    fired = await await_until(task, what="watchdog to exit on the operator stop")
    assert fired is False
    assert probe.calls["n"] == 1  # the confirm never completed into a 2nd probe


@pytest.mark.asyncio
async def test_operator_stop_during_the_confirming_probe_exits_without_firing() -> None:
    """An operator stop that arrives while the confirming probe runs owns the
    shutdown: the watchdog returns False (exit 0) even though that probe failed."""
    shutdown = _Event()
    calls = {"n": 0}

    async def _probe() -> pw.ProbeResult | None:
        calls["n"] += 1
        if calls["n"] == 2:
            shutdown.set()  # SIGTERM handled while the confirming probe is in flight
        return _enosys()

    fired = await pw.run_persistence_watchdog(shutdown, _probe, interval=0.0, confirm_delay=0.0)
    assert fired is False
    assert calls["n"] == 2
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    done.set_result(fired)
    assert pw.shutdown_exit_code(done) == 0


@pytest.mark.asyncio
async def test_operator_stop_exits_zero() -> None:
    shutdown = _Event()
    shutdown.set()  # already stopping before the watchdog arms
    probe = _probe_sequence([_ok()])
    fired = await pw.run_persistence_watchdog(shutdown, probe, interval=10.0, confirm_delay=10.0)
    assert fired is False
    done: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
    done.set_result(False)
    assert pw.shutdown_exit_code(done) == 0


@pytest.mark.asyncio
async def test_exit_code_mapper_is_defensive() -> None:
    assert pw.shutdown_exit_code(None) == 0
    loop = asyncio.get_event_loop()
    running: asyncio.Future[bool] = loop.create_future()
    assert pw.shutdown_exit_code(running) == 0  # not done yet
    cancelled: asyncio.Future[bool] = loop.create_future()
    cancelled.cancel()
    assert pw.shutdown_exit_code(cancelled) == 0
    crashed: asyncio.Future[bool] = loop.create_future()
    crashed.set_exception(RuntimeError("boom"))
    assert pw.shutdown_exit_code(crashed) == 0


@pytest.mark.asyncio
async def test_probe_data_home_marks_enosys_restartable(monkeypatch) -> None:
    from kiro_crew import platform_compat

    seen = {}

    def _fake_probe(directory):
        seen["dir"] = directory
        return "cannot write files in /x: [Errno 38] Function not implemented"

    monkeypatch.setattr(platform_compat, "probe_file_persistence", _fake_probe)
    probe = pw.probe_data_home(Path("/x"))
    result = await probe()
    assert result is not None
    assert result.restartable is True  # ENOSYS -> a restart may cure it
    assert "Errno 38" in result.reason
    assert seen["dir"] == Path("/x")


@pytest.mark.asyncio
async def test_probe_data_home_marks_enospc_not_restartable(monkeypatch) -> None:
    from kiro_crew import platform_compat

    monkeypatch.setattr(
        platform_compat,
        "probe_file_persistence",
        lambda _d: "cannot write files in /x: [Errno 28] No space left on device",
    )
    probe = pw.probe_data_home(Path("/x"))
    result = await probe()
    assert result is not None
    assert result.restartable is False  # a full disk is not restart-curable


@pytest.mark.asyncio
async def test_probe_data_home_healthy_returns_none(monkeypatch) -> None:
    from kiro_crew import platform_compat

    monkeypatch.setattr(platform_compat, "probe_file_persistence", lambda _d: None)
    probe = pw.probe_data_home(Path("/x"))
    assert await probe() is None


@pytest.mark.asyncio
async def test_probe_data_home_reports_thread_exhaustion(monkeypatch) -> None:
    async def _boom(*_a, **_k):
        raise RuntimeError("no threads")

    monkeypatch.setattr(pw.asyncio, "to_thread", _boom)
    probe = pw.probe_data_home(Path("/x"))
    result = await probe()
    assert result is not None
    # A worker-thread shortage means the probe could not RUN; it is reported but
    # must NOT take the self-exit path, since a restart is not known to cure it.
    assert result.restartable is False
    assert "persistence probe" in result.reason


@pytest.mark.asyncio
async def test_real_probe_enosys_message_is_classified_restartable(
    tmp_path: Path, monkeypatch
) -> None:
    """End-to-end over the REAL ``probe_file_persistence``: an ``ENOSYS`` raised
    by the first file syscall (what a torn-down sandbox's seccomp filter does)
    must flow through ``probe_data_home`` as ``restartable=True``.

    This pins the one link the stubbed tests cannot: that the actual message the
    real probe produces for ``ENOSYS`` is the text ``_is_enosys`` matches. It
    patches the syscall, not the probe, so the probe's own error-formatting runs.
    A live sandbox teardown cannot be staged in CI, so injecting the genuine
    ``OSError(ENOSYS)`` at the syscall boundary is the closest faithful proxy."""
    import errno
    import os

    from kiro_crew import platform_lock_compat

    def _enosys_mkstemp(*_a, **_k):
        raise OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))

    monkeypatch.setattr(platform_lock_compat.tempfile, "mkstemp", _enosys_mkstemp)

    probe = pw.probe_data_home(tmp_path)
    result = await probe()
    assert result is not None
    # The real probe formatted the real OSError; the classifier read ENOSYS.
    assert result.restartable is True
    assert ("[Errno 38]" in result.reason) or ("Function not implemented" in result.reason)
