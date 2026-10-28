"""``GET /api/sessions`` shares one full-list computation between identical requests.

Every turn end pushes a ``history`` refresh to every open tab, so N tabs ask for the
same O(all transcripts) listing at once. Requests whose key matches an in-flight
computation await it; a request arriving after it finished, or after a transcript
write, computes again.

Each test holds the shared computation in flight on a ``threading.Event`` until every
reader has reached it, because the property only holds for readers that arrive while
it runs (see writing-tests, single-flight).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Callable
from unittest.mock import MagicMock

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_sessions
from kiro_crew.dashboard.handlers.sessions_parts import history_reads
from kiro_crew.history import ConversationLog
from kiro_crew.testing.wait import async_wait_until, await_until, finished

_BOUND = 30.0  # seconds; bounds only the worker thread's wait on the gate


def _request(log: Any, query: dict[str, str]) -> web.Request:
    state = MagicMock()
    state.conversation_log = log
    state._slots = {}
    request = MagicMock(spec=web.Request)
    request.get = {}.get  # the dashboard user: no app claim
    request.app = {"state": state}
    request.query = query
    return request


async def _call(request: web.Request) -> dict:
    resp = await api_sessions(request)
    return json.loads(resp.body.decode("utf-8"))


class _Gate:
    """Wraps a listing so it blocks on its worker thread until released, and counts."""

    def __init__(self, inner: Callable[[], list[dict]]) -> None:
        self._inner = inner
        self.release = threading.Event()
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self, *args: Any, **kwargs: Any) -> list[dict]:
        with self._lock:
            self.calls += 1
        assert self.release.wait(_BOUND), "the gate was never released"
        return self._inner(*args, **kwargs)


@pytest.fixture
def arrivals(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Every key a request has handed to the single-flight, in arrival order."""
    seen: list[tuple] = []
    real = history_reads._single_flight

    async def _counting(key: tuple, compute: Any) -> Any:
        seen.append(key)
        return await real(key, compute)

    monkeypatch.setattr(history_reads, "_single_flight", _counting)
    return seen


def _mock_log(rows: list[dict], gate: _Gate | None = None) -> MagicMock:
    log = MagicMock()
    log.list_sessions = gate or (lambda: rows)
    log._canonical_key = ConversationLog._canonical_key
    log.write_epoch.return_value = 0
    return log


@pytest.mark.asyncio
async def test_concurrent_identical_listings_compute_once(arrivals: list[tuple]) -> None:
    """Six tabs, different pages of one listing, one ``list_sessions`` call."""
    rows = [{"key": f"dashboard_chat-{i}", "title": f"t{i}"} for i in range(10)]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)

    tasks = [
        asyncio.create_task(_call(_request(log, {"limit": "3", "offset": str(off)})))
        for off in (0, 0, 3, 3, 6, 9)
    ]
    await async_wait_until(lambda: len(arrivals) == len(tasks))
    gate.release.set()
    bodies = await await_until(asyncio.gather(*tasks), what="the listing")

    assert gate.calls == 1
    assert [b["total"] for b in bodies] == [10] * 6
    assert [b["sessions"][0]["key"] for b in bodies] == [
        "dashboard_chat-0",
        "dashboard_chat-0",
        "dashboard_chat-3",
        "dashboard_chat-3",
        "dashboard_chat-6",
        "dashboard_chat-9",
    ]
    assert history_reads._LISTING_FLIGHTS.get(asyncio.get_running_loop()) == {}


@pytest.mark.asyncio
async def test_a_finished_listing_is_not_reused() -> None:
    """No time cache: two sequential requests compute twice."""
    calls = []
    log = _mock_log([])
    log.list_sessions = lambda: calls.append(1) or [{"key": "dashboard_a", "title": "a"}]

    await _call(_request(log, {}))
    await _call(_request(log, {}))

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_different_filters_compute_separately(arrivals: list[tuple]) -> None:
    rows = [{"key": "subagent_x", "title": "machine"}, {"key": "dashboard_a", "title": "a"}]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)

    full = asyncio.create_task(_call(_request(log, {})))
    user = asyncio.create_task(_call(_request(log, {"user_only": "1"})))
    await async_wait_until(lambda: len(arrivals) == 2)
    gate.release.set()
    full_body, user_body = await await_until(asyncio.gather(full, user), what="the listing")

    assert gate.calls == 2
    assert full_body["total"] == 2
    assert [s["key"] for s in user_body["sessions"]] == ["dashboard_a"]


