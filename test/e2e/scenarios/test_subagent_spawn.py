"""Scenario: one agent turn inside a pod runs to completion, tools included.

The flow: a user sends a message, the agent answers, and a tool call it made
shows up in the turn. This is the deepest end-to-end path in the product, and in
a pod it is also the one most likely to be silently broken -- the pod remaps its
agent child's ``HOME``, and a revision once shipped with every ACP spawn dead in
a pod while ``/api/health`` answered 200 the whole time.

Offline and deterministic: the pod's gateway spawns the packaged fake ACP
backend, pinned into the service definition by the ``pod`` fixture, and the
``[[TOOL]]`` sentinel makes it emit a tool call as well as text.

Scope is what the fake backend supports. It speaks the ACP subset the client
drives and answers on prompt sentinels, so this asserts that a turn COMPLETES
with a tool call in it. It does not assert real subagent orchestration, which
needs a model.
"""

from __future__ import annotations

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
    assert (
        "tool_call" in text
    ), f"the {TOOL_TRIGGER} turn emitted no tool_call event.\nstream tail: {text[-2000:]!r}"

    # The turn is also durable, not just streamed: the slot must now hold it.
    slots = pod.api("GET", "chat/slots")
    names = _slot_names(slots)
    assert slot in names, f"the turn's slot is not in GET /api/chat/slots: {sorted(names)}"


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
