"""Step questions over Kiro Agent's ``_kiro/userInput`` reach the person (round 4 gap 13)."""

from __future__ import annotations

import pytest
from kas_workflow_helpers import ChatProvider, driven, make_router, parallel_state

from kiro_crew.kas_workflows.router import KasWorkflowError

UI = "_kiro/userInput"

_RICH = {
    "sessionId": "sa",
    "toolCallId": "tc-9",
    "question": "Which database?",
    "options": [
        {"title": "Postgres", "recommended": True, "subOptions": [{"title": "PostGIS"}]},
        {"title": "MySQL", "description": "the old one"},
    ],
}


async def _held(router, params=_RICH):
    from kiro_crew.testing.wait import async_wait_until

    rt = driven(router, parallel_state())
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": False}
    router.note_chat_session("chat-1", ChatProvider(rt), mode=lambda: ("trust", "trust"))
    router._step_runtime["sa"] = router._run_runtime["w1"]
    sink = router.sink_for(rt)
    assert sink.claims_step_request("sa", UI)
    sink.on_step_request(11, "sa", UI, params)
    await async_wait_until(lambda: router.questions(), timeout=2.0)
    return rt


@pytest.mark.asyncio
async def test_a_user_input_step_question_is_held_with_its_rich_options():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router)
    await router.recheck_pending()
    assert rt.responses == [], "no mode answers a question"
    (held,) = router.questions()
    (item,) = held["questions"]
    assert item["options"][0] == {
        "label": "Postgres",
        "recommended": True,
        "subOptions": [{"label": "PostGIS"}],
    }
    assert item["options"][1] == {"label": "MySQL", "description": "the old one"}


@pytest.mark.asyncio
async def test_answering_it_sends_kas_answer_text():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router)
    key = router.questions()[0]["key"]
    with pytest.raises(KasWorkflowError, match="not offered"):
        await router.answer_question(key, {"tc-9": ["Postgres", "Nope"]})
    await router.answer_question(key, {"tc-9": ["Postgres", "PostGIS"]})
    assert rt.responses == [(11, {"action": "answered", "answer": "Postgres [PostGIS]"})]


@pytest.mark.asyncio
async def test_dismissing_it_answers_dismissed():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router)
    await router.answer_question(router.questions()[0]["key"], None, dismiss=True)
    assert rt.responses == [(11, {"action": "dismissed"})]


@pytest.mark.asyncio
async def test_a_free_form_step_question_reaches_the_person():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router, {**_RICH, "options": []})
    (held,) = router.questions()
    assert held["questions"][0]["freeText"] is True
    await router.answer_question(held["key"], {"tc-9": "SQLite, please"})
    assert rt.responses == [(11, {"action": "answered", "answer": "SQLite, please"})]


@pytest.mark.asyncio
async def test_a_stopped_step_withdraws_it_in_its_own_shape():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router)
    await router._withdraw_session_asks("sa")
    assert rt.responses == [(11, {"action": "dismissed"})]


@pytest.mark.asyncio
async def test_a_question_for_no_known_step_is_dismissed():
    from kiro_crew.testing.wait import async_wait_until

    router = make_router(audit=lambda **_k: None)
    rt = driven(router, parallel_state())
    await router._admit(router._run_runtime["w1"], 12, "nobody", _RICH, UI)
    await async_wait_until(lambda: rt.responses, timeout=2.0)
    assert rt.responses == [(12, {"action": "dismissed"})]
