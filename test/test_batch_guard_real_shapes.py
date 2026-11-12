"""The batch write guard against the batch shapes agents really send.

Each case is a shape seen in recorded agent sessions, rebuilt with synthetic ids:
a ledger read sent in the same batch as the decides and session sends that
quote its values, a create sent with the bind or send that names the id it
returns, a merge sent with its read-back and the report that quotes it. The
write is refused and the refusal names the value it guessed. The same write
sent alone in a later step passes. One shape stays out of reach: a send that
claims a same-batch write's outcome in words, quoting no value (see
``test_a_send_that_claims_a_decides_outcome_in_words_passes``).
"""

from __future__ import annotations

import asyncio
import importlib
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, EVENT_TOOL_CALL, AcpEvent, JsonRpcMessage

_ITEM = "it_16e4c6ed"
_TS = "07:30:45Z"
_SHA = "d25f0e89912c9713fdbb7a1ed451c75b840a2c6a"
#: An item id a same-batch create returns.
_NEW_ITEM = "it_7c0ffee1"
_KEY = "chat-5018-1793952328"
_WORK = "kirocrew-work"
_DASH = "kirocrew-dashboard"


def _guard():
    return importlib.import_module("kiro_crew.acp.batch_guard")


def _args(**fields) -> str:
    return json.dumps(fields)


def _ledger_read_output() -> str:
    return json.dumps(
        {
            "items": [
                {
                    "item_id": _ITEM,
                    "last_report_at": f"2026-11-06T{_TS}",
                    "summary": f"merged as {_SHA}",
                }
            ]
        }
    )


# -- the shapes --


def test_a_ledger_read_batched_with_a_decide_and_a_send_quoting_it_is_refused():
    # The item id was seen earlier; the read's time and sha were not.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Patrol: read the ledger for {_ITEM}.")
    window.dispatched("r", guard.READ, text=_args(item_id=_ITEM))
    decide = _args(action="decide", item_id=_ITEM, decision=f"merged as {_SHA} at {_TS}")
    send = _args(target=_KEY, message=f"{_ITEM} merged as {_SHA}")
    window.dispatched("d", guard.WRITE, text=decide)
    window.dispatched("s", guard.WRITE, text=send)
    window.settled("r", _ledger_read_output())
    for call in ("d", "s"):
        reason = window.refusal_for(call, guard.WRITE)
        assert reason is not None
        assert _SHA[:12] in reason


def test_a_read_batched_with_a_decide_quoting_only_its_time_is_refused():
    # A time is not a token on its own; a returned read that printed it is proof.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Patrol {_ITEM}.")
    window.dispatched("r", guard.READ, text=_args(item_id=_ITEM))
    window.dispatched("d", guard.WRITE, text=_args(item_id=_ITEM, decision=f"retry after {_TS}"))
    # Before the read returns there is no proof for a time.
    assert window.refusal_for("d", guard.WRITE) is None
    window.settled("r", _ledger_read_output())
    reason = window.refusal_for("d", guard.WRITE)
    assert reason is not None
    assert _TS[:8] in reason


def test_a_send_that_claims_a_decides_outcome_in_words_passes():
    # Known gap: the send states the same-batch decide's result in words and
    # quotes only the item id the session already saw. No value to compare, so
    # the token rule cannot see the dependency; it passes.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Patrol {_ITEM}.")
    window.dispatched("d", guard.WRITE, text=_args(action="decide", item_id=_ITEM))
    window.dispatched(
        "s", guard.WRITE, text=_args(message=f"the decide on {_ITEM} hit a log error")
    )
    window.settled("d", f"Recorded decide. item={_ITEM}")
    assert window.refusal_for("s", guard.WRITE) is None


def test_a_ledger_create_batched_with_a_send_of_the_new_item_is_refused():
    # The create mints the item id; a send quoting it in the same batch guessed it.
    guard = _guard()
    window = guard.BatchWindow()
    create = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="c",
        tool_name="work_ledger_record",
        mcp_server_name=_WORK,
        mcp_identity_trusted=True,
        tool_input=_args(action="create", title="review"),
    )
    guard.note_dispatched(window, create)
    window.dispatched("s", guard.WRITE, text=_args(target=_KEY, message=f"Bound {_NEW_ITEM}."))
    reason = window.refusal_for("s", guard.WRITE)
    assert reason is not None
    assert _NEW_ITEM in reason


