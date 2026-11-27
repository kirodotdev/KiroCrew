"""No reader gets a credential that a display cut of a KAS engine sub-agent's text split.

An engine sub-agent's card, its approval cards and its saved record show model-written
strings, each cut to a display bound: the task, the agent, why a child failed, the
name of the sub-agent that asks, a tool call's title. A credential such a cut splits
(a connection URI whose password ends at the bound, its ``@host`` after it) does not
read as one any more, so a redaction pass that runs after the cut lets the whole
password through. Two passes run after a cut:

* the card's own, with credential redaction in force;
* every reader's who is not the owner, when the owner's setting has redaction stand
  down in the owner's own chat turn (``docs/decisions``: off applies to the owner's
  own view only).

So every such cut is made by one helper (``engine_subagent_cards.shown_cut``), which
reads past the bound and stores the text masked where the cut would split something.
The session handle keeps each string as the engine sent it, to its bound plus that
reach, and cuts none for display.

Every case here is one such URI, placed so its password ends exactly at the bound of
the cut under test, with a different password per cut. A check looks for the start of
the password: a later cut that keeps that much of it is still a leak.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from engine_turn_helpers import live_reading_says_kas, registered, session

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew import redaction_seams
from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_END_TURN,
    AcpEvent,
)
from kiro_crew.dashboard import engine_subagent_cards as cards
from kiro_crew.dashboard import engine_subagent_record as record
from kiro_crew.dashboard import redaction_audience
from kiro_crew.dashboard.chat_turn.native_cards import _MAX_NATIVE_CARD_ERROR
from kiro_crew.dashboard.chat_utils_display import _redact_meta_for_role
from kiro_crew.security import redact

SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
CHILD_CALL = "run_command_toolu_1"
_HOST = "@db.internal/app"
#: How much of a password counts as shown.
_LEAK = 32


def _split_at(cap: int, lead: str = "", fill: str = "7") -> tuple[str, str]:
    """``(text, password)``: a connection URI whose password ends at character *cap*.

    Cut to *cap* first, the ``@host`` is gone and what is left does not read as a
    URI; whole, it does. *fill* tells one cut's password from another's. The
    password returned is its first characters only (see the module docstring).
    """
    head = f"{lead}postgres://admin:"
    password = "Pw" + fill * (cap - len(head) - 2)
    assert len(head + password) == cap
    whole = head + password + _HOST
    start = password[:_LEAK]
    # The premise of every case: the cut is what hides the credential.
    assert start in redact(whole[:cap])
    assert start not in redact(whole)
    return whole, start


#: One password per cut. The task of the first dispatch is split by the card's own
#: bound; the second, shorter one only by the approval card's and the record's.
TASK_SHOWN = children.NATIVE_CHILD_TASK_CAP
AGENT_SHOWN = children.NATIVE_CHILD_LABEL_CAP
REASON_SHOWN = _MAX_NATIVE_CARD_ERROR
ASKER_SHOWN = children.REQUESTER_NAME_CAP
TASK, TASK_PW = _split_at(TASK_SHOWN, "Migrate with ")
BRIEF, BRIEF_PW = _split_at(cards.APPROVAL_TASK_MAX, "Copy from ", "2")
AGENT, AGENT_PW = _split_at(AGENT_SHOWN, fill="5")
REASON, REASON_PW = _split_at(REASON_SHOWN, "refused: ", "3")
ASKER, ASKER_PW = _split_at(ASKER_SHOWN, fill="9")
TITLE, TITLE_PW = _split_at(cards.ENGINE_TOOL_TITLE_MAX, "psql ", "4")
PASSWORDS = (TASK_PW, BRIEF_PW, AGENT_PW, REASON_PW, ASKER_PW, TITLE_PW)
#: A task with a credential the card's cut does not touch: it ends well before the bound.
WHOLE_PW = "Pw" + "8" * 40
UNSPLIT = f"Migrate with postgres://admin:{WHOLE_PW}{_HOST} " + "then report. " * 300

assert cards.APPROVAL_TASK_MAX == record.RECORD_TASK_MAX < len(BRIEF) < TASK_SHOWN


def _dispatch(status: str, task: str = TASK, **more: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "sessionUpdate": "tool_call" if status == "pending" else "tool_call_update",
        "toolCallId": CALL,
        "title": "Sub-agent: kirocrew-helper",
        "status": status,
        "rawInput": {"name": AGENT, "prompt": task},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": SUBTASK}},
    }
    frame.update(more)
    return frame


def _child_request(asker: str) -> dict[str, Any]:
    return {
        "toolCall": {"toolCallId": CHILD_CALL, "status": "pending", "title": "ls"},
        "_meta": {
            "kiro": {
                "toolId": "run_command",
                "requester": {
                    "sessionId": "sess_1",
                    "subagent": {"agentSubtaskId": SUBTASK, "agentName": asker, "depth": 1},
                },
            }
        },
    }


def _child_tool(title: str) -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-call-1",
        "title": title,
        "_meta": {"kiro": {"agentSubtaskId": SUBTASK}},
    }


def _handle() -> Any:
    """The fields the handle's sub-agent parsers read and write, and nothing else of one."""
    return SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )


