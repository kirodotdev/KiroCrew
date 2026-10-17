"""Tests for kiro_crew.signal_cli.renderer and plaintext (flatten, split, deliver)."""

from __future__ import annotations

import pytest

from kiro_crew.signal_cli.plaintext import chunk_plaintext, to_plaintext
from kiro_crew.signal_cli.renderer import SignalRenderer
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError
from kiro_crew.signal_cli.transport import SIGNAL_CAPABILITIES, SIGNAL_SAFE_MESSAGE_CHARS

OWNER = "+447700900002"


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.typing: list[str] = []
        self.read: list[tuple[str, int]] = []
        self.fail_on: int | None = None

    async def send(self, recipient: str, text: str) -> str:
        if self.fail_on is not None and len(self.sent) == self.fail_on:
            raise RpcTransportError("signal-cli is not running")
        self.sent.append((recipient, text))
        return str(len(self.sent))

    async def send_typing(self, recipient: str) -> None:
        self.typing.append(recipient)

    async def mark_read(self, recipient: str, timestamp: int) -> None:
        self.read.append((recipient, timestamp))


def _renderer(client: FakeClient, *, ts: int = 1700000000100) -> SignalRenderer:
    return SignalRenderer(
        client, OWNER, SIGNAL_CAPABILITIES, reply_timestamp=ts  # type: ignore[arg-type]
    )


class TestPlaintext:
    def test_markdown_is_flattened(self) -> None:
        assert to_plaintext("**bold** and `code`") == "bold and code"

    def test_fenced_code_is_kept_verbatim(self) -> None:
        out = to_plaintext("```\n  indented  **x**\n```")
        assert "  indented  **x**" in out

    def test_long_text_is_split_within_the_limit(self) -> None:
        text = ("word " * 1000).strip()
        chunks = chunk_plaintext(text, SIGNAL_SAFE_MESSAGE_CHARS)
        assert len(chunks) > 1
        assert all(len(c) <= SIGNAL_SAFE_MESSAGE_CHARS for c in chunks)
        assert " ".join(chunks).split() == text.split()


class TestTurn:
    @pytest.mark.asyncio
    async def test_turn_start_sends_a_read_receipt_and_typing(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_turn_start()
        await renderer.on_turn_start()  # idempotent
        assert client.read == [(OWNER, 1700000000100)]
        assert client.typing == [OWNER]

    @pytest.mark.asyncio
    async def test_the_answer_is_sent_once_flat_to_the_recipient(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("## Plan\n\n**Do** ")
        await renderer.on_thinking("hidden reasoning")
        await renderer.on_text_chunk("the thing")
        await renderer.on_done("end_turn")
        await renderer.close()
        assert client.sent == [(OWNER, "Plan\n\nDo the thing")]

    @pytest.mark.asyncio
    async def test_a_long_answer_is_split_into_signal_sized_messages(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("paragraph. " * 500)
        await renderer.on_done("end_turn")
        assert len(client.sent) > 1
        assert all(len(text) <= SIGNAL_SAFE_MESSAGE_CHARS for _, text in client.sent)

    @pytest.mark.asyncio
    async def test_options_become_a_numbered_list(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("Pick one\n[OPTIONS: Yes | No]")
        await renderer.on_done("end_turn")
        body = client.sent[0][1]
        assert "[OPTIONS" not in body
        assert "1." in body and "Yes" in body and "No" in body

    @pytest.mark.asyncio
    async def test_a_delivery_failure_reraises(self) -> None:
        client = FakeClient()
        client.fail_on = 0
        renderer = _renderer(client)
        await renderer.on_text_chunk("answer")
        with pytest.raises(RpcTransportError):
            await renderer.on_done("end_turn")

    @pytest.mark.asyncio
    async def test_close_without_done_sends_the_error_and_contains_failures(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.close()
        assert "went wrong" in client.sent[0][1]
        failing = FakeClient()
        failing.fail_on = 0
        await _renderer(failing).close()  # must not raise during teardown

    @pytest.mark.asyncio
    async def test_a_redacted_credential_gets_a_follow_up_notice(self) -> None:
        client = FakeClient()
        renderer = _renderer(client)
        await renderer.on_text_chunk("key: AKIAIOSFODNN7EXAMPLE")
        await renderer.on_done("end_turn")
        assert "AKIAIOSFODNN7EXAMPLE" not in client.sent[0][1]
        assert len(client.sent) == 2  # the answer, then the redaction notice

    @pytest.mark.asyncio
    async def test_a_failed_notice_does_not_fail_the_delivered_answer(self) -> None:
        client = FakeClient()
        client.fail_on = 1
        renderer = _renderer(client)
        await renderer.on_text_chunk("key: AKIAIOSFODNN7EXAMPLE")
        await renderer.on_done("end_turn")
        assert len(client.sent) == 1

    @pytest.mark.asyncio
    async def test_rpc_errors_are_reraised_too(self) -> None:
        class Rejecting(FakeClient):
            async def send(self, recipient: str, text: str) -> str:
                raise RpcError(-1, "Failed to send message")

        renderer = _renderer(Rejecting())
        await renderer.on_text_chunk("x")
        with pytest.raises(RpcError):
            await renderer.on_done("end_turn")
