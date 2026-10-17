"""Tests for the abandoned-agent-scope reaper (session_scope_reap.py).

Every test builds a fake cgroup slice tree and a fake ``/proc`` under
``tmp_path`` and injects the systemd/signal seams, so no real systemd unit is
stopped and no real process is signalled — the reclaim path is asserted purely
through recorded calls.
"""

from __future__ import annotations

import signal
import sys
from pathlib import Path

import pytest

from kiro_crew import session_scope_reap as r
from kiro_crew.constants import KIROCREW_SPAWNED_ENV, KIROCREW_SPAWNED_VALUE

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Agent scope reaping is Linux-only")

# now_monotonic reference used across tests; ages are derived from active-enter.
_NOW = 1_000_000.0


@pytest.fixture(autouse=True)
def _scope_errors_fail_the_test(request, monkeypatch):
    """Fail any test in which a scope evaluation or reclaim raised, unless it opts in.

    The sweep turns an exception into a skip so one bad scope cannot stop the
    others, which would otherwise let every "skips X" test pass over a crash in
    the very rule it means to pin. A test that provokes an error on purpose
    requests ``allow_scope_errors``.
    """
    errors: list[tuple[str, str, BaseException]] = []
    real = r._warn_scope_error

    def record(unit_name, phase, exc, errored):
        errors.append((unit_name, phase, exc))
        real(unit_name, phase, exc, errored)

    monkeypatch.setattr(r, "_warn_scope_error", record)
    monkeypatch.setattr(r, "_SCOPE_ERRORS_WARNED", {})
    yield errors
    if "allow_scope_errors" not in request.fixturenames:
        assert errors == [], f"a scope raised during the sweep: {errors!r}"


@pytest.fixture
def allow_scope_errors(_scope_errors_fail_the_test):
    """Opt in to scope errors; the value is the list of ``(unit, phase, exc)`` raised."""
    return _scope_errors_fail_the_test


def _pin_tick_rate(monkeypatch, hz: int) -> None:
    """Make ``os.sysconf("SC_CLK_TCK")`` answer *hz*, behind the real guards."""
    real = r.platform_compat.os.sysconf
    monkeypatch.setattr(
        r.platform_compat.os, "sysconf", lambda name: hz if name == "SC_CLK_TCK" else real(name)
    )


def _pin_boot_clock(monkeypatch, *, now: float = 100.0) -> None:
    """Pin the boot clock at *now* and the tick rate at 100 Hz."""
    monkeypatch.setattr(r.platform_compat, "boottime_now", lambda: now)
    _pin_tick_rate(monkeypatch, 100)


def _count_stat_reads(monkeypatch) -> list[int]:
    reads: list[int] = []
    real = r.platform_compat.read_proc_stat

    def counting(pid, *, proc_root=None):
        reads.append(pid)
        return real(pid, proc_root=proc_root)

    monkeypatch.setattr(r.platform_compat, "read_proc_stat", counting)
    return reads


def _enter_us_for_age(age_secs: float) -> int:
    return int((_NOW - age_secs) * 1_000_000)


def _make_proc(
    proc_root: Path,
    pid: int,
    *,
    pgrp: int,
    marker: bool = True,
    comm: str = "kiro-cli-chat",
    ppid: int = 1,
    cmdline: bytes | None = None,
    raw_comm: bytes | None = None,
    start_ticks: int = 4242,
) -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    environ = b"PATH=/usr/bin\x00"
    if marker:
        environ += f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode() + b"\x00"
    (d / "environ").write_bytes(environ)
    # /proc/<pid>/stat: "pid (comm) state ppid pgrp ...". After the last ')':
    # index 0 state, 1 ppid, 2 pgrp, ... 19 starttime.
    after = ["S", str(ppid), str(pgrp)] + ["0"] * 16 + [str(start_ticks)]
    # ``raw_comm`` writes the name as the kernel stores it: any bytes a process
    # set through prctl(PR_SET_NAME), valid UTF-8 or not.
    name = raw_comm if raw_comm is not None else comm.encode()
    (d / "stat").write_bytes(f"{pid} (".encode() + name + b") " + " ".join(after).encode())
    (d / "cmdline").write_bytes(cmdline if cmdline is not None else comm.encode() + b"\x00")


def _make_scope(slice_dir: Path, unit: str, pids: list[int]) -> Path:
    scope = slice_dir / unit
    scope.mkdir(parents=True, exist_ok=True)
    (scope / "cgroup.procs").write_text("\n".join(str(p) for p in pids) + ("\n" if pids else ""))
    return scope


class _Recorder:
    def __init__(self, *, empty_on_stop: bool = True):
        self.stopped: list[str] = []
        self.killed: list[tuple[int, int]] = []
        self.slept: list[float] = []
        self._empty_on_stop = empty_on_stop
        self._scope_by_unit: dict[str, Path] = {}

    def register(self, unit: str, scope_dir: Path) -> None:
        self._scope_by_unit[unit] = scope_dir

    def stop_unit(self, unit: str) -> bool:
        self.stopped.append(unit)
        if self._empty_on_stop and unit in self._scope_by_unit:
            (self._scope_by_unit[unit] / "cgroup.procs").write_text("")
        return True

    def kill(self, pid: int, sig: int) -> None:
        self.killed.append((pid, sig))

    def signal_owned(
        self,
        pid: int,
        sig: int,
        _members: list[int],
        _scope_dir: Path,
        _proc_root: Path,
        _pinned,
    ) -> tuple[bool, str]:
        self.kill(pid, sig)
        return True, ""

    def sleep(self, secs: float) -> None:
        self.slept.append(secs)


