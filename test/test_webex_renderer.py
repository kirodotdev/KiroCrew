"""Tests for kiro_crew.webex.renderer (WebexRenderer, Layer 2b)."""

from __future__ import annotations

import pytest

from kiro_crew.webex.client import WEBEX_MAX_TEXT
from kiro_crew.webex.renderer import _TOOL_EDIT_BUDGET, WebexRenderer, _strip_options
from kiro_crew.webex.transport import WEBEX_CAPABILITIES


class TestStripOptionsRedos:
    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): the old lazy ``\s*(.*?)`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. Webex now shares the tempered
        # OPTIONS_RE_TRAILER (forbids only a re-occurring "[OPTIONS:"), so the
        # body is unambiguous (linear). A whitespace-padded unterminated tag and
        # many repeated "[OPTIONS:" prefixes (the real pump) must both return
        # promptly.
        import time

        evil = "[OPTIONS:" + ("\t" * 200_000) + "x"
        start = time.perf_counter()
        result = _strip_options(evil)
        assert time.perf_counter() - start < 1.0, "possible ReDoS"
        assert result == ""

        evil = "[OPTIONS:" * 100_000 + "x"
        start = time.perf_counter()
        _strip_options(evil)
        assert time.perf_counter() - start < 1.0, "possible ReDoS"


class FakeClient:
    """Records message sends, edits, and deletes."""

    def __init__(self, edit_ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []  # (conversation_id, markdown)
        self.edits: list[tuple[str, str, str]] = []  # (message_id, room_id, markdown)
        self.deleted: list[str] = []
        self._edit_ok = edit_ok
        self._next_id = 0

    async def send_message(self, conversation_id: str, markdown: str, **kw) -> str:
        self.sent.append((conversation_id, markdown))
        self._next_id += 1
        return f"MSG{self._next_id}"

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        self.edits.append((message_id, room_id, markdown))
        return self._edit_ok

    async def delete_message(self, message_id: str) -> None:
        self.deleted.append(message_id)


def _renderer(client: FakeClient) -> WebexRenderer:
    return WebexRenderer(client, "ROOM", WEBEX_CAPABILITIES)


class TestPlaceholder:
    @pytest.mark.asyncio
    async def test_turn_start_posts_placeholder(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        assert c.sent == [("ROOM", "🤔 Thinking…")]

    @pytest.mark.asyncio
    async def test_turn_start_idempotent(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_turn_start()  # second call no-ops
        assert len(c.sent) == 1


class TestFinalAnswer:
    @pytest.mark.asyncio
    async def test_final_answer_edits_placeholder(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Hello ")
        await r.on_text_chunk("world")
        await r.on_done()
        assert c.edits == [("MSG1", "ROOM", "Hello world")]
        assert len(c.sent) == 1  # only the placeholder was posted

    @pytest.mark.asyncio
    async def test_edit_failure_falls_back_to_new_message(self) -> None:
        c = FakeClient(edit_ok=False)
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("answer")
        await r.on_done()
        # New message posted with the answer, stale placeholder deleted.
        assert ("ROOM", "answer") in c.sent
        assert c.deleted == ["MSG1"]

    @pytest.mark.asyncio
    async def test_long_answer_chunked_as_followups(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("x" * (WEBEX_MAX_TEXT + 100))
        await r.on_done()
        # First chunk via edit, overflow as a follow-up message.
        assert len(c.edits) == 1
        followups = [m for (conv, m) in c.sent if conv == "ROOM" and m != "🤔 Thinking…"]
        assert followups == ["x" * 100]

    @pytest.mark.asyncio
    async def test_multibyte_answer_split_losslessly(self) -> None:
        """A multibyte reply under the char cap but over the BYTE cap must be
        split (byte-aware), not silently tail-truncated by the send path."""
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        text = "🐾" * 3000  # 12000 bytes, only 3000 chars
        await r.on_text_chunk(text)
        await r.on_done()
        delivered = c.edits[0][2] + "".join(
            m for (conv, m) in c.sent if conv == "ROOM" and m != "🤔 Thinking…"
        )
        assert delivered == text  # nothing lost

    @pytest.mark.asyncio
    async def test_options_trailer_stripped(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B | C]")
        await r.on_done()
        assert c.edits[-1][2] == "Pick one"

    @pytest.mark.asyncio
    async def test_error_done_shows_error_text(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_done(stop_reason="error")
        assert "⚠️" in c.edits[-1][2]


class TestToolStatus:
    @pytest.mark.asyncio
    async def test_tool_call_edits_placeholder(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        r._last_status = 0.0  # bypass throttle for the test
        await r.on_tool_call("t1", "fs_read", tool_kind="read")
        assert any("🔧 Running: fs_read" in m for (_, _, m) in c.edits)

    @pytest.mark.asyncio
    async def test_tool_edits_respect_budget(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        for i in range(_TOOL_EDIT_BUDGET + 5):
            r._last_status = 0.0  # bypass throttle
            await r.on_tool_call(f"t{i}", f"tool_{i}")
        status_edits = [m for (_, _, m) in c.edits if m.startswith("🔧")]
        assert len(status_edits) == _TOOL_EDIT_BUDGET

    @pytest.mark.asyncio
    async def test_tool_edit_failure_burns_budget(self) -> None:
        c = FakeClient(edit_ok=False)
        r = _renderer(c)
        await r.on_turn_start()
        r._last_status = 0.0
        await r.on_tool_call("t1", "tool_a")
        r._last_status = 0.0
        await r.on_tool_call("t2", "tool_b")  # budget burned -> no second edit
        status_edits = [m for (_, _, m) in c.edits if m.startswith("🔧")]
        assert len(status_edits) == 1

    @pytest.mark.asyncio
    async def test_final_answer_survives_exhausted_tool_budget(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        for i in range(_TOOL_EDIT_BUDGET):
            r._last_status = 0.0
            await r.on_tool_call(f"t{i}", f"tool_{i}")
        await r.on_text_chunk("final answer")
        await r.on_done()
        assert c.edits[-1][2] == "final answer"


class TestDeliveryFailure:
    @pytest.mark.asyncio
    async def test_first_chunk_failure_suppresses_followups(self) -> None:
        """If both the placeholder edit and the fallback send fail, the
        follow-up chunks must NOT be posted — a response that starts
        mid-answer is worse than no response."""

        class DeadClient(FakeClient):
            def __init__(self) -> None:
                super().__init__(edit_ok=False)

            async def send_message(self, conversation_id: str, markdown: str, **kw):
                self.sent.append((conversation_id, markdown))
                return None  # every send fails

        c = DeadClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("x" * (WEBEX_MAX_TEXT + 100))
        await r.on_done()
        # Only the placeholder attempt + ONE first-chunk send attempt — the
        # 100-char follow-up was never attempted.
        bodies = [m for (_, m) in c.sent]
        assert "x" * 100 not in bodies

    @pytest.mark.asyncio
    async def test_midsequence_failure_stops_remaining_chunks(self) -> None:
        """A failed follow-up send stops the sequence so the delivered prefix
        stays coherent (no spliced gap in the middle of the answer)."""

        class FlakyClient(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self._sends = 0

            async def send_message(self, conversation_id: str, markdown: str, **kw):
                self._sends += 1
                self.sent.append((conversation_id, markdown))
                if self._sends >= 2 and markdown != "🤔 Thinking…":
                    return None  # follow-up sends fail
                self._next_id += 1
                return f"MSG{self._next_id}"

        c = FlakyClient()
        r = _renderer(c)
        await r.on_turn_start()
        # 3 chunks: first via edit, then two follow-ups; the first follow-up fails.
        await r.on_text_chunk("x" * (2 * WEBEX_MAX_TEXT + 100))
        await r.on_done()
        followups = [m for (conv, m) in c.sent if conv == "ROOM" and m != "🤔 Thinking…"]
        assert len(followups) == 1  # second follow-up never attempted


class TestClose:
    @pytest.mark.asyncio
    async def test_close_after_done_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("done text")
        await r.on_done()
        edits_before = len(c.edits)
        await r.close()
        assert len(c.edits) == edits_before

    @pytest.mark.asyncio
    async def test_close_without_done_finalizes(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("partial")
        await r.close()  # turn never reached on_done (e.g. cold-start failure)
        assert c.edits[-1][2] == "partial"


class TestNoOps:
    @pytest.mark.asyncio
    async def test_prompt_choice_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        before = (len(c.sent), len(c.edits))
        await r.on_prompt_choice([{"label": "yes"}], "rq")  # Webex has no buttons
        assert (len(c.sent), len(c.edits)) == before

    @pytest.mark.asyncio
    async def test_thinking_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_thinking("pondering")
        assert len(c.sent) == 1 and c.edits == []
