"""The Activity card of a KAS engine sub-agent (the ``kas_subagents`` preview on).

The engine runs such a child inside the parent's turn and reports it as tool frames
on the parent's session. Its card is a native card (``chat_turn/native_cards``); in a
turn whose session runs the engine's own sub-agents the card also shows what the
engine reported (the task, each tool call, why a child did not complete) and is
marked ``engine`` so the page can say what the engine does not report. Every other
native card keeps the path it had.

The frames below are the shapes kiro-cli 3.5.0 (KAS 0.66.119) sent in a real run,
cut to the fields read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from engine_turn_helpers import client, registered
from kas_engine_child_helpers import _agents_dir, _batch, _default_spec
from kas_engine_child_helpers import _handle as _session_handle
from kas_engine_child_helpers import _own, _setting, _write

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY, EVENT_SUBAGENT_LIST, AcpEvent
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.dashboard import engine_subagent_cards as cards
from kiro_crew.dashboard.state import DashboardState

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
PROMPT = "Run this exact shell command: sleep 20; echo one\nReport the exact command output."
RAW_INPUT = {"name": "kirocrew-helper", "prompt": PROMPT, "explanation": "e", "contextFiles": []}


def _dispatch(
    status: str, *, subtask: str = SUBTASK, title: str = "Sub-agent: kirocrew-helper", **more: Any
):
    frame: dict[str, Any] = {
        "sessionUpdate": "tool_call" if status == "pending" else "tool_call_update",
        "toolCallId": CALL,
        "title": title,
        "status": status,
        "rawInput": RAW_INPUT,
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": subtask}},
    }
    frame.update(more)
    return frame


def _handle() -> Any:
    """The fields ``_handle_kas_subagent`` reads and writes, and nothing else of a handle."""
    return SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )


def _roster(*frames: dict[str, Any]) -> list[dict[str, Any]]:
    """The roster the session handle emits after *frames*, as the turn receives it."""
    handle = _handle()
    events: list[AcpEvent] = []
    for frame in frames:
        events = children._handle_kas_subagent(handle, frame) or []
    assert [e.kind for e in events] == [EVENT_SUBAGENT_LIST]
    return events[0].subagents


def _state() -> Any:
    state = MagicMock()
    state._native_cards = {}
    return state


def _slot() -> Any:
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    return slot


def _frames(state: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(c.args[0], c.args[1]) for c in state.broadcast_ws.call_args_list]


def _sync(state: Any, slot: Any, rows: list[dict[str, Any]], *, engine: bool) -> None:
    """Draw *rows* as the turn does: through the engine pre-pass in an engine turn."""
    if engine:
        rows = list(cards.engine_rows(rows, slot._native_subagent_tracker))
    cr._native_subagent_sync(
        state, slot, rows, slot._native_subagent_tracker, slot._native_subagent_output
    )


# ── the roster row: what the dispatch frame said ──────────────────────────────


def test_the_roster_row_keeps_the_task_the_target_and_the_failure_reason() -> None:
    [row] = _roster(_dispatch("pending"))
    assert row["target"] == "kirocrew-helper"
    assert row["prompt"] == PROMPT
    assert "failure" not in row
    # What every other reader of the row reads is what it was.
    assert row["initialQuery"] == row["sessionName"] == "Sub-agent: kirocrew-helper"
    assert row["status"] == {"type": "pending", "message": ""}

    [failed] = _roster(_dispatch("failed", rawOutput="Sub-agent execution was cancelled"))
    assert failed["failure"] == "Sub-agent execution was cancelled"
    # A completed child's ``rawOutput`` is its answer, not a failure reason.
    [done] = _roster(_dispatch("completed", rawOutput="one"))
    assert "failure" not in done


def test_the_stored_task_and_reason_are_bounded_and_only_strings_are_kept() -> None:
    big = {"name": "n" * 5000, "prompt": "p" * 50_000}
    [row] = _roster(_dispatch("failed", rawInput=big, rawOutput="r" * 50_000))
    # Each is kept to what a card shows of it plus the reach of the card's own cut.
    ahead = children.NATIVE_CHILD_SHOWN_LOOKAHEAD
    assert len(row["prompt"]) == children.NATIVE_CHILD_TASK_CAP + ahead
    assert len(row["target"]) == children.NATIVE_CHILD_LABEL_CAP + ahead
    assert len(row["failure"]) == children.NATIVE_CHILD_LABEL_CAP + ahead
    # And the card shows no more of each than its own bound.
    [card] = cards.engine_rows([row], {})
    assert len(card["initialQuery"]) == cards.ENGINE_TASK_MAX
    assert len(card["agentName"]) == cards.ENGINE_AGENT_MAX
    assert len(card["status"]["message"]) == cards.ENGINE_REASON_MAX

    odd = {"name": 7, "prompt": ["not", "text"]}
    [row] = _roster(_dispatch("failed", rawInput=odd, rawOutput={"not": "text"}))
    assert not {"target", "prompt", "failure"} & set(row)
    [row] = _roster(_dispatch("in_progress", rawInput="not a mapping"))
    assert not {"target", "prompt", "failure"} & set(row)


# ── the card of an engine turn ────────────────────────────────────────────────


def test_an_engine_card_names_the_task_and_is_marked_on_spawn_and_done() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("pending"), _dispatch("in_progress")), engine=True)
    [(kind, spawn)] = _frames(state)
    assert kind == "subagent_spawn"
    assert spawn == {
        "id": f"native:{SUBTASK}",
        "slot": "slot-1",
        "task": PROMPT,
        "agent": "kirocrew-helper",
        "engine": True,
    }

    state.broadcast_ws.reset_mock()
    slot._native_subagent_output[f"native:{SUBTASK}"] = ["one"]
    rows = _roster(_dispatch("pending"), _dispatch("completed", rawOutput="one"))
    _sync(state, slot, rows, engine=True)
    [(kind, done)] = _frames(state)
    assert kind == "subagent_done"
    assert done["engine"] is True
    assert done["error"] is None
    assert done["result"] == "one"
    assert done["task"] == PROMPT


def test_a_dispatch_the_engine_has_not_started_draws_no_card() -> None:
    """``pending`` may be a dispatch waiting on its approval card: a card that read
    "Starting..." with a running clock would say a child runs that does not."""
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("pending")), engine=True)
    assert _frames(state) == []
    assert slot._native_subagent_tracker == {}
    assert state._native_cards == {}


def test_a_child_the_turns_stop_cancelled_ends_as_an_error_with_the_engines_reason() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    rows = _roster(_dispatch("failed", rawOutput="Sub-agent execution was cancelled"))
    _sync(state, slot, rows, engine=True)
    [(kind, done)] = _frames(state)
    assert kind == "subagent_done"
    assert done["error"] == "Sub-agent execution was cancelled"
    # No output reached the card, and none is in the chat either.
    assert done["result"] == ""
    assert done["engine"] is True


def test_a_failed_child_with_no_reason_still_ends_as_an_error() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    _sync(state, slot, _roster(_dispatch("failed")), engine=True)
    [(_, done)] = _frames(state)
    assert done["error"] == cards.ENGINE_FAILED_NO_REASON


def test_a_refused_dispatch_is_one_card_ended_with_the_engines_reason() -> None:
    """The engine names a dispatch it never started by its CALL id on the failed frame,
    not by the id its pending frame carried."""
    state, slot = _state(), _slot()
    refused = _dispatch(
        "failed",
        subtask=CALL,
        title="Sub-agent execution",
        rawOutput="The user rejected this tool call.",
    )
    handle = _handle()
    children._handle_kas_subagent(handle, _dispatch("pending"))
    [event] = children._handle_kas_subagent(handle, refused) or []
    assert len(event.subagents) == 2  # the pending row and the refused one

    _sync(state, slot, event.subagents, engine=True)
    kinds = [kind for kind, _ in _frames(state)]
    assert kinds == ["subagent_spawn", "subagent_done"]
    spawn, done = (frame for _, frame in _frames(state))
    assert spawn["id"] == done["id"] == f"native:{CALL}"
    assert spawn["agent"] == "kirocrew-helper"
    assert spawn["task"] == PROMPT
    assert done["error"] == "The user rejected this tool call."
    assert done["result"] == ""
    # The turn's end closes nothing more: the pending row never became a card.
    state.broadcast_ws.reset_mock()
    cr._native_subagent_close_all(state, slot, slot._native_subagent_tracker, {})
    assert _frames(state) == []


def test_a_task_or_a_reason_holding_a_credential_is_redacted_on_the_card() -> None:
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=7)
    state, slot = _state(), _slot()
    leaky = {"name": "kirocrew-helper", "prompt": f"push with {token} now"}
    _sync(state, slot, _roster(_dispatch("in_progress", rawInput=leaky)), engine=True)
    rows = _roster(_dispatch("failed", rawInput=leaky, rawOutput=f"refused {token}"))
    _sync(state, slot, rows, engine=True)
    assert token not in repr(_frames(state))
    assert token not in repr(slot._native_subagent_tracker)


# ── every other native card is drawn as it was ────────────────────────────────


def test_the_same_rows_outside_an_engine_turn_draw_what_they_drew() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("pending")), engine=False)
    [(kind, spawn)] = _frames(state)
    assert kind == "subagent_spawn"
    assert spawn == {
        "id": f"native:{SUBTASK}",
        "slot": "slot-1",
        "task": "Sub-agent: kirocrew-helper",
        "agent": "kirocrew-helper",
    }

    state.broadcast_ws.reset_mock()
    rows = _roster(_dispatch("failed", rawOutput="Sub-agent execution was cancelled"))
    _sync(state, slot, rows, engine=False)
    [(kind, done)] = _frames(state)
    assert kind == "subagent_done"
    assert done["error"] is None
    assert done["result"] == "(output in chat)"
    assert "engine" not in done


def test_a_card_closed_as_stale_is_marked_only_when_it_is_an_engine_card() -> None:
    for engine in (False, True):
        state, slot = _state(), _slot()
        info: dict[str, Any] = {
            "started": 0.0,
            "last_activity": 0.0,
            "done": False,
            "agent": "kirocrew-helper",
            "task": "t",
            "label": "L",
            "model": "m",
        }
        if engine:
            info["engine"] = True
        slot._native_subagent_tracker["gone"] = info
        _sync(state, slot, [], engine=False)
        [(kind, done)] = _frames(state)
        assert kind == "subagent_done"
        assert done["error"] == "timed out (no activity)"
        extras = {key: done[key] for key in ("label", "model", "engine") if key in done}
        assert extras == ({"label": "L", "model": "m", "engine": True} if engine else {})


def test_a_row_off_the_wire_cannot_mark_itself() -> None:
    """The mark is the row's type, which only the engine pre-pass builds."""
    state, slot = _state(), _slot()
    [row] = _roster(_dispatch("in_progress"))
    _sync(state, slot, [{**row, "engine": True}], engine=False)
    [(_, spawn)] = _frames(state)
    assert "engine" not in spawn
    assert "engine" not in slot._native_subagent_tracker[SUBTASK]


