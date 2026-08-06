"""Tests for kiro_crew.messaging -- v1a contracts (ABCs, value objects)."""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.messaging import (
    COMPACTION,
    DONE,
    OUTPUT_KINDS,
    PROMPT_CHOICE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    THINKING,
    TOOL_CALL,
    ConfiguredChannelTarget,
    InboundMessage,
    MessagingTransport,
    OutputEvent,
    Renderer,
    TransportCapabilities,
    chunk_text,
)


class TestTransportCapabilities:
    def test_conservative_defaults(self):
        cap = TransportCapabilities()
        assert cap.streaming is False
        assert cap.rich_blocks is False
        assert cap.max_message_chars == 4096
        assert cap.max_buttons == 3
        assert cap.supports_proactive_send is True

    def test_to_dict_round_trip(self):
        cap = TransportCapabilities(streaming=True, max_message_chars=2000, max_buttons=5)
        d = cap.to_dict()
        assert d["streaming"] is True
        assert d["max_message_chars"] == 2000
        assert set(d) == {
            "streaming",
            "edit",
            "reactions",
            "files_inbound",
            "files_outbound",
            "rich_blocks",
            "threads",
            "max_message_chars",
            "max_buttons",
            "supports_proactive_send",
        }


class TestInboundMessage:
    def test_defaults(self):
        msg = InboundMessage(channel_type="slack", user_id="U1", conversation_id="C1", text="hi")
        assert msg.thread_id is None
        assert msg.attachments == []
        assert msg.is_mention is False

    def test_attachments_are_per_instance(self):
        a = InboundMessage(channel_type="x", user_id="u", conversation_id="c", text="t")
        b = InboundMessage(channel_type="x", user_id="u", conversation_id="c", text="t")
        a.attachments.append("file")
        assert b.attachments == []


class TestConfiguredChannelTarget:
    def test_serializes_with_its_transport_namespace(self):
        target = ConfiguredChannelTarget(
            "user:42",
            "Discord DM · 42",
            available=False,
            unavailable_reason="not ready",
        )

        assert target.to_dict("discord") == {
            "channel_type": "discord",
            "target_id": "user:42",
            "label": "Discord DM · 42",
            "available": False,
            "unavailable_reason": "not ready",
        }


class TestAbstract:
    def test_transport_not_instantiable(self):
        with pytest.raises(TypeError):
            MessagingTransport()  # type: ignore[abstract]

    def test_renderer_not_instantiable(self):
        with pytest.raises(TypeError):
            Renderer(TransportCapabilities())  # type: ignore[abstract]

    def test_minimal_concrete_transport(self):
        class FakeTransport(MessagingTransport):
            channel_type = "fake"

            def __init__(self):
                self.capabilities = TransportCapabilities()

            async def send_message(self, conversation_id, content, thread_id=None):
                return "m1"

            async def resolve_conversation(self, user_id):
                return "dm:" + user_id

            async def fetch_history(self, conversation_id, thread_id=None):
                return []

            async def receive(self, raw_envelope):
                return None

            def authorize(self, msg):
                return False

        t = FakeTransport()
        assert asyncio.run(t.send_message("c", "x")) == "m1"
        assert t.authorize(None) is False


class TestChunkText:
    def test_empty(self):
        assert chunk_text("", 10) == []

    def test_shorter(self):
        assert chunk_text("abc", 10) == ["abc"]

    def test_splits(self):
        assert chunk_text("abcdef", 2) == ["ab", "cd", "ef"]

    def test_nonpositive_disables(self):
        assert chunk_text("abcdef", 0) == ["abcdef"]


class _RecordingRenderer(Renderer):
    def __init__(self, capabilities):
        super().__init__(capabilities)
        self.calls: list[tuple] = []

    async def on_text_chunk(self, text):
        self.calls.append(("text_chunk", text))

    async def on_thinking(self, text):
        self.calls.append(("thinking", text))

    async def on_tool_call(self, tool_call_id, title, tool_kind="", tool_purpose=""):
        self.calls.append(("tool_call", tool_call_id, title))

    async def on_prompt_choice(self, options, request_id):
        self.calls.append(("prompt_choice", options, request_id))

    async def on_compaction(self, context_usage_pct):
        self.calls.append(("compaction", context_usage_pct))

    async def on_done(self, stop_reason=""):
        self.calls.append(("done", stop_reason))


class TestRendererDispatch:
    def test_kinds_registered(self):
        assert OUTPUT_KINDS == {
            TEXT_CHUNK,
            THINKING,
            TOOL_CALL,
            PROMPT_CHOICE,
            COMPACTION,
            DONE,
            STEER_CONSUMED,
        }

    def test_routes_each_kind(self):
        r = _RecordingRenderer(TransportCapabilities())
        events = [
            OutputEvent(kind=TEXT_CHUNK, text="hi"),
            OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="grep"),
            OutputEvent(kind=PROMPT_CHOICE, options=[{"id": "a"}], request_id="r1"),
            OutputEvent(kind=DONE, stop_reason="end_turn"),
        ]

        async def run():
            for ev in events:
                await r.dispatch(ev)

        asyncio.run(run())
        assert [c[0] for c in r.calls] == ["text_chunk", "tool_call", "prompt_choice", "done"]
        assert r.calls[2] == ("prompt_choice", [{"id": "a"}], "r1")

    def test_unknown_kind_raises(self):
        r = _RecordingRenderer(TransportCapabilities())
        with pytest.raises(ValueError):
            asyncio.run(r.dispatch(OutputEvent(kind="bogus")))
