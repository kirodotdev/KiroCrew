"""``/api/chat/slots/{slot}/question[/answer]``: one slot's question card.

The app-facing way to read and answer the ``ask_question`` card a slot raised
(the app-sdk ``ChatEmbed`` uses it). Pinned here:

* the owner app of a slot, holding ``permissions.sessionApproval``, reads its card,
  answers it (the blocked wait gets the answer) and the card is gone after;
* an app never reads or answers a card on a slot it does not own --
  another app's slot or a user session, even with the grant -- and never answers a
  foreign slot's ask by naming its id under its own slot;
* without the grant even the owner app is refused;
* the dashboard owner's path, and the app-token refusal on ``/api/ask-question/*``,
  are unchanged.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app
from slot_ownership_checkpoint_helpers import (  # noqa: F401 -- fixtures are requested by name
    _GRANT,
    _NOT_FOUND,
    APP,
    _as,
    sel_spy_fixture,
    state_fixture,
)

from kiro_crew.dashboard import slot_ownership
from kiro_crew.dashboard.handlers.ask_question import (
    api_ask_question_answer,
    api_ask_question_pending,
)
from kiro_crew.dashboard.handlers.slot_question import (
    api_chat_slot_question,
    api_chat_slot_question_answer,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.testing.wait import await_until

_QUESTIONS = [{"question": "Which branch?", "options": [{"label": "main"}, {"label": "dev"}]}]


def _client(state, caller: str | None) -> TestClient:
    app = _make_app(state)
    app.router.add_get("/api/chat/slots/{slot}/question", api_chat_slot_question)
    app.router.add_post("/api/chat/slots/{slot}/question/answer", api_chat_slot_question_answer)
    app.router.add_get("/api/ask-question/pending", api_ask_question_pending)
    app.router.add_post("/api/ask-question/{ask_id}/answer", api_ask_question_answer)
    app.middlewares.insert(0, _as(caller))
    return TestClient(TestServer(app))


def _seed(state) -> None:
    state.get_or_create_slot("mine", app=APP)
    state.get_or_create_slot("theirs", app="other-app")
    state.get_or_create_slot("u1", origin=SlotOrigin.USER)


async def _ask(state, slot: str, ask_id: str) -> asyncio.Task:
    task = asyncio.create_task(state.request_question(ask_id, slot, _QUESTIONS, 30))
    for _ in range(20):
        if ask_id in state._pending_questions:
            break
        await asyncio.sleep(0)
    assert ask_id in state._pending_questions
    return task


def _ask_id(n: int) -> str:
    return f"{n:032x}"


@pytest.mark.asyncio
async def test_owner_app_reads_answers_and_the_card_clears(state, sel_spy) -> None:
    _seed(state)
    task = await _ask(state, "mine", _ask_id(1))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as client:
            resp = await client.get("/api/chat/slots/mine/question")
            assert resp.status == 200
            pending = (await resp.json())["pending"]
            assert [(c["ask_id"], c["slot"]) for c in pending] == [(_ask_id(1), "mine")]
            assert pending[0]["questions"][0]["question"] == "Which branch?"

            resp = await client.post(
                "/api/chat/slots/mine/question/answer",
                json={"ask_id": _ask_id(1), "answers": {"Which branch?": "dev"}},
            )
            assert resp.status == 200, await resp.text()
            assert await await_until(task, what="the ask") == {"Which branch?": "dev"}

            resp = await client.get("/api/chat/slots/mine/question")
            assert (await resp.json())["pending"] == []


@pytest.mark.asyncio
async def test_a_slot_replaced_during_the_grant_read_is_not_read(state, sel_spy) -> None:
    """A close and same-name user slot inside the caller check's await: no cards leak."""
    _seed(state)

    def replace(_app: str) -> bool:
        state._slots.pop("mine")
        state.get_or_create_slot("mine", origin=SlotOrigin.USER)
        return True

    task = None
    with patch(_GRANT, side_effect=replace):
        async with _client(state, APP) as client:
            task = await _ask(state, "mine", _ask_id(8))
            resp = await client.get("/api/chat/slots/mine/question")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    # The grant read is what replaced the slot, so the refusal is the recheck's.
    assert state._slots["mine"]._app == ""
    assert any(r.get("error") == "slot replaced during the grant read" for r in sel_spy.denials())
    state.resolve_question(_ask_id(8), None)
    await await_until(task, what="the ask")


@pytest.mark.asyncio
async def test_the_polled_read_does_not_write_an_allow_row_per_call(
    state, sel_spy, monkeypatch
) -> None:
    """An embed polls every 1.5 s: its allows go through the 5-minute window."""
    # The approval module reads `sel` from chat_handlers at call time.
    from kiro_crew.dashboard import chat_handlers

    caller_rows: list[dict] = []

    class _Spy:
        def log_api_access(self, **kw):
            caller_rows.append(kw)

    monkeypatch.setattr(chat_handlers, "sel", lambda: _Spy())
    monkeypatch.setattr(slot_ownership, "_allow_audits", type(slot_ownership._allow_audits)())
    _seed(state)
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as client:
            for _ in range(5):
                resp = await client.get("/api/chat/slots/mine/question")
                assert resp.status == 200
    assert [r for r in caller_rows if r.get("outcome") == "allowed"] == []
    allows = [
        r
        for r in sel_spy.rows
        if r.get("outcome") == "allowed" and r.get("operation") == "chat_slot_question"
    ]
    assert len(allows) == 1