class _Slot:
    """Only what open-slot key resolution reads."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.linked_session_key = ""
        self.channel_origin = False


@pytest.mark.asyncio
async def test_app_scope_is_part_of_the_key_and_open_slots_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An app caller and the owner never share; the open-slot set is applied per request."""
    log = _mock_log([])
    owner = _request(log, {"exclude_open": "1"})
    app = _request(log, {"exclude_open": "1"})
    app.get = {"app": "someapp"}.get
    keys: list[tuple] = []

    async def _record(key: tuple, compute: Any) -> Any:
        keys.append(key)
        return (0, [])

    monkeypatch.setattr(history_reads, "_single_flight", _record)
    await api_sessions(owner)
    await api_sessions(app)
    owner.app["state"]._slots = {"chat-1": _Slot("chat-1")}
    await api_sessions(owner)

    assert len(set(keys)) == 2
    assert keys[0] == keys[2]


@pytest.mark.asyncio
async def test_a_session_resumed_during_the_scan_is_not_listed(arrivals: list[tuple]) -> None:
    """``exclude_open`` reads the open slots after the shared scan, not before it."""
    rows = [{"key": "dashboard_chat-1", "title": "a"}, {"key": "dashboard_chat-2", "title": "b"}]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)
    request = _request(log, {"exclude_open": "1"})

    task = asyncio.create_task(_call(request))
    await async_wait_until(lambda: len(arrivals) == 1)
    request.app["state"]._slots = {"chat-1": _Slot("chat-1")}
    gate.release.set()
    body = await await_until(task, what="the listing")

    assert [s["key"] for s in body["sessions"]] == ["dashboard_chat-2"]
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_every_waiter_sees_the_error_and_the_next_request_recomputes(
    arrivals: list[tuple],
) -> None:
    def _boom() -> list[dict]:
        raise OSError("history dir unreadable")

    gate = _Gate(_boom)
    log = _mock_log([], gate)

    tasks = [asyncio.create_task(_call(_request(log, {}))) for _ in range(3)]
    await async_wait_until(lambda: len(arrivals) == 3)
    gate.release.set()
    results = await await_until(asyncio.gather(*tasks, return_exceptions=True), what="the listing")

    assert gate.calls == 1
    assert all(isinstance(r, OSError) for r in results)

    log.list_sessions = lambda: [{"key": "dashboard_a", "title": "a"}]
    body = await _call(_request(log, {}))
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_a_cancelled_waiter_does_not_cancel_the_shared_listing(
    arrivals: list[tuple],
) -> None:
    rows = [{"key": "dashboard_a", "title": "a"}]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)

    leader = asyncio.create_task(_call(_request(log, {})))
    other = asyncio.create_task(_call(_request(log, {})))
    await async_wait_until(lambda: len(arrivals) == 2)
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    gate.release.set()
    body = await await_until(other, what="the listing")

    assert gate.calls == 1
    assert body["total"] == 1


@pytest.mark.asyncio
async def test_preview_rows_are_not_written_into_the_shared_list(
    arrivals: list[tuple],
) -> None:
    rows = [{"key": "dashboard_a", "title": "a"}]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)
    log.last_message_preview.return_value = "hello"

    with_preview = asyncio.create_task(_call(_request(log, {"preview": "1"})))
    without = asyncio.create_task(_call(_request(log, {})))
    await async_wait_until(lambda: len(arrivals) == 2)
    gate.release.set()
    a, b = await await_until(asyncio.gather(with_preview, without), what="the listing")

    assert gate.calls == 1
    assert a["sessions"][0]["preview"] == "hello"
    assert "preview" not in b["sessions"][0]
    assert "preview" not in rows[0]


