"""A workflow step's tool inputs go through the chat turn's own screen before any allow.

The screen is ``tool_approval.tool_input_screen`` (title tier, tool-input tier with
its path, shell and deny-rule checks and its size ceiling), run once in ``_admit``
before a request can be granted, auto-approved or held. So under Trust a step's
file tool on a sensitive path is refused, never answered allow_once.
"""

from __future__ import annotations

from typing import Any

import pytest
from kas_workflow_helpers import FakeRuntime, framed_identity, make_router

from kiro_crew.kas_workflows import policy, wire
from kiro_crew.testing.wait import async_wait_until

PERM = "session/request_permission"
_OPTIONS = [
    {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
    {"optionId": "always", "kind": "allow_always", "name": "Always"},
    {"optionId": "no", "kind": "reject_once", "name": "Reject"},
]
_SSH_KEY = "~/.ssh/id_rsa"


def _file_tool(tool_call: dict[str, Any], **kiro: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "sessionId": "s0",
        "toolCall": {"toolCallId": "tc-1", "title": "Read File", **tool_call},
        "options": list(_OPTIONS),
    }
    if kiro:
        params["_meta"] = {"kiro": kiro}
    return params


#: Each shape names the sensitive path somewhere a step's request carries inputs.
_REFUSED = {
    "rawInput.path": _file_tool({"rawInput": {"path": _SSH_KEY}}),
    "locations[].path": _file_tool({"locations": [{"path": _SSH_KEY}]}),
    "consent.resource": _file_tool(
        {}, toolId="read_file", consent={"capability": "fs_read", "resource": _SSH_KEY}
    ),
    "mcp-body-field": _file_tool(
        {"title": "probe_echo", "rawInput": {"message": _SSH_KEY, "to": "a"}},
        toolId="mcp_probe_probe_echo",
    ),
    "oversize-input": _file_tool({"rawInput": {"path": "a" * 30_000}}),
}


async def _router(mode: str = "trust", **kwargs: Any):
    router = make_router(audit=kwargs.pop("audit", lambda **_k: None), **kwargs)
    await router.ensure_ledger()
    router._ledger["w0"] = {"slot": "chat-1", "cwd": "", "createdAt": 1.0}
    current = [mode]
    router.note_chat_session("chat-1", None, mode=lambda: (current[0], current[0]))
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
    return router, rt, sink, current


def _sent(rt: FakeRuntime) -> list[str | None]:
    return [result.get("outcome", {}).get("optionId") for _rid, result in rt.responses]


@pytest.mark.parametrize("shape", sorted(_REFUSED))
@pytest.mark.asyncio
async def test_a_step_input_the_chat_screen_refuses_is_rejected_under_trust(shape):
    audits: list[dict[str, Any]] = []
    router, rt, sink, _ = await _router(audit=lambda **kw: audits.append(kw))
    sink.on_step_request(1, "s0", PERM, _REFUSED[shape])
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["no"], "rejected, never allow_once"
    assert router.approvals() == []
    [record] = audits
    assert record["outcome"] == "auto_rejected"
    assert record["metadata"]["reason"].startswith("input screen: ")


@pytest.mark.asyncio
async def test_a_benign_step_input_still_auto_approves_under_trust():
    router, rt, sink, _ = await _router()
    sink.on_step_request(2, "s0", PERM, _file_tool({"rawInput": {"path": "notes/todo.txt"}}))
    await async_wait_until(lambda: rt.responses)
    assert _sent(rt) == ["yes"]


_SITES = ("trust", "person-pick", "grant-use", "mode-sweep")


@pytest.mark.parametrize("site", _SITES)
@pytest.mark.asyncio
async def test_no_allow_path_answers_a_screened_step(site):
    # Every allow path starts behind _admit's screen: a refused request is never
    # granted, auto-approved, held for a person to pick, or found by the mode sweep.
    start = "normal" if site in ("person-pick", "mode-sweep") else "trust"
    router, rt, sink, mode = await _router(mode=start)
    if site == "grant-use":
        # An "Always" grant on the tool exists before the refused call arrives.
        mode[0] = "normal"
        sink.on_step_request(9, "s0", PERM, _file_tool({}, toolId="read_file"))
        await async_wait_until(lambda: router.approvals())
        await router.answer(router.approvals()[0]["key"], "always")
        rt.responses.clear()
    sink.on_step_request(1, "s0", PERM, _REFUSED["rawInput.path"])
    await async_wait_until(lambda: rt.responses)
    assert router.approvals() == [], "nothing is held for a person to allow"
    if site == "mode-sweep":
        mode[0] = "trust"
        await router.recheck_pending()
    assert _sent(rt) == ["no"]


def test_the_step_screen_is_the_chat_screen(monkeypatch):
    # One helper serves both: patching it is seen by the step screen.
    from kiro_crew.llm_helpers_parts import tool_approval

    seen: list[str] = []

    def _screen(event, hooks, normalized, judged_title, regexes):
        seen.append(event.tool_input)
        return None

    monkeypatch.setattr(tool_approval, "tool_input_screen", _screen)
    params = _REFUSED["rawInput.path"]
    event, _why = framed_identity(params)
    assert policy.step_input_denial(event, params) is None
    assert _SSH_KEY in seen[0]
