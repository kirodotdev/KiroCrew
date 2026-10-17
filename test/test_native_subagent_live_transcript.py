"""A native kiro-cli sub-agent's work reaches its Subagents-panel card live.

kiro-cli runs a native ``subagent`` crew inside the parent process and tags
every child frame -- tool calls, their results, reasoning, text -- with the
CHILD's session id on a plain ``session/update``. The handle re-tags them as
``EVENT_SUBAGENT_ACTIVITY`` (never parent transcript), and
``_native_subagent_activity`` in ``chat_runner`` projects them onto the
child's card as ``subagent_tool`` / ``subagent_tool_result`` /
``subagent_thinking`` / ``subagent_chunk`` frames.

The frames below are the shapes a recorded kiro-cli 2.31 run produced for one
child shell call: the extension-spelling ``tool_call_chunk`` naming the bare
tool, the plain ``tool_call`` with the command-bearing title and ``rawInput``,
a content-only ``tool_call_update``, the terminal ``tool_call_update`` with
``status: completed`` + ``rawOutput`` (the SAME output again), and an
``agent_message_chunk``. Before this projection a child that only called
tools left its card on "Starting…" / "Waiting for output…" for its whole run.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    METHOD_SESSION_UPDATE,
    SUBAGENT_ACTIVITY_THINKING,
    SUBAGENT_ACTIVITY_TOOL_RESULT,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.dashboard.chat_runner import (
    _native_done_result,
    _native_subagent_activity,
    _native_subagent_close_all,
    _native_subagent_sync,
)
from kiro_crew.dashboard.native_subagent_cards import (
    NATIVE_SUBAGENT_CALL_ID_CHARS,
    NATIVE_SUBAGENT_CALLS_MAX,
    NATIVE_SUBAGENT_INPUT_CHARS,
    NATIVE_SUBAGENT_RESULT_CHARS,
    NATIVE_SUBAGENT_UNNAMED_FEED_LINE,
)
from kiro_crew.dashboard.state import NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE

PARENT = "175b75a1-parent"
CHILD = "dbbf290c-child"
TCID = "toolu_bdrk_019Gx7yVTnwcGidAPpdncDV2"


# ── handle: child frames become tagged activity ───────────────────────────────


def _handle() -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.acp_backend = "kiro"
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle(PARENT, asyncio.Queue(), rt)


def _child(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_SESSION_UPDATE, params={"sessionId": CHILD, "update": update}
    )


def _call_frame() -> dict:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": TCID,
        "title": "Running: echo probe-hello && uname -s",
        "kind": "execute",
        "rawInput": {
            "__tool_use_purpose": "Run the probe command as instructed.",
            "command": "echo probe-hello && uname -s",
        },
        "_meta": {"kiro": {"toolName": "shell"}},
    }


def _content_result_frame() -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": TCID,
        "content": [
            {"type": "content", "content": {"type": "text", "text": "probe-hello\nLinux\n"}}
        ],
    }


def _terminal_result_frame() -> dict:
    return {
        "sessionUpdate": "tool_call_update",
        "toolCallId": TCID,
        "kind": "execute",
        "status": "completed",
        "title": "Running: echo probe-hello && uname -s",
        "rawInput": {"command": "echo probe-hello && uname -s"},
        "rawOutput": {
            "items": [
                {
                    "Json": {
                        "exit_status": "exit status: 0",
                        "stdout": "probe-hello\nLinux\n",
                        "stderr": "",
                    }
                }
            ]
        },
    }


def test_child_tool_call_retags_with_input_and_kind():
    events = _handle()._handle_update(_child(_call_frame()))

    assert [e.kind for e in events] == [EVENT_SUBAGENT_ACTIVITY]
    ev = events[0]
    assert ev.sub_session_id == CHILD
    assert ev.activity == ""
    assert ev.tool_call_id == TCID
    assert "echo probe-hello" in ev.title
    assert ev.tool_kind == "execute"
    assert "echo probe-hello && uname -s" in ev.tool_input
    assert ev.tool_purpose == "Run the probe command as instructed."


def test_child_tool_result_retags_as_tool_result_activity():
    handle = _handle()
    handle._handle_update(_child(_call_frame()))

    content = handle._handle_update(_child(_content_result_frame()))
    terminal = handle._handle_update(_child(_terminal_result_frame()))

    assert [e.kind for e in content] == [EVENT_SUBAGENT_ACTIVITY]
    assert content[0].activity == SUBAGENT_ACTIVITY_TOOL_RESULT
    assert content[0].tool_call_id == TCID
    assert "probe-hello" in content[0].tool_output
    assert content[0].tool_final is False
    # The terminal frame refines the SAME call (title + input) and closes it.
    kinds = {(e.activity, e.tool_final) for e in terminal if e.kind == EVENT_SUBAGENT_ACTIVITY}
    assert (SUBAGENT_ACTIVITY_TOOL_RESULT, True) in kinds
    assert all(e.kind == EVENT_SUBAGENT_ACTIVITY for e in terminal)
    assert all(e.sub_session_id == CHILD for e in content + terminal)


def test_child_thinking_retags_as_thinking_activity():
    events = _handle()._handle_update(
        _child(
            {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "weighing options"},
            }
        )
    )

    assert [(e.kind, e.activity, e.text) for e in events] == [
        (EVENT_SUBAGENT_ACTIVITY, SUBAGENT_ACTIVITY_THINKING, "weighing options")
    ]


def test_child_text_retags_untagged_as_before():
    events = _handle()._handle_update(
        _child(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}}
        )
    )

    assert [(e.kind, e.activity, e.text) for e in events] == [(EVENT_SUBAGENT_ACTIVITY, "", "done")]


def test_child_frames_never_become_parent_transcript():
    handle = _handle()
    frames = [
        _call_frame(),
        _content_result_frame(),
        _terminal_result_frame(),
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "t"}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}},
    ]
    kinds = {e.kind for f in frames for e in handle._handle_update(_child(f))}
    assert kinds == {EVENT_SUBAGENT_ACTIVITY}
    assert not kinds & {EVENT_TOOL_CALL, EVENT_TOOL_RESULT, EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK}


# ── chat_runner: tagged activity becomes card frames ──────────────────────────


class _Projection:
    """One turn's native-card state, as ``_run_chat`` holds it."""

    def __init__(self) -> None:
        self.state = MagicMock()
        self.state.broadcast_ws = MagicMock()
        self.slot = MagicMock()
        self.slot.key = "slot-1"
        self.tracker: dict[str, dict] = {}
        self.output: dict[str, list[str]] = {}
        self.output_len: dict[str, int] = {}
        self.tc_card: dict[str, str] = {}
        self.result_seen: dict[str, int] = {}
        self.streams: dict[str, dict] = {}

    def spawn(self, sid: str = CHILD) -> str:
        _native_subagent_sync(
            self.state,
            self.slot,
            [
                {
                    "sessionId": sid,
                    "sessionName": "child",
                    "agentName": "probe-child",
                    "initialQuery": "Run the shell command",
                    "status": {"type": "working", "message": "Running"},
                }
            ],
            self.tracker,
            self.output,
        )
        return self.tracker[sid]["id"]

    def feed(self, event: AcpEvent) -> None:
        _native_subagent_activity(
            self.state,
            self.slot,
            event,
            self.tracker,
            self.output,
            self.output_len,
            self.tc_card,
            self.result_seen,
            self.streams,
        )

    def done(self, sid: str = CHILD, status: str = "terminated") -> dict:
        """kiro-cli's roster frame closing the child; returns its done frame."""
        _native_subagent_sync(
            self.state,
            self.slot,
            [
                {
                    "sessionId": sid,
                    "sessionName": "child",
                    "agentName": "probe-child",
                    "initialQuery": "Run the shell command",
                    "status": {"type": status},
                }
            ],
            self.tracker,
            self.output,
            self.output_len,
            self.streams,
        )
        (frame,) = self.frames("subagent_done")
        return frame

    def frames(self, etype: str) -> list[dict]:
        return [c.args[1] for c in self.state.broadcast_ws.call_args_list if c.args[0] == etype]