def _session_create() -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="c",
        tool_name="session_create",
        mcp_server_name=_DASH,
        mcp_identity_trusted=True,
        tool_input=_args(title="reviewer"),
    )


def test_a_session_create_batched_with_a_send_to_a_guessed_key_is_refused():
    # The create mints the session key; a send naming one in the same batch guessed it.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, "Open a reviewer session and hand it the task.")
    guard.note_dispatched(window, _session_create())
    window.dispatched("s", guard.WRITE, text=_args(target=_KEY, message="Start."))
    reason = window.refusal_for("s", guard.WRITE)
    assert reason is not None
    assert _KEY[:12] in reason


def test_a_session_create_batched_with_a_bind_of_a_guessed_key_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Open a reviewer session for {_ITEM}.")
    guard.note_dispatched(window, _session_create())
    bind = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="b",
        tool_name="work_ledger_record",
        mcp_server_name=_WORK,
        mcp_identity_trusted=True,
        tool_input=_args(action="bind", item_id=_ITEM, worker_session_key=_KEY),
    )
    guard.note_dispatched(window, bind)
    reason = window.refusal_for("b", guard.classify(bind))
    assert reason is not None
    assert _KEY[:12] in reason
    assert _ITEM not in reason


def test_a_session_create_named_only_by_its_approval_still_refuses_a_guessed_bind():
    # KAS announces the call by server alone; its permission request names the
    # tool. A creator has no class, and the refresh must keep its flag anyway.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Open a reviewer session for {_ITEM}.")
    window.dispatched("c", "", text=_args(title="reviewer"))
    create = _session_create()
    window.refresh(
        "c", guard.classify(create), text=_args(title="reviewer"), creator=guard.is_creator(create)
    )
    window.dispatched(
        "b",
        guard.WRITE,
        text=_args(action="bind", item_id=_ITEM, worker_session_key=_KEY),
    )
    reason = window.refusal_for("b", guard.WRITE)
    assert reason is not None
    assert _KEY[:12] in reason


def test_a_session_create_batched_with_a_send_to_a_key_an_earlier_call_printed_passes():
    # The key came back from a call of an earlier step, so the send depends on
    # nothing its own batch returns.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, "Open a second reviewer session and nudge the first.")
    window.dispatched("earlier", "", text=_args(title="first reviewer"))
    window.settled("earlier", f"Created session {_KEY}.")
    window.reset()
    guard.note_dispatched(window, _session_create())
    window.dispatched("s", guard.WRITE, text=_args(target=_KEY, message="Any update?"))
    assert window.refusal_for("s", guard.WRITE) is None


def test_a_session_create_is_never_refused_itself():
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, "Open a reviewer session.")
    create = _session_create()
    guard.note_dispatched(window, create)
    window.dispatched("r", guard.READ, "gh pr view 7")
    assert window.refusal_for("c", guard.classify(create)) is None


def test_an_enqueue_its_read_back_and_a_report_in_one_batch_is_refused():
    guard = _guard()
    window = guard.BatchWindow()
    merge = "gh pr merge 1761 --squash --auto"
    view = "gh pr view 1761 --json headRefOid,mergeStateStatus"
    window.dispatched("m", guard.classify_command(merge), merge)
    window.dispatched("v", guard.classify_command(view), view)
    window.dispatched("w", guard.WRITE, text=_args(summary=f"#1761 queued at head {_SHA[:8]}"))
    window.settled("v", f'{{"headRefOid": "{_SHA}", "mergeStateStatus": "BLOCKED"}}')
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert _SHA[:8] in reason


def test_the_same_write_alone_in_a_later_step_passes():
    guard = _guard()
    now = [1000.0]
    window = guard.BatchWindow(clock=lambda: now[0])
    guard.note_prompt(window, f"Patrol {_ITEM}.")
    window.dispatched("r", guard.READ, text=_args(item_id=_ITEM))
    window.settled("r", _ledger_read_output())
    # The next model step comes after a model round-trip.
    now[0] += 1.0
    decide = _args(action="decide", item_id=_ITEM, decision=f"merged as {_SHA} at {_TS}")
    window.dispatched("d", guard.WRITE, text=decide)
    assert window.refusal_for("d", guard.WRITE) is None