def _parse(handle: Any, frame: dict[str, Any]) -> list[dict[str, Any]]:
    """The roster the session handle emits for *frame*, as the turn receives it."""
    [event] = children._handle_kas_subagent(handle, frame) or []
    assert event.kind == EVENT_SUBAGENT_LIST
    return [dict(row) for row in event.subagents]


def _sync(state: Any, slot: Any, rows: list[dict[str, Any]]) -> None:
    """Draw *rows* as an engine turn does: the engine pre-pass, then the card's sync."""
    cr._native_subagent_sync(
        state,
        slot,
        list(cards.engine_rows(rows, slot._native_subagent_tracker)),
        slot._native_subagent_tracker,
        slot._native_subagent_output,
    )


def _state_and_slot() -> tuple[Any, Any]:
    state = MagicMock()
    state._native_cards = {}
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    return state, slot


def _frames(state: Any, kind: str) -> list[dict[str, Any]]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == kind]


def _engine_turn(*, engine: bool = True) -> Any:
    return SimpleNamespace(_engine_children=engine, _engine_dispatch_tasks={})


# ── the handle keeps, the card cuts ───────────────────────────────────────────


def test_the_row_keeps_each_shown_string_as_sent_past_its_bound_and_cuts_none() -> None:
    [row] = _parse(_handle(), _dispatch("failed", rawOutput=REASON))
    # Whole here, because each is shorter than its bound plus the reach of the cut.
    assert (row["prompt"], row["target"], row["failure"]) == (TASK, AGENT, REASON)
    assert children.kas_requesting_subagent(_child_request(ASKER)) == ASKER


def test_what_the_handle_keeps_is_what_the_cards_cut_reads() -> None:
    """The card can only see past its cut what the handle kept past it."""
    assert children.NATIVE_CHILD_SHOWN_LOOKAHEAD == cards.SHOWN_LOOKAHEAD
    assert cards.ENGINE_TASK_MAX == TASK_SHOWN
    assert cards.ENGINE_AGENT_MAX == AGENT_SHOWN
    assert cards.ASKER_MAX == ASKER_SHOWN
    # The card's sync clips an error to its own bound: the reason never needs that cut.
    assert cards.ENGINE_REASON_MAX == REASON_SHOWN <= children.NATIVE_CHILD_LABEL_CAP


def test_each_shown_field_of_the_card_is_redacted_whole_and_still_bounded() -> None:
    [row] = _parse(_handle(), _dispatch("failed", rawOutput=REASON))
    [card] = cards.engine_rows([row], {})

    assert card["initialQuery"] == redact(TASK)[: cards.ENGINE_TASK_MAX]
    assert card["agentName"] == redact(AGENT)[: cards.ENGINE_AGENT_MAX]
    assert card["status"]["message"] == redact(REASON)[: cards.ENGINE_REASON_MAX]
    shown = repr((card["initialQuery"], card["agentName"], card["status"]))
    for password in (TASK_PW, AGENT_PW, REASON_PW):
        assert password not in shown
    # The host the password belonged to is still named: the text was not just dropped.
    assert shown.count(_HOST) == 3


def test_the_call_id_a_request_is_matched_by_is_kept_as_it_came() -> None:
    """Never shown, and compared with the id the approval request names: rewriting it
    would lose the card its task."""
    [row] = _parse(_handle(), _dispatch("pending"))
    assert row["dispatch_call"] == CALL


