"""The peer's batch read route, ``POST /api/chat/work-items/pull``, end to end.

Pinned here: with the preview on, an owner reads a real bound slot's copy and its
slot booleans; a non-owner is refused; every malformed field is refused 400; with
the preview off the route answers as if it did not exist; a reply over the byte
budget flags ``more`` instead of carrying the copy; a dirty mailbox is not read;
a planted credential leaves redacted.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from redaction_structured_cases import github_classic
from test_remote_dispatch_work_item import (
    HUB_CONDUCTOR,
    _call,
    _report,
    _work_item,
    preview,
    state,
    worker_routes,
)

from kiro_crew import remote_dispatch_mailbox as mailbox
from kiro_crew import work_ledger as wl
from kiro_crew.dashboard.handlers import remote_ingest_peer, remote_work_items_pull

__all__ = ["preview", "state", "worker_routes"]

PATH = "/api/chat/work-items/pull"


def _app(st: Any, *, owner: bool = True) -> web.Application:
    from dashboard_owner_helpers import as_owner

    from kiro_crew.dashboard import chat_handlers

    app = web.Application()
    app["state"] = st
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_post(PATH, remote_work_items_pull.api_chat_work_items_pull)
    if owner:
        return as_owner(app)

    @web.middleware
    async def _app_token(request, handler):
        request["user"] = "local-app"
        request["app"] = "notes"
        return await handler(request)

    app.middlewares.append(_app_token)
    return app


async def _pull(st: Any, body: Any, *, owner: bool = True) -> tuple[int, Any]:
    async with TestClient(TestServer(_app(st, owner=owner))) as client:
        resp = await client.post(PATH, json=body)
        ctype = resp.headers.get("Content-Type", "")
        return resp.status, await resp.json() if "json" in ctype else None


async def _bound(st: Any) -> str:
    status, created = await _call(st, "POST", "/api/chat/slots", {"work_item": _work_item()})
    assert status == 200, created
    return str(created["key"])


def _hint(slot: str, **extra: Any) -> dict[str, Any]:
    return {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": slot, **extra}}}


@pytest.mark.asyncio
async def test_an_owner_reads_a_bound_slots_news_and_booleans(state, preview, worker_routes):
    slot = await _bound(state)
    status, body = await _report(slot, {"status": "progress", "summary": "going"})
    assert status == 200, body
    status, body = await _pull(state, _hint(slot))
    assert status == 200, body
    row = body["items"]["it_0000abcd"]
    assert row["found"] and row["live"] and row["running"] is False
    assert row["pending_approval"] is False and row["trust_active"] is False
    assert row["change"]["work_item"]["summary"] == "going"
    report = [e for e in row["change"]["events"] if e["kind"] == "report"][-1]
    cursor = _hint(
        slot,
        after=[report["id"]],
        stamp=row["change"]["work_item"]["last_report_at"],
        digest=remote_ingest_peer.worker_digest(row["change"]["work_item"]),
    )
    status, body = await _pull(state, cursor)
    assert status == 200 and "change" not in body["items"]["it_0000abcd"]


@pytest.mark.asyncio
async def test_a_reply_over_the_byte_budget_flags_more(state, preview, worker_routes, monkeypatch):
    slot = await _bound(state)
    await _report(slot, {"status": "progress", "summary": "going"})
    monkeypatch.setattr(remote_work_items_pull, "MAX_CHANGES_BYTES", 10)
    status, body = await _pull(state, _hint(slot))
    assert status == 200
    row = body["items"]["it_0000abcd"]
    assert row["more"] is True and "change" not in row


@pytest.mark.asyncio
async def test_a_planted_credential_leaves_redacted(state, preview, worker_routes):
    slot = await _bound(state)
    planted = github_classic(seed=7)
    await _report(slot, {"status": "progress", "summary": f"token {planted}"})
    status, body = await _pull(state, _hint(slot))
    assert status == 200
    assert planted not in str(body)


@pytest.mark.asyncio
async def test_a_non_owner_is_refused(state, preview):
    slot = await _bound(state)
    status, _body = await _pull(state, _hint(slot), owner=False)
    assert status in (401, 403)


@pytest.mark.asyncio
async def test_preview_off_answers_as_if_the_route_did_not_exist(state, preview):
    preview["on"] = False
    status, body = await _pull(state, _hint("chat-1"))
    assert status == 404 and body is None


@pytest.mark.parametrize(
    "body",
    [
        {"items": {}},
        {"conductor_key": "", "items": {}},
        {"conductor_key": "x" * (mailbox.MAX_CONDUCTOR_KEY_CHARS + 1), "items": {}},
        {"conductor_key": HUB_CONDUCTOR, "items": []},
        {
            "conductor_key": HUB_CONDUCTOR,
            "items": {f"it_{n:08x}": {"slot": "s"} for n in range(33)},
        },
        {"conductor_key": HUB_CONDUCTOR, "items": {"nope": {"slot": "s"}}},
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": "s"}},
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": ""}}},
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": "s", "after": "x"}}},
        {
            "conductor_key": HUB_CONDUCTOR,
            "items": {"it_0000abcd": {"slot": "s", "after": ["0" * 16] * 17}},
        },
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": "s", "after": ["x"]}}},
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": "s", "stamp": 5}}},
        {"conductor_key": HUB_CONDUCTOR, "items": {"it_0000abcd": {"slot": "s", "digest": 5}}},
        {"conductor_key": HUB_CONDUCTOR, "items": {}, "held_skip": "s"},
        {"conductor_key": HUB_CONDUCTOR, "items": {}, "held_skip": [5]},
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_body_is_refused(state, preview, body):
    status, reply = await _pull(state, body)
    assert status == 400, reply


@pytest.mark.asyncio
async def test_a_dirty_mailbox_is_not_read(state, preview, monkeypatch):
    slot = await _bound(state)
    monkeypatch.setattr(wl, "cache_dirty", lambda board: "dirty")
    status, _body = await _pull(state, _hint(slot))
    assert status != 200


@pytest.mark.asyncio
async def test_a_failed_mailbox_read_answers_503(state, preview, monkeypatch):
    slot = await _bound(state)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise OSError("disk")

    monkeypatch.setattr(remote_work_items_pull, "_read", broken)
    status, body = await _pull(state, _hint(slot))
    assert status == 503 and body["code"] == "ledger_read_failed"


def test_slot_flags_read_running_and_pending_approval():
    pending = SimpleNamespace(done=lambda: False)
    slot = SimpleNamespace(
        key="chat-1",
        turn_running=True,
        _approval_futures={"a": pending},
        _coordinator_approvals=None,
    )
    st = SimpleNamespace(_slots={"chat-1": slot})
    assert remote_work_items_pull._slot_flags(st, "chat-1") == {
        "live": True,
        "running": True,
        "pending_approval": True,
    }
    linked = SimpleNamespace(
        key="chat-2",
        turn_running=False,
        linked_turn_running=True,
        _approval_futures={},
        _coordinator_approvals=lambda key: [{"id": 1}],
    )
    st = SimpleNamespace(_slots={"chat-2": linked})
    assert remote_work_items_pull._slot_flags(st, "chat-2") == {
        "live": True,
        "running": True,
        "pending_approval": True,
    }
