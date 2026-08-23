"""Command-surface and dispatch fixes shared by the messaging channels.

Four defects, each of which used to surface as "the feature is broken" rather
than as an error:

1. ``!dashboard 0h`` minted a login link that had already expired, on Discord and
   Telegram alike, because ``parse_duration("0h")`` answers ``0`` and ``0 is not
   None``. Both parsers now floor the lifetime, and the reply reports the
   lifetime the token really has.
2. An unrecognized ``!command`` was forwarded to the model, which answered the
   literal text. The false-positive direction matters as much as the fix: prose
   may legitimately open with ``!``, and a message with attachments is never a
   command.
3. A bare ``!queue`` / ``!steer`` reached the model as literal text on Discord
   while Telegram answered with the directive's usage.
4. A ``HOOK_REPLY`` from a user-defined ``on_message`` hook was silently
   discarded by the shared channel pipeline, so auto-replies worked on Slack
   only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from kiro_crew.discord import commands as dcmd
from kiro_crew.discord import transport_dispatch as dtd
from kiro_crew.discord.session_resume import RoutingDecision
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.discord.transport_dispatch import DiscordDispatcher
from kiro_crew.hooks import HookResult
from kiro_crew.messaging import dispatch as D
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram import commands as tcmd

# ── 1. An unrecognized ``!command`` (and the prose that must not trip it) ───


class TestUnknownCommandUsage:
    def test_a_mistyped_command_is_answered_with_the_usage(self) -> None:
        out = dcmd.unknown_command_usage("!sesions")
        assert "!sesions" in out  # names what the user typed
        assert "!sessions" in out  # the card carries the real spelling
        assert "without the leading `!`" in out  # and the way back to chatting

    def test_a_mistyped_command_with_arguments_is_caught_too(self) -> None:
        assert dcmd.unknown_command_usage("!compct now") != ""

    def test_the_usage_reply_fits_a_single_discord_message(self) -> None:
        """The card grows with every command added, so pin the ceiling now."""
        assert len(dcmd.unknown_command_usage("!sesions")) <= DISCORD_CAPABILITIES.max_message_chars

    def test_case_is_irrelevant(self) -> None:
        assert dcmd.unknown_command_usage("!SESIONS") != ""

    def test_every_real_command_is_left_alone(self) -> None:
        for name, _desc in dcmd.COMMAND_SPEC:
            assert dcmd.unknown_command_usage(f"!{name}") == "", name

    def test_the_typo_safe_aliases_are_left_alone(self) -> None:
        for text in ("!session", "!models", "!start", "!cancel", "!new please"):
            assert dcmd.unknown_command_usage(text) == "", text

    def test_the_mid_turn_directives_are_left_to_their_own_guard(self) -> None:
        """They are message prefixes, and each has a usage reply of its own."""
        assert dcmd.unknown_command_usage("!queue") == ""
        assert dcmd.unknown_command_usage("!steer") == ""
        assert dcmd.unknown_command_usage("!queue do the thing") == ""

    def test_prose_that_merely_opens_with_a_bang_reaches_the_model(self) -> None:
        for text in (
            "",
            "   ",
            "hello there",
            "!",
            "! see below",
            "!!! the build is broken",
            "!?",
            "!5 minutes left",
            "!-x",
            "!(this one)",
            "!_private",
            "!a",
            "wait !sesions was a typo",  # not the FIRST token
            "!supercalifragilisticexpialidociouslylongtokenname",  # past 32 chars
            "/sesions",  # the slash prefix belongs to Discord's own picker
            "/etc/hosts is wrong",
        ):
            assert dcmd.unknown_command_usage(text) == "", text


# ── 2. A bare ``!queue`` / ``!steer`` ───────────────────────────────────────


class TestBareMidTurnOverride:
    def test_a_lone_directive_is_detected(self) -> None:
        assert dcmd.is_bare_mid_turn_override("!queue") is True
        assert dcmd.is_bare_mid_turn_override("  !STEER ") is True
        assert dcmd.is_bare_mid_turn_override("/queue") is True  # both prefixes

    def test_a_directive_with_a_body_is_not_bare(self) -> None:
        assert dcmd.is_bare_mid_turn_override("!queue do this") is False
        assert dcmd.is_bare_mid_turn_override("!steer go left") is False

    def test_other_text_is_not_a_directive(self) -> None:
        assert dcmd.is_bare_mid_turn_override("!new") is False
        assert dcmd.is_bare_mid_turn_override("hello") is False
        assert dcmd.is_bare_mid_turn_override("") is False

    def test_it_matches_telegram_on_the_shared_cases(self) -> None:
        """The divergence this closes was Discord answering where Telegram did."""
        for name in ("/queue", "/steer"):
            assert dcmd.is_bare_mid_turn_override(name) is True
            assert tcmd.is_bare_mid_turn_override(name) is True
            assert dcmd.is_bare_mid_turn_override(f"{name} body") is False
            assert tcmd.is_bare_mid_turn_override(f"{name} body") is False


# ── Discord dispatcher harness (both items end to end) ────────────────────


class _Client:
    """Records what the dispatcher posts back to the channel."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, channel_id: str, text: str, **kw: Any) -> str:
        self.sent.append(text)
        return "m1"

    async def send_typing(self, channel_id: str) -> None:
        return None

    async def edit_message(self, channel_id: str, message_id: str, text: str, **kw: Any) -> bool:
        self.sent.append(text)
        return True


