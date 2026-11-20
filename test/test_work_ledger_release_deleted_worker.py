"""A deleted worker chat's work-board binding is released before its transcript goes.

A worker binding is keyed on the worker's slot key, and slot keys are recycled. The
history delete therefore releases the worker's OPEN local item before the unlink, as
a recorded ``release`` entry, so a later chat under the same key is not that item's
worker and a rebuild from the record does not bind it again. A release that cannot be
recorded refuses the delete. Driven through the real routes and a real crew log.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import work_ledger_projection_helpers
from aiohttp import web
from work_ledger_projection_helpers import (
    _SLOTS,
    ACCEPTANCE,
    CONDUCTOR,
    WORKER,
    _board,
    _real_units,
    _record,
    _report,
    _Slot,
)

from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers.sessions_parts import delete_routes
from kiro_crew.history import ConversationLog

_isolated = work_ledger_projection_helpers._isolated

CODE_WORKER_RELEASE_UNRECORDED = routes.CODE_WORKER_RELEASE_UNRECORDED
release_deleted_worker = routes.release_deleted_worker

#: The worker chat's history key, as the history delete receives it.
HISTORY_KEY = f"dashboard_{WORKER}"


def _state() -> MagicMock:
    state = MagicMock()
    state.sessions = None
    return state


def _events(item_id: str) -> list[str]:
    return [event.kind for event in wl.read_events(CONDUCTOR, item_id)]


@pytest.mark.asyncio
async def test_a_deleted_workers_open_item_is_released_and_a_reused_key_is_not_its_worker(
    monkeypatch,
):
    _real_units(monkeypatch)
    item_id = await _board()
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)

    assert await release_deleted_worker(_state(), HISTORY_KEY) is None

    assert wl.read_binding(WORKER) is None
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.state == "open" and item.worker_session_key is None
    assert _events(item_id)[-1] == "release"
    # A later chat under the recycled key is nobody's worker.
    _SLOTS[WORKER] = _Slot(created_by="someone-else")
    status, body = await _report(WORKER, {"status": "progress", "summary": "new chat"})
    assert (status, body["code"]) == (403, "not_bound"), body
    assert wl.read_work_item(CONDUCTOR, item_id).summary != "new chat"


@pytest.mark.asyncio
async def test_a_rebuild_after_the_release_does_not_bind_the_deleted_worker_again(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    assert await release_deleted_worker(_state(), HISTORY_KEY) is None

    wl.rebuild_from_projection(CONDUCTOR)

    assert wl.read_binding(WORKER) is None
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.state == "open" and item.worker_session_key is None
    # The conductor can bind the open item again; a new chat is then its worker.
    _SLOTS["chat-9-next"] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": item_id, "worker_session_key": "chat-9-next"}
    )
    assert status == 200, body
    wl.rebuild_from_projection(CONDUCTOR)
    assert wl.read_binding("chat-9-next") == (CONDUCTOR, item_id)
    assert wl.read_binding(WORKER) is None


@pytest.mark.asyncio
async def test_a_release_that_is_not_recorded_refuses_and_leaves_the_binding(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    real = routes.crew_log_emit.on_work_recorded

    def _refuse_release(unit, data):
        return False if data.get("action") == "release" else real(unit, data)

    monkeypatch.setattr(routes.crew_log_emit, "on_work_recorded", _refuse_release)

    resp = await release_deleted_worker(_state(), HISTORY_KEY)

    assert resp is not None and resp.status == 409
    assert json.loads(resp.text)["code"] == CODE_WORKER_RELEASE_UNRECORDED
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    item = wl.read_work_item(CONDUCTOR, item_id)
    assert item is not None and item.worker_session_key == WORKER
    assert "release" not in _events(item_id)


@pytest.mark.asyncio
async def test_a_release_with_the_crew_log_off_refuses(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    monkeypatch.setattr(routes.crew_log_emit, "enabled", lambda: False)

    resp = await release_deleted_worker(_state(), HISTORY_KEY)

    assert resp is not None and json.loads(resp.text)["code"] == CODE_WORKER_RELEASE_UNRECORDED
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)


@pytest.mark.asyncio
async def test_a_binding_to_a_terminal_item_is_left_alone(monkeypatch):
    _real_units(monkeypatch)
    item_id = await _board()
    status, body = await _record(
        CONDUCTOR, {"action": "close", "item_id": item_id, "state": "abandoned"}
    )
    assert status == 200, body
    events_before = _events(item_id)

    assert await release_deleted_worker(_state(), HISTORY_KEY) is None

    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)
    assert wl.read_work_item(CONDUCTOR, item_id).worker_session_key == WORKER
    assert _events(item_id) == events_before


@pytest.mark.asyncio
async def test_only_the_exact_ledger_key_is_released(monkeypatch):
    """A key that only another rule folds alike (``chat:9-worker`` and ``chat_9-worker``
    share a transcript-name fold) keeps its binding; the dashboard spellings of the
    worker's own key all release it."""
    _real_units(monkeypatch)
    item_id = await _board()
    alike = "chat_9-worker"
    _SLOTS[alike] = _Slot(created_by=CONDUCTOR)
    status, body = await _record(
        CONDUCTOR, {"action": "create", "title": "item two", "acceptance": ACCEPTANCE}
    )
    assert status == 200, body
    second = body["item"]["item_id"]
    status, body = await _record(
        CONDUCTOR, {"action": "bind", "item_id": second, "worker_session_key": alike}
    )
    assert status == 200, body

    assert await release_deleted_worker(_state(), "chat:9-worker") is None
    assert wl.read_binding(alike) == (CONDUCTOR, second)
    assert wl.read_binding(WORKER) == (CONDUCTOR, item_id)

    assert await release_deleted_worker(_state(), f"dashboard:{WORKER}") is None
    assert wl.read_binding(WORKER) is None
    assert wl.read_binding(alike) == (CONDUCTOR, second)


