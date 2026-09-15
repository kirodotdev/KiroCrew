"""Scenario: one agent turn inside a pod runs to completion, tools included.

The flow: a user sends a message, the agent answers, and a tool call it made
shows up in the turn. This is the deepest end-to-end path in the product, and in
a pod it is also the one most likely to be silently broken -- the pod remaps its
agent child's ``HOME``, and a revision once shipped with every ACP spawn dead in
a pod while ``/api/health`` answered 200 the whole time.

Offline and deterministic: the pod's gateway spawns the packaged fake ACP
backend, pinned into the service definition by the ``pod`` fixture, and the
``[[TOOL]]`` sentinel makes it emit a tool call as well as text.

What the assertion reads is the DASHBOARD's stream, not the ACP wire. The fake
emits an ACP ``tool_call`` update, and ``/api/chat`` surfaces that to the client
as an SSE row of ``type: "tool"`` (``chat_runner`` renders the tool title into
it); the literal ``tool_call`` never appears in the body. The first nightly run of
this suite failed on macOS asserting that literal against a turn that had in fact
completed WITH its tool row -- a wrong contract in the test, not a lost tool call.

Scope is what the fake backend supports. It speaks the ACP subset the client
drives and answers on prompt sentinels, so this asserts that a turn COMPLETES
with a tool call in it. It does not assert real subagent orchestration, which
needs a model.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.timeout(600)

# 5 minutes: a cold pod's first turn pays the agent child's whole bootstrap.
TURN_TIMEOUT = 300.0


def test_one_agent_turn_completes_with_a_tool_call(pod) -> None:
    from kiro_crew.testing.fake_acp_backend import REPLY_TEXT, TOOL_TRIGGER

    slot = "e2e-scenario-turn"
    # POST /api/chat streams the turn as SSE and closes when the turn ends, so
    # the response body IS the completed turn: no polling, and no way to read a
    # half-finished turn as a pass.
    stream = pod.api(
        "POST",
        "chat",
        {"message": f"{TOOL_TRIGGER} say pong", "slot": slot},
        timeout=TURN_TIMEOUT,
    )
    text = stream if isinstance(stream, str) else repr(stream)

    assert REPLY_TEXT in text, (
        "the agent turn produced no reply from the pinned fake backend, so either "
        "the pod spawned a different agent or the turn never completed.\n"
        f"stream tail: {text[-2000:]!r}\n{pod.logs()}"
    )
    tool_rows = [row for row in _sse_rows(text) if row.get("type") == "tool"]
    assert tool_rows, (
        f"the {TOOL_TRIGGER} turn surfaced no SSE row of type 'tool', so the fake's "
        "tool call never reached the dashboard stream.\n"
        f"stream tail: {text[-2000:]!r}"
    )
    assert any("hello-from-fake" in str(row.get("content", "")) for row in tool_rows), (
        f"the {TOOL_TRIGGER} turn emitted no expected tool output.\n"
        f"stream tail: {text[-2000:]!r}"
    )

    # The turn is also durable, not just streamed: the slot must now hold it.
    slots = pod.api("GET", "chat/slots")
    names = _slot_names(slots)
    assert slot in names, f"the turn's slot is not in GET /api/chat/slots: {sorted(names)}"


def _sse_rows(body: str) -> list[dict]:
    """Every JSON object carried on a ``data:`` line of an SSE body.

    Only the parseable object rows: the terminal ``data: [DONE]`` and any
    non-object payload are dropped rather than failing the parse, because the
    scenario's claim is about which rows are PRESENT, and a framing detail must
    not read as a missing tool call.
    """
    rows: list[dict] = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            parsed = json.loads(payload)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


def _slot_names(body: object) -> set[str]:
    """Slot names out of ``GET /api/chat/slots``, tolerant of the wrapper key.

    Reads whichever of the documented shapes came back rather than pinning one:
    the scenario's claim is that the slot EXISTS, and failing on the envelope's
    shape instead would report a routing change as a lost turn.
    """
    rows: object = body
    if isinstance(body, dict):
        for key in ("slots", "items", "rows"):
            if isinstance(body.get(key), list):
                rows = body[key]
                break
    if not isinstance(rows, list):
        return set()
    out: set[str] = set()
    for row in rows:
        if isinstance(row, str):
            out.add(row)
        elif isinstance(row, dict):
            for key in ("slot", "name", "key", "id"):
                val = row.get(key)
                if isinstance(val, str) and val:
                    out.add(val)
    return out
