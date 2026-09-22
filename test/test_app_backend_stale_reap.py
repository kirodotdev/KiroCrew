"""Tests for the app-backend startup stale-reap (pidfile + PID-reuse-safe kill).

These pin the safety contract of ``_reap_stale_app_backends``: it terminates a
recorded app-backend pid ONLY when the pid is alive AND its start_time POSITIVELY
matches what was recorded at spawn (so a recycled pid, an unreadable start_time,
or a pid owned by another uid is never killed). Entries it cannot confirm-and-kill
but that are still alive are KEPT for a later attempt; handled entries are dropped.
"""
from __future__ import annotations

import signal
from unittest.mock import MagicMock, patch

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


def _model_a_host_without_ps(monkeypatch, *, identity):
    """Model a platform whose start-time probe is NOT `/proc` and NOT `ps`.

    That is precisely Windows: `sys.platform` is not "linux", and there is no
    standard `ps` for the fallback to reach. `platform_compat` is the layer that
    can still answer there (process creation FILETIME through a query-only
    handle) — its real per-platform behaviour is pinned in test_platform_compat.
    """
    monkeypatch.setattr(backend_mod.sys, "platform", "win32")

    def _no_ps(*_a, **_k):
        raise FileNotFoundError(2, "No such file or directory", "ps")

    monkeypatch.setattr(backend_mod.subprocess, "check_output", _no_ps)
    monkeypatch.setattr(
        backend_mod.platform_compat, "process_start_time", lambda _pid: identity
    )


def test_a_host_without_ps_still_records_a_reapable_identity(pidfile, monkeypatch):
    """The stale-reap must not be structurally dead on a `ps`-less platform.

    `_record_app_pid` stores whatever the start-time probe returns, and the reap
    refuses to signal when the recorded identity is falsy ("identity unconfirmed
    -> do not kill", and the entry is KEPT). A probe that can only answer via
    `/proc` or `ps` therefore records None on Windows, and every stale backend
    there survives forever while its pidfile entry accumulates — the reap fails
    safe into never reaping at all.
    """
    _model_a_host_without_ps(monkeypatch, identity="WIN-CREATION-8817")

    backend_mod._record_app_pid("code_reviewer", 4321, 9100)

    entry = backend_mod._read_pidfile()["code_reviewer"]
    assert entry["start_time"] == "WIN-CREATION-8817", (
        "no start-time identity was recorded on a host without /proc or ps, so "
        "the stale-reap can never positively identify this backend")

    # The consequence: with an identity on file, the reap can now confirm and act
    # -- and the recorded identity is what the terminate is PINNED to, so the
    # chain from probe to kill carries one value end to end.
    killed: list[tuple[int, str, int]] = []
    monkeypatch.setattr(
        backend_mod.platform_compat, "pid_liveness",
        lambda _pid: backend_mod.platform_compat.PID_ALIVE,
    )
    monkeypatch.setattr(
        backend_mod.platform_compat,
        "kill_process_tree_pinned",
        lambda pid, expected, sig, **kwargs: bool(killed.append((pid, expected, sig))) or True,
    )
    monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(backend_mod, "sel", lambda: None)

    assert backend_mod._reap_stale_app_backends() == 1
    assert killed and killed[0][:2] == (4321, "WIN-CREATION-8817")
    assert backend_mod._read_pidfile() == {}, "the handled entry was not cleared"


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


def test_reap_keeps_the_entry_when_the_kill_identity_cannot_be_pinned(pidfile):
    """The reap goes through the identity-PINNED terminate, and honours its refusal.

    ``kill_process_tree_pinned`` returns False when the process cannot be opened
    or its identity does not match, which on Windows is the pid having been
    recycled between the start-time check and the signal. That must behave like
    every other unconfirmed-identity case here: no kill, and the entry is KEPT so
    a later start can retry -- leak-not-mis-kill.

    Liveness is stubbed at ``platform_compat.pid_liveness`` rather than at
    ``os.kill`` so the case is about the pin and runs identically on every host;
    the Windows liveness probe is an ``OpenProcess``, not an ``os.kill``.
    """
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(
            backend_mod.platform_compat,
            "pid_liveness",
            return_value=backend_mod.platform_compat.PID_ALIVE,
        ),
        patch.object(
            backend_mod.platform_compat, "kill_process_tree_pinned", return_value=False
        ) as mock_pinned,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()

    assert n == 0
    mock_pinned.assert_called_once()
    assert "app" in backend_mod._read_pidfile(), "an unpinnable entry must be retried"