@pytest.mark.asyncio
async def test_owner_app_declines_a_blocking_card(state, sel_spy) -> None:
    _seed(state)
    task = await _ask(state, "mine", _ask_id(2))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as client:
            resp = await client.post(
                "/api/chat/slots/mine/question/answer",
                json={"ask_id": _ask_id(2), "dismissed": True},
            )
            assert resp.status == 200
    assert await await_until(task, what="the ask") is None


@pytest.mark.parametrize("slot", ["theirs", "u1", "absent"])
@pytest.mark.asyncio
async def test_an_app_cannot_reach_a_slot_it_does_not_own(state, sel_spy, slot) -> None:
    """Another app's slot, a user session (grant or not) and a missing name: one 404."""
    _seed(state)
    task = None if slot == "absent" else await _ask(state, slot, _ask_id(3))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as client:
            resp = await client.get(f"/api/chat/slots/{slot}/question")
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
            resp = await client.post(
                f"/api/chat/slots/{slot}/question/answer",
                json={"ask_id": _ask_id(3), "answers": {"Which branch?": "dev"}},
            )
            assert (resp.status, await resp.json()) == (404, _NOT_FOUND)
    if task is not None:
        assert not task.done()
        assert _ask_id(3) in state._pending_questions
        state.resolve_question(_ask_id(3), None)
        await await_until(task, what="the ask")


@pytest.mark.asyncio
async def test_an_app_cannot_answer_a_foreign_ask_under_its_own_slot(state, sel_spy) -> None:
    """Naming another slot's ask id from the app's own slot resolves nothing."""
    _seed(state)
    task = await _ask(state, "u1", _ask_id(4))
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as client:
            resp = await client.post(
                "/api/chat/slots/mine/question/answer",
                json={"ask_id": _ask_id(4), "answers": {"Which branch?": "dev"}},
            )
            assert resp.status == 404
            assert (await resp.json())["code"] == "question_not_found"
            # The foreign card is not listed under the app's slot either.
            resp = await client.get("/api/chat/slots/mine/question")
            assert (await resp.json())["pending"] == []
    assert not task.done()
    state.resolve_question(_ask_id(4), None)
    await await_until(task, what="the ask")


@pytest.mark.asyncio
async def test_without_the_grant_the_owner_app_is_refused(state, sel_spy) -> None:
    _seed(state)
    task = await _ask(state, "mine", _ask_id(5))
    with patch(_GRANT, return_value=False):
        async with _client(state, APP) as client:
            resp = await client.get("/api/chat/slots/mine/question")
            assert resp.status == 403
            assert (await resp.json())["code"] == "session_approval_not_granted"
            resp = await client.post(
                "/api/chat/slots/mine/question/answer",
                json={"ask_id": _ask_id(5), "answers": {"Which branch?": "dev"}},
            )
            assert resp.status == 403
    assert not task.done()
    state.resolve_question(_ask_id(5), None)
    await await_until(task, what="the ask")


@pytest.mark.asyncio
async def test_a_grant_removed_during_the_body_read_refuses_the_write(state, sel_spy) -> None:
    _seed(state)
    task = await _ask(state, "mine", _ask_id(6))
    reads = iter([True, False])
    with patch(_GRANT, side_effect=lambda _app: next(reads)):
        async with _client(state, APP) as client:
            resp = await client.post(
                "/api/chat/slots/mine/question/answer",
                json={"ask_id": _ask_id(6), "answers": {"Which branch?": "dev"}},
            )
            assert resp.status == 403
    assert not task.done()
    state.resolve_question(_ask_id(6), None)
    await await_until(task, what="the ask")


@pytest.mark.asyncio
async def test_dashboard_owner_path_is_unchanged(state, sel_spy) -> None:
    """The owner keeps both routes; an app still gets 403 on /api/ask-question/*."""
    _seed(state)
    task = await _ask(state, "u1", _ask_id(7))
    async with _client(state, "") as owner:
        resp = await owner.get("/api/ask-question/pending")
        assert [c["ask_id"] for c in (await resp.json())["pending"]] == [_ask_id(7)]
        resp = await owner.get("/api/chat/slots/u1/question")
        assert [c["ask_id"] for c in (await resp.json())["pending"]] == [_ask_id(7)]
    with patch(_GRANT, return_value=True):
        async with _client(state, APP) as app_client:
            resp = await app_client.get("/api/ask-question/pending")
            assert (resp.status, (await resp.json())["code"]) == (403, "app_token_forbidden")
            resp = await app_client.post(
                f"/api/ask-question/{_ask_id(7)}/answer", json={"answers": {"Which branch?": "x"}}
            )
            assert (resp.status, (await resp.json())["code"]) == (403, "app_token_forbidden")
    assert not task.done()
    async with _client(state, "") as owner:
        resp = await owner.post(
            "/api/chat/slots/u1/question/answer",
            json={"ask_id": _ask_id(7), "answers": {"Which branch?": "main"}},
        )
        assert resp.status == 200
    assert await await_until(task, what="the ask") == {"Which branch?": "main"}
