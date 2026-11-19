"""The held wait between patrol pulls (W3b): peer route and hub loop.

Pinned here, on the peer (``POST /api/chat/work-items/wait``): news already past the
hub's cursor answers at once; a covered cursor answers quiet when the wait runs
out; a worker report on the mailbox releases a held wait; a full crew refuses 429;
a bad ``wait_secs`` is refused 400; the preview off answers 404.

On the hub (``remote_puller``), with a fake clock and no real sleep: news pulls the
board and waits again; a quiet wait waits again at once; one wait per crew, none
for a board that needs no pull; a 404 crew is left to the patrol pull until its
retry time; failed and fast-answered waits back off, doubling to the cap; the loop
ends when the crew has no open item left. End to end through a stub crew:
``test_remote_dispatch_puller``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from test_remote_dispatch_work_item import (
    HUB_CONDUCTOR,
    _call,
    _report,
    _work_item,
    preview,
    state,
    worker_routes,
)

from kiro_crew.dashboard.handlers import (
    remote_batch_pull,
    remote_ingest_peer,
    remote_puller,
    remote_work_items_pull,
)

__all__ = ["preview", "state", "worker_routes"]

PATH = "/api/chat/work-items/wait"
PULL = "/api/chat/work-items/pull"
ITEM = "it_0000abcd"


# ── the peer route ───────────────────────────────────────────────────────────


def _app(st: Any) -> web.Application:
    from dashboard_owner_helpers import as_owner

    app = web.Application()
    app["state"] = st
    app.router.add_post(PATH, remote_work_items_pull.api_chat_work_items_wait)
    app.router.add_post(PULL, remote_work_items_pull.api_chat_work_items_pull)
    return as_owner(app)


async def _post(st: Any, path: str, body: Any) -> tuple[int, Any]:
    async with TestClient(TestServer(_app(st))) as client:
        resp = await client.post(path, json=body)
        ctype = resp.headers.get("Content-Type", "")
        return resp.status, await resp.json() if "json" in ctype else None


async def _bound(st: Any) -> str:
    status, created = await _call(st, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200, created
    return str(created["key"])


def _body(slot: str, wait: Any = 0, **cursor: Any) -> dict[str, Any]:
    return {
        "conductor_key": HUB_CONDUCTOR,
        "items": {ITEM: {"slot": slot, **cursor}},
        "wait_secs": wait,
    }


def _count_reads(monkeypatch) -> list[int]:
    """How many times the wait route reads the mailbox, as a one-item list."""
    reads = [0]
    real = remote_work_items_pull._news_now

    async def counted(*a: Any) -> Any:
        reads[0] += 1
        return await real(*a)

    monkeypatch.setattr(remote_work_items_pull, "_news_now", counted)
    return reads


async def _cursor(st: Any, slot: str) -> dict[str, Any]:
    """The hub's cursor after a pull that filed everything the peer holds."""
    status, body = await _post(st, PULL, _body(slot))
    assert status == 200, body
    change = body["items"][ITEM]["change"]
    report = [e for e in change["events"] if e["kind"] == "report"][-1]
    return {
        "after": [report["id"]],
        "stamp": change["work_item"]["last_report_at"],
        "digest": remote_ingest_peer.worker_digest(change["work_item"]),
    }


@pytest.mark.asyncio
async def test_news_past_the_cursor_answers_at_once(state, preview, worker_routes, monkeypatch):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    reads = _count_reads(monkeypatch)
    status, body = await asyncio.wait_for(_post(state, PATH, _body(slot, 25)), 30)
    assert (status, body) == (200, {"news": True})
    # One read and no wait: the news was there.
    assert reads == [1]
    assert remote_ingest_peer.waiting_count() == 0


@pytest.mark.asyncio
async def test_a_covered_cursor_answers_quiet_when_the_wait_runs_out(state, preview, worker_routes):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    cursor = await _cursor(state, slot)
    status, body = await _post(state, PATH, _body(slot, 0, **cursor))
    assert (status, body) == (200, {"news": False})


@pytest.mark.asyncio
async def test_hub_fields_no_report_carries_are_not_news(state, preview, worker_routes):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    cursor = await _cursor(state, slot)
    # The hub kept a pr from a retired worker: its digest never matches the copy's.
    kept = {**cursor, "digest": "0" * 32}
    status, body = await _post(state, PATH, _body(slot, 0, **kept))
    assert (status, body) == (200, {"news": False})
    # The hub filed the newest id but holds an older stamp (the peer clock stepped
    # back): ids decide, so it is not news.
    stepped = {**cursor, "stamp": "2020-01-01T00:00:00+00:00"}
    status, body = await _post(state, PATH, _body(slot, 0, **stepped))
    assert (status, body) == (200, {"news": False})
    # A binding from before peer ids sends none: judged on its stamp alone.
    legacy = {"stamp": cursor["stamp"]}
    status, body = await _post(state, PATH, _body(slot, 0, **legacy))
    assert (status, body) == (200, {"news": False})