def test_the_asking_sub_agents_name_is_redacted_whole_and_still_bounded() -> None:
    held = children.kas_requesting_subagent(_child_request(ASKER))
    shown = cards.approval_card_fields(_engine_turn(), SimpleNamespace(requesting_subagent=held))
    assert shown == {"asked_by_subagent": redact(ASKER)[: cards.ASKER_MAX]}
    assert ASKER_PW not in shown["asked_by_subagent"]


def test_a_task_the_cut_would_split_reaches_no_card_frame_and_no_record() -> None:
    state, slot = _state_and_slot()
    handle = _handle()
    _sync(state, slot, _parse(handle, _dispatch("in_progress")))

    [spawn] = _frames(state, "subagent_spawn")
    assert spawn["engine"] is True
    assert TASK_PW not in spawn["task"]
    assert _HOST in spawn["task"]
    assert AGENT_PW not in spawn["agent"]

    [entry] = record.engine_record(slot._native_subagent_tracker)["children"]
    assert TASK_PW not in entry["task"] and AGENT_PW not in entry["agent"]


def test_a_failure_reason_the_cut_would_split_reaches_no_card_frame_and_no_record() -> None:
    state, slot = _state_and_slot()
    handle = _handle()
    _sync(state, slot, _parse(handle, _dispatch("in_progress")))
    _sync(state, slot, _parse(handle, _dispatch("failed", rawOutput=REASON)))

    [done] = _frames(state, "subagent_done")
    assert REASON_PW not in done["error"]
    assert _HOST in done["error"]

    stored = record.engine_record(slot._native_subagent_tracker)
    [entry] = stored["children"]
    assert entry["end"] == "failed"
    assert REASON_PW not in entry["reason"]
    assert _HOST in entry["reason"]
    for password in PASSWORDS:
        assert password not in repr(state.broadcast_ws.call_args_list)
        assert password not in repr(slot._native_subagent_tracker)
        assert password not in repr(stored)


def test_the_task_on_the_approval_card_holds_no_password() -> None:
    turn = _engine_turn()
    cards.remember_dispatch_tasks(
        turn._engine_dispatch_tasks, _parse(_handle(), _dispatch("pending"))
    )
    assert set(turn._engine_dispatch_tasks) == {CALL}
    shown = cards.approval_card_fields(turn, SimpleNamespace(tool_call_id=CALL))
    assert TASK_PW not in shown["subagent_task"]
    assert _HOST in shown["subagent_task"]
    assert len(shown["subagent_task"]) <= cards.APPROVAL_TASK_MAX


# ── a real turn: the frames, the approval cards and the saved reply row ───────


def _engine_turn_script(handle: Any, task: str, replays: list[Any]) -> Any:
    """A turn whose engine dispatch waits on its card, starts, makes one tool call,
    asks for approval of another and fails. Each frame is parsed where the session
    handle parses it: inside the stream the turn consumes. *replays* gains what a
    reconnecting page would be replayed, once while the card runs and once after it
    ended."""
    from turn_harness import APPROVED, Do, Emit, TurnScript

    parser = _handle()
    replay = Do(lambda ctx: replays.append(ctx.state.native_subagent_snapshots()))

    def roster(frame: dict[str, Any]) -> Emit:
        return Emit(lambda ctx: AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_parse(parser, frame)))

    def child_tool(ctx: Any) -> AcpEvent:
        [event] = children._build_child_tool_activity_prefix(parser, _child_tool(TITLE))
        return event

    return TurnScript(
        events=[
            roster(_dispatch("pending", task)),
            AcpEvent(
                kind=EVENT_PERMISSION_REQUEST,
                request_id="req-dispatch",
                tool_call_id=CALL,
                title="Sub-agent: kirocrew-helper",
                tool_kind="other",
            ),
            roster(_dispatch("in_progress", task)),
            Emit(child_tool),
            replay,
            Emit(
                lambda ctx: AcpEvent(
                    kind=EVENT_PERMISSION_REQUEST,
                    request_id="req-child",
                    tool_call_id=CHILD_CALL,
                    title="ls",
                    tool_kind="execute",
                    requesting_subagent=children.kas_requesting_subagent(_child_request(ASKER)),
                )
            ),
            roster(_dispatch("failed", task, rawOutput=REASON)),
            replay,
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="The sub-agent failed."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ],
        answers={"req-dispatch": APPROVED, "req-child": APPROVED},
        provider=session(handle),
    )


