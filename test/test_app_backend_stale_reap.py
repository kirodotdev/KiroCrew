"""Tests for the app-backend startup stale-reap (pidfile + PID-reuse-safe kill).

These pin the safety contract of ``_reap_stale_app_backends``: it terminates a
recorded app-backend pid ONLY when the pid is alive AND its start_time POSITIVELY
matches what was recorded at spawn (so a recycled pid, an unreadable start_time,
or a pid owned by another uid is never killed). Entries it cannot confirm-and-kill
but that are still alive are KEPT for a later attempt; handled entries are dropped.
"""
from __future__ import annotations

import signal
from unittest.mock import patch

import pytest

import kiro_crew.apps.backend as backend_mod


@pytest.fixture
def pidfile(tmp_path, monkeypatch):
    path = tmp_path / "app_backends.pids.json"
    monkeypatch.setattr(backend_mod, "_pidfile_path", lambda: path)
    return path


def test_record_read_forget_roundtrip(pidfile):
    with patch.object(backend_mod, "_proc_start_time", return_value="ST-1"):
        backend_mod._record_app_pid("code_reviewer", 4321, 9100)
    assert backend_mod._read_pidfile()["code_reviewer"] == {
        "pid": 4321, "start_time": "ST-1", "port": 9100,
    }
    backend_mod._forget_app_pid("code_reviewer")
    assert "code_reviewer" not in backend_mod._read_pidfile()


def test_record_ignores_nonpositive_pid(pidfile):
    backend_mod._record_app_pid("x", 0, 9100)
    assert backend_mod._read_pidfile() == {}


def test_reap_empty_pidfile_returns_zero(pidfile):
    assert backend_mod._reap_stale_app_backends() == 0