def _activity(**fields) -> AcpEvent:
    return AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=CHILD, **fields)


def test_tool_call_projects_a_tool_frame_and_counts_once_per_call():
    p = _Projection()
    card = p.spawn()

    # kiro-cli: bare tool name first (extension spelling), then the command-bearing title.
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(
        _activity(
            tool_call_id=TCID,
            title="Running: echo probe-hello",
            tool_kind="execute",
            tool_input='{"command": "echo probe-hello"}',
        )
    )

    tools = p.frames("subagent_tool")
    assert [t["tool"] for t in tools] == ["shell", "Running: echo probe-hello"]
    assert all(
        t["id"] == card and t["slot"] == "slot-1" and t["tool_call_id"] == TCID for t in tools
    )
    assert [t["tool_count"] for t in tools] == [1, 1], "a refinement is the same call"
    # The frame carries what the row shows; the kind has no reader on the card.
    assert all("tool_kind" not in t for t in tools)
    assert "echo probe-hello" in tools[1]["input"]
    assert "input" not in tools[0]
    assert p.tc_card[TCID] == card
    assert p.tracker[CHILD]["last_tool"] == "Running: echo probe-hello"
    assert p.tracker[CHILD]["tool_count"] == 1
    # The roster frame alone still says nothing about progress.
    assert p.frames("subagent_chunk") == []


def test_second_call_moves_the_count():
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id="a", title="shell"))
    p.feed(_activity(tool_call_id="b", title="read"))

    assert [t["tool_count"] for t in p.frames("subagent_tool")] == [1, 2]


