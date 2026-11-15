"""``GET /api/crew-board``: concurrent reads share one build that started after they arrived."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import make_mocked_request

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import crew_board_flight as flight
from kiro_crew.dashboard.handlers import work_ledger_board as board

CONDUCTOR = "chat-1-conductor"
OTHER = "chat-9-other-conductor"
WORKER = "chat-2-worker"
OWNER = "owner-subject"

#: Longest any signal or task in these tests may take before the test fails instead of hanging.
_BOUND_S = 60.0


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


async def _signalled(event: asyncio.Event, *tasks: asyncio.Future) -> None:
    """Wait for *event* within the bound; a background task that ends first re-raises its error."""
    waiter = asyncio.ensure_future(event.wait())
    try:
        done, _ = await asyncio.wait(
            {waiter, *tasks}, timeout=_BOUND_S, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if task is not waiter:
                task.result()
        assert event.is_set(), "the signal never arrived"
    finally:
        waiter.cancel()


async def _finished(task: asyncio.Future):
    """The result of *task*, within the bound."""
    return await asyncio.wait_for(task, _BOUND_S)


async def _released(event: asyncio.Event) -> None:
    """Block a fake build until *event* is set, within the bound."""
    await asyncio.wait_for(event.wait(), _BOUND_S)


def _item(conductor: str, title: str) -> str:
    wl.ensure_conductor(conductor, goal="drive the fleet")
    made = wl.apply_conductor_action(
        conductor, "create", title=title, acceptance={"kind": "human_approval"}
    )
    return made["item"].item_id


def _state(slots: dict | None = None):
    table = {} if slots is None else slots
    return SimpleNamespace(_slots=table, get_slot=lambda key: table.get(key), owner_id=OWNER)


async def _get(conductor: str, *, user: str = OWNER, headers: dict | None = None, state=None):
    request = make_mocked_request(
        "GET",
        f"/api/crew-board?conductor={conductor}",
        headers=headers or {},
        app={"state": state if state is not None else _state()},
    )
    request["user"] = user
    request["app"] = ""
    return await board.api_work_ledger_board(request)


def _rows(response) -> list[dict]:
    return json.loads(response.body.decode())["items"]


def _titles(response) -> list[str]:
    return [row["title"] for row in _rows(response)]


@pytest.fixture
def builds(monkeypatch):
    """Count how many times the ledger's items are listed: one per build."""
    calls: list[str] = []
    real = wl.list_work_items

    def counting(key):
        calls.append(key)
        return real(key)

    monkeypatch.setattr(wl, "list_work_items", counting)
    return calls


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_build_and_one_body(builds):
    _item(CONDUCTOR, "port the gate")
    builds.clear()
    responses = await asyncio.gather(*(_get(CONDUCTOR) for _ in range(10)))
    assert len(builds) == 1
    assert {r.status for r in responses} == {200}
    assert len({r.body for r in responses}) == 1


@pytest.mark.asyncio
async def test_nothing_is_kept_after_a_build(builds):
    _item(CONDUCTOR, "port the gate")
    builds.clear()
    await _get(CONDUCTOR)
    await _get(CONDUCTOR)
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_a_stop_then_a_re_read_shows_the_new_state():
    """The board's own re-read after a Stop sees the worker gone at once (no window)."""
    item_id = _item(CONDUCTOR, "busy")
    wl.apply_conductor_action(CONDUCTOR, "bind", item_id=item_id, worker_session_key=WORKER)
    table = {WORKER: SimpleNamespace(running=True)}
    state = _state(table)
    assert _rows(await _get(CONDUCTOR, state=state))[0]["alive"] == "running"
    table.clear()  # the stop took the worker's turn and its slot away; no ledger write
    assert _rows(await _get(CONDUCTOR, state=state))[0]["alive"] == "closed"


@pytest.mark.asyncio
async def test_a_request_never_joins_a_build_that_started_before_it():
    started = asyncio.Event()
    gate = asyncio.Event()
    built: list[str] = []

    async def old():
        built.append("old")
        started.set()
        await _released(gate)
        return flight.BoardAnswer(200, b"old")

    async def new():
        built.append("new")
        return flight.BoardAnswer(200, b"new")

    first = asyncio.ensure_future(flight.shared_board("b", "b", old))
    later: asyncio.Future | None = None
    try:
        await _signalled(started, first)
        later = asyncio.ensure_future(flight.shared_board("b", "b", new))
        gate.set()
        assert (await _finished(first)).body == b"old"
        assert (await _finished(later)).body == b"new"
        assert built == ["old", "new"]
    finally:
        gate.set()
        first.cancel()
        if later is not None:
            later.cancel()