def test_reap_kills_matched_alive_orphan(pidfile):
    backend_mod._write_pidfile({"code_reviewer": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    alive = {"v": True}

    def fake_kill(pid, sig):
        if sig == 0 and not alive["v"]:
            raise ProcessLookupError
        return None

    def fake_killpg(pgid, sig):
        if sig == signal.SIGTERM:
            alive["v"] = False  # SIGTERM took effect

    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(backend_mod.os, "kill", side_effect=fake_kill),
        patch.object(backend_mod.os, "getpgid", return_value=4321),
        patch.object(backend_mod.os, "killpg", side_effect=fake_killpg) as mock_killpg,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()

    assert n == 1
    mock_killpg.assert_any_call(4321, signal.SIGTERM)
    assert backend_mod._read_pidfile() == {}  # cleared for the new generation


def test_reap_skips_recycled_pid(pidfile):
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-OLD", "port": 9100}})
    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-NEW"),  # mismatch
        patch.object(backend_mod.os, "kill", return_value=None),  # alive
        patch.object(backend_mod.os, "killpg") as mock_killpg,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 0
    mock_killpg.assert_not_called()  # recycled pid must NOT be killed
    # Alive but unconfirmed → KEPT for a later attempt (not abandoned).
    assert "app" in backend_mod._read_pidfile()


def test_reap_keeps_alive_entry_when_start_time_unreadable(pidfile):
    # ps failing NOW (live start_time None) must not kill and must not drop the
    # entry — a transient ps failure should not permanently abandon a real orphan.
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    with (
        patch.object(backend_mod, "_proc_start_time", return_value=None),  # ps failed
        patch.object(backend_mod.os, "kill", return_value=None),  # alive
        patch.object(backend_mod.os, "killpg") as mock_killpg,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 0
    mock_killpg.assert_not_called()
    assert "app" in backend_mod._read_pidfile()  # kept


def test_reap_does_not_kill_when_recorded_start_time_missing(pidfile):
    # Fail-open guard: a record whose start_time was None at spawn must NOT be
    # killed (it cannot be positively identified), and must be kept while alive.
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": None, "port": 9100}})
    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-LIVE"),
        patch.object(backend_mod.os, "kill", return_value=None),  # alive
        patch.object(backend_mod.os, "killpg") as mock_killpg,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 0
    mock_killpg.assert_not_called()
    assert "app" in backend_mod._read_pidfile()


def test_reap_skips_dead_pid(pidfile):
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})

    def dead(pid, sig):
        raise ProcessLookupError

    with (
        patch.object(backend_mod.os, "kill", side_effect=dead),
        patch.object(backend_mod.os, "killpg") as mock_killpg,
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 0
    mock_killpg.assert_not_called()
    assert backend_mod._read_pidfile() == {}  # dead entry dropped


def test_reap_skips_pid_owned_by_other_uid(pidfile):
    # os.kill(pid, 0) raising PermissionError means the process EXISTS but is
    # ours to leave alone — not killed, and dropped (not our orphan).
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})

    def eperm(pid, sig):
        raise PermissionError

    with (
        patch.object(backend_mod.os, "kill", side_effect=eperm),
        patch.object(backend_mod.os, "killpg") as mock_killpg,
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 0
    mock_killpg.assert_not_called()
    assert backend_mod._read_pidfile() == {}


def test_reap_escalates_to_sigkill_when_sigterm_ignored(pidfile):
    # A matched orphan that ignores SIGTERM must be SIGKILLed after the grace
    # window. Patch the timing constants to keep the test fast.
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    signals: list[int] = []

    def fake_killpg(pgid, sig):
        signals.append(sig)  # never dies, even after SIGTERM

    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(backend_mod.os, "kill", return_value=None),  # always alive
        patch.object(backend_mod.os, "getpgid", return_value=4321),
        patch.object(backend_mod.os, "killpg", side_effect=fake_killpg),
        patch.object(backend_mod, "sel"),
        patch.object(backend_mod, "_REAP_SIGTERM_GRACE", 0.05),
        patch.object(backend_mod, "_REAP_POLL_INTERVAL", 0.01),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 1
    assert signal.SIGTERM in signals
    assert signal.SIGKILL in signals  # escalation fired
    assert backend_mod._read_pidfile() == {}  # handled → dropped


def test_reap_per_pid_grace_not_shared(pidfile):
    # Two SIGTERM-ignoring orphans: each must get its OWN SIGKILL. A shared
    # deadline would SIGKILL the second instantly but still kill both — so assert
    # BOTH pids are SIGKILLed (the regression would skip the SIGKILL entirely if
    # the budget were exhausted by the first and the while/else logic differed).
    backend_mod._write_pidfile({
        "a": {"pid": 11, "start_time": "ST", "port": 9100},
        "b": {"pid": 22, "start_time": "ST", "port": 9101},
    })
    killed: list[tuple[int, int]] = []

    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST"),
        patch.object(backend_mod.os, "kill", return_value=None),  # always alive
        patch.object(backend_mod.os, "getpgid", side_effect=lambda p: p),
        patch.object(backend_mod.os, "killpg", side_effect=lambda pg, s: killed.append((pg, s))),
        patch.object(backend_mod, "sel"),
        patch.object(backend_mod, "_REAP_SIGTERM_GRACE", 0.02),
        patch.object(backend_mod, "_REAP_POLL_INTERVAL", 0.01),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 2
    assert (11, signal.SIGKILL) in killed
    assert (22, signal.SIGKILL) in killed


def test_reap_preserves_concurrent_write_during_scan(pidfile):
    # The final pidfile rewrite must MERGE, not clobber: an entry added by a
    # concurrent enable mid-scan (simulated by writing during _proc_start_time)
    # must survive even though it was not present when the scan began. 'old' is
    # alive + matched, so it is reaped and dropped.
    backend_mod._write_pidfile({"old": {"pid": 4321, "start_time": "ST-1", "port": 9100}})

    def racing_start_time(pid):
        # Simulate a concurrent _record_app_pid landing during the scan.
        data = backend_mod._read_pidfile()
        data["new"] = {"pid": 5555, "start_time": "ST-NEW", "port": 9101}
        backend_mod._write_pidfile(data)
        return "ST-1"  # matches → 'old' is reaped

    with (
        patch.object(backend_mod, "_proc_start_time", side_effect=racing_start_time),
        patch.object(backend_mod.os, "kill", return_value=None),  # alive, then exits
        patch.object(backend_mod.os, "getpgid", return_value=4321),
        patch.object(backend_mod.os, "killpg"),
        patch.object(backend_mod, "sel"),
        patch.object(backend_mod, "_REAP_SIGTERM_GRACE", 0.0),  # skip the SIGKILL wait
        patch.object(backend_mod, "_REAP_POLL_INTERVAL", 0.01),
    ):
        backend_mod._reap_stale_app_backends()
    result = backend_mod._read_pidfile()
    assert "new" in result  # concurrent write preserved
    assert "old" not in result  # reaped entry dropped


def test_reap_keeps_handled_entry_rerecorded_with_new_pid(pidfile):
    # A handled (reaped) app that a concurrent enable RE-RECORDS with a NEW pid
    # mid-scan must NOT be clobbered by the final merge. The unconditional pop
    # would delete the fresh new-generation entry and re-introduce the orphan
    # leak this feature prevents; the merge drops an entry only if it still
    # equals what was handled.
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})

    def racing_start_time(pid):
        # A concurrent _record_app_pid for the SAME app with a NEW pid lands
        # during the scan (user re-enabling the app while the stale pid is reaped).
        data = backend_mod._read_pidfile()
        data["app"] = {"pid": 9999, "start_time": "ST-NEW", "port": 9100}
        backend_mod._write_pidfile(data)
        return "ST-1"  # matches the ORIGINAL pid 4321 → it is reaped

    with (
        patch.object(backend_mod, "_proc_start_time", side_effect=racing_start_time),
        patch.object(backend_mod.os, "kill", return_value=None),  # alive
        patch.object(backend_mod.os, "getpgid", return_value=4321),
        patch.object(backend_mod.os, "killpg"),
        patch.object(backend_mod, "sel"),
        patch.object(backend_mod, "_REAP_SIGTERM_GRACE", 0.0),
        patch.object(backend_mod, "_REAP_POLL_INTERVAL", 0.01),
    ):
        backend_mod._reap_stale_app_backends()
    # The fresh entry (new pid) survived the merge; it was NOT clobbered by the
    # reaped original.
    assert backend_mod._read_pidfile().get("app", {}).get("pid") == 9999


