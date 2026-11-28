"""A channel task card takes the refined title a ``tool_call_update`` sends.

claude-agent-acp names a call in its first ``tool_call`` before the call's input has
streamed ("Read", "Terminal") and sends the real title in a ``tool_call_update``. The
dashboard row and the Subagents card take it; ``TurnDriver`` sends it to the channel
renderer as one ``TOOL_TITLE`` event for the call it announced, only on a backend whose
capabilities say ``refines_tool_call_titles`` (``ACP_BACKENDS_TOOL_TITLE_REFINEMENT``),
and the transport renderer renames that call's open card in place.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from test_slack_renderer import _RecSlack

from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_TOOL_TITLE_REFINEMENT,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.messaging import TransportCapabilities, TurnDriver
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.slack.renderer import SlackRenderer

COMPLETE = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


class _Recorder(Renderer):
    def __init__(self):
        super().__init__(TransportCapabilities())
        self.events: list[tuple] = []

    async def on_text_chunk(self, text):
        self.events.append(("text_chunk", text))

    async def on_thinking(self, text):
        self.events.append(("thinking", text))

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        self.events.append(("tool_call", tool_call_id, title))

    async def on_tool_title(self, tool_call_id, title, raw_title=""):
        self.events.append(("tool_title", tool_call_id, title, raw_title))

    async def on_tool_result(self, tool_call_id, tool_status):
        self.events.append(("tool_result", tool_call_id, tool_status))

    async def on_prompt_choice(self, options, request_id, tool_title="", tool_purpose="", **kw):
        self.events.append(("prompt_choice", request_id))

    async def on_compaction(self, pct):
        self.events.append(("compaction", pct))

    async def on_done(self, stop_reason=""):
        self.events.append(("done", stop_reason))


class _Provider:
    def __init__(self, backend, events):
        self.capabilities = capabilities_for(backend)
        self._events = events

    async def stream(self, message):
        for ev in self._events:
            yield ev


def _drive(backend, events, renderer=None):
    renderer = renderer or _Recorder()
    driver = TurnDriver(_Provider(backend, events), renderer, approval_mode="auto")
    asyncio.run(driver.run("go"))
    return renderer


def _claude_read(call_id="t1"):
    """The reproduction's stream, through the real ``session/update`` parser.

    Shaped as ``acp_frames/claude/read-image.jsonl`` is: the updates name no ``kind``,
    and the refinement comes twice, as an Edit's does in the gated capture.
    """
    caches = {"tool_input_cache": {}, "shell_cache": {}, "raw_params_cache": {}}
    refinement = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": call_id,
        "title": "Read /proj/src/app.py",
        "rawInput": {"file_path": "/proj/src/app.py"},
    }
    frames = [
        {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": "Read",
            "kind": "read",
            "rawInput": {},
            "status": "pending",
        },
        refinement,
        refinement,
        {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "completed"},
    ]
    return [ev for frame in frames for ev in parse_session_update(frame, **caches)]


def _call(call_id="t1", title="Terminal", **kw):
    return AcpEvent(
        kind=EVENT_TOOL_CALL, tool_call_id=call_id, title=title, tool_kind="execute", **kw
    )


def _update(call_id="t1", command="ls -la", title="", **kw):
    return AcpEvent(
        kind=EVENT_TOOL_CALL_UPDATE,
        tool_call_id=call_id,
        title=title or command,
        tool_kind="execute",
        is_shell=True,
        tool_input=json.dumps({"command": command}),
        **kw,
    )


def _result(call_id="t1"):
    return AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=call_id, tool_status="completed")


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN), ids=lambda b: b or "kiro")
def test_a_refining_backend_renames_the_call_before_its_result(backend):
    events = _drive(backend, _claude_read() + [COMPLETE]).events
    refined = [("tool_title", "t1", "Read src/app.py", "")]
    expected = [("tool_call", "t1", "Read")]
    expected += refined if backend in ACP_BACKENDS_TOOL_TITLE_REFINEMENT else []
    assert events == expected + [("tool_result", "t1", "completed"), ("done", "end_turn")]


@pytest.mark.parametrize(
    "stream, shown",
    [
        pytest.param([_call(), _result()], "Terminal", id="no-update"),
        pytest.param([_call(title="List files"), _update(), _result()], "List files", id="same"),
        pytest.param([_call(), _update(call_id="t9"), _result()], "Terminal", id="unannounced"),
    ],
)
def test_an_update_that_renames_nothing_sends_nothing(stream, shown):
    events = _drive(ACP_BACKEND_CLAUDE, stream + [COMPLETE]).events
    assert events == [
        ("tool_call", "t1", shown),
        ("tool_result", "t1", "completed"),
        ("done", "end_turn"),
    ]


def test_the_title_comes_from_the_redacted_input_never_the_raw_params():
    update = _update(
        command="rm -rf build", title="Terminal", raw_tool_params={"command": "ls -la"}
    )
    events = _drive(ACP_BACKEND_CLAUDE, [_call(), update, _result(), COMPLETE]).events
    assert ("tool_title", "t1", "rm -rf build", "rm -rf build") in events


class _TaskRec(_RecSlack):
    async def append_task(self, channel, ts, task_id, title, status, details="", output=""):
        self.calls.append(("task", {"id": task_id, "title": title, "status": status}))
        return True


def _cards(rec):
    return [(kw["id"], kw["title"], kw["status"]) for name, kw in rec.calls if name == "task"]


def test_the_open_card_takes_the_refined_title_in_place():
    rec = _TaskRec()
    stream = _claude_read() + [AcpEvent(kind=EVENT_TEXT_CHUNK, text="It reads fine."), COMPLETE]
    _drive(ACP_BACKEND_CLAUDE, stream, SlackRenderer(rec, "C1", "t1", reactions_enabled=False))
    cards = _cards(rec)
    assert {card_id for card_id, _, _ in cards} == {"tool_1"}
    assert [title for _, title, _ in cards[:2]] == ["Read", "Read src/app.py"]
    assert cards[-1][1].startswith("Read src/app.py") and cards[-1][2] == "complete"


@pytest.mark.parametrize(
    "stream, titles",
    [
        pytest.param(
            [_call(tool_purpose="Check the build"), _update()],
            {"tool_1": {"Check the build"}},
            id="purpose-titled",
        ),
        pytest.param(
            [_call(), _call(call_id="t2", title="Read"), _update()],
            {"tool_1": {"Terminal"}, "tool_2": {"Read"}},
            id="card-closed",
        ),
    ],
)
def test_a_card_that_does_not_show_the_stub_keeps_its_title(stream, titles):
    rec = _TaskRec()
    renderer = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
    _drive(ACP_BACKEND_CLAUDE, stream + [COMPLETE], renderer)
    shown: dict[str, set[str]] = {}
    for card_id, title, _ in _cards(rec):
        shown.setdefault(card_id, set()).add(title.split("  ⏱")[0])
    assert shown == titles