def _delete_request(log: ConversationLog, key: str) -> web.Request:
    state = MagicMock()
    state.conversation_log = log
    state._slots = {}
    state.crons = None
    request = MagicMock(spec=web.Request)
    request.get = {}.get
    request.app = {"state": state}
    request.query = {}
    request.match_info = {"key": key}
    return request


@pytest.mark.asyncio
async def test_the_delete_releases_before_the_unlink_and_a_refusal_keeps_the_transcript(
    tmp_path,
):
    from kiro_crew.dashboard.handlers.sessions import api_session_delete

    log = ConversationLog(base_dir=tmp_path / "history")
    await asyncio.to_thread(log.append, HISTORY_KEY, "user", "hello")
    path = tmp_path / "history" / f"{HISTORY_KEY}.jsonl"
    before = path.read_bytes()
    seen: list[bool] = []
    refusal = web.json_response({"code": CODE_WORKER_RELEASE_UNRECORDED}, status=409)

    async def _refused(state, key):
        seen.append(path.exists())
        return refusal

    with (
        patch(
            "kiro_crew.dashboard.handlers.work_ledger.release_deleted_worker",
            _refused,
        ),
        patch(
            "kiro_crew.dashboard.handlers.sessions._remove_slot_for_history_key",
            new_callable=AsyncMock,
        ) as remove_slot,
    ):
        resp = await api_session_delete(_delete_request(log, HISTORY_KEY))

    assert resp.status == 409
    assert json.loads(resp.text)["code"] == CODE_WORKER_RELEASE_UNRECORDED
    assert seen == [True]
    assert path.read_bytes() == before
    remove_slot.assert_not_awaited()

    async def _released(state, key):
        seen.append(path.exists())
        return None

    with (
        patch(
            "kiro_crew.dashboard.handlers.work_ledger.release_deleted_worker",
            _released,
        ),
        patch(
            "kiro_crew.dashboard.handlers.sessions._remove_slot_for_history_key",
            new_callable=AsyncMock,
        ),
    ):
        resp = await api_session_delete(_delete_request(log, HISTORY_KEY))

    assert resp.status == 200, resp.text
    assert seen == [True, True]
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_bulk_clear_reports_a_row_it_could_not_release_and_keeps_it(tmp_path):
    from kiro_crew.dashboard.handlers import api_sessions_clear

    log = ConversationLog(base_dir=tmp_path / "history")
    await asyncio.to_thread(log.append, HISTORY_KEY, "user", "worker")
    await asyncio.to_thread(log.append, "dashboard_chat-9-other", "user", "other")
    worker_path = tmp_path / "history" / f"{HISTORY_KEY}.jsonl"
    other_path = tmp_path / "history" / "dashboard_chat-9-other.jsonl"
    before = worker_path.read_bytes()

    async def _refuse_the_worker(state, key):
        if key != HISTORY_KEY:
            return None
        return web.json_response({"code": CODE_WORKER_RELEASE_UNRECORDED}, status=409)

    with (
        patch(
            "kiro_crew.dashboard.handlers.work_ledger.release_deleted_worker", _refuse_the_worker
        ),
        patch(
            "kiro_crew.dashboard.handlers.sessions._remove_slot_for_history_key",
            new_callable=AsyncMock,
        ),
        patch("kiro_crew.dashboard.handlers.sel"),
    ):
        resp = await api_sessions_clear(_delete_request(log, ""))

    body = json.loads(resp.text)
    assert body["undeletable"] == [{"id": HISTORY_KEY, "code": CODE_WORKER_RELEASE_UNRECORDED}]
    assert body["cleared"] == 1 and body["ok"] is False
    assert worker_path.read_bytes() == before
    assert not other_path.exists()


def test_the_delete_routes_spell_the_refusal_code_the_release_answers():
    assert delete_routes.WORKER_RELEASE_UNRECORDED_CODE == CODE_WORKER_RELEASE_UNRECORDED
