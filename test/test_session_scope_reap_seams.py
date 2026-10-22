"""Tests for the agent-scope reaper's entry point and host seams (session_scope_reap.py).

``reap_abandoned_agent_scopes`` is a no-op off Linux, aborts before any scan on an
incomplete tracked-pid snapshot, and otherwise hands the tracked and active pids and the
age floor to ``reap_scopes``. The seams the sweep stands on are pinned here: the
per-instance slice lookup and the slice listing error, ``systemctl`` resolved only from a
trusted absolute path with an unusable active-enter stamp read as unknown, pidfd signalling
that pins a process before verifying it and never falls back to a numeric kill, the gateway
boot stamp and the member-age fallback derived from ``/proc`` stat ticks, the one
categorized INFO summary of old skips, and the periodic cleanup tick that runs the real
reaper against a synthetic process table across successive cycles.

The fake slice tree and ``/proc`` builders, the recorder, the ``_reap`` driver,
``_ModuleProxy`` and the autouse fixture that fails a test on any swallowed scope error come
from ``session_scope_reap_helpers.py``. One test reads the real ``/proc/self/stat`` to pin
the boot stamp against the kernel's own line.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest
from session_scope_reap_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _NOW,
    _enter_us_for_age,
    _make_proc,
    _make_scope,
    _ModuleProxy,
    _pin_boot_clock,
    _pin_tick_rate,
    _reap,
    _Recorder,
    _scope_errors_fail_the_test_fixture,
)

from kiro_crew import session_scope_reap as r
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Agent scope reaping is Linux-only")


def test_non_linux_is_no_op(monkeypatch):
    monkeypatch.setattr(r.sys, "platform", "darwin")
    summary = r.reap_abandoned_agent_scopes(set())
    assert summary.supported is False
    assert "not Linux" in summary.reason


def test_instance_dir_none_when_token_unavailable(monkeypatch):
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: Path("/nonexistent-parent"))
    # Degraded token: child name equals the shared parent -> not attributable.
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: sandbox._CGROUP_AGENTS_SLICE)
    slice_dir, why = r._instance_scope_dir()
    assert slice_dir is None
    assert "shared slice" in why


def test_systemctl_absence_fails_closed(monkeypatch):
    monkeypatch.setattr(r.platform_compat, "trusted_system_bin", lambda _name: None)

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError("subprocess must not run without a trusted systemctl")

    monkeypatch.setattr(r.subprocess, "run", unexpected_run)
    assert r._scope_active_enter_us("run-u1.scope") is None
    assert r._systemctl_stop("run-u1.scope") is False


def test_systemctl_uses_trusted_absolute_path(monkeypatch):
    calls = []
    monkeypatch.setattr(r.platform_compat, "trusted_system_bin", lambda _name: "/usr/bin/systemctl")

    class Result:
        returncode = 0
        stdout = "123\n"

    monkeypatch.setattr(r.subprocess, "run", lambda argv, **_kwargs: calls.append(argv) or Result())
    assert r._scope_active_enter_us("run-u1.scope") == 123
    assert r._systemctl_stop("run-u1.scope") is True
    assert all(argv[0] == "/usr/bin/systemctl" for argv in calls)


def test_pidfd_pin_precedes_ownership_and_signal(monkeypatch, tmp_path):
    events = []
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201])
    monkeypatch.setattr(
        r,
        "os",
        _ModuleProxy(
            r.os,
            pidfd_open=lambda _pid: events.append("pin") or 71,
            close=lambda _fd: events.append("close"),
        ),
    )
    monkeypatch.setattr(
        r,
        "_scope_owned_pids",
        lambda _members, _reads: events.append("verify") or ({201}, ""),
    )
    monkeypatch.setattr(
        r.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: events.append("signal"),
        raising=False,
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, tmp_path, {})

    assert sent is True and reason == ""
    assert events == ["pin", "verify", "signal", "close"]


def test_pidfd_skips_pid_removed_from_scope_before_pin(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201])
    signalled = []

    def pin_after_pid_reuse(_pid):
        (scope / "cgroup.procs").write_text("")
        return 71

    monkeypatch.setattr(
        r, "os", _ModuleProxy(r.os, pidfd_open=pin_after_pid_reuse, close=lambda _fd: None)
    )
    monkeypatch.setattr(
        r.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: signalled.append(201),
        raising=False,
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, proc, {})

    assert sent is False and reason == ""
    assert signalled == []


def test_pidfd_process_lookup_is_quiet(monkeypatch, tmp_path):
    def gone(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(r.os, "pidfd_open", gone, raising=False)
    monkeypatch.setattr(r.signal, "pidfd_send_signal", lambda *_args: None, raising=False)
    assert r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path, {}) == (False, "")


def test_pidfd_unavailable_never_falls_back_to_numeric_kill(monkeypatch, tmp_path):
    monkeypatch.delattr(r.os, "pidfd_open", raising=False)
    monkeypatch.delattr(r.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        r.platform_compat,
        "kill_pid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("numeric kill fallback used")),
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path, {})

    assert sent is False
    assert reason == "pidfd signalling unavailable"


def test_pidfd_open_oserror_never_falls_back(monkeypatch, tmp_path):
    def unsupported(_pid):
        raise OSError(38, "not implemented")

    monkeypatch.setattr(r.os, "pidfd_open", unsupported, raising=False)
    monkeypatch.setattr(r.signal, "pidfd_send_signal", lambda *_args: None, raising=False)
    monkeypatch.setattr(
        r.platform_compat,
        "kill_pid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("numeric kill fallback used")),
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path, {})

    assert sent is False
    assert reason == "pidfd_open failed (38)"


def test_pidfd_refusal_logs_once_per_scope(tmp_path, caplog):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    scope = _make_scope(slice_dir, "run-u1.scope", [201, 202])
    rec = _Recorder(empty_on_stop=False)

    with caplog.at_level("WARNING", logger=r.__name__):
        cleared = r._reclaim_scope(
            scope,
            "run-u1.scope",
            proc_root=proc,
            stop_unit=rec.stop_unit,
            signal_owned=lambda *_args: (False, "pidfd signalling unavailable"),
            sleep=rec.sleep,
        )

    assert cleared is False
    warnings = [m for m in caplog.messages if "signalling skipped" in m]
    assert warnings == [
        "agent_scope_reap signalling skipped unit=run-u1.scope "
        "reasons=pidfd signalling unavailable"
    ]


def test_old_skips_emit_one_categorized_info_summary(tmp_path, caplog):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200, marker=False)
    _make_proc(proc, 301, pgrp=300)
    _make_scope(slice_dir, "run-old.scope", [201])
    _make_scope(slice_dir, "run-young.scope", [301])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        _reap(
            slice_dir,
            proc,
            rec,
            enter={
                "run-old.scope": _enter_us_for_age(700),
                "run-young.scope": _enter_us_for_age(100),
            },
        )

    summaries = [m for m in caplog.messages if "skipped old scope(s)" in m]
    assert summaries == ["agent_scope_reap: skipped old scope(s): too-young=1 unowned=1"]


def test_incomplete_tracking_snapshot_aborts_before_scan(monkeypatch, tmp_path, caplog):
    from kiro_crew import sandbox, session_pid

    monkeypatch.setattr(r.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, ""))
    monkeypatch.setattr(r, "_instance_scope_dir", lambda: (tmp_path, ""))
    monkeypatch.setattr(session_pid, "_read_tracked_agent_pids", lambda: ({201}, False))
    monkeypatch.setattr(
        r,
        "reap_scopes",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("scan must not run")),
    )

    with caplog.at_level("WARNING", logger=r.__name__):
        summary = r.reap_abandoned_agent_scopes(set())

    assert summary.supported is True
    assert summary.reason == "tracked-pid snapshot incomplete"
    assert summary.scanned == summary.reclaimed == summary.skipped == 0
    assert "tracked-pid snapshot incomplete" in caplog.text


def test_complete_tracking_snapshot_proceeds(monkeypatch, tmp_path):
    from kiro_crew import sandbox, session_pid

    expected = r.ReapSummary(scanned=2, reclaimed=1, skipped=1)
    seen = {}
    monkeypatch.setattr(r.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, ""))
    monkeypatch.setattr(r, "_instance_scope_dir", lambda: (tmp_path, ""))
    monkeypatch.setattr(session_pid, "_read_tracked_agent_pids", lambda: ({201}, True))
    monkeypatch.setattr(r, "_cached_gateway_boot_us", lambda: 1)
    # flake-ok: pre-existing worker-wide rebind of the stdlib time.clock_gettime (r.time is the stdlib module) for one synchronous call, restored at teardown; moved verbatim, tracked for a de-flake pass
    monkeypatch.setattr(r.time, "clock_gettime", lambda _clock: _NOW)

    def fake_reap(*_args, **kwargs):
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(r, "reap_scopes", fake_reap)

    assert r.reap_abandoned_agent_scopes({301}) is expected
    assert seen["tracked_pids"] == {201}
    assert seen["active_pids"] == {301}
    assert seen["min_age_secs"] == r._REAP_MIN_AGE_SECS


def test_gateway_boot_monotonic_us_matches_real_proc_start():
    before_mono = r.time.clock_gettime(r.time.CLOCK_MONOTONIC)
    boot_us = r.gateway_boot_monotonic_us()
    after_mono = r.time.clock_gettime(r.time.CLOCK_MONOTONIC)
    after_boot = r.time.clock_gettime(r.time.CLOCK_BOOTTIME)
    # Parsed here independently of the reader under test, so a field-index slip
    # shared by the reader and the hand-built fixtures still fails against a
    # real kernel line: starttime is field 22, index 19 after the last ')'.
    raw = Path("/proc/self/stat").read_bytes()
    start_ticks = int(raw[raw.rfind(b")") + 1 :].split()[19])
    clk_tck = r.os.sysconf("SC_CLK_TCK")
    expected_us = int((after_mono - (after_boot - start_ticks / clk_tck)) * 1_000_000)

    assert isinstance(boot_us, int)
    assert int(before_mono * 1_000_000) >= boot_us
    # flake-ok: pre-existing 2 s tolerance between real CLOCK_MONOTONIC and CLOCK_BOOTTIME samples and the kernel's own stat line; moved verbatim, tracked for a de-flake pass
    assert abs(boot_us - expected_us) < 2_000_000


def _own_stat(proc_root: Path, raw: bytes) -> None:
    own = proc_root / str(r.os.getpid())
    own.mkdir(parents=True)
    (own / "stat").write_bytes(raw)


def test_gateway_boot_reads_a_non_utf8_own_comm_from_bytes(tmp_path, monkeypatch):
    _own_stat(tmp_path, b"7 (kirocrew\xe2\x9c) S 1 7 " + b"0 " * 16 + b"4242 0")
    _pin_boot_clock(monkeypatch, now=100.0)
    monkeypatch.setattr(r, "time", _ModuleProxy(r.time, clock_gettime=lambda _clock: 50.0))

    # Elapsed on the boot clock is 100 - 42.42; the start is that far before 50.
    assert r.gateway_boot_monotonic_us(tmp_path) == int((50.0 - (100.0 - 42.42)) * 1_000_000)


def test_gateway_boot_reads_monotonic_before_the_boot_clock(tmp_path, monkeypatch):
    # A stall between the two reads must lengthen the elapsed time, moving the
    # derived start EARLIER (toward not reaping), never later than the truth.
    _own_stat(tmp_path, b"7 (kirocrew) S 1 7 " + b"0 " * 16 + b"4242 0")
    order: list[str] = []
    _pin_tick_rate(monkeypatch, 100)
    monkeypatch.setattr(
        r, "time", _ModuleProxy(r.time, clock_gettime=lambda _clock: order.append("mono") or 50.0)
    )
    monkeypatch.setattr(
        r.platform_compat, "boottime_now", lambda: order.append("boot") or 100.0 + 0.2
    )

    stamp = r.gateway_boot_monotonic_us(tmp_path)

    assert order == ["mono", "boot"]
    truth_us = int((50.0 - (100.0 - 42.42)) * 1_000_000)
    assert stamp is not None and stamp <= truth_us


@pytest.mark.parametrize(
    "raw", [b"malformed", b"7 (kirocrew) S 1 7", b"7 (kirocrew) S 1 7 " + b"0 " * 16 + b"x"]
)
def test_gateway_boot_returns_none_for_a_malformed_own_stat(tmp_path, raw):
    _own_stat(tmp_path, raw)

    assert r.gateway_boot_monotonic_us(tmp_path) is None


def test_gateway_boot_monotonic_us_returns_none_on_proc_read_error(tmp_path):
    assert r.gateway_boot_monotonic_us(tmp_path / "no-such-proc") is None


def test_gateway_boot_monotonic_us_rejects_zero_clock_ticks(tmp_path, monkeypatch):
    _own_stat(tmp_path, b"7 (kirocrew) S 1 7 " + b"0 " * 16 + b"4242 0")
    _pin_tick_rate(monkeypatch, 0)

    assert r.gateway_boot_monotonic_us(tmp_path) is None


@pytest.mark.parametrize("raw_comm", [None, b"\xc3"])
def test_member_age_uses_proc_start_ticks(tmp_path, monkeypatch, raw_comm):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200, raw_comm=raw_comm)
    _pin_boot_clock(monkeypatch, now=100.0)

    assert r._scope_age_secs(None, [201], _NOW, r._ProcReads(proc)) == pytest.approx(57.58)


def test_member_age_returns_none_for_malformed_stat(tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    (proc / "201" / "stat").write_text("malformed")

    assert r._scope_age_secs(None, [201], _NOW, r._ProcReads(proc)) is None


def test_member_age_rejects_zero_clock_ticks(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _pin_tick_rate(monkeypatch, 0)

    assert r._scope_age_secs(None, [201], _NOW, r._ProcReads(proc)) is None


def test_scope_age_falls_back_to_youngest_readable_member(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200, start_ticks=1_000)
    _make_proc(proc, 202, pgrp=200, start_ticks=9_000)
    _pin_boot_clock(monkeypatch, now=100.0)

    assert r._scope_age_secs(None, [201, 202], _NOW, r._ProcReads(proc)) == pytest.approx(10.0)


def test_scope_age_fallback_returns_none_without_readable_member(tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    (proc / "201" / "stat").unlink()
    (proc / "202" / "stat").write_text("malformed")

    assert r._scope_age_secs(None, [201, 202], _NOW, r._ProcReads(proc)) is None


def test_instance_dir_reports_missing_per_instance_cgroup(tmp_path, monkeypatch):
    from kiro_crew import sandbox

    parent = tmp_path / "agents.slice"
    parent.mkdir()
    monkeypatch.setattr(sandbox, "_agents_slice_cgroup_dir", lambda: parent)
    monkeypatch.setattr(sandbox, "_agents_slice_name", lambda: "kirocrew-agents-test.slice")

    slice_dir, why = r._instance_scope_dir()

    assert slice_dir is None
    assert why == "per-instance slice has no cgroup dir (no scopes)"


def test_reap_scopes_reports_slice_listing_error(tmp_path):
    slice_file = tmp_path / "slice"
    slice_file.write_text("not a directory")
    rec = _Recorder()

    summary = _reap(slice_file, tmp_path / "proc", rec)

    assert summary.scanned == 0
    assert summary.reclaimed == 0
    assert summary.skipped == 0
    assert summary.reason.startswith("cannot list slice dir:")


def test_pidfd_send_error_survives_close_error(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201])
    closed = []
    real_os = r.os

    def send_error(_fd, _sig):
        raise OSError(5, "send failed")

    def close_error(fd):
        closed.append(fd)
        raise OSError(5, "close failed")

    monkeypatch.setattr(
        r,
        "os",
        _ModuleProxy(real_os, pidfd_open=lambda _pid: 71, close=close_error),
    )
    monkeypatch.setattr(r.signal, "pidfd_send_signal", send_error, raising=False)

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, proc, {})

    assert sent is False
    assert reason == "pidfd_send_signal failed (5)"
    assert closed == [71]


def test_scope_active_enter_rejects_zero_and_invalid_output(monkeypatch):
    monkeypatch.setattr(r.platform_compat, "trusted_system_bin", lambda _name: "/bin/systemctl")
    outputs = iter(["0\n", "not-a-timestamp\n"])

    class Result:
        @property
        def stdout(self):
            return next(outputs)

    monkeypatch.setattr(r.subprocess, "run", lambda *_args, **_kwargs: Result())

    assert r._scope_active_enter_us("never-active.scope") is None
    assert r._scope_active_enter_us("invalid.scope") is None


def test_scope_active_enter_returns_none_on_subprocess_errors(monkeypatch):
    monkeypatch.setattr(r.platform_compat, "trusted_system_bin", lambda _name: "/bin/systemctl")
    errors = iter([OSError(5, "failed"), r.subprocess.SubprocessError("failed")])

    def raise_next(*_args, **_kwargs):
        raise next(errors)

    monkeypatch.setattr(r.subprocess, "run", raise_next)

    assert r._scope_active_enter_us("oserror.scope") is None
    assert r._scope_active_enter_us("subprocess-error.scope") is None


@pytest.mark.asyncio
async def test_periodic_ticks_reclaim_successive_runtime_trees_without_gateway_restart(
    tmp_path, monkeypatch
):
    """Real loop, watchdog registration and reaper; only the OS table is synthetic."""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kiro_crew import sandbox, session, session_pid
    from kiro_crew.config import KiroCrewConfig

    proc, slice_dir = tmp_path / "proc", tmp_path / "slice"
    rec = _Recorder()
    now = [_NOW]
    boot = _enter_us_for_age(2000)
    entered = {}
    protected = {}
    for pid in range(701, 708):
        unit = f"run-protected-{pid}.scope"
        _make_proc(proc, pid, pgrp=pid if pid == 707 else 700, marker=pid != 706)
        scope = _make_scope(slice_dir, unit, [pid])
        rec.register(unit, scope)
        entered[unit] = _enter_us_for_age(1000)
        protected[scope / "cgroup.procs"] = (scope / "cgroup.procs").read_bytes()

    cfg = KiroCrewConfig()
    cfg.session.pool_size = 0
    cfg.session.timeout_secs = 60
    cfg.session.watchdog_rss_max_mb = 0
    manager = session.SessionManager(cfg, provider_factory=None)
    cleanup = manager._cleanup_boundary()
    client = SimpleNamespace(_pid=701)
    manager._sessions["test:active"] = SimpleNamespace(provider=SimpleNamespace(client=client))
    manager._warm_pool.put_nowait((SimpleNamespace(client=SimpleNamespace(_pid=702)), 0.0))
    manager._starting_pids.add(703)
    # nosemgrep: kirocrew.mock-pid-allocatable -- a fake companion runtime's pid, read only into the active-pid shield through _companion_runtime_pids and never signalled; moved verbatim
    manager._subagent_runtimes["test:companion"] = SimpleNamespace(pid=704, is_alive=lambda: True)
    monkeypatch.setattr(session_pid, "_protected_pids", lambda: set())
    monkeypatch.setattr(session_pid, "_read_tracked_agent_pids", lambda: ({705}, True))
    monkeypatch.setattr(sandbox, "_probe_cgroup_scope", lambda: (True, "fixture"))
    monkeypatch.setattr(r, "_instance_scope_dir", lambda: (slice_dir, ""))
    monkeypatch.setattr(r, "_cached_gateway_boot_us", lambda: boot)
    monkeypatch.setattr(r, "time", _ModuleProxy(r.time, clock_gettime=lambda _: now[0]))
    monkeypatch.setattr(r, "os", _ModuleProxy(r.os, getpid=lambda: 900))
    monkeypatch.setattr(r, "_sel_scope_reap", lambda *args: None)
    core = r.reap_scopes
    summaries = []

    def reap_fixture(path, **kwargs):
        assert kwargs["gateway_boot_us"] == boot
        result = core(
            path,
            **kwargs,
            proc_root=proc,
            stop_unit=rec.stop_unit,
            signal_owned=rec.signal_owned,
            sleep=rec.sleep,
            active_enter_us=entered.get,
        )
        summaries.append(result.reclaimed)
        return result

    monkeypatch.setattr(r, "reap_scopes", reap_fixture)
    # Keep the registered scope hook real; unrelated maintenance must never run.
    for name in (
        "_expire_idle_hook",
        "_orphan_mcp_hook",
        "_rss_threshold_check",
        "_stuck_turn_check",
        "_bg_drain_reap_hook",
    ):
        monkeypatch.setattr(cleanup, name, AsyncMock())
    for name in (
        "_sweep_session_roots",
        "_sweep_sandbox_artifacts",
        "_maybe_prune_pycache",
        "_sweep_periodic_pids",
    ):
        monkeypatch.setattr(cleanup, name, AsyncMock())

    ticks = []
    round_state = {}

    async def advance():
        tick = len(ticks)
        cycle, phase = divmod(tick, 5)
        leader = 200 + cycle * 10
        unit = f"run-cycle-{cycle}.scope"
        client._pid = None if phase == 2 else 701  # incomplete active snapshot
        if phase == 0:
            _make_proc(proc, leader, pgrp=leader)
            _make_proc(proc, leader + 1, pgrp=leader)
            scope = _make_scope(slice_dir, unit, [leader, leader + 1])
            rec.register(unit, scope)
            entered[unit] = int(now[0] * 1_000_000)
            round_state["scope"] = scope
        elif phase == 1:
            # The runtime leader exits before the grace floor, not the gateway.
            now[0] += 1
            for path in (proc / str(leader)).iterdir():
                path.unlink()
            (proc / str(leader)).rmdir()
            (round_state["scope"] / "cgroup.procs").write_text(f"{leader + 1}\n")
        elif phase == 2:
            now[0] += r._REAP_MIN_AGE_SECS + 1
        raise asyncio.TimeoutError

    shutdown = SimpleNamespace(is_set=lambda: len(ticks) == 10, wait=advance)
    monkeypatch.setattr(session, "shutdown_event", shutdown)

    async def record_tick():
        cycle, phase = divmod(len(ticks), 5)
        expected = [f"run-cycle-{n}.scope" for n in range(cycle + int(phase >= 3))]
        assert rec.stopped == expected
        assert all(path.read_bytes() == body for path, body in protected.items())
        if phase >= 3:
            assert (round_state["scope"] / "cgroup.procs").read_bytes() == b""
        ticks.append(phase)

    monkeypatch.setattr(cleanup, "_sweep_untracked_mcps", record_tick)
    with ThreadPoolExecutor(max_workers=1) as executor:
        monkeypatch.setattr(session, "maintenance_executor", lambda: executor)
        await finished(
            cleanup._run_cleanup_ticks(cleanup._adopt_idle_policy()),
            "ten cleanup ticks over the one-worker executor",
        )
    assert ticks == list(range(5)) * 2
    assert summaries == [0, 0, 1, 0] * 2
    assert rec.killed == rec.slept == []