@pytest.mark.asyncio
async def test_a_worker_report_releases_a_held_wait(state, preview, worker_routes):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    cursor = await _cursor(state, slot)
    held = asyncio.ensure_future(_post(state, PATH, _body(slot, 25, **cursor)))

    async def registered() -> None:
        while remote_ingest_peer.waiting_count() == 0:
            assert not held.done(), held.result()
            await asyncio.sleep(0)

    await asyncio.wait_for(registered(), 30)
    status, reported = await _report(slot, {"status": "done", "summary": "shipped", "pr": 3})
    assert status == 200, reported
    assert await asyncio.wait_for(held, 30) == (200, {"news": True})
    assert remote_ingest_peer.waiting_count() == 0


@pytest.mark.asyncio
async def test_a_report_on_another_board_does_not_release_it(state, preview, worker_routes):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    cursor = await _cursor(state, slot)
    with remote_ingest_peer.report_waiter("hub:elsewhere") as other:
        with remote_ingest_peer.report_waiter("hub:" + HUB_CONDUCTOR) as mine:
            remote_ingest_peer.note_report("hub:elsewhere")
            assert other.is_set() and not mine.is_set()
    assert remote_ingest_peer.waiting_count() == 0
    status, body = await _post(state, PATH, _body(slot, 0, **cursor))
    assert body == {"news": False}


@pytest.mark.asyncio
async def test_a_full_crew_refuses_429(state, preview, monkeypatch):
    slot = await _bound(state)
    monkeypatch.setattr(remote_work_items_pull, "MAX_WAITERS", 0)
    status, body = await _post(state, PATH, _body(slot, 25))
    assert status == 429 and body["code"] == "too_many_waits"


@pytest.mark.asyncio
async def test_a_huge_wait_is_capped(state, preview, worker_routes, monkeypatch):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    reads = _count_reads(monkeypatch)
    status, body = await asyncio.wait_for(_post(state, PATH, _body(slot, 10**400)), 30)
    assert (status, body) == (200, {"news": True}) and reads == [1]


@pytest.mark.parametrize("wait", ["5", -1, True, None])
@pytest.mark.asyncio
async def test_a_bad_wait_is_refused(state, preview, wait):
    status, body = await _post(state, PATH, _body("chat-1", wait))
    assert status == 400, body


@pytest.mark.asyncio
async def test_preview_off_answers_404(state, preview):
    preview["on"] = False
    status, body = await _post(state, PATH, _body("chat-1"))
    assert status == 404 and body is None


def test_the_hold_fits_under_the_idle_limits():
    from kiro_crew.dashboard import slowloris
    from kiro_crew.instances.constants import DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS

    hold = remote_work_items_pull.MAX_WAIT_SECS
    assert hold + remote_puller.WAIT_SLACK_SECS < DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS
    assert hold < slowloris.KEEPALIVE_TIMEOUT
    assert remote_batch_pull.WAIT_SECS == hold
    assert remote_puller.WAIT_MAX_ITEMS == remote_work_items_pull.MAX_PULL_ITEMS


# ── the hub's wait_crew ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        ((200, {"news": True}), remote_batch_pull.WAIT_NEWS),
        ((200, {"news": False}), remote_batch_pull.WAIT_QUIET),
        ((404, None), remote_batch_pull.WAIT_UNSUPPORTED),
        # A crew that ignores the route's contract, or refuses: never read as news.
        ((200, {"ok": True}), remote_batch_pull.WAIT_FAILED),
        ((200, {"news": "yes"}), remote_batch_pull.WAIT_FAILED),
        ((429, {"code": "too_many_waits"}), remote_batch_pull.WAIT_FAILED),
        ((503, {"code": "ledger_read_failed"}), remote_batch_pull.WAIT_FAILED),
        (RuntimeError("tunnel"), remote_batch_pull.WAIT_FAILED),
    ],
)
def test_wait_crew_reads_each_answer(monkeypatch, answer, outcome):
    from kiro_crew.dashboard.handlers import remote_ingest

    sent: list[dict[str, Any]] = []

    async def peer_call(mgr, crew, method, path, *, body=None, params=None):
        sent.append({"method": method, "path": path, "body": body})
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def ready(*_a):
        return True

    async def hint(board, item):
        return {"slot": item.worker_session_key}

    monkeypatch.setattr(remote_ingest, "_routes", lambda: SimpleNamespace(peer_call=peer_call))
    monkeypatch.setattr(remote_batch_pull, "_crew_ready", ready)
    monkeypatch.setattr(remote_batch_pull, "_hint", hint)
    item = SimpleNamespace(item_id=ITEM, worker_session_key="chat-1")
    got = asyncio.run(remote_batch_pull.wait_crew(None, None, "board", "crew", [item]))
    assert got == outcome
    assert sent == [
        {
            "method": "POST",
            "path": remote_batch_pull.WAIT_PATH,
            "body": {
                "conductor_key": "board",
                "items": {ITEM: {"slot": "chat-1"}},
                "wait_secs": remote_batch_pull.WAIT_SECS,
            },
        }
    ]


