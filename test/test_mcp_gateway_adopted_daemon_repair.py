"""Repairing a stale adopted daemon, not just reporting it.

``test_mcp_gateway_target_map_drift`` pins the DETECTION half: an adopted
survivor whose baked target map no longer covers the configured stub set is
found and warned about, and an unknown target at the pre-flight degrades to a
per-session exec instead of dying. What that leaves is the cost the warning
itself names -- pooling and the strict session key stay lost for every drifted
server, for as long as the daemon holds the socket.

This module pins the REPAIR: the incumbent is asked to release the socket, and a
daemon carrying the current map binds in its place.

Doing it by asking rather than taking is the whole design. The starting gateway
must not unlink the endpoint itself: ``_clear_stale_socket`` is a
connect-probe-then-unlink whose documented false-stale window would take a LIVE
incumbent's socket, which is the socket-theft class the flock guard exists to
prevent. A voluntary stand-down has no such window -- the request only ever
reaches a daemon that just answered on that socket.

Three properties beyond "it works" are pinned here, each found by review:

* the wait is on the singleton LOCK, not the endpoint (they part company for a
  daemon's whole drain on Windows);
* a daemon that ACCEPTED but is still draining is never adopted (it accepts
  nothing, so adopting it is worse than the drift);
* an incumbent that REFUSES is still adopted, exactly as before, because it is
  still serving -- the fail-open is deliberate and must not regress.

Every test that starts a real daemon isolates KIROCREW_HOME and the working
directory: the suite sets neither, and ``manager._gatewayd_log_path`` falls back
to ``config_dir()`` -- the operator's real data home -- while a spawned daemon
inherits pytest's CWD, which is the checkout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import manager as mgr
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.gatewayd import resolvable_target_stems

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="observes a socket file and lock being released"
)

_TARGET_PREFIXES = ("KIROCREW_MCP_TARGET_", "MC_MCP_TARGET_")


def _only_targets(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    """Make ``mapping`` the process's ENTIRE target set, leaving the rest of env alone.

    Never ``setattr(os, "environ", {...})``: ``gatewayd.os`` is the shared stdlib
    module, so replacing its ``environ`` replaces the process-wide mapping and
    drops every variable the suite set for isolation -- including KIROCREW_HOME,
    which sends a spawned daemon's log into the operator's real data home.
    """
    for key in [k for k in os.environ if any(k.startswith(p) for p in _TARGET_PREFIXES)]:
        monkeypatch.delenv(key, raising=False)
    for key, value in mapping.items():
        monkeypatch.setenv(key, value)


def _manager(tmp_path: Path, target_env: dict[str, str]) -> mgr.GatewayManager:
    return mgr.GatewayManager(
        mgr.GatewaySpec(socket_path=tmp_path / "gw.sock", mcp_target_env=dict(target_env))
    )


# ── gatewayd: the daemon yields its own socket ─────────────────────


class TestApplyStandDown:
    def test_stands_down_when_it_cannot_resolve_a_needed_stem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": ["KIROCREW_CORE", "PDF"]}, stop)
        assert reply["type"] == "standing-down"
        assert reply["missing"] == ["KIROCREW_CORE"]
        assert stop.is_set(), "the daemon must take the graceful SIGTERM path"

    def test_refuses_when_it_already_covers_everything_needed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Not a bare kill switch: nothing to gain means nothing to do."""
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": ["PDF"]}, stop)
        assert reply["type"] == "stand-down-rejected"
        assert not stop.is_set()

    def test_a_superset_daemon_is_fit_and_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Coverage is a superset test, matching the drift check.

        A daemon serving MORE than this config needs (another agent's servers) is
        fit; cycling it would be a needless full broker outage. This is why the
        frame carries the needed stems rather than an equality fingerprint.
        """
        _only_targets(
            monkeypatch,
            {
                "KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf",
                "KIROCREW_MCP_TARGET_EXCALIDRAW": "node x.js",
            },
        )
        stop = asyncio.Event()
        reply = gw._apply_stand_down({"type": "stand-down", "need": ["PDF"]}, stop)
        assert reply["type"] == "stand-down-rejected"
        assert not stop.is_set()

    @pytest.mark.parametrize("need", [None, [], "PDF", ["", "PDF"], [17]])
    def test_refuses_a_malformed_need(self, need: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
        stop = asyncio.Event()
        frame: dict[str, Any] = {"type": "stand-down"}
        if need is not None:
            frame["need"] = need
        assert gw._apply_stand_down(frame, stop)["type"] == "stand-down-rejected"
        assert not stop.is_set()

    def test_refuses_when_shutdown_is_not_wired(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An accepted-but-inert frame would leave the caller waiting on a lock
        that is never released."""
        _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
        reply = gw._apply_stand_down({"type": "stand-down", "need": ["CORE"]}, None)
        assert reply["type"] == "stand-down-rejected"


# ── the lock probe ─────────────────────────────────────────────────


class TestSingletonLockFree:
    def test_free_when_nobody_holds_it(self, short_sock_dir: Path) -> None:
        assert transport.singleton_lock_free(short_sock_dir / "gw.sock") is True

    def test_held_while_another_holder_has_it(self, short_sock_dir: Path) -> None:
        sock = short_sock_dir / "gw.sock"
        fd = transport.acquire_singleton_lock(sock)
        assert fd is not None
        try:
            assert transport.singleton_lock_free(sock) is False
        finally:
            os.close(fd)
        assert transport.singleton_lock_free(sock) is True

    def test_the_probe_leaves_the_lock_available(self, short_sock_dir: Path) -> None:
        """Acquire-and-release, not acquire-and-hold -- a leak wedges every spawn."""
        sock = short_sock_dir / "gw.sock"
        assert transport.singleton_lock_free(sock) is True
        fd = transport.acquire_singleton_lock(sock)
        assert fd is not None, "the probe must not still be holding the lock"
        os.close(fd)


# ── the stand-down request ─────────────────────────────────────────


class TestRequestStandDown:
    @pytest.mark.asyncio
    async def test_a_refusal_short_circuits_before_the_wait(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager,
            "_control_roundtrip",
            AsyncMock(return_value={"type": "stand-down-rejected", "reason": "covered"}),
        )
        looked = {"n": 0}

        def _free(_p: Path) -> bool:
            looked["n"] += 1
            return True

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        assert await manager._request_stand_down(["CORE"]) == mgr._REFUSED
        assert looked["n"] == 0

    @pytest.mark.asyncio
    async def test_no_answer_is_a_refusal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pre-upgrade daemon refuses an unrecognised frame by closing."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_control_roundtrip", AsyncMock(return_value=None))
        assert await manager._request_stand_down(["CORE"]) == mgr._REFUSED

    @pytest.mark.asyncio
    async def test_waits_on_the_lock_not_the_endpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Windows shape: the endpoint is gone long before the lock is free.

        run_gatewayd closes its server FIRST and releases the lock LAST, so on
        Windows the pipe name stops resolving at the very start of shutdown and
        the whole drain sits inside that gap. Waiting on the endpoint would
        return here immediately, the replacement would lose the still-held lock,
        exit rc=0 without binding, and nothing would rebind.
        """
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )
        monkeypatch.setattr(mgr.transport, "endpoint_exists", lambda _p: False)
        drain = {"ticks": 0}

        def _free(_p: Path) -> bool:
            drain["ticks"] += 1
            return drain["ticks"] > 4

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        assert await manager._request_stand_down(["CORE"]) == mgr._RELEASED
        assert drain["ticks"] == 5, "the wait must track the lock, not the endpoint"

    @pytest.mark.asyncio
    async def test_a_drain_that_outruns_the_budget_is_draining_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )
        monkeypatch.setattr(mgr.transport, "singleton_lock_free", lambda _p: False)
        monkeypatch.setattr(mgr, "_SHUTDOWN_GRACE_SECS", 0.2)
        assert await manager._request_stand_down(["CORE"]) == mgr._DRAINING

    @pytest.mark.asyncio
    async def test_gives_up_the_wait_when_shutdown_is_requested(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A shutdown must not be made to wait out another process's drain."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(
            manager, "_control_roundtrip", AsyncMock(return_value={"type": "standing-down"})
        )

        def _free(_p: Path) -> bool:
            manager._stopping = True
            return False

        monkeypatch.setattr(mgr.transport, "singleton_lock_free", _free)
        monkeypatch.setattr(mgr, "_SHUTDOWN_GRACE_SECS", 30.0)
        assert await manager._request_stand_down(["CORE"]) == mgr._DRAINING


class TestShutdownAnnouncesItselfEarly:
    @pytest.mark.asyncio
    async def test_stopping_is_set_before_the_lifecycle_lock_is_taken(self, tmp_path: Path) -> None:
        """Otherwise a start mid-stand-down-wait can never observe the shutdown."""
        manager = _manager(tmp_path, {})
        await manager._lifecycle_lock.acquire()
        try:
            task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0.05)
            assert manager._stopping is True, "shutdown must announce before blocking"
            assert not task.done(), "and it is genuinely still waiting for the lock"
        finally:
            manager._lifecycle_lock.release()
            await asyncio.wait_for(task, timeout=10)


# ── the repair decision ────────────────────────────────────────────


class TestRepairOrAdopt:
    @pytest.mark.asyncio
    async def test_a_yielding_incumbent_makes_room_for_a_spawn(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        asked = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)
        assert await manager._repair_or_adopt(["CORE"]) == mgr._SPAWN
        asked.assert_awaited_once_with(["CORE"])

    @pytest.mark.asyncio
    async def test_a_refusing_incumbent_is_still_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fail-open must not regress: a refusing daemon is still SERVING, so
        adopting keeps every server it can resolve working and the drifted ones
        degrade to per-session exec."""
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._REFUSED))
        assert await manager._repair_or_adopt(["CORE"]) == mgr._ADOPT

    @pytest.mark.asyncio
    async def test_a_draining_incumbent_is_never_adopted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Accepted-but-slow is not the same as refused.

        Once a daemon accepts it closes its accept loop, so it answers ping while
        accepting no new connection. Adopting it would turn a partial degradation
        (the drifted servers lose pooling) into a total outage (nothing can
        register) and report success while doing it.
        """
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._DRAINING))
        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._repair_or_adopt(["CORE"]) == mgr._ABORT
        assert any(
            "NOT adopted" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        )


class TestOscillationCap:
    """Bounding the case _ELECTION_ROUNDS cannot reach: two long-lived instances.

    The watchdog also assesses incumbents on an unbounded respawn loop, so
    without a process-lifetime cap two gateways sharing a socket path with
    divergent stub sets would stand each other's daemon down forever.
    """

    @pytest.mark.asyncio
    async def test_stops_asking_past_the_cap_and_settles_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = _manager(tmp_path, {})
        asked = AsyncMock(return_value=mgr._REFUSED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)

        for _ in range(mgr._MAX_STAND_DOWN_REQUESTS):
            assert await manager._repair_or_adopt(["CORE"]) == mgr._ADOPT
            manager._stand_downs_issued += 1  # the real counter lives in the mocked method
        assert asked.await_count == mgr._MAX_STAND_DOWN_REQUESTS

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._repair_or_adopt(["CORE"]) == mgr._ADOPT
        assert asked.await_count == mgr._MAX_STAND_DOWN_REQUESTS, "must stop asking"
        assert any(
            "already issued" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR
        )

    @pytest.mark.asyncio
    async def test_the_counter_advances_on_every_real_request(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {})
        monkeypatch.setattr(manager, "_control_roundtrip", AsyncMock(return_value=None))
        assert manager._stand_downs_issued == 0
        await manager._request_stand_down(["CORE"])
        await manager._request_stand_down(["CORE"])
        assert manager._stand_downs_issued == 2

    def test_the_counter_is_total_without_init(self) -> None:
        """Built via __new__, as several call sites and tests do."""
        bare = mgr.GatewayManager.__new__(mgr.GatewayManager)
        assert bare._stand_downs_issued == 0


class TestAdoptedDriftRecheck:
    """Re-checking coverage while the daemon is merely being kept alive.

    ``TestElection`` pins the start-time repair. That fires only at a
    transition -- adoption, or the pre-respawn gate -- and an adopted daemon
    that keeps answering ping is in neither: the watchdog supervises it for
    LIVENESS alone. So a survivor that goes stale AFTER adoption (the gateway
    restarts with a new stub_servers set while the daemon lives on) kept its
    stale map for as long as it held the socket. That is the 25-day-old daemon
    from the field; this class pins the loop that now catches it.

    The re-check rides the liveness ping's own reply, so it costs nothing extra
    on the wire -- the coverage report is in a frame the watchdog already reads.
    """

    @pytest.mark.asyncio
    async def test_a_fit_adopted_daemon_is_left_alone(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        asked = AsyncMock(return_value=mgr._SPAWN)
        monkeypatch.setattr(manager, "_repair_or_adopt", asked)
        assert await manager._reconcile_adopted({"type": "pong", "targets": ["CORE"]}) is False
        asked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_recheck_is_rate_limited_to_its_own_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cadence is the whole reason this is safe to run in the loop.

        The liveness ping is every 30s and a manager may only ever issue
        _MAX_STAND_DOWN_REQUESTS stand-downs, so re-assessing on every ping
        would spend that entire budget within ~90s against an incumbent that
        refuses to yield -- and then never ask again for the life of the
        process. One assessment per interval is what keeps the repair available.
        """
        # A fresh macOS hosted runner can have less uptime than the five-minute
        # interval.  Zero is the manager's "never checked" sentinel, not a real
        # monotonic timestamp, so the first assessment must not depend on host
        # uptime.
        monkeypatch.setattr(mgr.time, "monotonic", lambda: 60.0)
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        asked = AsyncMock(return_value=mgr._ADOPT)
        monkeypatch.setattr(manager, "_repair_or_adopt", asked)
        pong = {"type": "pong", "targets": []}

        assert await manager._reconcile_adopted(pong) is False
        assert asked.await_count == 1, "the first call assesses"
        for _ in range(5):
            assert await manager._reconcile_adopted(pong) is False
        assert asked.await_count == 1, "further pings inside the interval must not re-ask"

        manager._last_drift_check -= mgr._DRIFT_RECHECK_INTERVAL_SECS
        assert await manager._reconcile_adopted(pong) is False
        assert asked.await_count == 2, "a new interval assesses again"

    @pytest.mark.asyncio
    async def test_a_drifted_adopted_daemon_is_repaired_and_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        # _reconcile_adopted is only ever reached from the watchdog's adopted
        # branch, so anything asserting on _adopted must start there.
        manager._adopted = True
        monkeypatch.setattr(manager, "_repair_or_adopt", AsyncMock(return_value=mgr._SPAWN))
        ours = MagicMock()
        ours.pid = 4242
        ours.returncode = None

        async def _spawned() -> dict:
            manager._process = ours
            return {"type": "pong", "targets": ["CORE"]}

        monkeypatch.setattr(manager, "_spawn_and_confirm", AsyncMock(side_effect=_spawned))
        assert await manager._reconcile_adopted({"type": "pong", "targets": []}) is True
        assert manager._adopted is False, "we own the daemon now"
        assert manager.is_running

    @pytest.mark.asyncio
    @pytest.mark.parametrize("verdict", [mgr._ADOPT, mgr._ABORT])
    async def test_an_unyielding_incumbent_stays_adopted_without_spinning(
        self, verdict: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Neither refusal nor draining may make the watchdog hot-loop.

        Returning True here would send the loop straight back around with no
        sleep, re-pinging a daemon that just declined -- burning CPU for as long
        as the drift lasts. False means the caller sleeps out its liveness
        interval, which is also what gives a draining daemon time to finish (the
        next ping then fails and the death path spawns a replacement).

        Note this differs from ``start()``, which FAILS on _ABORT: a start that
        reported ready would hand sessions a daemon accepting nothing, whereas
        the watchdog is already supervising and simply keeps doing so.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        # _reconcile_adopted is only ever reached from the watchdog's adopted
        # branch, so anything asserting on _adopted must start there.
        manager._adopted = True
        monkeypatch.setattr(manager, "_repair_or_adopt", AsyncMock(return_value=verdict))
        spawn = AsyncMock()
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawn)
        assert await manager._reconcile_adopted({"type": "pong", "targets": []}) is False
        assert manager._adopted is True
        spawn.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["nothing_serves", "lost_the_race"])
    async def test_a_yield_we_could_not_capitalise_on_returns_to_adoption(
        self, outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two ways our spawn does not end up ours, one recovery.

        The incumbent released the socket, but either the spawn failed outright
        or a sibling gateway won the freed lock first. Either way this manager
        holds no usable process, so it must go back to the adopted branch --
        leaving _adopted False with _process None idles the watchdog forever
        with no daemon and no retry.

        Recovery deliberately does NOT go through _adopt_incumbent(), which
        starts a watchdog task: calling it from inside the watchdog would leave
        a second, uncancellable supervisor running.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        # _reconcile_adopted is only ever reached from the watchdog's adopted
        # branch, so anything asserting on _adopted must start there.
        manager._adopted = True
        monkeypatch.setattr(manager, "_repair_or_adopt", AsyncMock(return_value=mgr._SPAWN))
        # A foreign daemon answering (flock loser exits rc=0, so _process stays
        # unusable) vs the spawn failing outright.
        reply = None if outcome == "nothing_serves" else {"type": "pong", "targets": ["CORE"]}
        monkeypatch.setattr(manager, "_spawn_and_confirm", AsyncMock(return_value=reply))
        watchdogs: list[object] = []
        monkeypatch.setattr(
            manager, "_adopt_incumbent", lambda: watchdogs.append("started") or True
        )

        assert await manager._reconcile_adopted({"type": "pong", "targets": []}) is True
        assert manager._adopted is True, "must return to the adopted branch"
        assert manager._process is None
        assert not watchdogs, "must not re-enter _adopt_incumbent (it starts a watchdog)"

    @pytest.mark.asyncio
    async def test_adoption_starts_the_recheck_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coverage was just assessed, so the first re-check waits a full interval.

        Without this the watchdog's very first ping would repeat the assessment
        the adoption gate had just made, and against a refusing incumbent that
        spends a stand-down from the cap for no new information.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        monkeypatch.setattr(manager, "_run_watchdog", AsyncMock())
        assert manager._last_drift_check == 0.0
        assert manager._adopt_incumbent() is True
        assert manager._last_drift_check > 0.0
        if manager._watchdog is not None:
            manager._watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await manager._watchdog

        asked = AsyncMock(return_value=mgr._SPAWN)
        monkeypatch.setattr(manager, "_repair_or_adopt", asked)
        assert await manager._reconcile_adopted({"type": "pong", "targets": []}) is False
        asked.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_spawn_setup_failure_does_not_kill_the_watchdog(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The repair may not cost us the supervisor.

        _spawn_and_confirm guards _spawn_once, but its _clear_stale_socket and
        prepare_dir steps run OUTSIDE that guard, so an OSError there (a full
        disk, a vanished parent dir, a permission change) propagates. Unguarded,
        it escapes the watchdog's adopted branch and terminates the supervisor
        task -- the daemon then has NO watchdog at all, which is strictly worse
        than the drift the repair came to fix.

        Asserted on the loop, not just the helper: a passing return value proves
        nothing if the exception killed the caller.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        manager._adopted = True
        manager._process = None
        pong = {"type": "pong", "targets": []}
        pings = 0

        async def _ping() -> dict:
            nonlocal pings
            pings += 1
            # Stop after this cycle: a True return `continue`s to the loop
            # condition, so the watchdog exits without sleeping out its
            # liveness interval. An UNGUARDED OSError never reaches here at
            # all -- it propagates and _run_watchdog raises, which is the
            # regression this pins.
            manager._stopping = True
            return pong

        monkeypatch.setattr(manager, "_ping_payload", _ping)
        monkeypatch.setattr(manager, "_repair_or_adopt", AsyncMock(return_value=mgr._SPAWN))
        # Raise from the real producer step, not from _spawn_and_confirm itself:
        # the guard lives inside that method, so mocking it to raise would pin a
        # guard that no longer exists there and would miss a regression at its
        # other call site.
        monkeypatch.setattr(
            manager,
            "_clear_stale_socket",
            AsyncMock(side_effect=OSError("no space left on device")),
        )
        monkeypatch.setattr(manager, "_spawn_once", AsyncMock())

        # Must not raise: the watchdog has to survive its own repair attempt.
        await asyncio.wait_for(manager._run_watchdog(), timeout=5)

        assert pings == 1
        assert manager._adopted is True, "adoption must be restored, not left dangling"
        assert manager._process is None

    @pytest.mark.asyncio
    async def test_the_respawn_gate_adoption_also_starts_the_recheck_clock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every path that ADOPTS owes the clock stamp, not just _adopt_incumbent.

        The watchdog's pre-respawn gate adopts an incumbent inline (it must not
        call _adopt_incumbent, which would start a second watchdog). It had
        assessed coverage one line earlier but left _last_drift_check at 0.0, so
        the adopted branch's very first iteration saw an elapsed interval of
        `now - 0.0` and re-assessed immediately -- spending a stand-down from the
        lifetime cap to re-derive what the gate had just decided.
        """
        monkeypatch.setattr(mgr, "_RESPAWN_BACKOFF_START_SECS", 0.0)
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        manager._adopted = False

        exited = MagicMock()
        exited.pid = 999
        exited.returncode = 0
        exited.wait = AsyncMock(return_value=0)
        manager._process = exited

        async def _never() -> str:
            await asyncio.sleep(3600)
            return "unreachable"

        monkeypatch.setattr(manager, "_liveness_probe_loop", _never)
        # A fit incumbent holds the socket: drift is empty, so the gate adopts.
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": ["CORE"]}),
        )
        seen: list[float] = []

        async def _capture(frame: dict) -> bool:
            seen.append(manager._last_drift_check)
            manager._stopping = True
            return True

        monkeypatch.setattr(manager, "_reconcile_adopted", AsyncMock(side_effect=_capture))
        await asyncio.wait_for(manager._run_watchdog(), timeout=5)

        assert manager._adopted is True
        assert seen, "the adopted branch never ran, so the gate was not exercised"
        assert seen[0] > 0.0, "the respawn gate adopted without starting the clock"

    @pytest.mark.asyncio
    async def test_start_returns_false_rather_than_raising_on_a_setup_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling call site, and the contract that made it matter.

        `_spawn_and_confirm` has two callers. Guarding only the watchdog's would
        leave `_start_locked`'s untouched, where the same OSError escapes
        `start()` -- whose docstring promises "Never raises -- callers treat a
        False return as fall back to per-session MCP" -- and reaches the
        unguarded `await manager.start()` in the gateway bootstrap, failing
        gateway startup instead of degrading to per-session MCP.

        Guarding the producer covers both callers at once, which is why this
        test lives beside the watchdog one: they are the same defect.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        # Nobody holds the socket, so the election goes straight to spawning.
        monkeypatch.setattr(manager, "_ping_payload", AsyncMock(return_value=None))
        monkeypatch.setattr(
            manager,
            "_clear_stale_socket",
            AsyncMock(side_effect=OSError("no space left on device")),
        )
        spawn = AsyncMock()
        monkeypatch.setattr(manager, "_spawn_once", spawn)

        assert await manager.start() is False, "start() must fall back, not raise"
        spawn.assert_not_awaited(), "a failed setup must not proceed to spawn"
        assert manager._watchdog is None, "no supervisor for a start that failed"

    def test_the_stamp_is_total_without_init(self) -> None:
        """Built via __new__, as several call sites and tests do."""
        bare = mgr.GatewayManager.__new__(mgr.GatewayManager)
        assert bare._last_drift_check == 0.0

    @pytest.mark.asyncio
    async def test_the_watchdog_reconciles_on_the_liveness_reply(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wiring: the adopted branch must read the pong, not just a bool.

        _ping_once() throws the payload away, so a watchdog built on it could
        never see coverage. This pins that the branch reads _ping_payload and
        hands the frame to the reconciler.
        """
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "run core"})
        manager._adopted = True
        manager._process = None
        pong = {"type": "pong", "targets": []}
        monkeypatch.setattr(manager, "_ping_payload", AsyncMock(return_value=pong))
        monkeypatch.setattr(
            manager,
            "_ping_once",
            AsyncMock(
                side_effect=AssertionError(
                    "the adopted branch must read the pong payload, not a bare bool"
                )
            ),
        )

        async def _stop_after_one(frame: dict) -> bool:
            assert frame is pong
            manager._stopping = True
            return True  # True short-circuits the sleep, so the loop exits at once

        monkeypatch.setattr(manager, "_reconcile_adopted", AsyncMock(side_effect=_stop_after_one))
        await asyncio.wait_for(manager._run_watchdog(), timeout=5)
        assert manager._reconcile_adopted.await_count == 1  # type: ignore[attr-defined]


