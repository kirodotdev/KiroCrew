"""What a ``session_send`` delivered reaches the sender's own row as typed fields.

The outgoing session-message card reads ``meta.sessionSend`` (``{outcome,
target}``) instead of the tool's human-readable result. The gateway parks the
receipt when it delivers (``POST /api/session-control/send``) and the caller's
turn claims it by the call's own arguments on that call's completed result.
Also covers ``relay_spans``, the positions the receiver's bubble drops.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent
from kiro_crew.dashboard import relay_spans, session_send_receipts
from kiro_crew.dashboard.chat_utils import effective_session_key


@pytest.fixture(autouse=True)
def _clean_receipts():
    session_send_receipts.reset()
    yield
    session_send_receipts.reset()


# ── the store ────────────────────────────────────────────────────────────────


def test_a_receipt_is_claimed_once_by_the_same_arguments_and_session():
    assert session_send_receipts.park("chat-1", "Planner", "go", "queued", "chat-9")
    # Another session, or other arguments, finds nothing.
    assert (
        session_send_receipts.claim("chat-2", session_send_receipts.selector("Planner", "go"))
        is None
    )
    assert (
        session_send_receipts.claim("chat-1", session_send_receipts.selector("Planner", "stop"))
        is None
    )
    key = session_send_receipts.selector("Planner", "go")
    assert session_send_receipts.claim("chat-1", key) == {"outcome": "queued", "target": "chat-9"}
    assert session_send_receipts.claim("chat-1", key) is None


def test_a_receipt_parked_before_the_turn_began_is_not_claimed():
    session_send_receipts.park("chat-1", "Planner", "go", "delivered", "chat-9")
    key = session_send_receipts.selector("Planner", "go")
    assert session_send_receipts.claim("chat-1", key, not_before=float("inf")) is None


def test_park_refuses_what_it_cannot_describe():
    assert not session_send_receipts.park("", "Planner", "go", "queued", "chat-9")
    assert not session_send_receipts.park("chat-1", "", "go", "queued", "chat-9")
    assert not session_send_receipts.park("chat-1", "Planner", "go", "sent", "chat-9")
    assert not session_send_receipts.park("chat-1", "Planner", "go", "queued", "")


@pytest.mark.parametrize(
    "raw",
    [
        {"target": "Planner", "message": "go", "steer": False, "_meta": {"x": 1}},
        {
            "server": "kirocrew-dashboard",
            "tool": "session_send",
            "arguments": {"target": "Planner", "message": "go"},
        },
        {
            "tool_id": "kirocrew-dashboard::session_send",
            "arguments": {"target": "Planner", "message": "go"},
        },
    ],
)
def test_the_call_selector_reads_the_arguments_the_server_was_sent(raw):
    assert session_send_receipts.call_selector(raw) == session_send_receipts.selector(
        "Planner", "go"
    )


def test_a_send_to_another_crew_never_takes_a_local_receipt():
    """Mutation guard: drop the ``crew`` check and the remote call registers the
    same selector as an identical local one, and can claim its receipt."""
    selectors: dict[str, str] = {}
    local = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-local",
        tool_name="session_send",
        mcp_server_name="kirocrew-dashboard",
        raw_tool_params={"target": "Planner", "message": "go"},
    )
    remote = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-remote",
        tool_name="session_send",
        mcp_server_name="kirocrew-dashboard",
        raw_tool_params={"target": "Planner", "message": "go", "crew": "laptop"},
    )
    session_send_receipts.note_call(selectors, local, initial=True)
    session_send_receipts.note_call(selectors, remote, initial=True)
    assert selectors == {"tc-local": session_send_receipts.selector("Planner", "go")}
    # A refinement that reveals the call is remote forgets what the initial frame held.
    selectors["tc-late"] = ""
    late = AcpEvent(
        kind=EVENT_TOOL_CALL, tool_call_id="tc-late", raw_tool_params=remote.raw_tool_params
    )
    session_send_receipts.note_call(selectors, late, initial=False)
    assert "tc-late" not in selectors
    assert (
        session_send_receipts.call_selector(
            {
                "tool_id": "kirocrew-dashboard::session_send",
                "arguments": {"target": "P", "message": "m", "crew": "x"},
            }
        )
        is None
    )


def test_a_wrapper_naming_another_tool_is_not_unwrapped():
    raw = {"tool_id": "other::session_send", "arguments": {"target": "Planner", "message": "go"}}
    assert session_send_receipts.call_selector(raw) is None


def test_the_outcome_follows_the_send_result():
    assert session_send_receipts.outcome_of({"steered": True, "started": False}) == "steered"
    assert session_send_receipts.outcome_of({"steered": False, "started": True}) == "delivered"
    assert session_send_receipts.outcome_of({"steered": False, "started": False}) == "queued"


# ── the route parks ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_send_route_parks_what_it_delivered(monkeypatch):
    """Mutation guard: drop the ``park`` call in ``api_session_control_send`` and
    no receipt exists for the caller's turn to claim."""
    from kiro_crew.dashboard.handlers import session_control as handler

    request = MagicMock()
    request.headers = {"X-Session-Key": "chat-1"}
    request.app = {"state": MagicMock()}
    monkeypatch.setattr(handler, "_require_internal", AsyncMock(return_value=None))
    body = {"target": " Planner", "message": "go", "receipt": True}
    monkeypatch.setattr(handler, "_body", AsyncMock(side_effect=lambda _r: dict(body)))
    monkeypatch.setattr(
        handler.work_ledger_routes, "read_send_handback", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        handler.sc, "revive_own_archived_worker", AsyncMock(side_effect=lambda s, k, t, **_: t)
    )
    monkeypatch.setattr(
        handler.sc,
        "send_to_target",
        AsyncMock(return_value={"ok": True, "target": "chat-9", "started": False, "steered": True}),
    )
    monkeypatch.setattr(handler, "_add_worker_capabilities", AsyncMock(return_value=None))

    resp = await handler.api_session_control_send(request)

    assert resp.status == 200
    # Parked under the target as the call spelled it, leading space and all.
    assert session_send_receipts.claim(
        "chat-1", session_send_receipts.selector(" Planner", "go")
    ) == {"outcome": "steered", "target": "chat-9"}

    # A send that asks for no receipt (a dispatch seed, an app) parks none.
    del body["receipt"]
    assert (await handler.api_session_control_send(request)).status == 200
    assert (
        session_send_receipts.claim("chat-1", session_send_receipts.selector(" Planner", "go"))
        is None
    )