def test_a_crew_not_ready_is_not_asked(monkeypatch):
    async def not_ready(*_a):
        return False

    monkeypatch.setattr(remote_batch_pull, "_crew_ready", not_ready)
    got = asyncio.run(remote_batch_pull.wait_crew(None, None, "board", "crew", []))
    assert got == remote_batch_pull.WAIT_FAILED


# ── the hub loop, on a fake clock ────────────────────────────────────────────


class _Hub:
    """The hub loop's world: a fake clock, scripted waits, a recorded pull."""

    def __init__(self, monkeypatch, outcomes: list[str], *, cycles: int, took: float = 25.0):
        self.now = 1000.0
        self.outcomes = list(outcomes)
        self.cycles = cycles
        self.took = took
        self.sleeps: list[float] = []
        self.pulls = 0
        self.waits = 0
        self.gate: asyncio.Event | None = None
        #: Set once this many waits have started.
        self.started = asyncio.Event()
        self.started_at = 0
        #: Fake seconds one pull takes.
        self.pull_took = 0.0
        monkeypatch.setattr(remote_puller, "_clock", lambda: self.now)
        monkeypatch.setattr(remote_puller, "_sleep", self.sleep)
        monkeypatch.setattr(remote_puller, "_open_by_crew", self.open_by_crew)
        monkeypatch.setattr(remote_puller, "pull_on_patrol", self.pull)
        monkeypatch.setattr(remote_batch_pull, "wait_crew", self.wait)
        monkeypatch.setattr(remote_puller, "_waits", {})
        monkeypatch.setattr(remote_puller, "_unsupported", {})

    def open_by_crew(self, board: str) -> dict[str, list[Any]]:
        if self.cycles <= 0:
            return {}
        self.cycles -= 1
        return {"crew-a": [SimpleNamespace(item_id=ITEM)]}

    async def sleep(self, secs: float) -> None:
        self.sleeps.append(secs)
        self.now += secs

    async def pull(self, state: Any, board: str) -> bool:
        self.pulls += 1
        self.now += self.pull_took
        return True

    async def wait(self, state, mgr, board, crew, items) -> str:
        self.waits += 1
        if self.started_at and self.waits >= self.started_at:
            self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        self.now += self.took
        return self.outcomes.pop(0) if self.outcomes else remote_batch_pull.WAIT_QUIET


def _run_loop() -> None:
    asyncio.run(remote_puller._wait_loop(SimpleNamespace(), "board", "crew-a"))


def test_news_pulls_the_board_and_waits_again(monkeypatch):
    hub = _Hub(monkeypatch, ["news", "quiet", "news"], cycles=3)
    _run_loop()
    assert (hub.waits, hub.pulls, hub.sleeps) == (3, 2, [])
    assert remote_puller._waits == {}


def test_the_loop_ends_when_the_crew_has_no_open_item(monkeypatch):
    hub = _Hub(monkeypatch, [], cycles=0)
    _run_loop()
    assert hub.waits == 0


def test_failed_waits_back_off_doubling_to_the_cap(monkeypatch):
    hub = _Hub(monkeypatch, ["failed"] * 9, cycles=9, took=0.0)
    _run_loop()
    assert hub.sleeps == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
    assert hub.pulls == 0


def test_a_quiet_answer_that_did_not_wait_backs_off(monkeypatch):
    hub = _Hub(monkeypatch, ["quiet", "news", "quiet"], cycles=3, took=0.5)
    _run_loop()
    # Instant quiet answers back off; one instant news (a report landed in the
    # pause) resets it.
    assert hub.sleeps == [5.0, 5.0]
    assert hub.pulls == 1


def test_a_slow_pull_does_not_hide_an_instant_answer(monkeypatch):
    hub = _Hub(monkeypatch, ["news"] * 3, cycles=3, took=0.0)
    hub.pull_took = 9.0
    _run_loop()
    # The first instant news is real; the ones after it mean a stuck cursor.
    assert hub.sleeps == [5.0, 10.0] and hub.pulls == 3