def test_the_pre_pass_leaves_the_handles_rows_as_they_were() -> None:
    rows = _roster(_dispatch("failed", rawOutput="Sub-agent execution was cancelled"))
    before = [dict(row) for row in rows]
    [shown] = cards.engine_rows(rows, {})
    assert rows == before
    assert shown["initialQuery"] == PROMPT
    assert shown["status"] == {"type": "failed", "message": "Sub-agent execution was cancelled"}
    assert cards.engine_rows("not a roster", {}) == []
    assert cards.engine_rows([None, 3, "x"], {}) == []


def test_a_started_dispatch_keeps_its_card_if_the_engine_says_pending_again() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    _sync(state, slot, _roster(_dispatch("pending")), engine=True)
    assert _frames(state) == []
    assert slot._native_subagent_tracker[SUBTASK]["done"] is False


# ── a child's tool calls ──────────────────────────────────────────────────────


def test_a_tool_frame_lists_a_call_once_and_renames_it_in_place() -> None:
    info: dict[str, Any] = {}
    first = cards.child_tool_frame(info, "c1", "Wait 20 seconds then print one")
    assert first == {
        "tool": "Wait 20 seconds then print one",
        "tool_call_id": "c1",
        "tool_count": 1,
    }
    # The call's later frames repeat its title: nothing new to list.
    assert cards.child_tool_frame(info, "c1", "Wait 20 seconds then print one") is None
    # A second call, then the first one renamed while the second is the newest.
    assert cards.child_tool_frame(info, "c2", "List a directory")["tool_count"] == 2
    renamed = cards.child_tool_frame(info, "c1", "Print one")
    assert renamed == {
        "tool": "Print one",
        "tool_call_id": "c1",
        "tool_count": 2,
        "is_update": True,
    }
    assert info["last_tool"] == "Print one"


