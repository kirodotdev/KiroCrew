"""A Discord turn that ends with no assistant text must never read as a reply.

The recorded incident shape: the backend closes the turn with a terminal
``end_turn`` and streams no text chunk at all. On that shape the pipeline used
to (1) edit the live ``…`` placeholder into ``…`` plus the ``Finished in …``
footer, which is indistinguishable from a finished answer, and (2) persist the
user's row alone, so the transcript held no trace that the model returned
nothing and the dashboard offered an "interrupted turn" recovery for a turn
that had in fact completed.

The sibling shape, seen the same day: the provider raised before any text,
after the user had steered mid-turn. The steer chip (a ``> quoted`` line) made
the body non-empty, so the error placeholder was skipped and the bubble closed
on the chip plus the footer -- and, because the exception escaped ahead of the
persist step, the transcript recorded neither the message nor the error.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_discord import (
    FakeClient,
    FakeCtx,
    FakeProvider,
    FakeSessions,
    _cfg,
    _Ev,
    _inbound,
    _prime_live,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.history import ConversationLog

FOOTER_MARK = "\n\n-# "


class _EmptyCompletionProvider(FakeProvider):
    """The incident's frame sequence: a terminal completion and nothing else."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")


class _ToolOnlyProvider(FakeProvider):
    """A turn that ran a tool and then closed without a closing reply."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TOOL_CALL, title="Read the config")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")


class _DyingProvider(FakeProvider):
    """The provider fails before any text lands (the backend-error shape)."""

    async def stream(self, message: str) -> Any:
        raise RuntimeError("The model failed to generate a response (transient error)")
        yield  # pragma: no cover -- makes this an async generator


class _WhitespaceReplyProvider(FakeProvider):
    """A turn whose only "text" is the separator a steer boundary emits."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text="\n")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")


class _SteeredEmptyProvider(FakeProvider):
    """A steer was acked mid-turn (the typed lifecycle event) before any text,
    and the turn then closed with no text at all."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_STEER_CONSUMED)
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")


class _UnclosedStreamProvider(FakeProvider):
    """The provider stream ends with no terminal completion event at all."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text="")


class _UnclosedAfterTextProvider(FakeProvider):
    """One text chunk, then the stream is exhausted with no terminal. The chunk
    is a bare word, which the stream redactor withholds whole until a flush."""

    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=self._text)


class _DiesAfterTextProvider(FakeProvider):
    """The backend streams half an answer, then fails."""

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text="Half an answer, then")
        raise RuntimeError("The model failed to generate a response (transient error)")


class _Sessions(FakeSessions):
    """``FakeSessions`` that hands the turn a chosen provider."""

    def __init__(self, provider: FakeProvider) -> None:
        super().__init__()
        self._provider = provider

    async def get_or_create(self, key: str, **kw: Any) -> Any:
        self.last_provider = self._provider
        return self._provider, True, False


def _dispatcher(
    provider: FakeProvider, log_dir: Path
) -> tuple[DiscordDispatcher, FakeClient, _Sessions, ConversationLog]:
    sess = _Sessions(provider)
    cfg = _cfg()
    _prime_live(cfg)
    conv_log = ConversationLog(base_dir=log_dir)
    d = DiscordDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=cfg,
        allowed_user_ids={"u1"},
        allowed_thread_ids=None,
        agent=None,
        conv_log=conv_log,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess, conv_log


def _body(final_text: str) -> str:
    """The bubble's body without the ``-# Finished in …`` subtext footer."""
    return final_text.split(FOOTER_MARK, 1)[0].strip()


def _rows(conv_log: ConversationLog, d: DiscordDispatcher) -> list[dict]:
    return conv_log.read_messages(d._session_key("u1", ""))