def test_tool_result_projects_output_once_from_the_terminal_frame():
    """kiro-cli sends a call's output twice: a content-only frame while the
    call is in progress, then the terminal frame with ``status: completed`` and
    the same text as ``rawOutput``. One frame reaches the card -- the terminal
    one, output and status together; the content frame, carrying no status,
    projects nothing (its text is kept as the call's pending fragment)."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: echo probe-hello"))

    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id=TCID,
            tool_output="probe-hello\nLinux\n",
        )
    )
    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id=TCID,
            tool_output="probe-hello\nLinux\n",
            tool_status="completed",
            tool_final=True,
        )
    )

    results = p.frames("subagent_tool_result")
    assert results == [
        {
            "id": card,
            "slot": "slot-1",
            "tool_call_id": TCID,
            "status": "completed",
            "output": "probe-hello\nLinux\n",
        }
    ]
    # The feed (the done-time result) names the call once, under its title --
    # without kiro-cli's "Running:" status word, which is false on the finished
    # card the feed is read on (the rows strip it the same way).
    feed = _native_done_result(p.output[card])
    assert feed.count("probe-hello\nLinux") == 1
    assert "\u2192 echo probe-hello\nprobe-hello\nLinux\n" in feed
    assert "Running:" not in feed
    assert TCID in p.result_seen


def test_tool_result_without_output_or_status_is_nothing():
    p = _Projection()
    p.spawn()

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_TOOL_RESULT, tool_call_id=TCID))

    assert p.frames("subagent_tool_result") == []


def test_tool_result_is_bounded_and_redacted():
    p = _Projection()
    p.spawn()
    secret = "AKIAIOSFODNN7EXAMPLE"
    long = "x" * (NATIVE_SUBAGENT_RESULT_CHARS + 500)

    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id="a",
            tool_output=f"aws_access_key_id = {secret}\n" + long,
            tool_status="completed",
            tool_final=True,
        )
    )

    (frame,) = p.frames("subagent_tool_result")
    assert secret not in frame["output"]
    assert len(frame["output"]) <= NATIVE_SUBAGENT_RESULT_CHARS


def test_tool_input_is_bounded():
    p = _Projection()
    p.spawn()

    p.feed(
        _activity(
            tool_call_id="a", title="shell", tool_input="y" * (NATIVE_SUBAGENT_INPUT_CHARS + 50)
        )
    )

    (frame,) = p.frames("subagent_tool")
    assert len(frame["input"]) == NATIVE_SUBAGENT_INPUT_CHARS


def test_thinking_projects_its_own_frame_and_stays_out_of_the_feed():
    p = _Projection()
    card = p.spawn()

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text="weighing options\n"))

    assert p.frames("subagent_thinking") == [
        {"id": card, "slot": "slot-1", "text": "weighing options\n"}
    ]
    assert p.frames("subagent_chunk") == []
    assert card not in p.output


def test_the_provider_copy_keeps_the_activity_tag_on_the_way_to_the_card():
    """The dashboard reads the provider's stream, not the handle's: every
    ``AcpEvent`` is rebuilt by ``AcpProvider._to_llm_event`` first. A copy that
    drops ``activity`` would reach the card untagged -- the child's result
    admitted as a new call, its reasoning shown as its answer -- so the child's
    thinking and result frames go through that copy here."""
    from kiro_crew.providers.acp import AcpProvider

    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: echo probe-hello"))

    p.feed(
        AcpProvider._to_llm_event(
            _activity(activity=SUBAGENT_ACTIVITY_THINKING, text="weighing options\n")
        )
    )
    p.feed(
        AcpProvider._to_llm_event(
            _result(TCID, tool_output="probe-hello\n", tool_status="completed", tool_final=True)
        )
    )

    assert p.frames("subagent_thinking") == [
        {"id": card, "slot": "slot-1", "text": "weighing options\n"}
    ]
    assert [t["tool_count"] for t in p.frames("subagent_tool")] == [1], "a result is not a call"
    assert [(r["tool_call_id"], r["output"]) for r in p.frames("subagent_tool_result")] == [
        (TCID, "probe-hello\n")
    ]
    assert p.frames("subagent_chunk") == []


def test_text_projects_a_chunk_into_the_feed_as_before():
    p = _Projection()
    card = p.spawn()

    p.feed(_activity(text="done\n"))

    assert p.frames("subagent_chunk") == [{"id": card, "slot": "slot-1", "text": "done\n"}]
    assert _native_done_result(p.output[card]) == "done\n"


def test_activity_for_an_unknown_or_finished_child_is_dropped():
    p = _Projection()
    p.feed(_activity(tool_call_id="a", title="shell"))
    assert p.state.broadcast_ws.call_args_list == []

    p.spawn()
    p.tracker[CHILD]["done"] = True
    p.feed(_activity(tool_call_id="a", title="shell"))
    assert p.frames("subagent_tool") == []


def test_a_finished_childs_call_is_still_attributed_to_its_card():
    """Nothing is projected for a closed child, but its call id is still
    mapped: the flat arms refuse a child's session-bound directive on that
    map, and a child's calls do not stop being the child's when its roster
    entry closes before its last frames arrive."""
    p = _Projection()
    card = p.spawn()
    p.tracker[CHILD]["done"] = True

    p.feed(_activity(tool_call_id="late", title="shell"))
    p.feed(_activity(tool_call_id="late", activity=SUBAGENT_ACTIVITY_TOOL_RESULT, tool_output="x"))

    assert p.tc_card == {"late": card}
    assert p.frames("subagent_tool") == [] and p.frames("subagent_tool_result") == []
    assert "tool_count" not in p.tracker[CHILD]


def test_a_closed_childs_call_past_the_bound_is_counted_as_refused(monkeypatch):
    """A refusal is counted on the record whether or not the child is done:
    the directive gate reads every record's count until the turn ends, and a
    closed child's refused call would otherwise leave no trace of being a
    child's. The closed record still takes no title and no count of calls."""
    from kiro_crew.dashboard import native_subagent_cards

    monkeypatch.setattr(native_subagent_cards, "NATIVE_SUBAGENT_CALLS_MAX", 1)
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id="first", title="shell"))
    p.tracker[CHILD]["done"] = True

    p.feed(_activity(tool_call_id="late", title="shell"))

    assert p.tc_card == {"first": card}
    assert p.tracker[CHILD]["calls_refused"] == 1
    assert p.tracker[CHILD]["tool_count"] == 1
    assert "late" not in p.tracker[CHILD].get("call_titles", {})


def test_an_uncarded_childs_call_is_attributed_to_the_child_not_the_parent():
    """kiro-cli sometimes announces a child with an empty initialQuery and
    sessionName; the sync records it done at once and spawns no card. Its
    calls are still the child's, under the id the flat arms would have used."""
    p = _Projection()
    _native_subagent_sync(
        p.state,
        p.slot,
        [
            {
                "sessionId": CHILD,
                "initialQuery": "",
                "sessionName": "",
                "status": {"type": "working"},
            }
        ],
        p.tracker,
        p.output,
    )
    assert p.tracker[CHILD]["done"] is True and "id" not in p.tracker[CHILD]

    p.feed(_activity(tool_call_id="a", title="shell"))

    assert list(p.tc_card) == ["a"] and p.tc_card["a"].startswith("native:")
    assert p.frames("subagent_spawn") == [] and p.frames("subagent_tool") == []


# ── a call first seen through its result ──────────────────────────────────────
#
# A child starts working the moment it exists, so its first ``tool_call`` can
# arrive before the roster frame that admits it -- and a frame for a child the
# tracker does not hold is dropped. The parser then turns the call's terminal
# ``tool_call_update`` into a result event followed by a refinement, in that
# order, so the first frame the projection sees of such a call is its RESULT.


def _result_first_call(p: _Projection, tcid: str = TCID, title: str = "Running: ls") -> None:
    """The terminal update of a call whose opening frame the child's admission missed."""
    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id=tcid,
            tool_output="a.txt\n",
            tool_status="completed",
            tool_final=True,
        )
    )
    p.feed(_activity(tool_call_id=tcid, title=title, tool_input='{"command": "ls"}'))