# -- read and write pairs --


@pytest.mark.parametrize(
    ("read_text", "read_class", "write_text", "output", "cited"),
    [
        pytest.param(
            _args(item_id=_ITEM),
            "read",
            _args(action="decide", item_id=_ITEM, decision=f"last report {_TS}"),
            _ledger_read_output(),
            _TS[:8],
            id="ledger-read+ledger-record",
        ),
        pytest.param(
            _args(item_id=_ITEM),
            "read",
            _args(target=_KEY, message=f"{_ITEM} is at {_SHA[:10]}"),
            _ledger_read_output(),
            _SHA[:10],
            id="ledger-read+session-send",
        ),
        pytest.param(
            _args(action="get-info", review="9001"),
            "",
            _args(path="notes/reviews.md", text=f"revision 4 is {_SHA[:9]}"),
            f"revision 4, commit {_SHA}",
            _SHA[:9],
            id="unclassified-review-read+fs-append",
        ),
        pytest.param(
            "gh pr view 1761 --json headRefOid",
            "read",
            _args(target=_KEY, message=f"head is {_SHA}"),
            _SHA,
            _SHA[:12],
            id="gh-pr-view+session-send",
        ),
    ],
)
def test_each_read_and_write_pair_is_refused_naming_the_value(
    read_text, read_class, write_text, output, cited
):
    # A third-party read the tables do not name still counts once it returned
    # the value: its output is the proof.
    guard = _guard()
    window = guard.BatchWindow()
    guard.note_prompt(window, f"Patrol {_ITEM} and send to {_KEY}.")
    window.dispatched("r", read_class, text=read_text)
    window.dispatched("w", guard.WRITE, text=write_text)
    window.settled("r", output)
    reason = window.refusal_for("w", guard.WRITE)
    assert reason is not None
    assert cited in reason


# -- the tools these shapes use are classified however they are named --


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"tool_name": "work_ledger_read", "mcp_server_name": _WORK}, "read"),
        ({"tool_name": "work_ledger_record", "mcp_server_name": _WORK}, "write"),
        ({"tool_name": "session_send", "mcp_server_name": _DASH}, "write"),
        ({"tool_name": "@kirocrew-work/work_ledger_read"}, "read"),
        ({"tool_name": "@kirocrew-dashboard/session_send"}, "write"),
        # A bare qualified KAS id carries no server: unclassified, as on main.
        ({"tool_name": "mcp_kirocrew_work_work_ledger_read"}, ""),
        ({"tool_name": "mcp_kirocrew_work_work_ledger_record"}, ""),
        ({"tool_name": "mcp_kirocrew_dashboard_session_send"}, ""),
        ({"tool_name": "fs_append"}, "write"),
        ({"tool_name": "mcp_example_other_tool"}, ""),
    ],
)
def test_the_shapes_tools_are_classified_under_every_name(fields, expected):
    event = AcpEvent(kind=EVENT_TOOL_CALL, mcp_identity_trusted=True, **fields)
    assert _guard().classify(event) == expected


def test_the_shapes_writes_are_withheld_from_auto_approval(monkeypatch):
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    policy = {
        "rules": [
            {
                "capability": "mcp",
                "match": ["kirocrew-work/*", "kirocrew-dashboard/*"],
                "effect": "allow",
            }
        ]
    }
    _policy, refs = guard.withhold_write_auto_approval(policy)
    assert "@kirocrew-work/work_ledger_record" in refs
    assert "@kirocrew-dashboard/session_send" in refs
    assert "@kirocrew-work/work_ledger_read" not in refs


# -- demo: the real approval path --


def _frame(call_id: str, server: str, tool: str, raw_input: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": call_id,
                "title": f"Running: @{server}/{tool}",
                "rawInput": raw_input,
                "_meta": {"kiro": {"toolName": tool, "mcpServerName": server}},
            },
        },
    )


def _done(call_id: str, text: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            },
        },
    )


def _ask(request_id: int, call_id: str, server: str, tool: str, raw_input: dict) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=request_id,
        tool_call_id=call_id,
        title=f"Running: @{server}/{tool}",
        mcp_server_name=server,
        tool_name=tool,
        mcp_identity_trusted=True,
        tool_input=json.dumps(raw_input),
    )


