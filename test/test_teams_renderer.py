"""Tests for the Teams renderer (typing indicator + single final answer,
OPTIONS trailer stripped, chunking) and command parsing."""

from __future__ import annotations

import pytest

from kiro_crew.teams.commands import HELP_TEXT, parse_command
from kiro_crew.teams.renderer import TeamsRenderer, _strip_options
from kiro_crew.teams.transport import TEAMS_CAPABILITIES


class _FakeClient:
    def __init__(self) -> None:
        self.typing: list[tuple[str, str]] = []
        self.sent: list[str] = []
        self.fail = False

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        self.typing.append((conversation_id, service_url))

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        if self.fail:
            return None
        self.sent.append(content)
        return f"mid-{len(self.sent)}"


def _renderer(client: _FakeClient) -> TeamsRenderer:
    return TeamsRenderer(client, "conv-1", "https://smba.example.com/", TEAMS_CAPABILITIES)


class TestCommands:
    def test_parse(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("/start") == "new"
        assert parse_command("/compact") == "compact"
        assert parse_command("/help") == "help"
        assert parse_command("hello there") is None
        assert parse_command("  /HELP  ") == "help"

    def test_help_text_nonempty(self) -> None:
        assert "/new" in HELP_TEXT and "/help" in HELP_TEXT


class TestStripOptions:
    def test_strips_trailer(self) -> None:
        assert _strip_options("answer\n[OPTIONS: a | b | c]") == "answer"

    def test_strips_partial(self) -> None:
        assert _strip_options("answer [OPTIONS: a | b") == "answer"

    def test_leaves_plain_text(self) -> None:
        assert _strip_options("just an answer") == "just an answer"


class TestRenderer:
    @pytest.mark.asyncio
    async def test_typing_then_single_answer(self) -> None:
        client = _FakeClient()
        r = _renderer(client)
        await r.on_turn_start()
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        await r.on_done()
        assert client.typing == [("conv-1", "https://smba.example.com/")]
        assert client.sent == ["Hello world"]

    @pytest.mark.asyncio
    async def test_options_trailer_dropped_in_final(self) -> None:
        client = _FakeClient()
        r = _renderer(client)
        await r.on_turn_start()
        await r.on_text_chunk("pick one\n[OPTIONS: yes | no]")
        await r.on_done()
        assert client.sent == ["pick one"]

    @pytest.mark.asyncio
    async def test_over_cap_reply_chunked_without_loss(self) -> None:
        client = _FakeClient()
        # tiny cap renderer to force chunking
        from dataclasses import replace

        caps = replace(TEAMS_CAPABILITIES, max_message_chars=10)
        r = TeamsRenderer(client, "conv-1", "https://smba.example.com/", caps)
        await r.on_turn_start()
        await r.on_text_chunk("abcdefghijklmnopqrstuvwxyz")  # 26 chars, cap 10
        await r.on_done()
        assert len(client.sent) == 3
        assert "".join(client.sent) == "abcdefghijklmnopqrstuvwxyz"

    @pytest.mark.asyncio
    async def test_prompt_choice_is_noop(self) -> None:
        client = _FakeClient()
        r = _renderer(client)
        # should not raise
        await r.on_prompt_choice([{"label": "x"}], "req-1")

    @pytest.mark.asyncio
    async def test_start_idempotent(self) -> None:
        client = _FakeClient()
        r = _renderer(client)
        await r.on_turn_start()
        await r.on_turn_start()
        assert len(client.typing) == 1

    @pytest.mark.asyncio
    async def test_close_finalizes_when_no_done(self) -> None:
        client = _FakeClient()
        r = _renderer(client)
        await r.on_turn_start()
        await r.on_text_chunk("partial")
        await r.close()
        assert client.sent == ["partial"]
