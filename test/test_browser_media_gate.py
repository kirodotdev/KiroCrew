"""``/api/browser/media`` queues for the browser lock through one gateway-wide gate.

Every agent op waits on ``browser_ops._OP_LOCK``, as the media route does. Without
the gate a page could fire many GETs or POSTs, over one session or many, and the
agent op that came next would wait behind all of them. These tests pin the bound
(at most one media request waits in the lock's queue) and that normal use (polls,
clicks, an agent op running) answers as it did before: the same codes, in arrival
order, and never an answer from before a click.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.browser_harness import live_hub as hub_mod
from kiro_crew.dashboard.handlers import browser_media
from kiro_crew.dashboard.handlers import browser_ops as ops_route
from kiro_crew.dashboard.routes import browser_live as route_slice
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.testing.wait import async_wait_until

SITES = ("https://www.example.com/watch",)
KEY = "app-tunes-1"


def _waiting(lock: LoopBoundLock) -> int:
    """How many tasks wait on *lock* in the running loop (none when unused)."""
    waiters = lock._bound()._waiters
    return sum(1 for w in waiters or () if not w.done())


async def _until(check: Callable[[], bool], what: str) -> None:
    await async_wait_until(check, describe=lambda: what)


class _Tab:
    """The ``_run`` stand-in: one media element, a log of every run in order."""

    def __init__(self) -> None:
        self.paused = False
        self.seq = 0
        self.runs: list[tuple[str, str]] = []

    async def run(
        self, _hub: Any, key: str, _sup: Any, _sites: Any, action: str, _value: Any
    ) -> dict[str, Any]:
        if action == "pause":
            self.paused = True
        elif action == "play":
            self.paused = False
        self.seq += 1
        self.runs.append((key, action))
        return {"ok": True, "paused": self.paused, "seq": self.seq}


@pytest.fixture
def tab(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Tab]:
    hub_mod.reset_live_hub()
    t = _Tab()
    monkeypatch.setattr(ops_route, "app_browser_sites", lambda app: SITES)
    monkeypatch.setattr(browser_media, "_running_supervisor", lambda: SimpleNamespace())
    monkeypatch.setattr(browser_media, "_run", t.run)
    monkeypatch.setattr(browser_media.browser_live, "_audit", lambda *_a, **_k: None)
    yield t
    hub_mod.reset_live_hub()


def _app(keys: list[str], rows: list[str] | None = None) -> web.Application:
    @web.middleware
    async def as_owner(request: web.Request, handler: Any) -> Any:
        request["user"], request["app"] = "local-app", ""
        return await handler(request)

    app = web.Application(middlewares=[as_owner])
    app["state"] = SimpleNamespace(
        owner_id="",
        _slots={k: SimpleNamespace(_app="tunes") for k in keys},
        deliver_ws_owners=lambda *_a, **_k: None,
        deliver_ws_owners_bytes=lambda *_a, **_k: None,
    )
    route_slice.register(app)
    hub = hub_mod.get_live_hub()
    for i, k in enumerate(keys):
        hub.bind_target(k, f"T{i}")
    return app


async def _get(client: TestClient, key: str = KEY) -> tuple[int, dict[str, Any]]:
    resp = await client.get("/api/browser/media", params={"session_key": key})
    return resp.status, await resp.json()


async def _post(client: TestClient, action: str, key: str = KEY) -> tuple[int, dict[str, Any]]:
    resp = await client.post("/api/browser/media", json={"session_key": key, "action": action})
    return resp.status, await resp.json()


async def _agent_after_flood(
    client: TestClient, tab: _Tab, send: list[Callable[[], Any]]
) -> tuple[list[Any], int]:
    """Hold the lock, flood, queue one agent-style acquire, release.

    Returns the answers and how many media runs went before the agent op.
    """
    lock = ops_route._OP_LOCK
    await lock.acquire()
    tasks = [asyncio.create_task(s()) for s in send]
    gate = browser_media._MEDIA_GATE
    # One media request waits on the lock; every other one waits on the gate.
    await _until(lambda: _waiting(lock) == 1 and _waiting(gate) == len(send) - 1, "the flood")
    assert _waiting(lock) == 1

    before_agent: list[int] = []

    async def agent_op() -> None:
        async with lock:
            before_agent.append(len(tab.runs))

    agent = asyncio.create_task(agent_op())
    await _until(lambda: _waiting(lock) == 2, "the agent op to queue")
    lock.release()
    answers = await asyncio.gather(*tasks)
    await agent
    return answers, before_agent[0]


@pytest.mark.asyncio
async def test_a_post_and_get_flood_on_one_session_passes_an_agent_op_once(tab: _Tab) -> None:
    async with TestClient(TestServer(_app([KEY]))) as client:
        send = [lambda: _post(client, "pause") for _ in range(50)]
        send += [lambda: _get(client) for _ in range(50)]
        answers, before = await _agent_after_flood(client, tab, send)
    # The agent op waited behind the one media request already queued, not 100.
    assert before == 1
    # Nothing was refused: every request still answered, one run each, as on main.
    assert all(status == 200 and body["ok"] for status, body in answers)
    assert len(tab.runs) == 100


@pytest.mark.asyncio
async def test_a_flood_over_many_sessions_passes_an_agent_op_once(tab: _Tab) -> None:
    keys = [f"app-tunes-{i}" for i in range(50)]
    async with TestClient(TestServer(_app(keys))) as client:
        send = [lambda k=k: _get(client, k) for k in keys]
        answers, before = await _agent_after_flood(client, tab, send)
    assert before == 1
    assert all(status == 200 and body["ok"] for status, body in answers)
    assert sorted(k for k, _ in tab.runs) == sorted(keys)


@pytest.mark.asyncio
async def test_a_wait_past_the_deadline_answers_browser_busy_as_before(
    tab: _Tab, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The lock is held for the whole wait, so every request times out at any runner speed.
    monkeypatch.setattr(browser_media, "LOCK_WAIT_S", 0.2)  # flake-ok: lock held throughout
    lock = ops_route._OP_LOCK
    async with TestClient(TestServer(_app([KEY]))) as client:
        await lock.acquire()
        try:
            answers = await asyncio.gather(
                *[_get(client) for _ in range(5)], *[_post(client, "pause") for _ in range(5)]
            )
        finally:
            lock.release()
        assert (
            answers
            == [(409, {"error": "another chat is using the browser", "code": "browser_busy"})] * 10
        )
        assert tab.runs == []
        assert not browser_media._MEDIA_GATE.locked()
        # The lock is free again, so the next request runs.
        assert (await _get(client))[0] == 200


class _RecordingAsyncio:
    """``asyncio`` for ``browser_media``: each ``wait_for`` timeout recorded, and a loop
    clock the test sets, so the time spent at the gate is chosen, not slept."""

    def __init__(self) -> None:
        self.timeouts: list[float] = []
        self.now = 0.0

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    def get_running_loop(self) -> Any:
        return SimpleNamespace(time=lambda: self.now)

    async def wait_for(self, aw: Any, timeout: float) -> Any:
        self.timeouts.append(timeout)
        return await asyncio.wait_for(aw, timeout)


@pytest.mark.asyncio
async def test_the_gate_wait_counts_against_the_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = _RecordingAsyncio()
    monkeypatch.setattr(browser_media, "asyncio", rec)
    wait_s = browser_media.LOCK_WAIT_S
    gate, lock = browser_media._MEDIA_GATE, ops_route._OP_LOCK
    await gate.acquire()  # another media request is still queueing
    waiter = asyncio.create_task(browser_media._acquire_op_lock())
    await _until(lambda: _waiting(gate) == 1, "the gate wait")
    rec.now = 30.0  # the request spent 30 s at the gate
    gate.release()
    assert await waiter is True
    lock.release()
    # One deadline from arrival: the lock wait got what the gate wait left, not a fresh one.
    assert rec.timeouts == [wait_s, wait_s - 30.0]


@pytest.mark.asyncio
async def test_two_pause_clicks_run_twice_and_write_two_rows(
    tab: _Tab, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows: list[str] = []
    monkeypatch.setattr(
        browser_media.browser_live, "_audit", lambda _r, *, operation, **_k: rows.append(operation)
    )
    async with TestClient(TestServer(_app([KEY]))) as client:
        answers = await asyncio.gather(_post(client, "pause"), _post(client, "pause"))
    assert [s for s, _ in answers] == [200, 200]
    assert tab.runs == [(KEY, "pause"), (KEY, "pause")]
    assert rows == ["browser_media", "browser_media"]


@pytest.mark.asyncio
async def test_a_read_sent_after_a_click_answers_after_the_click(tab: _Tab) -> None:
    lock = ops_route._OP_LOCK
    gate = browser_media._MEDIA_GATE
    async with TestClient(TestServer(_app([KEY]))) as client:
        await lock.acquire()  # an agent op holds the browser
        poll = asyncio.create_task(_get(client))
        await _until(lambda: _waiting(lock) == 1, "the poll to queue")
        click = asyncio.create_task(_post(client, "pause"))
        await _until(lambda: _waiting(gate) == 1, "the click to queue")
        refresh = asyncio.create_task(_get(client))
        await _until(lambda: _waiting(gate) == 2, "the refresh to queue")
        lock.release()
        (_, polled), (_, clicked), (_, refreshed) = await asyncio.gather(poll, click, refresh)
    assert tab.runs == [(KEY, "state"), (KEY, "pause"), (KEY, "state")]
    assert polled["media"]["paused"] is False
    assert clicked["media"]["paused"] is True
    # The read sent after the click shows it: no answer from a run queued before it.
    assert refreshed["media"]["paused"] is True and refreshed["media"]["seq"] == 3


@pytest.mark.asyncio
async def test_normal_use_while_an_agent_op_runs_gets_every_answer_in_order(tab: _Tab) -> None:
    lock, gate = ops_route._OP_LOCK, browser_media._MEDIA_GATE
    async with TestClient(TestServer(_app([KEY, "app-radio-1"]))) as client:
        done = asyncio.Event()

        async def agent_op() -> None:
            async with lock:
                await done.wait()

        agent = asyncio.create_task(agent_op())
        await _until(lock.locked, "the agent op")
        # Two windows polling the same session, a second app's player, and a click,
        # each sent once the one before it is queued, so the arrival order is known.
        sends = [
            lambda: _get(client),
            lambda: _get(client),
            lambda: _get(client, "app-radio-1"),
            lambda: _post(client, "play"),
        ]
        tasks = []
        for i, s in enumerate(sends):
            tasks.append(asyncio.create_task(s()))
            await _until(
                lambda i=i: _waiting(lock) == 1 and _waiting(gate) == i, f"request {i} to queue"
            )
        done.set()  # the agent op ends; the media requests run
        answers = await asyncio.gather(*tasks)
        await agent
    assert all(status == 200 and body["ok"] for status, body in answers)
    # Arrival order, one run each: the gate keeps the order the lock gave before.
    assert tab.runs == [(KEY, "state"), (KEY, "state"), ("app-radio-1", "state"), (KEY, "play")]


@pytest.mark.asyncio
async def test_back_to_back_polls_with_a_free_browser_run_at_once(tab: _Tab) -> None:
    async with TestClient(TestServer(_app([KEY]))) as client:
        for _ in range(20):
            status, body = await _get(client)
            assert status == 200 and body["ok"] is True
        assert (await _post(client, "pause"))[1]["media"]["paused"] is True
        assert (await _get(client))[1]["media"]["paused"] is True
    assert len(tab.runs) == 22
    assert not browser_media._MEDIA_GATE.locked()


@pytest.mark.asyncio
async def test_a_cancelled_wait_lets_the_gate_go() -> None:
    lock = ops_route._OP_LOCK
    await lock.acquire()
    try:
        waiter = asyncio.create_task(browser_media._acquire_op_lock())
        await _until(lambda: _waiting(lock) == 1, "the wait")
        assert browser_media._MEDIA_GATE.locked()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not browser_media._MEDIA_GATE.locked()
        assert _waiting(lock) == 0
    finally:
        lock.release()
    # The next media request takes both at once.
    assert await browser_media._acquire_op_lock() is True
    lock.release()