def test_a_result_first_call_is_admitted_on_its_result_and_titled_by_its_refinement():
    """The result counts and slots the call, so the refinement that follows reads
    a counter that is set (``KeyError('tool_count')`` here aborted the parent
    turn) and names the row the result opened."""
    p = _Projection()
    card = p.spawn()

    _result_first_call(p)

    (result,) = p.frames("subagent_tool_result")
    assert result == {
        "id": card,
        "slot": "slot-1",
        "tool_call_id": TCID,
        "status": "completed",
        "output": "a.txt\n",
    }
    (tool,) = p.frames("subagent_tool")
    assert tool["tool"] == "Running: ls" and tool["tool_call_id"] == TCID
    assert tool["tool_count"] == 1
    assert p.tracker[CHILD]["tool_count"] == 1
    assert p.tracker[CHILD]["call_titles"] == {TCID: "Running: ls"}
    assert p.tracker[CHILD]["last_tool"] == "Running: ls"


def test_a_result_first_call_is_counted_once_and_a_later_call_moves_the_count():
    p = _Projection()
    p.spawn()

    _result_first_call(p, "a")
    p.feed(_activity(tool_call_id="a", title="Running: ls -l"))  # a second refinement
    p.feed(_activity(tool_call_id="b", title="read"))

    assert [t["tool_count"] for t in p.frames("subagent_tool")] == [1, 1, 2]
    assert p.tracker[CHILD]["tool_count"] == 2


def test_a_result_first_calls_output_joins_the_feed_untitled():
    """The result arrives before any title exists for the call, so its output
    is filed without a ``→ title`` line rather than held back: the feed is the
    parent's result, and the output is what matters in it."""
    p = _Projection()
    card = p.spawn()

    _result_first_call(p)

    assert _native_done_result(p.output[card]) == "a.txt\n\n"


def test_a_result_first_call_honours_the_title_bounds():
    """Admission on the result applies the same bounds a call's opening frame
    does: an over-long id and a call past the turn's call bound hold no row
    anywhere -- not in the attribution map, the titles or the printed-output
    record -- so the refinement that follows refines nothing, the count stays
    where the bound left it, and the feed files the output under the unnamed
    line. The frames still project."""
    p = _Projection()
    card = p.spawn()
    long_id = "x" * (NATIVE_SUBAGENT_CALL_ID_CHARS + 1)

    _result_first_call(p, long_id)

    assert p.tracker[CHILD].get("tool_count", 0) == 0
    assert p.tracker[CHILD]["calls_refused"] == 2  # the result frame and the refinement
    assert p.tracker[CHILD].get("call_titles", {}) == {}
    assert long_id not in p.tc_card and long_id not in p.result_seen
    (tool,) = p.frames("subagent_tool")
    assert tool["tool"] == "Running: ls" and tool["tool_count"] == 0
    assert _native_done_result(p.output[card]) == f"{NATIVE_SUBAGENT_UNNAMED_FEED_LINE}a.txt\n\n"

    p2 = _Projection()
    p2.spawn()
    for i in range(NATIVE_SUBAGENT_CALLS_MAX):
        p2.feed(_activity(tool_call_id=f"c{i}", title="shell"))
    _result_first_call(p2, "late")

    assert p2.tracker[CHILD]["tool_count"] == NATIVE_SUBAGENT_CALLS_MAX
    assert "late" not in p2.tracker[CHILD]["call_titles"]
    assert "late" not in p2.tc_card and "late" not in p2.result_seen


def test_a_result_first_call_ends_no_reasoning_segment():
    """Admitting a call on its result does not make the result the child's own
    output: a key split across the reasoning chunks either side of it still
    reaches the wire redacted."""
    p = _Projection()
    p.spawn()

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"key is {_AKIA_HEAD}"))
    _result_first_call(p)
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"{_AKIA_TAIL} ok\n"))

    wire = _wire(p, "subagent_thinking")
    assert _AKIA not in wire
    assert "[REDACTED: credential]" in wire
    assert p.frames("subagent_tool") and p.frames("subagent_tool_result")


def test_the_feed_files_a_calls_output_without_the_running_prefix():
    """kiro-cli's ``Running:`` is a status word; on the finished card the feed is
    read on, it is false, so the ``→ title`` line drops it as the rows do."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: npm test"))
    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id=TCID,
            tool_output="ok\n",
            tool_status="completed",
            tool_final=True,
        )
    )

    assert _native_done_result(p.output[card]) == "\u2192 npm test\nok\n\n"
    # The live frame's title is sent as received: one client rule strips it.
    assert p.frames("subagent_tool")[0]["tool"] == "Running: npm test"


def test_done_result_carries_the_projected_feed():
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: echo probe-hello"))
    p.feed(
        _activity(
            activity=SUBAGENT_ACTIVITY_TOOL_RESULT,
            tool_call_id=TCID,
            tool_output="probe-hello\n",
            tool_final=True,
            tool_status="completed",
        )
    )
    p.feed(_activity(text="done"))

    done = p.done()

    assert done["id"] == card
    assert "\u2192 echo probe-hello\nprobe-hello\n" in done["result"]
    assert done["result"].endswith("done")


# ── streams: a credential split across chunks never crosses the wire whole ────

#: A 20-character AWS access-key id, as kiro-cli would stream it: two chunks
#: of ten, neither of which is a credential on its own.
_AKIA = "AKIAIOSFODNN7EXAMPLE"
assert len(_AKIA) == 20
_AKIA_HEAD, _AKIA_TAIL = _AKIA[:10], _AKIA[10:]


def _wire(p: _Projection, etype: str) -> str:
    return "".join(f["text"] for f in p.frames(etype))


def test_a_credential_split_across_two_reasoning_chunks_is_redacted_on_the_wire():
    """Per-chunk redaction cannot see a key whose halves arrive in two chunks,
    and the client joins the chunks back together -- so the child's reasoning
    goes through a rolling redactor, as the parent's does."""
    p = _Projection()
    p.spawn()

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"key is {_AKIA_HEAD}"))
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"{_AKIA_TAIL} ok\n"))

    wire = _wire(p, "subagent_thinking")
    assert _AKIA not in wire
    assert _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert wire.startswith("key is ") and wire.endswith(" ok\n")