class TestEmptyTurnIsNeverAFinishedReply:
    @pytest.mark.asyncio
    async def test_a_completed_turn_with_no_text_posts_a_notice_not_the_live_placeholder(
        self, tmp_path: Path
    ) -> None:
        d, cli, _sess, _log = _dispatcher(_EmptyCompletionProvider(), tmp_path)

        await d.handle_message(_inbound("how do I run ten coders at once?"))

        final = cli.final_text()
        assert final is not None
        body = _body(final)
        # The live placeholder is "…"; a turn that CLOSED with nothing must not
        # hand the user that same glyph under a "Finished in" footer.
        assert body != "…", f"the finished bubble is the live placeholder: {final!r}"
        assert body, f"the finished bubble carries only the footer: {final!r}"
        assert "returned nothing" in body

    @pytest.mark.asyncio
    async def test_the_empty_reply_is_recorded_in_the_transcript(self, tmp_path: Path) -> None:
        d, cli, sess, log = _dispatcher(_EmptyCompletionProvider(), tmp_path)

        await d.handle_message(_inbound("how do I run ten coders at once?"))

        rows = _rows(log, d)
        roles = [r["role"] for r in rows]
        # The user's row must not be the last word: a reader (or the dashboard's
        # recovery) needs the record that the turn completed with no reply.
        assert roles[:1] == ["user"]
        assert len(rows) >= 2, f"the transcript ends on the user's row: {roles}"
        assert rows[-1]["role"] == "notice"
        assert rows[-1]["content"] == _body(cli.final_text() or "")
        # The prompt reached the model and the turn closed, so the session's
        # health counter is untouched -- the notice is the outcome, not a fault.
        assert sess.successes and not sess.failures

    @pytest.mark.asyncio
    async def test_a_tool_only_turn_says_so_instead_of_claiming_nothing_happened(
        self, tmp_path: Path
    ) -> None:
        d, cli, _sess, log = _dispatcher(_ToolOnlyProvider(), tmp_path)

        await d.handle_message(_inbound("check the config"))

        body = _body(cli.final_text() or "")
        assert "without a closing reply" in body
        assert "returned nothing" not in body
        assert _rows(log, d)[-1]["content"] == body

    @pytest.mark.asyncio
    async def test_a_steer_chip_does_not_mask_a_turn_that_died_without_text(self) -> None:
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_turn_start()
        r.note_steer("steer-me-42")
        # The turn never reached on_done: the dispatcher's finally closes it.
        await r.close()

        final = cli.final_text()
        assert final is not None
        body = _body(final)
        assert "steer-me-42" in body  # the user's own steer is still shown
        assert (
            body.replace("> ↪️ steer-me-42", "").replace("> steer-me-42", "").strip()
        ), f"the bubble is the steer chip alone under a finished footer: {final!r}"
        assert "⚠️" in body

    @pytest.mark.asyncio
    async def test_a_steer_burst_never_pushes_the_placeholder_past_the_platform_cut(self) -> None:
        """The chip rides on the placeholder, not through the length rotation, and
        the client cuts one payload at ``DISCORD_MAX_TEXT``. A burst of steers
        (each already capped by ``_neutralize_md``) must be bounded so the notice
        and the footer -- the sentence this path exists to deliver -- survive."""
        from kiro_crew.discord.client import DISCORD_MAX_TEXT

        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_turn_start()
        for i in range(50):
            r.note_steer(f"steer number {i:02d} " + "x" * 100)
        await r.close()

        final = cli.final_text()
        assert final is not None
        assert len(final) <= DISCORD_MAX_TEXT, len(final)
        assert "⚠️ Error" in final
        assert "-# Finished in" in final
        assert final.startswith("> steer number 00")  # the chip is cut, not dropped

    @pytest.mark.asyncio
    async def test_a_turn_that_died_before_any_text_is_recorded_with_its_error(
        self, tmp_path: Path
    ) -> None:
        d, cli, sess, log = _dispatcher(_DyingProvider(), tmp_path)

        await d.handle_message(_inbound("hello?"))

        assert sess.failures
        body = _body(cli.final_text() or "")
        assert "⚠️" in body
        rows = _rows(log, d)
        roles = [r["role"] for r in rows]
        assert roles == ["user", "error"], f"the failed turn left no record: {roles}"
        assert "failed to generate" in rows[-1]["content"]

    @pytest.mark.asyncio
    async def test_an_undelivered_notice_is_recorded_as_a_failure(self, tmp_path: Path) -> None:
        """The notice is the turn's ENTIRE delivery; if Discord never took it, the
        user heard nothing, and that is the undelivered turn `record_failure` is
        for -- not a success with an empty body."""
        d, _cli, sess, log = _dispatcher(_EmptyCompletionProvider(), tmp_path)
        _cli.edit_ok = False
        _cli.fail_sends = True

        await d.handle_message(_inbound("anyone there?"))

        assert sess.failures and not sess.successes
        # The record of what happened is still written: the transcript is the
        # one place a reader can learn the turn closed empty.
        assert [r["role"] for r in _rows(log, d)] == ["user", "notice"]

    @pytest.mark.asyncio
    async def test_a_whitespace_only_reply_files_no_assistant_row(self, tmp_path: Path) -> None:
        """The steer-boundary separator alone (``"\\n"``) is not a reply: the
        durable write files no assistant row and records the notice instead."""
        d, cli, _sess, log = _dispatcher(_WhitespaceReplyProvider(), tmp_path)

        await d.handle_message(_inbound("hm"))

        assert [r["role"] for r in _rows(log, d)] == ["user", "notice"]
        assert "returned nothing" in _body(cli.final_text() or "")

    @pytest.mark.asyncio
    async def test_a_steered_turn_that_ends_with_no_text_still_posts_the_notice(
        self, tmp_path: Path
    ) -> None:
        """The acked steer rotates the (empty) pre-steer segment, which counts as
        a seal even though nothing was posted. The renderer's "earlier segments
        carried the turn, stay silent" shortcut must not swallow the verdict:
        Discord gets the sentence the transcript records."""
        d, cli, sess, log = _dispatcher(_SteeredEmptyProvider(), tmp_path)

        await d.handle_message(_inbound("do the thing"))

        final = cli.final_text()
        assert final is not None, "the steered turn closed and Discord got nothing"
        body = _body(final)
        assert "returned nothing" in body
        rows = _rows(log, d)
        assert [r["role"] for r in rows] == ["user", "notice"]
        assert rows[-1]["content"] == body
        assert sess.successes and not sess.failures

    @pytest.mark.asyncio
    async def test_a_steered_turn_whose_notice_never_landed_is_a_failure(
        self, tmp_path: Path
    ) -> None:
        """The success record follows the notice that was actually posted: when
        Discord refuses it, the turn is undelivered, steer or no steer."""
        d, cli, sess, log = _dispatcher(_SteeredEmptyProvider(), tmp_path)
        cli.edit_ok = False
        cli.fail_sends = True

        await d.handle_message(_inbound("do the thing"))

        assert (
            sess.failures and not sess.successes
        ), f"Discord took nothing, yet the turn recorded success: {sess.successes}"
        assert [r["role"] for r in _rows(log, d)] == ["user", "notice"]

    @pytest.mark.asyncio
    async def test_a_steer_chip_on_the_placeholder_is_redacted_like_every_other_send(
        self,
    ) -> None:
        """The chip rides the placeholder straight to the client instead of
        through the seal path, so it must get the same display-form redaction:
        under a shared DM scope the steer can be another person's, and a
        credential typed into it must not land in this thread."""
        cli = FakeClient()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        await r.on_turn_start()
        # Assembled at runtime so no source line spells a key id.
        key = "AK" + "IA" + "IOSFODNN7EXAMPLE"
        r.note_steer(f"use {key} for the bucket")
        await r.close()

        # The placeholder went out first; the redaction notice followed it, so the
        # last send is the notice and the placeholder is the one before.
        assert len(cli.sent) == 2, cli.sent
        placeholder = cli.sent[0][0]
        assert key not in placeholder, f"the steer chip carried the credential: {placeholder!r}"
        assert "[REDACTED: credential]" in placeholder
        assert key not in cli.sent[1][0]
        # The landed payload is tallied like a sealed segment, so the reader
        # learns the quoted command will not run as pasted.
        assert cli.sent[1][0].startswith("Security notice:")

    @pytest.mark.asyncio
    async def test_a_stream_that_never_closed_posts_the_verdict_it_records(
        self, tmp_path: Path
    ) -> None:
        """No DONE reached the renderer, so the dispatcher hands it one: the bubble
        carries the same sentence the transcript records, and delivery is judged
        after that send, not before it."""
        d, cli, sess, log = _dispatcher(_UnclosedStreamProvider(), tmp_path)

        await d.handle_message(_inbound("hello?"))

        rows = _rows(log, d)
        assert [r["role"] for r in rows] == ["user", "notice"]
        body = _body(cli.final_text() or "")
        assert body == rows[-1]["content"], f"bubble {body!r} vs transcript {rows[-1]['content']!r}"
        assert sess.successes and not sess.failures

    @pytest.mark.asyncio
    async def test_an_unclosed_turn_whose_notice_never_landed_is_a_failure(
        self, tmp_path: Path
    ) -> None:
        d, cli, sess, log = _dispatcher(_UnclosedStreamProvider(), tmp_path)
        cli.edit_ok = False
        cli.fail_sends = True

        await d.handle_message(_inbound("hello?"))

        assert (
            sess.failures and not sess.successes
        ), f"Discord took nothing, yet the unclosed turn recorded success: {sess.successes}"
        assert [r["role"] for r in _rows(log, d)] == ["user", "notice"]

    @pytest.mark.asyncio
    async def test_an_unclosed_stream_that_carried_text_delivers_and_records_it(
        self, tmp_path: Path
    ) -> None:
        """The stream redactor holds a trailing word back until a flush, and the
        flush ran only under a terminal: a reply whose last chunk was "Done"
        followed by a clean exhaustion was judged textless, and the user was told
        the turn ended without a reply while "Done" sat in a buffer."""
        d, cli, sess, log = _dispatcher(_UnclosedAfterTextProvider("Done"), tmp_path)

        await d.handle_message(_inbound("did it work?"))

        rows = _rows(log, d)
        assert [r["role"] for r in rows] == [
            "user",
            "assistant",
        ], f"the buffered reply was dropped: {[(r['role'], r['content']) for r in rows]}"
        assert rows[-1]["content"] == "Done"
        assert _body(cli.final_text() or "") == "Done"
        assert sess.successes and not sess.failures

    @pytest.mark.asyncio
    async def test_an_unclosed_stream_flushes_its_tail_through_the_redactor(
        self, tmp_path: Path
    ) -> None:
        """Flushing the withheld tail must still be a redacted flush."""
        key = "AK" + "IA" + "IOSFODNN7EXAMPLE"  # assembled so no source line spells a key id
        d, cli, _sess, log = _dispatcher(_UnclosedAfterTextProvider(f"key {key}"), tmp_path)

        await d.handle_message(_inbound("what is the key?"))

        rows = _rows(log, d)
        assert [r["role"] for r in rows] == ["user", "assistant"]
        assert rows[-1]["content"] == "key [REDACTED: credential]"
        assert key not in (cli.final_text() or "")
        assert all(key not in (t or "") for t, _c in cli.sent)
        assert all(key not in (t or "") for _m, t, _c in cli.edits)

    @pytest.mark.asyncio
    async def test_a_turn_that_died_after_streaming_text_keeps_that_text_on_record(
        self, tmp_path: Path
    ) -> None:
        """The user read half an answer before the backend failed; the transcript
        must not say the turn produced nothing. What is recorded is what the
        renderer was handed: the stream redactor holds back a short tail until
        a flush the exception pre-empted, so the tail is missing from BOTH."""
        d, cli, sess, log = _dispatcher(_DiesAfterTextProvider(), tmp_path)

        await d.handle_message(_inbound("explain"))

        assert sess.failures
        body = _body(cli.final_text() or "")
        assert body.startswith("Half an answer"), body
        rows = _rows(log, d)
        assert [r["role"] for r in rows] == [
            "user",
            "assistant",
            "error",
        ], f"the text the user saw is not on record: {[r['role'] for r in rows]}"
        assert rows[1]["content"].strip() == body
        assert "failed to generate" in rows[-1]["content"]


