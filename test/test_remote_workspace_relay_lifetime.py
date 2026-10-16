"""A remote workspace relay is closed when its runtime ends or a restart replaces it."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from kiro_crew.coder import remote_worker
from kiro_crew.remote_session import relay as relay_mod
from kiro_crew.testing.ids import seq_ids
from kiro_crew.testing.wait import await_until


class _Guest:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    async def start_worker(self, worker_id, _argv, _secret) -> int:
        self.started.append(worker_id)
        return 40000

    async def stop_worker(self, worker_id) -> None:
        self.stopped.append(worker_id)


@pytest.fixture
def launch_for(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_worker, "_RUNTIME_END_POLL_SECONDS", 0.01)
    ids = seq_ids("w")
    made: list[remote_worker.RemoteWorkerLaunch] = []

    def make(guest: _Guest) -> remote_worker.RemoteWorkerLaunch:
        launch = remote_worker.RemoteWorkerLaunch("binding", AsyncMock(), tmp_path / "secrets")
        launch.install = launch.reap_stale_workers = AsyncMock()
        launch.new_worker_id = lambda: next(ids)
        launch.start_worker, launch.stop_worker = guest.start_worker, guest.stop_worker
        made.append(launch)
        return launch

    yield make
    for live in [r for r in relay_mod._LIVE if getattr(r, "launch", None) in made]:
        relay_mod._LIVE.discard(live)


async def _opened(launch) -> tuple[object, str]:
    await launch.workspace_server("/workspace/app")
    return launch.relay, (await launch.relay._spare)[0]


async def _settle() -> None:
    while remote_worker._RELAY_TASKS:
        await asyncio.gather(*list(remote_worker._RELAY_TASKS))
        await asyncio.sleep(0)


def _closed(relay) -> bool:
    return relay not in relay_mod._LIVE and not relay.token_path.exists()


@pytest.mark.asyncio
async def test_relay_closes_when_its_runtime_ends(launch_for):
    guest = _Guest()
    launch = launch_for(guest)
    relay, spare = await _opened(launch)
    polled = asyncio.Event()
    ended = False

    def is_alive() -> bool:
        polled.set()
        return not ended

    launch.close_relay_when_ended(is_alive)
    await await_until(polled, what="runtime liveness polled")
    assert not _closed(relay)
    ended = True
    await await_until(asyncio.ensure_future(_settle()), what="relay close tasks")
    assert _closed(relay) and guest.stopped == [spare] and launch.relay is None


@pytest.mark.asyncio
async def test_a_restart_closes_the_relay_it_replaces(launch_for):
    guest = _Guest()
    launch = launch_for(guest)
    first, first_spare = await _opened(launch)
    second, _ = await _opened(launch)
    assert _closed(first) and guest.stopped == [first_spare] and not _closed(second)
    # The replaced runtime's watcher closing its own relay leaves the new one serving.
    await launch.close_relay(first)
    assert launch.relay is second and not _closed(second)
    await launch.close_relay()
    assert _closed(second)


@pytest.mark.asyncio
async def test_ended_runtimes_never_fill_the_live_relay_cap(launch_for, monkeypatch):
    monkeypatch.setattr(relay_mod, "_MAX_LIVE_RELAYS", 3)
    guest = _Guest()
    for _ in range(8):
        launch = launch_for(guest)
        await _opened(launch)
        launch.close_relay_when_ended(lambda: False)
        await await_until(asyncio.ensure_future(_settle()), what="relay close tasks")
    assert sorted(guest.stopped) == sorted(guest.started) and len(guest.started) == 8


@pytest.mark.asyncio
async def test_a_slow_close_finishes_in_the_background(launch_for, monkeypatch):
    monkeypatch.setattr(remote_worker, "_RELAY_CLOSE_WAIT_SECONDS", 0.01)
    launch = launch_for(_Guest())
    release, done = asyncio.Event(), asyncio.Event()

    class _Slow:
        name = "slow"

        async def close(self) -> None:
            await release.wait()
            done.set()

    launch.relay = _Slow()
    await await_until(asyncio.ensure_future(launch.close_relay()), what="relay close handoff")
    assert launch.relay is None and not done.is_set()
    release.set()
    await await_until(asyncio.ensure_future(_settle()), what="relay close tasks")
    assert done.is_set()