@pytest.mark.asyncio
async def test_a_write_between_calls_is_visible_to_the_next_call(tmp_path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard:first", "user", "hello")

    before = await _call(_request(log, {"user_only": "1"}))
    log.append("dashboard:second", "user", "hi again")
    after = await _call(_request(log, {"user_only": "1"}))

    assert [s["key"] for s in before["sessions"]] == ["dashboard_first"]
    assert sorted(s["key"] for s in after["sessions"]) == ["dashboard_first", "dashboard_second"]


@pytest.mark.asyncio
async def test_a_request_after_a_write_never_joins_a_listing_started_before_it(
    tmp_path, arrivals: list[tuple]
) -> None:
    """The write epoch is in the key, so the late reader computes its own listing."""
    log = ConversationLog(base_dir=tmp_path)
    log.append("dashboard:first", "user", "hello")
    gate = _Gate(log.list_sessions)
    log.list_sessions = gate  # type: ignore[method-assign]

    early = asyncio.create_task(_call(_request(log, {"user_only": "1"})))
    await async_wait_until(lambda: len(arrivals) == 1)
    log.append("dashboard:second", "user", "hi again")
    late = asyncio.create_task(_call(_request(log, {"user_only": "1"})))
    await async_wait_until(lambda: len(arrivals) == 2)
    gate.release.set()
    _, late_body = await await_until(asyncio.gather(early, late), what="the listing")

    assert gate.calls == 2
    assert sorted(s["key"] for s in late_body["sessions"]) == [
        "dashboard_first",
        "dashboard_second",
    ]


def _seeded_log(tmp_path: Any, count: int) -> ConversationLog:
    """*count* transcripts written through the log, every other one untitled."""
    log = ConversationLog(base_dir=tmp_path)
    for i in range(count):
        role = "user" if i % 2 else "assistant"
        log.append(f"dashboard:seeded-{i}", role, f"message {i}")
    return log


@pytest.mark.asyncio
async def test_concurrent_listings_on_a_real_log_scan_once(tmp_path, arrivals: list[tuple]) -> None:
    """Twelve tabs on four pages of a 300-transcript home share one scan."""
    log = _seeded_log(tmp_path, 300)
    real_has_messages = log.has_messages
    head_reads: list[str] = []

    def _counting_has_messages(key: str) -> bool:
        head_reads.append(key)
        return real_has_messages(key)

    log.has_messages = _counting_has_messages  # type: ignore[method-assign]
    await finished(_call(_request(log, {"user_only": "1"})), "one sequential listing")
    sequential_reads = len(head_reads)
    head_reads.clear()
    arrivals.clear()

    gate = _Gate(log.list_sessions)
    log.list_sessions = gate  # type: ignore[method-assign]
    tasks = [
        asyncio.create_task(_call(_request(log, {"user_only": "1", "limit": "50", "offset": off})))
        for off in ("0", "50", "100", "150")
        for _ in range(3)
    ]
    await async_wait_until(lambda: len(arrivals) == len(tasks))
    gate.release.set()
    bodies = await await_until(asyncio.gather(*tasks), what="twelve listings")

    assert gate.calls == 1
    assert sequential_reads > 0
    assert len(head_reads) == sequential_reads
    assert len({b["total"] for b in bodies}) == 1


@pytest.mark.asyncio
async def test_listing_flight_counters_count_joins(arrivals: list[tuple]) -> None:
    rows = [{"key": "dashboard_a", "title": "a"}]
    gate = _Gate(lambda: rows)
    log = _mock_log(rows, gate)
    started = history_reads.listing_flight_counts["listing_flight_started"]
    joined = history_reads.listing_flight_counts["listing_flight_joined"]

    tasks = [asyncio.create_task(_call(_request(log, {}))) for _ in range(12)]
    await async_wait_until(lambda: len(arrivals) == len(tasks))
    gate.release.set()
    await await_until(asyncio.gather(*tasks), what="twelve listings")

    assert history_reads.listing_flight_counts["listing_flight_started"] - started == 1
    assert history_reads.listing_flight_counts["listing_flight_joined"] - joined == 11
    assert history_reads._LISTING_FLIGHTS.get(asyncio.get_running_loop()) == {}