_PREVIEW_ON = {"dashboard": {"feature_previews": {"kas_subagents": True}}}


async def _turn_and_replays(
    monkeypatch: pytest.MonkeyPatch, *, redaction_off: bool, task: str = TASK
) -> tuple[Any, list[Any]]:
    """A real dashboard turn on a session that mounted the engine's tool, and the two
    replays taken during it."""
    from turn_harness import run_turn

    monkeypatch.setattr(redaction_seams, "_redaction_off", redaction_off)
    live_reading_says_kas(monkeypatch)
    replays: list[Any] = []
    turn = await run_turn(_engine_turn_script(registered(), task, replays), config=_PREVIEW_ON)
    return turn, replays


async def _turn(monkeypatch: pytest.MonkeyPatch, *, redaction_off: bool, task: str = TASK) -> Any:
    turn, _replays = await _turn_and_replays(monkeypatch, redaction_off=redaction_off, task=task)
    return turn


def _record_of(turn: Any) -> dict[str, Any]:
    [reply] = [row for row in turn.history_rows if record.RECORD_META_KEY in row.get("meta", {})]
    return reply


@pytest.mark.asyncio
async def test_a_turn_shows_and_saves_no_password_the_storage_cut_would_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redaction in force: the ``subagent_spawn`` frame's task, the ``subagent_done``
    frame's error, the tool frame, both approval cards and the record saved on the
    reply row."""
    turn = await _turn(monkeypatch, redaction_off=False)

    [spawn] = [f.payload for f in turn.frames("subagent_spawn")]
    assert spawn["engine"] is True
    assert TASK_PW not in spawn["task"] and _HOST in spawn["task"]
    assert AGENT_PW not in spawn["agent"]
    [tool] = [f.payload for f in turn.frames("subagent_tool")]
    assert TITLE_PW not in tool["tool"] and len(tool["tool"]) <= cards.ENGINE_TOOL_TITLE_MAX
    [done] = [f.payload for f in turn.frames("subagent_done")]
    assert REASON_PW not in done["error"] and _HOST in done["error"]

    dispatch_card, child_card = turn.approval_cards
    assert TASK_PW not in dispatch_card["meta"]["subagent_task"]
    assert _HOST in dispatch_card["meta"]["subagent_task"]
    assert ASKER_PW not in child_card["meta"]["asked_by_subagent"]

    [entry] = _record_of(turn)["meta"][record.RECORD_META_KEY]["children"]
    assert entry["end"] == "failed"
    assert _HOST in entry["task"] and _HOST in entry["reason"]

    everything = repr((turn.ws_frames, turn.history_rows, turn.window))
    for password in PASSWORDS:
        assert password not in everything


# ── the owner's setting off: what every other reader gets ─────────────────────


def _to_a_non_owner(frame: Any) -> dict[str, Any]:
    """*frame*'s payload as a socket that is not the owner's receives it."""
    text = json.dumps({"type": frame.kind, "data": frame.payload})
    shown = redaction_audience.for_socket({}, text, owner=False)
    assert shown is not None
    return json.loads(shown)["data"]


def _read_back_by_a_non_owner(row: Any) -> dict[str, Any]:
    """A saved row's meta as a reader who is not the owner is served it: the row's own
    load pass, run in full."""
    return redaction_audience._in_full(_redact_meta_for_role, row["role"], dict(row["meta"]))


def _spawn_task(turn: Any) -> str:
    [frame] = turn.frames("subagent_spawn")
    return _to_a_non_owner(frame)["task"]


def _spawn_agent(turn: Any) -> str:
    [frame] = turn.frames("subagent_spawn")
    return _to_a_non_owner(frame)["agent"]


def _done_error(turn: Any) -> str:
    [frame] = turn.frames("subagent_done")
    return _to_a_non_owner(frame)["error"]


def _tool_title(turn: Any) -> str:
    [frame] = turn.frames("subagent_tool")
    return _to_a_non_owner(frame)["tool"]


def _approval_card(turn: Any, which: int) -> str:
    """The turn's first approval card (the dispatch's) or its second (the child's call)."""
    shown = [f for f in turn.frames("chat_message") if f.payload.get("role") == "permission"]
    assert len(shown) == 2
    return json.dumps(_to_a_non_owner(shown[which]))


def _saved_record(turn: Any) -> str:
    return json.dumps(_read_back_by_a_non_owner(_record_of(turn))[record.RECORD_META_KEY])


#: Each cut this draft makes of a shown string, with the reader's copy it reaches:
#: ``(the dispatch's task, the copy, the password that cut splits)``.
_CUTS = {
    "card-task": (TASK, _spawn_task, TASK_PW),
    "card-agent": (TASK, _spawn_agent, AGENT_PW),
    "card-error": (TASK, _done_error, REASON_PW),
    "tool-title": (TASK, _tool_title, TITLE_PW),
    "approval-asker": (TASK, lambda turn: _approval_card(turn, 1), ASKER_PW),
    "approval-task": (BRIEF, lambda turn: _approval_card(turn, 0), BRIEF_PW),
    "record-task": (BRIEF, _saved_record, BRIEF_PW),
    "record-agent-reason-tools": (TASK, _saved_record, None),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", sorted(_CUTS))
async def test_with_the_owners_setting_off_no_other_reader_gets_a_credential_a_cut_split(
    monkeypatch: pytest.MonkeyPatch, cut: str
) -> None:
    task, copy_of, password = _CUTS[cut]
    turn = await _turn(monkeypatch, redaction_off=True, task=task)

    shown = copy_of(turn)

    for leaked in PASSWORDS if password is None else (password,):
        assert leaked not in shown
    assert _HOST in shown, "the text was dropped, not masked"


class _NotTheOwnersSocket(dict):  # type: ignore[type-arg]
    """A page socket that is not the owner's, keeping what it was sent."""

    def __init__(self) -> None:
        super().__init__()
        self.sent: list[str] = []

    async def send_str(self, text: str) -> None:
        self.sent.append(text)

    async def send_json(self, frame: Any) -> None:
        self.sent.append(json.dumps(frame))


@pytest.mark.asyncio
@pytest.mark.parametrize(("card", "at"), [("running", 0), ("finished", 1)])
async def test_with_the_owners_setting_off_a_replayed_card_gives_no_other_reader_one_either(
    monkeypatch: pytest.MonkeyPatch, card: str, at: int
) -> None:
    """A page that connects later is replayed the card from the slot's own record
    (the Subagents panel), through the replay's own send to a socket that is not the
    owner's."""
    from kiro_crew.dashboard import ws_subagent_send

    _turn_record, replays = await _turn_and_replays(monkeypatch, redaction_off=True)
    [snapshot] = replays[at]
    assert snapshot["done"] is (card == "finished")
    socket = _NotTheOwnersSocket()
    kind = "subagent_done" if snapshot["done"] else "subagent_snapshot"
    state = SimpleNamespace(_owner_ws_clients=())

    await ws_subagent_send._send_frame(socket, state, {"type": kind, "data": snapshot})

    [shown] = socket.sent
    for password in PASSWORDS:
        assert password not in shown
    assert _HOST in shown, "the text was dropped, not masked"


@pytest.mark.asyncio
async def test_with_the_owners_setting_off_the_owners_own_card_shows_the_task_as_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The owner's one redaction setting still decides the owner's own view: a task
    whose credential no cut touches is shown as written, cut. Every other reader's
    copy of that same frame has it masked, because it is whole there."""
    turn = await _turn(monkeypatch, redaction_off=True, task=UNSPLIT)

    [frame] = turn.frames("subagent_spawn")
    assert frame.payload["task"] == UNSPLIT[:TASK_SHOWN]
    assert WHOLE_PW in frame.payload["task"]
    assert WHOLE_PW not in _to_a_non_owner(frame)["task"]


@pytest.mark.asyncio
async def test_with_the_owners_setting_off_a_task_the_cut_would_split_is_stored_masked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one case the owner's own view changes: the bound falls where a scan of the
    whole text masks. The stored text is then the masked one, on every surface."""
    turn = await _turn(monkeypatch, redaction_off=True)

    [spawn] = [f.payload for f in turn.frames("subagent_spawn")]
    assert TASK_PW not in spawn["task"]
    assert spawn["task"].startswith("Migrate with postgres://admin:") and _HOST in spawn["task"]
    everything = repr((turn.ws_frames, turn.history_rows, turn.window))
    for password in PASSWORDS:
        assert password not in everything


# ── a child's tool call whose title the handle cuts when the call finishes ────
#
# The handle names a finished child call by the title it kept from the call's
# earlier frames (``acp.kas_child_finished``), cut to that module's own bound, and
# the card lists the call from that event.

#: A command whose credential starts inside the first 80 characters and runs past
#: the bound the handle keeps a finished call's title to.
_LONG_PW = "Pw" + "6" * 700
LONG_COMMAND = f"psql postgres://admin:{_LONG_PW}{_HOST} -c 'select 1'"
assert LONG_COMMAND.index(_LONG_PW) < cards.ENGINE_TOOL_TITLE_MAX and len(LONG_COMMAND) > 512
assert _LONG_PW[:_LEAK] not in redact(LONG_COMMAND)


def _real_handle() -> Any:
    """A real KAS session handle: its own parser, caches and finished-call marking."""
    import asyncio
    from unittest.mock import AsyncMock

    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS

    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sB", asyncio.Queue(), rt)


def _child_call_script(handle: Any) -> Any:
    """An engine child starts, then makes one shell call that finishes: each frame goes
    through a real handle where the turn's stream is consumed."""
    from turn_harness import Emit, TurnScript

    from kiro_crew.acp.types import JsonRpcMessage

    real = _real_handle()
    child = {"kiro": {"agentSubtaskId": SUBTASK}}
    started = {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-tc",
        "title": "Run Command",
        "kind": "execute",
        "status": "in_progress",
        "rawInput": {"command": LONG_COMMAND},
        "_meta": child,
    }
    finished = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "child-tc",
        "status": "completed",
        "rawOutput": {"stdout": "1"},
        "_meta": child,
    }

    def through_the_handle(frame: dict[str, Any]) -> Emit:
        def make(ctx: Any) -> AcpEvent:
            [event] = real._handle_update(
                JsonRpcMessage(method="session/update", params={"update": frame})
            )
            return event

        return Emit(make)

    return TurnScript(
        events=[
            through_the_handle(_dispatch("in_progress", "run the query")),
            through_the_handle(started),
            through_the_handle(finished),
            AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
            AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN),
        ],
        provider=session(handle),
    )