def test_the_remembered_calls_are_bounded_and_the_count_is_not() -> None:
    info: dict[str, Any] = {}
    total = cards.ENGINE_TOOL_CALLS_KEPT + 5
    for i in range(total):
        assert cards.child_tool_frame(info, f"c{i}", f"tool {i}") is not None
    assert len(info["tool_calls"]) == cards.ENGINE_TOOL_CALLS_KEPT
    assert info["tool_count"] == total


def test_a_tool_title_is_cut_and_an_empty_one_lists_nothing() -> None:
    info: dict[str, Any] = {}
    frame = cards.child_tool_frame(info, "c1", "x" * 500)
    assert frame is not None
    assert len(frame["tool"]) == cards.ENGINE_TOOL_TITLE_MAX
    assert cards.child_tool_frame(info, "c2", "") is None
    assert info["tool_count"] == 1


def _turn(state: Any, slot: Any) -> Any:
    return SimpleNamespace(
        state=state,
        slot=slot,
        _native_tracker=slot._native_subagent_tracker,
        _native_tc_card={},
        _native_child_tc=set(),
        _native_card_output=slot._native_subagent_output,
        _native_card_output_len={},
    )


def _tool_event(call: str, title: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=SUBTASK, tool_call_id=call, title=title
    )


@pytest.mark.asyncio
async def test_an_engine_childs_tool_call_reaches_its_card_as_a_tool_frame() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    turn = _turn(state, slot)

    await cr._on_subagent_activity(turn, _tool_event("c1", "Wait 20 seconds then print one"))
    await cr._on_subagent_activity(turn, _tool_event("c1", "Wait 20 seconds then print one"))
    assert _frames(state) == [
        (
            "subagent_tool",
            {
                "id": f"native:{SUBTASK}",
                "slot": "slot-1",
                "tool": "Wait 20 seconds then print one",
                "tool_call_id": "c1",
                "tool_count": 1,
            },
        )
    ]
    assert slot._native_subagent_tracker[SUBTASK]["last_tool"] == "Wait 20 seconds then print one"


