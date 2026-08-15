"""Coverage for the platform-specific edges of :mod:`kiro_crew.dashboard.port_reclaim`.

``test_port_reclaim.py`` covers the outcome matrix with every collaborator
injected. Untested there: the POSIX ``lsof`` lookup and its three degrade paths,
the Windows ``taskkill`` signal branch, the SIGKILL-denied escalation, the
holder description that separates a wedged fork from a live gateway, and the two
default collaborators (the real identity check and the real terminator) that
production actually uses.

No test here shells out or signals a real process: ``subprocess.check_output``,
``os.kill``, ``kill_process_tree`` and the liveness check are all patched.
"""

from __future__ import annotations

import subprocess

import pytest

import kiro_crew.cli_server as cli_server
from kiro_crew.dashboard import port_reclaim as pr


async def _unhealthy(_port: int, _timeout: float) -> bool:
    return False


# ---------------------------------------------------------------------------
# _listeners_on_port — POSIX lsof path
# ---------------------------------------------------------------------------


def test_listeners_posix_parses_and_dedupes_lsof_output(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _check_output(cmd, **_kw):
        calls.append(cmd)
        return "1234\n1234\n5678\n\nnot-a-pid\n"

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.subprocess, "check_output", _check_output)

    assert pr._listeners_on_port(5476) == [1234, 5678]
    # The LISTEN filter matters: a client socket on the port is not a holder.
    assert calls == [["lsof", "-ti", "TCP:5476", "-sTCP:LISTEN"]]


def test_listeners_posix_lsof_missing_returns_none(monkeypatch) -> None:
    """No lsof -> None, so the caller degrades to wait/retry, not "no holder"."""

    def _boom(_cmd, **_kw):
        raise FileNotFoundError("lsof")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.subprocess, "check_output", _boom)
    assert pr._listeners_on_port(5476) is None


def test_listeners_posix_wedged_lsof_returns_none(monkeypatch) -> None:
    """A wedged lsof is as useless as a missing one -- also wait/retry."""

    def _timeout(_cmd, **_kw):
        raise subprocess.TimeoutExpired(cmd="lsof", timeout=5)

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.subprocess, "check_output", _timeout)
    assert pr._listeners_on_port(5476) is None


def test_listeners_posix_no_match_returns_empty_list(monkeypatch) -> None:
    """lsof exits non-zero when nothing matches -- that is genuinely no holder."""

    def _nonzero(_cmd, **_kw):
        raise subprocess.CalledProcessError(returncode=1, cmd="lsof")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.subprocess, "check_output", _nonzero)
    assert pr._listeners_on_port(5476) == []


# ---------------------------------------------------------------------------
# _terminate_pids — POSIX and Windows signal delivery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_posix_already_exited_process_counts_as_success(monkeypatch) -> None:
    """ProcessLookupError means the holder died first -- the goal is met."""

    def _kill(_pid: int, _sig: int) -> None:
        raise ProcessLookupError("no such process")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.os, "kill", _kill)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)

    assert await pr._terminate_pids([4242], term_wait=0.05, kill_wait=0.05, poll=0.01)


@pytest.mark.asyncio
async def test_posix_denied_sigkill_reports_failure(monkeypatch) -> None:
    """SIGTERM lands, the holder survives, SIGKILL is denied -> reclaim failed.

    This is the "run it manually with sudo" path; silently returning True would
    let the gateway retry the bind forever against a port nothing will free.
    """
    signals: list[int] = []

    def _kill(_pid: int, sig: int) -> None:
        signals.append(sig)
        if sig == pr.platform_compat.SIGKILL:
            raise PermissionError("operation not permitted")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(pr.os, "kill", _kill)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: False)

    ok = await pr._terminate_pids([4242], term_wait=0.02, kill_wait=0.02, poll=0.01)
    assert ok is False
    assert signals == [pr.platform_compat.SIGTERM, pr.platform_compat.SIGKILL]


@pytest.mark.asyncio
async def test_windows_uses_the_process_tree_killer(monkeypatch) -> None:
    """On Windows the whole tree is reaped, or kiro-cli children are orphaned."""
    killed: list[tuple[int, int]] = []

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        pr.platform_compat, "kill_process_tree",
        lambda pid, sig: killed.append((pid, sig)),
    )
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)

    assert await pr._terminate_pids([99], term_wait=0.05, kill_wait=0.05, poll=0.01)
    assert killed == [(99, pr.platform_compat.SIGTERM)]


@pytest.mark.asyncio
async def test_windows_already_gone_counts_as_success(monkeypatch) -> None:
    def _tree(_pid: int, _sig: int) -> None:
        raise ProcessLookupError("gone")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pr.platform_compat, "kill_process_tree", _tree)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)

    assert await pr._terminate_pids([99], term_wait=0.05, kill_wait=0.05, poll=0.01)