def test_a_credential_split_across_two_text_chunks_is_redacted_on_the_wire_and_feed():
    p = _Projection()
    card = p.spawn()

    p.feed(_activity(text=f"token {_AKIA_HEAD}"))
    p.feed(_activity(text=f"{_AKIA_TAIL}.\n"))

    assert _AKIA not in _wire(p, "subagent_chunk")
    assert _AKIA not in _native_done_result(p.output[card])


def test_a_withheld_reasoning_tail_is_flushed_when_the_child_moves_on():
    """A chunk ending mid-token is held back only until the next frame of
    another kind (here a tool call), not lost."""
    p = _Projection()
    card = p.spawn()

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text="plan: run tests"))
    assert _wire(p, "subagent_thinking") == "plan: run "

    p.feed(_activity(tool_call_id=TCID, title="shell"))

    assert _wire(p, "subagent_thinking") == "plan: run tests"
    (tool,) = p.frames("subagent_tool")
    assert tool["id"] == card


def _result(tcid: str, **fields) -> AcpEvent:
    return _activity(activity=SUBAGENT_ACTIVITY_TOOL_RESULT, tool_call_id=tcid, **fields)


def test_a_tool_result_between_two_reasoning_chunks_ends_no_segment():
    """A result is the host reporting on a call, not the child moving on: were
    it to flush the reasoning redactor, the withheld head of a key split across
    the chunks either side of it would cross the wire bare, and the tail after
    it is no credential on its own -- the two halves reassemble on the wire."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"key is {_AKIA_HEAD}"))
    p.feed(_result(TCID, tool_output="ok\n", tool_final=True, tool_status="completed"))
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"{_AKIA_TAIL} ok\n"))

    wire = _wire(p, "subagent_thinking")
    assert _AKIA not in wire
    assert _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert wire.startswith("key is ") and wire.endswith(" ok\n")
    (result,) = p.frames("subagent_tool_result")
    assert result["output"] == "ok\n"


def test_a_title_refinement_between_two_reasoning_chunks_ends_no_segment():
    """kiro-cli refines a call's title after announcing it; the refinement
    is about the call, not a new one, so it is no segment boundary either."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"key is {_AKIA_HEAD}"))
    p.feed(_activity(tool_call_id=TCID, title="Running: git log -1"))
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"{_AKIA_TAIL} ok\n"))

    wire = _wire(p, "subagent_thinking")
    assert _AKIA not in wire
    assert _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert [f["tool"] for f in p.frames("subagent_tool")] == ["shell", "Running: git log -1"]


def test_a_tool_result_between_two_text_chunks_ends_no_segment():
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))

    p.feed(_activity(text=f"token {_AKIA_HEAD}"))
    p.feed(_result(TCID, tool_output="ok\n", tool_final=True, tool_status="completed"))
    p.feed(_activity(text=f"{_AKIA_TAIL}.\n"))

    wire = _wire(p, "subagent_chunk")
    assert _AKIA not in wire
    assert _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert _AKIA not in _native_done_result(p.output[card])


def test_a_refused_calls_frames_end_no_segment(monkeypatch):
    """A call the bound refused holds no row, so each of its frames -- the
    first and every refinement -- looks like a first frame. None of them is
    the child moving on for the redactors' purposes: were a refused frame to
    flush them, the withheld head of a key split across the chunks either side
    of it would cross the wire bare. Only an ADMITTED new call ends a segment."""
    from kiro_crew.dashboard import native_subagent_cards

    monkeypatch.setattr(native_subagent_cards, "NATIVE_SUBAGENT_CALLS_MAX", 1)
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"key is {_AKIA_HEAD}"))
    p.feed(_activity(tool_call_id="refused", title="shell"))
    p.feed(_activity(tool_call_id="refused", title="Running: shell"))
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text=f"{_AKIA_TAIL} ok\n"))

    wire = _wire(p, "subagent_thinking")
    assert _AKIA not in wire
    assert _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert wire.startswith("key is ") and wire.endswith(" ok\n")