@pytest.mark.asyncio
async def test_a_tool_title_holding_a_credential_is_redacted_before_the_cut() -> None:
    """The card's own sync, its output boundary, redacts the title before the helper cuts it."""
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=8)
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    await cr._on_subagent_activity(
        _turn(state, slot), _tool_event("c1", f"curl -H {token} " + "x" * 500)
    )
    [(kind, frame)] = _frames(state)
    assert kind == "subagent_tool"
    assert token not in frame["tool"]
    assert len(frame["tool"]) <= cards.ENGINE_TOOL_TITLE_MAX
    assert token not in repr(slot._native_subagent_tracker)


@pytest.mark.asyncio
async def test_a_native_card_outside_an_engine_turn_gets_no_tool_frame() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=False)
    state.broadcast_ws.reset_mock()
    turn = _turn(state, slot)

    await cr._on_subagent_activity(turn, _tool_event("c1", "Wait 20 seconds then print one"))
    assert _frames(state) == []
    assert turn._native_tc_card == {"c1": f"native:{SUBTASK}"}


def _output_turn(state: Any, slot: Any, backend: str = ACP_BACKEND_KAS) -> Any:
    from kiro_crew.agent_sdk.capabilities import capabilities_for

    turn = _turn(state, slot)
    turn.client = SimpleNamespace(capabilities=capabilities_for(backend))
    turn._native_result_seen = set()
    return turn