class TestLiveWindowAndDiskAgree:
    """A resumed dashboard session mirrors the turn into its open window first and
    then persists under the SAME row ids; the two writers must file the same rows
    for a textless turn, or the slot's own save lands a row the disk never got."""

    @staticmethod
    def _state_and_slot(tmp_path: Path) -> tuple[Any, Any]:
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        state.sessions.channel_key_for_stem = lambda stem: ""
        slot = state.get_or_create_slot(name="chat-1", linked_session_key="dashboard:chat-1")
        return state, slot

    @pytest.mark.parametrize("save_first", [True, False], ids=["save-first", "write-first"])
    def test_the_notice_lands_exactly_once_and_no_assistant_row_appears(
        self, tmp_path: Path, save_first: bool
    ) -> None:
        from kiro_crew.dashboard.channel_slots import (
            project_channel_row_live,
            project_channel_turn_live,
        )
        from kiro_crew.dashboard.chat_persistence import _save_slot_to_history
        from kiro_crew.dashboard.state import row_mid
        from kiro_crew.messaging.driver import EMPTY_TURN_NOTICE

        state, slot = self._state_and_slot(tmp_path)
        key = "dashboard:chat-1"
        # What the dispatcher hands both writers: the reply normalized ONCE to
        # "" (the turn streamed only a steer-boundary "\n"), and the verdict.
        mids = project_channel_turn_live(state, key, "hello", "")
        assert mids is not None
        notice_mid = project_channel_row_live(
            state, key, "notice", EMPTY_TURN_NOTICE, "msg msg-info"
        )
        assert notice_mid

        window = [(m["role"], m["content"]) for m in slot.messages]
        assert window == [("user", "hello"), ("notice", EMPTY_TURN_NOTICE)]

        if save_first:
            assert _save_slot_to_history(state, slot, force=True)
        DiscordDispatcher._persist_turn(
            SimpleNamespace(conv_log=state.conversation_log),  # type: ignore[arg-type]
            key,
            "hello",
            "",
            False,
            agent="kirocrew",
            mirror_mids=mids,
            extra_row=("notice", EMPTY_TURN_NOTICE, "msg msg-info", notice_mid),
        )
        if not save_first:
            assert _save_slot_to_history(state, slot, force=True)

        rows = state.conversation_log.read_messages(key)
        assert [(r["role"], r["content"]) for r in rows] == window
        assert [row_mid(r) for r in rows] == [mids[0], notice_mid]


