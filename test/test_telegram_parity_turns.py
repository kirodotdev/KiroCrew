"""What a Telegram turn sends back, and what it leaves behind.

The duration and context footer, which message a turn quotes, a splitter that
invents no fence, spoken replies (the length floor, the settings, the voice note,
the toggle), the generated title and the fallback that yields to it, the
compaction reinjection flag a turn consumes and puts back, and the traffic
counters ``/status`` reads. The doubles come from ``test_telegram`` and the shared
messages and renderer from ``telegram_parity_helpers.py``.
"""

from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from telegram_parity_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _drop_live_config_snapshot,
    _forum_msg,
    _msg,
    _renderer,
)
from test_telegram import FakeClient, FakeProvider, _dispatcher, _dm, _Ev, _origin, _prime_live

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramInboundMessage


class TestTurnFooter:
    """Duration and the context gauge — shown only when either is actionable."""

    def _renderer_with_ctx(self, pct):
        renderer, client = _renderer()
        renderer.attach_context_client(SimpleNamespace(context_usage_pct=lambda: pct))
        return renderer, client

    def test_a_fast_low_context_turn_gets_no_footer(self) -> None:
        # A footer under every reply is one the reader learns to skip — including
        # on the turn where the context warning finally matters.
        renderer, _ = self._renderer_with_ctx(4)
        assert renderer._turn_footer() == ""

    def test_a_slow_turn_reports_its_duration(self) -> None:
        import time as _time

        renderer, _ = self._renderer_with_ctx(4)
        # flake-ok: pre-existing real monotonic stamp 95 s back; the footer text changes only if a second passes between the two reads, moved verbatim; tracked for a de-flake pass
        renderer._turn_started = _time.monotonic() - 95
        footer = renderer._turn_footer()
        assert "Finished in 1m 35s" in footer and "ctx 4%" in footer

    @pytest.mark.parametrize("pct,icon", [(50, "🟠"), (72, "🔴"), (55, "🟠")])
    def test_a_high_context_reading_reports_itself_however_fast_the_turn(
        self, pct: int, icon: str
    ) -> None:
        renderer, _ = self._renderer_with_ctx(pct)
        footer = renderer._turn_footer()
        assert icon in footer and f"ctx {pct}%" in footer

    def test_an_unreadable_context_client_still_reports_a_slow_turn(self) -> None:
        import time as _time

        def _boom() -> float:
            raise RuntimeError("no session")

        renderer, _ = _renderer()
        renderer.attach_context_client(SimpleNamespace(context_usage_pct=_boom))
        # flake-ok: pre-existing real monotonic stamp 30 s back; the footer text changes only if a second passes between the two reads, moved verbatim; tracked for a de-flake pass
        renderer._turn_started = _time.monotonic() - 30
        assert renderer._turn_footer() == "Finished in 30s"

    def test_no_context_client_at_all_still_reports_a_slow_turn(self) -> None:
        import time as _time

        renderer, _ = _renderer()
        # flake-ok: pre-existing real monotonic stamp 30 s back; the footer text changes only if a second passes between the two reads, moved verbatim; tracked for a de-flake pass
        renderer._turn_started = _time.monotonic() - 30
        assert renderer._turn_footer() == "Finished in 30s"

    @pytest.mark.asyncio
    async def test_the_footer_rides_the_answer_rather_than_a_second_message(self) -> None:
        renderer, client = self._renderer_with_ctx(80)
        renderer._buf = ["The answer."]
        await renderer.on_done()
        landed = [t for t, _ in client.sent] + [t for _, t, _ in client.edits]
        assert any("The answer." in t and "ctx 80%" in t for t in landed)
        # One bubble: a second would cost a notification and a rate-limit slot the
        # answer itself needs.
        assert not [t for t in landed if "ctx 80%" in t and "The answer." not in t]


