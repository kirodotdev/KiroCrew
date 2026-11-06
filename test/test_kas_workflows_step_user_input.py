"""Step questions over Kiro Agent's ``_kiro/userInput`` reach the person."""

from __future__ import annotations

import pytest
from kas_workflow_helpers import ChatProvider, driven, make_router, parallel_state

from kiro_crew.acp import elicitation as el
from kiro_crew.kas_workflows.router import KasWorkflowError

UI = "_kiro/userInput"
#: KAS reads any reply but ``answered`` with text as "move on", which is yes.
_DECLINED = {"action": "answered", "answer": el.USER_INPUT_DECLINED_ANSWER}
_NO_PERSON = {"action": "answered", "answer": el.USER_INPUT_NO_PERSON_ANSWER}

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
    await async_wait_until(lambda: router.questions())
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
async def test_dismissing_it_tells_kas_the_person_declined():
    router = make_router(audit=lambda **_k: None)
    rt = await _held(router)
    await router.answer_question(router.questions()[0]["key"], None, dismiss=True)
    assert rt.responses == [(11, _DECLINED)]


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
    router._mark_stopped("sa")
    await router._withdraw_session_asks("sa")
    assert rt.responses == [(11, _NO_PERSON)]


@pytest.mark.asyncio
async def test_a_question_for_no_known_step_says_no_person_saw_it():
    from kiro_crew.testing.wait import async_wait_until

    router = make_router(audit=lambda **_k: None)
    rt = driven(router, parallel_state())
    from kiro_crew.kas_workflows import router_answers

    claim = router_answers._register(router, router._run_runtime["w1"], 12, "nobody", UI)
    await router._admit(claim, _RICH)
    await async_wait_until(lambda: rt.responses)
    assert rt.responses == [(12, _NO_PERSON)]


def _ready(router):
    """A driven run whose chat reads step questions (``_held`` without the ask)."""
    rt = driven(router, parallel_state())
    router._ledger["w1"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0, "ended": False}
    router.note_chat_session("chat-1", ChatProvider(rt), mode=lambda: ("trust", "trust"))
    router._step_runtime["sa"] = router._run_runtime["w1"]
    return rt, router.sink_for(rt)


async def _ask_too_long(router, _mp):
    rt, sink = _ready(router)
    sink.on_step_request("x" * 300, "sa", UI, _RICH)
    return rt


async def _ask_unowned(router, _mp):
    from kiro_crew.kas_workflows import router_answers

    rt, _sink = _ready(router)
    claim = router_answers._register(router, router._run_runtime["w1"], 12, "nobody", UI)
    await router._admit(claim, _RICH)
    return rt


async def _ask_queue_full(router, _mp):
    rt, sink = _ready(router)
    router._max_pending = 0
    sink.on_step_request(13, "sa", UI, _RICH)
    return rt


async def _ask_unbuildable(router, monkeypatch):
    # A legacy-bridge question its builder cannot turn into a card (forced here:
    # admission already drops the options it would refuse).
    from kas_workflow_helpers import question_request

    from kiro_crew.kas_workflows import asks

    rt, sink = _ready(router)
    monkeypatch.setattr(asks, "question_from_request", lambda *_a, **_k: None)
    sink.on_step_request(14, "sa", "session/request_permission", question_request())
    return rt


async def _person_dismisses(router, _mp):
    rt = await _held(router)
    await router.answer_question(router.questions()[0]["key"], None, dismiss=True)
    return rt


async def _person_answers(router, _mp):
    rt = await _held(router)
    await router.answer_question(router.questions()[0]["key"], {"tc-9": ["MySQL"]})
    return rt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decide, outcome, reason",
    [
        (_ask_too_long, "decline", "request id too long"),
        (_ask_unowned, "decline", "no run owns this step"),
        (_ask_queue_full, "decline", "too many asks waiting"),
        (_ask_unbuildable, "decline", "no usable question"),
        (_person_dismisses, "decline", "person"),
        (_person_answers, "accept", "person"),
    ],
    ids=["too-long-id", "no-owner", "queue-full", "unbuildable", "dismissed", "answered"],
)
async def test_every_step_question_decision_writes_one_security_log_row(
    decide, outcome, reason, monkeypatch
):
    from kiro_crew.testing.wait import async_wait_until

    audits: list[dict] = []
    router = make_router(audit=lambda **kw: audits.append(kw))
    rt = await decide(router, monkeypatch)
    await async_wait_until(lambda: rt.responses and not router._tasks)
    assert len(rt.responses) == 1, "one answer"
    [row] = audits
    assert (row["tool_name"], row["outcome"], row["metadata"]["reason"]) == (
        "mcp_elicitation",
        outcome,
        reason,
    )
    assert len(row["request_id"]) <= 71, "the id is logged in its safe shape"


@pytest.mark.asyncio
async def test_a_request_replayed_while_still_open_is_decided_once():
    from kiro_crew.testing.wait import async_wait_until

    audits: list[dict] = []
    router = make_router(audit=lambda **kw: audits.append(kw))
    rt, sink = _ready(router)
    sink.on_step_request("x" * 300, "sa", UI, _RICH)
    assert sink.on_step_request("x" * 300, "sa", UI, _RICH) is None, "a replay, not a new ask"
    await async_wait_until(lambda: rt.responses and not router._tasks)
    assert len(rt.responses) == 1 and len(audits) == 1, "one decision, one row, one answer"