def test_reap_hands_the_recorded_identity_to_the_pinned_kill(pidfile):
    """The pin must re-verify against the RECORDED baseline, not a fresh read.

    Re-reading inside the pin would compare the process against itself and
    confirm any pid, which is the check the window exists to survive.
    """
    backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
    alive = {"v": True}

    def fake_pinned(pid, expected, sig, **kwargs):
        alive["v"] = False  # the terminate took effect
        return True

    with (
        patch.object(backend_mod, "_proc_start_time", return_value="ST-1"),
        patch.object(
            backend_mod.platform_compat,
            "pid_liveness",
            return_value=backend_mod.platform_compat.PID_ALIVE,
        ),
        patch.object(backend_mod, "_pid_alive", side_effect=lambda pid: alive["v"]),
        patch.object(
            backend_mod.platform_compat,
            "kill_process_tree_pinned",
            side_effect=fake_pinned,
        ) as mock_pinned,
        patch.object(backend_mod, "sel"),
    ):
        n = backend_mod._reap_stale_app_backends()

    assert n == 1
    assert mock_pinned.call_args.args[:2] == (4321, "ST-1")
    assert backend_mod._read_pidfile() == {}


class TestDeadLeaderOrphanedGroup:
    """The leak behind the reported 502.

    A backend is spawned with ``start_new_session=True``, so it leads its own
    process group and the group OUTLIVES it. When the gateway is SIGKILLed the
    leader can exit while a worker child keeps the app's port bound. The reap used
    to read ``PID_DEAD``, drop the pidfile row, and stop -- so the survivor stayed,
    the next generation spawned onto the port it still owned, and the route
    answered 502. These pin that a dead leader now costs its group a signal, and
    that the signal is never aimed at the bare group number.
    """

    @staticmethod
    def _dead_leader(monkeypatch):
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "pid_liveness",
            lambda _pid: backend_mod.platform_compat.PID_DEAD,
        )
        monkeypatch.setattr(backend_mod, "sel", lambda: None)

    def test_a_dead_leader_still_costs_its_group_a_signal(self, pidfile, monkeypatch):
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        calls: list[tuple[int, int, str, object]] = []

        def _signal(pgid, sig, instance, *, expected=None):
            calls.append((pgid, sig, instance, expected))
            members = {555: "s555"}
            return (members, {}) if sig == backend_mod.platform_compat.SIGKILL else (members, members)

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        # The member ignores SIGTERM, so the escalation must be reached. It stays
        # alive after the SIGKILL pass too, which is what keeps the row (below).
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        # The return value stays "leaders terminated": there was no live leader.
        assert backend_mod._reap_stale_app_backends() == 0

        assert [(c[0], c[1]) for c in calls] == [
            (4321, backend_mod.platform_compat.SIGTERM),
            (4321, backend_mod.platform_compat.SIGKILL),
        ], "the group is resolved from the session-leader contract (pgid == leader pid)"
        assert all(c[2] == "inst-a" for c in calls), "every pass is pinned to this spawn"
        assert calls[0][3] is None
        assert calls[1][3] == {555: "s555"}, (
            "the escalation must target the members the FIRST pass vouched, not a "
            "fresh census -- a member seen only now owes no grace and is what a "
            "fresh occupant of the recycled group number looks like"
        )
        assert "app" in backend_mod._read_pidfile(), (
            "a member outlived the SIGKILL pass, so the row -- this orphan's only "
            "handle -- must be KEPT for a later start to retry"
        )

    def test_a_group_whose_members_exit_on_sigterm_reaches_no_member_with_sigkill(
        self, pidfile, monkeypatch
    ):
        """A member that exited owes no SIGKILL.

        The escalation call still happens — it doubles as the final census — but its
        target set is the members that took a SIGTERM, so a group that already exited
        is signalled by nothing.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        calls: list[tuple[int, object]] = []

        def _signal(pgid, sig, inst, *, expected=None):
            calls.append((sig, expected))
            if sig == backend_mod.platform_compat.SIGTERM:
                return {555: "s555"}, {555: "s555"}
            # Real code filters ``expected`` down to members still alive under the
            # same start id; every one of them exited, so nothing is signalled.
            return {}, {}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: False)

        backend_mod._reap_stale_app_backends()

        assert [c[0] for c in calls] == [
            backend_mod.platform_compat.SIGTERM,
            backend_mod.platform_compat.SIGKILL,
        ]
        assert calls[1][1] == {555: "s555"}, "the escalation is scoped to the signalled members"
        assert backend_mod._read_pidfile() == {}, (
            "the final census found nothing live, so nothing is left for the row to recover"
        )

    def test_a_row_is_kept_when_the_reap_itself_fails(self, pidfile, monkeypatch):
        """A transient failure must not discard the orphan's only record.

        Dropping the row on a /proc scan that raised, or a signal the kernel
        refused, would strand the port holder with nothing naming it — no later
        start could ever retry, and the periodic sweep does not cover an app
        backend's worker.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)

        def _boom(*_a, **_k):
            raise OSError("group listing failed")

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _boom)

        assert backend_mod._reap_stale_app_backends() == 0
        assert "app" in backend_mod._read_pidfile(), "a retryable failure must keep the row"

    def test_a_row_is_kept_when_the_escalation_fails(self, pidfile, monkeypatch):
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)

        def _signal(pgid, sig, instance, *, expected=None):
            if sig == backend_mod.platform_compat.SIGKILL:
                raise OSError("signal refused")
            return {555: "s555"}, {555: "s555"}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        backend_mod._reap_stale_app_backends()

        assert "app" in backend_mod._read_pidfile(), "a refused SIGKILL must keep the row"

    def test_a_member_the_signal_could_not_reach_still_keeps_the_row(
        self, pidfile, monkeypatch
    ):
        """Retention must read the CENSUS, not the subset a signal reached.

        A group can hold a member A the signal reaches and a member B it cannot
        (``pidfd_open`` answering EMFILE, or EPERM). Measuring liveness over the
        signalled set alone reports the group gone the moment A dies -- and drops the
        row that names B, which is still alive and still holding the app's port.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        # A (=111) was signalled; B (=222) was vouched but unreachable.
        monkeypatch.setattr(
            backend_mod,
            "signal_orphaned_spawn_group",
            lambda pgid, sig, inst, **_k: ({111: "s111", 222: "s222"}, {111: "s111"}),
        )
        # A died on SIGTERM; B is still alive.
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda pid: pid == 222)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        assert backend_mod._reap_stale_app_backends() == 0

        assert "app" in backend_mod._read_pidfile(), (
            "a live vouched member the signal never reached must KEEP the row, even "
            "though every SIGNALLED member is gone"
        )

    def test_a_replacement_forked_on_sigterm_keeps_the_row(self, pidfile, monkeypatch):
        """Retention must read the group at DECISION time, not the opening snapshot.

        A supervisor/worker backend can fork a replacement from its SIGTERM handler. That
        child inherits the session group and the instance token, so it is a vouched
        member — but it did not exist when the first census ran. Deciding on that
        snapshot drops the row once the originally-censused members die, leaving the
        replacement holding the app's port with nothing left naming it.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        # First census sees only 111. It takes the SIGTERM, forks 999 and exits, so
        # the FINAL census sees 999 — which the opening snapshot could not contain.
        state = {"forked": False}

        def _signal(pgid, sig, inst, *, expected=None):
            if sig == backend_mod.platform_compat.SIGTERM:
                state["forked"] = True
                return {111: "s111"}, {111: "s111"}
            return ({999: "s999"} if state["forked"] else {}), {}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        # 111 died on the SIGTERM; the forked 999 is alive.
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda pid: pid == 999)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        assert backend_mod._reap_stale_app_backends() == 0

        assert "app" in backend_mod._read_pidfile(), (
            "a member forked after the opening census is still a live port holder, so "
            "the row must be KEPT for a later start"
        )

    def test_the_final_scan_runs_even_when_nothing_needs_killing(self, pidfile, monkeypatch):
        """The kill pass is also the fresh reading, so it is never skipped.

        Gating it on a surviving signalled member is what would leave retention with
        only the stale snapshot to go on.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        sigs: list[int] = []

        def _signal(pgid, sig, inst, *, expected=None):
            sigs.append(sig)
            return {111: "s111"}, ({111: "s111"} if sig == backend_mod.platform_compat.SIGTERM else {})

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        # Every signalled member exited during the grace: nothing to escalate.
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: False)

        backend_mod._reap_stale_app_backends()

        assert sigs == [
            backend_mod.platform_compat.SIGTERM,
            backend_mod.platform_compat.SIGKILL,
        ], "the second call is the final census and must happen regardless"

    def test_the_escalation_targets_only_the_signalled_subset(self, pidfile, monkeypatch):
        """A member that never took a SIGTERM owes no grace and no SIGKILL."""
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        expected_seen: list[object] = []

        def _signal(pgid, sig, inst, *, expected=None):
            if sig == backend_mod.platform_compat.SIGKILL:
                expected_seen.append(expected)
                return {111: "s111"}, {111: "s111"}
            return {111: "s111", 222: "s222"}, {111: "s111"}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        backend_mod._reap_stale_app_backends()

        assert expected_seen == [{111: "s111"}], (
            "the escalation is owed only to the members a SIGTERM actually reached"
        )

    def test_a_group_whose_signals_are_all_refused_keeps_its_row(self, pidfile, monkeypatch):
        """The regression the signalled-count could not see.

        The members are ALIVE and the kernel refused every signal (EPERM, or no
        pidfd to pin the identity with), so nothing was signalled. Reading that as
        "the group is gone" discards the only record naming a process that still
        holds the app's port — and no later start could find it again, since the
        periodic sweep does not cover an app backend's worker. The vouch census is
        what tells this apart from a genuinely empty group.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        # Vouched members exist and are ALIVE; none of them could be signalled.
        monkeypatch.setattr(
            backend_mod,
            "signal_orphaned_spawn_group",
            lambda pgid, sig, inst, **_k: ({555: "s555"}, {}),
        )
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda pid: pid == 555)
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        assert backend_mod._reap_stale_app_backends() == 0

        assert "app" in backend_mod._read_pidfile(), (
            "live vouched members that refused the signal must KEEP the row"
        )

    def test_a_vouched_member_that_has_since_exited_drops_its_row(self, pidfile, monkeypatch):
        """Retention needs a LIVE member, not merely a member in the census.

        The census is a snapshot; if the member it named has exited by the time
        retention is decided, there is nothing left for the row to recover.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            backend_mod,
            "signal_orphaned_spawn_group",
            lambda pgid, sig, inst, **_k: ({555: "s555"}, {555: "s555"}),
        )
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: False)

        assert backend_mod._reap_stale_app_backends() == 0
        assert backend_mod._read_pidfile() == {}

    def test_an_empty_census_drops_the_row_only_once_the_group_is_gone(
        self, pidfile, monkeypatch
    ):
        """An empty census is not evidence of an empty group.

        Every read the vouch makes is fail-OPEN — the /proc scan, each stat and each
        environ read all swallow OSError — so fd exhaustion produces an empty census
        without raising. Dropping on that would discard the orphan's only handle
        exactly when the host is under pressure, so absence has to be confirmed by a
        probe that cannot fail open.
        """
        row = {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            backend_mod, "signal_orphaned_spawn_group", lambda pgid, sig, inst, **_k: ({}, {})
        )

        # Census empty AND the group is positively gone: nothing to recover.
        backend_mod._write_pidfile({"app": dict(row)})
        monkeypatch.setattr(backend_mod.platform_compat, "pgroup_exists", lambda _pgid: False)
        assert backend_mod._reap_stale_app_backends() == 0
        assert backend_mod._read_pidfile() == {}

        # Census empty but the group still EXISTS (unreadable, or unsignalable):
        # something is in it, so the handle must survive.
        backend_mod._write_pidfile({"app": dict(row)})
        monkeypatch.setattr(backend_mod.platform_compat, "pgroup_exists", lambda _pgid: True)
        assert backend_mod._reap_stale_app_backends() == 0
        assert "app" in backend_mod._read_pidfile(), (
            "an unreadable census must not be read as an empty group"
        )

    def test_a_vouched_group_that_dies_under_sigkill_drops_its_row(self, pidfile, monkeypatch):
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        alive = {"v": True}

        def _signal(pgid, sig, instance, *, expected=None):
            if sig == backend_mod.platform_compat.SIGKILL:
                alive["v"] = False  # the kill landed
            return {555: "s555"}, {555: "s555"}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)
        monkeypatch.setattr(backend_mod, "_pid_alive", lambda _pid: alive["v"])
        monkeypatch.setattr(backend_mod, "_REAP_SIGTERM_GRACE", 0.0)

        backend_mod._reap_stale_app_backends()

        assert backend_mod._read_pidfile() == {}, (
            "the group is gone, so the row has nothing left to recover"
        )

    def test_a_row_without_an_instance_token_is_never_signalled(self, pidfile, monkeypatch):
        """No incarnation pin, no authority.

        A row written by a build that did not stamp the token leaves nothing to
        vouch the group with, and the group NUMBER is the dead leader's pid --
        which the kernel may have reissued to an unrelated session leader. Leaking
        is the correct trade; ``killpg`` on that number would take a stranger's
        tree. The row IS dropped here: a token is never added to an existing row,
        so no later start could do better and keeping it would grow the pidfile
        forever.
        """
        backend_mod._write_pidfile({"app": {"pid": 4321, "start_time": "ST-1", "port": 9100}})
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        signal_group = MagicMock()
        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", signal_group)

        assert backend_mod._reap_stale_app_backends() == 0

        signal_group.assert_not_called()
        assert backend_mod._read_pidfile() == {}, "no later start could act on this row"

    def test_a_host_that_cannot_vouch_signals_nothing(self, pidfile, monkeypatch):
        """The vouch reads /proc/<pid>/environ, which is Linux-only.

        The row is dropped rather than kept: the platform is the same on the next
        start, so no retry could do better and an accumulating pidfile would be the
        only result. The decline log line is the record.
        """
        backend_mod._write_pidfile(
            {"app": {"pid": 4321, "start_time": "ST-1", "port": 9100, "spawn_instance": "inst-a"}}
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: False)
        signal_group = MagicMock()
        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", signal_group)

        assert backend_mod._reap_stale_app_backends() == 0

        signal_group.assert_not_called()
        assert backend_mod._read_pidfile() == {}

    def test_one_app_failing_does_not_end_the_sweep(self, pidfile, monkeypatch):
        backend_mod._write_pidfile(
            {
                "a": {"pid": 11, "start_time": "S", "port": 1, "spawn_instance": "i1"},
                "b": {"pid": 22, "start_time": "S", "port": 2, "spawn_instance": "i2"},
            }
        )
        self._dead_leader(monkeypatch)
        monkeypatch.setattr(backend_mod, "group_vouching_available", lambda: True)
        seen: list[int] = []

        def _signal(pgid, sig, inst, **_k):
            seen.append(pgid)
            if pgid == 11:
                raise OSError("group listing failed")
            return {}, {}

        monkeypatch.setattr(backend_mod, "signal_orphaned_spawn_group", _signal)

        backend_mod._reap_stale_app_backends()

        assert seen == [11, 22], "a failed reap must not abort the remaining apps"

    def test_spawn_instance_is_recorded_so_the_group_can_be_vouched_later(self, pidfile):
        """The reap can only vouch a group the spawn stamped an instance on."""
        with patch.object(backend_mod, "_proc_start_time", return_value="ST-1"):
            backend_mod._record_app_pid("app", 4321, 9100, "inst-a")
        assert backend_mod._read_pidfile()["app"] == {
            "pid": 4321,
            "start_time": "ST-1",
            "port": 9100,
            "spawn_instance": "inst-a",
        }
