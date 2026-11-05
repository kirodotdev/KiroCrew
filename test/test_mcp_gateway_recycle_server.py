"""``kirocrew mcp reload <server>`` recycles one MCP server without a restart.

A pooled backend whose ``PoolKey`` is unchanged but whose boot-time input moved
(a token read from a file, an env key the identity hash leaves out) keeps
serving the old value. ``BackendPool.drain_server`` retires that one server's
backends, the ``recycle-server`` control frame reaches it over the existing
uid-gated socket, and the CLI sends the frame through ``daemon_control``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import cli_commands_mcp
from kiro_crew.mcp_gateway import daemon_control as dc
from kiro_crew.mcp_gateway import gatewayd
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.daemon.control import _apply_recycle_server, _serve_control_frame
from kiro_crew.mcp_gateway.pool import DRAIN_DEADLINE_SECS, BackendPool, PoolKey
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="AF_UNIX listener")


def _key(server: str, variant: str = "a") -> PoolKey:
    return PoolKey(
        server_name=server,
        command_args_hash=f"args-{variant}",
        effective_env_hash="env",
        work_dir="/tmp/test",
        binary_version="1.0",
        os_uid=1000,
    )


def _backend(key: PoolKey, *, refcount: int = 0, pinned: bool = False) -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    stdin = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    backend = Backend(
        pool_key=key,
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    backend.refcount = refcount
    backend.pinned = pinned
    backend.shutdown = AsyncMock()  # type: ignore[method-assign]
    return backend


class TestDrainServer:
    @pytest.mark.asyncio
    async def test_retires_only_the_named_server(self) -> None:
        pool = BackendPool(max_backends=10)
        idle = _backend(_key("github", "a"))
        warm = _backend(_key("github", "b"), pinned=True)
        busy = _backend(_key("github", "c"), refcount=1)
        other = _backend(_key("slack"))
        for b in (idle, warm, busy, other):
            await pool.add(b.pool_key, b)

        result = await pool.drain_server("github")

        assert (result.evicted, result.draining, result.private) == (2, 1, 0)
        # Idle backends, the prewarmed one included, are stopped at once.
        assert idle.shutdown.await_count == 1
        assert warm.shutdown.await_count == 1
        # The busy one keeps serving until it is reaped.
        assert busy.shutdown.await_count == 0
        assert pool.draining_count == 1
        assert busy in pool.all_backends()
        # Every github backend left the acquire index; slack was not touched.
        assert await pool.get(idle.pool_key) is None
        assert await pool.get(busy.pool_key) is None
        assert await pool.get(other.pool_key) is other
        assert other.shutdown.await_count == 0

    @pytest.mark.asyncio
    async def test_busy_backend_uses_the_bluegreen_deadline_and_is_reaped(self) -> None:
        pool = BackendPool(max_backends=10)
        busy = _backend(_key("github"), refcount=1)
        await pool.add(busy.pool_key, busy)
        before = time.monotonic()

        await pool.drain_server("github")

        entry = pool._draining[0]
        assert (
            before + DRAIN_DEADLINE_SECS
            <= entry.deadline
            <= time.monotonic() + (DRAIN_DEADLINE_SECS)
        )
        assert await pool.reap_draining() == []
        busy.refcount = 0
        assert await pool.reap_draining() == [busy]
        assert busy.shutdown.await_count == 1

    @pytest.mark.asyncio
    async def test_reserved_backend_drains_instead_of_dying_mid_attach(self) -> None:
        pool = BackendPool(max_backends=10)
        reserved = _backend(_key("github"))
        await pool.add(reserved.pool_key, reserved, reserve=True)

        result = await pool.drain_server("github")

        assert (result.evicted, result.draining) == (0, 1)
        assert reserved.shutdown.await_count == 0

    @pytest.mark.asyncio
    async def test_next_acquire_spawns_a_fresh_process(self) -> None:
        pool = BackendPool(max_backends=10)
        key = _key("github")
        old = _backend(key)
        await pool.add(key, old)
        await pool.drain_server("github")
        fresh = _backend(key)

        got = await pool.get_or_create(key, AsyncMock(return_value=fresh))

        assert got is fresh

    @pytest.mark.asyncio
    async def test_private_backends_are_counted_and_left_alone(self) -> None:
        pool = BackendPool(max_backends=10)
        private = _backend(_key("github"), refcount=1)
        pool._exclusive["stub-1"] = private

        result = await pool.drain_server("github")

        assert result.private == 1
        assert private.shutdown.await_count == 0
        assert pool._exclusive["stub-1"] is private

    @pytest.mark.asyncio
    async def test_inflight_spawn_of_the_server_is_respawned(self) -> None:
        pool = BackendPool(max_backends=10)
        key = _key("github")
        stale, fresh = _backend(key), _backend(key)
        spawned: list[Backend] = []

        async def spawn() -> Backend:
            if not spawned:
                spawned.append(stale)
                await pool.drain_server("github")
                return stale
            spawned.append(fresh)
            return fresh

        assert await pool.get_or_create(key, spawn) is fresh
        assert stale.shutdown.await_count == 1

    @pytest.mark.asyncio
    async def test_inflight_spawn_of_another_server_is_kept(self) -> None:
        pool = BackendPool(max_backends=10)
        key = _key("slack")
        only = _backend(key)
        calls = 0

        async def spawn() -> Backend:
            nonlocal calls
            calls += 1
            await pool.drain_server("github")
            return only

        assert await pool.get_or_create(key, spawn) is only
        assert calls == 1
        assert only.shutdown.await_count == 0

    @pytest.mark.asyncio
    async def test_teardown_mid_drain_still_reaps_every_idle_backend(self) -> None:
        # The control handler that runs drain_server is what daemon teardown
        # cancels. Idle backends already left _backends, so a cancel before
        # each one's shutdown would orphan it unless shutdown_all can join it.
        pool = BackendPool(max_backends=10)
        first, second = _backend(_key("github", "a")), _backend(_key("github", "b"))
        for b in (first, second):
            await pool.add(b.pool_key, b)
        started, release = asyncio.Event(), asyncio.Event()

        async def blocking_shutdown(timeout: float = 0) -> None:
            started.set()
            await release.wait()

        first.shutdown.side_effect = blocking_shutdown
        second.shutdown.side_effect = blocking_shutdown
        drain = asyncio.create_task(pool.drain_server("github"))
        await asyncio.wait_for(started.wait(), FALLBACK_TIMEOUT_SECS)
        drain.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await drain

        await pool.shutdown_all()

        assert first.shutdown.await_count == 1
        assert second.shutdown.await_count == 1


class TestRecycleServerFrame:
    @pytest.fixture
    def audits(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            gatewayd, "_audit_recycle_server", lambda s, o, d: rows.append((s, o, d))
        )
        return rows

    @pytest.mark.asyncio
    async def test_recycles_and_audits(self, audits: list[tuple[str, str, str]]) -> None:
        pool = BackendPool(max_backends=10)
        b = _backend(_key("github"))
        await pool.add(b.pool_key, b)

        reply = await _apply_recycle_server({"type": "recycle-server", "server": "github"}, pool)

        assert reply == {
            "type": "recycled",
            "server": "github",
            "evicted": 1,
            "draining": 0,
            "private": 0,
            "deadline_secs": DRAIN_DEADLINE_SECS,
        }
        assert audits and audits[0][:2] == ("github", "allowed")
        assert "evicted=1" in audits[0][2]

    @pytest.mark.asyncio
    async def test_mcp_json_key_reaches_backends_pooled_under_its_alias(
        self, audits: list[tuple[str, str, str]]
    ) -> None:
        # Pool keys of a slash-bearing mcp.json key use its slash-free alias.
        pool = BackendPool(max_backends=10)
        b = _backend(_key("playwright-mcp"))
        await pool.add(b.pool_key, b)

        reply = await _apply_recycle_server(
            {"type": "recycle-server", "server": "npm:@playwright/mcp"}, pool
        )

        assert (reply["type"], reply["evicted"]) == ("recycled", 1)
        assert await pool.get(b.pool_key) is None
        assert b.shutdown.await_count == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("server", [None, "", 7, "a\nforged", "x" * 257])
    async def test_invalid_name_is_refused_and_audited(
        self, server: Any, audits: list[tuple[str, str, str]]
    ) -> None:
        pool = BackendPool(max_backends=10)
        b = _backend(_key("github"))
        await pool.add(b.pool_key, b)

        reply = await _apply_recycle_server({"type": "recycle-server", "server": server}, pool)

        assert reply["type"] == "recycle-server-rejected"
        assert audits == [("", "denied", "missing or invalid server name")]
        assert await pool.get(b.pool_key) is b

    @pytest.mark.asyncio
    async def test_dispatched_by_the_control_frame_router(
        self, audits: list[tuple[str, str, str]]
    ) -> None:
        pool = BackendPool(max_backends=10)
        writer = MagicMock()
        writer.drain = AsyncMock()
        handled = await _serve_control_frame(
            {"type": "recycle-server", "server": "github"}, writer, pool
        )
        assert handled is True
        line = b"".join(c.args[0] for c in writer.write.call_args_list)
        assert json.loads(line)["type"] == "recycled"


def _serve_control_on(path: Path, pool_factory: Any) -> tuple[threading.Thread, Any]:
    """Run the daemon's REAL control router on an AF_UNIX listener in a thread."""
    ready = threading.Event()
    state: dict[str, Any] = {}

    async def main() -> None:
        pool = await pool_factory()
        state["pool"] = pool
        stop = asyncio.Event()
        state["stop"] = stop
        state["loop"] = asyncio.get_running_loop()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            frame = json.loads(await reader.readline())
            await _serve_control_frame(frame, writer, pool)
            writer.close()

        server = await asyncio.start_unix_server(handle, path=str(path))
        ready.set()
        async with server:
            await stop.wait()

    t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    t.start()
    assert ready.wait(5)
    return t, state