def test_news_after_a_long_pause_resets_the_backoff(monkeypatch):
    # Failures pushed the pause up; a busy worker's report then waits at each wake.
    hub = _Hub(monkeypatch, ["failed"] * 4 + ["news", "quiet"], cycles=6, took=0.0)
    real_wait = hub.wait

    async def wait(*a):
        outcome = await real_wait(*a)
        if outcome == "quiet":
            hub.now += 25.0
        return outcome

    monkeypatch.setattr(remote_batch_pull, "wait_crew", wait)
    _run_loop()
    assert hub.sleeps == [5.0, 10.0, 20.0, 40.0] and hub.pulls == 1


def test_a_held_wait_resets_the_backoff(monkeypatch):
    hub = _Hub(monkeypatch, ["failed", "failed", "quiet", "failed"], cycles=4, took=0.0)
    real_wait = hub.wait

    async def wait(*a):
        outcome = await real_wait(*a)
        if outcome == "quiet":
            hub.now += 25.0
        return outcome

    monkeypatch.setattr(remote_batch_pull, "wait_crew", wait)
    _run_loop()
    assert hub.sleeps == [5.0, 10.0, 5.0]


def test_a_404_crew_is_left_to_the_patrol_until_its_retry(monkeypatch):
    hub = _Hub(monkeypatch, ["unsupported"], cycles=10)

    async def go() -> tuple[list[str], list[str], list[str]]:
        first = await remote_puller.start_waits(SimpleNamespace(), "board")
        await asyncio.wait_for(remote_puller._waits[("board", "crew-a")], 30)
        again = await remote_puller.start_waits(SimpleNamespace(), "board")
        hub.now += remote_puller.WAIT_UNSUPPORTED_RETRY_SECS
        hub.gate = asyncio.Event()  # hold the retried wait open
        later = await remote_puller.start_waits(SimpleNamespace(), "board")
        remote_puller._waits[("board", "crew-a")].cancel()
        return first, again, later

    first, again, later = asyncio.run(go())
    assert (first, again, later) == (["crew-a"], [], ["crew-a"])
    assert hub.pulls == 0 and hub.sleeps == []


def test_one_wait_per_crew_however_often_the_patrol_ticks(monkeypatch):
    hub = _Hub(monkeypatch, [], cycles=100)
    hub.gate = asyncio.Event()
    monkeypatch.setattr(
        remote_puller,
        "_open_by_crew",
        lambda board: {
            "crew-a": [SimpleNamespace(item_id="it_00000001"), SimpleNamespace(item_id="i2")],
            "crew-b": [SimpleNamespace(item_id="it_00000003")],
        },
    )

    hub.started_at = 2

    async def go() -> tuple[list[str], list[str], int]:
        first = await remote_puller.start_waits(SimpleNamespace(), "board")
        await asyncio.wait_for(hub.started.wait(), 30)
        second = await remote_puller.start_waits(SimpleNamespace(), "board")
        running = len(remote_puller._waits)
        for task in list(remote_puller._waits.values()):
            task.cancel()
        return first, second, running

    first, second, running = asyncio.run(go())
    assert sorted(first) == ["crew-a", "crew-b"] and second == [] and running == 2
    assert hub.waits == 2


def test_a_board_that_needs_no_pull_holds_no_wait(monkeypatch):
    monkeypatch.setattr(remote_puller, "_needs_pull", lambda board: False)
    monkeypatch.setattr(remote_puller, "_waits", {})
    assert asyncio.run(remote_puller.start_waits(SimpleNamespace(), "board")) == []
    assert remote_puller._waits == {}


def test_a_broken_board_read_ends_the_loop_and_never_raises(monkeypatch):
    def broken(board):
        raise OSError("disk gone")

    monkeypatch.setattr(remote_puller, "_open_by_crew", broken)
    monkeypatch.setattr(remote_puller, "_waits", {})
    _run_loop()
    assert asyncio.run(remote_puller.start_waits(SimpleNamespace(), "board")) == []


def test_a_hung_wait_is_cut_off_and_backed_off(monkeypatch):
    hub = _Hub(monkeypatch, [], cycles=1)
    monkeypatch.setattr(remote_puller, "WAIT_SLACK_SECS", 0.0)
    monkeypatch.setattr(remote_batch_pull, "WAIT_SECS", 0.0)
    hub.gate = asyncio.Event()  # never set: the wait hangs
    _run_loop()
    assert hub.sleeps == [5.0] and hub.pulls == 0
