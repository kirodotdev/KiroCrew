"""Tests for the Teams renderer (typing indicator + single final answer,
OPTIONS trailer stripped, chunking) and command parsing."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.messaging import TurnDriver
from kiro_crew.teams.client import TeamsSendError
from kiro_crew.teams.commands import HELP_TEXT, parse_command
from kiro_crew.teams.renderer import TeamsRenderer, _strip_options
from kiro_crew.teams.transport import TEAMS_CAPABILITIES


class _FakeClient:
    def __init__(self) -> None:
        self.typing: list[tuple[str, str]] = []
        self.sent: list[str] = []
        self.cards: list[dict] = []
        self.fail = False

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        self.typing.append((conversation_id, service_url))

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        if self.fail:
            return None
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def send_card(self, conversation_id: str, card: dict, service_url: str):
        self.cards.append(card)
        return f"card-{len(self.cards)}"

    async def update_card(self, conversation_id, activity_id, card, service_url) -> bool:
        self.cards.append(card)
        return True


def _renderer(client: _FakeClient) -> TeamsRenderer:
    return TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)


class _Provider:
    def __init__(self, events: list[AcpEvent]) -> None:
        self.events = events

    async def stream(self, message: str) -> Any:
        for event in self.events:
            yield event

    async def approve_tool(self, request_id: Any, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


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
        assert client.typing == [("conv-1", "https://smba.trafficmanager.net/")]
        assert client.sent == ["Hello world"]

    @pytest.mark.asyncio
    async def test_options_trailer_becomes_tappable_chips(self) -> None:
        """Teams renders choices as Adaptive Card actions rather than dropping them.

        Stripping the trailer left the user unable to see the offered choices at
        all, which reads as the feature not existing.
        """
        client = _FakeClient()
        r = _renderer(client)
        await r.on_turn_start()
        await r.on_text_chunk("pick one\n[OPTIONS: yes | no]")
        await r.on_done()

        assert client.sent == ["pick one"], "the body loses the raw markup"
        assert len(client.cards) == 1
        titles = [a["title"] for a in client.cards[0]["content"]["actions"]]
        assert titles == ["yes", "no"]

    @pytest.mark.asyncio
    async def test_over_cap_reply_chunked_without_loss(self) -> None:
        client = _FakeClient()
        # tiny cap renderer to force chunking
        from dataclasses import replace

        caps = replace(TEAMS_CAPABILITIES, max_message_chars=10)
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", caps)
        await r.on_turn_start()
        await r.on_text_chunk("abcdefghijklmnopqrstuvwxyz")  # 26 chars, cap 10
        await r.on_done()
        assert len(client.sent) == 3
        assert "".join(client.sent) == "abcdefghijklmnopqrstuvwxyz"

    @pytest.mark.asyncio
    async def test_shared_driver_hides_compaction_summary_body(self) -> None:
        client = _FakeClient()
        provider = _Provider(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Conversation comp"),
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK,
                    text="acted: ## OBJECTIVE\ninternal user guidance",
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        await TurnDriver(provider, _renderer(client), approval_mode="auto").run("hi")
        assert client.sent == ["✅ Context compacted."]
        assert "OBJECTIVE" not in "".join(client.sent)
        assert "user guidance" not in "".join(client.sent)

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


class _EditingClient:
    """Models the Connector contract: sends return an id, PUTs edit in place."""

    def __init__(self, *, can_edit: bool = True, send_raises_after: int | None = None) -> None:
        self.typing: list[tuple[str, str]] = []
        self.sent: list[str] = []
        self.updates: list[tuple[str, str]] = []
        self._can_edit = can_edit
        self._send_raises_after = send_raises_after

    async def send_typing(self, conversation_id: str, service_url: str) -> None:
        self.typing.append((conversation_id, service_url))

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        if self._send_raises_after is not None and len(self.sent) >= self._send_raises_after:
            raise TeamsSendError("connector said no")
        self.sent.append(content)
        return f"mid-{len(self.sent)}"

    async def update_message(self, conversation_id, activity_id, content, service_url) -> bool:
        if not self._can_edit:
            return False
        self.updates.append((activity_id, content))
        return True


class TestProgressiveOutput:
    """A long agentic turn must not look dead, and must not leave litter behind."""

    @pytest.mark.asyncio
    async def test_the_answer_reuses_the_progress_message(self) -> None:
        client = _EditingClient()
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_read")
        await r.on_text_chunk("the answer")
        await r.on_done()

        assert client.sent == ["🔧 fs_read…"], "exactly one message is posted"
        assert (
            client.updates[-1][1] == "the answer"
        ), "the answer must EDIT the progress bubble, not leave '🔧 …' stranded above it"

    @pytest.mark.asyncio
    async def test_a_turn_with_no_tools_posts_exactly_one_message(self) -> None:
        client = _EditingClient()
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_turn_start()
        await r.on_text_chunk("quick reply")
        await r.on_done()

        assert client.sent == ["quick reply"]
        assert client.updates == [], "no progress bubble was opened, so nothing to edit"

    @pytest.mark.asyncio
    async def test_progress_writes_are_throttled(self) -> None:
        """Teams allows 7 requests/second per thread; a tool-heavy turn must pace."""
        client = _EditingClient()
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_turn_start()
        for index in range(6):
            await r.on_tool_call(f"t{index}", f"tool_{index}")

        assert len(client.sent) + len(client.updates) <= 1, (
            "six back-to-back tool calls inside the throttle window must not "
            "produce six outbound writes"
        )

    @pytest.mark.asyncio
    async def test_a_failed_edit_falls_back_to_a_fresh_message(self) -> None:
        """An un-editable progress bubble must not swallow the answer."""
        client = _EditingClient(can_edit=False)
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_read")
        await r.on_text_chunk("the answer")
        await r.on_done()

        assert "the answer" in client.sent, "the answer is delivered even when the edit fails"

    @pytest.mark.asyncio
    async def test_delivery_stops_at_the_first_failed_chunk_and_raises(self) -> None:
        """Two properties, and the second one changed for a reason.

        It still stops rather than skipping ahead -- a gap spliced into the middle of an
        answer is worse than a short one. But it now RAISES instead of returning
        quietly: ``drive_turn`` treats a clean return from the renderer as delivery, so
        swallowing the refusal made it run ``record_success`` and persist the full
        answer while the Connector had refused it. Raising skips both and records a
        failure the user can retry.
        """
        client = _EditingClient(send_raises_after=0)
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_text_chunk("body")

        with pytest.raises(TeamsSendError):
            await r.on_done()

        assert client.sent == []


class TestFenceSafeSplitting:
    @pytest.mark.asyncio
    async def test_a_code_fence_is_not_cut_in_half(self) -> None:
        """Blind fixed-width slicing splits a fence and corrupts the rendering."""
        client = _EditingClient()
        caps = replace(TEAMS_CAPABILITIES, max_message_chars=200)
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", caps)
        body = "intro\n\n```python\n" + "\n".join(f"line_{i} = {i}" for i in range(40)) + "\n```\n"
        await r.on_text_chunk(body)
        await r.on_done()

        assert len(client.sent) > 1, "the reply must actually have been split"
        for chunk in client.sent:
            assert (
                chunk.count("```") % 2 == 0
            ), f"chunk leaves a fence unbalanced, so Teams renders the rest as code: {chunk!r}"


class TestDisplayRedaction:
    @pytest.mark.asyncio
    async def test_a_credential_hidden_by_markdown_is_still_redacted(self) -> None:
        """Teams renders markup away, reassembling a key the raw scan saw as broken."""
        client = _EditingClient()
        r = TeamsRenderer(client, "conv-1", "https://smba.trafficmanager.net/", TEAMS_CAPABILITIES)
        await r.on_text_chunk("key AKIA**IOSFODNN7EXAMPLE** here")
        await r.on_done()

        delivered = client.sent[0]
        assert "AKIAIOSFODNN7EXAMPLE" not in delivered.replace("*", "")