def test_the_session_send_tool_asks_for_a_receipt():
    """Mutation guard: drop ``receipt`` from the tool's request and the gateway
    parks nothing for its row."""
    from kiro_crew import mcp_dashboard_session_runs as runs

    ctx = MagicMock()
    ctx.client.post.return_value = {"target": "chat-9", "started": True}
    runs._run_session_send({"target": "Planner", "message": "go"}, ctx)
    path, payload = ctx.client.post.call_args.args[:2]
    assert path == "/api/session-control/send"
    assert payload["receipt"] is True


# ── the turn claims it onto the call's own row ───────────────────────────────


def _stub_state(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    state.slack_client = None
    return state


def _send_call(
    tcid: str, *, server: str = "kirocrew-dashboard", args: dict | None = None
) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tcid,
        title="session_send",
        tool_name="session_send",
        mcp_server_name=server,
        raw_tool_params=args if args is not None else {"target": "Planner", "message": "go"},
        tool_input=json.dumps(args if args is not None else {"target": "Planner", "message": "go"}),
    )


async def _drive(state, slot, events, monkeypatch, *, park=None):
    """Run *events* through the real ``_run_chat``; *park* runs right before the
    first tool result, the moment the gateway handles the tool's request."""
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        parked = False
        for ev in events:
            if ev.kind == EVENT_TOOL_RESULT and park and not parked:
                park()
                parked = True
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    slot.messages.append({"role": "user", "content": "tell the planner", "ts": "t0", "cls": ""})
    try:
        await chat_runner._run_chat(state, slot, "tell the planner")
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _send_rows(slot, tcid="tc-send"):
    return [
        m
        for m in slot.messages
        if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_call_id") == tcid
    ]