class _Sessions:
    """Just enough session manager for the pre-turn half of handle_message."""

    def __init__(self, busy: bool = False) -> None:
        self._busy = busy
        self.busy_checks: list[str] = []
        self.started: list[str] = []

    def max_generation(self, bucket: str) -> int:
        return 0

    def is_busy(self, key: str) -> bool:
        self.busy_checks.append(key)
        return self._busy

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return False

    async def get_or_create(self, key: str, **kw: Any) -> Any:
        self.started.append(key)
        # Nothing past acquisition is under test here; failing loudly keeps the
        # fake honest about how far the message actually travelled.
        raise RuntimeError("no provider in this harness")

    async def record_failure(self, key: str) -> None:
        return None

    def release(self, key: str) -> None:
        return None

    def find_mirror_sessions(self, link: Any, *, inbound_only: bool = False) -> list[str]:
        return []


class _Resume:
    """Routing stub: this conversation owns itself and owes no detach notice."""

    def __init__(self) -> None:
        self.pickers: dict[str, Any] = {}
        self.dashboard_state = None

    async def route(self, channel_id: str) -> RoutingDecision:
        return RoutingDecision()

    def resumed_session(self, channel_id: str) -> str | None:
        return None


def _dispatcher(busy: bool = False) -> tuple[DiscordDispatcher, _Client, _Sessions]:
    sessions = _Sessions(busy=busy)
    dispatcher = DiscordDispatcher(
        sessions=sessions,  # type: ignore[arg-type]
        ctx_builder=SimpleNamespace(hooks=None),  # type: ignore[arg-type]
        cfg=SimpleNamespace(
            discord=SimpleNamespace(soft_threshold_pct=80),
            agent=SimpleNamespace(default_agent=""),
            messaging=SimpleNamespace(
                dm_scope="per-channel-peer",
                idle_reset_minutes=0,
                daily_reset_hour=-1,
                queue_mode="steer",
            ),
            dashboard=SimpleNamespace(url=""),
        ),
        allowed_user_ids={"u1"},
    )
    client = _Client()
    dispatcher.client = client  # type: ignore[assignment]
    dispatcher._session_resume = _Resume()  # type: ignore[assignment]
    return dispatcher, client, sessions


