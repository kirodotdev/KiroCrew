"""Tests for kiro_crew.imessage.renderer (IMessageRenderer, Layer 2b)."""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.imessage.renderer import IMessageRenderer
from kiro_crew.imessage.rpc import RpcError, RpcTransportError
from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES

HANDLE = "+15551234567"
SELECTOR = {"chat_guid": "iMessage;-;+15551234567"}


class FakeClient:
    """Records sends, typing pokes, and read receipts in order."""

    def __init__(self, *, typing: bool = True, read: bool = True) -> None:
        self.sent: list[str] = []
        self.typing_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []
        self.typing_supported = typing
        self.read_supported = read

    async def send(self, to: str, text: str) -> str:
        assert to == HANDLE
        self.sent.append(text)
        return f"GUID-{len(self.sent)}"

    async def send_typing(self, selector: dict[str, Any]) -> None:
        if self.typing_supported:
            self.typing_calls.append(dict(selector))

    async def mark_read(self, selector: dict[str, Any]) -> None:
        if self.read_supported:
            self.read_calls.append(dict(selector))


def _renderer(client: FakeClient, **kwargs: Any) -> IMessageRenderer:
    kwargs.setdefault("chat_selector", SELECTOR)
    return IMessageRenderer(client, HANDLE, IMESSAGE_CAPABILITIES, **kwargs)  # type: ignore[arg-type]


class TestDeliveryFailureIsNotSuccess:
    """A reply that never reached the user must not read as a completed turn.

    `on_done` is the terminal hook, so a clean return there tells `drive_turn` the
    answer was delivered and the turn is persisted as successful. Catching the
    failure in `client.send` and catching it again here would only move the defect
    up a layer.
    """

    @pytest.mark.asyncio
    async def test_on_done_propagates_a_delivery_failure(self) -> None:
        client = FakeClient()

        async def boom(to: str, text: str) -> str:
            raise RpcError(-32001, "delivery in flight")

        client.send = boom  # type: ignore[method-assign]
        renderer = _renderer(client)
        await renderer.on_text_chunk("an answer the user never sees")
        with pytest.raises(RpcError):
            await renderer.on_done()

    @pytest.mark.asyncio
    async def test_a_transport_failure_propagates_too(self) -> None:
        client = FakeClient()

        async def boom(to: str, text: str) -> str:
            raise RpcTransportError("bridge exited")

        client.send = boom  # type: ignore[method-assign]
        renderer = _renderer(client)
        await renderer.on_text_chunk("hello")
        with pytest.raises(RpcTransportError):
            await renderer.on_done()

    @pytest.mark.asyncio
    async def test_close_contains_the_failure(self) -> None:
        # `close` is the ONE place it is absorbed, because it is already the
        # failure path: letting a send error escape teardown would replace the
        # turn's real error and skip the rest of cleanup.
        client = FakeClient()

        async def boom(to: str, text: str) -> str:
            raise RpcTransportError("bridge exited")

        client.send = boom  # type: ignore[method-assign]
        renderer = _renderer(client)
        await renderer.close()  # must not raise

    @pytest.mark.asyncio
    async def test_a_later_chunk_failure_still_propagates(self) -> None:
        # Not just the first chunk: a mid-delivery failure is still an
        # undelivered answer.
        client = FakeClient()
        sent: list[str] = []

        async def flaky(to: str, text: str) -> str:
            sent.append(text)
            if len(sent) >= 2:
                raise RpcTransportError("bridge exited mid-delivery")
            return "G-1"

        client.send = flaky  # type: ignore[method-assign]
        renderer = _renderer(client)
        await renderer.on_text_chunk("x" * (IMESSAGE_CAPABILITIES.max_message_chars * 2))
        with pytest.raises(RpcTransportError):
            await renderer.on_done()
        assert len(sent) == 2


class TestNoPlaceholder:
    @pytest.mark.asyncio
    async def test_turn_start_sends_no_message(self) -> None:
        # A sent iMessage cannot be edited, so any placeholder would be stranded
        # above the answer permanently.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_turn_start_acknowledges_via_read_and_typing(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        assert client.read_calls == [SELECTOR]
        assert client.typing_calls == [SELECTOR]

    @pytest.mark.asyncio
    async def test_turn_start_is_idempotent(self) -> None:
        # Both the dispatcher and the driver call it.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_turn_start()
        assert len(client.typing_calls) == 1


class TestTypingIndicator:
    @pytest.mark.asyncio
    async def test_tool_calls_refresh_the_indicator_but_are_throttled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The indicator expires on its own, so a long tool run needs re-poking --
        # but one call per tool event would burst the single mutation worker.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        for _ in range(5):
            await renderer.on_tool_call("t", "grep")
        assert len(client.typing_calls) == 1

    @pytest.mark.asyncio
    async def test_the_indicator_refreshes_once_the_throttle_elapses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        # Advance the renderer's own clock rather than sleeping a real interval.
        renderer._last_typing -= 100.0
        await renderer.on_tool_call("t", "grep")
        assert len(client.typing_calls) == 2

    @pytest.mark.asyncio
    async def test_the_tool_name_is_never_sent_as_a_message(self) -> None:
        # Naming it would need a message, and a message here cannot be recalled.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_tool_call("t", "read_credentials_file")
        assert all("read_credentials_file" not in text for text in client.sent)

    @pytest.mark.asyncio
    async def test_no_selector_means_no_typing_attempt(self) -> None:
        client = FakeClient()
        renderer = _renderer(client, chat_selector={})
        await renderer.on_turn_start()
        await renderer.on_tool_call("t", "grep")
        assert client.typing_calls == []

    @pytest.mark.asyncio
    async def test_a_bridge_without_typing_still_completes_the_turn(self) -> None:
        client = FakeClient(typing=False, read=False)
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_text_chunk("done")
        await renderer.on_done()
        assert client.sent == ["done"]


