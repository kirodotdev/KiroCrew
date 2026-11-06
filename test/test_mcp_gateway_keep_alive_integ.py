"""Integration: a kept-alive server keeps ONE backend across agents and idle time.

Real daemon, real stub processes, real backend processes. Two stubs under two
agents (a chat and a worker) declare one server identically and reach one backend.
Both detach, the daemon sweeps idle backends, and a third stub arrives later:

* with the server named in ``keep_alive_servers`` it is answered by the SAME
  backend, so a server holding its sign-in in memory signs in once;
* without it (the default), the idle sweep drained the backend and a new one starts.

``KIROCREW_KEEP_ALIVE_IDLE_SECS`` sets the idle timeout (default 2s); the manual
proof for the PR ran it at the shipped 300s.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest
from test_mcp_gateway_pool_integ import (
    _FAKE_SERVER,
    _drive_initialize,
    _launch_count,
    _reap,
    _spawn_stub,
)

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.pool import BackendPool
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_IDLE_SECS = int(os.environ.get("KIROCREW_KEEP_ALIVE_IDLE_SECS", "2"))


class _SweepLog:
    """Records, per idle sweep, whether the ``fake`` backend was eligible by age.

    Eligible means detached (refcount 0) and idle past the sweep's own cutoff, so a
    sweep recorded as eligible is one that would evict it unless kept alive.
    """

    def __init__(self) -> None:
        self.eligible_sweeps = 0
        self.evicted = 0

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = BackendPool.evict_idle
        log = self

        async def recording(pool, older_than_secs, **kwargs):  # noqa: ANN001, ANN202
            if kwargs.get("include_pinned"):
                return await original(pool, older_than_secs, **kwargs)
            cutoff = time.monotonic() - older_than_secs
            eligible = any(
                b.pool_key.server_name == "fake" and b.refcount == 0 and b.last_used_at < cutoff
                for b in list(pool._backends.values())
            )
            evicted = await original(pool, older_than_secs, **kwargs)
            if eligible:
                log.eligible_sweeps += 1
                log.evicted += evicted
            return evicted

        monkeypatch.setattr(BackendPool, "evict_idle", recording)


async def _run(tmp_path: Path, sock: Path, keep_alive: frozenset[str], sweeps: _SweepLog) -> int:
    work_dir = tmp_path / "ws"
    work_dir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    launch_log = tmp_path / "launches.txt"

    def _resolver(_key: object) -> tuple[str, list[str], dict[str, str], str]:
        import sys

        return (sys.executable, [str(_FAKE_SERVER), str(launch_log)], {}, str(work_dir))

    stop = asyncio.Event()
    daemon = asyncio.create_task(
        gw.run_gatewayd(
            socket_path=sock,
            max_backends=8,
            idle_timeout_secs=_IDLE_SECS,  # flake-ok: the idle timeout is the property under test, not a wait bound
            stop_event=stop,
            target_resolver=_resolver,
            keep_alive_servers=keep_alive,
        )
    )
    procs: list[asyncio.subprocess.Process] = []
    try:
        await async_wait_until(
            lambda: transport.endpoint_exists(sock),
            off_loop=True,
            describe=lambda: "gatewayd never bound its endpoint",
        )
        for i, agent in enumerate(("kirocrew", "kirocrew-worker")):
            proc = await _spawn_stub(
                socket_path=sock, server="fake", agent=agent, work_dir=work_dir, home=home
            )
            procs.append(proc)
            assert "result" in await _drive_initialize(proc, req_id=i + 1)
        assert _launch_count(launch_log) == 1, "two agents did not share one backend"

        await _reap(procs)
        procs.clear()
        # Wait for a sweep that found the detached backend idle past its cutoff,
        # so the next stub arrives after a sweep that would have drained it.
        await async_wait_until(
            lambda: sweeps.eligible_sweeps >= 1,
            timeout=_IDLE_SECS * 3 + 30,  # flake-ok: scales with the idle timeout under test
            describe=lambda: "no idle sweep ever judged the detached backend",
        )

        late = await _spawn_stub(
            socket_path=sock, server="fake", agent="kirocrew", work_dir=work_dir, home=home
        )
        procs.append(late)
        assert "result" in await _drive_initialize(late, req_id=9)
        return _launch_count(launch_log)
    finally:
        await _reap(procs)
        stop.set()
        await await_until(daemon)


@pytest.mark.asyncio
async def test_kept_alive_server_starts_one_backend_for_the_run(
    tmp_path: Path, short_sock_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps = _SweepLog()
    sweeps.install(monkeypatch)
    launches = await _run(tmp_path, short_sock_dir / "gw.sock", frozenset({"fake"}), sweeps)
    assert sweeps.evicted == 0, "an eligible sweep evicted the kept-alive backend"
    assert launches == 1, f"kept-alive server started {launches} backends"


@pytest.mark.asyncio
async def test_default_still_drains_an_idle_backend(
    tmp_path: Path, short_sock_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps = _SweepLog()
    sweeps.install(monkeypatch)
    launches = await _run(tmp_path, short_sock_dir / "gw.sock", frozenset(), sweeps)
    assert sweeps.evicted >= 1, "the eligible sweep did not evict the idle backend"
    assert launches == 2, f"default run started {launches} backends, expected a respawn"