def _inbound(text: str, attachments: list[Any] | None = None) -> InboundMessage:
    return InboundMessage(
        channel_type="discord",
        user_id="u1",
        conversation_id="c1",
        text=text,
        attachments=attachments or [],
    )


@pytest.fixture()
def permitted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pass the per-message governance gate without touching the profile store."""

    async def _permitted(_channel_type: str) -> bool:
        return True

    monkeypatch.setattr(dtd, "channel_inbound_permitted", _permitted)


@pytest.mark.asyncio
async def test_an_unknown_command_answers_with_usage_and_starts_no_turn(
    permitted: None,
) -> None:
    dispatcher, client, sessions = _dispatcher()

    await dispatcher.handle_message(_inbound("!sesions"))

    assert len(client.sent) == 1
    assert "isn't a command" in client.sent[0]
    assert "!sessions" in client.sent[0]
    # The decisive half: the message never reached the turn machinery, so the
    # model never saw the literal text.
    assert sessions.busy_checks == []
    assert sessions.started == []


@pytest.mark.asyncio
async def test_a_bare_directive_answers_with_its_usage_and_starts_no_turn(
    permitted: None,
) -> None:
    dispatcher, client, sessions = _dispatcher()

    await dispatcher.handle_message(_inbound("!queue"))

    assert client.sent == ["Those take a message: `!queue <msg>` or `!steer <msg>`."]
    assert sessions.busy_checks == []
    assert sessions.started == []


@pytest.mark.asyncio
async def test_prose_opening_with_a_bang_still_reaches_the_model(permitted: None) -> None:
    """The false-positive direction: no usage card, and the turn does run."""
    dispatcher, _client, sessions = _dispatcher(busy=True)
    handled: list[str] = []

    async def _busy(session_key: str, msg: Any, text: str, override_mode: str | None) -> None:
        handled.append(text)

    dispatcher._handle_busy = _busy  # type: ignore[assignment]

    await dispatcher.handle_message(_inbound("!!! the build is broken"))

    # Reached the mid-turn ladder as ordinary turn content, verbatim.
    assert handled == ["!!! the build is broken"]
    assert sessions.busy_checks  # it got as far as the concurrency check


@pytest.mark.asyncio
async def test_a_captioned_attachment_is_never_read_as_a_command(permitted: None) -> None:
    """An attachment makes the message content, so the caption must not intercept.

    Answering ``!sesions`` with the usage card here would also silently discard
    the attached file, which is the class of bug the attachment guard exists to
    prevent.
    """
    dispatcher, client, sessions = _dispatcher(busy=True)
    handled: list[str] = []

    async def _busy(session_key: str, msg: Any, text: str, override_mode: str | None) -> None:
        handled.append(text)

    dispatcher._handle_busy = _busy  # type: ignore[assignment]

    await dispatcher.handle_message(_inbound("!sesions", attachments=[{"url": "u", "id": "1"}]))

    assert handled == ["!sesions"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_a_drained_queue_message_is_never_read_as_a_command(permitted: None) -> None:
    """``interpret_commands=False`` (the drain path) skips both new guards."""
    dispatcher, client, sessions = _dispatcher(busy=True)
    handled: list[str] = []

    async def _busy(session_key: str, msg: Any, text: str, override_mode: str | None) -> None:
        handled.append(text)

    dispatcher._handle_busy = _busy  # type: ignore[assignment]

    await dispatcher.handle_message(_inbound("!sesions"), interpret_commands=False)

    assert handled == ["!sesions"]
    assert client.sent == []


@pytest.mark.asyncio
async def test_a_real_command_still_runs(permitted: None) -> None:
    """The guards sit behind every command intercept, not in front of them."""
    dispatcher, client, sessions = _dispatcher()

    await dispatcher.handle_message(_inbound("!help"))

    assert len(client.sent) == 1
    assert "Kiro Crew" in client.sent[0]
    assert "isn't a command" not in client.sent[0]


# ── 4. HOOK_REPLY on the shared channel pipeline ────────────────────────────


class _PipelineSessions:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.released = 0

    async def get_or_create(self, key: str, agent: str = "", channel_id: str = "") -> Any:
        self.created.append(key)
        return object(), False, False

    async def set_channel(self, key: str, channel_id: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        return None

    async def record_failure(self, key: str) -> None:
        return None

    def release(self, key: str) -> None:
        self.released += 1

    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return False


class _MutedSessions(_PipelineSessions):
    def is_mirror_paused(self, key: str, *, origin: bool = False) -> bool:
        return True


class _PipelineRenderer:
    capabilities = None
    channel_type = "weixin"

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.done = 0
        self.closed = 0
        self.started = 0

    async def on_turn_start(self) -> None:
        self.started += 1

    async def on_text_chunk(self, text: str) -> None:
        self.chunks.append(text)

    async def on_done(self, stop_reason: str = "") -> None:
        self.done += 1

    async def close(self) -> None:
        self.closed += 1


class _Hooks:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.seen: list[str] = []
        self.auto_approve_subagent_spawn = False

    def on_message(self, text: str) -> Any:
        self.seen.append(text)
        return self.result

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class _CtxBuilder:
    def __init__(self, hook_result: Any = None) -> None:
        self.hooks = _Hooks(hook_result if hook_result is not None else HookResult.passthrough())
        self.built: list[str] = []

    def build_message(self, text: str, is_new: bool, session_key: str, **kw: Any) -> Any:
        self.built.append(text)
        return text, None


class _Driver:
    def __init__(self, *a: Any, **kw: Any) -> None:
        pass

    async def run(self, message: str) -> str:
        return "the model's answer"


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _permitted(_channel_type: str) -> bool:
        return True

    async def _publish(_sessions: Any, _key: str) -> None:
        return None

    async def _embed(fn: Any, *args: Any, **kw: Any) -> Any:
        return fn(*args, **kw)

    monkeypatch.setattr(D, "inbound_permitted", _permitted)
    monkeypatch.setattr(D, "publish_turn_identity", _publish)
    monkeypatch.setattr(D, "run_in_embed_pool", _embed)
    monkeypatch.setattr(D, "TurnDriver", _Driver)


def _turn(renderer: Any, persisted: list[tuple[str, str, bool]]) -> ChannelTurn:
    return ChannelTurn(
        channel_type="weixin",
        session_key="weixin:agentA:direct:userA",
        conversation_id="weixin:userA",
        agent="agentA",
        user_text="ping",
        renderer=renderer,
        approval_mode="auto",
        persist=lambda user_text, reply, is_new: persisted.append((user_text, reply, is_new)),
    )


@pytest.mark.asyncio
async def test_a_hook_reply_is_delivered_and_no_session_is_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    sessions = _PipelineSessions()
    renderer = _PipelineRenderer()
    persisted: list[tuple[str, str, bool]] = []
    ctx = _CtxBuilder(HookResult.reply("pong"))

    await drive_turn(_turn(renderer, persisted), sessions=sessions, ctx_builder=ctx)

    assert ctx.hooks.seen == ["ping"]
    assert renderer.chunks == ["pong"]
    assert renderer.done == 1
    # No LLM session, no context build, and no typing indicator for a turn that
    # never runs -- the whole point of an auto-reply.
    assert sessions.created == []
    assert ctx.built == []
    assert renderer.started == 0
    # Nothing was acquired, so nothing may be released; the renderer is still
    # finalized by the pipeline's finally.
    assert sessions.released == 0
    assert renderer.closed == 1
    # The exchange the user saw is recorded, with no new-session bookkeeping.
    assert persisted == [("ping", "pong", False)]


@pytest.mark.asyncio
async def test_a_hook_reply_is_redacted_before_it_reaches_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This path skips TurnDriver, which is what redacts everything else."""
    _patch_pipeline(monkeypatch)
    leaked = "key AKIAIOSFODNN7EXAMPLE here"
    renderer = _PipelineRenderer()
    persisted: list[tuple[str, str, bool]] = []

    await drive_turn(
        _turn(renderer, persisted),
        sessions=_PipelineSessions(),
        ctx_builder=_CtxBuilder(HookResult.reply(leaked)),
    )

    assert renderer.chunks and "AKIAIOSFODNN7EXAMPLE" not in renderer.chunks[0]
    # And the transcript records what was shown, not the raw credential.
    assert "AKIAIOSFODNN7EXAMPLE" not in persisted[0][1]