class TestReplyThreading:
    """A turn's FIRST outbound quotes the message it answers — where that helps.

    Slack attaches every answer to its trigger because a thread is Slack's unit of
    conversation. Telegram has no unit below the forum Topic, so the target is
    chosen rather than applied unconditionally: a quote block above every reply in
    a 1:1 DM is chrome with no information in it.
    """

    def test_a_live_dm_turn_gets_no_quote(self) -> None:
        from kiro_crew.telegram.transport import TelegramInboundMessage
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        msg = TelegramInboundMessage(
            channel_type="telegram",
            user_id="1",
            conversation_id="1",
            text="hi",
            message_id=42,
            chat_type="private",
        )
        assert TelegramDispatcher._reply_target(msg, interpret_commands=True) is None

    def test_a_forum_topic_turn_quotes_the_question(self) -> None:
        # Several allow-listed participants can be talking at once, so a flat
        # answer belongs to nobody in particular.
        from kiro_crew.telegram.transport import TelegramInboundMessage
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        msg = TelegramInboundMessage(
            channel_type="telegram",
            user_id="1",
            conversation_id="-100123",
            text="hi",
            message_id=42,
            chat_type="supergroup",
            thread_id="9",
        )
        assert TelegramDispatcher._reply_target(msg, interpret_commands=True) == 42

    def test_a_drained_queue_turn_quotes_even_in_a_dm(self) -> None:
        # It is answered after the turn that was already running, so it lands well
        # below the message it answers. interpret_commands=False is the marker the
        # drain path passes.
        from kiro_crew.telegram.transport import TelegramInboundMessage
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        msg = TelegramInboundMessage(
            channel_type="telegram",
            user_id="1",
            conversation_id="1",
            text="hi",
            message_id=42,
            chat_type="private",
        )
        assert TelegramDispatcher._reply_target(msg, interpret_commands=False) == 42

    def test_a_missing_message_id_is_no_target_rather_than_zero(self) -> None:
        from kiro_crew.messaging.transport import InboundMessage
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        bare = InboundMessage(
            channel_type="telegram", user_id="1", conversation_id="-100123", text="hi"
        )
        assert TelegramDispatcher._reply_target(bare, interpret_commands=False) is None

    @pytest.mark.asyncio
    async def test_only_the_first_send_of_a_turn_carries_it(self) -> None:
        # Every later bubble is attached by adjacency; quoting each one would
        # triple the visual weight of a multi-part answer for no added information.
        client = FakeClient()
        r = TelegramRenderer(
            client,  # type: ignore[arg-type]
            1,
            TELEGRAM_CAPABILITIES,
            reply_to_message_id=42,
        )
        assert r._consume_reply_to() == 42
        assert r._consume_reply_to() is None

    @pytest.mark.asyncio
    async def test_the_streamed_opener_quotes_and_its_edits_do_not(self) -> None:
        client = FakeClient()
        r = TelegramRenderer(
            client,  # type: ignore[arg-type]
            1,
            TELEGRAM_CAPABILITIES,
            reply_to_message_id=42,
        )
        await r.on_text_chunk("first frame of the answer")
        await r._stream_live(force=True)
        assert client.reply_targets and client.reply_targets[0] == 42
        # The second frame is an EDIT of the same message, which carries no reply
        # target at all — so the only thing to assert is that no further send
        # spent one.
        await r.on_text_chunk(" and more text arriving after it")
        await r._stream_live(force=True)
        assert [t for t in client.reply_targets if t] == [42]


class TestSplitterConvergence:
    """Telegram's splitter does not fabricate fence delimiters.

    The channel-local predecessor rebalanced by counting backticks
    (``ch.count("```") % 2``), which is not the fence grammar. On a
    ````-delimited block containing a bare ``` line the parity flips, and the
    state stays inverted for the rest of the message. ``_split_markdown`` now
    delegates to the shared ``split_markdown_safe`` — the same implementation
    Discord uses — which tracks run length and info string.
    """

    #: A 4-backtick block whose body contains a bare 3-backtick line. This is the
    #: exact input the parity counter gets wrong, and it is ordinary content: a
    #: ````-fenced block is how anyone quotes markdown that itself contains fences.
    _BUDGET = 400

    def _payload(self) -> str:
        body = "\n".join(f"line {i:03d} " + "a" * 40 for i in range(30))
        return f"intro\n\n````diff\n{body[:400]}\n```\n{body[400:]}\n````\n\ntail prose"

    def test_no_chunk_invents_a_closer_the_source_never_had(self) -> None:
        from kiro_crew.telegram.renderer import _split_markdown

        chunks = _split_markdown(self._payload(), self._BUDGET)
        assert len(chunks) > 1, "the payload must actually split for this to mean anything"
        # A 3-backtick run appearing as the LAST line of a chunk would be the
        # fabricated closer: it cannot close a 4-backtick fence, so it renders as
        # literal text and inverts every chunk after it.
        bad = [c for c in chunks if c.rstrip().endswith("```") and not c.rstrip().endswith("````")]
        assert not bad, f"a 3-backtick closer was synthesized for a 4-backtick fence: {bad}"

    def test_every_continuation_reopens_with_the_original_opener(self) -> None:
        # The parity rebalancer reopened with a bare ``` — losing both the run
        # length and the `diff` info string, so the continuation lost its
        # highlighting and stopped matching its own closer.
        from kiro_crew.telegram.renderer import _split_markdown

        chunks = _split_markdown(self._payload(), self._BUDGET)
        reopened = [c for c in chunks[1:] if c.startswith("`")]
        assert reopened, "expected at least one continuation inside the fence"
        assert all(c.startswith("````diff") for c in reopened), reopened

    def test_the_content_survives_the_round_trip(self) -> None:
        # Whatever the delimiters do, no authored character may be dropped.
        from kiro_crew.telegram.renderer import _split_markdown

        payload = self._payload()
        chunks = _split_markdown(payload, self._BUDGET)
        rejoined = "".join(chunks)
        for token in ("intro", "line 000", "line 029", "tail prose"):
            assert token in rejoined, f"{token!r} lost while splitting"

    def test_a_plain_three_backtick_block_still_balances(self) -> None:
        # The common case must not regress: each chunk is self-contained markdown,
        # so the per-chunk HTML pass wraps it in <pre> instead of leaking a fence.
        from kiro_crew.telegram.renderer import _md_to_telegram_html, _split_markdown

        code = "\n".join(f"row <{i}> & 'v'" for i in range(200))
        chunks = _split_markdown(f"code:\n\n```python\n{code}\n```\n\ndone", 400)
        assert len(chunks) > 1
        htmls = [_md_to_telegram_html(c) for c in chunks]
        assert all("```" not in h for h in htmls), "a literal fence leaked into the HTML"

    def test_it_is_the_shared_implementation_not_a_copy(self) -> None:
        # The point of the change is one splitter, not two that agree today. A
        # channel-local reimplementation would drift the moment the shared
        # contract gains a rule.
        from kiro_crew.messaging.split import split_markdown_safe
        from kiro_crew.telegram.renderer import _split_markdown

        payload = self._payload()
        assert _split_markdown(payload, self._BUDGET) == split_markdown_safe(payload, self._BUDGET)


