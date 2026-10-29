"""The access routes refuse before they act, and map each access-set failure to one status.

The owner gate and the access set are stubbed; what is real is each handler's
order of checks and the status it answers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.shared_crew import access_set
from kiro_crew.shared_crew import routes_access as ra


def _req(method: str, user_id: str = "42", state: Any = None) -> web.Request:
    app = web.Application()
    if state is not None:
        app["state"] = state
    req = make_mocked_request(method, "/x", match_info={"user_id": user_id}, app=app)
    return req


def _body(resp: web.Response) -> dict[str, Any]:
    assert resp.text is not None
    return json.loads(resp.text)


@pytest.fixture()
def owner(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allowed(_r: web.Request) -> None:
        return None

    monkeypatch.setattr(ra, "_owner_refusal", allowed)
    monkeypatch.setattr(ra, "_actor", lambda _r: "local:owner")
    monkeypatch.setattr(access_set, "local_authorization", lambda: True)


HANDLERS = [ra.api_approve, ra.api_dismiss, ra.api_remove]


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS + [ra.api_access, ra.api_ack])
async def test_a_non_owner_is_refused_before_anything_runs(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    async def refuse(_r: web.Request) -> web.Response:
        return web.json_response({"code": "owner_only"}, status=403)

    monkeypatch.setattr(ra, "_owner_refusal", refuse)
    monkeypatch.setattr(access_set, "approve", pytest.fail)
    resp = await handler(_req("POST"))
    assert resp.status == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS)
async def test_approve_dismiss_remove_wait_for_local_authorization(
    owner: None, monkeypatch: pytest.MonkeyPatch, handler: Any
) -> None:
    monkeypatch.setattr(access_set, "local_authorization", lambda: False)
    resp = await handler(_req("POST"))
    assert resp.status == 409
    assert _body(resp)["code"] == ra.CODE_LOCAL_AUTHORIZATION_OFF


@pytest.mark.asyncio
@pytest.mark.parametrize("handler", HANDLERS)
@pytest.mark.parametrize("user_id", ["0", "abc", "1" * 21, ""])
async def test_a_malformed_user_id_is_400(owner: None, handler: Any, user_id: str) -> None:
    resp = await handler(_req("POST", user_id=user_id))
    assert resp.status == 400
    assert _body(resp)["code"] == "bad_user_id"


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (access_set.CODE_NOT_WAITING, 404),
        (access_set.CODE_NOT_APPROVED, 404),
        (access_set.CODE_ACK_REQUIRED, 409),
        (access_set.CODE_UNREADABLE, 503),
        ("something_else", 400),
    ],
)
def test_access_failures_map_to_one_status(code: str, status: int) -> None:
    resp = ra._access_failure(access_set.AccessError(code))
    assert resp.status == status
    assert _body(resp)["code"] == code


def _raise(code: str) -> Any:
    def fail(*_a: Any) -> Any:
        raise access_set.AccessError(code)

    return fail


@pytest.mark.asyncio
async def test_approve_and_dismiss_answer_the_access_set(
    owner: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(access_set, "approve", lambda uid, actor: {"user_id": uid, "by": actor})
    monkeypatch.setattr(access_set, "dismiss", lambda _uid, _actor: None)
    assert _body(await ra.api_approve(_req("POST"))) == {
        "approved": {"user_id": "42", "by": "local:owner"}
    }
    assert _body(await ra.api_dismiss(_req("POST"))) == {"dismissed": "42"}

    monkeypatch.setattr(access_set, "approve", _raise(access_set.CODE_ACK_REQUIRED))
    monkeypatch.setattr(access_set, "dismiss", _raise(access_set.CODE_NOT_WAITING))
    assert (await ra.api_approve(_req("POST"))).status == 409
    assert (await ra.api_dismiss(_req("POST"))).status == 404


@pytest.mark.asyncio
async def test_remove_answers_the_access_set(owner: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def removed(uid: str, _actor: str) -> dict[str, Any]:
        return {"removed": uid}

    async def not_approved(_uid: str, _actor: str) -> dict[str, Any]:
        raise access_set.AccessError(access_set.CODE_NOT_APPROVED)

    monkeypatch.setattr(access_set, "remove_approval", removed)
    assert _body(await ra.api_remove(_req("DELETE"))) == {"removed": "42"}
    monkeypatch.setattr(access_set, "remove_approval", not_approved)
    assert (await ra.api_remove(_req("DELETE"))).status == 404


@pytest.mark.asyncio
async def test_ack_answers_and_maps_an_unreadable_set(
    owner: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    acked: list[str] = []
    monkeypatch.setattr(access_set, "acknowledge_first_share", acked.append)
    assert _body(await ra.api_ack(_req("POST"))) == {"acknowledged": True}
    assert acked == ["local:owner"]
    monkeypatch.setattr(access_set, "acknowledge_first_share", _raise(access_set.CODE_UNREADABLE))
    assert (await ra.api_ack(_req("POST"))).status == 503


@pytest.mark.asyncio
async def test_counts_read_unknown_when_their_sources_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory
    from kiro_crew.shared_crew import routes_identity

    async def broken_rows(_r: web.Request) -> list[dict[str, Any]]:
        raise RuntimeError("catalog unreadable")

    async def broken_store(_state: Any) -> Any:
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(routes_identity, "_catalog_rows", broken_rows)
    monkeypatch.setattr(memory, "_get_vector_store_async", broken_store)
    counts = await ra._counts(_req("GET", state=SimpleNamespace()))
    assert counts == {"sessions": None, "memory_items": None}


@pytest.mark.asyncio
async def test_counts_add_semantic_and_episodic_memory(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew.dashboard.handlers import memory
    from kiro_crew.shared_crew import routes_identity

    async def rows(_r: web.Request) -> list[dict[str, Any]]:
        return [{"key": "chat-1"}, {"key": "chat-2"}]

    store = SimpleNamespace(memory_stats=lambda: {"semantic_active": 3, "episodic_active": 4})

    async def vector(_state: Any) -> Any:
        return store

    monkeypatch.setattr(routes_identity, "_catalog_rows", rows)
    monkeypatch.setattr(routes_identity, "_machine_only", lambda _k: False)
    monkeypatch.setattr(memory, "_get_vector_store_async", vector)
    counts = await ra._counts(_req("GET", state=SimpleNamespace()))
    assert counts == {"sessions": 2, "memory_items": 7}


def _work_app(slot: Any) -> web.Application:
    app = web.Application()
    app["state"] = SimpleNamespace(_slots={slot.key: slot}, push_slots_update=lambda: None)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [True, False])
async def test_a_stop_declined_by_compaction_cancels_their_turn_task(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    """A compacting decline stopped nothing, so the turn task charged to them is cancelled."""
    import asyncio

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.shared_crew import turn_gate

    slot = SimpleNamespace(key="chat-1", running=True, messages=[])

    async def declined(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"ok": True, "info": "compacting", "compacting": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", declined)
    task = asyncio.ensure_future(asyncio.sleep(30))
    if not cancelled:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setitem(turn_gate._UNADMITTED_RUNNING, "chat-1", ("u-9", task))
    result = await ra.stop_slot_work(_work_app(slot), frozenset({"u-9"}))
    assert result == {"queued": 0, "running": int(cancelled)}
    await asyncio.sleep(0)
    assert task.cancelled()


@pytest.mark.asyncio
async def test_a_stopped_turn_counts_as_running_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from kiro_crew.dashboard import chat_handlers
    from kiro_crew.shared_crew import turn_gate

    slot = SimpleNamespace(key="chat-1", running=True, messages=[])
    calls: list[str] = []

    async def stopped(_state: Any, s: Any, **_k: Any) -> dict[str, Any]:
        calls.append(s.key)
        return {"ok": True}

    monkeypatch.setattr(chat_handlers, "stop_slot_turn", stopped)
    task = asyncio.ensure_future(asyncio.sleep(30))
    monkeypatch.setitem(turn_gate._UNADMITTED_RUNNING, "chat-1", ("u-9", task))
    try:
        result = await ra.stop_slot_work(_work_app(slot), frozenset({"u-9"}))
    finally:
        task.cancel()
    assert result == {"queued": 0, "running": 1}
    assert calls == ["chat-1"]
