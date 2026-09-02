"""Tests for kiro_crew.webex.renderer (WebexRenderer, Layer 2b)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.webex.client import WEBEX_MAX_TEXT
from kiro_crew.webex.renderer import _STATUS_EDIT_BUDGET, WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES


class FakeClient:
    """Records message sends, edits, and deletes."""

    def __init__(self, edit_ok: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []  # (conversation_id, markdown)
        #: Every send with its kwargs, for assertions about cards and threading.
        self.sent_full: list[tuple[str, str, dict]] = []
        self.edits: list[tuple[str, str, str]] = []  # (message_id, room_id, markdown)
        self.deleted: list[str] = []
        self.files: list[dict] = []
        self._edit_ok = edit_ok
        self._next_id = 0

    async def send_message(self, conversation_id: str, markdown: str, **kw) -> str:
        self.sent.append((conversation_id, markdown))
        self.sent_full.append((conversation_id, markdown, dict(kw)))
        self._next_id += 1
        return f"MSG{self._next_id}"

    async def send_file(self, conversation_id: str, markdown: str, **kw) -> str:
        self.files.append({"conversation_id": conversation_id, "markdown": markdown, **kw})
        self._next_id += 1
        return f"FILE{self._next_id}"

    async def edit_message(self, message_id: str, room_id: str, markdown: str) -> bool:
        self.edits.append((message_id, room_id, markdown))
        return self._edit_ok

    async def delete_message(self, message_id: str) -> None:
        self.deleted.append(message_id)


class ChoicePublisher:
    """Stands in for the dispatcher's ``LiveChoices``.

    The renderer renders an options card only when it has somewhere to publish
    what the card offered — a card whose press cannot be resolved is a row of
    inert buttons — so a test that wants the widget has to supply one.
    """

    def __init__(self) -> None:
        self.published: list[tuple[str, list[str]]] = []

    def __call__(self, nonce: str, choices: list[str]) -> None:
        self.published.append((nonce, list(choices)))


def _renderer(
    client: FakeClient,
    *,
    publisher: ChoicePublisher | None = None,
    thread_id: str = "",
    uploads_allowed: bool = True,
) -> WebexRenderer:
    return WebexRenderer(
        client,
        "ROOM",
        WEBEX_CAPABILITIES,
        thread_id=thread_id,
        uploads_allowed=uploads_allowed,
        publish_choices=publisher,
    )


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
    async def test_options_within_the_cap_render_as_a_card(self) -> None:
        """Choices become Adaptive Card buttons, and never vanish.

        The answer itself carries no numbered list when a card is rendered — the
        buttons ARE the list — but the card rides its own message, because Webex
        refuses to edit a message that carries an attachment and the answer needs
        its final edit.
        """
        c = FakeClient()
        pub = ChoicePublisher()
        r = _renderer(c, publisher=pub)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B | C]")
        await r.on_done()

        assert c.edits[-1][2] == "Pick one"
        card_sends = [kw for (_, _, kw) in c.sent_full if kw.get("attachments")]
        assert len(card_sends) == 1
        actions = card_sends[0]["attachments"][0]["content"]["actions"]
        assert [a["title"] for a in actions] == ["A", "B", "C"]

    @pytest.mark.asyncio
    async def test_a_delimiter_split_credential_is_redacted(self) -> None:
        """The driver's byte scan cannot see this one.

        ``AKIA**IOSF**ODNN7EXAMPLE`` survives a byte-for-byte scan and is then
        reassembled whole by Webex's own markdown renderer, so the answer is
        scanned in its DISPLAYED form as well.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("token AKIA**IOSF**ODNN7EXAMPLE ok")
        await r.on_done()

        delivered = c.edits[-1][2]
        assert "AKIAIOSFODNN7EXAMPLE" not in delivered.replace("*", "")
        assert "REDACTED" in delivered

    @pytest.mark.asyncio
    async def test_a_status_frame_is_scanned_too(self) -> None:
        """The frame carries the FORMING answer's tail.

        So a delimiter-split credential reaches the room here first — minutes
        before ``on_done`` would have caught it.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("token AKIA**IOSF**ODNN7EXAMPLE")
        # The throttle window opened at on_turn_start; a status frame is a paced
        # edit, so reopening it is what lets one render inside the test.
        r._last_status = 0.0
        await r.on_tool_call("t2", "fs_write")

        frames = [body for (_mid, _room, body) in c.edits]
        assert frames, "no status frame was rendered"
        assert not any("AKIAIOSFODNN7EXAMPLE" in f.replace("*", "") for f in frames)

    @pytest.mark.asyncio
    async def test_an_email_address_is_not_defanged(self) -> None:
        """Webex has no ``@everyone``-style broadcast grammar.

        The shared ``display_safe`` inserts a zero-width space after every ``@``
        to neutralize those; doing that here would mangle every address the agent
        prints — on the one channel whose allow-list IS email addresses.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("ask kyle@example.com")
        await r.on_done()

        assert "kyle@example.com" in c.edits[-1][2]

    @pytest.mark.asyncio
    async def test_a_failed_upload_puts_its_reference_back(self) -> None:
        """Extraction already removed the markup from the answer.

        So a silently failed upload leaves the user with neither the image nor any
        hint that one was meant to be there.
        """
        c = FakeClient()
        r = _renderer(c)
        r.authorize_upload_root("/tmp")

        async def _no_upload(*_a, **_kw):
            return None

        c.send_file = _no_upload  # type: ignore[method-assign]
        item = SimpleNamespace(path="/tmp/chart.png", alt="chart", data=b"x", mime="image/png")

        await r._report_failed_uploads(await r._send_uploads([item]))

        bodies = [m for (_conv, m) in c.sent]
        assert any("/tmp/chart.png" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_a_successful_upload_reports_nothing(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        item = SimpleNamespace(path="/tmp/chart.png", alt="chart", data=b"x", mime="image/png")

        failed = await r._send_uploads([item])
        await r._report_failed_uploads(failed)

        assert failed == []
        assert not any("Couldn't upload" in m for (_conv, m) in c.sent)

    @pytest.mark.asyncio
    async def test_a_credential_in_the_upload_caption_is_redacted(self) -> None:
        """The caption is the upload message's own text and ``alt`` is model-authored.

        A delimiter-split credential in the alt survives the byte scan on the
        answer body (these files were extracted OUT of it) and is whole once
        Webex renders the markup away, so the caption gets the same display-form
        floor every other outbound Webex text passes through.
        """
        c = FakeClient()
        r = _renderer(c)
        item = SimpleNamespace(
            path="/tmp/chart.png", alt="AKIA**IOSF**ODNN7EXAMPLE", data=b"x", mime="image/png"
        )

        await r._send_uploads([item])

        caption = c.files[-1]["markdown"]
        assert "AKIAIOSFODNN7EXAMPLE" not in caption
        assert "REDACTED" in caption

    @pytest.mark.asyncio
    async def test_the_upload_filename_is_derived_not_trusted(self) -> None:
        """Defence in depth on the name Webex labels the attachment with.

        The stream redactor mangles a clean credential in the answer text before
        extraction ever resolves the path, so this is the backstop rather than the
        live hole: the shared helper re-scans the name it derived and replaces one
        that still reads as a secret, instead of the room getting it as a label.
        """
        c = FakeClient()
        r = _renderer(c)
        item = SimpleNamespace(
            path="/tmp/AKIAIOSFODNN7EXAMPLE.png", alt="chart", data=b"x", mime="image/png"
        )

        await r._send_uploads([item])

        assert "AKIAIOSFODNN7EXAMPLE" not in c.files[-1]["filename"]

    @pytest.mark.asyncio
    async def test_the_upload_extension_follows_the_sniffed_mime(self) -> None:
        """The reachable half: extraction admits a file on its BYTES alone.

        It never reconciles the sniff with the path's own suffix, so without the
        shared helper the sent name can disagree with the bytes every gate actually
        checked — a part typed ``image/gif`` labelled ``.exe``.
        """
        c = FakeClient()
        r = _renderer(c)
        item = SimpleNamespace(path="/tmp/chart.exe", alt="chart", data=b"x", mime="image/png")

        await r._send_uploads([item])

        assert c.files[-1]["filename"] == "chart.png"

    @pytest.mark.asyncio
    async def test_a_failed_card_send_falls_back_to_numbered_text(self) -> None:
        """The card carries the ONLY copy of the kept choices.

        The answer text has them stripped precisely so widget and text do not
        duplicate one list — so a card that fails to post would otherwise leave
        the user reading a question with no visible answers.
        """
        c = FakeClient()
        pub = ChoicePublisher()
        r = _renderer(c, publisher=pub)

        real_send = c.send_message

        async def _fail_the_card(conv, markdown, **kw):
            if kw.get("attachments"):
                return None
            return await real_send(conv, markdown, **kw)

        c.send_message = _fail_the_card  # type: ignore[method-assign]

        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B]")
        await r.on_done()

        bodies = [m for (_conv, m) in c.sent]
        assert any("1. A" in b and "2. B" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_the_choices_appear_once_and_on_the_card_message(self) -> None:
        """The ANSWER never repeats them; the card's own message carries them.

        Webex requires text alongside an attachment and that text is what a client
        which cannot render Adaptive Cards receives, so the numbered list rides the
        card message as its fallback — while the answer keeps them stripped, which
        is what stops one list being shown twice.
        """
        c = FakeClient()
        r = _renderer(c, publisher=ChoicePublisher())

        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B]")
        await r.on_done()

        assert c.edits[-1][2] == "Pick one"
        with_choices = [m for (_conv, m) in c.sent if "1. A" in m and "2. B" in m]
        assert len(with_choices) == 1
        card_bodies = [m for (_c, m, kw) in c.sent_full if kw.get("attachments")]
        assert card_bodies == with_choices

    @pytest.mark.asyncio
    async def test_options_past_the_cap_overflow_into_numbered_text(self) -> None:
        """Widget and text form ONE list, continuing the button slots.

        The shared ``apply_options_cap`` contract every widget channel follows:
        the first ``max_buttons`` choices become buttons and the remainder is
        numbered from there, so nothing is hidden and nothing is duplicated.
        """
        c = FakeClient()
        pub = ChoicePublisher()
        r = _renderer(c, publisher=pub)
        n = WEBEX_CAPABILITIES.max_buttons
        trailer = " | ".join(f"C{i}" for i in range(n + 3))
        await r.on_turn_start()
        await r.on_text_chunk(f"Pick one\n\n[OPTIONS: {trailer}]")
        await r.on_done()

        card = next(kw for (_, _, kw) in c.sent_full if kw.get("attachments"))
        actions = card["attachments"][0]["content"]["actions"]
        assert [a["title"] for a in actions] == [f"C{i}" for i in range(n)]
        # The overflow is numbered CONTINUING the widget slots, so the first text
        # entry is n+1 rather than 1.
        final = c.edits[-1][2]
        for offset in range(3):
            assert f"{n + offset + 1}. C{n + offset}" in final

    @pytest.mark.asyncio
    async def test_an_unterminated_options_fragment_survives_in_the_answer(self) -> None:
        """By ``on_done`` the buffer is SEALED, so the tail is the model's prose.

        This renderer buffers a whole turn and sends once, so an unfinished
        ``[OPTIONS`` at the end is not a marker mid-flight — it is text the
        assistant wrote, and cutting it is permanent data loss. Same trade the
        shared ``render_options_as_text`` makes for the other buffer-and-send
        channels; WeCom, which really does stream, hides it instead.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B")
        await r.on_done()
        assert c.edits[-1][2] == "Pick one\n\n[OPTIONS: A | B"

    @pytest.mark.asyncio
    async def test_a_status_frame_still_hides_an_unterminated_fragment(self) -> None:
        """The one surface where the other reading holds.

        A status frame renders text that is still arriving, so a partial marker
        there may genuinely be one mid-flight, and showing reserved protocol as
        raw text — even for a single frame — is what this hides. Costless: the
        next frame and the sealed answer both re-render from the buffer.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_text_chunk("Pick one\n\n[OPTIONS: A | B")

        assert r.text() == "Pick one"

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
        for i in range(_STATUS_EDIT_BUDGET + 5):
            r._last_status = 0.0  # bypass throttle
            await r.on_tool_call(f"t{i}", f"tool_{i}")
        status_edits = [m for (_, _, m) in c.edits if m.startswith("🔧")]
        assert len(status_edits) == _STATUS_EDIT_BUDGET

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
        for i in range(_STATUS_EDIT_BUDGET):
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
        chunks = [m for m in followups if not m.startswith("⚠️")]
        assert len(chunks) == 1  # second follow-up chunk never attempted
        # The truncation is ANNOUNCED rather than silent: the answer stops
        # mid-sentence, and anything posted after it (an upload, a choice card)
        # would otherwise read as attached to a reply the user thinks is whole.
        assert any("could not be delivered" in m for m in followups)


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
    async def test_prompt_choice_posts_its_own_message_and_spends_no_edit(self) -> None:
        """The approval prompt must not touch the answer placeholder.

        Its edits are the scarcest resource on this channel (10 per message, one
        reserved for the final answer), and a prompt has to survive as readable
        history after the turn ends.
        """
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_tool_call("t1", "fs_write")
        sent_before, edits_before = len(c.sent), len(c.edits)
        await r.on_prompt_choice([{"label": "yes"}], "rq")
        assert len(c.sent) == sent_before + 1
        assert len(c.edits) == edits_before  # placeholder untouched
        prompt = c.sent[-1][1]
        assert "fs_write" in prompt and "1" in prompt and "2" in prompt

    @pytest.mark.asyncio
    async def test_thinking_is_noop(self) -> None:
        c = FakeClient()
        r = _renderer(c)
        await r.on_turn_start()
        await r.on_thinking("pondering")
        assert len(c.sent) == 1 and c.edits == []
