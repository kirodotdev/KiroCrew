"""The engine is sent an unchanged ``[RUNTIME]`` or ``[PROJECT]`` line once per conversation.

Every built message carries both lines. Each has its own delivery slot beside
the reply-format rules, with the same acknowledgment and triggers: a changed
line has another digest and goes out. A harness whose compaction Kiro Crew does
not see, and a turn that is not interactive, get both lines on every message.

Each test reads the prompt the transport was handed.
"""

from pathlib import Path

import pytest
from test_reply_format_once import (  # noqa: F401 (_close_skills_loaders: a fixture)
    PROVIDERS,
    _agent_change,
    _builder,
    _Chat,
    _clear_command,
    _close_skills_loaders,
    _compact_command,
    _DoesNotSay,
    _fresh_conversation,
    _native_clear,
    _seen_compaction,
    _Wire,
)

from kiro_crew.acp.types import EVENT_COMPLETE, STOP_REASON_CANCELLED, AcpEvent

RUNTIME = "[RUNTIME] "
PROJECT = "[PROJECT] Active project directory: "
DIFF = "For THIS turn: this surface renders no tool cards"

pytestmark = pytest.mark.usefixtures("ample_host_resources")


def _lines(prompt: str) -> tuple[bool, bool]:
    return RUNTIME in prompt, PROJECT in prompt


async def _turn(chat: _Chat, **kw: object) -> tuple[bool, bool]:
    """One dashboard follow-up; which lines the engine was sent."""
    kw.setdefault("runtime_source", "dashboard")
    message = chat.build(**kw)
    assert _lines(message) == (True, True), "every built message carries both lines"
    return _lines(await chat.send(message))


def _chat(tmp_path: Path, backend: str = "", kind: str = "direct") -> _Chat:
    wire = _Wire(backend, work_dir=tmp_path)
    return _Chat(_builder(tmp_path), wire, PROVIDERS[kind](wire))


class TestAWarmFollowUp:
    @pytest.mark.parametrize("kind", sorted(PROVIDERS))
    @pytest.mark.asyncio
    async def test_is_not_sent_the_lines_again(self, kind: str, tmp_path: Path) -> None:
        chat = _chat(tmp_path, kind=kind)
        assert await _turn(chat) == (True, True)

        for _ in range(3):
            assert await _turn(chat) == (False, False)
            assert "[CURRENT USER REQUEST" in chat.wire.sent[-1]

    @pytest.mark.asyncio
    async def test_the_prompt_is_shorter_by_exactly_the_two_lines(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        await _turn(chat)
        await _turn(chat)
        first, second = chat.wire.sent

        runtime = first[first.index(RUNTIME) :].split("\n\n", 1)[0] + "\n\n"
        project = first[first.index(PROJECT) :].split("\n\n", 1)[0] + "\n\n"
        rules = first[first.index("[REPLY FORMAT RULES]") : first.index("[CURRENT USER REQUEST")]
        assert first.replace(runtime, "").replace(project, "").replace(rules, "") == second


class TestAChangedLineIsSent:
    @pytest.mark.asyncio
    async def test_another_surface_and_back(self, tmp_path: Path) -> None:
        """A dashboard slot runs a message from its linked channel thread as a slack turn."""
        chat = _chat(tmp_path)
        await _turn(chat)
        assert await _turn(chat) == (False, False)

        assert await _turn(chat, runtime_source="slack") == (True, False)
        assert "[RUNTIME] Slack" in chat.wire.sent[-1]
        assert await _turn(chat, runtime_source="slack") == (False, False)
        assert DIFF in chat.wire.sent[-1], "the channel's diff mandate goes out every turn"

        assert await _turn(chat) == (True, False)
        assert "[RUNTIME] KiroCrew dashboard" in chat.wire.sent[-1]  # brand-ok: prompt bytes

    @pytest.mark.asyncio
    async def test_another_project(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        await _turn(chat)
        assert await _turn(chat) == (False, False)

        other = str(tmp_path / "other")
        assert await _turn(chat, project=other) == (False, True)
        assert f"{PROJECT}{other}" in chat.wire.sent[-1]
        assert await _turn(chat, project=other) == (False, False)


async def _failed_turn(chat: _Chat) -> None:
    good, chat.wire.events = chat.wire.events, [
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_CANCELLED)
    ]
    await _turn(chat)
    chat.wire.events = good


EVENTS = {
    "a compaction seen on the stream": _seen_compaction,
    "/compact": _compact_command,
    "a native clear": _native_clear,
    "/clear": _clear_command,
    "an agent switch": _agent_change,
    "a failed turn": _failed_turn,
    "a new conversation": _fresh_conversation,
}


class TestEverythingThatCanDropTheLinesSendsThemAgain:
    @pytest.mark.parametrize("event", sorted(EVENTS))
    @pytest.mark.asyncio
    async def test_the_next_prompt_carries_both(self, event: str, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        await _turn(chat)
        assert await _turn(chat) == (False, False)

        await EVENTS[event](chat)

        assert await _turn(chat) == (True, True)
        assert await _turn(chat) == (False, False)

    @pytest.mark.asyncio
    async def test_a_compaction_the_agent_started(self, tmp_path: Path) -> None:
        chat = _chat(tmp_path)
        await _turn(chat)
        assert await _turn(chat) == (False, False)

        assert await _turn(chat, needs_reinjection=True) == (True, True)
        assert await _turn(chat) == (False, False)


class TestEveryMessage:
    @pytest.mark.parametrize("backend", ["goose", "opencode", "pi"])
    @pytest.mark.asyncio
    async def test_on_a_harness_that_does_not_report_compaction(
        self, backend: str, tmp_path: Path
    ) -> None:
        chat = _chat(tmp_path, backend)
        assert chat.target.reports_compaction is False

        assert [await _turn(chat) for _ in range(3)] == [(True, True)] * 3

    @pytest.mark.asyncio
    async def test_on_a_provider_that_does_not_say(self, tmp_path: Path) -> None:
        wire = _Wire(work_dir=tmp_path)
        chat = _Chat(_builder(tmp_path), wire, _DoesNotSay(wire))

        project = str(tmp_path)
        assert [await _turn(chat, project=project) for _ in range(3)] == [(True, True)] * 3

    @pytest.mark.asyncio
    async def test_on_a_turn_that_is_not_interactive(self, tmp_path: Path) -> None:
        """A non-interactive build in an acknowledged chat goes out as it was built."""
        chat = _chat(tmp_path)
        await _turn(chat)
        assert await _turn(chat) == (False, False)

        for _ in range(2):
            assert await _turn(chat, interactive=False) == (True, True)
        assert await _turn(chat) == (False, False)