# ── the election ───────────────────────────────────────────────────


class TestElection:
    """What happens when our own spawn loses the socket to somebody else.

    The flock guard makes a duplicate spawn exit rc=0 WITHOUT binding, so a pong
    arriving after our spawn is not proof the daemon is ours.
    """

    @pytest.mark.asyncio
    async def test_a_fit_incumbent_is_adopted_with_no_stand_down(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": ["PDF"]}),
        )
        asked = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", asked)
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn"))
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawned)
        try:
            assert await manager._start_locked() is True
            assert manager._adopted is True
            asked.assert_not_awaited()
            spawned.assert_not_awaited()
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_a_stale_incumbent_that_yields_is_replaced(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "kirocrew mcp-core"})
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": ["PDF"]}),
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._RELEASED))
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 4242

        async def _spawn() -> dict[str, Any]:
            manager._process = proc
            return {"type": "pong", "targets": ["CORE"]}

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            assert manager.is_running, "we must own the replacement"
            assert manager._adopted is False
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_a_foreign_stale_daemon_forces_one_more_round(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Round two assesses whoever won the freed lock; here it yields."""
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "kirocrew mcp-core"})
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(side_effect=[None, {"type": "pong", "targets": ["PDF"]}]),
        )
        stood_down = AsyncMock(return_value=mgr._RELEASED)
        monkeypatch.setattr(manager, "_request_stand_down", stood_down)
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 4242
        rounds: list[int] = []

        async def _spawn() -> dict[str, Any]:
            rounds.append(1)
            if len(rounds) == 1:
                return {"type": "pong", "targets": ["PDF"]}  # lost the flock
            manager._process = proc
            return {"type": "pong", "targets": ["CORE"]}

        monkeypatch.setattr(manager, "_spawn_and_confirm", _spawn)
        try:
            assert await manager._start_locked() is True
            assert len(rounds) == 2, "a foreign stale daemon must force round two"
            stood_down.assert_awaited_once_with(["CORE"])
            assert manager.is_running
        finally:
            if manager._watchdog is not None:
                manager._watchdog.cancel()

    @pytest.mark.asyncio
    async def test_rounds_are_bounded_and_exhaustion_is_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "kirocrew mcp-core"})
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": ["PDF"]}),
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._RELEASED))
        spawn = AsyncMock(return_value={"type": "pong", "targets": ["PDF"]})
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawn)

        with caplog.at_level(logging.ERROR, logger=mgr.logger.name):
            assert await manager._start_locked() is False
        assert spawn.await_count == mgr._ELECTION_ROUNDS
        assert manager._adopted is False
        assert any(
            "election rounds" in r.getMessage()
            for r in caplog.records
            if r.levelno >= logging.ERROR
        )

    @pytest.mark.asyncio
    async def test_start_fails_rather_than_claiming_ready_on_a_draining_incumbent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = _manager(tmp_path, {"KIROCREW_MCP_TARGET_CORE": "kirocrew mcp-core"})
        monkeypatch.setattr(
            manager,
            "_ping_payload",
            AsyncMock(return_value={"type": "pong", "targets": ["PDF"]}),
        )
        monkeypatch.setattr(manager, "_request_stand_down", AsyncMock(return_value=mgr._DRAINING))
        spawned = AsyncMock(side_effect=RuntimeError("must not spawn into a held lock"))
        monkeypatch.setattr(manager, "_spawn_and_confirm", spawned)

        assert await manager._start_locked() is False
        spawned.assert_not_awaited()
        assert manager._adopted is False
        assert manager._watchdog is None


# ── end to end over a real socket ──────────────────────────────────


async def _round_trip(socket_path: Path, frame: dict[str, Any]) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(transport.connect(socket_path), timeout=10)
    try:
        writer.write(json.dumps(frame).encode("utf-8") + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=10)
        result = json.loads(line.decode("utf-8"))
        assert isinstance(result, dict)
        return result
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def _await_endpoint(socket_path: Path, *, timeout: float = 60.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await asyncio.to_thread(transport.endpoint_exists, socket_path):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"daemon never bound {socket_path}")


def _isolate(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Scope a real daemon's side effects: its data home AND its CWD.

    KIROCREW_HOME because the suite does not set it and
    ``manager._gatewayd_log_path`` falls back to ``config_dir()`` -- the
    operator's real data home. The working directory because a spawned daemon
    INHERITS pytest's CWD, which is the checkout; ``_spawn_once`` deliberately
    passes no ``cwd`` (in production the daemon should inherit the gateway's), so
    the test moves itself rather than changing that.
    """
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.chdir(home)
    return home


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_real_daemon_leaves_nothing_in_the_repository(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No test side effects, asserted rather than assumed."""
    repo = Path(__file__).resolve().parent.parent

    def _snapshot() -> set[str]:
        skip = {".git", ".venv", "node_modules", "__pycache__"}
        return {e.name for e in repo.iterdir() if e.name not in skip}

    before = _snapshot()
    run_dir = _isolate(monkeypatch, tmp_path / "home")
    assert Path.cwd() == run_dir.resolve(), "the test must not run in the checkout"
    _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})

    sock = short_sock_dir / "gw.sock"
    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        assert (await _round_trip(sock, {"type": "ping"}))["type"] == "pong"
    finally:
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(daemon, timeout=30)

    assert _snapshot() == before, "a daemon under test must not write into the repo"


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_the_stand_down_frame_is_wired_into_the_serving_daemon(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ends the real daemon AND frees the lock a replacement must win.

    This is the test that fails if ``run_gatewayd`` stops forwarding its
    ``stop_event`` into the handler: the frame would answer
    ``stand-down-rejected`` and the socket would stay bound.
    """
    _isolate(monkeypatch, tmp_path / "home")
    _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
    sock = short_sock_dir / "gw.sock"
    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=1, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        reply = await _round_trip(sock, {"type": "stand-down", "need": ["KIROCREW_CORE"]})
        assert reply["type"] == "standing-down"
        await asyncio.wait_for(daemon, timeout=60)
        assert not await asyncio.to_thread(transport.endpoint_exists, sock)
        assert await asyncio.to_thread(
            transport.singleton_lock_free, sock
        ), "and its lock, which is what a replacement must win"
    finally:
        stop.set()
        if not daemon.done():
            daemon.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await daemon


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_stale_survivor_is_repaired_end_to_end(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field case, start to finish, with TWO real daemon processes.

    The survivor is a real subprocess launched with an OLD target map, standing
    in for the 25-day-old daemon the drift module describes. That matters: its
    environment is genuinely frozen at spawn, so the manager cannot influence
    what it reports -- the invariant the whole check rests on, and one an
    in-process survivor sharing ``os.environ`` cannot model.

    The gateway then starts wanting a stem the survivor does not have. Before
    this change it adopted the survivor and every session's stub for that server
    degraded to per-session exec; now the survivor yields and a daemon with the
    current map binds.
    """
    home = tmp_path / "home"
    run_dir = _isolate(monkeypatch, home)
    sock = short_sock_dir / "gw.sock"

    stale_env = {
        **{
            k: v
            for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _TARGET_PREFIXES)
        },
        "KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio",
        "KIROCREW_HOME": str(home),
    }
    survivor = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "kiro_crew.mcp_gateway.gatewayd",
        "--socket",
        str(sock),
        "--idle-timeout-secs",
        "60",
        "--max-backends",
        "2",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        env=stale_env,
        cwd=str(run_dir),
        start_new_session=True,
    )
    manager: mgr.GatewayManager | None = None
    try:
        await _await_endpoint(sock)
        assert (await _round_trip(sock, {"type": "ping"}))["targets"] == ["PDF"]

        # The starting gateway's config now wants kirocrew-core stubbed too.
        wanted = {
            "KIROCREW_MCP_TARGET_KIROCREW_CORE": "kirocrew mcp-core",
            "KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio",
        }
        _only_targets(monkeypatch, wanted)
        manager = mgr.GatewayManager(
            mgr.GatewaySpec(
                socket_path=sock,
                max_backends=2,
                idle_timeout_secs=60,
                mcp_target_env=dict(wanted),
            )
        )

        assert await manager.start() is True, "the gateway must come up"
        await asyncio.wait_for(survivor.wait(), timeout=60)
        assert manager._adopted is False, "the stale survivor must not be adopted"
        assert manager.is_running, "we must own the replacement"
        assert manager._process is not None
        assert manager._process.pid != survivor.pid, "two distinct real processes"

        pong = await _round_trip(sock, {"type": "ping"})
        assert "KIROCREW_CORE" in pong["targets"], "the new daemon covers the new stem"
    finally:
        if manager is not None:
            await manager.shutdown()
        if survivor.returncode is None:
            survivor.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(survivor.wait(), timeout=10)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_covering_survivor_is_still_adopted_end_to_end(
    short_sock_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behaviour adoption exists for must survive the repair layer.

    A survivor already covering the wanted stems is adopted with no spawn and no
    teardown -- otherwise this change would trade a partial degradation for a
    full broker cycle on every ordinary restart.
    """
    _isolate(monkeypatch, tmp_path / "home")
    sock = short_sock_dir / "gw.sock"
    targets = {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf --stdio"}
    _only_targets(monkeypatch, targets)
    manager = mgr.GatewayManager(
        mgr.GatewaySpec(
            socket_path=sock, max_backends=2, idle_timeout_secs=60, mcp_target_env=dict(targets)
        )
    )
    stop = asyncio.Event()
    survivor = asyncio.create_task(
        gw.run_gatewayd(socket_path=sock, max_backends=2, idle_timeout_secs=60, stop_event=stop)
    )
    try:
        await _await_endpoint(sock)
        assert await manager.start() is True
        assert manager._adopted is True, "a covering survivor must still be adopted"
        assert not manager.is_running, "adoption must not spawn a competitor"
        assert not survivor.done(), "and must not tear it down"
    finally:
        await manager.shutdown()
        stop.set()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(survivor, timeout=30)


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_stand_down_is_recorded_in_the_security_event_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The audit claim is asserted, not just documented.

    A stand-down ends the daemon, so it is the most consequential frame the
    control surface accepts; both the allow and the refusal belong in the SEL
    beside the claim/abort/peer decisions.
    """
    _isolate(monkeypatch, tmp_path / "home")
    _only_targets(monkeypatch, {"KIROCREW_MCP_TARGET_PDF": "npx -y server-pdf"})
    recorded: list[dict[str, Any]] = []

    class _Spy:
        def log_api_access(self, **kwargs: Any) -> None:
            recorded.append(kwargs)

    monkeypatch.setattr(gw, "SecurityEventLog", _Spy)

    gw._apply_stand_down({"type": "stand-down", "need": ["PDF"]}, asyncio.Event())
    gw._apply_stand_down({"type": "stand-down", "need": ["KIROCREW_CORE"]}, asyncio.Event())

    ops = [r for r in recorded if r.get("operation") == "mcp-gateway.stand_down"]
    assert len(ops) == 2, f"both outcomes must be audited, got {recorded}"
    assert ops[0]["outcome"] == "denied"
    assert ops[1]["outcome"] == "allowed"
    assert resolvable_target_stems() == ["PDF"]