def _finished(call: str, output: str) -> AcpEvent:
    """A child's call as the session marks it finished (``acp.kas_child_finished``)."""
    event = _tool_event(call, "echo hi")
    event.tool_final, event.tool_status, event.tool_output = True, "completed", output
    return event


@pytest.mark.asyncio
async def test_a_finished_child_call_puts_its_output_on_its_card_once() -> None:
    """The output reaches the card redacted whole, then cut, once per call; the card
    that closes with no text of the child's own ends on that output."""
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=9)
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()
    turn = _output_turn(state, slot)

    long_tail = "y" * 5000
    await cr._on_subagent_activity(turn, _finished("c1", f"hi {token} {long_tail}"))
    await cr._on_subagent_activity(turn, _finished("c1", f"hi {token} {long_tail}"))

    chunks = [f for kind, f in _frames(state) if kind == "subagent_chunk"]
    assert len(chunks) == 1
    [chunk] = chunks
    assert chunk["id"] == f"native:{SUBTASK}" and chunk["text"].startswith("hi ")
    assert token not in chunk["text"] and len(chunk["text"]) <= 4001
    assert token not in repr(slot._native_subagent_output)

    _sync(state, slot, _roster(_dispatch("in_progress"), _dispatch("completed")), engine=True)
    [done] = [f for kind, f in _frames(state) if kind == "subagent_done"]
    assert done["result"].startswith("hi ") and token not in done["result"]


def test_the_finished_call_set_is_bounded_by_key_and_by_count() -> None:
    from kiro_crew.dashboard.chat_turn import native_cards

    seen: set[str] = set()
    long_id = "c" * 5000
    assert native_cards._first_native_result(seen, long_id)
    assert not native_cards._first_native_result(seen, long_id)
    assert all(len(key) <= 600 for key in seen)
    for n in range(native_cards.NATIVE_RESULT_SEEN_CAP * 2):
        native_cards._first_native_result(seen, f"call-{n}")
    assert len(seen) <= native_cards.NATIVE_RESULT_SEEN_CAP


@pytest.mark.asyncio
async def test_an_unfinished_call_or_another_backend_puts_no_output_on_the_card() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    state.broadcast_ws.reset_mock()

    running = _finished("c1", "partial")
    running.tool_final, running.tool_status = False, "in_progress"
    await cr._on_subagent_activity(_output_turn(state, slot), running)
    await cr._on_subagent_activity(
        _output_turn(state, slot, ACP_BACKEND_KIRO), _finished("c2", "hi")
    )
    assert [kind for kind, _f in _frames(state) if kind == "subagent_chunk"] == []


@pytest.mark.asyncio
async def test_a_finished_engine_card_takes_no_more_tool_frames() -> None:
    state, slot = _state(), _slot()
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    _sync(state, slot, _roster(_dispatch("completed", rawOutput="one")), engine=True)
    state.broadcast_ws.reset_mock()

    await cr._on_subagent_activity(_turn(state, slot), _tool_event("c9", "late frame"))
    assert _frames(state) == []


# ── which turns are engine turns ──────────────────────────────────────────────


def _reading_turn(handle: Any = None) -> Any:
    """A turn whose slot published a live client holding *handle*."""
    return SimpleNamespace(_engine_children=None, slot=SimpleNamespace(_acp_client=client(handle)))