def _handle():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    runtime = MagicMock()
    runtime.send_response = AsyncMock()
    runtime.send_error = AsyncMock()
    handle = AcpSessionHandle("s1", asyncio.Queue(), runtime)
    # As a sent prompt leaves it: the (patched) setting read off the loop.
    handle._batch_window.set_feeding(_guard().guard_settings()[0])
    return handle


@pytest.mark.asyncio
async def test_demo_the_approval_path_refuses_the_batch_and_passes_the_solo_write(monkeypatch):
    """The refusal a model reads and the solo control, printed for the PR body."""
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (True, ()))
    handle = _handle()
    monkeypatch.setattr(handle, "steer", AsyncMock(return_value=True), raising=False)
    guard.note_prompt(handle._batch_window, f"Patrol: read the ledger for {_ITEM}.")
    decide = {"action": "decide", "item_id": _ITEM, "decision": f"merged as {_SHA}"}

    # One batch: the read, then the decide that quotes it.
    handle._handle_update(_frame("tc-read", _WORK, "work_ledger_read", {"item_id": _ITEM}))
    handle._handle_update(_frame("tc-decide", _WORK, "work_ledger_record", decide))
    handle._handle_update(_done("tc-read", _ledger_read_output()))
    handle._permission_gate_events[71] = _ask(71, "tc-decide", _WORK, "work_ledger_record", decide)
    assert await handle.approve_tool(71) is False
    reason = handle.take_batch_refusal(71)
    assert reason and _SHA[:12] in reason
    print(f"BATCHED: approve_tool -> False\n{reason}")

    # A later step: the same decide sent alone, after the read returned and a
    # model round-trip went by.
    later = handle._batch_window._clock() + 1.0
    handle._batch_window._clock = lambda: later
    handle._handle_update(_frame("tc-decide-2", _WORK, "work_ledger_record", decide))
    handle._permission_gate_events[72] = _ask(
        72, "tc-decide-2", _WORK, "work_ledger_record", decide
    )
    assert await handle.approve_tool(72) is True
    print("SOLO, LATER STEP: approve_tool -> True")


# -- what the transports feed the guard --


@pytest.mark.asyncio
async def test_a_native_steer_makes_its_ids_seen(monkeypatch):
    # A user steering a running turn with an item id hands the model that id.
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    handle = _handle()
    handle._session_id = "s1"
    monkeypatch.setattr(handle._runtime, "acp_backend", "", raising=False)
    handle._runtime.send_request = AsyncMock(return_value=None)
    assert await handle.steer(f"bind {_ITEM} now") is True
    assert _ITEM in handle._batch_window.seen


@pytest.mark.asyncio
async def test_a_client_native_steer_makes_its_ids_seen(tmp_path, monkeypatch):
    # The client transport's default `_session/steer` path feeds the guard too.
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    from kiro_crew.acp.client import AcpClient

    client = AcpClient(work_dir=tmp_path)
    client._batch_window.set_feeding(True)
    client._session_id = "sid"
    client._send_request = AsyncMock(return_value=5)
    assert await client.steer(f"bind {_ITEM} now") is True
    assert client._send_request.await_args[0][0] == "_session/steer"
    assert _ITEM in client._batch_window.seen


@pytest.mark.asyncio
async def test_a_client_pi_steer_makes_its_ids_seen_only_when_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    from kiro_crew.acp import pi_steer_turn
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_turn_control import ACP_BACKEND_PI

    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    client._batch_window.set_feeding(True)
    client._session_id = "sid"
    monkeypatch.setattr(pi_steer_turn, "steer", AsyncMock(return_value=False))
    assert await client.steer(f"bind {_ITEM} now") is False
    assert _ITEM not in client._batch_window.seen
    monkeypatch.setattr(pi_steer_turn, "steer", AsyncMock(return_value=True))
    assert await client.steer(f"bind {_ITEM} now") is True
    assert _ITEM in client._batch_window.seen