@pytest.mark.asyncio
async def test_a_passthrough_hook_still_runs_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_pipeline(monkeypatch)
    sessions = _PipelineSessions()
    renderer = _PipelineRenderer()
    persisted: list[tuple[str, str, bool]] = []
    ctx = _CtxBuilder(HookResult.passthrough())

    await drive_turn(_turn(renderer, persisted), sessions=sessions, ctx_builder=ctx)

    assert sessions.created == ["weixin:agentA:direct:userA"]
    assert ctx.built == ["ping"]
    assert renderer.started == 1
    assert persisted == [("ping", "the model's answer", False)]
    assert sessions.released == 1


@pytest.mark.asyncio
async def test_a_modify_hook_is_not_treated_as_a_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only HOOK_REPLY short-circuits; modify/inject belong to the turn."""
    _patch_pipeline(monkeypatch)
    sessions = _PipelineSessions()
    renderer = _PipelineRenderer()

    await drive_turn(
        _turn(renderer, []),
        sessions=sessions,
        ctx_builder=_CtxBuilder(HookResult.modify("ping, politely")),
    )

    assert sessions.created == ["weixin:agentA:direct:userA"]
    assert renderer.chunks == []


@pytest.mark.asyncio
async def test_a_context_builder_without_hooks_still_runs_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hook manager is optional on this seam, so its absence is not a failure."""
    _patch_pipeline(monkeypatch)
    sessions = _PipelineSessions()

    class _NoHooks:
        def build_message(self, text: str, is_new: bool, session_key: str, **kw: Any) -> Any:
            return text, None

    await drive_turn(_turn(_PipelineRenderer(), []), sessions=sessions, ctx_builder=_NoHooks())

    assert sessions.created == ["weixin:agentA:direct:userA"]


