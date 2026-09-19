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
) -> None:
    d = proc_root / str(pid)
    d.mkdir(parents=True, exist_ok=True)
    environ = b"PATH=/usr/bin\x00"
    if marker:
        environ += f"{KIROCREW_SPAWNED_ENV}={KIROCREW_SPAWNED_VALUE}".encode() + b"\x00"
    (d / "environ").write_bytes(environ)
    # /proc/<pid>/stat: "pid (comm) state ppid pgrp ...". After the last ')':
    # index 0 state, 1 ppid, 2 pgrp, ... 19 starttime.
    after = ["S", str(ppid), str(pgrp)] + ["0"] * 16 + ["4242"]
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(after))
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
        stop_unit=rec.stop_unit,
        signal_owned=rec.signal_owned,
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
    monkeypatch.setattr(r.os, "pidfd_open", lambda pid: pid + 1000, raising=False)
    monkeypatch.setattr(r.os, "close", lambda _fd: None)
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
    assert rec.slept == [r._TERM_GRACE_SECS]  # SIGTERM, grace, then SIGKILL
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
    monkeypatch.setattr(r.os, "pidfd_open", lambda _pid: events.append("pin") or 71, raising=False)
    monkeypatch.setattr(r.os, "close", lambda _fd: events.append("close"))
    monkeypatch.setattr(
        r,
        "_scope_owned_pids",
        lambda _members, _proc: events.append("verify") or ({201}, ""),
    )
    monkeypatch.setattr(
        r.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: events.append("signal"),
        raising=False,
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, tmp_path)

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

    monkeypatch.setattr(r.os, "pidfd_open", pin_after_pid_reuse, raising=False)
    monkeypatch.setattr(r.os, "close", lambda _fd: None)
    monkeypatch.setattr(
        r.signal,
        "pidfd_send_signal",
        lambda _fd, _sig: signalled.append(201),
        raising=False,
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, proc)

    assert sent is False and reason == ""
    assert signalled == []


def test_pidfd_process_lookup_is_quiet(monkeypatch, tmp_path):
    def gone(_pid):
        raise ProcessLookupError

    monkeypatch.setattr(r.os, "pidfd_open", gone, raising=False)
    monkeypatch.setattr(r.signal, "pidfd_send_signal", lambda *_args: None, raising=False)
    assert r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path) == (False, "")


def test_pidfd_unavailable_never_falls_back_to_numeric_kill(monkeypatch, tmp_path):
    monkeypatch.delattr(r.os, "pidfd_open", raising=False)
    monkeypatch.delattr(r.signal, "pidfd_send_signal", raising=False)
    monkeypatch.setattr(
        r.platform_compat,
        "kill_pid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("numeric kill fallback used")),
    )

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path)

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

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], tmp_path, tmp_path)

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
    stat = Path("/proc/self/stat").read_text(encoding="utf-8")
    start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
    clk_tck = r.os.sysconf("SC_CLK_TCK")
    expected_us = int((after_mono - (after_boot - start_ticks / clk_tck)) * 1_000_000)

    assert isinstance(boot_us, int)
    assert int(before_mono * 1_000_000) >= boot_us
    assert abs(boot_us - expected_us) < 2_000_000


def test_gateway_boot_monotonic_us_rejects_zero_clock_ticks(monkeypatch):
    real_os = r.os

    def zero_clock_ticks(name):
        if name == "SC_CLK_TCK":
            return 0
        return real_os.sysconf(name)

    monkeypatch.setattr(r, "os", _ModuleProxy(real_os, sysconf=zero_clock_ticks))

    assert r.gateway_boot_monotonic_us() is None


def test_gateway_boot_monotonic_us_returns_none_on_proc_read_error(monkeypatch):
    class UnreadableProcStat:
        def read_text(self, **_kwargs):
            raise OSError(5, "unreadable")

    monkeypatch.setattr(r, "Path", lambda _path: UnreadableProcStat())

    assert r.gateway_boot_monotonic_us() is None


def test_pid_age_secs_uses_proc_start_ticks(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    real_os = r.os
    real_time = r.time
    monkeypatch.setattr(
        r,
        "os",
        _ModuleProxy(
            real_os,
            sysconf=lambda name: 100 if name == "SC_CLK_TCK" else real_os.sysconf(name),
        ),
    )
    monkeypatch.setattr(
        r,
        "time",
        _ModuleProxy(real_time, clock_gettime=lambda clock: 100.0),
    )

    assert r._pid_age_secs(201, proc) == pytest.approx(57.58)


def test_pid_age_secs_returns_none_for_malformed_stat(tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    (proc / "201" / "stat").write_text("malformed")

    assert r._pid_age_secs(201, proc) is None


def test_pid_age_secs_rejects_zero_clock_ticks(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    real_os = r.os

    def zero_clock_ticks(name):
        if name == "SC_CLK_TCK":
            return 0
        return real_os.sysconf(name)

    monkeypatch.setattr(r, "os", _ModuleProxy(real_os, sysconf=zero_clock_ticks))

    assert r._pid_age_secs(201, proc) is None


def test_scope_age_falls_back_to_youngest_readable_member(tmp_path, monkeypatch):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    for pid, start_ticks in ((201, 1_000), (202, 9_000)):
        stat_path = proc / str(pid) / "stat"
        prefix, _old_ticks = stat_path.read_text().rsplit(" ", 1)
        stat_path.write_text(f"{prefix} {start_ticks}")

    real_os = r.os
    real_time = r.time
    monkeypatch.setattr(
        r,
        "os",
        _ModuleProxy(
            real_os,
            sysconf=lambda name: 100 if name == "SC_CLK_TCK" else real_os.sysconf(name),
        ),
    )
    monkeypatch.setattr(
        r,
        "time",
        _ModuleProxy(real_time, clock_gettime=lambda clock: 100.0),
    )

    assert r._scope_age_secs(None, [201, 202], proc, _NOW) == pytest.approx(10.0)


def test_scope_age_fallback_returns_none_without_readable_member(tmp_path):
    proc = tmp_path / "proc"
    _make_proc(proc, 201, pgrp=200)
    _make_proc(proc, 202, pgrp=200)
    (proc / "201" / "stat").unlink()
    (proc / "202" / "stat").write_text("malformed")

    assert r._scope_age_secs(None, [201, 202], proc, _NOW) is None


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

    sent, reason = r._pidfd_signal_owned(201, signal.SIGTERM, [201], scope, proc)

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
