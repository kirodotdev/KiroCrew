"""Discord's ``!temporary`` / ``!incognito`` modifiers, with Slack's meaning.

They put the conversation into the shared ``privacy_mode`` modes: nothing the
conversation says is recorded, and a temporary turn's prompt takes no memory. Each
refusal runs nothing: an unallowed user, a reply already running, a ``!queue`` /
``!steer`` prefix, and a resumed session (a ``!bot`` thread or a dashboard session).

The pipeline half, ``ChannelTurns``' ``blocks_reads`` adapter, is pinned here too: a
channel that passes none (every channel but Discord) builds its prompt exactly as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from channel_turn_fakes import FakeSessions as PipelineSessions
from channel_turn_fakes import Recorder, RecordingCtxBuilder, RecordingDispatcher
from channel_turns_helpers import governance_fixture  # noqa: F401
from channel_turns_helpers import published_fixture  # noqa: F401
from channel_turns_helpers import spooled_fixture  # noqa: F401
from channel_turns_helpers import KEY, _answer, _turns
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from discord_helpers import _PNG, FakeCtx, _dispatcher, _inbound
from test_discord_private_slash import _THREAD, _assert_private, _cmd, _wired

import kiro_crew.discord.transport_dispatch as td
from kiro_crew.discord.client import DiscordInbound
from kiro_crew.discord.commands import (
    COMMAND_SPEC,
    application_command_payload,
    build_help_text,
    parse_command,
)
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.messaging import privacy_mode
from kiro_crew.messaging.dispatch import Approvals, ChannelTurns
from kiro_crew.messaging.transport import InboundMessage


@pytest.fixture(autouse=True)
def _isolate_privacy_trackers():
    """The mode trackers are process globals."""
    privacy_mode.reset()
    yield
    privacy_mode.reset()


class _BuildCtx(FakeCtx):
    """Records each prompt build's ``blocks_reads``."""

    def __init__(self) -> None:
        super().__init__()
        self.blocks_reads: list[bool] = []

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        self.blocks_reads.append(bool(kw.get("blocks_reads")))
        return super().build_message(text, is_new, key, **kw)


def _texts(cli: Any) -> list[str]:
    return [text for text, _components in cli.sent]


def _record_spy(d: Any) -> list[str]:
    """The session keys the transcript writer was reached for."""
    written: list[str] = []
    d._persist_turn = lambda key, *a, **kw: written.append(key)  # type: ignore[method-assign]
    return written


# ── the grammar, the help card and the slash menu ────────────────────────────


class TestGrammar:
    @pytest.mark.parametrize(
        ("text", "name"),
        [
            ("!temporary hi", "temporary"),
            ("/temporary", "temporary"),
            ("!Incognito hi", "incognito"),
            ("/incognito", "incognito"),
        ],
    )
    def test_each_command_parses(self, text: str, name: str) -> None:
        assert parse_command(text) == name

    def test_a_modifier_inside_a_message_is_no_command(self) -> None:
        assert parse_command("please !incognito this") is None

    def test_help_lists_them(self) -> None:
        card = build_help_text()
        for usage in ("!temporary <msg>", "!incognito <msg>"):
            assert usage in card

    def test_each_becomes_a_slash_command_without_an_option(self) -> None:
        rows = {row["name"]: row for row in application_command_payload()}
        names = {name for name, _desc in COMMAND_SPEC}
        assert {"temporary", "incognito"} <= names <= set(rows)
        for name in ("temporary", "incognito"):
            assert "options" not in rows[name]
            assert 0 < len(rows[name]["description"]) <= 100


# ── !temporary / !incognito ──────────────────────────────────────────────────


