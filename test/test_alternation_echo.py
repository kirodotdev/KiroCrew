"""The backend's alternation placeholder never reaches the transcript.

A turn that ends on a tool result leaves the backend's history with no assistant
message after it, and the backend fills that gap with a placeholder the model then
repeats at the start of a later turn. Those words are not a reply: the live stream
drops them. Rows an older build already stored are left as they are.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.dashboard.chat_utils import EMPTY_TURN_NOTICE_KIND

pytestmark = pytest.mark.xdist_group("alternation_echo")

PLACEHOLDER = (
    "(No assistant response was recorded for the preceding turn. This "
    "placeholder was inserted automatically to preserve message "
    "alternation \u2014 it is not a reply the assistant produced.)"
)


class TestPredicate:
    # Imported per test so the runner tests below still collect without the module.
    @pytest.mark.parametrize(
        "text",
        [PLACEHOLDER, "(No assistant response was recorded.)", "\n" + PLACEHOLDER + "\n\n"],
        ids=["long", "short", "padded"],
    )
    def test_the_placeholder_in_each_spelling_is_an_echo(self, text):
        from kiro_crew.messaging.alternation_echo import could_be_echo, is_echo

        assert is_echo(text)
        assert could_be_echo(text)

    @pytest.mark.parametrize("cut", [0, 1, 5, 20, 60, len(PLACEHOLDER) - 1])
    def test_every_prefix_could_still_be_the_echo(self, cut):
        from kiro_crew.messaging.alternation_echo import could_be_echo

        assert could_be_echo(PLACEHOLDER[:cut])

    @pytest.mark.parametrize(
        "text",
        [
            "(Note) the build passed.",
            "No assistant response was recorded",
            PLACEHOLDER + " Done.",
            "Hello",
        ],
        ids=["parenthesis", "no-parenthesis", "words-after", "plain"],
    )
    def test_the_model_s_own_words_are_not_an_echo(self, text):
        from kiro_crew.messaging.alternation_echo import could_be_echo, is_echo

        assert not is_echo(text)
        assert not could_be_echo(text)


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


async def _drive_turn(state, slot, events, monkeypatch):
    """Stream *events* through the real ``_run_chat``; return the turns it queued."""
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    queue_calls: list = []
    queue_insert = type(slot).queue_insert

    def _record_queue(self_slot, *args, **kwargs):
        queue_calls.append((args, kwargs))
        return queue_insert(self_slot, *args, **kwargs)

    monkeypatch.setattr(type(slot), "queue_insert", _record_queue)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    slot.messages.append({"role": "nudge", "content": "patrol", "ts": "t0", "cls": ""})
    try:
        await chat_runner._run_chat(
            state, slot, "patrol", _directive_user_origin=False, _directive_self_wake=True
        )
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return queue_calls


def _text(text: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


def _tool(tcid: str = "tc-1") -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=tcid, title="Read a file", tool_name="read"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output="ok", tool_final=True),
    ]


_END = AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


def _streamed(state) -> str:
    return "".join(
        c.args[1]["content"]
        for c in state.broadcast_ws.call_args_list
        if c.args and c.args[0] == "chat_chunk"
    )


def _assistant(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "assistant"]


class TestLiveStream:
    @pytest.mark.asyncio
    async def test_a_turn_that_opens_with_the_placeholder_shows_only_its_work(
        self, tmp_path, monkeypatch
    ):
        """The field case: the turn after a quiet end opens with the placeholder,
        token by token, then calls a tool. No chunk, no assistant row."""
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("echo-then-tool")
        slot._titled = True
        events = [_text(PLACEHOLDER[:7]), _text(PLACEHOLDER[7:50]), _text(PLACEHOLDER[50:])]
        events += _tool() + [_text("All done."), _END]
        await _drive_turn(state, slot, events, monkeypatch)
        assert _assistant(slot) == ["All done."], slot.messages
        assert _streamed(state) == "All done."
        assert any(m.get("role") == "tool" for m in slot.messages)

    @pytest.mark.asyncio
    async def test_a_turn_that_is_only_the_placeholder_is_handled_as_an_empty_reply(
        self, tmp_path, monkeypatch
    ):
        """A turn whose whole reply is the placeholder shows no bubble and streams
        nothing, and takes the same empty-reply path as a turn with no text."""

        async def _run(key, events):
            state = _stub_state(tmp_path / key)
            slot = state.get_or_create_slot(key)
            slot._titled = True
            before = len(slot.messages)
            queue_calls = await _drive_turn(state, slot, events, monkeypatch)
            added = slot.messages[before:]
            assert [m for m in added if m.get("role") in ("assistant", "chunk")] == []
            assert _streamed(state) == ""
            notices = [
                m["content"]
                for m in added
                if (m.get("meta") or {}).get("kind") == EMPTY_TURN_NOTICE_KIND
            ]
            return [args for args, _ in queue_calls], notices

        plain = await _run("no-text", [_END])
        echoed = await _run("echo-only", [_text(PLACEHOLDER[:30]), _text(PLACEHOLDER[30:]), _END])
        assert plain[0] or plain[1], plain
        assert echoed == plain

    @pytest.mark.asyncio
    async def test_words_after_the_placeholder_in_one_segment_are_kept(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("echo-then-words")
        slot._titled = True
        events = [_text(PLACEHOLDER), _text("\n\nHere is the answer."), _END]
        await _drive_turn(state, slot, events, monkeypatch)
        assert _assistant(slot) == ["Here is the answer."]
        assert _streamed(state) == "Here is the answer."

    @pytest.mark.asyncio
    async def test_a_reply_that_opens_with_a_parenthesis_is_kept_whole(self, tmp_path, monkeypatch):
        state = _stub_state(tmp_path)
        slot = state.get_or_create_slot("paren-reply")
        slot._titled = True
        events = [_text("(N"), _text("ote) the build passed."), _END]
        await _drive_turn(state, slot, events, monkeypatch)
        assert _assistant(slot) == ["(Note) the build passed."]
        assert _streamed(state) == "(Note) the build passed."

    @pytest.mark.asyncio
    async def test_a_tool_turn_with_a_dropped_placeholder_keeps_its_recovery(
        self, tmp_path, monkeypatch
    ):
        """A turn that opens with the placeholder, runs a tool and ends with no
        closing text gets the same empty-reply recovery as the same turn without
        the placeholder: the dropped placeholder is not an answer."""

        async def _run(key, events):
            state = _stub_state(tmp_path / key)
            slot = state.get_or_create_slot(key)
            slot._titled = True
            before = len(slot.messages)
            queue_calls = await _drive_turn(state, slot, events, monkeypatch)
            notices = [
                m["content"]
                for m in slot.messages[before:]
                if (m.get("meta") or {}).get("kind") == EMPTY_TURN_NOTICE_KIND
            ]
            return [args for args, _ in queue_calls], notices

        plain = await _run("tool-only", _tool() + [_END])
        echoed = await _run("echo-tool-only", [_text(PLACEHOLDER)] + _tool() + [_END])
        assert plain[0] and plain[1], plain
        assert echoed == plain


class _ScriptedProvider:
    """Yields a fixed event script, like a backend mid-turn."""

    def __init__(self, events: list[AcpEvent]) -> None:
        self._events = events

    async def stream(self, message: str):
        for event in self._events:
            yield event


class TestCollectedTurns:
    """The turns that collect text outside the dashboard runner and the channel
    driver: ``stream_and_collect`` (cron, heartbeat, a thread wake, a reply thread)
    and the native Slack turn."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("events", "expected"),
        [
            (
                [_text(PLACEHOLDER[:30]), _text(PLACEHOLDER[30:]), _text("\n\nHere it is."), _END],
                "Here it is.",
            ),
            (
                [
                    _text(PLACEHOLDER[:30]),
                    _text(PLACEHOLDER[30:]),
                    *_tool(),
                    _text("Here it is."),
                    _END,
                ],
                "Here it is.",
            ),
            ([_text(PLACEHOLDER), _END], ""),
            (
                [_text("(No assistant"), _text(" was needed here.) Done."), _END],
                "(No assistant was needed here.) Done.",
            ),
            ([_text("(No assistant"), *_tool(), _END], "(No assistant"),
            ([_text("(No assistant"), _END], "(No assistant"),
        ],
        ids=[
            "words-after",
            "then-tool",
            "only",
            "opens-like-it",
            "prefix-held-to-a-tool-call",
            "prefix-held-to-the-end",
        ],
    )
    async def test_a_collected_turn_returns_and_streams_only_its_own_words(self, events, expected):
        from kiro_crew.llm_helpers import stream_and_collect

        chunks: list[str] = []
        text = await stream_and_collect(
            _ScriptedProvider(events),  # type: ignore[arg-type]
            "q",
            retry_transient=False,
            on_chunk=chunks.append,
        )
        assert text == expected
        assert "".join(chunks) == expected

    @pytest.mark.parametrize("streaming", [True, False], ids=["stream", "post"])
    @pytest.mark.parametrize(
        ("events", "kept"),
        [
            (
                [_text(PLACEHOLDER[:30]), _text(PLACEHOLDER[30:]), _text("\n\nHere it is.")],
                "Here it is.",
            ),
            (
                [_text(PLACEHOLDER[:30]), _text(PLACEHOLDER[30:]), *_tool(), _text("Here it is.")],
                "Here it is.",
            ),
            ([_text("(No assistant"), *_tool()], "(No assistant"),
            ([_text("(No assistant")], "(No assistant"),
        ],
        ids=["words-after", "then-tool", "prefix-held-to-a-tool-call", "prefix-held-to-the-end"],
    )
    def test_a_native_slack_turn_posts_only_its_own_words(self, events, kept, streaming):
        from slack_handler_helpers import FakeProvider, FakeSessionManager

        from conftest import MockSlackClient
        from kiro_crew.slack.handler import handle_message

        slack = MockSlackClient()
        slack._stream_enabled = streaming
        asyncio.run(
            handle_message(
                slack, FakeSessionManager(FakeProvider(events)), "C1", "q?", None, "m1", "U1"
            )
        )
        sent = repr(slack.actions)
        assert "No assistant response" not in sent
        assert kept in sent