class TestVoiceOut:
    """Telegram speaks its answers, closing the loop it already had one half of.

    The channel has transcribed inbound voice notes since before this change
    (``messaging/attachments.transcribe_audio_attachments``), so a user could talk
    to it and only ever be answered in text. Slack closes exactly that asymmetry
    with ``auto_reply_to_voice``; this is the same capability, opted into per
    conversation.
    """

    def test_off_by_default(self) -> None:
        d, _, _ = _dispatcher({1})
        assert d._voice_enabled(("direct", "1")) is False

    @pytest.mark.asyncio
    async def test_a_markup_split_credential_is_never_synthesized(self) -> None:
        """Audio is an egress a reader cannot un-see, so it gets the display floor.

        This leg bypasses the renderer, which is where a turn normally gets that
        floor, and the driver's pass is BYTE-level: `AKIA**IOSFODNN7EXAMPLE**` does
        not match, because the `**` sits inside the key. A synthesizer reads the
        characters and not the markup, so the credential the byte pass missed would
        be spoken aloud.
        """
        d, _, _ = _dispatcher({1})
        head, tail = "AKIA", "IOSFODNN7EXAMPLE"
        spoken: list[str] = []

        # Real signature: (deliver, text, **settings). A mismatch here is swallowed
        # by the handler's own `except Exception`, which is why the assertion below
        # requires the synthesizer to have been REACHED rather than only checking
        # what it saw.
        async def _capture(deliver, text, **_settings):  # noqa: ANN001 - test stub
            spoken.append(text)
            return True

        with patch("kiro_crew.telegram.transport_dispatch.synthesize_and_deliver", _capture):
            await d._speak_reply(
                ("direct", "1"), 1, f"Here is the key {head}**{tail}** for the build.", None
            )

        assert spoken, "the synthesizer must have been reached"
        said = spoken[0]
        assert head + tail not in said, "the contiguous key must not reach the synthesizer"
        assert tail not in said, "the second half must not survive either"
        assert "REDACTED" in said

    def test_the_config_default_reaches_a_conversation_with_no_override(self) -> None:
        # Absent rather than pre-seeded, so changing telegram.voice_replies reaches
        # every conversation the operator has not overridden.
        d, _, _ = _dispatcher({1})
        d.cfg.telegram.voice_replies = True
        _prime_live(d.cfg)
        assert d._voice_enabled(("direct", "1")) is True

    def test_an_explicit_off_beats_a_configured_on(self) -> None:
        d, _, _ = _dispatcher({1})
        d.cfg.telegram.voice_replies = True
        _prime_live(d.cfg)
        d._voice_pref[("direct", "1")] = False
        assert d._voice_enabled(("direct", "1")) is False

    @pytest.mark.asyncio
    async def test_a_bare_voice_command_reports_rather_than_toggles(self) -> None:
        # A toggle whose direction depends on state the user cannot see is how you
        # turn voice ON in a room where you wanted it off.
        d, client, _ = _dispatcher({1})
        await d._handle_voice(("direct", "1"), 1, "", None)
        assert ("direct", "1") not in d._voice_pref
        assert "off" in client.sent[-1][0]

    @pytest.mark.asyncio
    async def test_on_and_off_set_the_route_preference(self) -> None:
        d, _, _ = _dispatcher({1})
        await d._handle_voice(("direct", "1"), 1, "on", None)
        assert d._voice_pref[("direct", "1")] is True
        await d._handle_voice(("direct", "1"), 1, "OFF", None)
        assert d._voice_pref[("direct", "1")] is False

    @pytest.mark.asyncio
    async def test_a_short_answer_is_not_spoken(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        d, _, _ = _dispatcher({1})
        called: list[str] = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "synthesize_and_deliver", lambda *a, **k: called.append("synth") or True)
            await d._speak_reply(("direct", "1"), 1, "Done.", None)
        assert not called, "speaking a 4-character answer costs a message to say less"

    @pytest.mark.asyncio
    async def test_a_tts_failure_never_raises_out_of_the_turn(self) -> None:
        # The text answer has already landed. A TTS problem must not surface as a
        # failed turn or re-post anything.
        from kiro_crew.telegram import transport_dispatch as td

        d, client, _ = _dispatcher({1})

        async def _boom(*_a: Any, **_k: Any) -> bool:
            raise RuntimeError("speech engine not installed")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "synthesize_and_deliver", _boom)
            await d._speak_reply(("direct", "1"), 1, "x" * 200, None)
        assert not client.sent, "a TTS failure must not post anything"

    def test_ogg_takes_the_native_voice_note_and_wav_does_not(self) -> None:
        # sendVoice REJECTS anything but OGG/Opus with a 400 rather than degrading,
        # and the system engine emits WAV — so the split is a Bot API constraint, not taste.
        from kiro_crew.telegram.client import TELEGRAM_VOICE_MIMES
        from kiro_crew.telegram.transport_dispatch import _audio_mime

        assert _audio_mime("/tmp/r.ogg") in TELEGRAM_VOICE_MIMES
        assert _audio_mime("/tmp/r.wav") not in TELEGRAM_VOICE_MIMES
        assert _audio_mime("/tmp/r.mp3") not in TELEGRAM_VOICE_MIMES
        assert _audio_mime("/tmp/r.unknown") == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_an_oversize_upload_is_refused_rather_than_413d(self) -> None:
        from kiro_crew.telegram.client import TELEGRAM_MAX_AUDIO_BYTES, TelegramClient

        client = TelegramClient(token="1:tok")
        sent: list = []

        async def _never(*a: Any, **k: Any) -> Any:
            sent.append(a)
            return {"message_id": 1}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, "_api_multipart", _never)
            mid = await client.send_voice(
                1,
                b"x" * (TELEGRAM_MAX_AUDIO_BYTES + 1),
                filename="r.wav",
                mime="audio/wav",
            )
        assert mid is None and not sent

    @pytest.mark.asyncio
    async def test_the_voice_message_is_sent_silently(self) -> None:
        # The text answer already pinged; a second notification for one turn is what
        # makes a chat with voice on read as broken.
        from kiro_crew.telegram.client import TelegramClient

        client = TelegramClient(token="1:tok")
        seen: list[dict] = []

        async def _capture(method: str, params: dict, *a: Any, **k: Any) -> Any:
            seen.append({"method": method, **params})
            return {"message_id": 9}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(client, "_api_multipart", _capture)
            await client.send_voice(1, b"audio-bytes", filename="r.wav", mime="audio/wav")
        assert seen and seen[0]["method"] == "sendAudio"
        assert seen[0]["disable_notification"] is True

    def test_the_synthesis_settings_come_from_one_reader(self) -> None:
        # Two channels reading the voice_reply section with their own key lists is
        # how they end up honouring different settings.
        from kiro_crew.voice_reply import DEFAULT_PROVIDER, synthesis_settings

        assert synthesis_settings(None)["provider"] == DEFAULT_PROVIDER
        assert synthesis_settings({"provider": "ploly"})["provider"] == DEFAULT_PROVIDER