def _reap(
    slice_dir,
    proc_root,
    rec,
    *,
    active=None,
    tracked=None,
    gateway_boot_us=0,
    enter=None,
    min_age=600,
    stop_unit=None,
    signal_owned=None,
):
    enter = enter or {}
    return r.reap_scopes(
        slice_dir,
        active_pids=set(active or set()),
        tracked_pids=set(tracked or set()),
        gateway_boot_us=gateway_boot_us,
        min_age_secs=min_age,
        now_monotonic=_NOW,
        proc_root=proc_root,
        stop_unit=stop_unit or rec.stop_unit,
        signal_owned=signal_owned or rec.signal_owned,
        sleep=rec.sleep,
        active_enter_us=lambda unit: enter.get(unit),
    )


def test_reclaims_dead_leader_untracked(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    # Leader pid 200 is DEAD (no /proc entry); members 201/202 point at it.
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    scope = _make_scope(slice_dir, "run-u1.scope", [201, 202])
    rec = _Recorder()
    rec.register("run-u1.scope", scope)

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 1
    assert summary.skipped == 0
    assert rec.stopped == ["run-u1.scope"]
    assert rec.killed == []  # systemctl stop emptied it; no signals needed


def test_skips_tracked_pid(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-u1.scope", [201])
    rec = _Recorder()

    summary = _reap(
        slice_dir, proc, rec, tracked={201}, enter={"run-u1.scope": _enter_us_for_age(700)}
    )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []


def test_skips_active_provider_pid(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-u1.scope", [201])
    rec = _Recorder()

    summary = _reap(
        slice_dir, proc, rec, active={201}, enter={"run-u1.scope": _enter_us_for_age(700)}
    )

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_live_leader_postdating_boot(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    # Live leader (200 present, pgrp == self) and scope postdates boot.
    _make_proc(proc, 200, pgrp=200)
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-u1.scope", [200, 201])
    rec = _Recorder()

    summary = _reap(
        slice_dir,
        proc,
        rec,
        gateway_boot_us=1,  # enter (>0) postdates boot
        enter={"run-u1.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_under_age_threshold(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)  # leader 200 dead
    _make_scope(slice_dir, "run-u1.scope", [201])
    rec = _Recorder()

    # Age 100s <= 600s threshold, even though the leader is dead.
    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(100)})

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_missing_marker(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200, marker=False)  # readable environ, no marker
    _make_scope(slice_dir, "run-u1.scope", [201])
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_marker_inheriting_detached_server_without_runtime_anchor(tmp_path, caplog):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        201,
        pgrp=200,
        comm="python",
        cmdline=b"python\x00-m\x00http.server\x008000",
    )
    _make_scope(slice_dir, "run-server.scope", [201])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-server.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert "no-runtime-anchor=1" in caplog.text


def test_skips_marker_inheriting_runtime_substring_without_exact_anchor(tmp_path, caplog):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        201,
        pgrp=200,
        comm="claude-proxy",
        cmdline=b"/usr/local/bin/claude-proxy\x00--serve",
    )
    _make_scope(slice_dir, "run-proxy.scope", [201])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-proxy.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert "no-runtime-anchor=1" in caplog.text


def test_kiro_cli_chat_exact_basename_anchors_scope(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        201,
        pgrp=200,
        comm="kiro-cli-chat",
        cmdline=b"/opt/kiro/bin/kiro-cli-chat\x00acp",
    )
    scope = _make_scope(slice_dir, "run-chat.scope", [201])
    rec = _Recorder()
    rec.register("run-chat.scope", scope)

    summary = _reap(
        slice_dir,
        proc,
        rec,
        enter={"run-chat.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 1
    assert summary.skipped == 0
    assert rec.stopped == ["run-chat.scope"]


def _creds_helper_cmdline(version: str = "1.0.5917.0", port: int = 45257) -> bytes:
    """The toolbox sandbox credential helper's real argv, NUL-separated."""
    argv = [
        f"/home/u/.toolbox/tools/aim/{version}/sandbox/creds_agent",
        "--port",
        str(port),
        "--session-id",
        "0d86abba-c249-4b97-8b2e-e28d11f8f345",
        "--exit-on-orphan",
    ]
    return b"\x00".join(a.encode() for a in argv) + b"\x00"


def test_marked_credential_helper_alone_anchors_abandoned_scope(tmp_path):
    # A session that ended -- cleanly or by a crash -- leaves its scope holding
    # only the credential helper: the runtime that was the group leader is gone,
    # so the helper has no client and its threads are pure overhead.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        401,
        pgrp=400,  # the runtime that led the group has exited
        comm="creds_agent",
        cmdline=_creds_helper_cmdline(),
    )
    scope = _make_scope(slice_dir, "run-creds.scope", [401])
    rec = _Recorder()
    rec.register("run-creds.scope", scope)

    summary = _reap(
        slice_dir,
        proc,
        rec,
        enter={"run-creds.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 1
    assert summary.skipped == 0
    assert rec.stopped == ["run-creds.scope"]


def test_helper_that_is_its_own_live_leader_waits_for_a_pre_boot_scope(tmp_path):
    # A reparented helper can still be its own live group leader, so the
    # leader-dead arm does not carry it. The scope is then reclaimed only on the
    # OTHER arm -- it predates this gateway's boot, which is the restart case --
    # and a scope younger than the boot stamp is left alone.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        402,
        pgrp=402,  # its own group leader, alive
        comm="creds_agent",
        cmdline=_creds_helper_cmdline(),
    )
    scope = _make_scope(slice_dir, "run-creds-boot.scope", [402])
    rec = _Recorder()
    rec.register("run-creds-boot.scope", scope)
    enter_us = _enter_us_for_age(700)

    postdates = _reap(
        slice_dir,
        proc,
        rec,
        gateway_boot_us=enter_us - 1,
        enter={"run-creds-boot.scope": enter_us},
    )
    assert postdates.reclaimed == 0 and postdates.skipped == 1
    assert rec.stopped == []

    predates = _reap(
        slice_dir,
        proc,
        rec,
        gateway_boot_us=enter_us + 1,
        enter={"run-creds-boot.scope": enter_us},
    )
    assert predates.reclaimed == 1
    assert rec.stopped == ["run-creds-boot.scope"]


def test_unmarked_credential_helper_scope_is_left_alone(tmp_path, caplog):
    # The helper is the toolbox's binary, not one Kiro Crew spawns by name, so
    # without the spawn marker it is not attributable to this install and the
    # ownership condition refuses the scope before the anchor is even consulted.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        403,
        pgrp=400,
        marker=False,
        comm="creds_agent",
        cmdline=_creds_helper_cmdline(),
    )
    _make_scope(slice_dir, "run-creds-bare.scope", [403])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-creds-bare.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert "unowned=1" in caplog.text


def test_unreadable_environ_credential_helper_scope_is_left_alone(tmp_path, caplog):
    # An environ that cannot be read is not evidence of ownership, so a helper
    # whose owner cannot be established is never signalled.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        404,
        pgrp=400,
        comm="creds_agent",
        cmdline=_creds_helper_cmdline(),
    )
    (proc / "404" / "environ").unlink()
    _make_scope(slice_dir, "run-creds-opaque.scope", [404])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-creds-opaque.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert "unowned=1" in caplog.text


def test_live_session_scope_holding_a_credential_helper_is_untouched(tmp_path, caplog):
    # The helper of a session that is still running shares its scope with that
    # session's tracked runtime, and a tracked member refuses the whole scope.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 500, pgrp=500, comm="kiro-cli-chat")
    _make_proc(
        proc,
        501,
        pgrp=500,
        comm="creds_agent",
        ppid=500,
        cmdline=_creds_helper_cmdline(),
    )
    _make_scope(slice_dir, "run-live.scope", [500, 501])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            tracked={500},
            enter={"run-live.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert rec.killed == []
    assert "tracked=1" in caplog.text


def test_a_detached_survivor_beside_the_helper_keeps_the_scope_alive(tmp_path, caplog):
    # A scope can hold BOTH a leaked helper and work the user meant to keep: a
    # preview server the agent detached inherits the marker and outlives the
    # runtime. Authorizing on the helper alone would stop the whole scope and
    # kill that server, so the helper authorizes only where nothing else is left.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(
        proc,
        405,
        pgrp=400,
        comm="creds_agent",
        cmdline=_creds_helper_cmdline(),
    )
    _make_proc(
        proc,
        406,
        pgrp=400,
        comm="node",
        cmdline=b"/usr/bin/node\x00/app/node_modules/.bin/vite\x00--port\x005173\x00",
    )
    _make_scope(slice_dir, "run-creds-plus-server.scope", [405, 406])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-creds-plus-server.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []
    assert rec.killed == []
    assert "no-runtime-anchor=1" in caplog.text


def test_several_helpers_and_nothing_else_still_authorizes_the_stop(tmp_path):
    # The rule is universal, not single-member: a scope left holding only helpers
    # has no client for any of them.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    for pid, port in ((407, 45257), (408, 39743)):
        _make_proc(
            proc,
            pid,
            pgrp=400,
            comm="creds_agent",
            cmdline=_creds_helper_cmdline(port=port),
        )
    scope = _make_scope(slice_dir, "run-creds-pair.scope", [407, 408])
    rec = _Recorder()
    rec.register("run-creds-pair.scope", scope)

    summary = _reap(
        slice_dir,
        proc,
        rec,
        enter={"run-creds-pair.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 1
    assert rec.stopped == ["run-creds-pair.scope"]


class TestSandboxCredentialHelperRule:
    """The argv shape and the universal rule that authorize a helper-only stop.

    Exercised through :func:`_scope_is_only_credential_helpers` rather than the
    existential anchor: the helper is deliberately not one of that anchor's
    identities, because one member there authorizes stopping every sibling.
    """

    def _authorizes(self, cmdline: bytes, *, marker: bool = True) -> bool:
        proc = self.tmp_path / "proc"
        _make_proc(proc, 601, pgrp=600, marker=marker, comm="creds_agent", cmdline=cmdline)
        return r._scope_is_only_credential_helpers([601], r._ProcReads(proc))

    @pytest.fixture(autouse=True)
    def _tmp(self, tmp_path):
        self.tmp_path = tmp_path

    @pytest.mark.parametrize("version", ["1.0.5431.0", "1.0.5989.0"])
    def test_a_marked_helper_authorizes_at_any_toolbox_version(self, version):
        # The version is a path component ABOVE ``sandbox/``, so one match holds
        # across the several versions a long-lived host accumulates.
        assert self._authorizes(_creds_helper_cmdline(version=version)) is True

    def test_an_unmarked_helper_does_not_authorize(self):
        assert self._authorizes(_creds_helper_cmdline(), marker=False) is False

    def test_a_same_named_binary_outside_a_sandbox_dir_does_not_authorize(self):
        # A user's own ``creds_agent`` on PATH must not grant stop authority over
        # a scope, so the ``sandbox/`` parent component is part of the shape.
        cmdline = b"/usr/local/bin/creds_agent\x00--port\x0045257\x00--session-id\x00x\x00"
        assert self._authorizes(cmdline) is False

    def test_a_helper_path_without_the_session_argument_does_not_authorize(self):
        cmdline = (
            b"/home/u/.toolbox/tools/aim/1.0.5917.0/sandbox/creds_agent\x00--port\x0045257\x00"
        )
        assert self._authorizes(cmdline) is False

    def test_the_argument_must_be_an_argv_token_not_a_path_substring(self):
        cmdline = b"/home/u/--session-id/sandbox/creds_agent\x00--port\x0045257\x00"
        assert self._authorizes(cmdline) is False

    def test_an_empty_scope_authorizes_nothing(self):
        assert (
            r._scope_is_only_credential_helpers([], r._ProcReads(self.tmp_path / "proc")) is False
        )

    def test_the_helper_is_not_one_of_the_existential_anchor_identities(self):
        # Pinned in the direction that matters: if the helper ever becomes an
        # existential anchor, one of them authorizes killing every sibling.
        from kiro_crew.session_pid import _is_agent_runtime_anchor

        cmdline = _creds_helper_cmdline()
        assert _is_agent_runtime_anchor(cmdline, has_kirocrew_marker=True) is False
        assert _is_agent_runtime_anchor(cmdline, has_kirocrew_marker=False) is False


def test_reclaims_env_clearing_descendants_by_tree(tmp_path):
    # Playwright shape: a marked kiro-cli runtime (dead leader 300) with
    # chrome-headless children that cleared their environ. Ownership is by
    # descent and authorization needs only one runtime anchor, so the scope is
    # reclaimable AND the unmarked children are signalled in the fallback
    # (systemctl stop left them behind).
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli")  # marked runtime anchor
    _make_proc(proc, 302, pgrp=300, marker=False, comm="chrome-headless", ppid=301)
    _make_proc(proc, 303, pgrp=300, marker=False, comm="chrome-headless", ppid=302)  # grandchild
    scope = _make_scope(slice_dir, "run-u1.scope", [301, 302, 303])
    rec = _Recorder(empty_on_stop=False)
    rec.register("run-u1.scope", scope)

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0 and summary.skipped == 1  # fake scope never empties
    assert rec.stopped == ["run-u1.scope"]
    assert {pid for pid, _sig in rec.killed} == {301, 302, 303}


def test_non_utf8_comm_is_parsed_from_bytes_and_reclaimed(tmp_path):
    # A comm that is not valid UTF-8 -- a 15-byte truncation through a multibyte
    # name, or arbitrary prctl(PR_SET_NAME) bytes, here also carrying a ')' --
    # still yields pgrp and ppid, so the leader-dead and tree-ownership arms
    # both answer for it instead of raising out of the sweep.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli", raw_comm=b"kiro-cli\xe2\x9c")
    _make_proc(proc, 302, pgrp=300, marker=False, comm="chrome", ppid=301, raw_comm=b"\xff) \xfe x")
    scope = _make_scope(slice_dir, "run-u1.scope", [301, 302])
    rec = _Recorder()
    rec.register("run-u1.scope", scope)

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 1
    assert rec.stopped == ["run-u1.scope"]


def test_a_failing_scope_never_stops_the_scopes_after_it(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    # The sweep is a sorted loop over every scope; an error deciding one must
    # cost that scope alone, warn once rather than every tick, stay in every
    # tick's INFO summary, and leave every scope sorting after it reclaimable.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 301, pgrp=300)
    _make_scope(slice_dir, "run-a.scope", [201])
    good = _make_scope(slice_dir, "run-b.scope", [301])
    rec = _Recorder()
    rec.register("run-b.scope", good)
    real_decide = r._scope_reclaimable

    def decide(scope_dir, **kwargs):
        if scope_dir.name == "run-a.scope":
            raise RuntimeError("unreadable scope")
        return real_decide(scope_dir, **kwargs)

    monkeypatch.setattr(r, "_scope_reclaimable", decide)
    enter = {"run-a.scope": _enter_us_for_age(700), "run-b.scope": _enter_us_for_age(700)}

    with caplog.at_level("INFO", logger=r.__name__):
        first = _reap(slice_dir, proc, rec, enter=enter)
        second = _reap(slice_dir, proc, rec, enter=enter)

    assert (first.scanned, first.reclaimed, first.skipped) == (2, 1, 1)
    assert (second.reclaimed, second.skipped) == (0, 2)  # run-b emptied by the first stop
    assert rec.stopped == ["run-b.scope"]
    warnings = [
        rec_.getMessage()
        for rec_ in caplog.records
        if rec_.levelname == "WARNING" and "unit=run-a.scope" in rec_.getMessage()
    ]
    assert len(warnings) == 1
    assert "evaluation failed (RuntimeError: unreadable scope)" in warnings[0]
    summaries = [m for m in caplog.messages if "skipped old scope(s)" in m]
    assert summaries == [
        "agent_scope_reap: skipped old scope(s): error=1",
        "agent_scope_reap: skipped old scope(s): error=1 no-members=1",
    ]
    assert [(unit, phase) for unit, phase, _exc in allow_scope_errors] == [
        ("run-a.scope", "evaluation"),
        ("run-a.scope", "evaluation"),
    ]


def _scope_warnings(caplog, unit: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and f"unit={unit}:" in record.getMessage()
    ]


def test_a_clean_check_re_arms_the_warning(tmp_path, monkeypatch, caplog, allow_scope_errors):
    # Error, clean ticks, error again: the second error is a new event and warns,
    # so the first day's WARNING is not the only one an operator ever gets.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-x.scope", [201])
    rec = _Recorder()
    real_decide = r._scope_reclaimable
    inject: dict[str, Exception] = {}

    def decide(scope_dir, **kwargs):
        if scope_dir.name in inject:
            raise inject[scope_dir.name]
        return real_decide(scope_dir, **kwargs)

    monkeypatch.setattr(r, "_scope_reclaimable", decide)

    with caplog.at_level("WARNING", logger=r.__name__):
        inject["run-x.scope"] = RuntimeError("first defect")
        _reap(slice_dir, proc, rec, tracked={201})
        inject.clear()
        for _ in range(3):
            _reap(slice_dir, proc, rec, tracked={201})
        assert r._SCOPE_ERRORS_WARNED == {}
        inject["run-x.scope"] = RuntimeError("the same defect, back")
        _reap(slice_dir, proc, rec, tracked={201})

    assert len(_scope_warnings(caplog, "run-x.scope")) == 2


def test_a_recurring_error_warns_again_on_a_new_type_or_after_the_interval(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-x.scope", [201])
    rec = _Recorder()
    raised: list[Exception] = []
    clock = [1000.0]
    monkeypatch.setattr(r, "time", _ModuleProxy(r.time, monotonic=lambda: clock[0]))

    def decide(_scope_dir, **_kwargs):
        raise raised[-1]

    monkeypatch.setattr(r, "_scope_reclaimable", decide)

    with caplog.at_level("WARNING", logger=r.__name__):
        for exc, advance in (
            (RuntimeError("one"), 0.0),
            (RuntimeError("one"), 60.0),  # same type, inside the interval: DEBUG only
            (TypeError("two"), 60.0),  # a different defect: WARNING at once
            (TypeError("two"), r._SCOPE_ERROR_REWARN_SECS),  # still failing an hour on
        ):
            clock[0] += advance
            raised.append(exc)
            _reap(slice_dir, proc, rec)

    warnings = _scope_warnings(caplog, "run-x.scope")
    assert [w.split("(", 1)[1].split(":", 1)[0] for w in warnings] == [
        "RuntimeError",
        "TypeError",
        "TypeError",
    ]


def test_a_reclaim_error_re_arms_and_is_counted_in_the_summary(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    # A reclaim whose stop raises leaves the member behind: audited failed, in
    # the INFO summary as reclaim_error, and warned again after a clean check.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300)
    _make_scope(slice_dir, "run-y.scope", [301])
    rec = _Recorder()
    audits: list[str] = []
    monkeypatch.setattr(r, "_sel_scope_reap", lambda *args: audits.append(args[3]))

    def stop_raises(_unit):
        raise RuntimeError("stop failed")

    enter = {"run-y.scope": _enter_us_for_age(700)}
    with caplog.at_level("INFO", logger=r.__name__):
        _reap(slice_dir, proc, rec, enter=enter, stop_unit=stop_raises)
        _reap(slice_dir, proc, rec, enter=enter, tracked={301})
        _reap(slice_dir, proc, rec, enter=enter, stop_unit=stop_raises)

    assert audits == ["failed", "failed"]
    assert len(_scope_warnings(caplog, "run-y.scope")) == 2
    summaries = [m for m in caplog.messages if "skipped old scope(s)" in m]
    assert summaries == [
        "agent_scope_reap: skipped old scope(s): reclaim_error=1",
        "agent_scope_reap: skipped old scope(s): tracked=1",
        "agent_scope_reap: skipped old scope(s): reclaim_error=1",
    ]
    assert not [
        rec.getMessage()
        for rec in caplog.records
        if rec.levelname == "WARNING" and "could not fully clear" in rec.getMessage()
    ]


def test_a_reclaim_that_keeps_raising_names_the_unit_at_warning_once(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    # Consecutive raising ticks with the member still there: the rate-limited
    # WARNING is the only one naming the unit; the per-tick residue is DEBUG.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300)
    _make_scope(slice_dir, "run-y.scope", [301])
    rec = _Recorder()
    monkeypatch.setattr(r, "_sel_scope_reap", lambda *args: None)

    def stop_raises(_unit):
        raise RuntimeError("stop failed")

    enter = {"run-y.scope": _enter_us_for_age(700)}
    with caplog.at_level("DEBUG", logger=r.__name__):
        for _ in range(3):
            _reap(slice_dir, proc, rec, enter=enter, stop_unit=stop_raises)

    named = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "run-y.scope" in record.getMessage()
    ]
    assert len(named) == 1 and "reclaim failed (RuntimeError" in named[0]
    residue = [
        record.levelname
        for record in caplog.records
        if "could not fully clear unit=run-y.scope" in record.getMessage()
    ]
    assert residue == ["DEBUG"] * 3


def test_an_assertion_in_the_rules_is_not_swallowed(tmp_path, monkeypatch):
    # A broken invariant (or a test tripwire) must fail loudly, not read as one
    # more unreadable scope.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-a.scope", [201])

    def decide(_scope_dir, **_kwargs):
        raise AssertionError("tripwire")

    monkeypatch.setattr(r, "_scope_reclaimable", decide)

    with pytest.raises(AssertionError, match="tripwire"):
        _reap(slice_dir, proc, _Recorder())


def test_an_assertion_in_the_reclaim_is_not_swallowed(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_scope(slice_dir, "run-a.scope", [201])

    def tripwire(_unit):
        raise AssertionError("tripwire")

    with pytest.raises(AssertionError, match="tripwire"):
        _reap(
            slice_dir,
            proc,
            _Recorder(),
            enter={"run-a.scope": _enter_us_for_age(700)},
            stop_unit=tripwire,
        )


def test_a_reclaim_that_raises_after_emptying_the_scope_is_audited_completed(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    # The stop may already have landed, so a raise is audited by what it left
    # behind: an empty scope is a completed reclaim, still warned, and the
    # scopes after it are reclaimed too.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 301, pgrp=300)
    bad = _make_scope(slice_dir, "run-a.scope", [201])
    good = _make_scope(slice_dir, "run-b.scope", [301])
    rec = _Recorder()
    rec.register("run-b.scope", good)
    audits: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        r,
        "_sel_scope_reap",
        lambda unit, members, _reason, outcome: audits.append((unit, members, outcome)),
    )

    def stop_unit(unit):
        if unit == "run-a.scope":
            (bad / "cgroup.procs").write_text("")  # the stop landed...
            raise UnicodeDecodeError("utf-8", b"\xa5", 0, 1, "invalid start byte")  # ...then raised
        return rec.stop_unit(unit)

    enter = {"run-a.scope": _enter_us_for_age(700), "run-b.scope": _enter_us_for_age(700)}
    with caplog.at_level("WARNING", logger=r.__name__):
        summary = _reap(slice_dir, proc, rec, enter=enter, stop_unit=stop_unit)

    assert audits == [("run-a.scope", 1, "completed"), ("run-b.scope", 1, "completed")]
    assert (summary.reclaimed, summary.skipped) == (2, 0)
    assert [(unit, phase) for unit, phase, _exc in allow_scope_errors] == [
        ("run-a.scope", "reclaim")
    ]
    assert len(_scope_warnings(caplog, "run-a.scope")) == 1


def test_a_signal_that_raises_is_a_failed_reclaim_and_keeps_earlier_refusals(
    tmp_path, monkeypatch, caplog, allow_scope_errors
):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    _make_scope(slice_dir, "run-a.scope", [201, 202])
    rec = _Recorder(empty_on_stop=False)  # the stop leaves the members behind
    audits: list[str] = []
    monkeypatch.setattr(r, "_sel_scope_reap", lambda *args: audits.append(args[3]))

    def signal_owned(pid, *_args):
        if pid == 201:
            return False, "pidfd_open failed (1)"
        raise OSError(5, "signal failed")

    with caplog.at_level("WARNING", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            enter={"run-a.scope": _enter_us_for_age(700)},
            signal_owned=signal_owned,
        )

    assert audits == ["failed"]
    assert (summary.reclaimed, summary.skipped) == (0, 1)
    assert [phase for _unit, phase, _exc in allow_scope_errors] == ["reclaim"]
    assert (
        "agent_scope_reap signalling skipped unit=run-a.scope reasons=pidfd_open failed (1)"
        in caplog.messages
    )


def test_a_live_tracked_scope_reads_no_stat(tmp_path, monkeypatch):
    # With an active-enter stamp, a scope rejected at step (i) needs no field of
    # any member's stat, and a reclaimed one reads each member exactly once.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    for pid in (201, 202, 203):
        _make_proc(proc, pid, pgrp=200)
    scope = _make_scope(slice_dir, "run-u1.scope", [201, 202, 203])
    rec = _Recorder()
    rec.register("run-u1.scope", scope)
    reads = _count_stat_reads(monkeypatch)
    enter = {"run-u1.scope": _enter_us_for_age(700)}

    assert _reap(slice_dir, proc, rec, tracked={202}, enter=enter).reclaimed == 0
    assert reads == []

    assert _reap(slice_dir, proc, rec, enter=enter).reclaimed == 1
    # Each member once; the leader 200 their pgrp names is dead, so never read.
    assert sorted(reads) == [201, 202, 203]


def test_one_evaluation_reads_each_pid_once(tmp_path, monkeypatch):
    # No active-enter stamp, so age, ownership and the leader check all need
    # stat; the memo gives every arm the same single read of each process.
    from collections import Counter

    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli")
    _make_proc(proc, 302, pgrp=300, marker=False, comm="chrome", ppid=301)
    scope = _make_scope(slice_dir, "run-u1.scope", [301, 302])
    rec = _Recorder()
    rec.register("run-u1.scope", scope)
    _pin_boot_clock(monkeypatch, now=10_000.0)
    reads = _count_stat_reads(monkeypatch)

    assert _reap(slice_dir, proc, rec).reclaimed == 1
    assert Counter(reads) == {301: 1, 302: 1}


def test_the_read_memo_is_not_a_container(tmp_path):
    # A mapping-shaped memo with only __getitem__ falls back to the sequence
    # protocol on ``in`` and iteration, reading /proc/0, /proc/1, ... forever.
    reads = r._ProcReads(tmp_path)
    with pytest.raises(TypeError):
        201 in reads  # noqa: B015
    with pytest.raises(TypeError):
        iter(reads)


def test_a_live_leader_outside_the_scope_keeps_it_alive(tmp_path):
    # The members' group leader is alive and still a leader but is not in the
    # cgroup (a recycled or foreign pid, or a group the spawn did not lead). Its
    # stat is read on its own; without that read the leader-dead arm would hold.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 300, pgrp=300, marker=False, comm="bash")  # NOT a member
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli")
    _make_scope(slice_dir, "run-u1.scope", [301])
    rec = _Recorder()

    summary = _reap(
        slice_dir,
        proc,
        rec,
        gateway_boot_us=_enter_us_for_age(800),  # the scope postdates boot
        enter={"run-u1.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_an_unparsable_member_pgrp_fails_closed_to_leader_alive(tmp_path, caplog):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli")
    (proc / "301" / "stat").write_bytes(b"301 (kiro-cli) S 1 x " + b"0 " * 16 + b"4242")
    _make_scope(slice_dir, "run-u1.scope", [301])
    rec = _Recorder()

    with caplog.at_level("INFO", logger=r.__name__):
        summary = _reap(
            slice_dir,
            proc,
            rec,
            gateway_boot_us=_enter_us_for_age(800),
            enter={"run-u1.scope": _enter_us_for_age(700)},
        )

    assert summary.reclaimed == 0
    assert "leader-alive=1" in caplog.text


def _reparenting_scope(tmp_path, monkeypatch, *, child_start: int):
    """Marked runtime 301 with env-cleared child 302; SIGTERM kills 301 only.

    When 301 dies, 302 is rewritten with ppid 1 and start ticks *child_start*:
    4242 (its own) models a reparented child, anything else a recycled pid.
    """
    proc = tmp_path / "proc"
    slice_dir = tmp_path / "slice"
    _make_proc(proc, 301, pgrp=300, comm="kiro-cli")
    _make_proc(proc, 302, pgrp=300, marker=False, comm="chrome", ppid=301)
    scope = _make_scope(slice_dir, "run-u1.scope", [301, 302])
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(
        r, "os", _ModuleProxy(r.os, pidfd_open=lambda pid: pid + 1000, close=lambda _fd: None)
    )

    def send(fd, sig):
        pid = fd - 1000
        signalled.append((pid, sig))
        if pid == 301 and sig == signal.SIGTERM:
            (scope / "cgroup.procs").write_text("302\n")
            _make_proc(proc, 302, pgrp=300, marker=False, comm="chrome", start_ticks=child_start)

    monkeypatch.setattr(r.signal, "pidfd_send_signal", send, raising=False)
    return slice_dir, proc, signalled


def test_a_child_reparented_by_its_parents_sigterm_still_gets_sigkill(tmp_path, monkeypatch):
    # The child ignored SIGTERM; its parent did not, so its ppid is now 1. It is
    # still the process this reclaim attributed (same pid, same start ticks),
    # so the escalation reaches it.
    slice_dir, proc, signalled = _reparenting_scope(tmp_path, monkeypatch, child_start=4242)

    _reap(
        slice_dir,
        proc,
        _Recorder(empty_on_stop=False),
        enter={"run-u1.scope": _enter_us_for_age(700)},
        signal_owned=r._pidfd_signal_owned,
    )

    assert (302, signal.SIGKILL) in signalled


def test_a_recycled_pid_in_the_scope_is_not_signalled_and_says_why(tmp_path, monkeypatch, caplog):
    # Same pid, different start ticks: a different process. The recheck reads
    # stat fresh after the pin, so the decision's reading cannot vouch for it.
    slice_dir, proc, signalled = _reparenting_scope(tmp_path, monkeypatch, child_start=9999)

    with caplog.at_level("WARNING", logger=r.__name__):
        _reap(
            slice_dir,
            proc,
            _Recorder(empty_on_stop=False),
            enter={"run-u1.scope": _enter_us_for_age(700)},
            signal_owned=r._pidfd_signal_owned,
        )

    assert (302, signal.SIGKILL) not in signalled
    assert any("member not attributable to this install" in m for m in caplog.messages)


def test_a_sigkill_that_settles_on_a_later_read_clears_the_scope(tmp_path):
    # A killed task stays in cgroup.procs until its exit completes; the reclaim
    # re-reads before judging, so a kill that worked is not audited as failed.
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201])
    rec = _Recorder(empty_on_stop=False)
    kills: list[int] = []

    def signal_owned(pid, sig, *_rest):
        if sig == signal.SIGTERM:
            return False, ""  # ignored: nothing to wait for
        kills.append(pid)
        return True, ""

    def sleep(secs):
        rec.sleep(secs)
        if kills:
            (scope / "cgroup.procs").write_text("")  # the exit completes

    cleared = r._reclaim_scope(
        scope,
        "run-u1.scope",
        proc_root=proc,
        stop_unit=rec.stop_unit,
        signal_owned=signal_owned,
        sleep=sleep,
    )

    assert cleared is True
    assert kills == [201]
    assert rec.slept == [r._EXIT_POLL_SECS]


def test_waits_end_when_the_signalled_members_are_gone(tmp_path):
    # 202 is refused, so it will never leave because of this reclaim; waiting
    # on the whole scope would spend the full grace and settle on it.
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201, 202])
    rec = _Recorder(empty_on_stop=False)

    def signal_owned(pid, _sig, *_rest):
        if pid == 202:
            return False, ""
        (scope / "cgroup.procs").write_text("202\n")  # 201 exits at once
        return True, ""

    cleared = r._reclaim_scope(
        scope,
        "run-u1.scope",
        proc_root=proc,
        stop_unit=rec.stop_unit,
        signal_owned=signal_owned,
        sleep=rec.sleep,
    )

    assert cleared is False  # 202 is still there, as before
    assert rec.slept == []


def test_a_gone_scope_drops_its_warning_entry(tmp_path, monkeypatch, allow_scope_errors):
    # The rate-limit map stays bounded by the scopes still failing: once a
    # failing scope is gone, the next sweep forgets it.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300)
    _make_scope(slice_dir, "run-b.scope", [301])

    def decide(_scope_dir, **_kwargs):
        raise RuntimeError("unreadable scope")

    monkeypatch.setattr(r, "_scope_reclaimable", decide)
    _reap(slice_dir, proc, _Recorder())
    assert list(r._SCOPE_ERRORS_WARNED) == [("run-b.scope", "evaluation")]

    (slice_dir / "run-b.scope" / "cgroup.procs").unlink()
    (slice_dir / "run-b.scope").rmdir()
    _reap(slice_dir, proc, _Recorder())
    assert r._SCOPE_ERRORS_WARNED == {}


def test_systemctl_output_is_decoded_leniently(monkeypatch):
    # A localized systemctl diagnostic in a legacy locale is not UTF-8; a strict
    # decode raised out of every scope's evaluation.
    monkeypatch.setattr(r.platform_compat, "trusted_system_bin", lambda _name: "/bin/systemctl")
    seen: list[dict] = []

    class Result:
        stdout = "123\n"
        returncode = 0

    monkeypatch.setattr(
        r.subprocess, "run", lambda *_args, **kwargs: seen.append(kwargs) or Result()
    )

    assert r._scope_active_enter_us("run-u1.scope") == 123
    assert r._systemctl_stop("run-u1.scope") is True
    assert [kwargs.get("errors") for kwargs in seen] == ["replace", "replace"]


def test_skips_unmarked_member_whose_parent_is_outside_scope(tmp_path):
    # An unmarked member parented to a pid that is NOT a scope member cannot be
    # attributed to us -> whole scope not reclaimable, nothing signalled. The
    # outsider is itself a child of our marked member, so only the "parent must
    # be a scope member" rule (not "parent must exist"/"parent > 1") blocks it.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300)  # marked
    _make_proc(proc, 999, pgrp=999, marker=False, comm="setsid-escapee", ppid=301)
    _make_proc(proc, 302, pgrp=300, marker=False, ppid=999)
    _make_scope(slice_dir, "run-u1.scope", [301, 302])  # 999 is NOT a member
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_scope_with_no_marked_member(tmp_path):
    # Unmarked members that only reference each other never bootstrap ownership.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 301, pgrp=300, marker=False)
    _make_proc(proc, 302, pgrp=300, marker=False, ppid=301)
    _make_scope(slice_dir, "run-u1.scope", [301, 302])
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_skips_unreadable_environ(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    # Member with a stat file but NO environ file -> unreadable -> fail closed.
    d = proc / "201"
    d.mkdir(parents=True)
    after = ["S", "1", "200"] + ["0"] * 16 + ["4242"]
    (d / "stat").write_text("201 (kiro-cli) " + " ".join(after))
    _make_scope(slice_dir, "run-u1.scope", [201])
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0
    assert rec.stopped == []


def test_unreadable_child_is_not_adopted_through_marked_parent(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    child = proc / "202"
    child.mkdir(parents=True)
    after = ["S", "201", "200"] + ["0"] * 16 + ["4242"]
    (child / "stat").write_text("202 (renderer) " + " ".join(after))
    _make_scope(slice_dir, "run-u1.scope", [201, 202])
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.reclaimed == 0
    assert summary.skipped == 1
    assert rec.stopped == []


def test_predates_boot_arm_reclaims_even_with_live_leader(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 200, pgrp=200)  # live leader
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(slice_dir, "run-u1.scope", [200, 201])
    rec = _Recorder()
    rec.register("run-u1.scope", scope)

    # enter predates the gateway boot stamp -> reclaimable despite a live leader.
    summary = _reap(
        slice_dir,
        proc,
        rec,
        gateway_boot_us=_enter_us_for_age(700) + 5_000_000,
        enter={"run-u1.scope": _enter_us_for_age(700)},
    )

    assert summary.reclaimed == 1


def test_fallback_signals_recheck_skips_recycled_pid(tmp_path, monkeypatch):
    # Exercises the reclaim fallback directly: at reclaim-recheck time pid 202
    # has lost the marker (its PID was recycled to an unrelated process), so it
    # must never be signalled even though it sits in cgroup.procs.
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)  # ours, still marked
    _make_proc(proc, 202, pgrp=200, marker=False)  # recycled: no marker
    scope = _make_scope(slice_dir, "run-u1.scope", [201, 202])
    rec = _Recorder(empty_on_stop=False)  # systemctl stop does NOT clear -> fallback fires
    signalled: list[tuple[int, int]] = []
    # Override os on the module reference (a proxy) rather than mutating the real
    # os module: a global ``os.close`` no-op silently leaks every descriptor that
    # any code — tmp_path teardown included — closes during this test.
    monkeypatch.setattr(
        r, "os", _ModuleProxy(r.os, pidfd_open=lambda pid: pid + 1000, close=lambda _fd: None)
    )
    monkeypatch.setattr(
        r.signal,
        "pidfd_send_signal",
        lambda fd, sig: signalled.append((fd - 1000, sig)),
        raising=False,
    )

    cleared = r._reclaim_scope(
        scope,
        "run-u1.scope",
        proc_root=proc,
        stop_unit=rec.stop_unit,
        signal_owned=r._pidfd_signal_owned,
        sleep=rec.sleep,
    )

    signalled_pids = {pid for pid, _sig in signalled}
    assert signalled_pids == {201}
    assert 202 not in signalled_pids
    # The grace for SIGTERM, then the settle after SIGKILL, each waited out in
    # full because the fake scope never empties.
    polls = round(r._TERM_GRACE_SECS / r._EXIT_POLL_SECS) + round(
        r._KILL_SETTLE_SECS / r._EXIT_POLL_SECS
    )
    assert rec.slept == [r._EXIT_POLL_SECS] * polls
    assert cleared is False  # fake scope never emptied


def test_fallback_skips_term_grace_when_no_signal_was_sent(tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(tmp_path / "slice", "run-u1.scope", [201])
    rec = _Recorder(empty_on_stop=False)
    attempted: list[int] = []

    def refuse_signal(
        _pid: int,
        sig: int,
        _members: list[int],
        _scope_dir: Path,
        _proc_root: Path,
        _pinned,
    ) -> tuple[bool, str]:
        attempted.append(sig)
        return False, ""

    cleared = r._reclaim_scope(
        scope,
        "run-u1.scope",
        proc_root=proc,
        stop_unit=rec.stop_unit,
        signal_owned=refuse_signal,
        sleep=rec.sleep,
    )

    assert attempted == [signal.SIGTERM, signal.SIGKILL]
    assert rec.slept == []
    assert cleared is False


def test_never_signals_pid_le_1_or_self(tmp_path, monkeypatch):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    my = 424242
    _make_proc(proc, 1, pgrp=1)
    _make_proc(proc, my, pgrp=200)
    _make_proc(proc, 201, pgrp=200)
    scope = _make_scope(slice_dir, "run-u1.scope", [1, my, 201])
    monkeypatch.setattr(r.os, "getpid", lambda: my)
    rec = _Recorder(empty_on_stop=False)

    r._reclaim_scope(
        scope,
        "run-u1.scope",
        proc_root=proc,
        stop_unit=rec.stop_unit,
        signal_owned=rec.signal_owned,
        sleep=rec.sleep,
    )

    signalled = {pid for pid, _sig in rec.killed}
    assert 1 not in signalled
    assert my not in signalled
    assert 201 in signalled


def test_empty_scope_is_skipped(tmp_path):
    slice_dir = tmp_path / "slice"
    proc = tmp_path / "proc"
    _make_scope(slice_dir, "run-u1.scope", [])
    rec = _Recorder()

    summary = _reap(slice_dir, proc, rec, enter={"run-u1.scope": _enter_us_for_age(700)})

    assert summary.scanned == 1
    assert summary.reclaimed == 0
    assert rec.stopped == []


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
    monkeypatch.setattr(r.time, "clock_gettime", lambda _clock: _NOW)

    def fake_reap(*_args, **kwargs):
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(r, "reap_scopes", fake_reap)

    assert r.reap_abandoned_agent_scopes({301}) is expected
    assert seen["tracked_pids"] == {201}
    assert seen["active_pids"] == {301}
    assert seen["min_age_secs"] == r._REAP_MIN_AGE_SECS


class _ModuleProxy:
    def __init__(self, module, **overrides):
        self._module = module
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._module, name)


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
        await asyncio.wait_for(cleanup._run_cleanup_ticks(cleanup._adopt_idle_policy()), 10)
    assert ticks == list(range(5)) * 2
    assert summaries == [0, 0, 1, 0] * 2
    assert rec.killed == rec.slept == []