def test_the_reading_is_the_sessions_registration_and_is_taken_once_per_turn() -> None:
    handle = registered()
    turn = _reading_turn(handle)

    assert cards.turn_runs_engine_children(turn) is True
    # The registration changing under a running turn does not re-mark its cards.
    handle.kas_shipped_helper_parent = ""
    assert cards.turn_runs_engine_children(turn) is True


def test_the_agent_the_session_runs_now_is_the_one_the_mount_is_read_for() -> None:
    handle = registered(mounted_for="kirocrew", agent="my-reviewer")
    turn = _reading_turn(handle)

    assert cards.turn_runs_engine_children(turn) is False
    # ``False`` is a reading too: it is not taken again.
    handle.kas_projected_agent = "kirocrew"
    assert cards.turn_runs_engine_children(turn) is False


def test_a_session_that_cannot_be_read_keeps_the_cards_as_they_were() -> None:
    class _Gone:
        @property
        def _acp_client(self) -> Any:
            raise RuntimeError("gone")

    turn = SimpleNamespace(_engine_children=None, slot=_Gone())
    assert cards.turn_runs_engine_children(turn) is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "agent", "key", "preview", "expected"),
    [
        (ACP_BACKEND_KAS, "kirocrew", "dashboard:chat-1", "on", True),
        # Registered with the preview off: nothing is marked.
        (ACP_BACKEND_KAS, "kirocrew", "dashboard:chat-1", "off", False),
        # A custom or AIM agent as the parent.
        (ACP_BACKEND_KAS, "my-reviewer", "dashboard:chat-1", "on", False),
        # Every other harness: its sessions register no KAS batch.
        (ACP_BACKEND_KIRO, "kirocrew", "dashboard:chat-1", "on", False),
        (ACP_BACKEND_CLAUDE, "kirocrew", "dashboard:chat-1", "on", False),
        (ACP_BACKEND_CODEX, "kirocrew", "dashboard:chat-1", "on", False),
        # A session that is not a person's dashboard chat.
        (ACP_BACKEND_KAS, "kirocrew", "cron:nightly", "on", False),
        (ACP_BACKEND_KAS, "kirocrew", "subagent:abc123", "on", False),
    ],
)
async def test_only_a_kas_default_agent_dashboard_chat_on_engine_is_marked(
    agents_dir, setting, tmp_path, backend: str, agent: str, key: str, preview: str, expected: bool
) -> None:
    """Through the session's real registration: what ``KasHarness.session_extras``
    mounted under the preview, recorded on a real session handle."""
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("my-reviewer"))
    setting(preview)
    if backend == ACP_BACKEND_KAS:
        handle = _session_handle(await _batch(agent, key), tmp_path, agent=agent, sid="sess-1")
    else:
        # No KAS projection ever runs for it: the handle keeps what it was built with.
        handle = AcpSessionHandle("sess-1", asyncio.Queue(), MagicMock(acp_backend=backend))

    assert cards.turn_runs_engine_children(_reading_turn(handle)) is expected


# ── a reconnect keeps the mark ────────────────────────────────────────────────


def test_a_replayed_card_keeps_the_engine_mark_running_and_finished() -> None:
    state, slot = _state(), _slot()
    state._slots = {slot.key: slot}
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=True)
    [running] = DashboardState.native_subagent_snapshots(state)
    assert running["frame_extras"] == {"engine": True}
    assert running["done"] is False
    assert running["task"] == PROMPT

    _sync(state, slot, _roster(_dispatch("completed", rawOutput="one")), engine=True)
    [done] = DashboardState.native_subagent_snapshots(state)
    assert done["frame_extras"] == {"engine": True}
    assert done["done"] is True


def test_a_replayed_card_outside_an_engine_turn_carries_no_mark() -> None:
    state, slot = _state(), _slot()
    state._slots = {slot.key: slot}
    _sync(state, slot, _roster(_dispatch("in_progress")), engine=False)
    [running] = DashboardState.native_subagent_snapshots(state)
    assert "frame_extras" not in running