@pytest.mark.asyncio
async def test_windows_permission_error_reports_failure(monkeypatch) -> None:
    def _tree(_pid: int, _sig: int) -> None:
        raise PermissionError("access denied")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pr.platform_compat, "kill_process_tree", _tree)

    ok = await pr._terminate_pids([99], term_wait=0.02, kill_wait=0.02, poll=0.01)
    assert ok is False


@pytest.mark.asyncio
async def test_windows_taskkill_oserror_rechecks_liveness(monkeypatch) -> None:
    """A generic taskkill failure is resolved by asking whether the pid is gone.

    Still alive -> failure; already gone -> success. Guessing from the OSError
    alone would mislabel both.
    """

    def _tree(_pid: int, _sig: int) -> None:
        raise OSError("taskkill blew up")

    monkeypatch.setattr(pr.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(pr.platform_compat, "kill_process_tree", _tree)
    monkeypatch.setattr(cli_server, "_pid_exited", lambda _pid: True)

    monkeypatch.setattr(pr.platform_compat, "pid_exists", lambda _pid: True)
    assert await pr._terminate_pids([99], term_wait=0.02, kill_wait=0.02, poll=0.01) is False

    monkeypatch.setattr(pr.platform_compat, "pid_exists", lambda _pid: False)
    assert await pr._terminate_pids([99], term_wait=0.02, kill_wait=0.02, poll=0.01) is True


# ---------------------------------------------------------------------------
# _describe_holders — the log line an operator has to act on
# ---------------------------------------------------------------------------


def test_describe_holders_names_a_wedged_fork(monkeypatch) -> None:
    """One thread == a fork orphaned before exec, not a running gateway."""
    counts = {1: 1, 2: 37, 3: None}
    monkeypatch.setattr(
        pr.platform_compat, "process_thread_count", lambda pid: counts[pid]
    )

    out = pr._describe_holders([1, 2, 3])

    assert "1 (1 thread" in out
    assert "wedged fork" in out
    assert "2 (37 threads)" in out
    # Unknown thread count degrades to the bare pid rather than lying.
    assert out.endswith("3")


# ---------------------------------------------------------------------------
# reclaim_stale_gateway_port — the default collaborators
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_identity_check_is_the_shared_cli_server_gate(monkeypatch) -> None:
    """With no injected checker, reclaim uses the same gate as `kirocrew stop`."""
    asked: list[int] = []

    def _is_kirocrew(pid: int) -> bool:
        asked.append(pid)
        return False

    monkeypatch.setattr(cli_server, "_is_kirocrew_process", _is_kirocrew)
    monkeypatch.setattr(pr.platform_compat, "process_thread_count", lambda _pid: 4)

    outcome = await pr.reclaim_stale_gateway_port(
        5476, list_listeners=lambda _p: [7777], probe_healthy=_unhealthy
    )

    assert outcome == pr.FOREIGN_HOLDER
    assert asked == [7777]


@pytest.mark.asyncio
async def test_default_terminator_is_used_when_none_injected(monkeypatch) -> None:
    """The production path passes no terminator; _terminate_pids must be it."""
    called: list[list[int]] = []

    async def _fake_terminate(pids: list[int]) -> bool:
        called.append(list(pids))
        return True

    monkeypatch.setattr(pr, "_terminate_pids", _fake_terminate)
    monkeypatch.setattr(pr.platform_compat, "process_thread_count", lambda _pid: 1)

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [8888],
        is_kirocrew=lambda _pid: True,
        probe_healthy=_unhealthy,
    )

    assert outcome == pr.RECLAIMED
    assert called == [[8888]]


@pytest.mark.asyncio
async def test_wedged_ps_on_the_pre_signal_recheck_is_unavailable() -> None:
    """The re-check exists to shrink a PID-reuse window; if it cannot complete,
    reclaim declines rather than signalling on stale evidence."""
    import time as _time

    calls = {"n": 0}

    def _checker(_pid: int) -> bool:
        calls["n"] += 1
        if calls["n"] > 1:
            _time.sleep(0.5)  # ps wedges on the re-check only
        return True

    terminate_called = False

    async def _terminate(_pids: list[int]) -> bool:
        nonlocal terminate_called
        terminate_called = True
        return True

    outcome = await pr.reclaim_stale_gateway_port(
        5476,
        list_listeners=lambda _p: [1234],
        is_kirocrew=_checker,
        probe_healthy=_unhealthy,
        terminate=_terminate,
        identity_timeout=0.15,
    )

    assert outcome == pr.UNAVAILABLE
    assert terminate_called is False