def test_a_new_call_still_ends_the_reasoning_segment():
    """The child's own output moving on -- here its NEXT call -- is still a
    segment end: the tail held back for the redactor is not lost to it."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(_result(TCID, tool_output="ok\n", tool_final=True, tool_status="completed"))

    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text="now: run tests"))
    assert _wire(p, "subagent_thinking") == "now: run "

    p.feed(_activity(tool_call_id="tc-2", title="shell"))

    assert _wire(p, "subagent_thinking") == "now: run tests"


def test_a_withheld_text_tail_is_flushed_into_the_feed_when_the_child_ends():
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(text="all done"))
    assert _wire(p, "subagent_chunk") == "all "

    done = p.done()

    assert _wire(p, "subagent_chunk") == "all done"
    assert done["id"] == card and done["result"].endswith("all done")


# ── call titles: bounded at insertion, unnamed in the feed past the bound ─────


def test_one_admission_bounds_every_store_keyed_on_a_calls_id():
    """The turn's attribution map is the admission set: past the bound a call
    holds no row in it, in its card's titles or in the printed-output record
    (a row in any sibling store would be the growth the bound refuses). The
    call still streams; the count stays where the bound left it and the
    refused frames are counted."""
    p = _Projection()
    p.spawn()
    for n in range(NATIVE_SUBAGENT_CALLS_MAX + 3):
        p.feed(_activity(tool_call_id=f"call-{n}", title=f"Running: step {n}"))
    p.feed(
        _result(f"call-{NATIVE_SUBAGENT_CALLS_MAX}", tool_output="late\n", tool_status="completed")
    )

    info = p.tracker[CHILD]
    assert len(p.tc_card) == NATIVE_SUBAGENT_CALLS_MAX
    assert len(info["call_titles"]) == NATIVE_SUBAGENT_CALLS_MAX
    assert len(p.result_seen) == 0 and f"call-{NATIVE_SUBAGENT_CALLS_MAX}" not in p.tc_card
    assert info["tool_count"] == NATIVE_SUBAGENT_CALLS_MAX
    assert info["calls_refused"] == 4  # three refused calls' first frames + the result
    last = p.frames("subagent_tool")[-1]
    assert last["tool"] == f"Running: step {NATIVE_SUBAGENT_CALLS_MAX + 2}"
    assert last["tool_count"] == NATIVE_SUBAGENT_CALLS_MAX
    (result,) = p.frames("subagent_tool_result")
    assert result["output"] == "late\n"


def test_an_over_long_call_id_is_never_recorded_but_its_call_still_streams():
    p = _Projection()
    card = p.spawn()
    long_id = "x" * (NATIVE_SUBAGENT_CALL_ID_CHARS + 1)

    p.feed(_activity(tool_call_id=long_id, title="Running: ls"))
    p.feed(_activity(tool_call_id=long_id, title="Running: ls -la"))

    info = p.tracker[CHILD]
    assert long_id not in info.get("call_titles", {}) and long_id not in p.tc_card
    # A refused call holds no row, so its refinement cannot be told from a new
    # call: each frame is counted as refused (as the roster counts its overflow)
    # and the admitted count does not move.
    assert info.get("tool_count", 0) == 0 and info["calls_refused"] == 2
    assert [f["tool"] for f in p.frames("subagent_tool")] == ["Running: ls", "Running: ls -la"]
    assert all(f["id"] == card for f in p.frames("subagent_tool"))


def test_a_refinement_of_a_kept_title_does_not_count_against_the_bound():
    p = _Projection()
    p.spawn()
    for n in range(NATIVE_SUBAGENT_CALLS_MAX):
        p.feed(_activity(tool_call_id=f"call-{n}", title="shell"))

    p.feed(_activity(tool_call_id="call-0", title="Running: echo refined"))

    info = p.tracker[CHILD]
    assert info["call_titles"]["call-0"] == "Running: echo refined"
    assert len(info["call_titles"]) == NATIVE_SUBAGENT_CALLS_MAX


def test_the_terminal_record_carries_no_call_titles_or_stream_state():
    """Only the bounded fields the reconnect snapshot reads survive the child:
    the title map and the redactors are per-run working state."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: echo probe-hello"))
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text="almost"))
    assert CHILD in p.streams

    p.done()

    info = p.tracker[CHILD]
    assert info["done"] and "call_titles" not in info
    assert CHILD not in p.streams
    assert info["last_tool"] == "Running: echo probe-hello"


@pytest.mark.asyncio
async def test_end_to_end_recorded_child_frames_reach_the_card():
    """The recorded kiro-cli 2.31 sequence, handle -> projection, lands four
    frame kinds on the card and flips it off "Starting…"."""
    handle = _handle()
    p = _Projection()
    card = p.spawn()
    frames = [
        _call_frame(),
        _content_result_frame(),
        _terminal_result_frame(),
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "done"}},
    ]
    for f in frames:
        for ev in handle._handle_update(_child(f)):
            p.feed(ev)

    tools = p.frames("subagent_tool")
    assert tools and all(t["id"] == card for t in tools)
    assert any("echo probe-hello" in t["tool"] for t in tools)
    results = p.frames("subagent_tool_result")
    assert any("probe-hello" in r.get("output", "") for r in results)
    assert any(r.get("status") == "completed" for r in results)
    # "done" has no terminator, so it is withheld until the child ends.
    assert p.frames("subagent_chunk") == []
    assert p.done()["result"].endswith("done")
    assert p.frames("subagent_chunk") == [{"id": card, "slot": "slot-1", "text": "done"}]


# ── turn-end close_all: the same flush the roster and staleness closes do ──


def test_turn_end_close_all_flushes_a_withheld_text_tail_into_the_result():
    """The parent turn ends (cancel, failure, missed terminal roster frame)
    while the child's last chunk is still held back by its redactor. The
    turn-end safety net must flush that tail like the other closes do, or the
    card and the reconnect result keep only the prefix."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(text="Final answer"))
    assert _wire(p, "subagent_chunk") == "Final "

    _native_subagent_close_all(
        p.state, p.slot, p.tracker, p.output, card_output_len=p.output_len, streams=p.streams
    )

    assert _wire(p, "subagent_chunk") == "Final answer"
    (done,) = p.frames("subagent_done")
    assert done["id"] == card and done["result"].endswith("Final answer")
    assert p.tracker[CHILD]["result"].endswith("Final answer")
    assert CHILD not in p.streams and "call_titles" not in p.tracker[CHILD]


def test_turn_end_close_all_flushes_a_withheld_reasoning_tail_too():
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(activity=SUBAGENT_ACTIVITY_THINKING, text="plan: run tests"))
    assert _wire(p, "subagent_thinking") == "plan: run "

    _native_subagent_close_all(
        p.state, p.slot, p.tracker, p.output, card_output_len=p.output_len, streams=p.streams
    )

    assert _wire(p, "subagent_thinking") == "plan: run tests"
    (done,) = p.frames("subagent_done")
    assert done["id"] == card


# ── the title bound shows in the feed, not on the wire ───────────────────────


def _overflow_titles(p: _Projection, extra: int = 3) -> None:
    for n in range(NATIVE_SUBAGENT_CALLS_MAX + extra):
        p.feed(_activity(tool_call_id=f"call-{n}", title=f"Running: step {n}"))


def test_an_output_whose_call_name_was_not_kept_is_filed_under_the_unnamed_line():
    """Past the title bound a call's output still reaches the finished card's
    result; the feed heads it with the unnamed line instead of its name, so the
    reader sees a name is missing rather than an output filed under nothing.
    The wire carries no count of it: the live row is titled by its own frame."""
    p = _Projection()
    card = p.spawn()
    _overflow_titles(p, extra=1)
    last = f"call-{NATIVE_SUBAGENT_CALLS_MAX}"

    p.feed(_result("call-0", tool_output="first\n", tool_status="completed"))
    p.feed(_result(last, tool_output="late\n", tool_status="completed"))

    assert _native_done_result(p.output[card]) == (
        "\u2192 step 0\nfirst\n\n" f"{NATIVE_SUBAGENT_UNNAMED_FEED_LINE}late\n\n"
    )
    assert all("call_titles_dropped" not in f for f in p.frames("subagent_tool"))
    done = p.done()
    assert "call_titles_dropped" not in done
    # The bound is named once, on the result's last line.
    assert done["result"].endswith(
        f"{NATIVE_SUBAGENT_UNNAMED_FEED_LINE}late\n\n{NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE}"
    )
    # The transient title map is dropped at done and nothing replaces it.
    assert "call_titles" not in p.tracker[CHILD]


def test_a_kept_call_announced_untitled_is_filed_under_its_output_alone():
    """A slot the bound kept but no frame ever titled is not an unnamed call: the
    output is filed as before, with no heading line at all."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title=""))

    p.feed(_result(TCID, tool_output="plain\n", tool_status="completed"))

    assert _native_done_result(p.output[card]) == "plain\n\n"