class TestEmptyTurnVerdict:
    """The driver's verdict is the one source the bubble and the transcript share."""

    def _run(self, events: list[Any]) -> tuple[Any, Any, str]:
        import asyncio

        from test_messaging_driver import _RecordingRenderer, _ScriptedProvider

        from kiro_crew.messaging.driver import TurnDriver

        renderer = _RecordingRenderer()
        driver = TurnDriver(_ScriptedProvider(events), renderer)
        accumulated = asyncio.run(driver.run("hello"))
        return driver, renderer, accumulated

    def test_a_textless_end_turn_is_an_empty_reply(self) -> None:
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.messaging.driver import EMPTY_TURN_NOTICE

        driver, renderer, accumulated = self._run(
            [AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")]
        )
        assert accumulated == ""
        assert driver.empty_turn_notice == EMPTY_TURN_NOTICE
        # The renderer learned the same verdict from the DONE event it was handed.
        assert renderer.empty_turn_notice == EMPTY_TURN_NOTICE

    def test_a_turn_that_produced_text_has_no_notice(self) -> None:
        from kiro_crew.acp.types import AcpEvent

        driver, renderer, _ = self._run(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="Here you go."),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert driver.empty_turn_notice == ""
        assert renderer.empty_turn_notice == ""

    def test_a_cancelled_turn_is_not_an_empty_reply(self) -> None:
        from kiro_crew.acp.types import AcpEvent

        driver, _renderer, _ = self._run([AcpEvent(kind=EVENT_COMPLETE, stop_reason="cancelled")])
        assert driver.empty_turn_notice == ""

    def test_a_tool_only_turn_takes_the_after_work_wording(self) -> None:
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.messaging.driver import EMPTY_TURN_NOTICE_AFTER_WORK

        driver, _renderer, _ = self._run(
            [
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="Read the config"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
            ]
        )
        assert driver.empty_turn_notice == EMPTY_TURN_NOTICE_AFTER_WORK

    def test_an_error_family_terminal_names_its_reason(self) -> None:
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL, AcpEvent

        driver, _renderer, _ = self._run(
            [AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_TOOL_STALL)]
        )
        assert driver.empty_turn_notice.startswith("⚠️")
        assert "tool stall" in driver.empty_turn_notice

    def test_a_backend_authored_error_reason_never_reaches_the_copy(self) -> None:
        """The ``error:`` family is open on the wire: a backend can put anything
        after the prefix. Only the closed protocol values are named; the rest
        take fixed generic copy, so no backend prose (a leaked token, a path)
        rides an unredacted notice into the channel and the transcript. The
        fixture is a made-up detail, not a credential shape: what the test
        proves is that the wire string is dropped WHOLE, whatever it holds."""
        from kiro_crew.acp.types import AcpEvent

        wire = "error: backend-detail-7f3c9e at /srv/secret/path"
        driver, _renderer, _ = self._run([AcpEvent(kind=EVENT_COMPLETE, stop_reason=wire)])
        assert driver.empty_turn_notice.startswith("⚠️")
        assert "backend error" in driver.empty_turn_notice
        assert "backend-detail" not in driver.empty_turn_notice
        assert "/srv/" not in driver.empty_turn_notice

    def test_a_stream_that_never_closed_the_turn_is_an_empty_reply(self) -> None:
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.messaging.driver import EMPTY_TURN_NOTICE_UNCLOSED

        driver, renderer, _ = self._run([AcpEvent(kind=EVENT_TEXT_CHUNK, text="")])
        assert driver.completion_observed is False
        assert driver.empty_turn_notice == EMPTY_TURN_NOTICE_UNCLOSED
        # No DONE was dispatched, so the renderer learned nothing; its close()
        # path posts its own error placeholder and the dispatcher records this.
        assert renderer.empty_turn_notice == ""

    def test_a_stream_that_never_closed_still_flushes_the_text_it_carried(self) -> None:
        """The redactor withholds a trailing word until a flush; the terminal path
        flushes, and so must the exhausted one, or the verdict reads a buffer."""
        from kiro_crew.acp.types import AcpEvent

        driver, _renderer, accumulated = self._run([AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done")])
        assert driver.completion_observed is False
        assert accumulated == "Done"
        assert driver.partial_text == "Done"
        assert driver.empty_turn_notice == ""

    def test_a_productive_turn_whose_stream_never_closed_is_not_told_to_resend(self) -> None:
        """The tool already ran. "Send your message again" would run it twice."""
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_CONTINUE, EMPTY_TURN_RESEND

        driver, _renderer, _ = self._run(
            [AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="Send the report")]
        )
        assert driver.completion_observed is False
        assert (
            EMPTY_TURN_RESEND not in driver.empty_turn_notice
        ), f"a turn whose tool ran is told to resend: {driver.empty_turn_notice!r}"
        assert EMPTY_TURN_CONTINUE in driver.empty_turn_notice

    def test_a_productive_turn_closed_by_an_error_terminal_is_not_told_to_resend(self) -> None:
        """``STOP_REASON_TOOL_STALL`` is synthesised only after a tool ran, so the
        error wording for it can never be the resend wording; it still names
        the failure."""
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL, AcpEvent
        from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_CONTINUE, EMPTY_TURN_RESEND

        driver, _renderer, _ = self._run(
            [
                AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1", title="Send the report"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_TOOL_STALL),
            ]
        )
        assert driver.empty_turn_notice.startswith("⚠️")
        assert "tool stall" in driver.empty_turn_notice
        assert (
            EMPTY_TURN_RESEND not in driver.empty_turn_notice
        ), f"a turn whose tool ran is told to resend: {driver.empty_turn_notice!r}"
        assert EMPTY_TURN_CONTINUE in driver.empty_turn_notice

    def test_a_turn_that_did_no_work_keeps_the_resend_wording_on_every_failure_path(
        self,
    ) -> None:
        """The split is on work done, not on the terminal: a textless turn that
        ran nothing can be resent safely, whichever way it ended."""
        from kiro_crew.acp.types import STOP_REASON_TOOL_STALL, AcpEvent
        from kiro_crew.messaging.empty_turn_copy import EMPTY_TURN_RESEND

        for events in (
            [AcpEvent(kind=EVENT_TEXT_CHUNK, text="")],
            [AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_TOOL_STALL)],
            [AcpEvent(kind=EVENT_COMPLETE, stop_reason="error: something else")],
        ):
            driver, _renderer, _ = self._run(events)
            assert driver.empty_turn_notice.endswith(EMPTY_TURN_RESEND), driver.empty_turn_notice


