"""The last run of a streamed Slack answer is settled when its message ends.

The live stream holds back the trailing run of each chunk until more text shows
where it ends. These tests drive the native handler and pin what happens to that
held run when the turn ends: clean text is sent, so the last word is not lost;
text that redacts differently as a whole is dropped and replaced by the final
copy. A run held before a tool call stays held until the text after it shows
where the run ends. After a ``wait``, once the joined text is cleared, the run
goes back to the end of the message it ended, in that message's own blocks.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest

from conftest import MockSlackClient
from kiro_crew.acp.client import AcpProcessDied
from kiro_crew.acp.types import EVENT_TOOL_CALL
from kiro_crew.providers.base import LLMEvent
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.handler import handle_message

_handler_tests = importlib.import_module("test_slack_handler")
FakeSessionManager = _handler_tests.FakeSessionManager
FakeProvider = _handler_tests.FakeProvider


class _StreamingSlack(MockSlackClient):
    def __init__(self) -> None:
        super().__init__()
        self._stream_enabled = True


class _TrackSessions(FakeSessionManager):
    def __init__(self, provider) -> None:
        super().__init__(provider)
        self.calls = {"success": 0, "failure": 0}

    def record_success(self, key) -> None:
        self.calls["success"] += 1

    async def record_failure(self, key):
        self.calls["failure"] += 1
        return False


def _run(events: list[LLMEvent]) -> tuple[_StreamingSlack, _TrackSessions]:
    slack = _StreamingSlack()
    sessions = _TrackSessions(FakeProvider(events))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    return slack, sessions


def _appends(slack: _StreamingSlack) -> list[tuple[str, str]]:
    return [
        (payload["ts"], payload["text"])
        for name, payload in slack.actions
        if name == "append_stream"
    ]


def _text(kind_text: str) -> LLMEvent:
    return LLMEvent(kind="text_chunk", text=kind_text)


def test_the_last_word_of_a_clean_answer_reaches_the_stream():
    slack, sessions = _run([_text("The answer is "), _text("forty2")])

    assert "".join(text for _ts, text in _appends(slack)) == "The answer is forty2"
    assert sessions.calls == {"success": 1, "failure": 0}


def test_a_one_word_answer_is_delivered_and_counted():
    slack, sessions = _run([_text("done")])

    assert [text for _ts, text in _appends(slack)] == ["done"]
    assert sessions.calls == {"success": 1, "failure": 0}


_OPAQUE = "q7Rk2LmZ9xWv4TbN8sYc1HdJ6pFe3GaU"


@pytest.mark.parametrize(
    "boundary",
    [
        LLMEvent(kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"),
        LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait"),
    ],
    ids=["tool-card", "wait"],
)
def test_a_value_split_around_a_tool_call_is_filtered_as_one(boundary):
    slack, _sessions = _run(
        [_text("Header: Authorization: Bearer "), boundary, _text(f"{_OPAQUE} then more")]
    )

    streamed = "".join(text for _ts, text in _appends(slack))
    assert _OPAQUE not in streamed
    assert "[REDACTED: credential]" in streamed


def test_a_held_run_is_replaced_by_the_final_copy_when_the_whole_text_redacts():
    slack, _sessions = _run([_text("The access key is AKIA"), _text("IOSFODNN7"), _text("EXAMPLE")])

    streamed = "".join(text for _ts, text in _appends(slack))
    assert "IOSFODNN7EXAMPLE" not in streamed
    final = [
        payload.get("text", "") for name, payload in slack.actions if name in ("update", "post")
    ]
    assert any("[REDACTED: credential]" in text for text in final)


def _wait_call() -> LLMEvent:
    return LLMEvent(kind=EVENT_TOOL_CALL, title="wait", tool_name="wait", tool_call_id="t-wait")


def _every_text(slack: _StreamingSlack) -> str:
    return "\n".join(str(payload.get("text") or "") for _name, payload in slack.actions)


def test_a_value_split_by_a_wait_that_ends_the_turn_stays_filtered_in_the_final_copy():
    slack, _sessions = _run([_text("Header: Authorization: Bearer "), _wait_call(), _text(_OPAQUE)])

    assert _OPAQUE not in _every_text(slack)


def test_the_last_word_before_a_wait_that_ends_the_turn_is_posted():
    slack, sessions = _run([_text("first part 42"), _wait_call()])

    assert "42" in _every_text(slack)
    assert sessions.calls == {"success": 1, "failure": 0}


def _messages(slack: _StreamingSlack) -> list[str]:
    by_message: dict[str, str] = {}
    for ts, text in _appends(slack):
        by_message[ts] = by_message.get(ts, "") + text
    return list(by_message.values())


class _StoredSlack(_StreamingSlack):
    """Keeps each streamed message and hands back its blocks, as Slack does."""

    def __init__(self, shape: str = "markdown", *, fail_update: bool = False) -> None:
        super().__init__()
        self.shape = shape
        self.fail_update = fail_update
        self.text_by_ts: dict[str, str] = {}

    async def append_stream(self, channel, ts, text):
        self.text_by_ts[ts] = self.text_by_ts.get(ts, "") + text
        return await super().append_stream(channel, ts, text)

    def blocks(self, text: str) -> list[dict]:
        plan = {"type": "plan", "title": "Steps", "tasks": [{"task_id": "tool_1"}]}
        if self.shape == "markdown":
            return [{"type": "markdown", "text": text}, plan]
        if self.shape == "section":
            return [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        head, _, rest = text.partition(" ")
        return [
            {
                "type": "rich_text",
                "elements": [
                    {
                        "type": "rich_text_section",
                        "elements": [
                            {"type": "text", "text": head, "style": {"bold": True}},
                            {"type": "text", "text": " " + rest},
                        ],
                    }
                ],
            },
            plan,
        ]

    async def fetch_thread_replies(
        self, channel, thread_ts, limit=200, warn_on_pagination=True, *, oldest=None, latest=None
    ):
        return [
            {"ts": ts, "text": text, "blocks": self.blocks(text)}
            for ts, text in self.text_by_ts.items()
            if oldest is None or float(ts) > float(oldest)
        ]

    async def update_message(self, channel, ts, text="", blocks=None):
        if self.fail_update:
            raise RuntimeError("refused")
        self.actions.append(
            ("update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        )


def _run_on(slack: _StreamingSlack, events: list[LLMEvent]) -> _TrackSessions:
    sessions = _TrackSessions(FakeProvider(events))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))
    return sessions


def _updates(slack: _StreamingSlack) -> list[dict]:
    return [payload for name, payload in slack.actions if name == "update"]


def _seen(slack: _StreamingSlack) -> str:
    """Everything the fake was sent, blocks included."""
    return "\n".join(json.dumps(payload, sort_keys=True) for _name, payload in slack.actions)


@pytest.mark.parametrize("shape", ["markdown", "rich_text"])
def test_the_word_held_at_a_wait_goes_back_to_its_message_with_its_format(shape):
    slack = _StoredSlack(shape)
    sessions = _run_on(slack, [_text("Checking now 42"), _wait_call(), _text("Results are in.")])

    first, second = list(slack.text_by_ts)
    assert _messages(slack) == ["Checking now ", "Results are in."]
    [edit] = _updates(slack)
    assert edit["ts"] == first
    assert edit["text"] == "Checking now 42"
    assert edit["blocks"] == slack.blocks("Checking now 42")
    stops = [payload for name, payload in slack.actions if name == "stop_stream"]
    assert stops[-1]["ts"] == second
    assert stops[-1]["text"] == "Results are in."
    assert sessions.calls == {"success": 1, "failure": 0}


@pytest.mark.parametrize(
    ("make", "saved"),
    [(_StoredSlack, "Results are in."), (_StreamingSlack, "42Results are in.")],
    ids=["given-back", "fallback"],
)
def test_the_saved_answer_matches_the_message_the_reader_sees(monkeypatch, make, saved):
    seen: list[str] = []

    async def _capture(_slack, _channel, _reply_ts, _user, _key, accumulated, *_rest):
        seen.append(accumulated)

    monkeypatch.setattr(slack_handler, "_reply_by_voice", _capture)
    slack = make()
    _run_on(slack, [_text("Checking now 42"), _wait_call(), _text("Results are in.")])

    assert seen == [saved]
    assert _messages(slack)[-1] == saved


@pytest.mark.parametrize(
    "make",
    [
        lambda: _StoredSlack(fail_update=True),
        lambda: _StoredSlack("section"),
        _StreamingSlack,
    ],
    ids=["edit-refused", "blocks-not-editable", "no-read-back"],
)
def test_the_word_held_at_a_wait_opens_the_next_message_when_the_edit_cannot_land(make):
    slack = make()
    sessions = _run_on(slack, [_text("Checking now 42"), _wait_call(), _text("Results are in.")])

    assert _messages(slack) == ["Checking now ", "42Results are in."]
    assert _updates(slack) == []
    assert sessions.calls == {"success": 1, "failure": 0}


@pytest.mark.parametrize(
    ("live", "given_back", "opening"),
    [(True, ["Checking now 42"], "42 tasks completed."), (False, [], "4242 tasks completed.")],
    ids=["sent-live", "sent-with-redacted-text"],
)
def test_the_next_message_keeps_its_own_opening_word_in_its_final_copy(
    monkeypatch, live, given_back, opening
):
    # Sent live, the run clears on its own and goes back. Sent together with text
    # the filter changes, it is not given back and opens the next message.
    if live:
        monkeypatch.setattr(slack_handler, "_EDIT_INTERVAL", 0.0)
    slack = _StoredSlack()
    _run_on(
        slack,
        [
            _text("Checking now 42"),
            _wait_call(),
            _text("42 tasks completed. "),
            _text("key AKIAIOSFODNN7EXAMPLE done"),
        ],
    )

    first, second = list(slack.text_by_ts)
    assert [edit["text"] for edit in _updates(slack) if edit["ts"] == first] == given_back
    [final] = [edit["text"] for edit in _updates(slack) if edit["ts"] == second]
    assert final.startswith(opening)
    assert "IOSFODNN7EXAMPLE" not in _seen(slack)


def test_a_word_held_across_two_waits_goes_back_to_the_message_it_ended():
    slack = _StoredSlack()
    _run_on(slack, [_text("Checking now 42"), _wait_call(), _wait_call(), _text("Results are in.")])

    first = next(iter(slack.text_by_ts))
    assert [(edit["ts"], edit["text"]) for edit in _updates(slack)] == [(first, "Checking now 42")]
    assert "".join(slack.text_by_ts.values()) == "Checking now Results are in."


def test_each_wait_gives_its_own_word_back_to_its_own_message():
    slack = _StoredSlack()
    _run_on(
        slack,
        [_text("Step one 42"), _wait_call(), _text("Step two 77"), _wait_call(), _text("Done.")],
    )

    first, second, _third = list(slack.text_by_ts)
    assert [(edit["ts"], edit["text"]) for edit in _updates(slack)] == [
        (first, "Step one 42"),
        (second, "Step two 77"),
    ]
    assert _messages(slack) == ["Step one ", "Step two ", "Done."]


class _SecondMessageDemotesSlack(_StoredSlack):
    """The second message opens, then refuses every append and no new stream opens."""

    async def append_stream(self, channel, ts, text):
        if self.text_by_ts and ts != next(iter(self.text_by_ts)):
            self.actions.append(("append_stream_refused", {"ts": ts, "text": text}))
            return False
        return await super().append_stream(channel, ts, text)

    async def start_stream(self, channel, thread_ts, **kwargs):
        if sum(1 for name, _ in self.actions if name == "start_stream") >= 2:
            return None
        return await super().start_stream(channel, thread_ts, **kwargs)


def test_a_given_back_word_is_not_repeated_when_the_next_message_is_refused():
    slack = _SecondMessageDemotesSlack()
    _run_on(slack, [_text("Checking now 42"), _wait_call(), _text("Results are in.")])

    first = next(iter(slack.text_by_ts))
    second = [payload["ts"] for name, payload in slack.actions if name == "start_stream"][1]
    # Message 2 falls back to an edit with its whole text, which starts after the word.
    assert [(edit["ts"], edit["text"]) for edit in _updates(slack)] == [
        (first, "Checking now 42"),
        (second, "Results are in."),
    ]
    sent = [
        str(payload.get("text") or "")
        for name, payload in slack.actions
        if name in ("append_stream", "append_stream_refused", "stop_stream", "update", "post")
    ]
    assert not any(text.startswith("42") for text in sent)
    stops = [payload for name, payload in slack.actions if name == "stop_stream"]
    assert stops[-1]["text"] == "Results are in."


@pytest.mark.parametrize(
    ("before", "after", "hidden"),
    [
        ("Here it is key=AKI", "AIOSFODNN7EXAMPLE and more", "IOSFODNN7EXAMPLE"),
        ("bot 12345", "67890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw then more", "AAHdqTcvCH1v"),
        ("see x", "AKIAIOSFODNN7EXAMPLE then more", "IOSFODNN7EXAMPLE"),
        ("The key is AKIA", "IOSFODNN7EXAMPLE and more", "IOSFODNN7EXAMPLE"),
        ("Header: Authorization: Bearer ", f"{_OPAQUE} then more", _OPAQUE),
    ],
    ids=["after-a-name", "inside-digits", "no-break-inside-a-value", "after-a-prefix", "bearer"],
)
@pytest.mark.parametrize("shape", ["markdown", "rich_text"])
def test_a_value_split_by_a_wait_is_never_given_back_split(shape, before, after, hidden):
    slack = _StoredSlack(shape)
    _run_on(slack, [_text(before), _wait_call(), _text(after)])

    assert hidden not in _seen(slack)
    first = next(iter(slack.text_by_ts))
    assert [edit for edit in _updates(slack) if edit["ts"] == first] == []


def test_a_value_split_across_two_waits_stays_filtered():
    tool = LLMEvent(
        kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"
    )
    slack, _sessions = _run(
        [
            _text("Header: Authorization: Bearer "),
            _text("A" * 5000),
            _wait_call(),
            tool,
            _wait_call(),
            _text(f"{_OPAQUE} then more"),
        ]
    )

    assert _OPAQUE not in _every_text(slack)
    assert "A" * 64 not in _every_text(slack)


class _SecondStreamFailsSlack(_StreamingSlack):
    """The first stream opens; every later one is refused."""

    def __init__(self) -> None:
        super().__init__()
        self._starts = 0

    async def start_stream(self, channel, thread_ts, **kwargs):
        self._starts += 1
        if self._starts > 1:
            return None
        return await super().start_stream(channel, thread_ts, **kwargs)


def test_a_value_split_by_a_wait_and_a_tool_line_stays_filtered():
    tool = LLMEvent(
        kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"
    )
    slack = _SecondStreamFailsSlack()
    sessions = _TrackSessions(
        FakeProvider(
            [_text("Header: Authorization: Bearer "), _wait_call(), tool, _text(f"{_OPAQUE} x")]
        )
    )
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    assert _OPAQUE not in _every_text(slack)


@pytest.mark.parametrize(
    ("before", "after", "hidden"),
    [
        ("Here it is key=AKI", "AIOSFODNN7EXAMPLE and more", "IOSFODNN7EXAMPLE"),
        ("bot 12345", "67890:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw then more", "AAHdqTcvCH1v"),
        ("see x", "AKIAIOSFODNN7EXAMPLE then more", "IOSFODNN7EXAMPLE"),
    ],
    ids=["after-a-name", "inside-digits", "no-break-inside-a-value"],
)
def test_a_value_split_by_a_wait_stays_filtered(before, after, hidden):
    slack, _sessions = _run([_text(before), _wait_call(), _text(after)])

    assert hidden not in _every_text(slack)


def test_a_key_split_after_its_prefix_by_a_wait_is_filtered_as_one():
    slack, _sessions = _run(
        [_text("The key is AKIA"), _wait_call(), _text("IOSFODNN7EXAMPLE and more")]
    )

    assert "IOSFODNN7EXAMPLE" not in _every_text(slack)


class _LastSendRefusedSlack(_StreamingSlack):
    """The first append lands; later appends are refused and no new stream opens."""

    def __init__(self) -> None:
        super().__init__()
        self._appends = 0
        self._starts = 0

    async def append_stream(self, channel, ts, text):
        self.actions.append(("append_stream", {"channel": channel, "ts": ts, "text": text}))
        self._appends += 1
        return self._appends == 1

    async def start_stream(self, channel, thread_ts, **kwargs):
        self._starts += 1
        if self._starts > 1:
            return None
        return await super().start_stream(channel, thread_ts, **kwargs)


def test_the_last_word_reaches_the_reader_when_its_send_and_retry_fail():
    slack = _LastSendRefusedSlack()
    sessions = _TrackSessions(FakeProvider([_text("The answer is "), _text("forty2")]))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    updates = [payload.get("text", "") for name, payload in slack.actions if name == "update"]
    assert any("The answer is forty2" in text for text in updates)
    assert sessions.calls == {"success": 1, "failure": 0}


def test_the_last_text_reaches_the_reader_when_the_stream_fails_while_finishing():
    slack = _LastSendRefusedSlack()
    sessions = _TrackSessions(
        FakeProvider([_text("The answer is "), _text("exactly forty2 and more")])
    )
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    updates = [payload.get("text", "") for name, payload in slack.actions if name == "update"]
    assert any("The answer is exactly forty2 and more" in text for text in updates)
    assert sessions.calls == {"success": 1, "failure": 0}


class _DiesAfterProvider(FakeProvider):
    """Yields its events, then the agent process dies before the turn completes."""

    async def stream(self, message, timeout=120.0):
        for event in self._events:
            yield event
        raise AcpProcessDied("gone")


def test_an_agent_that_dies_during_a_wait_shows_the_error_notice():
    slack = _StreamingSlack()
    sessions = _TrackSessions(_DiesAfterProvider([_text("first part 42"), _wait_call()]))
    asyncio.run(handle_message(slack, sessions, "C1", "q?", None, "msg1", "U1"))

    posts = [payload.get("text", "") for name, payload in slack.actions if name == "post"]
    assert any("Agent process died" in text for text in posts)


def test_a_key_split_by_a_wait_and_another_tool_call_is_filtered_as_one():
    tool = LLMEvent(
        kind=EVENT_TOOL_CALL, title="Running: read", tool_name="read", tool_call_id="t1"
    )
    slack, _sessions = _run(
        [_text("The key is AKIA"), _wait_call(), tool, _text("IOSFODNN7EXAMPLE and more")]
    )

    assert "IOSFODNN7EXAMPLE" not in _every_text(slack)
