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