class TestOneStoryAcrossSurfaces:
    """The dashboard runner and the channel driver post the SAME sentences for a
    textless turn, read from one module, so a channel thread mirrored into the
    dashboard cannot tell two stories -- and the remedy a sentence names is
    spelled in exactly one place."""

    def test_both_surfaces_read_the_sentences_from_the_shared_module(self) -> None:
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.messaging import driver, empty_turn_copy

        # ``is``, not ``==``: an identical literal re-spelled in either file
        # would compare equal and still be the drift this pins against.
        for name in ("EMPTY_TURN_NOTICE", "EMPTY_TURN_NOTICE_AFTER_WORK"):
            shared = getattr(empty_turn_copy, name)
            assert getattr(driver, name) is shared, name
            assert getattr(chat_runner, name) is shared, name
        assert (
            chat_runner.EMPTY_TURN_NOTICE_AFTER_RECOVERY
            is empty_turn_copy.EMPTY_TURN_NOTICE_AFTER_RECOVERY
        )

    def test_the_remedies_are_spelled_once(self) -> None:
        from kiro_crew.dashboard import chat_runner
        from kiro_crew.messaging import driver, empty_turn_copy

        for module in (driver, chat_runner):
            source = Path(module.__file__).read_text(encoding="utf-8")
            for remedy in (empty_turn_copy.EMPTY_TURN_RESEND, empty_turn_copy.EMPTY_TURN_CONTINUE):
                assert remedy not in source, f"{module.__name__} respells {remedy!r}"

    def test_every_sentence_names_exactly_one_remedy(self) -> None:
        """A productive turn's sentence continues; a non-productive one resends;
        the refusal rephrases. No sentence asks for both, and none asks for
        neither -- a verdict with no remedy leaves the user waiting."""
        from kiro_crew.messaging import empty_turn_copy as c

        resend = {
            c.EMPTY_TURN_NOTICE,
            c.EMPTY_TURN_NOTICE_AFTER_RECOVERY,
            c.EMPTY_TURN_NOTICE_ERROR,
        }
        resend.add(c.EMPTY_TURN_NOTICE_UNCLOSED)
        cont = {
            c.EMPTY_TURN_NOTICE_AFTER_WORK,
            c.EMPTY_TURN_NOTICE_ERROR_AFTER_WORK,
            c.EMPTY_TURN_NOTICE_UNCLOSED_AFTER_WORK,
        }
        for sentence in resend:
            assert sentence.endswith(c.EMPTY_TURN_RESEND), sentence
            assert c.EMPTY_TURN_CONTINUE not in sentence
        for sentence in cont:
            assert sentence.endswith(c.EMPTY_TURN_CONTINUE), sentence
            assert c.EMPTY_TURN_RESEND not in sentence
        assert c.EMPTY_TURN_NOTICE_REFUSAL.endswith("Rephrase it to continue.")