@pytest.mark.asyncio
async def test_a_muted_conversation_records_the_hook_reply_but_does_not_post_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disconnecting a channel drops the writes back, not the bookkeeping."""
    _patch_pipeline(monkeypatch)
    renderer = _PipelineRenderer()
    persisted: list[tuple[str, str, bool]] = []

    await drive_turn(
        _turn(renderer, persisted),
        sessions=_MutedSessions(),
        ctx_builder=_CtxBuilder(HookResult.reply("pong")),
    )

    assert renderer.chunks == []
    assert renderer.done == 0
    assert persisted == [("ping", "pong", False)]


def test_hook_auto_reply_reads_none_as_run_the_turn() -> None:
    """The helper's contract, which the pipeline's ``is not None`` check rests on."""
    assert D.hook_auto_reply(_CtxBuilder(HookResult.passthrough()), "hi") is None
    assert D.hook_auto_reply(_CtxBuilder(HookResult.inject_context("ctx")), "hi") is None
    assert D.hook_auto_reply(None, "hi") is None
    assert D.hook_auto_reply(SimpleNamespace(hooks=None), "hi") is None
    # An empty auto-reply still CLAIMS the message: "" is not None, so the turn
    # is skipped rather than run against the operator's rule.
    assert D.hook_auto_reply(_CtxBuilder(HookResult.reply("")), "hi") == ""


def test_the_dispatch_pipeline_is_still_importable_without_a_loop() -> None:
    """Guards the import-time additions (hooks + security) against a cycle."""
    assert asyncio.iscoroutinefunction(drive_turn)
