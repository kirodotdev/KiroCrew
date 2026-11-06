"""A step's tool permission is never answered ``allow_always`` in Kiro Agent.

Crew's own step grant is the only "always": every path that answers a non-plan
step request (a person's pick, an approval mode, a recorded grant, the mode sweep,
a reject) sends allow-once or a reject, whatever the request's shape. A KAS-side
always-allow would persist a rule that skips Crew's deny-rule screen and its
security event log.
"""

from __future__ import annotations

from typing import Any

import pytest
from kas_workflow_helpers import FakeRuntime, make_router

from kiro_crew.kas_workflows import asks, router_base, wire
from kiro_crew.kas_workflows.router import KasWorkflowError
from kiro_crew.testing.wait import async_wait_until

PERM = "session/request_permission"
_OPTIONS = [
    {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
    {"optionId": "always", "kind": "allow_always", "name": "Always"},
    {"optionId": "no", "kind": "reject_once", "name": "Reject"},
]


def _request(*, command: Any = None, tool_id: str | None = None, title: str = "Run it", **meta):
    kiro: dict[str, Any] = dict(meta)
    if tool_id is not None:
        kiro["toolId"] = tool_id
    raw = {"command": command} if command is not None else {}
    params: dict[str, Any] = {
        "sessionId": "s0",
        "toolCall": {"title": title, "rawInput": raw},
        "options": list(_OPTIONS),
    }
    if kiro:
        params["_meta"] = {"kiro": kiro}
    return params


#: Every non-plan shape, with and without a grant key Crew can build.
_SHAPES = {
    "readable-command": _request(command="ls"),
    "over-cap-command": _request(command="x" * (router_base._MAX_GRANT_COMMAND_CHARS + 1)),
    "over-cap-tool-id": _request(tool_id="t" * (asks.MAX_ID_CHARS + 1)),
    "over-cap-title": _request(title="t" * (router_base._MAX_DISPLAY_CHARS + 1)),
    "unreadable-command": _request(command=["make", 7]),
    "plan-marked-with-a-command": _request(command="ls", tool_id=wire.TOOL_UPDATE_WORKFLOW),
}


async def _router_with_step(mode: list[str]):
    router = make_router(audit=lambda **_k: None)
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    router.note_chat_session("chat-1", None, mode=lambda: (mode[0], mode[0]))
    rt = FakeRuntime()
    sink = router.sink_for(rt)
    sink.on_notification(
        wire.NOTIFY_RUN_START,
        {"workflowId": "w0", "nodeTree": [{"type": "step", "id": "a", "label": "Do"}]},
    )
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w0", "nodeId": "a", "type": "step", "sessionId": "s0"},
    )
    return router, rt, sink


async def _ask(router, rt, sink, request_id: int, params: dict) -> None:
    """Send one request and wait until it is either answered or held."""
    seen = len(rt.responses)
    sink.on_step_request(request_id, "s0", PERM, params)
    await async_wait_until(
        lambda: len(rt.responses) > seen or _held_key(router, request_id) is not None
    )


def _held_key(router, request_id: int) -> str | None:
    return next((k for k, a in router._pending.items() if a.request_id == request_id), None)


async def _answer_if_held(router, request_id: int, option_id: str) -> None:
    key = _held_key(router, request_id)
    if key is not None:
        await router.answer(key, option_id)


async def _site_person_always(router, rt, sink, params, mode):
    await _ask(router, rt, sink, 1, params)
    await _answer_if_held(router, 1, "always")


async def _site_trust(router, rt, sink, params, mode):
    mode[0] = "trust"
    await _ask(router, rt, sink, 1, params)
    await _answer_if_held(router, 1, "always")


async def _site_grant_use(router, rt, sink, params, mode):
    await _ask(router, rt, sink, 1, params)
    await _answer_if_held(router, 1, "always")
    await _ask(router, rt, sink, 2, params)
    await _answer_if_held(router, 2, "always")


async def _site_mode_sweep(router, rt, sink, params, mode):
    await _ask(router, rt, sink, 1, params)
    mode[0] = "trust"
    await router.recheck_pending()
    await _answer_if_held(router, 1, "always")


async def _site_reject(router, rt, sink, params, mode):
    await _ask(router, rt, sink, 1, params)
    await _answer_if_held(router, 1, "no")


_SITES = {
    "person-always": _site_person_always,
    "trust-auto-approve": _site_trust,
    "grant-use": _site_grant_use,
    "mode-sweep": _site_mode_sweep,
    "reject": _site_reject,
}


@pytest.mark.parametrize("site", sorted(_SITES))
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.asyncio
async def test_no_path_answers_a_step_tool_allow_always(shape, site):
    mode = ["normal"]
    router, rt, sink = await _router_with_step(mode)
    await _SITES[site](router, rt, sink, _SHAPES[shape], mode)
    sent = [result["outcome"].get("optionId") for _rid, result in rt.responses]
    assert sent, "every request is answered once the person or a mode decides"
    assert "always" not in sent
    if site == "reject":
        assert sent == ["no"]


@pytest.mark.parametrize(
    "shape", ["over-cap-command", "over-cap-tool-id", "over-cap-title", "unreadable-command"]
)
@pytest.mark.asyncio
async def test_an_always_with_no_grant_key_is_one_call_and_says_so(shape):
    router, rt, sink = await _router_with_step(["normal"])
    await _ask(router, rt, sink, 1, _SHAPES[shape])
    [card] = router.approvals()
    always = next(o for o in card["options"] if o["kind"] == "allow_always")
    assert always["grant"] == {"scope": "once", "match": None}
    await router.answer(card["key"], "always")
    assert rt.responses == [(1, {"outcome": {"outcome": "selected", "optionId": "yes"}})]
    # Nothing was granted: the same call asks again.
    await _ask(router, rt, sink, 2, _SHAPES[shape])
    assert _held_key(router, 2) is not None


@pytest.mark.asyncio
async def test_a_step_offering_only_always_allow_is_refused_and_can_still_be_rejected():
    router, rt, sink = await _router_with_step(["normal"])
    params = {**_request(command="ls"), "options": [_OPTIONS[1], _OPTIONS[2]]}
    await _ask(router, rt, sink, 1, params)
    key = _held_key(router, 1)
    with pytest.raises(KasWorkflowError):
        await router.answer(key, "always")
    assert rt.responses == [], "allow_always is never sent"
    assert _held_key(router, 1) == key, "the card stays"
    await router.answer(key, "no")
    assert rt.responses == [(1, {"outcome": {"outcome": "selected", "optionId": "no"}})]


@pytest.mark.asyncio
async def test_a_plan_handoff_still_passes_the_persons_always_through():
    router, rt, sink = await _router_with_step(["trust"])
    plan = _request(tool_id=wire.TOOL_UPDATE_WORKFLOW)
    await _ask(router, rt, sink, 1, plan)
    [card] = router.approvals()
    assert router._pending[card["key"]].is_plan
    await router.answer(card["key"], "always")
    assert rt.responses == [(1, {"outcome": {"outcome": "selected", "optionId": "always"}})]
