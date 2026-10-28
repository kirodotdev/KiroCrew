"""Adoption-provenance, quarantine and restart-row coverage for :mod:`kiro_crew.apps.backend`.

Complements ``test_app_backend.py`` (port allocation, spawn survival, dispatch)
and ``test_app_backend_stale_reap.py`` (pidfile reap safety) by exercising the
branches those files leave untouched. This module covers starting a backend:

* which functions may mutate the process table, the port probes and PID ancestry,
* which PIDs may be attributed to an app (only a live child of this gateway),
* ``start_app_backend``'s coordination and who publishes a spawned process,
* the spawn body's port resolution and the adopt-an-already-healthy-instance path.

The rest lives in siblings named ``test_apps_backend_coverage_<topic>.py``:
``_dependency_install``, ``_dispatch``, ``_stop``, ``_supervision`` and ``_boot``.
The doubles and fixtures the family shares live in ``apps_backend_coverage_helpers.py``.

Everything here is hermetic and order-independent: no real process is spawned,
no socket is bound, no network request is made, and no wall-clock duration is
asserted. ``subprocess.Popen`` / ``subprocess.run``, the ``socket`` module,
and ``loopback_urlopen`` are stubbed, and the spawn body is frozen at the
``Popen`` seam with a sentinel exception.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from apps_backend_coverage_helpers import _isolated_module_state  # noqa: F401
from apps_backend_coverage_helpers import spawn_root_fixture  # noqa: F401
from apps_backend_coverage_helpers import (
    _capture_popen,
    _fake_proc,
    _FakeResp,
    _install_fake_socket,
    _manifest,
    _StopSpawn,
    _stub_listeners,
)

import kiro_crew.apps.backend as bmod
from kiro_crew.apps.backend import AppProcess


class TestAdoptionProvenance:
    """One verdict per identity case the adoption branch can meet.

    A health answer on the manifest's declared port names no code and no
    process, so on its own it lets a backend that outlived its app's uninstall
    be adopted by the next install that happens to use the same app name. The
    gateway adopts only what it can attribute to the spawn it recorded for that
    app, by the recorded leader's identity or by the spawn instance its whole
    tree carries, and refuses everything else.
    """

    @staticmethod
    def _row(**over: Any) -> dict[str, Any]:
        entry = {"pid": 4242, "start_time": "st-4242", "port": 9000, "spawn_instance": "sp-1"}
        entry.update(over)
        return entry

    def _verdict(self, owners: list[int] | None = None, app: str = "app") -> tuple[bool, str]:
        return bmod._adoption_provenance(app, [111, 222] if owners is None else owners)

    def test_the_recorded_leader_still_holding_the_port_is_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pid plus a start instant names one process for good, on every platform."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111")})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        # No instance route is needed, so this host need not be able to vouch at all.
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        attributed, reason = self._verdict([111])
        assert attributed is True
        assert "every owner [111]" in reason

    def test_a_recycled_leader_pid_is_not_ours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The recorded number came back as an unrelated process: the start instant differs."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111", spawn_instance=None)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-recycled")
        attributed, reason = self._verdict()
        assert attributed is False
        assert "no spawn instance" in reason

    def test_a_revoked_row_is_refused_even_when_the_owner_matches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An update/uninstall that could not confirm the row's deletion stamps it
        ``revoked``. The surviving row still names the recorded leader AND the spawn
        instance a detached survivor inherited, so without this gate the leader route
        (or the tree route) would adopt an OLD-code listener after a restart. A
        revoked row attributes nothing, regardless of how well an owner matches."""

        bmod._write_pidfile({"app": self._row(pid=111, start_time="st-111", revoked=True)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-1")
        attributed, reason = self._verdict([111])
        assert attributed is False
        assert "revoked" in reason

    def test_every_member_of_the_recorded_spawn_is_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leader has exited; pre-fork workers of the same spawn hold the port.

        The whole tree inherits the spawn instance, so the token attributes members
        whose leader's pid names nothing live.
        """

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-1")
        attributed, reason = self._verdict()
        assert attributed is True
        assert "every owner [111, 222]" in reason

    def test_one_foreign_co_owner_refuses_the_whole_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A same-UID SO_REUSEPORT co-binder shares the winning dispatch tier.

        Every captured owner enters the managed set and is signalled at stop, and
        the start-time token stop re-checks was captured at the same moment, so it
        confirms the co-binder rather than excluding it. Attributing the set from
        one match would hand stop an unrelated process to terminate.
        """

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(
            bmod, "_env_spawn_instance", lambda pid: "sp-1" if pid == 111 else "sp-stranger"
        )
        attributed, reason = self._verdict()
        assert attributed is False
        assert "[222]" in reason
        assert "not placed by this gateway for this app" in reason

    def test_an_empty_owner_set_is_not_ours(self) -> None:
        bmod._write_pidfile({"app": self._row()})
        attributed, reason = self._verdict([])
        assert attributed is False
        assert reason == "no owning pid to attribute"

    def test_a_survivor_of_a_prior_install_of_the_same_name_is_not_ours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The defect this guard exists for.

        The listener is a real Kiro Crew app backend and it answers on the port
        the manifest declares, but it belongs to a spawn of the app that used
        this name before: its instance is a different one.
        """

        bmod._write_pidfile({"app": self._row(spawn_instance="sp-current")})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        monkeypatch.setattr(bmod, "_env_spawn_instance", lambda _pid: "sp-previous-install")
        attributed, reason = self._verdict()
        assert attributed is False
        assert "not placed by this gateway for this app" in reason

    def test_no_recorded_spawn_is_not_ours(self) -> None:
        """Nothing was recorded, so nothing attributes the listener.

        This is the state an uninstall leaves behind: stopping the backend drops
        the app's row, so a survivor that rebinds the port meets an empty record
        on the next install's first start.
        """

        bmod._write_pidfile({"other-app": self._row()})
        attributed, reason = self._verdict()
        assert attributed is False
        assert reason == "no spawn recorded for this app"

    def test_an_unreadable_record_is_not_ours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A record that cannot be read decides nothing, so it decides against adoption."""

        def _boom() -> dict[str, Any]:
            raise OSError("pidfile unreadable")

        monkeypatch.setattr(bmod, "_read_pidfile", _boom)
        attributed, reason = self._verdict()
        assert attributed is False
        assert "unreadable" in reason

    def test_a_malformed_row_is_not_ours(self) -> None:
        bmod._write_pidfile({"app": "not-a-mapping"})
        attributed, reason = self._verdict()
        assert attributed is False
        assert reason == "no spawn recorded for this app"

    def test_a_row_with_no_instance_cannot_vouch_a_tree_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual, pinned at its current answer: a spawn recorded without a
        token has nothing to attribute a surviving member with, so the member is
        refused. Self-healing forward -- the next spawn records a token."""

        bmod._write_pidfile({"app": self._row(spawn_instance=None)})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: True)
        attributed, reason = self._verdict()
        assert attributed is False
        assert "no spawn instance to vouch its tree with" in reason

    def test_a_host_that_cannot_read_an_instance_refuses_a_tree_member(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual, pinned at its current answer: the token read is
        ``/proc/<pid>/environ``, which exists on Linux alone. Elsewhere a
        surviving member whose leader is gone cannot be attributed, and the
        reason says so rather than reading as "not ours"."""

        bmod._write_pidfile({"app": self._row()})
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: None)
        monkeypatch.setattr(bmod, "group_vouching_available", lambda: False)
        monkeypatch.setattr(
            bmod,
            "_env_spawn_instance",
            lambda _pid: pytest.fail("read on a host with no oracle"),
        )
        attributed, reason = self._verdict()
        assert attributed is False
        assert "cannot read a process's spawn instance" in reason


class TestAdoptionProvenanceAtTheSpawnBody:
    @pytest.fixture()
    def occupied(self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
        (spawn_root / "server.py").write_text("x = 1\n")
        _install_fake_socket(monkeypatch, connect_exc=None)
        monkeypatch.setattr(
            bmod, "popen_limited", lambda *_a, **_k: pytest.fail("spawned onto a taken port")
        )
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        monkeypatch.setattr(bmod, "loopback_urlopen", lambda *_a, **_k: _FakeResp(200))
        monkeypatch.setattr(bmod, "_proc_start_time", lambda pid: f"st-{pid}")
        _stub_listeners(monkeypatch, [bmod.platform_compat.PortListener(555, "127.0.0.1", "4")])
        return spawn_root

    def _run(self, port: int) -> Any:
        return bmod._start_app_backend_body("adoptee", _manifest("server.py", port=str(port)))

    def test_an_unattributed_healthy_listener_is_not_adopted(
        self, occupied: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        """End to end: healthy, on the declared port, and still refused.

        No record names it, so no process table entry is created -- the app does
        not start on a listener the gateway does not own. The port claim taken
        before the branch is released by the caller's spawn bookkeeping, as it is
        on every other refusal here.
        """

        with caplog.at_level(logging.WARNING):
            refused = self._run(bmod._MIN_PORT + 21)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert any("refusing to adopt" in r.message for r in caplog.records)
        assert "adoptee" not in bmod._processes

    def test_the_refusal_is_audited_as_unattributed(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit trail records the verdict reached, not the verdict attempted."""

        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        assert isinstance(self._run(bmod._MIN_PORT + 22), bmod.AdoptionRefused)
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert "refused_unattributed" in outcomes
        assert "adopted" not in outcomes

    def test_an_attributed_listener_is_adopted_and_audited(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bmod._write_pidfile(
            {"adoptee": {"pid": 555, "start_time": "st-555", "port": 0, "spawn_instance": "sp-a"}}
        )
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        ap = self._run(bmod._MIN_PORT + 23)
        assert ap is not None
        assert ap.adopted_pids == [555]
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert "adopted" in outcomes
        assert "refused_unattributed" not in outcomes

    def test_the_refusal_returns_a_reason_naming_the_port(self, occupied: Any) -> None:
        """The body hands its refusal back instead of a bare None, so the caller
        has something to show the user: the port, and the step to take."""

        port = bmod._MIN_PORT + 25
        refused = self._run(port)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert refused.port == port
        assert refused.reason
        assert f"port {port}" in refused.notice
        assert f"Close the program using port {port}" in refused.notice

    def test_a_quarantined_app_is_refused_as_quarantined(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even a listener the record attributes is refused while the app is
        quarantined: it may be the old-code survivor an update replaced."""

        bmod._write_pidfile(
            {"adoptee": {"pid": 555, "start_time": "st-555", "port": 0, "spawn_instance": "sp-a"}}
        )
        monkeypatch.setattr(bmod, "is_backend_quarantined", lambda _name: True)
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        port = bmod._MIN_PORT + 26
        refused = self._run(port)
        assert isinstance(refused, bmod.AdoptionRefused)
        assert refused.reason == "quarantined"
        assert f"port {port}" in refused.notice
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert outcomes == ["refused_quarantined"]
        assert "adoptee" not in bmod._processes

    def test_the_refusal_reaches_the_caller_as_a_backend_notice(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Through the public entry point the refusal is still None to every
        existing caller, and its notice is what the app's responses carry."""

        port = bmod._MIN_PORT + 27
        monkeypatch.setattr(
            bmod, "get_app_manifest", lambda _name: _manifest("server.py", port=str(port))
        )
        bmod.clear_backend_notice("adoptee")
        try:
            assert bmod.start_app_backend("adoptee") is None
            notice = bmod.backend_notice("adoptee")
            assert notice is not None
            assert f"port {port}" in notice
            assert "adoptee" not in bmod._processes
        finally:
            bmod.clear_backend_notice("adoptee")

    def test_a_successful_start_after_a_refusal_clears_the_notice(
        self, occupied: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A notice describes the LAST attempt only, so a later successful start clears it.

        The first start is refused (an unattributed listener on the port) and records a
        backend notice. The next start succeeds — ``_start_app_backend`` clears the
        notice at the top of the attempt, before running the body — so once a start
        publishes a process the stale refusal notice is gone, and the app's status does
        not tell the user to close a program that has already cleared.

        Replacing the ``clear_backend_notice`` call in ``_start_app_backend`` with
        ``pass`` leaves the refusal notice standing after the successful start, turning
        this test red.
        """
        port = bmod._MIN_PORT + 28
        monkeypatch.setattr(
            bmod, "get_app_manifest", lambda _name: _manifest("server.py", port=str(port))
        )
        bmod.clear_backend_notice("adoptee")
        try:
            # First attempt: refused by the occupied, unattributed port -> notice recorded.
            assert bmod.start_app_backend("adoptee") is None
            assert bmod.backend_notice("adoptee") is not None

            # Second attempt succeeds: the body publishes a healthy process.
            published = AppProcess(app_name="adoptee", port=port, proc=_fake_proc(pid=733))

            def _body(name: str, _manifest_arg: Any) -> AppProcess:
                with bmod._lock:
                    bmod._processes[name] = published
                return published

            monkeypatch.setattr(bmod, "_start_app_backend_body", _body)
            assert bmod.start_app_backend("adoptee") is published
            # The refusal notice is cleared by the successful attempt.
            assert bmod.backend_notice("adoptee") is None
        finally:
            bmod.clear_backend_notice("adoptee")
            with bmod._lock:
                bmod._processes.pop("adoptee", None)

    def test_a_free_port_never_consults_provenance(
        self, spawn_root: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is listening, so there is no listener to attribute and the
        ordinary spawn path is unchanged."""

        (spawn_root / "server.py").write_text("x = 1\n")
        _capture_popen(monkeypatch)
        monkeypatch.setattr(
            bmod,
            "_adoption_provenance",
            lambda *_a, **_k: pytest.fail("provenance consulted with no listener to judge"),
        )
        with pytest.raises(_StopSpawn):
            bmod._start_app_backend_body(
                "freeport", _manifest("server.py", port=str(bmod._MIN_PORT + 24))
            )


class TestRebindAttributesTheNewOwners:
    """Recovery re-captures owners, so it re-asks whose they are.

    This path runs after an adopted backend stops answering. The set it captures
    can be a different population from the one adoption attributed, so inheriting
    that verdict would let an unrelated listener answering the declared health path
    be written into the owner record and promoted -- the same shape a start refuses,
    reached through recovery.
    """

    @pytest.fixture()
    def adopted(self, monkeypatch: pytest.MonkeyPatch) -> AppProcess:
        ap = AppProcess(
            app_name="rebindee",
            port=bmod._MIN_PORT + 30,
            pid=0,
            proc=None,
            healthy=False,
            started_at=0.0,
            log_path="/dev/null",
            adopted_pids=[111],
            adopted_start_times={111: "st-111"},
            gateway_started=True,
            admitted_builtin=False,
        )
        with bmod._lock:
            bmod._processes["rebindee"] = ap
        monkeypatch.setattr(
            bmod, "_capture_adopted_owners", lambda *_a, **_k: ([777], {777: "st-777"})
        )
        return ap

    def test_an_unattributed_replacement_is_not_bound(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The recorded owners survive untouched, so stop still aims where it did."""

        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (False, "stranger"))
        with caplog.at_level(logging.WARNING):
            assert bmod._rebind_adopted_owners(adopted, "/health") is False
        assert any("refusing to re-bind" in r.message for r in caplog.records)
        assert adopted.adopted_pids == [111]
        assert adopted.adopted_start_times == {111: "st-111"}

    def test_the_refusal_is_audited(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (False, "stranger"))
        sink = MagicMock()
        monkeypatch.setattr(bmod, "sel", lambda: sink)
        assert bmod._rebind_adopted_owners(adopted, "/health") is False
        outcomes = [c.kwargs.get("outcome") for c in sink.log_api_access.call_args_list]
        assert outcomes == ["refused_unattributed"]

    def test_an_attributed_replacement_is_bound(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(bmod, "_adoption_provenance", lambda *_a: (True, "ours"))
        assert bmod._rebind_adopted_owners(adopted, "/health") is True
        assert adopted.adopted_pids == [777]
        assert adopted.adopted_start_times == {777: "st-777"}

    def test_the_captured_set_is_what_gets_judged(
        self, adopted: AppProcess, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not the set already on the record -- judging that would re-confirm the
        owners the adoption already cleared and never look at the newcomers."""

        seen: list[tuple[str, list[int]]] = []

        def _judge(app_name: str, owners: list[int]) -> tuple[bool, str]:
            seen.append((app_name, list(owners)))
            return True, "ours"

        monkeypatch.setattr(bmod, "_adoption_provenance", _judge)
        assert bmod._rebind_adopted_owners(adopted, "/health") is True
        assert seen == [("rebindee", [777])]


class TestARefusedStopRestoresTheProvenanceRecord:
    """The stop drops the app's pidfile row inside its lifecycle transition,
    before it signals anything, and it can then refuse and restore tracking.

    That row is where an adopted backend's provenance is read from, so a refusal
    that leaves it dropped makes the retry's re-bind refuse on "no spawn recorded"
    for good: a withdrawn trust ceiling could never converge on a listener that
    keeps serving, and across a restart the listener stops being managed at all.
    Whatever the transition removed is put back on every refusal.
    """

    @pytest.fixture(autouse=True)
    def _fast_wait(self, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
        _floor_monkeypatch.setattr(bmod, "_wait_for_pids", lambda _pids, timeout=2.0: None)

    ROW = {"pid": 4242, "start_time": "st-4242", "port": 9000, "spawn_instance": "sp-keep"}

    def _track(self, **kwargs: Any) -> AppProcess:
        ap = AppProcess(app_name="ext", port=bmod._MIN_PORT + 13, pid=0, proc=None, **kwargs)
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        return ap

    def test_a_refusal_with_no_recorded_pids_puts_the_row_back(self) -> None:
        self._track(healthy=True)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_a_refusal_whose_restore_write_fails_quarantines_the_backend(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: when the refusal's durable restore does NOT persist (ENOSPC/EDQUOT
        swallows the write), the row the retry reads after a gateway restart is gone
        and adoption provenance fails closed with no row -- so a backend under a
        withdrawn ceiling could keep serving un-trusted code with nothing left to stop
        it by. The refusal must then quarantine the name: the in-memory backstop
        refuses any later adoption of this generation even when its own marker write
        also fails, rather than silently stranding tracking.
        """
        self._track(healthy=True)
        bmod._quarantined_backends.discard("ext")
        # The restore does not persist (ENOSPC/EDQUOT swallowed the write), so the
        # durable row the retry reads after a restart is gone.
        monkeypatch.setattr(bmod, "_restore_app_pid", lambda *_a, **_k: False)
        try:
            assert bmod.stop_app_backend("ext") is False
            # The durable row did not land, but the fail-closed backstop holds.
            assert bmod.is_backend_quarantined("ext") is True
        finally:
            bmod._quarantined_backends.discard("ext")

    def test_a_refusal_on_a_replacement_still_serving_puts_the_row_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda *_a: True)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        monkeypatch.setattr(
            bmod, "_health_probe", lambda *_a, **_k: bmod.HealthProbeOutcome.answered(200)
        )
        assert bmod.stop_app_backend("ext", _retry_if_serving="/health") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_a_refusal_on_a_descendant_still_serving_puts_the_row_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exited-root descendant-serving refusal restores the row too.

        A launcher root exited while a forked worker still answers the declared
        port, and a withdrawn trust ceiling calls stop with ``_retry_if_serving``.
        The drain finds the survivor (``_drain_exited_root_tree`` -> False), the
        ceiling refuses -- and that refusal must restore the row it dropped, or the
        un-trusted listener it declined to stop becomes permanently unattributable:
        ``_adoption_provenance`` answers "no spawn recorded" and the reap has no
        pid/start_time to census it from.
        """

        proc = _fake_proc(pid=4242, returncode=-15)  # a spawned root that has exited
        ap = AppProcess(
            app_name="ext",
            port=bmod._MIN_PORT + 13,
            pid=4242,
            pid_start_time="st-4242",
            proc=proc,
            healthy=True,
        )
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        # The drain reports a survivor, so the ceiling refuses outright.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: False)
        assert bmod.stop_app_backend("ext", _retry_if_serving="/health") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_an_ordinary_stop_tolerating_a_survivor_keeps_the_recovery_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F1: an ORDINARY stop (no withdrawn ceiling, ``_retry_if_serving``
        is None) of a tracked root whose descendant outlived it REPORTS SUCCESS but
        must KEEP the pidfile row.

        A plain disable (or the stop half of a rollback-capable update) tolerates a
        surviving port-holder, but the row is the only handle a later re-enable has
        to re-attribute that listener. Dropping it here -- as the code did before
        this fix -- left the survivor permanently unattributable:
        ``_adoption_provenance`` fails closed with no spawn recorded and the reaper
        has no row to census it from, so the enabled app stays unreachable with no
        self-correcting path. In-memory tracking is still popped (the backend we
        spawned is gone); only the recovery row survives.
        """

        proc = _fake_proc(pid=4242, returncode=-15)  # a spawned root that has exited
        ap = AppProcess(
            app_name="ext",
            port=bmod._MIN_PORT + 13,
            pid=4242,
            pid_start_time="st-4242",
            proc=proc,
            healthy=True,
        )
        with bmod._lock:
            bmod._processes["ext"] = ap
            bmod._allocated_ports["ext"] = ap.port
        bmod._write_pidfile({"ext": dict(self.ROW)})
        # The drain reports a survivor; with NO _retry_if_serving the stop tolerates
        # it and returns success -- but must retain the recovery row.
        monkeypatch.setattr(bmod, "_drain_exited_root_tree", lambda *_a, **_k: False)
        assert bmod.stop_app_backend("ext") is True
        assert bmod._read_pidfile().get("ext") == self.ROW
        # In-memory tracking is gone -- only the recovery row persists.
        with bmod._lock:
            assert "ext" not in bmod._processes

    def test_a_successful_stop_leaves_the_row_dropped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The restore undoes a refusal, never a stop that worked -- a row that
        outlived its backend would attribute a later listener to a dead spawn."""

        self._track(adopted_pids=[111], adopted_start_times={111: "st-111"}, healthy=True)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "st-111")
        monkeypatch.setattr(bmod.platform_compat, "kill_pid_pinned", lambda *_a: True)
        monkeypatch.setattr(bmod.platform_compat, "pid_exists", lambda _pid: False)
        assert bmod.stop_app_backend("ext") is True
        assert "ext" not in bmod._read_pidfile()

    def test_a_newer_spawn_keeps_its_own_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A restore must not overwrite an identity recorded after the removal:
        that would aim the stale-reap and provenance at a process that is gone."""

        newer = {"pid": 5555, "start_time": "st-5555", "port": 9001, "spawn_instance": "sp-new"}
        self._track(healthy=True)
        real_forget = bmod._forget_app_pid

        def _forget_then_respawn(app_name: str) -> dict[str, Any] | None:
            removed = real_forget(app_name)
            bmod._write_pidfile({app_name: dict(newer)})
            return removed

        monkeypatch.setattr(bmod, "_forget_app_pid", _forget_then_respawn)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == newer

    def test_an_untracked_stop_leaves_the_recovery_row_intact(self) -> None:
        """A stop for an app this process never tracked (nothing in ``_processes``)
        must not discard the recorded row. That row is what a later start attributes
        an adopted survivor against; forgetting it here on a stop that knew nothing
        of the backend would strand the survivor, unadoptable."""

        bmod._write_pidfile({"ext": dict(self.ROW)})
        with bmod._lock:
            bmod._processes.pop("ext", None)
        assert bmod.stop_app_backend("ext") is False
        assert bmod._read_pidfile().get("ext") == self.ROW

    def test_read_pidfile_strict_raises_on_a_corrupt_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The strict reader surfaces a corrupt file as OSError (so the confirmed-revoke
        path aborts) while a MISSING file is still an empty dict — there is simply no
        row to read. The lenient _read_pidfile keeps masking both as {}."""

        pf = tmp_path / "app_backends.pids.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        # Missing file: both readers agree it is empty.
        assert bmod._read_pidfile_strict() == {}
        # Corrupt file: lenient returns {}, strict raises OSError.
        pf.write_text("{not json")
        assert bmod._read_pidfile() == {}
        with pytest.raises(OSError):
            bmod._read_pidfile_strict()

    def test_revoke_backend_provenance_stamps_confirms_and_hands_back_the_old_row(
        self,
    ) -> None:
        """GPT F2: the DURABLE pre-destruction guard. revoke stamps ``revoked`` on the
        row, confirms it by re-reading, and hands back the PRE-revoke row so an
        update's rollback can restore the un-revoked record. The live row is now
        revoked, so _adoption_provenance refuses it even after a restart (the stamp
        rides on disk, unlike the in-memory quarantine)."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        handed_back = bmod.revoke_backend_provenance("upd")
        assert handed_back == self.ROW, "the pre-revoke row is returned for rollback"
        live = bmod._read_pidfile().get("upd")
        assert live is not None and live.get("revoked") is True
        attributed, reason = bmod._adoption_provenance("upd", [self.ROW["pid"]])
        assert attributed is False and "revoked" in reason
        # Rollback restores the un-revoked row the caller held.
        with bmod._lock:
            bmod._processes.pop("upd", None)
        bmod._write_pidfile({})
        bmod._restore_app_pid("upd", handed_back)
        assert bmod._read_pidfile().get("upd") == self.ROW

    def test_revoke_backend_provenance_raises_when_the_stamp_cannot_be_written(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT F2 core: a swallowed write (ENOSPC/EDQUOT) must NOT be reported as a
        durable revoke. The in-memory quarantine does not survive a restart, so a
        caller that committed the destructive step on an unconfirmed revoke would let
        a same-name reinstall adopt an old-code survivor. revoke raises
        PidfileRevokeFailed so the caller REFUSES the destructive work."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        monkeypatch.setattr(bmod, "_write_pidfile", lambda data: False)
        with pytest.raises(bmod.PidfileRevokeFailed):
            bmod.revoke_backend_provenance("upd")

    def test_revoke_backend_provenance_raises_when_the_stamp_does_not_read_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that reports success but whose stamp does not re-read as revoked (a
        short/partial write) must still fail closed. revoke re-reads to confirm the
        ``revoked`` flag reached disk and raises PidfileRevokeFailed otherwise."""

        bmod._write_pidfile({"upd": dict(self.ROW)})
        # Write "succeeds" but the row that reads back is NOT revoked.
        monkeypatch.setattr(bmod, "_write_pidfile", lambda data: True)
        monkeypatch.setattr(bmod, "_read_pidfile_strict", lambda: {"upd": dict(self.ROW)})
        with pytest.raises(bmod.PidfileRevokeFailed):
            bmod.revoke_backend_provenance("upd")

    def test_revoke_backend_provenance_is_a_noop_for_an_absent_row(self) -> None:
        """No row vouches for nothing at revoke time — but leaving the name UNwritten
        is unsafe: a stop racing from another process can restore a row afterwards.
        So revoke writes a BARE revoked tombstone and returns None (nothing to restore
        on rollback). The tombstone makes a later restore a no-op and keeps adoption
        refused."""

        with bmod._lock:
            bmod._processes.pop("absent", None)
        bmod._write_pidfile({})
        assert bmod.revoke_backend_provenance("absent") is None
        live = bmod._read_pidfile().get("absent")
        assert live is not None and live.get("revoked") is True and live.get("tombstone") is True
        # A racing stop's restore of a (would-be) row cannot overwrite the tombstone.
        bmod._restore_app_pid(
            "absent", {"pid": 5, "start_time": "s", "port": 9, "spawn_instance": "i"}
        )
        after = bmod._read_pidfile().get("absent")
        assert (
            after is not None and after.get("revoked") is True
        ), "tombstone must survive a restore"
        attributed, reason = bmod._adoption_provenance("absent", [5])
        assert attributed is False and "revoked" in reason

    def test_unrevoke_deletes_a_bare_tombstone_on_rollback(self) -> None:
        """A failed uninstall that wrote a BARE tombstone (no row at revoke time) must
        delete it on rollback — there was no real row to restore, and leaving the
        tombstone would refuse the still-installed app's next spawn until it records
        its own row."""

        with bmod._lock:
            bmod._processes.pop("absent2", None)
        bmod._write_pidfile({})
        assert bmod.revoke_backend_provenance("absent2") is None
        assert bmod._read_pidfile().get("absent2", {}).get("tombstone") is True
        bmod._unrevoke_app_pid("absent2", None)
        assert (
            "absent2" not in bmod._read_pidfile()
        ), "the bare tombstone must be deleted on rollback"

    def test_unrevoke_restores_the_preserved_row_over_a_bare_tombstone(self) -> None:
        """A failed update whose stop forgot the tracked row before the revoke ran must
        still recover the survivor on rollback.

        The update's stop forgets the spawn row; the pre-replacement revoke then finds
        no row and writes a BARE pid-less tombstone (returning None). But the rollback
        holds the pre-stop row as ``preserved_row`` and passes it to _unrevoke_app_pid
        as a real ``original``. Because the tombstone has no pid/start_time/
        spawn_instance, the same-spawn check can never match it; without the bare-
        tombstone branch the recovery row would be lost and the still-installed app's
        surviving backend would be refused by adoption. The row must be put back."""

        with bmod._lock:
            bmod._processes.pop("upd", None)
        # Stop forgot the row, then the revoke stamped a bare tombstone over the gap.
        bmod._write_pidfile({"upd": {"revoked": True, "tombstone": True}})
        bmod._unrevoke_app_pid("upd", dict(self.ROW))
        restored = bmod._read_pidfile().get("upd")
        assert restored is not None, "the preserved row must be restored on rollback"
        assert restored.get("pid") == self.ROW["pid"]
        assert restored.get("spawn_instance") == self.ROW["spawn_instance"]
        assert not restored.get("revoked"), "the restored row must not carry the stamp"
        assert not restored.get("tombstone")
        # The restored row carries no stamp, so adoption does not refuse it for the
        # ``revoked`` reason.
        _, reason = bmod._adoption_provenance("upd", [self.ROW["pid"]])
        assert "revoked" not in reason

    def test_unrevoke_leaves_a_successors_real_row_over_a_tombstone_restore(self) -> None:
        """The bare-tombstone restore must never clobber a genuine successor. If a fresh
        spawn recorded its own real (non-tombstone) row between the revoke and the
        rollback, _unrevoke_app_pid leaves it untouched — a real spawn records a pid,
        never a tombstone, so it falls through neither the tombstone branch nor the
        same-spawn branch for a different ``original``."""

        successor = {"pid": 7777, "start_time": "st-7777", "port": 9001, "spawn_instance": "sp-new"}
        with bmod._lock:
            bmod._processes.pop("upd", None)
        bmod._write_pidfile({"upd": dict(successor)})
        bmod._unrevoke_app_pid("upd", dict(self.ROW))
        live = bmod._read_pidfile().get("upd")
        assert (
            live is not None and live.get("pid") == successor["pid"]
        ), "a successor's real spawn row must be left untouched"

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits")
    def test_the_pidfile_lock_sidecar_is_owner_only(self) -> None:
        """A fresh boot must not leave the interprocess lock sidecar readable by
        other users: it lives in the owner-only data home, so it is created 0o600,
        with no group or other bits set."""

        import stat

        prior_umask = os.umask(0o022)
        try:
            with bmod._pidfile_lock:
                lock = bmod._pidfile_lock._lock_path()
                assert lock.exists(), "the lock sidecar is created on acquisition"
                mode = stat.S_IMODE(lock.stat().st_mode)
                assert mode & 0o077 == 0, f"lock sidecar is group/other-accessible: {oct(mode)}"
        finally:
            os.umask(prior_umask)

    def test_revoke_backend_provenance_is_idempotent_on_an_already_revoked_row(
        self,
    ) -> None:
        """A retry after a landed-but-unconfirmed stamp must not fail: an already
        ``revoked`` row is the confirmed state we want, so revoke returns it."""

        bmod._write_pidfile({"upd": {**self.ROW, "revoked": True}})
        result = bmod.revoke_backend_provenance("upd")
        assert result is not None and result.get("revoked") is True

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX fcntl.flock probe")
    def test_the_pidfile_guard_takes_an_interprocess_flock(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """GPT F2: the pidfile read-modify-write is serialized across PROCESSES, not
        just threads. ``_pidfile_lock`` is now a composite guard that, inside its
        block, holds an exclusive advisory flock on a sidecar lockfile so the offline
        CLI uninstall cannot interleave its revoke with the gateway's spawn record.
        The flock is best-effort: it never weakens the in-process guarantee."""
        import os as _os

        monkeypatch.setattr(bmod, "config_dir", lambda: tmp_path)
        lock_path = tmp_path / "app_backends.pids.lock"
        with bmod._pidfile_lock:
            # The sidecar lockfile exists and this process holds an exclusive flock on
            # it: a second NON-BLOCKING exclusive acquire from another fd must fail.
            assert lock_path.is_file()
            import fcntl

            other = _os.open(str(lock_path), _os.O_RDWR)
            try:
                raised = False
                try:
                    fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(other, fcntl.LOCK_UN)
                except OSError:
                    raised = True
                assert raised, "the guard must hold the sidecar flock exclusively inside its block"
            finally:
                _os.close(other)
        # Released outside the block: a fresh acquire now succeeds.
        after = _os.open(str(lock_path), _os.O_RDWR)
        try:
            import fcntl

            fcntl.flock(after, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(after, fcntl.LOCK_UN)
        finally:
            _os.close(after)

    def test_restore_app_pid_never_overwrites_an_unreadable_pidfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Lenient-read ratchet: _restore_app_pid rewrites the WHOLE pidfile, so a
        transient read error (EACCES/EIO) must abort rather than publish an empty
        document over every recorded row. It reads through _read_pidfile_strict,
        which raises on a non-missing read error; the fail-closed except returns
        False and no write is attempted, so the rows on disk survive intact.
        Without the strict read (the lenient _read_pidfile masking the error as
        {}), the setdefault+write would wipe the other apps' spawn rows."""

        other = {"other": {"pid": 9, "start_time": "st-9", "port": 80, "spawn_instance": "sp-x"}}
        bmod._write_pidfile(dict(other))

        def _boom() -> dict[str, Any]:
            raise OSError("transient EACCES")

        wrote = False
        real_write = bmod._write_pidfile

        def _spy_write(data: dict[str, Any]) -> bool:
            nonlocal wrote
            wrote = True
            return real_write(data)

        monkeypatch.setattr(bmod, "_read_pidfile_strict", _boom)
        monkeypatch.setattr(bmod, "_write_pidfile", _spy_write)

        assert bmod._restore_app_pid("ext", dict(self.ROW)) is False, "fails closed on EACCES"
        assert wrote is False, "no write is attempted when the base read fails"
        assert bmod._read_pidfile() == other, "the recorded rows survive the failed read"

    def test_unrevoke_app_pid_never_overwrites_an_unreadable_pidfile(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same ratchet for the rollback path: _unrevoke_app_pid also rewrites the
        whole pidfile, so a transient read error must abort. It reads through
        _read_pidfile_strict; the raise lands in the fail-closed except (a no-op),
        so the recorded rows are never clobbered with emptiness."""

        other = {"other": {"pid": 9, "start_time": "st-9", "port": 80, "spawn_instance": "sp-x"}}
        bmod._write_pidfile(dict(other))

        def _boom() -> dict[str, Any]:
            raise OSError("transient EACCES")

        wrote = False
        real_write = bmod._write_pidfile

        def _spy_write(data: dict[str, Any]) -> bool:
            nonlocal wrote
            wrote = True
            return real_write(data)

        monkeypatch.setattr(bmod, "_read_pidfile_strict", _boom)
        monkeypatch.setattr(bmod, "_write_pidfile", _spy_write)

        bmod._unrevoke_app_pid("ext", dict(self.ROW))
        assert wrote is False, "no write is attempted when the base read fails"
        assert bmod._read_pidfile() == other, "the recorded rows survive the failed read"


class TestBackendQuarantine:
    """GPT F1: an update whose spawn-record deletion could not be CONFIRMED
    (ENOSPC/EDQUOT) leaves a stale row that vouches for an OLD-code survivor, so a
    later enable would adopt it as the new version. The quarantine blocks that later
    start until the record is confirmed gone.
    """

    @pytest.fixture(autouse=True)
    def _clear_memory_quarantine(self):
        # The in-memory backstop is module-global; isolate each test from the others.
        bmod._quarantined_backends.clear()
        yield
        bmod._quarantined_backends.clear()

    def test_quarantine_set_read_and_clear_roundtrip(self, tmp_path: Path, monkeypatch) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        assert bmod.is_backend_quarantined("ext") is False
        bmod.quarantine_backend("ext", "update could not confirm delete")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.clear_backend_quarantine("ext")
        assert bmod.is_backend_quarantined("ext") is False

    def test_clear_is_a_noop_when_not_quarantined(self, tmp_path: Path, monkeypatch) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod.clear_backend_quarantine("never")  # must not raise
        assert bmod.is_backend_quarantined("never") is False

    def test_a_present_but_corrupt_quarantine_file_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F2: a marker that EXISTS but cannot be parsed must be treated as
        quarantined. Reading corrupt JSON as empty would silently defeat the gate and
        let a retained spawn row adopt an old-code survivor."""
        qf = tmp_path / "app_backends.quarantine.json"
        qf.write_text("{not json")
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        assert bmod.is_backend_quarantined("ext") is True

    def test_clearing_the_quarantine_lifts_the_hold(self, tmp_path: Path, monkeypatch) -> None:
        """Once the stale row the quarantine guarded against is confirmed gone, the
        hold is lifted so the next start/enable can proceed. clear_backend_quarantine
        is the direct lift; a fresh spawn record (next test) clears it as a side
        effect of recording its own identity."""
        pf = tmp_path / "app_backends.pids.json"
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._write_pidfile({"ext": {"pid": 7, "start_time": "s", "port": 9000}})
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.clear_backend_quarantine("ext")
        assert bmod.is_backend_quarantined("ext") is False

    def test_a_fresh_spawn_record_clears_the_quarantine(self, tmp_path: Path, monkeypatch) -> None:
        """A fresh spawn we launched owns the row, overwriting the stale one, so the
        hold is lifted — a fresh row vouches for the new backend, not the old survivor."""
        pf = tmp_path / "app_backends.pids.json"
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_pidfile_path", lambda: pf)
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        monkeypatch.setattr(bmod, "_proc_start_time", lambda _pid: "fresh-st")
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod._record_app_pid("ext", 4242, 9000, spawn_instance="sp-new")
        assert bmod.is_backend_quarantined("ext") is False

    def test_quarantine_holds_in_memory_when_the_marker_write_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F1: the on-disk marker write fails under the SAME ENOSPC that triggers
        the quarantine, so a disk-only marker is absent exactly when needed. The
        in-memory backstop must still refuse adoption for the running generation."""
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")

        def _enospc(*_a, **_k):
            raise OSError("[Errno 28] No space left on device")

        monkeypatch.setattr(bmod, "atomic_write", _enospc)
        # The marker write fails, but quarantine_backend still records in memory...
        assert bmod.quarantine_backend("ext", "update could not confirm delete") is True
        assert not qf.exists()  # nothing landed on disk
        # ...and the guard reads CLOSED from the in-memory backstop.
        assert bmod.is_backend_quarantined("ext") is True
        bmod._quarantined_backends.discard("ext")  # cleanup

    def test_clear_lifts_both_the_memory_and_disk_quarantine(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod.quarantine_backend("ext", "stale")
        assert bmod.is_backend_quarantined("ext") is True
        bmod.clear_backend_quarantine("ext")
        assert bmod.is_backend_quarantined("ext") is False
        assert "ext" not in bmod._quarantined_backends

    def test_a_present_but_unreadable_marker_fails_closed(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """GPT F2: a quarantine marker that EXISTS but cannot be read (EACCES/EIO/fd
        exhaustion/corruption) must be treated as quarantined -- reading it as empty
        would let a retained spawn row adopt an old-code survivor past the gate."""
        qf = tmp_path / "app_backends.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        qf.write_text('{"ext": "stale"}')  # the marker is PRESENT

        import builtins

        real_open = builtins.open

        def _deny(path, *a, **k):
            if str(path) == str(qf):
                raise PermissionError("[Errno 13] Permission denied")
            return real_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", _deny)
        # _read_quarantine raises QuarantineUnreadable, and the gate fails CLOSED.
        assert bmod.is_backend_quarantined("ext") is True

    def test_a_truly_absent_marker_reads_as_not_quarantined(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The complement: a MISSING marker (FileNotFoundError -- the ENOSPC
        write-never-landed case) is not quarantined, so the gate does not block
        every app forever when no quarantine was ever written."""
        qf = tmp_path / "does_not_exist.quarantine.json"
        monkeypatch.setattr(bmod, "_quarantine_path", lambda: qf)
        bmod._quarantined_backends.discard("ext")
        assert bmod.is_backend_quarantined("ext") is False


class TestRestartKeepsTheSpawnRowUntilTheRespawnDecides:
    """The exited leader's row is the spawn tree's only witness, so the restart keeps it.

    A restart that drops the row before the respawn asks leaves the adopt branch with
    no record to read: a pre-fork worker or a detached child of this SAME app that is
    still holding the declared port cannot be attributed, the respawn refuses it, and
    the app stays down for as long as its own orphan keeps the port. The drop therefore
    happens once the attempt has decided, and only when nothing the row attributes is
    left tracked.
    """

    ROW: dict[str, Any] = {
        "pid": 617,
        "start_time": "st-617",
        "port": 9136,
        "spawn_instance": "sp-tree",
    }

    @pytest.fixture(autouse=True)
    def _fast_backoff(self, _floor_monkeypatch: pytest.MonkeyPatch) -> list[float]:
        delays: list[float] = []
        _floor_monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(
                is_set=lambda: False,
                wait=lambda delay: delays.append(delay) or False,
            ),
        )
        _floor_monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: True)
        bmod._write_pidfile({"app": dict(self.ROW)})
        return delays

    @staticmethod
    def _track(name: str = "app") -> AppProcess:
        ap = AppProcess(
            app_name=name,
            port=9136,
            pid=617,
            pid_start_time="st-617",
            proc=_fake_proc(pid=617, returncode=-15),
            healthy=False,
            mcp_healthy=False,
        )
        with bmod._lock:
            bmod._processes[name] = ap
            bmod._allocated_ports[name] = ap.port
        return ap

    def test_the_row_is_still_readable_when_the_respawn_asks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordering pin: the respawn's own attribution needs the record intact."""

        ap = self._track()
        seen: list[dict[str, Any] | None] = []

        def _start(name: str) -> AppProcess:
            seen.append(bmod._read_pidfile().get(name))
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=618))
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert seen == [self.ROW]

    def test_a_transient_spawn_failure_that_restores_tracking_keeps_the_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restored retry must not drop the row a later exit would otherwise take.

        The loop commits to superseding (arming the deferred drop), the respawn then
        fails and ``_settle_superseding_start`` restores ``ap`` to tracking and answers
        ``\"continue\"``. ``ap`` is tracked again with a non-None ``proc``, so the
        adopted-instance guard would NOT save it -- the deferred drop must be DISARMED
        on that restore, or the next exit reaches the ``finally`` and deletes the row of
        a leader that is once again live and tracked, orphaning its group.

        Driven by making the spawn fail and the settlement restore ``ap`` then, on the
        re-entered loop, letting a shutdown end it: the row must survive.
        """

        ap = self._track()
        calls = {"settle": 0}

        def _settle(_name: str, _ap: AppProcess, _replacement: Any) -> str:
            calls["settle"] += 1
            if calls["settle"] == 1:
                # Restore ap to tracking exactly as the real settle's "nothing tracked"
                # branch does, and ask the loop to continue.
                with bmod._lock:
                    bmod._processes["app"] = _ap
                return "continue"
            return "return_false"

        # Spawn fails transiently -> replacement is None; the app stays DECLARED
        # (a manifest with a backend entryPoint), so the loop does not early-return
        # True and falls through to the final settlement.
        monkeypatch.setattr(bmod, "_start_app_backend", lambda _name: None)
        monkeypatch.setattr(
            bmod,
            "get_app_manifest",
            lambda _name: SimpleNamespace(backend=SimpleNamespace(entryPoint="app.py")),
        )
        monkeypatch.setattr(bmod, "_settle_superseding_start", _settle)

        # A shutdown that trips only after the first wait, so the first pass proceeds
        # to the failed spawn + settlement-restore, and the restored second pass exits.
        state = {"waits": 0, "down": False}

        def _wait(_delay: float) -> bool:
            state["waits"] += 1
            if state["waits"] >= 2:
                state["down"] = True
            return state["down"]

        monkeypatch.setattr(
            bmod,
            "shutdown_event",
            SimpleNamespace(is_set=lambda: state["down"], wait=_wait),
        )
        bmod._restart_exited_backend(ap, -15)
        # The row survives: the restored, tracked leader still owns it.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_an_adopted_replacement_keeps_the_row_it_was_attributed_by(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Its ownership is re-read from this row on every re-bind, so the row stays."""

        ap = self._track()
        adopted = AppProcess(
            app_name="app",
            port=9136,
            proc=None,
            adopted_pids=[9001],
            adopted_start_times={9001: "st-9001"},
        )

        def _start(name: str) -> AppProcess:
            with bmod._lock:
                bmod._processes[name] = adopted
            return adopted

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_a_fresh_spawn_keeps_its_own_row_and_not_the_exited_leader_s(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drop stays keyed on the exited leader, so a successor's row survives it."""

        ap = self._track()
        successor = {
            "pid": 618,
            "start_time": "st-618",
            "port": 9137,
            "spawn_instance": "sp-new",
        }

        def _start(name: str) -> AppProcess:
            bmod._write_pidfile({name: dict(successor)})
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=618))
            with bmod._lock:
                bmod._processes[name] = replacement
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        assert bmod._restart_exited_backend(ap, -15) is True
        assert bmod._read_pidfile().get("app") == successor

    def test_a_respawn_that_produces_nothing_drops_the_stale_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is tracked, so the row names a process that is gone and it goes."""

        ap = self._track()
        monkeypatch.setattr(bmod, "_start_app_backend", lambda _name: None)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest(""))
        assert bmod._restart_exited_backend(ap, -15) is True
        assert "app" not in bmod._read_pidfile()

    def test_an_activation_denial_drops_the_row_on_its_early_return(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The removal covers every decided exit, including the ones before the spawn."""

        ap = self._track()
        monkeypatch.setattr(
            bmod,
            "_activation_denied",
            lambda *_args: bmod.ActivationVerdict(denied="not admitted"),
        )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("a denied restart must not spawn"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert "app" not in bmod._read_pidfile()

    def test_every_retry_can_still_read_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A retrying loop asks attribution again, so the row outlives each attempt."""

        ap = self._track()
        seen: list[dict[str, Any] | None] = []
        verdicts = [
            bmod.ActivationVerdict(denied="evaluator down", transient=True),
            bmod.ActivationVerdict(denied="not admitted"),
        ]

        def _verdict(*_args: Any) -> Any:
            seen.append(bmod._read_pidfile().get("app"))
            return verdicts[min(len(seen) - 1, len(verdicts) - 1)]

        monkeypatch.setattr(bmod, "_activation_denied", _verdict)
        monkeypatch.setattr(bmod, "get_app_manifest", lambda _name: _manifest("server.py"))
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("neither verdict admits a spawn"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert seen == [self.ROW, self.ROW]

    def test_a_dropped_record_leaves_a_successor_s_own_row_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A deliberate stop or a successor owns the row; the drop keys on the leader."""

        ap = self._track()
        successor = {"pid": 618, "start_time": "st-618", "port": 9137, "spawn_instance": "sp-2"}
        bmod._write_pidfile({"app": dict(successor)})
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
                bmod._processes["app"] = AppProcess(
                    app_name="app", port=9137, proc=_fake_proc(pid=618)
                )
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        assert bmod._read_pidfile().get("app") == successor

    def test_an_identity_moved_stop_leaves_the_row_for_the_winning_operation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Identity moved (a stop popped the slot), so the restart loop does NOT own
        the row -- the winning stop does.

        The restart loop is the LOSING party once another lifecycle operation has taken
        the slot: a stop that popped it already decided the row's fate (an ordinary stop
        that tolerated a surviving port-holder deliberately RETAINS the row so a later
        re-enable can re-attribute it). The restart loop dropping it here would override
        that decision and strand the survivor unmanageable, so on every identity-moved
        verdict the row is left exactly as the winning operation left it.
        """

        ap = self._track()
        with bmod._health_reconcile_lock:
            with bmod._lock:
                bmod._advance_lifecycle_locked("app", bmod._LIFECYCLE_STOP)
                bmod._processes.pop("app")
                bmod._allocated_ports.pop("app")
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not restart a stopped record"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        # The row the stop left in place survives the restart loop -- the loop does not
        # delete what another lifecycle operation owns.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_a_transient_early_return_keeps_the_row_for_the_surviving_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An early return BEFORE the supersede commit must not drop the row.

        The leader has exited but its group can still hold the declared port, and the
        loop returns early -- ``installed.json`` reads transiently unreadable (the
        tri-state ``_app_enabled_state`` is exactly for that), or a shutdown lands --
        before it ever commits to superseding this leader. The base dropped the row
        only AFTER those guards, so dropping it here would leave the surviving group
        the app's port with nothing naming it: the reap has no row to census and the
        next adoption reports no spawn recorded. The row therefore stays.
        """

        ap = self._track()
        # The tracked proc has exited (returncode set), so the loop passes the
        # liveness check and reaches the enabled-state guard, which reads as
        # "transiently unreadable" (None) -> an early ``return False`` before the
        # supersede commit.
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: None)
        monkeypatch.setattr(
            bmod,
            "_start_app_backend",
            lambda _name: pytest.fail("must not respawn on a transient early return"),
        )
        assert bmod._restart_exited_backend(ap, -15) is False
        # The row survives: the surviving group still has a witness for the reap and
        # for a later attribution.
        assert bmod._read_pidfile().get("app") == self.ROW

    def test_the_tracking_lock_is_released_before_the_pidfile_write(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: the row delete goes through ``_forget_app_pid_if`` -> the
        cross-process ``_pidfile_lock`` flock, which waits up to 300s under
        contention. ``_forget_exited_leader_row`` runs on the supervisor THREAD;
        holding the in-process ``_lock`` across that wait stalls every on-loop
        ``list_app_processes()`` for the same window (a watchdog-exit hazard). Assert
        ``_lock`` is NOT held while the pidfile write runs — only the in-memory
        successor check is under it."""
        ap = self._track()
        # Remove the live-handle tracking entry so the drop is NOT short-circuited by
        # the ``proc is None`` successor check — we want the delete path to run.
        with bmod._lock:
            bmod._processes.pop("app", None)

        lock_held_during_write = {"value": None}
        real_forget = bmod._forget_app_pid_if

        def _probe_forget(name, pid, start_time):
            # If ``_lock`` is still held here, a concurrent thread (e.g. the event
            # loop in list_app_processes) could not acquire it. acquire(blocking=False)
            # returns False when the lock is held by anyone, True when it is free.
            got = bmod._lock.acquire(blocking=False)
            if got:
                lock_held_during_write["value"] = False
                bmod._lock.release()
            else:
                lock_held_during_write["value"] = True
            return real_forget(name, pid, start_time)

        monkeypatch.setattr(bmod, "_forget_app_pid_if", _probe_forget)
        bmod._forget_exited_leader_row("app", ap)

        assert lock_held_during_write["value"] is False, (
            "the tracking _lock must be released before the pidfile write, or the "
            "300s flock wait stalls the event loop via list_app_processes()"
        )
        # And the drop still happened (row is gone), so correctness is preserved.
        assert "app" not in bmod._read_pidfile()

    def test_the_check_and_the_delete_run_under_the_lifecycle_flock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GPT 6.1: adoption reuses the row it finds, so an adoption landing between
        the tracking check and the delete would lose its provenance to the pid-matched
        pop. Both run inside the lifecycle flock a start holds across its spawn body."""
        import contextlib as _ctxlib

        ap = self._track()
        with bmod._lock:
            bmod._processes.pop("app", None)
        events: list[str] = []

        @_ctxlib.contextmanager
        def _recording_flock(name: str):
            events.append(f"enter:{name}")
            try:
                yield
            finally:
                events.append(f"exit:{name}")

        real_forget = bmod._forget_app_pid_if

        def _probe_forget(name, pid, start_time):
            events.append("delete")
            return real_forget(name, pid, start_time)

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _recording_flock)
        monkeypatch.setattr(bmod, "_forget_app_pid_if", _probe_forget)
        bmod._forget_exited_leader_row("app", ap)

        assert events == ["enter:app", "delete", "exit:app"]
        assert "app" not in bmod._read_pidfile()

    def test_an_adoption_tracked_under_the_flock_keeps_its_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The adoption that wins the flock first is what the check sees: it is tracked
        with no handle of ours, so the row it rests on is not deleted."""
        import contextlib as _ctxlib

        ap = self._track()
        adopted = AppProcess(app_name="app", port=9136, pid=0, proc=None, started_at=0.0)

        @_ctxlib.contextmanager
        def _flock_after_an_adoption(name: str):
            # The racing start held the flock first and published its adoption.
            with bmod._lock:
                bmod._processes[name] = adopted
            yield

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", _flock_after_an_adoption)
        bmod._forget_exited_leader_row("app", ap)

        assert bmod._read_pidfile().get("app") == self.ROW

    def test_an_unavailable_flock_keeps_the_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The flock's 300s ceiling raises ``OSError``; this runs in the restart's
        ``finally``, so it keeps the row (it names an exited leader) and does not raise."""
        ap = self._track()
        with bmod._lock:
            bmod._processes.pop("app", None)

        class _Unavailable:
            def __enter__(self) -> None:
                raise OSError("lifecycle lock timeout")

            def __exit__(self, *_exc: object) -> None:
                raise AssertionError("an unentered flock was released")

        monkeypatch.setattr(bmod, "app_backend_lifecycle_flock", lambda _name: _Unavailable())
        bmod._forget_exited_leader_row("app", ap)

        assert bmod._read_pidfile().get("app") == self.ROW

    def test_a_disable_landing_after_the_spawn_keeps_a_concurrent_stops_restored_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disable that lands AFTER the spawn must not delete the row a racing stop restored.

        The loop commits to superseding (popping the leader and arming the deferred
        drop), the spawn produces a replacement, and only THEN does a disable land: the
        post-spawn re-check sees ``_app_enabled_state`` is not ``True``, logs
        "cancelled after spawn", stops the replacement and returns. Meanwhile the
        concurrent stop that disabled the app found a detached worker still holding the
        port and RESTORED a recovery row for it (an ordinary stop that tolerates a
        survivor keeps that row so a later re-enable can re-attribute it).

        Without the fix the ``finally`` still carries ``leader_row_superseded`` armed on
        this return, so ``_forget_exited_leader_row`` deletes the row the stop just put
        back -- and the next enable refuses the survivor as unattributed, telling the
        user to stop the app's own backend. This asserts the restored row SURVIVES the
        losing restart's return, so reverting the disarm on that return turns it red.
        """
        ap = self._track()
        # The stop that disabled the app found a detached worker still holding the port
        # and RESTORED the recovery row for it. An ordinary stop restores what was
        # there -- the leader's OWN identity -- so the row carries ``ap``'s pid and
        # start_time, which is exactly what makes the pid-matched
        # ``_forget_app_pid_if`` in the ``finally`` delete it unless the drop is
        # disarmed on this losing return.
        recovery_row = {
            "pid": 617,
            "start_time": "st-617",
            "port": 9136,
            "spawn_instance": "sp-survivor",
        }

        enabled = {"value": True}

        def _start(name: str) -> AppProcess:
            # The spawn succeeds, producing a replacement...
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=720))
            # ...then the disable lands: the app is not enabled, and the stop that
            # disabled it stopped this replacement (so NO live-handle successor is left
            # tracked -- the ``proc is None`` short-circuit cannot save the row) and
            # restored the recovery row for the surviving detached worker.
            enabled["value"] = False
            with bmod._lock:
                bmod._processes.pop(name, None)
                bmod._allocated_ports.pop(name, None)
            bmod._write_pidfile({name: dict(recovery_row)})
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "_app_enabled_state", lambda _name: enabled["value"])
        # The replacement stop on the cancellation path is modelled by _start above.
        monkeypatch.setattr(bmod, "stop_app_backend", lambda *a, **k: None)

        assert bmod._restart_exited_backend(ap, -15) is False
        # The row the concurrent stop restored is intact: the losing restart did not
        # let the ``finally`` drop it. Without the disarm the pid-matched
        # ``_forget_app_pid_if`` deletes it here (same pid+start_time), stranding the
        # survivor so the next enable refuses it as unattributed.
        assert bmod._read_pidfile().get("app") == recovery_row

    def test_a_superseding_stop_landing_after_the_spawn_keeps_its_restored_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A superseding STOP that lands after the spawn must not delete its restored row.

        Distinct from the disable path above: here the post-spawn re-check finds the
        lifecycle generation has CHANGED to a non-start kind (a stop superseded this
        restart), so ``superseding_start`` is False and the loop takes the
        generation-changed cancellation return — the one guarded by
        ``lifecycle_now != lifecycle_snapshot and not superseding_start``. That return
        must disarm ``leader_row_superseded`` too: the superseding stop found a detached
        worker still on the port and restored the leader's recovery row, and leaving the
        flag armed lets the ``finally`` delete the row the stop just put back.

        Removing the disarm on THIS return (the generation-changed path) turns this test
        red while leaving the disable-path test green.
        """
        ap = self._track()
        recovery_row = {
            "pid": 617,
            "start_time": "st-617",
            "port": 9136,
            "spawn_instance": "sp-survivor",
        }

        def _start(name: str) -> AppProcess:
            replacement = AppProcess(app_name=name, port=9137, proc=_fake_proc(pid=721))
            # A stop supersedes this restart right after the spawn: bump the lifecycle
            # generation to a STOP kind (so superseding_start is False), drop the
            # replacement from tracking, and restore the leader's recovery row for the
            # surviving detached worker.
            bmod._advance_lifecycle_locked(name, bmod._LIFECYCLE_STOP)
            with bmod._lock:
                bmod._processes.pop(name, None)
                bmod._allocated_ports.pop(name, None)
            bmod._write_pidfile({name: dict(recovery_row)})
            return replacement

        monkeypatch.setattr(bmod, "_start_app_backend", _start)
        monkeypatch.setattr(bmod, "stop_app_backend", lambda *a, **k: None)

        assert bmod._restart_exited_backend(ap, -15) is False
        # The superseding stop's restored row survives: the losing restart left row
        # cleanup to the operation that won.
        assert bmod._read_pidfile().get("app") == recovery_row
