"""Unacking a cron note takes back only the acked item its cron ack added.

``POST /api/crons/<id>/ack`` appends the note's summary to the job's
``acked_items`` (what the next run is told not to repeat) and marks the note
read. ``POST /api/notifications/unack`` removed the job's newest item for ANY
cron note, on every call: a note read from the bell added nothing, so its
unack deleted an acknowledgement recorded elsewhere (Slack), and a repeated
unack emptied the list.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from dashboard_owner_helpers import as_owner
from handlers_messaging_coverage_helpers import _payload, _Req, _run, _state

import kiro_crew.dashboard.handlers.messaging as messaging
from kiro_crew.cron import CronService, CronStoreBusy
from kiro_crew.dashboard.cron_note_ack import (
    CRON_ACK_ITEM_KEY,
    mark_cron_item_recorded,
    recorded_cron_item,
)
from kiro_crew.dashboard.handlers.cron import api_cron_ack
from kiro_crew.notifications.bus import NotificationBus, NotificationPayload
from kiro_crew.testing.wait import async_wait_until, await_until


def _cron_note(**extra: Any) -> dict[str, Any]:
    return {"ts": "1", "kind": "cron", "job_id": "j1", **extra}


def _record(ack_id: str = "a1") -> dict[str, Any]:
    return {CRON_ACK_ITEM_KEY: {"job_id": "j1", "ack_id": ack_id}}


def _unack(state: Any) -> dict[str, Any]:
    return _payload(_run(messaging.api_notification_unack, _Req(state, {"ts": "1"})))


def _unack_state(note: dict[str, Any], unack: AsyncMock | None = None) -> Any:
    state = _state(_notification_log=[note], unack_notification=AsyncMock(return_value=True))
    state.crons.unack_job_async = unack or AsyncMock(return_value=True)
    # A real state holds no lock table until the first transition makes one;
    # the mock would otherwise hand back an attribute of its own.
    state._cron_note_ack_locks = None
    return state


def test_unacking_a_note_read_from_the_bell_leaves_the_job_items_alone() -> None:
    state = _unack_state(_cron_note(acked=True))
    assert _unack(state)["ok"] is True
    state.crons.unack_job_async.assert_not_awaited()


def test_the_unack_names_the_exact_entry_and_runs_once() -> None:
    note = _cron_note(acked=True, **_record())
    state = _unack_state(note)
    assert _unack(state)["ok"] is True
    assert CRON_ACK_ITEM_KEY not in note
    _unack(state)
    _unack(state)
    state.crons.unack_job_async.assert_awaited_once_with("j1", "a1")


def test_a_busy_store_keeps_the_record_for_the_next_unack() -> None:
    note = _cron_note(acked=True, **_record())
    state = _unack_state(note, AsyncMock(side_effect=CronStoreBusy("busy")))
    assert _unack(state)["ok"] is True
    assert recorded_cron_item(state, "1") == ("j1", "a1")

    state.crons.unack_job_async = AsyncMock(return_value=True)
    _unack(state)
    state.crons.unack_job_async.assert_awaited_once_with("j1", "a1")
    assert CRON_ACK_ITEM_KEY not in note


def test_a_record_naming_another_job_is_not_honoured() -> None:
    state = _state(
        _notification_log=[_cron_note(**{CRON_ACK_ITEM_KEY: {"job_id": "other", "ack_id": "a1"}})]
    )
    assert recorded_cron_item(state, "1") is None


def test_a_record_of_the_wrong_shape_is_not_honoured() -> None:
    shapes = ("j1", ["j1", "x"], {"job_id": "j1"}, {"job_id": "j1", "ack_id": 3})
    for record in (*shapes, {"job_id": "j1", "ack_id": ""}, {"job_id": "j1", "item": "x"}):
        state = _state(_notification_log=[_cron_note(**{CRON_ACK_ITEM_KEY: record})])
        assert recorded_cron_item(state, "1") is None


def test_the_record_is_not_set_for_a_note_of_another_job() -> None:
    note = _cron_note()
    mark_cron_item_recorded(_state(_notification_log=[note]), "1", "other", "a1")
    assert CRON_ACK_ITEM_KEY not in note


def test_a_pushed_note_starts_with_no_record() -> None:
    notes: list[dict[str, Any]] = []
    NotificationBus(sink=notes.append).push(
        NotificationPayload(
            source="system",
            channel="system.cron",
            title="Run",
            body="",
            meta={"job_id": "j1", CRON_ACK_ITEM_KEY: {"job_id": "j1", "ack_id": "x"}},
        )
    )
    assert CRON_ACK_ITEM_KEY not in notes[0]


# -- the store removes the entry its ack added, by id ----------------------------- #


def _items(svc: CronService, job_id: str) -> list[str]:
    return [j.acked_items for j in svc._jobs if j.id == job_id][0]


def test_the_store_removes_the_named_entry_and_keeps_a_later_one(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    svc.ack_job(job.id, "from the dashboard", "dash-1")
    svc.ack_job(job.id, "from slack")

    assert svc.unack_job(job.id, "dash-1") is True
    assert svc.unack_job(job.id, "dash-1") is False
    assert _items(CronService(base_dir=tmp_path), job.id) == ["from slack"]


def test_an_entry_aged_out_does_not_take_a_later_equal_summary(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    svc.ack_job(job.id, "no new items", "old")
    for n in range(20):
        svc.ack_job(job.id, f"run {n}", f"r{n}")
    svc.ack_job(job.id, "no new items", "new")

    assert svc.unack_job(job.id, "old") is False
    assert _items(svc, job.id)[-1] == "no new items"
    assert svc.unack_job(job.id, "new") is True
    assert "no new items" not in _items(CronService(base_dir=tmp_path), job.id)


def test_ids_that_do_not_line_up_name_no_entry(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    svc.ack_job(job.id, "first", "a1")
    svc.ack_job(job.id, "second", "a2")
    held = [j for j in svc._jobs if j.id == job.id][0]
    held.acked_items.append("added by a writer that kept no id")

    assert svc.unack_job(job.id, "a2") is False
    assert len(_items(svc, job.id)) == 3


def test_items_changed_without_the_ids_leave_the_ids_naming_no_entry(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    for n in range(20):
        svc.ack_job(job.id, f"run {n}", f"r{n}")
    held = [j for j in svc._jobs if j.id == job.id][0]
    # What an older build does: append and trim the items, keep the ids as is.
    held.acked_items = [*held.acked_items, "from an older gateway"][-20:]
    svc._save()

    reloaded = CronService(base_dir=tmp_path)
    assert reloaded.unack_job(job.id, "r1") is False
    assert _items(reloaded, job.id)[0] == "run 1"


def test_a_summary_ending_in_half_an_emoji_acks_and_unacks(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    # What `body.slice(0, 200)` leaves when character 200 starts an emoji.
    cut = "x" * 199 + "\ud83d"

    assert svc.ack_job(job.id, cut, "a1") is True
    reloaded = CronService(base_dir=tmp_path)
    assert reloaded.unack_job(job.id, "a1") is True
    assert _items(CronService(base_dir=tmp_path), job.id) == []


@pytest.mark.xfail(strict=True, reason="#29223: equal summaries keep the digest matching")
def test_an_older_rewrite_of_identical_summaries_invalidates_the_ids(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    for n in range(20):
        svc.ack_job(job.id, "no new items", f"r{n}")
    held = [j for j in svc._jobs if j.id == job.id][0]
    held.acked_items = [*held.acked_items, "no new items"][-20:]
    svc._save()

    reloaded = CronService(base_dir=tmp_path)
    reloaded.unack_job(job.id, "r0")
    assert len(_items(reloaded, job.id)) == 20


@pytest.mark.parametrize("stored", [None, "a1", [1, 2], {"a": 1}, ["a1", None]])
def test_malformed_stored_ids_load_as_none_and_the_store_still_saves(tmp_path, stored) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    svc.ack_job(job.id, "seen", "a1")
    path = tmp_path / "crons.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["jobs"][0]["acked_item_ids"] = stored
    path.write_text(json.dumps(doc), encoding="utf-8")

    reloaded = CronService(base_dir=tmp_path)
    assert [j.acked_item_ids for j in reloaded._jobs] == [[]]
    assert reloaded.unack_job(job.id, "a1") is False
    assert reloaded.ack_job(job.id, "seen again", "a2") is True
    assert _items(CronService(base_dir=tmp_path), job.id) == ["seen", "seen again"]


def test_the_store_still_removes_the_newest_without_an_id(tmp_path) -> None:
    svc = CronService(base_dir=tmp_path)
    job = svc.add_job(name="digest", message="m", every_secs=3600)
    svc.ack_job(job.id, "first")
    svc.ack_job(job.id, "second")

    assert svc.unack_job(job.id) is True
    assert _items(svc, job.id) == ["first"]


# -- the cron ack route ----------------------------------------------------------- #

_TS = "2026-05-13T00:00:00Z"


def _cron_app(note: dict[str, Any], ack: AsyncMock) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(
        crons=SimpleNamespace(ack_job_async=ack),
        push_refresh=MagicMock(),
        ack_notification=AsyncMock(),
        _notification_log=[note],
    )
    app.router.add_route("*", "/api/crons/{job_id}/ack", api_cron_ack)
    return as_owner(app)


@pytest.mark.asyncio
async def test_the_cron_ack_route_records_the_entry_id_and_no_summary_text() -> None:
    note = _cron_note(ts=_TS)
    ack = AsyncMock(return_value=True)
    app = _cron_app(note, ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/api/crons/j1/ack", json={"summary": "token sk-live-123", "ts": _TS}
        )

    assert response.status == 200
    record = note[CRON_ACK_ITEM_KEY]
    assert set(record) == {"job_id", "ack_id"} and record["job_id"] == "j1"
    ack.assert_awaited_once_with("j1", "token sk-live-123", record["ack_id"])
    assert "sk-live-123" not in json.dumps(note)


@pytest.mark.asyncio
async def test_a_repeated_cron_ack_adds_the_item_once() -> None:
    note = _cron_note(ts=_TS)
    ack = AsyncMock(return_value=True)
    app = _cron_app(note, ack)
    body = {"summary": "seen", "ts": _TS}

    async with TestClient(TestServer(app)) as client:
        first = await client.post("/api/crons/j1/ack", json=body)
        second = await client.post("/api/crons/j1/ack", json=body)
        second_body = await second.json()

    assert first.status == 200 and second.status == 200
    assert second_body == {"ok": True}
    ack.assert_awaited_once()
    assert app["state"].ack_notification.await_count == 2


def test_an_unack_with_a_non_string_ts_answers_as_before() -> None:
    state = _unack_state(_cron_note(acked=True, **_record()))
    result = _payload(_run(messaging.api_notification_unack, _Req(state, {"ts": ["1"]})))
    assert result == {"ok": True}
    state.crons.unack_job_async.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_cron_ack_with_a_non_string_ts_answers_as_before() -> None:
    note = _cron_note(ts=_TS)
    ack = AsyncMock(return_value=True)
    app = _cron_app(note, ack)

    async with TestClient(TestServer(app)) as client:
        response = await client.post("/api/crons/j1/ack", json={"summary": "seen", "ts": [_TS]})

    assert response.status == 200
    assert CRON_ACK_ITEM_KEY not in note


@pytest.mark.asyncio
async def test_overlapping_cron_acks_add_the_item_once() -> None:
    note = _cron_note(ts=_TS)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_ack(*_args: Any) -> bool:
        entered.set()
        await await_until(release, what="the test releasing the first store write")
        return True

    ack = AsyncMock(side_effect=slow_ack)
    app = _cron_app(note, ack)
    state = app["state"]
    body = {"summary": "seen", "ts": _TS}

    async with TestClient(TestServer(app)) as client:
        first = asyncio.ensure_future(client.post("/api/crons/j1/ack", json=body))
        await await_until(entered, what="the first ack reaching the store")
        second = asyncio.ensure_future(client.post("/api/crons/j1/ack", json=body))
        # Hold the first store write until the second request waits on the note.
        await async_wait_until(
            lambda: state._cron_note_ack_locks[_TS][1] == 2,
            describe=lambda: state._cron_note_ack_locks,
        )
        release.set()
        responses = await await_until(asyncio.gather(first, second), what="both acks")

    assert [r.status for r in responses] == [200, 200]
    ack.assert_awaited_once()
    assert state._cron_note_ack_locks == {}
