"""The record route's ``work_dispatch`` rule: a dispatch create never opens a ledger.

``docs/request-for-change/rfc-work-dispatch.md``, record-route amendment. Every case
drives the real ``POST /api/work-ledger/record`` handler against the real store in
a private data home. The rule reads only the verified caller key and the store,
never which backend runs the session, so one caller shape covers kiro, KAS and the
array backends alike.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew import mcp_work, validation
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import work_ledger as routes
from kiro_crew.dashboard.handlers import work_ledger_conductor

pytestmark = pytest.mark.asyncio

CONDUCTOR = "chat-d-conductor"
_ACCEPT = {"kind": "human_approval"}


@pytest.fixture(autouse=True)
def _open_route(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """A private data home, and the entry gates opened (their own suites cover them)."""
    mp = _floor_monkeypatch
    mp.setenv("KIROCREW_HOME", str(tmp_path / "home"))

    async def _recognized(*a: Any, **k: Any) -> None:
        return None

    mp.setattr(routes, "_recognize_session", _recognized)
    mp.setattr(routes, "_is_restricted_session", lambda *a: False)
    mp.setattr(routes, "reaches_a_channel", lambda state, sk: False)
    mp.setattr(routes.crew_log_emit, "enabled", lambda: True)
    mp.setattr(routes, "unit_for_session_key", lambda sessions, key: f"unit:{key}")
    mp.setattr(routes.crew_log_emit, "on_work_recorded", lambda unit, data: True)


def _req(body: dict[str, Any]) -> web.Request:
    app = web.Application()
    state = MagicMock()
    state.get_slot = MagicMock(return_value=None)
    app["state"] = state
    req = make_mocked_request(
        "POST", "/api/work-ledger/record", app=app, headers={"X-Session-Key": CONDUCTOR}
    )
    req["internal_auth"] = True
    req.json = AsyncMock(return_value=body)  # type: ignore[method-assign]
    return req


async def _record(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    resp = await routes.api_work_ledger_record(_req(body))
    return resp.status, json.loads(resp.text)


def _dispatch_create() -> dict[str, Any]:
    return {"action": "create", "title": "t", "acceptance": _ACCEPT, "dispatch": True}


async def test_a_dispatch_create_without_a_ledger_is_refused_and_opens_none() -> None:
    status, body = await _record(_dispatch_create())
    assert (status, body["code"]) == (404, wl.CODE_NO_LEDGER)
    assert wl.read_conductor(CONDUCTOR) is None
    assert wl.list_work_items(CONDUCTOR) == []


async def test_the_refusal_names_goal_and_not_create() -> None:
    _, body = await _record(_dispatch_create())
    assert "action=goal" in body["error"]
    assert "action=create" not in body["error"]


@pytest.mark.parametrize("mark", [True, False, None, "yes"])
async def test_any_presence_of_the_mark_counts(mark: Any) -> None:
    """The mark can only add a refusal, so its value does not matter."""
    status, _ = await _record({**_dispatch_create(), "dispatch": mark})
    assert status == 404
    assert wl.read_conductor(CONDUCTOR) is None


async def test_a_dispatch_create_with_a_ledger_goes_through() -> None:
    status, _ = await _record({"action": "goal", "goal": "drive the fleet"})
    assert status == 200
    status, body = await _record(_dispatch_create())
    assert status == 200, body
    (item,) = wl.list_work_items(CONDUCTOR)
    assert item.item_id == body["item"]["item_id"]


async def test_a_ledger_purged_before_the_write_is_not_recreated(
    _floor_monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep's real purge lands after the request is admitted and before the
    write: the marked create is refused and no ledger comes back."""
    # A finished board, the only kind the purge removes: one item, closed.
    status, body = await _record({"action": "create", "title": "t", "acceptance": _ACCEPT})
    assert status == 200
    item_id = body["item"]["item_id"]
    status, _ = await _record({"action": "close", "item_id": item_id, "state": "abandoned"})
    assert status == 200
    real_snapshot = wl.snapshot_for_write

    def _purge_then_snapshot(key: str, **kw: Any) -> Any:
        assert wl.purge_conductor(key, allow_unreadable=False, idle_for=timedelta(0))
        return real_snapshot(key, **kw)

    _floor_monkeypatch.setattr(wl, "snapshot_for_write", _purge_then_snapshot)
    status, body = await _record(_dispatch_create())
    assert (status, body["code"]) == (404, wl.CODE_NO_LEDGER)
    assert wl.read_conductor(CONDUCTOR) is None
    assert not wl.conductor_dir(CONDUCTOR).exists()


async def test_a_plain_create_still_opens_a_ledger() -> None:
    status, body = await _record({"action": "create", "title": "t", "acceptance": _ACCEPT})
    assert status == 200, body
    assert wl.read_conductor(CONDUCTOR) is not None


def test_the_work_tool_cannot_send_the_mark() -> None:
    """``kirocrew-work`` forwards only ``_RECORD_FIELDS``, and its schema refuses
    the field, so the unmarked path stays the work tool's alone."""
    assert work_ledger_conductor.DISPATCH_FIELD not in mcp_work._RECORD_FIELDS
    with pytest.raises(validation.ValidationError):
        validation.validate_tool_args(
            {"action": "create", "dispatch": True}, validation.WORK_LEDGER_RECORD_SCHEMA
        )