def test_a_calls_output_prints_from_its_terminal_frame_once():
    """kiro-cli sends a call's output as a content frame while the call is in
    progress and again as the terminal frame's rawOutput. The output prints
    ONCE, from the terminal frame: a frame in progress carries its status
    alone (its text is kept as the call's pending fragment, below), and a
    repeated terminal frame prints nothing again -- deduplication is by
    content, so a terminal frame that differs from the last printed output
    (a summary after a progress line) still prints."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: make"))

    p.feed(_result(TCID, tool_output="collecting\n", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output="12 passed\n", tool_status="completed", tool_final=True))
    p.feed(_result(TCID, tool_output="12 passed\n", tool_status="completed", tool_final=True))

    frames = p.frames("subagent_tool_result")
    assert [f.get("status") for f in frames] == ["in_progress", "completed", "completed"]
    assert [f.get("output") for f in frames] == [None, "12 passed\n", None]
    assert _native_done_result(p.output[card]) == "\u2192 make\n12 passed\n\n"


def test_a_results_fragments_never_cross_the_wire_on_their_own():
    """A credential split across two content frames of one call would cross
    the wire in halves if each fragment were broadcast after being redacted on
    its own: neither half matches a credential alone. No fragment is broadcast;
    the terminal frame carries the whole output, redacted whole."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))

    p.feed(_result(TCID, tool_output=f"key {_AKIA_HEAD}", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output=f"{_AKIA_TAIL} ok\n", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output=f"key {_AKIA} ok\n", tool_status="completed", tool_final=True))

    outputs = [f.get("output") for f in p.frames("subagent_tool_result")]
    assert outputs[:2] == [None, None]
    wire = "".join(o for o in outputs if o) + _native_done_result(p.output[card])
    assert _AKIA not in wire and _AKIA_HEAD not in wire and _AKIA_TAIL not in wire
    assert outputs[2].startswith("key ") and outputs[2].endswith(" ok\n")


def test_a_terminal_frame_without_output_prints_the_calls_fragments_in_order():
    """A call whose terminal frame carries no text still had output: its
    fragments print then, in order and through one rolling redactor, under the
    call's title."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: make"))

    p.feed(_result(TCID, tool_output="collecting\n", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output="3 passed\n", tool_status="in_progress"))
    p.feed(_result(TCID, tool_status="completed", tool_final=True))

    outputs = [f.get("output") for f in p.frames("subagent_tool_result")]
    assert outputs == [None, None, "collecting\n3 passed\n"]
    assert _native_done_result(p.output[card]) == "\u2192 make\ncollecting\n3 passed\n\n"
    assert "call_pending" not in p.tracker[CHILD] or not p.tracker[CHILD]["call_pending"]


_BEARER = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"


def test_the_fallback_redacts_a_credential_split_across_fragments():
    """The fragments a call's fallback prints pass through ONE rolling redactor
    in order, so a bearer header whose prefix and token arrived in separate
    frames is redacted whole when a status-only terminal frame publishes them.
    Keeping only the last fragment would publish the bare token: on its own it
    is not recognisable as a credential."""
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: curl"))

    p.feed(_result(TCID, tool_output="Authorization: Bearer ", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output=_BEARER, tool_status="in_progress"))
    p.feed(_result(TCID, tool_output="\nok\n", tool_status="in_progress"))
    p.feed(_result(TCID, tool_status="completed", tool_final=True))

    outputs = [f.get("output") for f in p.frames("subagent_tool_result")]
    assert outputs[:3] == [None, None, None]
    assert _BEARER not in outputs[3] and _BEARER not in _native_done_result(p.output[card])
    assert outputs[3] == "[REDACTED: credential]\nok\n"


def test_a_childs_close_redacts_a_credential_split_across_pending_fragments():
    """The same rolling redaction covers the close: a child that ends mid-call
    with a split bearer header pending publishes it redacted whole."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: curl"))
    p.feed(_result(TCID, tool_output="Authorization: Bearer ", tool_status="in_progress"))
    p.feed(_result(TCID, tool_output=_BEARER, tool_status="in_progress"))

    done = p.done()

    outputs = [f.get("output") for f in p.frames("subagent_tool_result")]
    assert _BEARER not in "".join(o for o in outputs if o) and _BEARER not in done["result"]
    assert done["result"] == "\u2192 curl\n[REDACTED: credential]\n"