def test_reap_skips_sigkill_when_pid_recycled_during_grace(pidfile):
    # An orphan that ignores SIGTERM is polled for the grace window; if its pid
    # is recycled to an unrelated process during that window (start_time
    # changes), the delayed SIGKILL must NOT fire — same PID-reuse guard as the
    # SIGTERM path (leak-not-mis-kill).
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    st_calls = {"n": 0}

    def start_time(pid):
        st_calls["n"] += 1
        # 1st call (scan) matches → SIGTERM fires. The pre-SIGKILL re-check sees a
        # DIFFERENT start_time → the pid was recycled during the grace window.
        return "ST-1" if st_calls["n"] == 1 else "ST-RECYCLED"

    sigkilled: list[int] = []

    def fake_killpg(pgid, sig):
        if sig == signal.SIGKILL:
            sigkilled.append(pgid)
        # SIGTERM is ignored: the (now-recycled) pid stays alive.

    with (
        patch.object(backend_mod, "_proc_start_time", side_effect=start_time),
        patch.object(backend_mod.os, "kill", return_value=None),  # always alive
        patch.object(backend_mod.os, "getpgid", return_value=4321),
        patch.object(backend_mod.os, "killpg", side_effect=fake_killpg),
        patch.object(backend_mod, "sel"),
        patch.object(backend_mod, "_REAP_SIGTERM_GRACE", 0.02),
        patch.object(backend_mod, "_REAP_POLL_INTERVAL", 0.01),
    ):
        n = backend_mod._reap_stale_app_backends()
    assert n == 1  # the SIGTERM was sent, so it counts as reaped
    assert sigkilled == []  # but the recycled pid must NOT be SIGKILLed