def test_the_handle_keeps_a_finished_calls_title_as_far_as_the_cards_cut_reads() -> None:
    from kiro_crew.acp import kas_child_finished

    kept, stored = kas_child_finished._TITLE_KEPT_CHARS, kas_child_finished._TITLE_MAX_CHARS
    assert kept - stored == cards.SHOWN_LOOKAHEAD
    assert kept >= cards.ENGINE_TOOL_TITLE_MAX + cards.SHOWN_LOOKAHEAD


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("redaction_off", "readers"),
    # With redaction in force the parser redacts the whole title before the handle
    # cuts it. With the owner's setting off it is the card's cut that masks it, which
    # needs the title kept as far as that cut reads.
    [(False, ("the owner", "not the owner")), (True, ("not the owner",))],
    ids=["setting-on-every-reader", "setting-off-a-reader-not-the-owner"],
)
async def test_a_finished_child_calls_long_title_gives_that_reader_no_part_of_its_credential(
    monkeypatch: pytest.MonkeyPatch, redaction_off: bool, readers: tuple[str, ...]
) -> None:
    from turn_harness import run_turn

    monkeypatch.setattr(redaction_seams, "_redaction_off", redaction_off)
    live_reading_says_kas(monkeypatch)

    turn = await run_turn(_child_call_script(registered()), config=_PREVIEW_ON)

    listed = turn.frames("subagent_tool")
    assert listed, "the card listed no tool call"
    for frame in listed:
        copies = {"the owner": frame.payload, "not the owner": _to_a_non_owner(frame)}
        for reader in readers:
            assert _LONG_PW[:_LEAK] not in copies[reader]["tool"], reader