def test_with_the_guard_off_a_transport_collects_no_tokens(monkeypatch):
    # The default install never turned the guard on, so it pays nothing for it:
    # no token is kept, and the set is incomplete, so a guard switched on later
    # refuses only on proof.
    guard = _guard()
    monkeypatch.setattr(guard, "guard_settings", lambda: (False, ()))
    handle = _handle()
    guard.note_prompt(handle._batch_window, f"Patrol {_ITEM}.")
    handle._handle_update(_frame("tc-log", _WORK, "work_ledger_read", {"item_id": _ITEM}))
    handle._handle_update(_done("tc-log", f"merged as {_SHA}"))
    window = handle._batch_window
    window.reset()
    assert len(window.seen) == 0
    assert window.seen.complete is False


def test_a_write_is_judged_by_its_arguments_not_its_rendered_diff():
    # A transport renders an edit as a unified diff that includes the deleted
    # text; the call's arguments never quoted that text.
    guard = _guard()
    window = guard.BatchWindow()
    window.dispatched("r", guard.READ, "date -u")
    diff = f"--- a/notes.md\n+++ b/notes.md\n@@ -1 +1 @@\n-merged as {_SHA}\n+clean\n"
    event = AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id="w",
        tool_name="fs_write",
        tool_input=diff,
        raw_tool_params={"path": "notes.md", "text": "clean"},
    )
    guard.note_dispatched(window, event)
    assert window.refusal_for("w", guard.WRITE) is None


@pytest.mark.asyncio
async def test_a_transport_window_never_reads_the_setting_on_the_event_loop(monkeypatch):
    # The config load runs off the loop at turn setup; feeding tokens never
    # calls it, and a window that has not been told stays off and incomplete.
    guard = _guard()
    calls: list[int] = []

    def _settings():
        calls.append(1)
        return True, ()

    monkeypatch.setattr(guard, "guard_settings", _settings)
    window = guard.BatchWindow(gated=True)
    guard.note_prompt(window, f"Patrol {_ITEM}.")
    window.reset()
    assert calls == []
    assert _ITEM not in window.seen
    assert window.seen.complete is False
    await guard.refresh_feeding(window)
    assert calls == [1]
    guard.note_prompt(window, f"Patrol {_ITEM}.")
    window.reset()
    assert calls == [1]
    assert _ITEM in window.seen


def test_a_long_result_is_learned_past_the_display_cut(monkeypatch):
    # The session handle cuts tool output for display; the guard reads the frame.
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    guard = _guard()
    handle = _handle()
    filler = "x " * 6000
    handle._handle_update(_frame("tc-log", _WORK, "work_ledger_read", {"item_id": _ITEM}))
    handle._handle_update(_done("tc-log", f"{filler} merged as {_SHA}"))
    window = handle._batch_window
    member = next(iter(window._members.values()))
    assert _SHA in member.output
    assert guard.MAX_SCANNED_TEXT_CHARS > len(filler)


def _content_only(call_id: str, text: str) -> JsonRpcMessage:
    """A ``tool_call_update`` carrying output and no status: the result streamed
    before the terminal frame."""
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            },
        },
    )


def _completed_without_output(call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": call_id,
                "status": "completed",
            },
        },
    )


def test_a_handle_learns_a_result_streamed_before_an_outputless_terminal(monkeypatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    handle = _handle()
    handle._handle_update(_frame("tc-log", _WORK, "work_ledger_read", {"item_id": _ITEM}))
    handle._handle_update(_content_only("tc-log", f"merged as {_SHA}"))
    window = handle._batch_window
    assert window._open, "a content-only update must not end the call"
    handle._handle_update(_completed_without_output("tc-log"))
    assert not window._open
    member = next(iter(window._members.values()))
    assert _SHA in member.output


def test_a_client_learns_a_result_streamed_before_an_outputless_terminal(tmp_path, monkeypatch):
    monkeypatch.setattr(_guard(), "guard_settings", lambda: (True, ()))
    guard = _guard()
    from kiro_crew.acp.client import AcpClient
    from kiro_crew.acp.client_tool_events import _extract_tool_call_update

    client = AcpClient(work_dir=tmp_path)
    window = client._batch_window
    window.set_feeding(True)
    window.dispatched("tc-log", guard.READ, "work_ledger_read")
    _extract_tool_call_update(client, _content_only("tc-log", f"merged as {_SHA}"))
    assert window._open, "a content-only update must not end the call"
    _extract_tool_call_update(client, _completed_without_output("tc-log"))
    assert not window._open
    member = next(iter(window._members.values()))
    assert _SHA in member.output