def _patches(state):
    return [
        c.args[1]
        for c in state.broadcast_ws.call_args_list
        if c.args
        and c.args[0] == "chat_message_update"
        and "sessionSend" in c.args[1].get("meta", {})
    ]


@pytest.mark.asyncio
async def test_a_delivered_session_send_row_carries_the_receipt(tmp_path, monkeypatch):
    """Mutation guard: drop ``stamp_result`` from ``_on_tool_result`` and the row
    carries no ``sessionSend``, so the card cannot draw."""
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    # The turn's own session key: the ``dashboard:``-namespaced form the MCP
    # tool sends as ``X-Session-Key``.
    key = effective_session_key(slot)
    events = [
        _send_call("tc-send"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-send", tool_output="sent", tool_final=True
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(
        state,
        slot,
        events,
        monkeypatch,
        park=lambda: session_send_receipts.park(key, "Planner", "go", "queued", "chat-9"),
    )

    rows = _send_rows(slot)
    assert rows
    assert all(
        r["meta"].get("sessionSend") == {"outcome": "queued", "target": "chat-9"} for r in rows
    )
    # Open tabs get it as a patch addressed to the row.
    patches = _patches(state)
    assert patches and all(p["slot"] == slot.key for p in patches)
    assert {p.get("mid") for p in patches} == {r["meta"].get("mid") for r in rows}


@pytest.mark.asyncio
async def test_a_result_with_no_matching_receipt_stamps_nothing(tmp_path, monkeypatch):
    """A receipt for other arguments, a failed call, and another server's
    same-named tool all leave the row without ``sessionSend``."""
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    # The turn's own session key: the ``dashboard:``-namespaced form the MCP
    # tool sends as ``X-Session-Key``.
    key = effective_session_key(slot)

    def _park():
        session_send_receipts.park(key, "Planner", "something else", "queued", "chat-9")
        session_send_receipts.park(key, "Planner", "go", "queued", "chat-9")

    events = [
        _send_call("tc-other", args={"target": "Planner", "message": "unsent"}),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-other", tool_output="x", tool_final=True),
        _send_call("tc-failed"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-failed",
            tool_status="failed",
            tool_output="err",
        ),
        _send_call("tc-foreign", server="evil-server"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-foreign", tool_output="ok", tool_final=True
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(state, slot, events, monkeypatch, park=_park)

    for tcid in ("tc-other", "tc-failed", "tc-foreign"):
        rows = _send_rows(slot, tcid)
        assert rows, tcid
        assert all("sessionSend" not in r["meta"] for r in rows), tcid
    assert _patches(state) == []


@pytest.mark.asyncio
async def test_a_failed_call_retires_its_receipt_so_a_refused_retry_cannot_wear_it(
    tmp_path, monkeypatch
):
    """The request delivered and parked a receipt, but the harness reported the
    call failed. A retry with the same arguments that the gateway refused (a
    completed result, no receipt of its own) must stay the tool line.

    Mutation guard: skip the claim on a failed frame and the retry draws as sent.
    """
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    key = effective_session_key(slot)
    events = [
        _send_call("tc-first"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-first",
            tool_status="failed",
            tool_output="timeout",
        ),
        _send_call("tc-retry"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-retry",
            tool_output="Error: refused",
            tool_final=True,
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(
        state,
        slot,
        events,
        monkeypatch,
        park=lambda: session_send_receipts.park(key, "Planner", "go", "delivered", "chat-9"),
    )
    for tcid in ("tc-first", "tc-retry"):
        rows = _send_rows(slot, tcid)
        assert rows, tcid
        assert all("sessionSend" not in r["meta"] for r in rows), tcid


def test_a_refinement_frame_supplies_the_arguments_an_initial_frame_lacked():
    selectors: dict[str, str] = {}
    first = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-1",
        tool_name="session_send",
        mcp_server_name="kirocrew-dashboard",
    )
    session_send_receipts.note_call(selectors, first, initial=True)
    assert selectors == {"tc-1": ""}
    update = AcpEvent(
        kind=EVENT_TOOL_CALL, tool_call_id="tc-1", raw_tool_params={"target": "P", "message": "m"}
    )
    session_send_receipts.note_call(selectors, update, initial=False)
    assert selectors == {"tc-1": session_send_receipts.selector("P", "m")}
    # A refinement for a call never identified records nothing.
    stray = AcpEvent(
        kind=EVENT_TOOL_CALL, tool_call_id="tc-2", raw_tool_params={"target": "P", "message": "m"}
    )
    session_send_receipts.note_call(selectors, stray, initial=False)
    assert "tc-2" not in selectors


def test_receipts_share_the_directive_mechanism_but_not_its_records():
    """A directive discard (the marker path retiring its twin) must not delete a
    pending receipt, and a receipt is never claimable as a directive."""
    from kiro_crew.dashboard import directive_queue

    directive_queue.reset()
    session_send_receipts.park("chat-1", "Planner", "go", "queued", "chat-9")
    key = session_send_receipts.selector("Planner", "go")
    assert directive_queue.discard("chat-1") == 0
    assert directive_queue.claim("chat-1", key) is None
    assert session_send_receipts.claim("chat-1", key) == {"outcome": "queued", "target": "chat-9"}


@pytest.mark.parametrize(
    "wire_title",
    [
        "@kirocrew-dashboard/session_send",
        "Running: @kirocrew-dashboard/session_send",
        "mcp__kirocrew-dashboard__session_send",
        "kirocrew-dashboard_session_send",
    ],
)
def test_a_call_named_only_by_its_wire_title_is_a_session_send(wire_title):
    assert session_send_receipts.is_session_send_call("", "", wire_title)
    assert session_send_receipts.is_session_send_call("kirocrew-dashboard", "", wire_title)


@pytest.mark.parametrize(
    ("server", "name", "wire_title"),
    [
        ("evil-server", "session_send", ""),
        ("", "session_send", ""),
        ("", "", "@evil-server/session_send"),
        ("", "", "session_send"),
        ("kirocrew-dashboard", "session_status", ""),
    ],
)
def test_another_tool_or_server_is_not(server, name, wire_title):
    assert not session_send_receipts.is_session_send_call(server, name, wire_title)


@pytest.mark.asyncio
async def test_a_kas_send_named_only_by_its_wire_title_gets_its_card(tmp_path, monkeypatch):
    """KAS frames carry no ``_meta`` identity: the call is named by its wire title
    and its arguments ride the Tool Search envelope. The row still gets the
    receipt, and the identity the dashboard checks before drawing the card.

    Mutation guard: drop the wire-title branch of ``is_session_send_call`` and
    the row stays the tool line.
    """
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    key = effective_session_key(slot)
    raw = {
        "tool_id": "kirocrew-dashboard::session_send",
        "arguments": {"target": "Planner", "message": "go"},
    }
    events = [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-kas",
            title="Running: @kirocrew-dashboard/session_send",
            wire_title="Running: @kirocrew-dashboard/session_send",
            raw_tool_params=raw,
            tool_input=json.dumps(raw),
        ),
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-kas", tool_output="sent", tool_final=True
        ),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(
        state,
        slot,
        events,
        monkeypatch,
        park=lambda: session_send_receipts.park(key, "Planner", "go", "delivered", "chat-9"),
    )
    rows = _send_rows(slot, "tc-kas")
    assert rows
    for row in rows:
        assert row["meta"]["sessionSend"] == {"outcome": "delivered", "target": "chat-9"}
        assert row["meta"]["tool_name"] == "session_send"
        assert row["meta"]["mcp_server"] == "kirocrew-dashboard"
    assert all(p["meta"].get("tool_name") == "session_send" for p in _patches(state))


def test_a_permission_request_naming_the_tool_registers_the_call():
    """Mutation guard: drop ``note_call`` from ``_on_permission_request`` and a
    call identified only there never claims its receipt."""
    from kiro_crew.dashboard.chat_turn import tool_approval

    turn = MagicMock()
    turn._pending_tool_ids = {}
    turn._send_receipt_selectors = {}
    turn.assistant_text = ""
    event = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-perm",
        tool_name="session_send",
        mcp_server_name="kirocrew-dashboard",
        raw_tool_params={"target": "Planner", "message": "go"},
    )
    import unittest.mock as um

    # Patched in the namespace the handler runs in: the runner composes it.
    handler = tool_approval._on_permission_request
    with um.patch.dict(
        handler.__globals__, {"_decide_permission_request": AsyncMock(return_value=None)}
    ):
        asyncio.run(handler(turn, event))
    assert turn._send_receipt_selectors == {
        "tc-perm": session_send_receipts.selector("Planner", "go")
    }


def test_a_row_named_by_a_qualified_tool_name_gets_the_canonical_identity():
    """kiro-cli can report ``kirocrew-dashboard___session_send``; the dashboard
    draws the card only for exactly ``session_send`` on ``kirocrew-dashboard``.

    Mutation guard: rewrite only a missing name and this row keeps the qualified
    one, so its card never draws.
    """
    state, slot = MagicMock(), MagicMock(key="chat-1")
    selectors: dict[str, str] = {}
    call = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="tc-q",
        tool_name="kirocrew-dashboard___session_send",
        mcp_server_name="kirocrew-dashboard",
        raw_tool_params={"target": "Planner", "message": "go"},
    )
    session_send_receipts.note_call(selectors, call, initial=True)
    session_send_receipts.park("dashboard:chat-1", "Planner", "go", "delivered", "chat-9")
    row = {
        "ts": "t1",
        "meta": {
            "tool_name": "kirocrew-dashboard___session_send",
            "mcp_server": "kirocrew-dashboard",
            "mid": "m1",
        },
    }
    done = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-q", tool_final=True)
    receipt = session_send_receipts.stamp_result(
        state, slot, selectors, "dashboard:chat-1", done, [row], terminal=True, not_before=0.0
    )
    assert receipt == {"outcome": "delivered", "target": "chat-9"}
    assert row["meta"]["tool_name"] == "session_send"
    assert row["meta"]["mcp_server"] == "kirocrew-dashboard"


# ── relay_spans ──────────────────────────────────────────────────────────────


def test_envelope_spans_count_utf16_units():
    """Mutation guard: count code points instead and the second span lands one
    unit early in the browser, past a body holding an astral character."""
    env = "[sent by session chat-9 via session_send]\n\n"
    text = f"[2 queued messages merged]\n\n{env}🚀 one\n\n{env}two"
    spans = relay_spans.envelope_spans(text, [env, env])
    utf16 = text.encode("utf-16-le")
    assert [utf16[2 * a : 2 * b].decode("utf-16-le") for a, b in spans] == [env, env]


def test_relay_for_rebuilds_each_envelope_from_its_own_record():
    fmt = "[sent by session {caller} via {via}]\n\n"
    text = "[sent by session chat-9 via session_send]\n\nhi"
    record = relay_spans.relay_for(
        text, [{"senderKey": "chat-9", "via": "session_send", "envelope": [[0, 999]]}], fmt
    )
    # The stored spans are not trusted; they are rebuilt for this text.
    assert record == {"senderKey": "chat-9", "via": "session_send", "envelope": [[0, 43]]}
    # An envelope that is not in the text: the sender stays, the spans go.
    assert relay_spans.relay_for("edited", [{"senderKey": "chat-9"}], fmt) == {
        "senderKey": "chat-9",
        "via": "session_send",
    }
    assert relay_spans.relay_for(text, ["not-a-record"], fmt) is None