class TestAutoTitle:
    """A Telegram conversation gets an LLM-generated name, not a truncation.

    Before this, ``_persist_turn`` froze the title at the first forty characters of
    the first message and the only correction was a manual ``/title``. Slack has had
    the generated version since before the transport path existed — though on its
    OWN default path it had stopped firing too, which the shared hoist also fixes.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import auto_title, privacy_mode

        auto_title.reset()
        privacy_mode.reset()

    @pytest.mark.asyncio
    async def test_a_turn_with_a_reply_claims_and_spawns_once(self) -> None:
        from kiro_crew.messaging import auto_title

        d, _, _ = _dispatcher({7})
        calls: list[tuple] = []

        async def _fake(*a: Any, **k: Any) -> str:
            calls.append((a, k))
            return "Two Word Title"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(auto_title, "maybe_auto_title", _fake)
            await d.handle_message(_dm("what is the plan"))
            await d.handle_message(_dm("and the next step"))
            # flake-ok: pre-existing zero-delay yield before the assert, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
        assert len(calls) == 1, "the conversation is named once, not once per turn"
        assert calls[0][1]["source"] == "telegram"

    @pytest.mark.asyncio
    async def test_a_restricted_session_is_never_titled(self) -> None:
        # There is nothing to title: it persists no transcript, and generating a
        # name would send its content to a background turn.
        from kiro_crew.messaging import auto_title, privacy_mode

        d, _, _ = _dispatcher({7})
        privacy_mode.mark_incognito(d._session_key(("direct", "7")))
        calls: list = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(
                auto_title,
                "maybe_auto_title",
                lambda *a, **k: calls.append(1) or _done(""),
            )
            await d.handle_message(_dm("what is the plan"))
            # flake-ok: pre-existing zero-delay yield before the absence assert, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)
        assert not calls

    @pytest.mark.asyncio
    async def test_the_task_is_held_so_it_cannot_be_collected_mid_flight(self) -> None:
        # asyncio keeps only a WEAK reference to a bare create_task, so without a
        # strong set the generation can vanish and the conversation silently keeps
        # its truncated name.
        from kiro_crew.messaging import auto_title

        d, _, _ = _dispatcher({7})
        started = asyncio.Event()

        async def _slow(*a: Any, **k: Any) -> str:
            started.set()
            # flake-ok: pre-existing 50 ms stub title generation, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms stub title generation, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)
            return "Named"

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(auto_title, "maybe_auto_title", _slow)
            await d.handle_message(_dm("what is the plan"))
            # flake-ok: pre-existing unbounded Event.wait on the title task the turn starts itself, moved verbatim; tracked for a de-flake pass
            await started.wait()
            assert d._title_tasks, "the in-flight task must be strongly referenced"
            # flake-ok: pre-existing unbounded gather of the title tasks, moved verbatim; tracked for a de-flake pass
            await asyncio.gather(*list(d._title_tasks))
        assert not d._title_tasks, "a finished task must be discarded, not accumulated"


async def _done(value: str) -> str:
    """An already-resolved coroutine, for a monkeypatched async call site."""
    return value


def _arm_reinjection(sessions: Any) -> dict[str, Any]:
    """Give the session stand-in the real manager's one-shot flag surface.

    Returns the ledger the test reads: which keys were consumed, how often the
    flag was re-armed, and whether it is armed now.
    """
    ledger: dict[str, Any] = {"consumed": [], "marks": 0, "armed": True}

    def _consume(key: str) -> bool:
        ledger["consumed"].append(key)
        was = ledger["armed"]
        ledger["armed"] = False
        return was

    def _mark(key: str) -> None:
        ledger["marks"] += 1
        ledger["armed"] = True

    sessions.consume_needs_reinjection = _consume
    sessions.mark_needs_reinjection = _mark
    return ledger


class _DyingProvider(FakeProvider):
    """A provider whose very next turn fails before any text lands."""

    async def stream(self, message: str) -> Any:
        raise RuntimeError("provider fell over")
        yield  # pragma: no cover -- makes this an async generator


class TestCompactionReinjection:
    """The Telegram turn loop is its own copy, so it must consume the flag itself.

    ``session_compaction`` marks ``needs_reinjection`` after an in-place compaction
    dropped the session-start context. A turn loop that does not read it runs
    every turn after ``/compact`` without the skills index or the response-preferences
    block.
    """

    @pytest.mark.asyncio
    async def test_a_compacted_session_forwards_the_flag_to_build_message(self) -> None:
        d, _, sess = _dispatcher({7})
        ledger = _arm_reinjection(sess)
        await d.handle_message(_dm("hi"))
        call = d.ctx_builder.build_calls[-1]
        assert ledger["consumed"] == [call["key"]], "consumed under the key the turn runs as"
        assert call["needs_reinjection"] is True
        # Landed: consumed exactly once, and NOT put back.
        assert ledger["marks"] == 0 and ledger["armed"] is False

    @pytest.mark.asyncio
    async def test_a_session_stand_in_without_the_flag_gets_the_false_default(self) -> None:
        d, _, sess = _dispatcher({7})
        assert not hasattr(sess, "consume_needs_reinjection")
        await d.handle_message(_dm("hi"))
        assert d.ctx_builder.build_calls[-1]["needs_reinjection"] is False
        assert sess.successes, "the turn still ran"

    @pytest.mark.asyncio
    async def test_a_cancelled_consuming_turn_puts_the_flag_back(self) -> None:
        # A /stop completes the turn normally with stop_reason "cancelled", and
        # the backend drops that turn from its transcript -- the re-injected
        # context goes with it, so the flag must come back like a raised turn.
        d, _, sess = _dispatcher({7})
        ledger = _arm_reinjection(sess)

        class _Cancelled(FakeProvider):
            async def stream(self, message: str) -> Any:
                yield _Ev(EVENT_COMPLETE, stop_reason="cancelled")

        async def _cancelled(key: str, **kw: Any) -> Any:
            return _Cancelled(), True, False

        sess.get_or_create = _cancelled  # type: ignore[method-assign]
        await d.handle_message(_dm("hi"))
        assert d.ctx_builder.build_calls[-1]["needs_reinjection"] is True
        assert ledger["marks"] == 1 and ledger["armed"] is True

    @pytest.mark.asyncio
    async def test_a_failed_consuming_turn_puts_the_flag_back(self) -> None:
        # The flag is cleared BEFORE build_message; a provider error on that very
        # turn discards the prompt carrying the re-injected context. Without the
        # re-arm the session runs without it until the NEXT compaction -- the
        # contract the dashboard runner keeps in its finally, applied here.
        d, _, sess = _dispatcher({7})
        ledger = _arm_reinjection(sess)

        async def _dying(key: str, **kw: Any) -> Any:
            return _DyingProvider(), True, False

        sess.get_or_create = _dying  # type: ignore[method-assign]
        await d.handle_message(_dm("hi"))
        assert d.ctx_builder.build_calls[-1]["needs_reinjection"] is True
        assert sess.failures and not sess.successes
        assert ledger["marks"] == 1 and ledger["armed"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("status", "marks"), [("completed", 1), ("failed", 0)])
    async def test_a_backend_compaction_arms_the_flag_for_the_next_turn(
        self, status: str, marks: int
    ) -> None:
        # The backend compacted its own window mid-turn: the session-start context
        # is gone even though this turn landed, so the next turn must send it.
        d, _, sess = _dispatcher({7})
        ledger = _arm_reinjection(sess)
        ledger["armed"] = False

        class _Compacting(FakeProvider):
            async def stream(self, message: str) -> Any:
                yield _Ev(EVENT_TEXT_CHUNK, text="hi")
                yield _Ev(EVENT_COMPACTION_STATUS, text=status)
                yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

        async def _compacting(key: str, **kw: Any) -> Any:
            return _Compacting(), False, False

        sess.get_or_create = _compacting  # type: ignore[method-assign]
        await d.handle_message(_dm("hi"))
        assert sess.successes, "the turn landed"
        assert ledger["marks"] == marks and ledger["armed"] is bool(marks)

    @pytest.mark.asyncio
    async def test_a_backend_compaction_arms_the_flag_when_the_turn_then_dies(self) -> None:
        d, _, sess = _dispatcher({7})
        ledger = _arm_reinjection(sess)
        ledger["armed"] = False

        class _CompactThenDie(FakeProvider):
            async def stream(self, message: str) -> Any:
                yield _Ev(EVENT_COMPACTION_STATUS, text="completed")
                raise RuntimeError("provider fell over after compacting")

        async def _dying(key: str, **kw: Any) -> Any:
            return _CompactThenDie(), False, False

        sess.get_or_create = _dying  # type: ignore[method-assign]
        await d.handle_message(_dm("hi"))
        assert sess.failures and not sess.successes
        assert ledger["marks"] == 1 and ledger["armed"] is True


class TestFallbackTitleYieldsToAutoTitle:
    """The deterministic title must not pre-empt the generated one.

    Both write the same field and the fallback always wins the race, because it runs
    synchronously on the turn while the generated one arrives from a background turn.
    ``auto_title``'s transcript write is guarded on the record having NO title, so the
    fallback landing first does not merely downgrade the name: it makes auto-titling
    inert on this channel while still spending a background turn per conversation to
    produce a name nobody ever sees.
    """

    def setup_method(self) -> None:
        from kiro_crew.messaging import auto_title, privacy_mode

        auto_title.reset()
        privacy_mode.reset()

    def _log(self) -> tuple[Any, list[tuple[str, str]]]:
        titles: list[tuple[str, str]] = []
        log = SimpleNamespace(
            append=lambda *a, **k: None,
            set_title=lambda key, title: titles.append((key, title)),
            atomic_appends=lambda _key: nullcontext(),
        )
        return log, titles

    def test_a_claimed_session_gets_no_fallback_title(self) -> None:
        from kiro_crew.messaging import auto_title

        d, _, _ = _dispatcher({7})
        log, titles = self._log()
        d.conv_log = log  # type: ignore[assignment]
        key = "telegram:kirocrew:direct:7"
        assert auto_title.try_claim(key)
        d._persist_turn(key, "a very long first message that would be truncated", "hi", True, "a")
        assert titles == [], "a name is on its way; writing the truncation blocks it"

    def test_an_unclaimed_session_still_gets_the_fallback(self) -> None:
        # The fallback is not dead code: a turn that produced no text never claims,
        # and a restricted session never titles, so the truncation is still what
        # names those conversations.
        d, _, _ = _dispatcher({7})
        log, titles = self._log()
        d.conv_log = log  # type: ignore[assignment]
        key = "telegram:kirocrew:direct:7"
        d._persist_turn(key, "first message", "hi", True, "a")
        assert titles == [(key, "first message")]

    def test_a_released_claim_lets_the_fallback_back_in(self) -> None:
        # maybe_auto_title releases the claim on SKIP or failure, so the next
        # exchange can retry. Until it does, the conversation should not be stuck
        # unnamed forever.
        from kiro_crew.messaging import auto_title

        d, _, _ = _dispatcher({7})
        log, titles = self._log()
        d.conv_log = log  # type: ignore[assignment]
        key = "telegram:kirocrew:direct:7"
        auto_title.try_claim(key)
        auto_title.release_claim(key)
        d._persist_turn(key, "first message", "hi", True, "a")
        assert titles == [(key, "first message")]


class TestStatsCounters:
    """`/status` reports this channel's own traffic, not zero forever.

    `Stats` is a process-wide counter that Slack increments on every turn. Telegram
    was a READER only — it offers `/status`, which renders `Stats().summary()` — so a
    Telegram-only install saw "msgs 0 (ok 0 / fail 0)" no matter how long the bot had
    been answering, and `Stats.daily_report` said "no messages".
    """

    def test_a_served_turn_counts_received_and_success(self) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        d, _, _ = _dispatcher({7})
        seen: list[str] = []
        recorder = SimpleNamespace(
            inc_message_received=lambda: seen.append("received"),
            inc_message_success=lambda: seen.append("success"),
            inc_message_failed=lambda: seen.append("failed"),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "Stats", lambda: recorder)
            asyncio.run(d.handle_message(_dm("a question")))
        assert seen == ["received", "success"]

    def test_a_governance_denied_message_counts_nothing(self) -> None:
        # It never happened as far as the operator's own traffic figures go, and
        # counting it would make the failure ratio read as the bot's fault.
        from kiro_crew.telegram import transport_dispatch as td

        d, _, _ = _dispatcher({7})
        seen: list[str] = []
        recorder = SimpleNamespace(
            inc_message_received=lambda: seen.append("received"),
            inc_message_success=lambda: seen.append("success"),
            inc_message_failed=lambda: seen.append("failed"),
        )
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "Stats", lambda: recorder)
            mp.setattr(td, "channel_inbound_permitted", _false)
            asyncio.run(d.handle_message(_dm("a question")))
        assert seen == []


async def _false(_channel: str) -> bool:
    """A governance gate that denies."""
    return False


# ── Dispatch characterization: owner-level contracts a module split must keep ──


class _VoiceClient(FakeClient):
    """A client that also records ``sendVoice``."""

    def __init__(self) -> None:
        super().__init__()
        self.voice: list[tuple[Any, ...]] = []
        self.voice_mid: Any = 555
        self.voice_error: BaseException | None = None

    async def send_voice(
        self, chat_id: int, data: bytes, *, filename: str, mime: str, message_thread_id: Any = None
    ) -> Any:
        if self.voice_error is not None:
            raise self.voice_error
        self.voice.append((chat_id, data, filename, mime, message_thread_id))
        return self.voice_mid


class TestVoiceReplyCharacterization:
    """Spoken replies: the length floor, the settings, the delivery leg, the toggle."""

    @staticmethod
    def _voice_dispatcher() -> tuple[Any, _VoiceClient]:
        d, _cli, _sess = _dispatcher({1})
        client = _VoiceClient()
        d.client = client  # type: ignore[assignment]
        return d, client

    @staticmethod
    def _synth(seen: list, results: list, path: Any = None):  # type: ignore[no-untyped-def]
        async def _synthesize(deliver, text, **settings):  # noqa: ANN001 - test stub
            seen.append((text, settings))
            if path is None:
                return False
            results.append(await deliver(str(path)))
            return results[-1]

        return _synthesize

    @pytest.mark.asyncio
    async def test_the_floor_is_fifty_raw_characters(self, monkeypatch: Any) -> None:
        seen: list[Any] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.synthesize_and_deliver", self._synth(seen, [])
        )
        d, _client = self._voice_dispatcher()
        await d._speak_reply(("direct", "1"), 1, "x" * 49, None)
        assert seen == []
        await d._speak_reply(("direct", "1"), 1, "x" * 50, None)
        assert len(seen) == 1
        d.client = None
        await d._speak_reply(("direct", "1"), 1, "x" * 200, None)
        assert len(seen) == 1

    @pytest.mark.asyncio
    async def test_settings_come_from_the_boot_config(self, monkeypatch: Any) -> None:
        from kiro_crew.voice_reply import synthesis_settings

        seen: list[Any] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.synthesize_and_deliver", self._synth(seen, [])
        )
        d, _client = self._voice_dispatcher()
        section = {"provider": "polly", "voice_id": "Joanna"}
        for raw, expected in (
            ({"voice_reply": section}, synthesis_settings(section)),
            ("junk", synthesis_settings(None)),
            ({"voice_reply": "junk"}, synthesis_settings(None)),
        ):
            seen.clear()
            d.cfg.raw = raw
            await d._speak_reply(("direct", "1"), 1, "y" * 60, None)
            assert seen[0][1] == expected, raw

    @pytest.mark.asyncio
    async def test_the_file_is_sent_as_a_voice_note(self, monkeypatch: Any, tmp_path: Any) -> None:
        path = tmp_path / "reply.ogg"
        path.write_bytes(b"OggS..")
        seen: list[Any] = []
        results: list[Any] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.synthesize_and_deliver",
            self._synth(seen, results, path),
        )
        d, client = self._voice_dispatcher()
        await d._speak_reply(("forum", "-100999:4"), -100999, "z" * 60, 4)
        assert client.voice == [(-100999, b"OggS..", "reply.ogg", "audio/ogg", 4)]
        assert results == [True]
        client.voice_mid = None
        await d._speak_reply(("forum", "-100999:4"), -100999, "z" * 60, 4)
        assert results == [True, False]

    @pytest.mark.asyncio
    async def test_an_unreadable_or_failing_send_is_contained(
        self, monkeypatch: Any, tmp_path: Any
    ) -> None:
        seen: list[Any] = []
        results: list[Any] = []
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.synthesize_and_deliver",
            self._synth(seen, results, tmp_path / "missing.wav"),
        )
        d, client = self._voice_dispatcher()
        await d._speak_reply(("direct", "1"), 1, "z" * 60, None)
        assert results == [False] and client.voice == []

        path = tmp_path / "reply.wav"
        path.write_bytes(b"RIFF")
        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.synthesize_and_deliver",
            self._synth(seen, results, path),
        )
        client.voice_error = OSError("upload refused")
        await d._speak_reply(("direct", "1"), 1, "z" * 60, None)
        assert client.sent == []

    @pytest.mark.parametrize(
        ("name", "mime"),
        [
            ("r.ogg", "audio/ogg"),
            ("r.opus", "audio/ogg"),
            ("r.mp3", "audio/mpeg"),
            ("r.wav", "audio/wav"),
            ("r.m4a", "audio/mp4"),
            ("R.OGG", "audio/ogg"),
            ("noext", "application/octet-stream"),
            ("a.tar.gz", "application/octet-stream"),
            ("a.flac", "application/octet-stream"),
        ],
    )
    def test_the_mime_table_is_exact(self, tmp_path: Any, name: str, mime: str) -> None:
        from kiro_crew.telegram.transport_dispatch import _audio_mime

        assert _audio_mime(str(tmp_path / name)) == mime

    def test_reading_returns_the_bytes_or_nothing(self, tmp_path: Any) -> None:
        from kiro_crew.telegram import transport_dispatch as td

        target = tmp_path / "a.wav"
        target.write_bytes(b"abc")
        assert td._read_bytes(str(target)) == b"abc"
        assert td._read_bytes(str(tmp_path / "nope")) == b""
        assert td._read_bytes(str(tmp_path)) == b""

    @pytest.mark.asyncio
    async def test_the_toggle_replies_are_exact(self) -> None:
        d, client, _ = _dispatcher({1})
        await d.handle_message(_msg("/voice on"))
        assert client.sent[-1][0] == "🔊 Voice replies on."
        await d.handle_message(_msg("/voice  OFF "))
        assert client.sent[-1][0] == "🔊 Voice replies off."
        await d.handle_message(_msg("/voice"))
        assert client.sent[-1][0] == "🔊 Voice replies are *off*. Use `/voice on` or `/voice off`."
        await d.handle_message(_msg("/voice maybe"))
        assert d._voice_pref[("direct", "1")] is False

    @pytest.mark.asyncio
    async def test_a_turn_speaks_after_success_and_before_persist(self) -> None:
        d, _client, sess = _dispatcher({1})
        d._voice_pref[("direct", "1")] = True
        order: list[Any] = []
        speak = AsyncMock(side_effect=lambda *a: order.append(("speak", list(sess.successes))))
        d._speak_reply = speak  # type: ignore[method-assign]
        d._persist_turn = lambda *a, **k: order.append(("persist",))  # type: ignore[method-assign]
        await d.handle_message(_msg("hello world"))
        assert speak.await_args.args == (("direct", "1"), 1, "Answer: hello world", None)
        assert order == [("speak", ["telegram:kirocrew:direct:1"]), ("persist",)]

    @pytest.mark.asyncio
    async def test_a_forum_turn_speaks_with_an_int_thread(self) -> None:
        d, _client, _sess = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        d._voice_pref[("forum", "-100999:4")] = True
        speak = AsyncMock()
        d._speak_reply = speak  # type: ignore[method-assign]
        await d.handle_message(_forum_msg("hello"))
        assert speak.await_args.args[3] == 4

    @pytest.mark.asyncio
    async def test_no_voice_when_off(self) -> None:
        d, _client, _sess = _dispatcher({1})
        speak = AsyncMock()
        d._speak_reply = speak  # type: ignore[method-assign]
        await d.handle_message(_msg("hello"))
        speak.assert_not_awaited()


class TestStatsAndReplyTargetCharacterization:
    """What a turn counts, and which message it visibly answers."""

    @staticmethod
    def _counted(monkeypatch: Any) -> list[str]:
        seen: list[str] = []
        recorder = SimpleNamespace(
            inc_message_received=lambda: seen.append("received"),
            inc_message_success=lambda: seen.append("success"),
            inc_message_failed=lambda: seen.append("failed"),
            summary=lambda: "S",
        )
        monkeypatch.setattr("kiro_crew.telegram.transport_dispatch.Stats", lambda: recorder)
        return seen

    def test_the_counters_per_exit(self, monkeypatch: Any) -> None:
        seen = self._counted(monkeypatch)
        d, _client, _sess = _dispatcher({1}, forum_activation="off")
        asyncio.run(d.handle_message(_forum_msg()))
        assert seen == ["received"]

        seen.clear()
        d, _client, _sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/ping")))
        assert seen == ["received"]

        seen.clear()
        d, _client, _sess = _dispatcher({7}, raise_on_get=True)
        asyncio.run(d.handle_message(_dm("hi")))
        assert seen == ["received"]

        seen.clear()
        d, _client, sess = _dispatcher({7})
        sess.closing = True
        asyncio.run(d.handle_message(_dm("hi")))
        assert seen == ["received"]

    def test_governance_bookkeeping_never_leaks(self, monkeypatch: Any) -> None:
        asked: list[str] = []

        async def _raise(channel: str) -> bool:
            asked.append(channel)
            raise RuntimeError("profile store down")

        monkeypatch.setattr(
            "kiro_crew.telegram.transport_dispatch.channel_inbound_permitted", _raise
        )
        d, _client, _sess = _dispatcher({7})
        with pytest.raises(RuntimeError):
            asyncio.run(d.handle_message(_dm("hi")))
        assert d._routing_checks == {} and asked == ["telegram"]

    @pytest.mark.asyncio
    async def test_which_message_a_turn_quotes(self) -> None:
        from kiro_crew.telegram.transport_dispatch import TelegramDispatcher

        d, client, _sess = _dispatcher({1})
        await d.handle_message(_msg("hi"))
        assert client.reply_targets[0] is None

        d, client, _sess = _dispatcher({1}, allow_forum=True, allowed_forum_chat_ids=[-100999])
        await d.handle_message(_forum_msg("hi"))
        assert client.reply_targets[0] == 7 and client.send_threads[0] == 4
        assert [t for t in client.reply_targets if t] == [7]

        d, client, sess = _dispatcher({1})
        sess.queued = [("1", "later", _origin(1, 1))]
        await d._drain_queue(d._session_key(("direct", "1")))
        assert client.reply_targets[0] is None

        zero = TelegramInboundMessage(
            channel_type="telegram",
            user_id="1",
            conversation_id="-1",
            text="x",
            chat_type="supergroup",
            message_id=0,
        )
        assert TelegramDispatcher._reply_target(zero, interpret_commands=True) is None
        group = TelegramInboundMessage(
            channel_type="telegram",
            user_id="1",
            conversation_id="-1",
            text="x",
            chat_type="group",
            message_id=9,
        )
        assert TelegramDispatcher._reply_target(group, interpret_commands=True) == 9