@pytest.mark.asyncio
async def test_requests_during_a_build_share_the_one_next_build():
    started = asyncio.Event()
    gate = asyncio.Event()
    built: list[int] = []

    async def build():
        built.append(1)
        if len(built) == 1:
            started.set()
            await _released(gate)
        return flight.BoardAnswer(200, b"{}")

    first = asyncio.ensure_future(flight.shared_board("b", "b", build))
    rest: list[asyncio.Future] = []
    try:
        await _signalled(started, first)
        rest = [asyncio.ensure_future(flight.shared_board("b", "b", build)) for _ in range(9)]
        gate.set()
        await _finished(asyncio.gather(first, *rest))
        assert len(built) == 2
    finally:
        gate.set()
        for task in (first, *rest):
            task.cancel()


@pytest.mark.asyncio
async def test_boards_do_not_share_a_build(builds):
    _item(CONDUCTOR, "mine")
    _item(OTHER, "theirs")
    mine, theirs = await asyncio.gather(_get(CONDUCTOR), _get(OTHER))
    assert _titles(mine) == ["mine"]
    assert _titles(theirs) == ["theirs"]


@pytest.mark.asyncio
async def test_a_non_owner_is_refused_while_an_owner_read_is_shared(builds):
    _item(CONDUCTOR, "mine")
    owner, other = await asyncio.gather(_get(CONDUCTOR), _get(CONDUCTOR, user="someone-else"))
    assert owner.status == 200
    assert other.status != 200
    assert "mine" not in other.body.decode()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 404, 409, 500, 503])
async def test_a_non_200_answer_is_not_handed_to_a_joined_waiter(status):
    built: list[int] = []

    async def build():
        built.append(1)
        await asyncio.sleep(0)
        return flight.BoardAnswer(status if len(built) == 1 else 200, b"{}")

    first, second = await asyncio.gather(
        flight.shared_board("k", "k", build), flight.shared_board("k", "k", build)
    )
    assert first.status == status
    assert second.status == 200
    assert len(built) == 2


@pytest.mark.asyncio
async def test_a_failed_build_is_not_handed_to_a_joined_waiter():
    built: list[int] = []

    async def build():
        built.append(1)
        await asyncio.sleep(0)
        if len(built) == 1:
            raise RuntimeError("boom")
        return flight.BoardAnswer(200, b"{}")

    first, second = await asyncio.gather(
        flight.shared_board("k", "k", build),
        flight.shared_board("k", "k", build),
        return_exceptions=True,
    )
    assert isinstance(first, RuntimeError)
    assert second.status == 200


@pytest.mark.asyncio
async def test_the_first_caller_going_away_does_not_cancel_the_shared_build():
    started = asyncio.Event()
    gate = asyncio.Event()

    async def build():
        started.set()
        await _released(gate)
        return flight.BoardAnswer(200, b"{}")

    first = asyncio.ensure_future(flight.shared_board("k", "k", build))
    second = asyncio.ensure_future(flight.shared_board("k", "k", build))
    try:
        await _signalled(started, first, second)
        first.cancel()
        gate.set()
        assert (await _finished(second)).status == 200
    finally:
        gate.set()
        first.cancel()
        second.cancel()


@pytest.mark.asyncio
async def test_an_unchanged_answer_is_a_304_for_a_client_that_holds_it():
    _item(CONDUCTOR, "port the gate")
    first = await _get(CONDUCTOR)
    again = await _get(CONDUCTOR, headers={"If-None-Match": first.headers["ETag"]})
    assert again.status == 304
    assert not again.body


@pytest.mark.asyncio
async def test_no_build_is_left_behind():
    async def build():
        return flight.BoardAnswer(200, b"{}")

    await asyncio.gather(*(flight.shared_board("k", "k", build) for _ in range(3)))
    assert flight._waiting == {}
    assert flight._running == {}