def test_a_childs_close_flushes_the_fragments_of_calls_that_never_ended():
    """A child that ends mid-call leaves a pending fragment: the close prints
    it, so the finished card and its result carry what the call had produced,
    and the terminal record keeps no pending text."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="Running: make"))
    p.feed(_result(TCID, tool_output="collecting\n", tool_status="in_progress"))

    done = p.done()

    assert [f.get("output") for f in p.frames("subagent_tool_result")] == [None, "collecting\n"]
    assert done["result"] == "\u2192 make\ncollecting\n\n"
    assert "call_pending" not in p.tracker[CHILD]


def test_a_refused_calls_fragment_is_not_kept_and_its_terminal_output_prints_whole(monkeypatch):
    """A call the bound refused holds no row, so nothing is kept for it: a
    fragment of its output carries the status alone and is gone, and its
    terminal frame -- the whole output -- prints under the unnamed line."""
    from kiro_crew.dashboard import native_subagent_cards

    monkeypatch.setattr(native_subagent_cards, "NATIVE_SUBAGENT_CALLS_MAX", 1)
    p = _Projection()
    card = p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(_result(TCID, tool_output="ok\n", tool_status="completed", tool_final=True))

    p.feed(_result("refused", tool_output="part", tool_status="in_progress"))
    p.feed(_result("refused", tool_output="whole\n", tool_status="completed", tool_final=True))

    outputs = [f.get("output") for f in p.frames("subagent_tool_result")]
    assert outputs == ["ok\n", None, "whole\n"]
    assert "refused" not in (p.tracker[CHILD].get("call_pending") or {})
    assert _native_done_result(p.output[card]) == (
        "\u2192 shell\nok\n\n" f"{NATIVE_SUBAGENT_UNNAMED_FEED_LINE}whole\n\n"
    )


def test_a_run_under_the_bound_counts_no_refusal():
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(_result(TCID, tool_output="ok\n", tool_status="completed"))

    done = p.done()

    assert done["result"] == "\u2192 shell\nok\n\n"
    assert "calls_refused" not in p.tracker[CHILD]


def _snapshot_state(p: _Projection):
    """A DashboardState whose one slot holds this projection's native cards."""
    from kiro_crew.dashboard.state import DashboardState

    slot = MagicMock()
    slot.key = p.slot.key
    slot._native_subagent_tracker = p.tracker
    slot._native_subagent_output = p.output
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    return state


def test_an_admitted_call_counts_once_however_many_frames_it_sends():
    """kiro-cli announces each call twice (bare tool name, then the command
    title). An admitted call counts once, not once per frame; a refused call
    cannot be recognised again and is counted per frame as refused."""
    p = _Projection()
    p.spawn()
    extra = 3
    for n in range(NATIVE_SUBAGENT_CALLS_MAX + extra):
        p.feed(_activity(tool_call_id=f"call-{n}", title="shell"))
        p.feed(_activity(tool_call_id=f"call-{n}", title=f"Running: step {n}"))

    info = p.tracker[CHILD]
    assert info["tool_count"] == NATIVE_SUBAGENT_CALLS_MAX
    assert info["calls_refused"] == 2 * extra
    assert len(info["call_titles"]) == NATIVE_SUBAGENT_CALLS_MAX
    # An admitted call's slot is refined by its second frame.
    assert info["call_titles"]["call-0"] == "Running: step 0"


def test_an_admitted_call_announced_without_a_title_holds_its_slot():
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title=""))
    p.feed(_activity(tool_call_id=TCID, title="Running: ls"))

    info = p.tracker[CHILD]
    assert info["call_titles"] == {TCID: "Running: ls"}


def test_turn_end_close_all_sheds_a_stopped_records_transients_without_a_frame():
    """The dashboard's stop path marks the record done itself; the turn-end
    close must still drop its title map and stream pair, silently."""
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(_activity(text="half"))
    p.tracker[CHILD]["done"] = True
    p.state.broadcast_ws.reset_mock()

    _native_subagent_close_all(
        p.state, p.slot, p.tracker, p.output, card_output_len=p.output_len, streams=p.streams
    )

    assert "call_titles" not in p.tracker[CHILD]
    assert CHILD not in p.streams
    p.state.broadcast_ws.assert_not_called()


def test_a_running_snapshot_says_once_that_the_bound_was_crossed(monkeypatch):
    """A reconnect while the run is going must not read like a run that never
    named those calls: the running snapshot's ``streaming`` ends with the
    overflow line, once, as the finished card's ``result`` does. A run under
    the bound says nothing."""
    from kiro_crew.dashboard import native_subagent_cards
    from kiro_crew.dashboard.state import DashboardState

    monkeypatch.setattr(native_subagent_cards, "NATIVE_SUBAGENT_CALLS_MAX", 1)
    p = _Projection()
    p.spawn()
    p.feed(_activity(tool_call_id=TCID, title="shell"))
    p.feed(_result(TCID, tool_output="ok\n", tool_status="completed"))
    (quiet,) = DashboardState.native_subagent_snapshots(_snapshot_state(p))
    assert NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE not in quiet["streaming"]

    p.feed(_activity(tool_call_id="refused-1", title="shell"))
    p.feed(_activity(tool_call_id="refused-2", title="shell"))
    (running,) = DashboardState.native_subagent_snapshots(_snapshot_state(p))
    done = p.done()

    assert running["streaming"].count(NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE) == 1
    assert running["streaming"].endswith(NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE)
    assert done["result"].count(NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE) == 1
    assert done["result"].endswith(NATIVE_SUBAGENT_CALLS_OVERFLOW_FEED_LINE)


def test_snapshots_carry_no_per_run_title_state():
    """The reconnect snapshot reads only the bounded card fields; the title
    map is per-run working state and nothing about it is published."""
    from kiro_crew.dashboard.state import DashboardState

    p = _Projection()
    p.spawn()
    _overflow_titles(p, extra=1)

    (running,) = DashboardState.native_subagent_snapshots(_snapshot_state(p))

    assert "call_titles" not in running and "call_titles_dropped" not in running