@_POSIX_ONLY
class TestRecycleServerClient:
    def test_round_trip_through_the_real_router(
        self, short_sock_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(gatewayd, "_audit_recycle_server", lambda *a: None)
        sock = short_sock_dir / "gw.sock"
        busy = _backend(_key("github", "b"), refcount=2)

        async def make_pool() -> BackendPool:
            pool = BackendPool(max_backends=10)
            await pool.add(_key("github", "a"), _backend(_key("github", "a")))
            await pool.add(busy.pool_key, busy)
            return pool

        t, state = _serve_control_on(sock, make_pool)
        try:
            result = dc.recycle_server("github", sock)
        finally:
            state["loop"].call_soon_threadsafe(state["stop"].set)
            t.join(timeout=5)

        # outcome, evicted, draining, private, deadline_secs
        assert result == dc.RecycleResult("recycled", 1, 1, 0, DRAIN_DEADLINE_SECS)
        assert state["pool"].draining_count == 1

    def test_no_daemon_is_absent(self, short_sock_dir: Path) -> None:
        assert dc.recycle_server("github", short_sock_dir / "absent.sock").outcome == "absent"


class TestCli:
    def test_reload_parses_into_the_handler_namespace(self) -> None:
        import argparse

        from kiro_crew import cli_parser_gateway

        parser = argparse.ArgumentParser()
        cli_parser_gateway.register_mcp_parser(parser.add_subparsers(dest="command"))
        args = parser.parse_args(["mcp", "reload", "github"])
        assert (args.command, args.mcp_action, args.server) == ("mcp", "reload", "github")

    @pytest.mark.parametrize(
        ("result", "code", "needle"),
        [
            # outcome, evicted, draining, private, deadline_secs
            (dc.RecycleResult("recycled", 1, 2, 0, 60.0), 0, "within 60s"),
            (dc.RecycleResult("recycled"), 0, "No running backends"),
            (dc.RecycleResult("absent"), 1, "not running"),
            (dc.RecycleResult("rejected", reason="nope"), 1, "refused the reload: nope"),
            (dc.RecycleResult("response_timeout"), 1, "response_timeout"),
        ],
    )
    def test_outcomes_map_to_exit_codes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        result: dc.RecycleResult,
        code: int,
        needle: str,
    ) -> None:
        monkeypatch.setattr(dc, "recycle_server", lambda server: result)
        import argparse

        rc = cli_commands_mcp.mcp_cmd(argparse.Namespace(mcp_action="reload", server="github"))
        captured = capsys.readouterr()
        assert rc == code
        assert needle in captured.out + captured.err

    def test_no_action_is_usage(self) -> None:
        import argparse

        assert cli_commands_mcp.mcp_cmd(argparse.Namespace(mcp_action=None)) == 2