class TestDelivery:
    @pytest.mark.asyncio
    async def test_the_answer_is_flattened_before_sending(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("**bold** and `code`")
        await renderer.on_done()
        assert client.sent == ["bold and code"]

    @pytest.mark.asyncio
    async def test_code_block_contents_survive_flattening(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("here:\n\n```py\nx = **1**\n```")
        await renderer.on_done()
        assert "x = **1**" in client.sent[0]

    @pytest.mark.asyncio
    async def test_chunks_are_accumulated_not_streamed(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("one ")
        await renderer.on_text_chunk("two ")
        await renderer.on_text_chunk("three")
        assert client.sent == []
        await renderer.on_done()
        assert client.sent == ["one two three"]

    @pytest.mark.asyncio
    async def test_a_long_answer_goes_out_as_several_messages(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        paragraph = "x" * 3000
        await renderer.on_text_chunk(paragraph + "\n\n" + paragraph)
        await renderer.on_done()
        assert len(client.sent) == 2
        assert all(len(text) <= IMESSAGE_CAPABILITIES.max_message_chars for text in client.sent)

    @pytest.mark.asyncio
    async def test_an_options_trailer_becomes_numbered_text(self) -> None:
        # iMessage has no tappable choices, but the choices are the answers to the
        # question the body just asked -- dropping them left the user with no way
        # to see what was offered.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("Pick one.\n\n[OPTIONS: a | b | c]")
        await renderer.on_done()
        assert client.sent == ["Pick one.\n\n1. a\n2. b\n3. c"]

    @pytest.mark.asyncio
    async def test_an_incomplete_options_fragment_is_not_cut_off_the_answer(self) -> None:
        # iMessage never streams, so an incomplete marker at finalization is not a
        # marker still arriving -- it is the assistant's text, and deleting it to
        # tidy up protocol would lose authored content.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("Pick one.\n\n[OPTIONS: a | b")
        await renderer.on_done()
        assert client.sent == ["Pick one.\n\n[OPTIONS: a | b"]

    @pytest.mark.asyncio
    async def test_an_empty_answer_still_sends_something(self) -> None:
        # Silence would read as the agent having ignored the message.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_done()
        assert client.sent == ["…"]

    @pytest.mark.asyncio
    async def test_an_errored_turn_says_so(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_done(stop_reason="error")
        assert len(client.sent) == 1
        assert "went wrong" in client.sent[0]

    @pytest.mark.asyncio
    async def test_a_partial_answer_survives_an_errored_turn(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("as far as I got")
        await renderer.on_done(stop_reason="error")
        assert client.sent == ["as far as I got"]

    @pytest.mark.asyncio
    async def test_done_is_idempotent(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("once")
        await renderer.on_done()
        await renderer.on_done()
        assert client.sent == ["once"]


class TestNoOpEvents:
    @pytest.mark.asyncio
    async def test_reasoning_is_not_surfaced_inline(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_thinking("let me consider the credentials file")
        await renderer.on_done()
        assert client.sent == ["…"]

    @pytest.mark.asyncio
    async def test_prompt_choice_sends_nothing(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_prompt_choice([{"label": "yes"}], 1)
        assert client.sent == []

    @pytest.mark.asyncio
    async def test_compaction_status_sends_nothing(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_compaction(91.5)
        assert client.sent == []


class TestClose:
    @pytest.mark.asyncio
    async def test_close_finalizes_a_turn_that_never_reached_done(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("partial")
        await renderer.close()
        assert client.sent == ["partial"]

    @pytest.mark.asyncio
    async def test_close_after_done_sends_nothing_more(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("answer")
        await renderer.on_done()
        await renderer.close()
        assert client.sent == ["answer"]


class TestTextAccessors:
    @pytest.mark.asyncio
    async def test_text_keeps_markdown_for_the_dashboard_mirror(self) -> None:
        # The archive renders markdown; flattening it would lose the code fence.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("**bold**")
        assert renderer.text() == "**bold**"
        assert renderer.delivery_text() == "bold"


class TestFlattenedCredentialIsRedacted:
    """The flatten-after-scan hazard ``display_safety`` exists to close.

    ``TurnDriver`` scans the provider stream as literal bytes, so a credential
    split by markup matches no pattern as written and survives that pass. This
    channel then collapses the markup ITSELF, which would hand the reader an
    intact secret unless the flattened form is re-scanned.
    """

    @pytest.mark.asyncio
    async def test_emphasis_split_key_does_not_survive_flattening(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("key **AKIA**IOSFODNN7EXAMPLE ok")

        # The raw markdown is what the driver's byte scan saw: still split, so
        # the literal pattern could not match it.
        assert "**AKIA**IOSFODNN7EXAMPLE" in renderer.text()
        # The form that actually ships must not contain the reassembled key.
        delivered = renderer.delivery_text()
        assert "AKIAIOSFODNN7EXAMPLE" not in delivered

    @pytest.mark.asyncio
    async def test_code_span_split_key_does_not_survive_flattening(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("`AKIA`IOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in renderer.delivery_text()

    @pytest.mark.asyncio
    async def test_credential_reassembled_through_link_grammar_is_caught(self) -> None:
        # Reassembly via the link grammar rather than emphasis: the key is split
        # across the label/target boundary, so the byte scan sees no key at all.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("[AKIA](https://x.example)IOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in renderer.delivery_text()

    @pytest.mark.asyncio
    async def test_ordinary_prose_is_delivered_unchanged(self) -> None:
        # Non-vacuity: the re-scan must not mangle text with nothing to redact.
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("Deployed **build 42** to staging.")
        assert renderer.delivery_text() == "Deployed build 42 to staging."
