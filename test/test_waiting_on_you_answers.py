"""Answering and dismissing a waiting-on-you card through the owner routes.

The answer is one ordinary person turn sent through ``POST /api/chat``: its text is
only what the owner chose or typed, it carries ``meta.waitingOnYou``, and the ask is
retired only once that send is accepted. Every stale or repeated answer is refused
without a second turn.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from waiting_on_you_helpers import (  # noqa: F401 -- fixtures are requested by name
    SLOT,
    ask,
    clock_fixture,
    make_app,
    new_store,
    pin,
    state_fixture,
    store_fixture,
    user_rows,
    waiting_rows,
)

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.history_schema import HUMAN_TURN_META_KEY

_RUN_CHAT = "kiro_crew.dashboard.chat_handlers._run_chat"
ANSWER = f"/api/chat/slots/{SLOT}/waiting-on-you/{{key}}/answer"
DISMISS = f"/api/chat/slots/{SLOT}/waiting-on-you/{{key}}/dismiss"


def _client(state, **kw) -> TestClient:
    return TestClient(TestServer(make_app(state, **kw)))


async def _answer(client, key: str, **body):
    resp = await client.post(ANSWER.format(key=key), json=body)
    return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_answer_sends_exactly_the_chosen_label_with_its_meta(state, store):
    pinned = await pin(store, ask())
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            status, body = await _answer(
                client,
                "scope-override",
                ask_id=pinned["ask_id"],
                answer_id="a" * 12,
                label="I posted it",
            )
    assert status == 200 and body == {"ok": True, "outcome": "sent", "answer_id": "a" * 12}
    rows = user_rows(state)
    assert [r["content"] for r in rows] == ["I posted it"]
    meta = rows[0]["meta"]
    assert meta[HUMAN_TURN_META_KEY] is True
    assert meta["waitingOnYou"] == {
        "key": "scope-override",
        "ask_id": pinned["ask_id"],
        "answer_id": "a" * 12,
    }
    assert store.asks(SLOT) == []
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["answered"]


@pytest.mark.asyncio
async def test_no_user_row_carries_the_asks_key_title_or_detail(state, store):
    item = ask(options=())
    pinned = await pin(store, item)
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            await _answer(client, item["key"], ask_id=pinned["ask_id"], answer_id="b" * 12, text="")
            second = await pin(store, ask("next", options=()))
            await _answer(
                client, "next", ask_id=second["ask_id"], answer_id="c" * 12, text="done it, see PR"
            )
    rows = user_rows(state)
    assert [r["content"] for r in rows] == ["Done", "done it, see PR"]
    for row in rows:
        for text in (item["key"], item["title"], item["detail"], "next"):
            assert text not in row["content"]


@pytest.mark.asyncio
async def test_answering_one_card_of_two_retires_only_that_one(state, store):
    first = await pin(store, ask("first"))
    await pin(store, ask("second"))
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            await _answer(client, "first", ask_id=first["ask_id"], answer_id="d" * 12, label="Hold")
    assert [a["key"] for a in store.asks(SLOT)] == ["second"]


@pytest.mark.asyncio
async def test_an_old_ask_id_after_a_redeclare_is_superseded(state, store):
    old = await pin(store, ask())
    new = await pin(store, {**ask(), "title": "Post the override now"})
    assert new["ask_id"] != old["ask_id"]
    with patch(_RUN_CHAT, new=AsyncMock()) as run:
        async with _client(state) as client:
            status, body = await _answer(
                client, "scope-override", ask_id=old["ask_id"], answer_id="e" * 12, label="Hold"
            )
    assert (status, body["code"]) == (409, "superseded")
    assert user_rows(state) == [] and run.await_count == 0


@pytest.mark.asyncio
async def test_a_label_not_among_the_options_is_refused_400(state, store):
    pinned = await pin(store, ask())
    async with _client(state) as client:
        status, body = await _answer(
            client, "scope-override", ask_id=pinned["ask_id"], answer_id="f" * 12, label="Ship it"
        )
        text_status, _ = await _answer(
            client, "scope-override", ask_id=pinned["ask_id"], answer_id="f" * 12, text="x"
        )
    assert (status, body["code"]) == (400, "invalid_answer")
    assert text_status == 400
    assert user_rows(state) == [] and len(store.asks(SLOT)) == 1


@pytest.mark.asyncio
async def test_a_second_window_gets_answered_and_no_turn(state, store):
    pinned = await pin(store, ask())
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            await _answer(
                client, "scope-override", ask_id=pinned["ask_id"], answer_id="g" * 12, label="Hold"
            )
            status, body = await _answer(
                client,
                "scope-override",
                ask_id=pinned["ask_id"],
                answer_id="h" * 12,
                label="I posted it",
            )
    assert (status, body["code"]) == (409, "answered")
    assert [r["content"] for r in user_rows(state)] == ["Hold"]


@pytest.mark.asyncio
async def test_a_retry_with_the_same_answer_id_sends_one_turn_even_after_the_next_turn(
    state, store
):
    pinned = await pin(store, ask())
    body = {"ask_id": pinned["ask_id"], "answer_id": "i" * 12, "label": "Hold"}
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            first = await _answer(client, "scope-override", **body)
            # The next turn starts and hears of the answer; the tombstone outlives it.
            from kiro_crew.dashboard.waiting_on_you_events import turn_block

            await turn_block(state, state._slots[SLOT])
            again = await _answer(client, "scope-override", **body)
    assert first == again == (200, {"ok": True, "outcome": "sent", "answer_id": "i" * 12})
    assert len(user_rows(state)) == 1


@pytest.mark.asyncio
async def test_two_concurrent_answers_send_one_turn(state, store):
    pinned = await pin(store, ask())
    gate = asyncio.Event()

    async def slow_run(*_a, **_k):
        await gate.wait()

    with patch(_RUN_CHAT, new=slow_run):
        async with _client(state) as client:
            one = asyncio.create_task(
                _answer(
                    client,
                    "scope-override",
                    ask_id=pinned["ask_id"],
                    answer_id="j" * 12,
                    label="Hold",
                )
            )
            two = asyncio.create_task(
                _answer(
                    client,
                    "scope-override",
                    ask_id=pinned["ask_id"],
                    answer_id="k" * 12,
                    label="I posted it",
                )
            )
            results = await asyncio.wait_for(asyncio.gather(one, two), timeout=30)
            gate.set()
    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409]
    assert [r[1]["code"] for r in results if r[0] == 409] == ["answered"]
    assert len(user_rows(state)) == 1


_LOCKED = {
    "error": "someone else is typing",
    "code": "composer_locked",
    "session_key": SLOT,
    "holder": {},
    "lease": {},
}


@pytest.mark.asyncio
async def test_a_refused_send_passes_its_refusal_through_and_the_ask_stays_pinned(state, store):
    pinned = await pin(store, ask())
    body = {"ask_id": pinned["ask_id"], "answer_id": "l" * 12, "label": "Hold"}
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            with patch("kiro_crew.shared_crew.typing_lease.refuse_if_locked", return_value=_LOCKED):
                status, refused = await _answer(client, "scope-override", **body)
            assert [a["state"] for a in store.asks(SLOT)] == ["pinned"]
            assert user_rows(state) == []
            # The reservation was released, so the same click goes through once allowed.
            retry = await _answer(client, "scope-override", **body)
    assert (status, refused["code"]) == (409, "composer_locked")
    assert retry[0] == 200 and [r["content"] for r in user_rows(state)] == ["Hold"]


@pytest.mark.asyncio
async def test_a_send_refused_after_its_row_landed_is_uncertain_and_a_retry_sends_nothing(
    state, store, monkeypatch
):
    pinned = await pin(store, ask())
    body = {"ask_id": pinned["ask_id"], "answer_id": "v" * 12, "label": "Hold"}
    slot = state._slots[SLOT]

    async def _close_during_the_hold(_state, _slot):
        # The tab closes between the row's append and the turn's dispatch.
        state._slots.pop(SLOT, None)

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_handlers.maybe_auto_link_slack", _close_during_the_hold
    )
    with patch(_RUN_CHAT, new=AsyncMock()) as run:
        async with _client(state) as client:
            status, refused = await _answer(client, "scope-override", **body)
            assert (status, refused["code"]) == (409, "slot_closed")
            assert [r["content"] for r in slot.messages if r.get("role") == "user"] == ["Hold"]
            assert [a["state"] for a in store.asks(SLOT)] == ["uncertain"]
            state._slots[SLOT] = slot
            retry_status, retried = await _answer(client, "scope-override", **body)
    assert (retry_status, retried["code"]) == (409, "uncertain")
    assert [r["content"] for r in user_rows(state)] == ["Hold"]
    assert run.await_count == 0


@pytest.mark.asyncio
async def test_a_paused_crew_queues_the_answer_as_its_own_entry(state, store):
    pinned = await pin(store, ask())
    with (
        patch(_RUN_CHAT, new=AsyncMock()),
        patch("kiro_crew.crew_pause.holds_slot", return_value=True),
    ):
        async with _client(state) as client:
            status, body = await _answer(
                client, "scope-override", ask_id=pinned["ask_id"], answer_id="q" * 12, label="Hold"
            )
    assert status == 200 and body["outcome"] == "queued"
    queued = state._slots[SLOT]._queue
    assert [e["content"] for e in queued] == ["Hold"]
    assert queued[0]["meta"]["waitingOnYou"]["ask_id"] == pinned["ask_id"]
    assert user_rows(state) == []


@pytest.mark.asyncio
async def test_a_dismissal_removes_the_ask_and_keeps_a_retired_record(state, store):
    pinned = await pin(store, ask())
    async with _client(state) as client:
        resp = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
        first = (resp.status, await resp.json())
        again = await client.post(
            DISMISS.format(key="scope-override"), json={"ask_id": pinned["ask_id"]}
        )
        second = (again.status, (await again.json())["code"])
    assert first == (200, {"ok": True})
    assert second == (409, "cleared")
    assert store.asks(SLOT) == []
    retired = store.snapshot_for_turn(SLOT)["retired"]
    assert [(r["key"], r["reason"]) for r in retired] == [("scope-override", "dismissed")]
    assert [r["meta"]["card"]["status"] for r in waiting_rows(state)] == ["dismissed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [ANSWER, DISMISS])
async def test_an_app_token_is_refused_on_the_owner_routes(state, store, route):
    pinned = await pin(store, ask())
    body = {"ask_id": pinned["ask_id"], "answer_id": "m" * 12, "label": "Hold"}
    async with _client(state, app_name="some-app") as client:
        resp = await client.post(route.format(key="scope-override"), json=body)
    assert resp.status == 403
    assert len(store.asks(SLOT)) == 1 and user_rows(state) == []


@pytest.mark.asyncio
async def test_a_caller_who_is_not_the_owner_is_refused(state, store):
    pinned = await pin(store, ask())
    async with _client(state, user="someone-else") as client:
        resp = await client.post(
            ANSWER.format(key="scope-override"),
            json={"ask_id": pinned["ask_id"], "answer_id": "n" * 12, "label": "Hold"},
        )
    assert resp.status == 403
    assert len(store.asks(SLOT)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True], ids=["before-send", "after-send"])
async def test_a_restart_with_a_pending_answer_leaves_it_uncertain_and_a_retry_sends_nothing(
    tmp_path, state, store, clock, monkeypatch, accepted
):
    pinned = await pin(store, ask())

    def _check(_ask):
        return "Hold"

    await store.reserve(SLOT, "scope-override", pinned["ask_id"], "o" * 12, _check)
    if accepted:
        # The turn went out; the gateway stopped before recording the outcome.
        state._slots[SLOT].append("user", "Hold", "msg msg-u")
    restarted = new_store(clock)
    await restarted.warm()
    assert [a["state"] for a in restarted.asks(SLOT)] == ["uncertain"]
    monkeypatch.setattr(store_mod, "_STORES", {store_mod.store_for_home()._path: restarted})
    state._waiting_on_you_store = restarted
    before = len(user_rows(state))
    with patch(_RUN_CHAT, new=AsyncMock()):
        async with _client(state) as client:
            status, body = await _answer(
                client, "scope-override", ask_id=pinned["ask_id"], answer_id="o" * 12, label="Hold"
            )
            assert (status, body["code"]) == (409, "uncertain")
            assert len(user_rows(state)) == before
            # A fresh click may send it again; that is the person's choice.
            status, _ = await _answer(
                client, "scope-override", ask_id=pinned["ask_id"], answer_id="p" * 12, label="Hold"
            )
    assert status == 200 and len(user_rows(state)) == before + 1


@pytest.mark.asyncio
async def test_every_refusal_names_its_contract_code_and_a_sentence_the_card_shows(state, store):
    pinned = await pin(store, ask())
    good = {"ask_id": pinned["ask_id"], "answer_id": "t" * 12}
    async with _client(state) as client:
        cases = {
            "invalid_answer": await client.post(
                ANSWER.format(key="scope-override"),
                data="[1]",
                headers={"Content-Type": "application/json"},
            ),
            "cleared": await client.post(
                ANSWER.format(key="never-pinned"), json={**good, "label": "Hold"}
            ),
        }
        replaced = await pin(store, {**ask(), "title": "replaced"})
        cases["superseded"] = await client.post(
            ANSWER.format(key="scope-override"), json={**good, "label": "Hold"}
        )
        with patch("kiro_crew.shared_crew.typing_lease.refuse_if_locked", return_value=_LOCKED):
            cases["composer_locked"] = await client.post(
                ANSWER.format(key="scope-override"),
                json={"ask_id": replaced["ask_id"], "answer_id": "u" * 12, "label": "Hold"},
            )
        bodies = {code: (resp.status, await resp.json()) for code, resp in cases.items()}
    for code, (status, body) in bodies.items():
        assert status in (400, 409), code
        assert body["ok"] is False and body["code"] == code
        assert isinstance(body["error"], str) and body["error"].strip(), code