class TestPrivacyModifiers:
    @pytest.mark.asyncio
    async def test_incognito_marks_the_conversation_answers_and_records_nothing(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        written = _record_spy(d)
        key = d._session_key("u1")

        await d.handle_message(_inbound("!incognito summarise this"))

        assert privacy_mode.is_incognito(key) and not privacy_mode.is_temporary(key)
        assert privacy_mode.NOTICE_INCOGNITO in _texts(cli)
        # The modifier is stripped and the rest runs; incognito still reads memory.
        assert d.ctx_builder.messages == ["summarise this"]
        assert d.ctx_builder.blocks_reads == [False]
        assert written == []

    @pytest.mark.asyncio
    async def test_temporary_blocks_memory_reads_for_this_and_later_turns(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        written = _record_spy(d)

        await d.handle_message(_inbound("!temporary first"))
        await d.handle_message(_inbound("second"))

        assert privacy_mode.is_temporary(d._session_key("u1"))
        assert privacy_mode.NOTICE_TEMPORARY in _texts(cli)
        assert d.ctx_builder.blocks_reads == [True, True]
        assert written == []

    @pytest.mark.asyncio
    async def test_a_persistent_conversation_still_reads_and_records(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        written = _record_spy(d)
        await d.handle_message(_inbound("hello"))
        assert d.ctx_builder.blocks_reads == [False]
        assert written == [d._session_key("u1")]

    @staticmethod
    def _with_file(cli: Any, text: str) -> InboundMessage:
        url = "https://cdn.discordapp.com/attachments/c/m/private.png"
        cli.attachment_bodies[url] = _PNG
        return InboundMessage(
            channel_type="discord",
            user_id="u1",
            conversation_id="c1",
            text=text,
            attachments=[
                {
                    "filename": "private.png",
                    "content_type": "image/png",
                    "size": len(_PNG),
                    "url": url,
                }
            ],
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("caption", ["!temporary what is this?", "!temporary"])
    async def test_a_modifier_on_a_file_caption_applies_and_the_file_is_answered(
        self, caption: str
    ) -> None:
        """A caption is never a command, but the modifier still applies, as on Slack."""
        d, cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        written = _record_spy(d)
        await d.handle_message(self._with_file(cli, caption))
        assert privacy_mode.is_temporary(d._session_key("u1"))
        assert d.ctx_builder.blocks_reads == [True]
        prompt = d.ctx_builder.messages[-1]
        assert "!temporary" not in prompt and ".png" in prompt
        assert written == []

    @pytest.mark.asyncio
    async def test_a_prefixed_modifier_on_a_file_caption_is_refused(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        await d.handle_message(self._with_file(cli, "!queue !incognito what is this?"))
        assert not privacy_mode.is_restricted(d._session_key("u1"))
        assert "was NOT processed" in _texts(cli)[-1]
        assert d.ctx_builder.messages == []

    @staticmethod
    def _record(monkeypatch: pytest.MonkeyPatch, mode: str | None) -> None:
        """The conversation's execution record (written when it ran in its dashboard tab)."""

        def _read(_key: str) -> Any:
            if mode is None:
                return None
            from kiro_crew.execution_context import stricter_memory_mode

            return SimpleNamespace(
                memory_mode=mode,
                with_mode=lambda m: SimpleNamespace(memory_mode=stricter_memory_mode(mode, m)),
            )

        monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", _read)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("live", [True, False])
    @pytest.mark.parametrize("text", ["!incognito", "!temporary keep this"])
    async def test_a_mode_that_would_tighten_the_execution_record_is_refused_untouched(
        self, monkeypatch: pytest.MonkeyPatch, text: str, live: bool
    ) -> None:
        """Allocation reads the mode off the record and refuses a provider open, or
        cold-starting, under the old one, so the modifier is refused BEFORE anything
        is stamped or torn down, whether or not a session is open right now."""
        self._record(monkeypatch, "persistent")
        d, cli, sess = _dispatcher({"u1"})
        sess._has = live
        d.ctx_builder = _BuildCtx()
        await d.handle_message(_inbound(text))
        assert not privacy_mode.is_restricted(d._session_key("u1"))
        assert "Send `!new`, then the modifier" in _texts(cli)[-1]
        assert "was NOT processed" in _texts(cli)[-1]
        assert d.ctx_builder.messages == [] and sess.discarded == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("record", "mode"),
        [
            (None, "incognito"),
            ("incognito", "incognito"),
            ("temporary", "incognito"),
            ("incognito", "temporary"),
        ],
    )
    async def test_the_mode_applies_when_it_tightens_no_record(
        self, monkeypatch: pytest.MonkeyPatch, record: str | None, mode: str
    ) -> None:
        """No record (a Discord-only conversation), or one already at least as strict.
        Tightening an incognito record to temporary is refused like any other."""
        self._record(monkeypatch, record)
        d, cli, _sess = _dispatcher({"u1"})
        await d.handle_message(_inbound(f"!{mode}"))
        tightens = record == "incognito" and mode == "temporary"
        assert privacy_mode.is_restricted(d._session_key("u1")) is not tightens
        expected = "Send `!new`, then the modifier" if tightens else privacy_mode.notice(mode)
        assert expected in _texts(cli)[-1]

    @pytest.mark.asyncio
    async def test_a_joined_announcement_is_not_sent_twice(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _joined(mode: str, key: str, *, notify: Any, **_kw: Any) -> bool:
            await notify(privacy_mode.notice(mode))
            return False

        monkeypatch.setattr(privacy_mode, "apply_mode", _joined)
        d, cli, _sess = _dispatcher({"u1"})
        await d.handle_message(_inbound("!incognito"))
        assert _texts(cli) == [privacy_mode.NOTICE_INCOGNITO]

    @pytest.mark.asyncio
    async def test_stacked_mid_turn_prefixes_are_refused_too(self) -> None:
        d, cli, _sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        await d.handle_message(_inbound("!queue !steer !queue !incognito keep this"))
        assert "was NOT processed" in _texts(cli)[-1]
        assert d.ctx_builder.messages == []

    def test_the_transcript_writer_refuses_a_private_conversation_on_its_own(self) -> None:
        """The second gate: a caller that skips ``_record_turn`` still writes nothing."""
        d, _cli, _sess = _dispatcher({"u1"})
        d.conv_log = mock.MagicMock()
        key = d._session_key("u1")
        privacy_mode.mark_incognito(key)
        d._persist_turn(key, "secret", "reply", True)
        assert d.conv_log.method_calls == []
        d._persist_turn(d._session_key("u2"), "open", "reply", True)
        assert d.conv_log.method_calls

    @pytest.mark.asyncio
    async def test_a_bare_modifier_marks_without_a_turn_and_answers_again(self) -> None:
        d, cli, sess = _dispatcher({"u1"})
        await d.handle_message(_inbound("!incognito"))
        await d.handle_message(_inbound("!incognito"))
        assert privacy_mode.is_incognito(d._session_key("u1"))
        assert _texts(cli) == [privacy_mode.NOTICE_INCOGNITO] * 2
        assert sess.begin_turns == 0

    @pytest.mark.asyncio
    async def test_new_leaves_the_private_conversation(self) -> None:
        d, _cli, _sess = _dispatcher({"u1"})
        await d.handle_message(_inbound("!temporary"))
        old = d._session_key("u1")
        await d.handle_message(_inbound("!new"))
        assert privacy_mode.is_temporary(old)
        assert not privacy_mode.is_restricted(d._session_key("u1"))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["!incognito", "!temporary keep this"])
    async def test_mid_turn_is_refused_and_nothing_runs(self, text: str) -> None:
        d, cli, sess = _dispatcher({"u1"})
        sess._busy = True
        await d.handle_message(_inbound(text))
        assert not privacy_mode.is_restricted(d._session_key("u1"))
        assert "was NOT processed" in _texts(cli)[-1]
        assert sess._gp.steered == [] and sess.queued == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prefix", ["!queue", "!steer"])
    async def test_behind_a_mid_turn_prefix_is_refused(self, prefix: str) -> None:
        d, cli, sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        await d.handle_message(_inbound(f"{prefix} !incognito keep this"))
        assert not privacy_mode.is_restricted(d._session_key("u1"))
        assert "was NOT processed" in _texts(cli)[-1]
        assert d.ctx_builder.messages == [] and sess._gp.steered == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text", ["!incognito", "!temporary hi"])
    async def test_a_resumed_session_or_bot_thread_is_refused(self, text: str) -> None:
        """``!bot`` and ``!sessions`` bind the conversation to a resumed key, whose
        memory mode lives on the dashboard slot, not in this channel's tracker."""
        d, cli, sess = _dispatcher({"u1"})
        d._session_resume.route = mock.AsyncMock(
            return_value=td.RoutingDecision(resumed_key="dashboard:bot-thread")
        )
        await d.handle_message(_inbound(text))
        assert not privacy_mode.is_restricted("dashboard:bot-thread")
        assert not privacy_mode.is_restricted(d._session_key("u1"))
        assert "dashboard session is resumed" in _texts(cli)[-1]
        assert sess.begin_turns == 0

    @pytest.mark.asyncio
    async def test_a_refused_mode_runs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _refuse(mode: str, key: str, **_kw: Any) -> bool:
            raise privacy_mode.PrivacyModeRefused(mode, key, privacy_mode.REFUSAL_LIMIT)

        monkeypatch.setattr(privacy_mode, "apply_mode", _refuse)
        d, _cli, sess = _dispatcher({"u1"})
        d.ctx_builder = _BuildCtx()
        await d.handle_message(_inbound("!incognito secret"))
        assert d.ctx_builder.messages == [] and sess.begin_turns == 0

    @pytest.mark.asyncio
    async def test_a_shutdown_refusal_does_not_spool_the_private_text(
        self, spooled: list[tuple[str, Any]]
    ) -> None:
        # ``spooled`` records every refusal spool (patched through the dispatch facade).
        d, _cli, sess = _dispatcher({"u1"})
        # The gateway is restarting: admission refuses before any command runs.
        sess.reserve_inbound_callback = lambda: None
        await d.handle_message(_inbound("plain words"))
        await d.handle_message(_inbound("!incognito my secret"))
        await d.handle_message(_inbound("!queue !temporary other secret"))
        assert [route.text for _channel, route in spooled] == ["plain words"]

    @pytest.mark.asyncio
    async def test_an_unallowed_user_never_reaches_the_dispatcher(self) -> None:
        dispatched: list[Any] = []

        async def _dispatch(msg: Any) -> None:
            dispatched.append(msg)

        from discord_helpers import FakeClient

        transport = DiscordTransport(
            FakeClient(),  # type: ignore[arg-type]
            allowed_user_ids=["u1"],
            allowed_thread_ids=[],
            allowed_channel_ids=[],
            dispatch=_dispatch,
        )
        for text in ("!incognito hi", "/temporary"):
            await transport.receive(DiscordInbound(channel_id="c1", user_id="u2", text=text))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_an_unallowed_user_cannot_use_the_slash_form(self) -> None:
        d, sess, recorder = _wired()
        itx = _cmd("incognito", _THREAD)
        itx.user_id = "intruder"
        await d.on_interaction(itx)
        assert recorder.calls == [] or not recorder.token_writes("cmdtok")[1:]
        assert not privacy_mode.is_restricted(d._session_key("intruder", _THREAD))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("name", ["incognito", "temporary"])
    async def test_the_slash_form_marks_and_answers_only_the_caller(self, name: str) -> None:
        d, sess, recorder = _wired()
        await d.on_interaction(_cmd(name, _THREAD))
        _assert_private(recorder, _THREAD)
        assert privacy_mode.is_restricted(d._session_key("u1", _THREAD))
        assert sess.begin_turns == 0


# ── the pipeline's blocks_reads adapter ──────────────────────────────────────


class TestPipelineBlocksReads:
    def test_a_channel_without_the_adapter_builds_as_before(self) -> None:
        """Every channel but Discord passes no adapter: even a key the trackers hold
        as temporary builds its prompt with reads on, exactly as before."""
        privacy_mode.mark_temporary(KEY)
        sessions = PipelineSessions()
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)
        _answer(_turns(sessions, ctx=ctx))
        assert [build["blocks_reads"] for build in ctx.builds] == [False]

    @pytest.mark.parametrize("answer", [True, False])
    def test_the_adapter_decides_for_the_turns_key(self, answer: bool) -> None:
        sessions = PipelineSessions()
        ctx = RecordingCtxBuilder(ledger=sessions.ledger)
        asked: list[str] = []

        async def _blocks(key: str) -> bool:
            asked.append(key)
            return answer

        recorder = Recorder()
        turns = ChannelTurns(
            "weixin",
            sessions=sessions,
            ctx_builder=ctx,
            dispatcher=RecordingDispatcher("auto"),
            agent=lambda: "agentA",
            record=recorder.record,
            blocks_reads=_blocks,
            approvals=Approvals(grant=None),
        )
        _answer(turns)
        assert asked == [KEY]
        assert [build["blocks_reads"] for build in ctx.builds] == [answer]
