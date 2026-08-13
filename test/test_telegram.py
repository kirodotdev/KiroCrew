"""Unit tests for the Telegram channel on the messaging-transport abstraction.

Covers: command parsing + conversation state (commands.py), text chunking +
[OPTIONS:] extraction + inline keyboards (renderer.py), deny-by-default auth +
capabilities + inbound normalization (transport.py), streaming render +
finalization (renderer.py), the interactive approval decider, and the dispatch
turn + callback routing (transport_dispatch.py).
"""

from __future__ import annotations

import asyncio
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.messaging.link import (
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    legacy_dashboard_mirror_key,
)
from kiro_crew.messaging.renderer import (
    DONE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    TOOL_CALL,
    OutputEvent,
)
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.session import _opt_out_key
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.telegram.client import (
    TELEGRAM_CHUNK_LIMIT,
    TELEGRAM_MAX_TEXT,
    TelegramClient,
    TelegramInbound,
    _cap_text,
    _record_api_duration,
    truncate_html_safe,
)
from kiro_crew.telegram.commands import (
    COMMAND_SPEC,
    ConversationState,
    bot_command_payload,
    build_help_text,
    format_ttl,
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_dashboard_ttl,
    parse_mid_turn_override,
)
from kiro_crew.telegram.renderer import (
    TelegramApprovalDecider,
    TelegramRenderer,
    _extract_options,
    _has_table,
    _may_exceed_rendered,
    _md_to_telegram_html,
    _rendered_len,
    _row_cell_count,
    _seal_table_fallback,
    _split_markdown,
    _split_markdown_bounded,
    _split_markdown_table_aware,
    _split_table_rows,
    _split_text,
    _strip_steering,
    build_inline_keyboard,
)
from kiro_crew.telegram.transport import (
    TELEGRAM_CAPABILITIES,
    TelegramInboundMessage,
    TelegramTransport,
    forum_gate_outcome,
)
from kiro_crew.telegram.transport_dispatch import (
    _MODEL_PICKER_TTL_SECS,
    _STEER_ACK_EMOJI,
    TelegramDispatcher,
    _user_safe_failure_reason,
)

# ── Fakes ──────────────────────────────────────────────────────────────────


class FakeClient:
    """Captures outbound Bot API calls."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.edits: list[tuple[int, str, Any]] = []
        self.drafts: list[tuple[int, str]] = []
        self.markup_edits: list[tuple[int, Any]] = []
        self.answered: list[str] = []
        self.reply_targets: list[Any] = []
        self.reactions: list[tuple[int, str]] = []
        # Forum-topic ids captured per outbound send/typing (parallel to sent).
        self.send_threads: list[Any] = []
        self.typing_threads: list[Any] = []
        self._mid = 100
        #: (markdown, reply_markup, thread) per sendRichMessage call.
        self.rich_sent: list[tuple[str, Any, Any]] = []
        self.rich_silent: list[bool] = []
        #: message_ids passed to deleteMessage.
        self.deleted: list[int] = []
        #: When True, send_rich_message reports failure (server lacks the API).
        self.rich_fails = False

    async def send_typing(self, chat_id: int, *, message_thread_id: Any = None) -> None:
        self.typing_threads.append(message_thread_id)
        return None

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        message_thread_id: Any = None,
    ) -> bool:
        self.drafts.append((draft_id, text))
        return True

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        retry_plain: bool = True,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
    ) -> int:
        await asyncio.sleep(0)  # yield like a real network await (exposes races)
        self._mid += 1
        self.sent.append((text, reply_markup))
        self.reply_targets.append(reply_to_message_id)
        self.send_threads.append(message_thread_id)
        return self._mid

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: Any = None,
        reply_markup: Any = None,
        retry_plain: bool = True,
    ) -> bool:
        self.edits.append((message_id, text, reply_markup))
        return True

    async def edit_message_reply_markup(
        self, chat_id: int, message_id: int, reply_markup: Any = None
    ) -> bool:
        self.markup_edits.append((message_id, reply_markup))
        return True

    async def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append(callback_query_id)

    async def send_rich_message(
        self,
        chat_id: int,
        markdown: str,
        *,
        reply_markup: Any = None,
        message_thread_id: Any = None,
        disable_notification: bool = False,
    ) -> Any:
        await asyncio.sleep(0)  # yield like a real network await
        if self.rich_fails:
            return None
        self._mid += 1
        self.rich_silent.append(disable_notification)
        self.rich_sent.append((markdown, reply_markup, message_thread_id))
        return self._mid

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def set_message_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((message_id, emoji))

    def final_text(self) -> Any:
        """Text the user ultimately sees on the live message: the last edit if it
        was edited (edit-streaming), else the last send."""
        if self.edits:
            return self.edits[-1][1]
        return self.sent[-1][0] if self.sent else None

    def final_markup(self) -> Any:
        if self.edits:
            return self.edits[-1][2]
        return self.sent[-1][1] if self.sent else None


class _Ev:
    def __init__(self, kind: str, text: str = "", stop_reason: str = "", title: str = "") -> None:
        self.kind = kind
        self.text = text
        self.stop_reason = stop_reason
        self.tool_call_id = ""
        self.title = title
        self.context_usage_pct = 0.0


class FakeProvider:
    supports_steer = True

    def __init__(self, reply: str = "Answer", models: list | None = None) -> None:
        self._reply = reply
        self.steered: list = []
        self.cancelled = 0
        self.active_turn = True  # gates _handle_busy's live-turn steer check
        # Models the backend "advertised" at session init, plus the ids a
        # /model press pushed through session/set_model.
        self.advertised: list = list(models or [])
        self.set_models: list[str] = []
        self.set_model_error: Exception | None = None
        self.client = SimpleNamespace(set_model=self._set_model)

    def available_models(self) -> list:
        return list(self.advertised)

    async def _set_model(self, model_id: str) -> None:
        if self.set_model_error is not None:
            raise self.set_model_error
        self.set_models.append(model_id)

    def has_active_turn(self) -> bool:
        return self.active_turn

    async def steer(self, text: str) -> bool:
        self.steered.append(text)
        return True

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> str:
        self.cancelled += 1
        return "acked"

    async def stream(self, message: str) -> Any:
        yield _Ev(EVENT_TEXT_CHUNK, text=f"{self._reply}: {message[:16]}")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def stream_command(self, command: str) -> Any:
        yield _Ev(EVENT_COMPACTION_STATUS, text="completed", title="ok")
        yield _Ev(EVENT_COMPLETE, stop_reason="end_turn")

    async def compact(self, context: str = "") -> None:
        return None

    async def wait_for_compaction(self, timeout: float = 0.0) -> dict:
        return {"type": "completed", "summary": "ok"}

    async def approve_tool(self, request_id: Any) -> None:
        return None

    async def reject_tool(self, request_id: Any) -> None:
        return None


class FakeSessions:
    def __init__(self, raise_on_get: bool = False) -> None:
        self.released: list[str] = []
        self.acquired: list[str] = []
        self.destroyed: list[str] = []
        self.discarded: list[str] = []
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.last_model: Any = None
        self.raise_on_get = raise_on_get
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()
        self.mirror_links: dict[str, Any] = {}
        self.mirror_opt_outs: set[str] = set()
        self.batch_depth = 0
        self.batched_writes: list[bool] = []
        self._pid: Any = None

    async def get_or_create(
        self, key: str, *, agent: Any = None, channel_id: Any = None, model: Any = None
    ) -> Any:
        self.last_agent = agent
        self.last_model = model
        if self.raise_on_get:
            raise RuntimeError("cold-start failed")
        return FakeProvider(), True, False

    async def set_channel(self, key: str, channel: str) -> None:
        return None

    def record_success(self, key: str) -> None:
        self.successes.append(key)

    async def record_failure(self, key: str) -> None:
        self.failures.append(key)

    def check_context_usage(self, key: str, provider: Any) -> float:
        return 10.0

    def release(self, key: str) -> None:
        self.released.append(key)

    def get_provider(self, key: str) -> Any:
        return self._gp

    def get_pid(self, key: str) -> Any:
        return self._pid

    def is_busy(self, key: str) -> bool:
        return self._busy

    def max_generation(self, bucket: str) -> int:
        return -1

    def set_mirror_link(
        self, key: str, link: Any, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> None:
        self.batched_writes.append(self.batch_depth > 0)
        self.mirror_links[key] = link

    def get_mirror_link(self, key: str) -> Any:
        return self.mirror_links.get(key)

    @contextmanager
    def batched_save(self) -> Any:
        self.batch_depth += 1
        try:
            yield
        finally:
            self.batch_depth -= 1

    def set_mirror_opt_out(self, key: str, opted_out: bool) -> None:
        self.batched_writes.append(self.batch_depth > 0)
        if opted_out:
            self.mirror_opt_outs.add(_opt_out_key(key))
        else:
            self.mirror_opt_outs.discard(_opt_out_key(key))

    def mirror_opt_out(self, key: str) -> bool:
        return _opt_out_key(key) in self.mirror_opt_outs

    def clear_mirror_link(self, key: str, *, reason: str = UNBIND_REASON_UNSPECIFIED) -> bool:
        self.batched_writes.append(self.batch_depth > 0)
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(
        self, link: Any, *, reason: str = UNBIND_REASON_UNSPECIFIED
    ) -> list[str]:
        cleared = [key for key, candidate in self.mirror_links.items() if candidate == link]
        for key in cleared:
            self.mirror_links.pop(key, None)
        return cleared

    def enqueue(self, key: str, ts: str, text: str, *, force: bool = False, **kw: Any) -> bool:
        if force or self._busy:
            self.queued.append((ts, text, kw))
            return True
        return False

    def dequeue(self, key: str) -> Any:
        return self.queued.pop(0) if self.queued else None

    def clear_queue(self, key: str) -> None:
        self.queued.clear()

    def has_session(self, key: str) -> bool:
        return self._has

    async def try_acquire(self, key: str) -> bool:
        # Mirror the real atomic acquire-if-idle: refuse if a turn holds the
        # semaphore or no session exists; otherwise "acquire" and record it.
        if self._busy or not self._has:
            return False
        self.acquired.append(key)
        return True

    async def destroy(self, key: str) -> None:
        self.destroyed.append(key)

    async def discard_conversation(self, key: str) -> None:
        self.discarded.append(key)


class _FakeHooks:
    auto_approve_subagent_spawn = False

    def on_tool_call(self, *a: Any, **k: Any) -> Any:
        return SimpleNamespace(action="allow")


class FakeCtx:
    def __init__(self) -> None:
        self.hooks = _FakeHooks()

    def build_message(self, text: str, is_new: bool, key: str, **kw: Any) -> Any:
        return text, None


def _cfg(
    soft: int = 80,
    default_agent: str = "",
    *,
    allow_forum: bool = False,
    allowed_forum_chat_ids: list | None = None,
    dm_scope: str = "per-channel-peer",
) -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(
            soft_threshold_pct=soft,
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids or [],
        ),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope=dm_scope,
            idle_reset_minutes=0,
            daily_reset_hour=-1,
            queue_mode="steer",
        ),
    )


def _dispatcher(
    allowed: set[int],
    *,
    raise_on_get: bool = False,
    default_agent: str = "",
    allow_forum: bool = False,
    allowed_forum_chat_ids: list | None = None,
    dm_scope: str = "per-channel-peer",
) -> tuple[TelegramDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    d = TelegramDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=_cfg(
            default_agent=default_agent,
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids,
            dm_scope=dm_scope,
        ),
        allowed_user_ids=allowed,
        agent=None,
        conv_log=None,
    )
    cli = FakeClient()
    d.client = cli  # type: ignore[assignment]
    return d, cli, sess


# ── commands.py ──────────────────────────────────────────────────────────


class TestParseCommand:
    def test_new_aliases(self) -> None:
        assert parse_command("/new") == "new"
        assert parse_command("/start") == "new"

    def test_compact(self) -> None:
        assert parse_command("/compact") == "compact"

    def test_help(self) -> None:
        assert parse_command("/help") == "help"

    def test_command_with_trailing_args(self) -> None:
        assert parse_command("/new please") == "new"

    def test_plain_text_is_not_a_command(self) -> None:
        assert parse_command("hello there") is None

    def test_unknown_slash_is_not_a_command(self) -> None:
        assert parse_command("/frobnicate") is None

    def test_mid_turn_override_queue(self) -> None:
        assert parse_mid_turn_override("/queue do this after") == ("queue", "do this after")

    def test_mid_turn_override_steer(self) -> None:
        assert parse_mid_turn_override("/steer stop now") == ("steer", "stop now")

    def test_mid_turn_override_case_insensitive_and_leading_space(self) -> None:
        assert parse_mid_turn_override("  /QUEUE later") == ("queue", "later")

    def test_mid_turn_override_none_for_plain_text(self) -> None:
        assert parse_mid_turn_override("hello there") == (None, "hello there")

    def test_mid_turn_override_none_without_body(self) -> None:
        # A bare directive with no message body is not an override.
        assert parse_mid_turn_override("/queue") == (None, "/queue")

    def test_model_and_yolo(self) -> None:
        assert parse_command("/model") == "model"
        assert parse_command("/models") == "model"  # typo-safe alias
        assert parse_command("/yolo") == "yolo"
        assert parse_command("/yolo on") == "yolo"

    def test_command_argument(self) -> None:
        assert parse_command_argument("/yolo on") == "on"
        assert parse_command_argument("/yolo   on  ") == "on"
        assert parse_command_argument("/yolo") == ""

    def test_bare_mid_turn_override_is_flagged(self) -> None:
        # A lone directive must be recognised so it can get a usage hint instead
        # of reaching the model as the literal string "/queue".
        assert is_bare_mid_turn_override("/queue") is True
        assert is_bare_mid_turn_override("  /STEER ") is True
        assert is_bare_mid_turn_override("/queue do this") is False
        assert is_bare_mid_turn_override("/new") is False
        assert is_bare_mid_turn_override("hello") is False

    def test_dashboard_command(self) -> None:
        """Dashboard command requires both /kirocrew and the 'dashboard' subcommand."""
        assert parse_command("/kirocrew dashboard") == "dashboard"
        assert parse_command("/kirocrew dashboard 2h") == "dashboard"
        assert parse_command("/KIROCREW DASHBOARD") == "dashboard"
        assert parse_command("  /kirocrew   dashboard  ") == "dashboard"

    def test_dashboard_command_requires_subcommand(self) -> None:
        """Bare /kirocrew without 'dashboard' is not a command."""
        assert parse_command("/kirocrew") is None
        assert parse_command("/kirocrew help") is None
        assert parse_command("/kirocrew other") is None


class TestBotMentionSuffix:
    """Telegram's own clients append @BotUsername to a slash command in any
    chat with more than one participant/bot -- e.g. /new@KiroCrewBot instead
    of /new. This is standard Bot API client behavior (triggered by
    registering a command menu via set_my_commands, done at gateway startup),
    not something this codebase's UI controls, and it fires in exactly the
    multi-user surface (a Telegram forum-topic supergroup) this integration
    is built to support. Every alias is defined without the suffix, so
    without stripping it every command silently fell through to the LLM as
    ordinary chat text there.

    The strip is gated on the mention matching THIS bot's own username (from
    getMe): Telegram delivers a command addressed to a different bot in the
    same group to every bot present, and stripping any mention unconditionally
    would let e.g. /yolo@OtherBot execute here instead of being ignored."""

    def test_parse_command_strips_bot_mention(self) -> None:
        assert parse_command("/new@KiroCrewBot", "KiroCrewBot") == "new"
        assert parse_command("/compact@KiroCrewBot", "KiroCrewBot") == "compact"
        assert parse_command("/model@KiroCrewBot", "KiroCrewBot") == "model"
        assert parse_command("/yolo@KiroCrewBot", "KiroCrewBot") == "yolo"
        assert parse_command("/link@KiroCrewBot", "KiroCrewBot") == "link"
        assert parse_command("/unlink@KiroCrewBot", "KiroCrewBot") == "unlink"
        assert parse_command("/stop@KiroCrewBot", "KiroCrewBot") == "stop"
        assert parse_command("/help@KiroCrewBot", "KiroCrewBot") == "help"

    def test_parse_command_with_mention_and_trailing_args(self) -> None:
        assert parse_command("/yolo@KiroCrewBot on", "KiroCrewBot") == "yolo"

    def test_parse_command_mention_is_case_insensitive(self) -> None:
        assert parse_command("/NEW@KiroCrewBot", "kirocrewbot") == "new"
        assert parse_command("/NEW@KIROCREWBOT", "KiroCrewBot") == "new"

    def test_unknown_command_with_mention_is_still_unknown(self) -> None:
        # The mention strip must not accidentally widen matching -- a
        # nonexistent command stays unrecognised, mention or not.
        assert parse_command("/frobnicate@KiroCrewBot", "KiroCrewBot") is None

    def test_mid_turn_override_strips_bot_mention(self) -> None:
        assert parse_mid_turn_override("/steer@KiroCrewBot do this instead", "KiroCrewBot") == (
            "steer",
            "do this instead",
        )
        assert parse_mid_turn_override("/queue@KiroCrewBot later", "KiroCrewBot") == (
            "queue",
            "later",
        )

    def test_bare_mid_turn_override_strips_bot_mention(self) -> None:
        assert is_bare_mid_turn_override("/queue@KiroCrewBot", "KiroCrewBot") is True
        assert is_bare_mid_turn_override("/steer@KiroCrewBot", "KiroCrewBot") is True

    def test_mention_pattern_requires_a_leading_at_sign(self) -> None:
        # A bare word after the command must not be mistaken for a mention
        # suffix and stripped -- only a real @-prefixed suffix is a mention.
        assert parse_command("/newsomething", "KiroCrewBot") is None

    def test_a_command_mentioning_a_different_bot_is_not_executed(self) -> None:
        """Security regression: Telegram fans a command addressed to another
        bot in the same group out to every bot present. Stripping the mention
        regardless of whose it is would let it match our own alias and
        execute -- e.g. silently turning on YOLO auto-approval because
        someone else's bot was told to. It must instead stay unrecognised."""
        assert parse_command("/yolo@OtherBot", "KiroCrewBot") is None
        assert parse_command("/yolo@OtherBot on", "KiroCrewBot") is None
        assert parse_command("/stop@OtherBot", "KiroCrewBot") is None
        assert parse_mid_turn_override("/steer@OtherBot do this", "KiroCrewBot") == (
            None,
            "/steer@OtherBot do this",
        )
        assert is_bare_mid_turn_override("/queue@OtherBot", "KiroCrewBot") is False

    def test_a_mention_is_never_stripped_before_our_username_is_known(self) -> None:
        """Before getMe() resolves at startup, bot_username is "" -- the
        default every caller uses. No mention can be verified as ours yet, so
        none should be treated as ours (fail closed, not open)."""
        assert parse_command("/new@KiroCrewBot") is None
        assert parse_command("/yolo@KiroCrewBot") is None

    def test_dashboard_command_strips_bot_mention(self) -> None:
        assert parse_command("/kirocrew@KiroCrewBot dashboard", "KiroCrewBot") == "dashboard"
        # A mention naming a different bot is not ours -- fail closed.
        assert parse_command("/kirocrew@OtherBot dashboard", "KiroCrewBot") is None


class TestParseDashboardTtl:
    def test_default_ttl(self) -> None:
        """Default is 1 hour when no TTL specified."""
        assert parse_dashboard_ttl("/kirocrew dashboard") == 3600

    def test_hours(self) -> None:
        assert parse_dashboard_ttl("/kirocrew dashboard 2h") == 7200
        assert parse_dashboard_ttl("/kirocrew dashboard 5H") == 18000

    def test_minutes(self) -> None:
        assert parse_dashboard_ttl("/kirocrew dashboard 30m") == 1800
        assert parse_dashboard_ttl("/kirocrew dashboard 90M") == 5400

    def test_invalid_ttl_uses_default(self) -> None:
        """Invalid TTL format falls back to 1 hour."""
        assert parse_dashboard_ttl("/kirocrew dashboard xyz") == 3600
        assert parse_dashboard_ttl("/kirocrew dashboard") == 3600


class TestFormatTtl:
    def test_exact_hours(self) -> None:
        assert format_ttl(3600) == "1h"
        assert format_ttl(7200) == "2h"

    def test_minutes_only(self) -> None:
        assert format_ttl(1800) == "30m"
        assert format_ttl(60) == "1m"

    def test_mixed_never_truncates(self) -> None:
        """90m must NOT display as '1h' -- the link lives 1.5h."""
        assert format_ttl(5400) == "1h 30m"
        assert format_ttl(3660) == "1h 1m"

    def test_sub_minute_floors_to_zero_minutes(self) -> None:
        # parse_duration never yields <60s, but the formatter stays total.
        assert format_ttl(0) == "0m"
        assert format_ttl(59) == "0m"


class TestCommandCatalogue:
    """COMMAND_SPEC is the one source behind /help AND the Bot API menu."""

    def test_help_card_lists_every_spec_row(self) -> None:
        help_text = build_help_text()
        for name, desc in COMMAND_SPEC:
            assert f"/{name} — {desc}" in help_text

    def test_every_spec_row_is_a_real_command(self) -> None:
        # A menu entry the parser does not recognise would send the user's tap
        # straight to the model as chat text.
        for name, _desc in COMMAND_SPEC:
            assert parse_command(f"/{name}") is not None, name

    def test_menu_excludes_body_taking_directives(self) -> None:
        # The Telegram client SENDS a menu entry on tap, so /queue and /steer —
        # which are prefixes needing a message body — must not be listed.
        names = {name for name, _ in COMMAND_SPEC}
        assert "queue" not in names and "steer" not in names
        # They stay documented in the help card's footer.
        assert "/queue <msg>" in build_help_text()

    def test_payload_matches_bot_api_shape(self) -> None:
        payload = bot_command_payload()
        assert payload[0] == {"command": "new", "description": "Start a fresh conversation"}
        assert len(payload) == len(COMMAND_SPEC)
        for row in payload:
            assert set(row) == {"command", "description"}
            assert row["command"] == row["command"].lower()
            assert row["command"].lstrip("/") == row["command"]  # no leading slash
            assert 1 <= len(row["command"]) <= 32
            assert 1 <= len(row["description"]) <= 256

    def test_malformed_row_is_dropped_not_sent(self, monkeypatch: Any) -> None:
        # Telegram rejects the WHOLE array on one bad row, so a malformed entry
        # must be skipped rather than cost the user the entire menu.
        from kiro_crew.telegram import commands as tg_commands

        monkeypatch.setattr(
            tg_commands,
            "COMMAND_SPEC",
            (("new", "ok"), ("Bad-Name", "rejected by the Bot API"), ("blank", "")),
        )
        assert tg_commands.bot_command_payload() == [{"command": "new", "description": "ok"}]


class TestConversationState:
    def test_gen_starts_at_zero_and_bumps(self) -> None:
        s = ConversationState()
        assert s.current_gen(1) == 0
        assert s.bump_gen(1) == 1
        assert s.current_gen(1) == 1

    def test_awaiting_flag_roundtrip(self) -> None:
        s = ConversationState()
        assert s.is_awaiting(1) is False
        s.set_awaiting(1)
        assert s.is_awaiting(1) is True
        s.clear_awaiting(1)
        assert s.is_awaiting(1) is False

    def test_bump_gen_clears_awaiting(self) -> None:
        s = ConversationState()
        s.set_awaiting(1)
        s.bump_gen(1)
        assert s.is_awaiting(1) is False

    def test_maybe_rotate_first_message_no_rotate(self) -> None:
        s = ConversationState()
        assert s.maybe_rotate(1, 1000.0, idle_minutes=30) is False
        assert s.current_gen(1) == 0

    def test_maybe_rotate_idle_bumps_gen(self) -> None:
        s = ConversationState()
        s.maybe_rotate(1, 1000.0, idle_minutes=30)
        assert s.maybe_rotate(1, 1000.0 + 31 * 60, idle_minutes=30) is True
        assert s.current_gen(1) == 1

    def test_maybe_rotate_records_activity_without_rotating(self) -> None:
        s = ConversationState()
        s.maybe_rotate(1, 1000.0, idle_minutes=30)
        assert s.maybe_rotate(1, 1000.0 + 60, idle_minutes=30) is False
        assert s.current_gen(1) == 0


# ── renderer.py helpers ────────────────────────────────────────────────────


class TestSplitText:
    def test_short_text_single_chunk(self) -> None:
        assert _split_text("hello", TELEGRAM_CHUNK_LIMIT) == ["hello"]

    def test_long_text_chunks_within_limit(self) -> None:
        text = "\n\n".join("para " + "x" * 500 for _ in range(20))
        chunks = _split_text(text, TELEGRAM_CHUNK_LIMIT)
        assert len(chunks) > 1
        assert all(len(c) <= TELEGRAM_CHUNK_LIMIT for c in chunks)

    def test_no_content_lost_when_hard_split(self) -> None:
        text = "y" * (TELEGRAM_CHUNK_LIMIT * 2 + 100)  # no break points
        chunks = _split_text(text, TELEGRAM_CHUNK_LIMIT)
        assert all(len(c) <= TELEGRAM_CHUNK_LIMIT for c in chunks)
        assert "".join(chunks) == text

    def test_split_markdown_keeps_fences_balanced_and_escaped(self) -> None:
        # A fenced code block longer than the limit must split into chunks that
        # each carry balanced ``` fences, so the per-chunk HTML pass wraps the
        # code in <pre> and escapes <,>,& instead of leaking a literal ``` and
        # 400-ing the send.
        code = "\n".join(f"row <{i}> & 'v'" for i in range(200))
        full = f"code:\n\n```python\n{code}\n```\n\ndone"
        chunks = _split_markdown(full, 400)
        assert len(chunks) > 1
        assert all(ch.count("```") % 2 == 0 for ch in chunks)  # balanced fences
        htmls = [_md_to_telegram_html(ch) for ch in chunks]
        assert all("```" not in h for h in htmls)  # no literal fence leaked
        assert any("<pre>" in h and "&lt;" in h for h in htmls)  # wrapped + escaped


_TAG_SCAN_RE = __import__("re").compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)[^>]*>")


def _html_is_balanced(html_text: str) -> bool:
    """True when every opened tag in ``html_text`` is closed.

    Mirrors what Telegram's parser rejects: an unmatched start tag produces
    "Can't find end tag corresponding to start tag ...".
    """
    stack: list[str] = []
    for m in _TAG_SCAN_RE.finditer(html_text):
        closing, name = m.group(1), m.group(2).lower()
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == name:
                    del stack[i]
                    break
        else:
            stack.append(name)
    return not stack


class TestTruncateHtmlSafe:
    """Tag-safe truncation of rendered Telegram HTML.

    The production failure this guards: a blind ``text[:4096]`` on rendered HTML
    cut between ``<code>`` and ``</code>``, so Telegram rejected the whole edit
    with 400 "Can't find end tag corresponding to start tag \"code\"".
    """

    def test_short_text_unchanged(self) -> None:
        assert truncate_html_safe("<b>hi</b>", 100) == "<b>hi</b>"

    def test_closes_open_tag_left_by_the_cut(self) -> None:
        text = "<code>" + "x" * 200 + "</code>"
        out = truncate_html_safe(text, 60)
        assert len(out) <= 60
        assert out.endswith("</code>")
        assert out.count("<code>") == out.count("</code>")

    def test_never_cuts_inside_a_tag(self) -> None:
        # Force the naive cut to land in the middle of the "<code>" open tag.
        text = "abcd<code>zzzz</code>"
        naive = text[:7]
        assert naive == "abcd<co"  # what the old blind slice produced
        out = truncate_html_safe(text, 7)
        assert "<co" not in out.replace("<code>", "")
        assert out == "abcd"

    def test_never_cuts_inside_an_entity(self) -> None:
        text = "ab&amp;cd" * 20
        out = truncate_html_safe(text, 4)
        assert not out.endswith("&")
        assert out == "ab"

    def test_nested_tags_closed_innermost_first(self) -> None:
        text = "<blockquote><b>" + "y" * 200 + "</b></blockquote>"
        out = truncate_html_safe(text, 80)
        assert len(out) <= 80
        assert out.endswith("</b></blockquote>")

    def test_result_always_within_limit(self) -> None:
        text = "<pre>" + "&lt;" * 500 + "</pre>"
        for limit in (16, 64, 256, 1024):
            out = truncate_html_safe(text, limit)
            assert len(out) <= limit, f"limit={limit} produced {len(out)}"

    def test_never_emits_unclosed_tags_when_closers_do_not_fit(self) -> None:
        # Regression (HIGH): a fixed 3-iteration reserve loop bailed to a bare
        # prefix here, leaving <b> and <i> unclosed -- the exact
        # "Can't find end tag" 400 this helper exists to prevent. A 4th pass
        # would have converged, so the budget, not the algorithm, was the bug.
        text = "<b><i><u><s><code>WXYZ</code></s></u></i></b>"
        out = truncate_html_safe(text, 37)
        assert len(out) <= 37
        assert _html_is_balanced(out), f"unclosed tags in {out!r}"
        assert out == "<b><i><u><s></s></u></i></b>"

    def test_entity_backoff_cannot_strand_the_cut_inside_a_tag(self) -> None:
        # Regression: backing out of a raw `&` in an attribute value used to drag
        # the cut into the middle of a COMPLETE tag, emitting `<a href="u?x=1`.
        text = '<a href="u?x=1&y=2">Z'
        out = truncate_html_safe(text, 20)
        assert len(out) <= 20
        assert _html_is_balanced(out)
        assert "<a" not in out or out.count("<a") == out.count(">")

    def test_prefers_a_shorter_closed_prefix_over_collapsing_to_empty(self) -> None:
        # Quality: the old reserve heuristic overshot to "" even when a closed
        # prefix fit.
        assert truncate_html_safe("<b><i>DEEP</i></b>", 12) == "<b></b>"

    def test_invariants_hold_across_a_balanced_corpus(self) -> None:
        import re as _re

        closers_only = _re.compile(r"^(?:</[a-zA-Z][a-zA-Z0-9-]*>)*$")

        def is_prefix_plus_closers(doc: str, out: str) -> bool:
            # I3 needs SOME valid split, not the greedy longest common prefix:
            # a closer's leading "<" can coincide with the doc's next "<".
            return any(
                out[:k] == doc[:k] and closers_only.match(out[k:]) for k in range(len(out), -1, -1)
            )

        corpus = [
            "<b><i><u><s><code>WXYZ</code></s></u></i></b>",
            "<blockquote><b>x</b></blockquote>",
            "<pre>" + "&lt;" * 60 + "</pre>",
            '<a href="https://e.com/a?b=1&amp;c=2">link</a>text',
            "<b>" * 40 + "X" + "</b>" * 40,
            "plain text only",
            "<blockquote>q</blockquote>" * 20,
        ]
        for doc in corpus:
            assert _html_is_balanced(doc), "fixture must itself be balanced"
            for limit in range(0, len(doc) + 3):
                out = truncate_html_safe(doc, limit)
                assert len(out) <= limit, f"I1: {doc[:30]!r} limit={limit}"
                assert _html_is_balanced(out), f"I2: {doc[:30]!r} limit={limit} -> {out!r}"
                assert is_prefix_plus_closers(doc, out), f"I3: {doc[:30]!r} limit={limit}"


class TestApiDurationMetric:
    """The histogram must measure what it claims: caller block time on outbound
    calls only. All three defects below shipped in the first cut of this metric.
    """

    def _record_calls(self, monkeypatch: Any) -> list[tuple[str, float, dict]]:
        seen: list[tuple[str, float, dict]] = []

        class _Rec:
            def histogram(self, name: str, value: float, *, unit: str = "ms", attrs=None, **kw):
                seen.append((name, value, dict(attrs or {})))

        # Patch the name in the CLIENT's namespace: get_recorder is imported at
        # module scope there, so patching metrics.provider would not be seen.
        monkeypatch.setattr("kiro_crew.telegram.client.get_recorder", lambda: _Rec(), raising=True)
        return seen

    def test_long_poll_is_not_recorded(self, monkeypatch: Any) -> None:
        # getUpdates blocks ~30s by design and runs back-to-back forever. The
        # Telemetry surface does not split on `method`, so recording it buried
        # the 50-500ms outbound distribution under a permanent 30000ms mode.
        seen = self._record_calls(monkeypatch)
        _record_api_duration("editMessageText", 120.0, ok=True, err_code=None)
        assert [m for _, _, a in seen for m in [a["method"]]] == ["editMessageText"]
        assert all(a["method"] != "getUpdates" for _, _, a in seen)

    def test_timeout_gets_its_own_outcome(self, monkeypatch: Any) -> None:
        # Transport failures used to record NOTHING, hiding the longest stalls.
        seen = self._record_calls(monkeypatch)
        _record_api_duration("editMessageText", 30000.0, ok=False, err_code=None, timed_out=True)
        assert seen and seen[-1][2]["outcome"] == "timeout"

    def test_outcome_mapping_is_low_cardinality(self, monkeypatch: Any) -> None:
        seen = self._record_calls(monkeypatch)
        _record_api_duration("sendMessage", 1.0, ok=True, err_code=None)
        _record_api_duration("sendMessage", 1.0, ok=False, err_code=400)
        _record_api_duration("sendMessage", 1.0, ok=False, err_code=429)
        _record_api_duration("sendMessage", 1.0, ok=False, err_code=None, timed_out=True)
        assert [a["outcome"] for _, _, a in seen] == [
            "ok",
            "error",
            "rate_limited",
            "timeout",
        ]
        # chat_id / description must never become attributes (unbounded values).
        assert all(set(a) == {"method", "outcome"} for _, _, a in seen)


class TestCapText:
    def test_plaintext_is_plain_sliced(self) -> None:
        text = "z" * (TELEGRAM_MAX_TEXT + 50)
        assert _cap_text(text, None) == text[:TELEGRAM_MAX_TEXT]

    def test_html_is_tag_safe_capped(self) -> None:
        # Oversize HTML whose 4096 boundary falls inside a code span.
        text = "<code>" + "q" * (TELEGRAM_MAX_TEXT + 100) + "</code>"
        out = _cap_text(text, "HTML")
        assert len(out) <= TELEGRAM_MAX_TEXT
        assert out.count("<code>") == out.count("</code>")

    def test_under_limit_untouched_for_both_modes(self) -> None:
        assert _cap_text("<b>ok</b>", "HTML") == "<b>ok</b>"
        assert _cap_text("ok", None) == "ok"


class TestRenderedBudget:
    """Splitting must budget the RENDERED HTML, not the pre-escape source.

    ``html.escape`` inflates ``&`` to ``&amp;`` (+4) and ``<`` to ``&lt;`` (+3),
    so a chunk that fits a source budget can render past Telegram's hard cap --
    which is what produced the oversize HTML in the first place.
    """

    def test_gate_says_plain_text_provably_fits(self) -> None:
        assert _may_exceed_rendered("just some plain prose", 4000) is False

    def test_gate_flags_escape_heavy_text(self) -> None:
        # Each "<" costs +3 on render; 900 of them blow a 1000-char cap.
        assert _may_exceed_rendered("<" * 900, 1000) is True

    def test_gate_flags_link_heavy_text(self) -> None:
        links = "[t](https://example.com/x)" * 40
        assert _may_exceed_rendered(links, len(links) + 10) is True

    def test_gate_refuses_to_guess_for_tag_producing_markup(self) -> None:
        # Regression: the gate used to model only html.escape + links, so these
        # shapes returned False ("provably fits") while rendering far past the
        # cap -- oversize HTML then reached the client and lost its tail.
        # Measured source -> rendered at cap 4000: blockquote 1000->5600,
        # heading 2000->4500, italic 3600->8100, bold 3720->5580,
        # inline code 2800->10500.
        cap = 4000
        shapes = {
            "blockquote": "> q\n\n" * 200,
            "heading": "# x\n" * 500,
            "italic": "*x* " * 900,
            "bold": "**x** " * 620,
            "inline_code": "`x` " * 700,
            "link": "[t](https://e.com/p) " * 180,
        }
        for name, text in shapes.items():
            assert len(text) < cap, f"{name} fixture must be under the cap"
            assert (
                _may_exceed_rendered(text, cap) is True
            ), f"{name}: gate returned False but renders to {_rendered_len(text)}"

    def test_gate_false_always_means_it_really_fits(self) -> None:
        # The load-bearing invariant: a False lets the caller SKIP the real
        # render, so it must never be wrong. Over-returning True is safe.
        import random

        rng = random.Random(99)
        toks = [
            "> q\n\n",
            "# h\n",
            "text ",
            "a ",
            "**b** ",
            "`c` ",
            "[l](https://e/x) ",
            "&",
            "<x> ",
            "'q' ",
            '"d" ',
            "_i_ ",
            "- b\n",
        ]
        cap = 4000
        for _ in range(600):
            text = "".join(rng.choice(toks) for _ in range(rng.randint(1, 700)))
            if len(text) >= cap:
                text = text[: cap - 1]
            if _may_exceed_rendered(text, cap) is False:
                assert _rendered_len(text) <= cap, (
                    f"gate said fits but rendered {_rendered_len(text)} > {cap}: " f"{text[:80]!r}"
                )

    def test_gate_still_short_circuits_plain_prose(self) -> None:
        # The hot-path shortcut must survive the fix for genuinely plain text.
        assert _may_exceed_rendered("just some plain prose with no markup", 4000) is False

    def test_split_preserves_code_indentation_across_chunks(self) -> None:
        # Regression: the continuation used a bare lstrip(), which ate the
        # leading indentation of the first line of every continuation chunk and
        # silently re-indented split code blocks. Render-aware splitting fires on
        # more shapes, making that pre-existing corruption much easier to hit.
        body = "\n".join(f'    if a["k{i}"] < b & c:   # <indented>' for i in range(120))
        src = f"```python\n{body}\n```"
        chunks = _split_markdown_bounded(src, 800)
        assert len(chunks) > 1
        for ch in chunks[1:]:
            code_lines = [ln for ln in ch.split("\n") if ln.strip() and not ln.startswith("```")]
            assert code_lines, "continuation chunk should carry code"
            assert code_lines[0].startswith(
                "    "
            ), f"indentation lost on continuation: {code_lines[0][:60]!r}"

    def test_shrinks_to_the_floor_instead_of_giving_up_early(self) -> None:
        # Regression: a fixed pass budget returned still-oversize chunks, which
        # the client backstop then truncated -- silently dropping content. Only
        # the _MIN_SPLIT_LIMIT floor may yield oversize chunks.
        cap = 4000
        for src in (
            "&" * 5000,
            "```\n" + "&" * 3000 + "\n```",
            "\n".join("q" * 40 + "&" for _ in range(400)),
        ):
            chunks = _split_markdown_bounded(src, cap)
            worst = max(_rendered_len(c) for c in chunks)
            assert worst <= cap, f"still oversize at {worst} for {src[:24]!r}"

    def test_terminates_when_content_cannot_fit_the_cap(self) -> None:
        # cap below what any _MIN_SPLIT_LIMIT-sized chunk can render to: must
        # return (worst-effort) rather than loop forever.
        chunks = _split_markdown_bounded("&" * 5000, 500)
        assert chunks and max(_rendered_len(c) for c in chunks) > 500

    def test_bounded_split_keeps_every_chunk_renderable(self) -> None:
        # Escape-dense code: source-only budgeting overflows the rendered cap.
        code = "\n".join(f'a["k{i}"] = b<c> & d>e "f" {i}' for i in range(400))
        full = f"here:\n\n```python\n{code}\n```\n\ndone"
        cap = 1000
        chunks = _split_markdown_bounded(full, cap)
        assert len(chunks) > 1
        for ch in chunks:
            assert _rendered_len(ch) <= cap, f"chunk renders to {_rendered_len(ch)} > cap {cap}"

    def test_old_source_only_split_would_have_overflowed(self) -> None:
        # Mutation-style guard: proves the new path is doing real work by
        # showing the previous strategy (source budget only) overflows here.
        code = "\n".join(f'x <{i}> & "{i}" & y' for i in range(300))
        full = f"```\n{code}\n```"
        cap = 1200
        source_only = _split_markdown(full, cap)
        assert any(
            _rendered_len(c) > cap for c in source_only
        ), "fixture no longer reproduces the inflation bug"
        bounded = _split_markdown_bounded(full, cap)
        assert all(_rendered_len(c) <= cap for c in bounded)

    def test_no_content_lost_by_bounded_split(self) -> None:
        text = "\n\n".join(f"para {i} with & and <tag>" for i in range(60))
        chunks = _split_markdown_bounded(text, 800)
        joined = " ".join(chunks)
        for i in range(60):
            assert f"para {i} " in joined or f"para {i}\n" in joined


class TestInlineKeyboard:
    def test_none_when_no_options(self) -> None:
        assert build_inline_keyboard([]) is None

    def test_callback_data_is_index_only_and_byte_safe(self) -> None:
        # Multi-byte (CJK) labels must not blow the 64-byte callback_data cap.
        kb = build_inline_keyboard(["开始实现 Tier 0 的完整方案很长的选项文字", "B"])
        assert kb is not None
        for row in kb["inline_keyboard"]:
            for btn in row:
                assert btn["callback_data"].startswith("opt:")
                assert len(btn["callback_data"].encode("utf-8")) <= 64

    def test_two_buttons_per_row(self) -> None:
        kb = build_inline_keyboard(["a", "b", "c"])
        assert kb is not None
        assert len(kb["inline_keyboard"][0]) == 2
        assert len(kb["inline_keyboard"][1]) == 1


class TestExtractOptions:
    def test_trailing_options_extracted(self) -> None:
        body, opts = _extract_options("Answer here\n\n[OPTIONS: Yes | No | Maybe]")
        assert body == "Answer here"
        assert opts == ["Yes", "No", "Maybe"]

    def test_no_options(self) -> None:
        body, opts = _extract_options("just text")
        assert body == "just text"
        assert opts == []

    def test_partial_streaming_fragment_hidden(self) -> None:
        body, opts = _extract_options("text so far [OPTIONS: Ye")
        assert "[OPTIONS" not in body
        assert opts == []

    def test_unterminated_options_tag_is_not_redos(self) -> None:
        # Regression (py/polynomial-redos): a plain greedy ``.*`` body could
        # consume a "[" that ALSO starts the outer "[OPTIONS:" literal, so over
        # text with many "[OPTIONS:" prefixes search() re-explored the body from
        # each position — polynomial. The tempered body
        # (?:[^[]|\[(?!OPTIONS:))* forbids only a re-occurring "[OPTIONS:", so
        # the body is unambiguous (linear). A whitespace-padded unterminated tag
        # and many repeated "[OPTIONS:" prefixes (the real pump) must both return
        # promptly.
        import time

        for evil in (
            "[OPTIONS:" + ("\t" * 200_000) + "x",
            "[OPTIONS:" * 100_000 + "x",
        ):
            start = time.perf_counter()
            body, opts = _extract_options(evil)
            elapsed = time.perf_counter() - start
            assert elapsed < 1.0, f"_extract_options took {elapsed:.2f}s (possible ReDoS)"
            assert opts == []


# ── transport.py: deny-by-default auth + capabilities + inbound ─────────────


class TestTransportAuth:
    """A Telegram bot is globally reachable, so auth is deny-by-default."""

    def _msg(self, uid: str) -> InboundMessage:
        return InboundMessage(channel_type="telegram", user_id=uid, conversation_id=uid, text="hi")

    def test_empty_allowlist_denies_everyone(self) -> None:
        t = TelegramTransport(FakeClient())  # type: ignore[arg-type]
        assert t.authorize(self._msg("8743158320")) is False

    def test_listed_user_allowed(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("8743158320")) is True

    def test_unlisted_user_denied(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("999")) is False

    def test_empty_user_id_denied(self) -> None:
        t = TelegramTransport(FakeClient(), allowed_user_ids=[8743158320])  # type: ignore[arg-type]
        assert t.authorize(self._msg("")) is False

    def test_capabilities(self) -> None:
        assert TELEGRAM_CAPABILITIES.streaming is True
        assert TELEGRAM_CAPABILITIES.edit is True
        assert TELEGRAM_CAPABILITIES.max_message_chars == TELEGRAM_CHUNK_LIMIT
        assert TELEGRAM_CAPABILITIES.max_buttons == 25


class TestTransportReceive:
    def _run_receive(self, allowed: list[int], inbound: TelegramInbound) -> list[InboundMessage]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        t = TelegramTransport(FakeClient(), allowed_user_ids=allowed, dispatch=_dispatch)  # type: ignore[arg-type]
        asyncio.run(t.receive(inbound))
        return dispatched

    def test_authorized_message_dispatched(self) -> None:
        inbound = TelegramInbound(chat_id=7, user_id=7, text="hello", chat_type="private")
        out = self._run_receive([7], inbound)
        assert len(out) == 1
        assert out[0].channel_type == "telegram"
        assert out[0].user_id == "7"
        assert out[0].text == "hello"

    def test_unauthorized_message_dropped(self) -> None:
        inbound = TelegramInbound(chat_id=9, user_id=9, text="hello", chat_type="private")
        assert self._run_receive([7], inbound) == []

    def test_non_text_message_dropped(self) -> None:
        inbound = TelegramInbound(chat_id=7, user_id=7, text="")
        assert self._run_receive([7], inbound) == []

    def test_non_private_chat_dropped(self) -> None:
        # A bot added to a group must not run a turn (its reply would land in
        # the group, leaking tool output to non-allowlisted members) even for
        # an allow-listed sender. Fail closed on any non-private chat.
        for ct in ("group", "supergroup", "channel", ""):
            inbound = TelegramInbound(chat_id=-100, user_id=7, text="hi", chat_type=ct)
            assert self._run_receive([7], inbound) == []


# ── renderer.py: streaming + finalization ───────────────────────────────────


class TestRenderer:
    def test_a_streamed_table_reply_still_goes_out_as_a_rich_message(self) -> None:
        # THE regression this feature exists to prevent. A normal agent reply
        # streams, so _stream_live has already sent a plaintext bubble and set
        # _stream_mid by the time the segment is sealed. Gating the rich path on
        # "_stream_mid is None" therefore skipped every real reply and the table
        # reached the user as literal pipes -- the feature was dead in the only
        # case that matters. Assert the rich send happens even though a bubble
        # was streamed, and that the superseded bubble is deleted so the user is
        # left with exactly one message.
        table = "Here you go:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r._stream_live(force=True)  # force the live bubble to exist
            assert r._stream_mid is not None, "precondition: a bubble streamed"
            await r._seal_current()

        asyncio.run(_go())

        assert len(cli.rich_sent) == 1, "table must be sealed via sendRichMessage"
        assert "| a | b |" in cli.rich_sent[0][0], "raw markdown table is passed through"
        assert cli.deleted, "the superseded plaintext bubble must be deleted"
        assert r._stream_mid is None, "stream id cleared after the bubble is dropped"

    def test_a_table_reply_falls_back_to_html_when_rich_is_unavailable(self) -> None:
        # If sendRichMessage fails, the streamed bubble must survive and be
        # sealed the legacy way. Losing the answer is far worse than a table
        # that degrades to literal pipes, so the delete must NOT have happened.
        table = "| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "rich send reported failure"
        assert not cli.deleted, "a failed rich send must never delete the answer"
        assert cli.edits, "the streamed bubble is still sealed via the HTML path"

    def test_a_reply_without_a_table_never_touches_the_rich_api(self) -> None:
        # Rich Messages are reserved for content the HTML subset cannot express.
        # An ordinary reply must take the unchanged HTML path, so the new code
        # adds zero API calls to the common case.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("just a plain **bold** answer, no table here")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "no table -> no rich send"
        assert not cli.deleted, "no table -> the streamed bubble is edited, not replaced"

    def test_table_detection_needs_a_separator_row(self) -> None:
        # A line of pipes alone is not a table (prose, or a code sample), and
        # sending it as rich would reflow it. Only a header + separator pair is.
        assert _has_table("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert _has_table("| a | b |\n|:---|---:|\n| 1 | 2 |")  # alignment colons
        assert not _has_table("pipes | in | prose are not a table")
        assert not _has_table("| a | b |\nno separator row follows")

    def test_table_detection_accepts_gfm_tables_without_outer_pipes(self) -> None:
        # GFM does not require leading/trailing pipes. Anchoring detection on a
        # leading `|` silently missed these and shipped them as literal pipes.
        assert _has_table("a | b\n--- | ---\n1 | 2")
        assert _has_table("a | b\n---|---\n1 | 2")
        assert _has_table("  a | b\n  --- | ---\n  1 | 2")  # indented
        # A pipe-bearing sentence above a horizontal rule is NOT a table: the
        # separator row has no pipe, so it stays on the ordinary HTML path.
        assert not _has_table("cost | benefit analysis\n---------------------")

    def test_table_detection_requires_matching_cell_counts(self) -> None:
        # THE bug: a header row glued to leading prose has one cell too many, so
        # no GFM parser sees a table. Claiming one anyway sends the block down
        # the rich path, where the server renders it as a single paragraph --
        # newlines collapsed, every pipe literal. Counting cells is what keeps
        # that content on the monospace path instead.
        assert not _has_table("Here you go:| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert not _has_table("a | b | c\n--- | ---\n1 | 2")  # malformed, 3 vs 2
        assert _has_table("| a |\n| --- |\n| 1 |")  # single column is still a table
        assert _has_table("| a\\|b | c |\n| --- | --- |\n| 1 | 2 |")  # escaped pipe
        # A one-cell separator must not promote the sentence above it to a table.
        assert not _has_table("just prose\n|---|")

    def test_table_detection_counts_cells_by_escape_parity(self) -> None:
        # `\|` is cell content, but `\\` is a literal backslash that leaves the
        # NEXT pipe a real boundary. A fixed-width lookbehind cannot tell those
        # apart: it reads the second backslash of an even run as an escape, merges
        # two cells, and can make a malformed header match its delimiter -- which
        # would route it to the rich path and flatten it.
        assert _row_cell_count(r"| a\|b | c |") == 2, "escaped pipe stays inside its cell"
        assert _row_cell_count("| a\\\\ | b |") == 2, "even backslash run does not escape"
        assert _row_cell_count(r"| a\\\|b | c |") == 2, "odd run after a pair escapes again"
        # The header below is 3 cells against a 2-cell delimiter once parity is
        # honoured, so it must NOT be claimed as a table.
        assert not _has_table("a\\\\ | b | c\n| --- | --- |\n| 1 | 2 |")

    def test_delimiter_cells_follow_the_observed_server_rule(self) -> None:
        # Every case here was checked against the live API by reading the echoed
        # `rich_message.blocks`, because a spec reading and the server disagree.
        # An EMPTY delimiter cell is rejected by the server (`paragraph`), so it
        # must not take the rich path.
        assert not _has_table("| a | b |\n| --- | |\n| 1 | 2 |")
        # A BROKEN dash run is accepted by the server (`table`), so demanding a
        # contiguous run would degrade a table it renders fine into monospace.
        assert _has_table("| a | b |\n| - - | --- |\n| 1 | 2 |")
        # Ordinary spellings, also confirmed as `table`.
        assert _has_table("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert _has_table("| a | b |\n| - | - |\n| 1 | 2 |")

    def test_a_glued_table_is_sealed_verbatim_instead_of_reflowed(self) -> None:
        # A header row sharing a line with prose has one cell too many, so no GFM
        # parser sees a table. Sending it as rich would render one paragraph with
        # every newline collapsed. Whether the extra cell is prose or a delimiter
        # the author got wrong is NOT decidable from the text, so nothing is
        # rewritten: the monospace seal reproduces the block as written.
        glued = "Here is the table you asked for:| a | b |\n| --- | --- |\n| 1 | 2 |"
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(glued)
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        assert cli.rich_sent == [], "non-conforming pipe markup must not take the rich path"
        assert not cli.deleted, "the streamed bubble is edited, not replaced"
        sealed = cli.edits[-1][1]
        assert "<pre>" in sealed and "</pre>" in sealed, "the pipe block is sealed monospace"
        for row in ("| a | b |", "| --- | --- |", "| 1 | 2 |"):
            assert row in sealed, f"{row} must survive verbatim"

    def test_a_degraded_table_is_sealed_monospace_not_as_ragged_pipes(self) -> None:
        # When rich is unavailable the table still has to go out, but sealing it
        # through the normal HTML path reflows it into ragged escaped pipes.
        # <pre> keeps the columns aligned, so the degraded case stays readable.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.edits[-1][1]
        assert "<pre>" in sealed and "</pre>" in sealed
        assert "| a | b |" in sealed, "the table text survives inside the pre block"

    def test_the_degraded_path_keeps_prose_formatting_around_the_table(self) -> None:
        # Regression: wrapping the WHOLE segment in <pre> made a prose+table
        # reply render worse than before the feature existed -- every bold, link
        # and inline-code span was lost and `**` markers showed up literally.
        # On a server without Rich Messages this is the permanent path, so it
        # must never be a downgrade. Only the table run may be monospace.
        text = "Here is the **summary**:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\nSee `docs` too."
        out = _seal_table_fallback(text)

        assert "<b>summary</b>" in out, "prose keeps its bold"
        assert "<code>docs</code>" in out, "prose keeps its inline code"
        assert "**" not in out, "no literal markdown markers leak to the user"
        # The table is the only monospace run, and it is intact.
        assert out.count("<pre>") == 1
        table_block = out.split("<pre>")[1].split("</pre>")[0]
        assert "| a | b |" in table_block and "| 1 | 2 |" in table_block
        assert "summary" not in table_block, "prose must not be swallowed into the pre"

    def test_the_degraded_path_handles_a_table_with_no_surrounding_prose(self) -> None:
        # The bare-table case must not emit stray empty prose fragments.
        out = _seal_table_fallback("| a | b |\n| --- | --- |\n| 1 | 2 |")
        assert out.startswith("<pre>") and out.endswith("</pre>")
        assert out.count("<pre>") == 1

    def test_a_degraded_mixed_reply_keeps_its_prose_formatting_end_to_end(self) -> None:
        # Pins the SEAL PATH, not just the helper: _seal_current must route the
        # failed-rich case through the mixed-segment renderer. Asserting only on
        # _seal_table_fallback() left the call site free to wrap the whole
        # segment in <pre> again, which is exactly the regression to prevent.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("The **key** result:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live(force=True)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.edits[-1][1]
        assert "<b>key</b>" in sealed, "prose bold survives the degraded seal"
        assert "**" not in sealed, "no literal markdown markers reach the user"
        assert "<pre>" in sealed, "the table run is still monospace"
        assert "key" not in sealed.split("<pre>")[1], "prose not swallowed into the pre"

    def test_a_near_limit_degraded_reply_is_not_truncated_by_pre_overhead(self) -> None:
        # The segment was sized against the PLAIN html render, and <pre> wrapping
        # only adds characters, so on a near-limit reply the wrapped form can
        # spill past the rendered ceiling and have its tail cut by _cap_text().
        # Losing the end of the answer is worse than losing column alignment, so
        # the plain render must win once the wrapped form would overflow.
        cli = FakeClient()
        cli.rich_fails = True
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        # Many small tables: source stays under the plaintext rotate threshold
        # while each table adds <pre></pre> overhead to the wrapped form.
        one = "| a | b |\n| - | - |\n| 1 | 2 |\n\n"
        text = one * (r._limit() // len(one) - 1)
        assert len(_seal_table_fallback(text)) > r._rendered_limit(), (
            "precondition: the wrapped form must overflow for this to test anything"
        )
        assert len(_md_to_telegram_html(text)) <= r._rendered_limit(), (
            "precondition: the plain render must fit"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)
            await r._seal_current()

        asyncio.run(_go())

        sealed = cli.final_text()
        assert len(sealed) <= r._rendered_limit(), "degraded seal respects the rendered cap"
        assert "<pre>" not in sealed, "overflowing wrap is dropped for the plain render"

    def test_fenced_table_markup_costs_a_rich_send_but_never_wrong_output(self) -> None:
        # Detection is deliberately fence-agnostic: a fenced sample of table
        # markup does take the rich path, but Rich Markdown parses the fence
        # itself and renders it as the code block it is. The cost is one extra
        # send, not wrong output -- and it buys not maintaining a second
        # CommonMark fence parser beside the HTML renderer's own.
        fenced = "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone"
        assert _has_table(fenced), "detection does not screen fences"
        # A real table after a closed fence is of course still found.
        assert _has_table(fenced + "\n\n| x | y |\n| --- | --- |\n| 1 | 2 |")

    def test_the_degraded_path_never_splits_a_code_fence(self) -> None:
        # The safety net lives on the FALLBACK side: a fenced segment renders
        # whole via the normal path, so no fence can be torn in half.
        fenced = "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone"
        out = _seal_table_fallback(fenced)
        assert out == _md_to_telegram_html(fenced), "rendered whole, unsplit"
        assert "&#x60;" not in out and "```" not in out, "no literal fence markers leak"

    def test_replacing_a_streamed_bubble_does_not_ping_the_user_twice(self) -> None:
        # send-rich-then-delete means two messages exist briefly and Telegram
        # notifies for each. The bubble already pinged, so the replacement must
        # be silent -- otherwise every table reply buzzes twice where main
        # buzzed once, a regression introduced by replacing instead of editing.
        cli = FakeClient()
        r = TelegramRenderer(cli, 71, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            await r._stream_live()  # streams the bubble -> user is pinged
            await r._seal_current()

        asyncio.run(_go())
        assert cli.rich_sent, "rich send happened"
        assert cli.rich_silent == [True], "the replacement is silent"

    def test_a_table_that_never_streamed_still_notifies(self) -> None:
        # No bubble streamed -> no earlier ping, so the rich send is the user's
        # ONLY notification. Suppressing it here would deliver the answer
        # silently and the reply would go unnoticed.
        cli = FakeClient()
        r = TelegramRenderer(cli, 72, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            # Throttle out the live frame -- the "segment never streamed"
            # case named in _seal_current's docstring.
            r._last_edit = time.monotonic()
            await r.on_text_chunk("| a | b |\n| --- | --- |\n| 1 | 2 |")
            assert r._stream_mid is None, "precondition: nothing streamed"
            await r._seal_current()

        asyncio.run(_go())
        assert cli.rich_sent, "rich send happened"
        assert cli.rich_silent == [False], "the only message must notify"

    def test_a_fenced_reply_is_never_split_by_the_degraded_path(self) -> None:
        # Splitting renders the pieces with separate _md_to_telegram_html calls,
        # so a cut inside a fence tears the block and leaks its delimiters.
        # Deciding where a fence starts/ends reliably means reimplementing
        # CommonMark as a second parser; declining to split removes the class.
        # These are the exact shapes that each defeated a hand-rolled parser:
        cases = [
            # plain fenced table markup
            "Example:\n\n```\n| a | b |\n| --- | --- |\n| 1 | 2 |\n```\n\ndone",
            # a ~~~ line inside a ``` block (mismatched delimiter)
            "How:\n\n```\n~~~\n| a | b |\n| --- | --- |\n```\n\ndone",
            # an info string on an inner delimiter
            "How:\n\n```\n```python\n| a | b |\n| --- | --- |\n```\n\ndone",
            # a longer opener closed only by a shorter run
            "````\n```\n| a | b |\n| --- | --- |\n",
            # a block fenced with tildes only -- the guard must cover both
            # delimiter characters, not just backticks
            "Example:\n\n~~~\n| a | b |\n| --- | --- |\n| 1 | 2 |\n~~~\n\ndone",
        ]
        for text in cases:
            out = _seal_table_fallback(text)
            assert out == _md_to_telegram_html(text), (
                f"a fenced segment must render whole, unsplit: {text!r}"
            )

    def test_the_degraded_path_still_aligns_tables_with_no_fence_present(self) -> None:
        # The no-split rule must not disable the feature for ordinary replies.
        text = "Here:\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n**after**"
        out = _seal_table_fallback(text)
        assert "<pre>" in out, "a fence-free table still gets monospace alignment"
        assert "<b>after</b>" in out, "prose around it keeps its formatting"

    def test_strip_steering_complete_and_unclosed(self) -> None:
        # Complete marker is removed anywhere in the text.
        out = _strip_steering("BANANA [STEERING steer-x: rephrase] tail")
        assert "STEERING" not in out and out.startswith("BANANA") and out.endswith("tail")
        # UNCLOSED trailing marker (still streaming, no closing "]") is also
        # removed, so the live draft never previews text that on_done strips.
        assert _strip_steering("BANANA\n\n[STEERING steer-abc: interpreted as wanting") == "BANANA"
        # No marker -> unchanged.
        assert _strip_steering("just text") == "just text"

    def _drive(self, events: list[OutputEvent]) -> FakeClient:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            for ev in events:
                await r.dispatch(ev)

        asyncio.run(_go())
        return cli

    def test_rotation_keeps_an_open_fence_open_in_the_retained_tail(self) -> None:
        # Regression: mid-stream the source fence is still open (the model has
        # not emitted its closing ``` yet). _split_markdown balances each chunk
        # with a synthetic closer, which is right for sealed chunks but wrong for
        # the tail we keep streaming into: later tokens would land after that
        # closer, render outside <pre>, and the real closing fence would show up
        # literally.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        open_src = "intro\n\n```python\n" + "\n".join(
            f'    x{i} = a["k{i}"] & b<c>' for i in range(150)
        )
        assert open_src.count("```") % 2 == 1, "fixture must leave the fence open"
        r._buf = [open_src]
        asyncio.run(r._rotate_on_length())
        tail = "".join(r._buf)
        assert not tail.rstrip().endswith(
            "```"
        ), f"synthetic closer retained in streaming tail: {tail[-40:]!r}"

    def test_rotation_preserves_a_real_closing_fence(self) -> None:
        # Control for the above: when the source fence IS closed, the tail must
        # keep its genuine closer.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        closed_src = (
            "intro\n\n```python\n"
            + "\n".join(f'    y{i} = a["k{i}"] & b<c>' for i in range(150))
            + "\n```"
        )
        assert closed_src.count("```") % 2 == 0
        r._buf = [closed_src]
        asyncio.run(r._rotate_on_length())
        assert "".join(r._buf).rstrip().endswith("```")

    def test_streaming_strips_options_and_renders_keyboard(self) -> None:
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t", title="fs_read"),
                OutputEvent(kind=TEXT_CHUNK, text="Hello. "),
                OutputEvent(kind=TEXT_CHUNK, text="Pick.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # Edit-streaming: the finished answer is the last edit, carrying the
        # [OPTIONS:] keyboard; the raw [OPTIONS:] directive is stripped from text.
        final_text = cli.final_text()
        final_kb = cli.final_markup()
        assert final_text == "Hello. Pick."  # [OPTIONS:] stripped
        labels = [b["text"] for row in final_kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_streams_live_via_send_then_edit(self) -> None:
        # Edit-streaming (OpenClaw-style): send one real message, then edit it in
        # place as text arrives. No draft (the ghost/vanish source). The final
        # formatted content is the last edit; the initial send seeds the bubble.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="one "),
                OutputEvent(kind=TEXT_CHUNK, text="two "),
                OutputEvent(kind=TEXT_CHUNK, text="three"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert cli.drafts == []  # no draft preview -> no ghost bubble
        assert cli.sent  # a real message was sent (live seed)
        assert cli.final_text() == "one two three"  # final formatted content

    def test_tool_footer_surfaces_immediately_on_tool_call(self) -> None:
        # A mid-turn tool call surfaces a transient "🔧 {tool}…" footer on the
        # live bubble immediately (force bypasses the edit throttle), so long
        # agentic turns show activity instead of a dead typing indicator.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="Checking the logs. "),
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="grep"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert any("🔧 grep…" in f for f in frames)  # footer shown live

    def test_tool_footer_cleared_by_text_and_absent_from_final(self) -> None:
        # The footer is transient: cleared the moment text resumes, and never
        # part of the sealed/final message (seals read _segment_text, which the
        # footer is deliberately kept out of).
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="fs_read"),
                OutputEvent(kind=TEXT_CHUNK, text="Found it. "),
                OutputEvent(kind=TOOL_CALL, tool_call_id="t2", title="shell"),
                OutputEvent(kind=TEXT_CHUNK, text="All done."),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert any("🔧 fs_read…" in f for f in frames)
        assert any("🔧 shell…" in f for f in frames)
        assert "🔧" not in cli.final_text()  # final is clean
        assert cli.final_text() == "Found it. All done."

    def test_strips_steering_marker_from_output(self) -> None:
        # kiro-cli's inline "[STEERING steer-<id>: …]" ack marker must never leak
        # raw into any posted message. An end-of-stream marker (no continuation
        # text after it) produces NO extra message — just the sealed answer.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="UTC is 09:03. BANANA\n\n"),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="[STEERING steer-f0783769b5c44c5a9b40de895109315f: "
                    "stop, only say BANANA]",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        texts = [t for t, _ in cli.sent]
        assert texts == ["UTC is 09:03. BANANA"]  # sealed clean, no ack tail
        assert "STEERING" not in " ".join(texts)

    def test_steer_seals_at_marker_into_new_bubble(self) -> None:
        # Seal-on-steer: the [STEERING] marker (kiro-cli's in-stream injection
        # point) seals the pre-steer text as its own message; the steered
        # continuation opens a FRESH message headed by a chip. The chip prefers
        # the SUMMARY embedded in the marker (dashboard parity) over the user's
        # own words — those are already on screen as the user's message.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        r.note_steer("stop and say BANANA")  # the dispatcher records the user's words

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="Root at 86% used. "))
            await r.dispatch(OutputEvent(kind=STEER_CONSUMED, text="stop"))
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="BANANA"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 2  # pre-steer bubble sealed + steered continuation
        assert texts[0] == "Root at 86% used."  # frozen pre-steer bubble
        assert "STEERING" not in texts[0] and "STEERING" not in texts[1]
        # The chip heads the new bubble with the MARKER's summary (not the quote).
        assert texts[1].startswith("<blockquote>↪️ stop</blockquote>")
        assert "BANANA" in texts[1] and "used.BANANA" not in texts[1]  # no leak

    def test_end_of_stream_marker_posts_no_tail_bubble(self) -> None:
        # Marker at the very END of the stream (kiri-cli folded the steer but
        # emitted no post-steer text): NO tail message at all. The answer already
        # covered the steer and the user's message carries the reaction receipt —
        # any trailing ack bubble (quote OR summary) is pure noise. Regression
        # for the trailing bubbles seen live on 2026-07-19.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        r.note_steer("顺便看看今天悉尼什么天气")

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="目录总结。天气:多云。"))
            await r.dispatch(
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="\n\n[STEERING steer-a180ae7f: 已并行查询悉尼天气,一并答复。]",
                )
            )
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 1  # only the sealed answer — no ack tail
        assert "STEERING" not in texts[0]
        assert "已并行查询" not in texts[0] and "顺便看看" not in texts[0]

    def test_options_keyboard_survives_length_rotation(self) -> None:
        # Codex finding: [OPTIONS:] must be extracted BEFORE length rotation.
        # A body long enough to rotate must still attach the keyboard to the
        # FINAL message — not strand the options text in a sealed segment and
        # fall into the "…" placeholder branch.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=("line\n" * 1200)),  # > limit
                OutputEvent(kind=TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        kb = cli.final_markup()
        assert kb is not None
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]
        # The options directive never leaks into any posted text.
        all_text = " ".join(t for t, _ in cli.sent) + " ".join(t for _, t, _ in cli.edits)
        assert "[OPTIONS" not in all_text

    def test_long_options_before_streamed_steer_ack_become_keyboard(self) -> None:
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=("x" * 7680)
                    + "\n\n[OPTIONS: Alpha | Bravo | Charlie]"
                    + "\n\n[STEERING steer-7e6a4a0d",
                ),
                OutputEvent(kind=TEXT_CHUNK, text="94314d2db: acknowledged]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )

        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        assert labels == ["Alpha", "Bravo", "Charlie"]
        visible = "\n".join([t for t, _ in cli.sent] + [t for _, t, _ in cli.edits])
        assert "[OPTIONS" not in visible
        assert "[STEERING" not in visible
        assert "steer-7e6a4a0d" not in visible
        assert "94314d2db" not in visible

    def test_tool_only_message_not_orphaned_at_steer_boundary(self) -> None:
        # Codex finding: a tool call BEFORE any assistant text creates a live
        # "🔧 tool…" message. If a steer marker then arrives with no pre-marker
        # text, nothing is sealed — the renderer must KEEP that message id so
        # the steered continuation replaces the transient footer in place,
        # instead of orphaning a permanent tool-footer bubble.
        cli = self._drive(
            [
                OutputEvent(kind=TOOL_CALL, tool_call_id="t1", title="grep"),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="[STEERING steer-abc123: checked] steered answer.",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # Exactly ONE message ever sent (the tool-footer one, reused).
        assert len(cli.sent) == 1
        assert cli.final_text() is not None
        assert "steered answer" in cli.final_text()
        assert "🔧" not in cli.final_text()

    def test_options_only_response_keeps_keyboard(self) -> None:
        # Codex finding: a response that is ONLY "[OPTIONS: A | B]" leaves an
        # empty body after extraction — the placeholder must still carry the
        # keyboard, not silently drop the user's choices.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="[OPTIONS: A | B]"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        assert len(markups) == 1
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_complete_options_straddling_length_cut_stays_intact(self) -> None:
        # Codex finding: a COMPLETE trailing [OPTIONS: A | B] whose text
        # crosses the length-rotation boundary must be detached whole before
        # _split_markdown — a bare split would put "[OPTIO" in one message and
        # "NS: A | B]" in the next, leaking protocol text and losing the
        # keyboard. Body sized so the directive itself straddles the cut.
        body = "x" * 3730  # just under the ~3840 limit; directive crosses it
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=body),
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="\n\nPick one below please.\n\n[OPTIONS: A | B]",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        # The final segment never live-streamed (rotation happened at the very
        # end), so the seal SENDS a fresh message — find the keyboard across
        # both sends and edits rather than via final_markup().
        markups = [m for _, m in cli.sent if m] + [m for _, _, m in cli.edits if m]
        assert len(markups) == 1
        labels = [b["text"] for row in markups[0]["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]
        # The options directive never leaks — whole or split — into any text.
        all_text = " ".join(t for t, _ in cli.sent) + " ".join(t for _, t, _ in cli.edits)
        assert "[OPTIO" not in all_text and "NS: A | B]" not in all_text

    def test_double_marker_chunk_keeps_first_chip(self) -> None:
        # Codex finding: one chunk carrying TWO complete [STEERING] markers must
        # not overwrite the first steer's chip — its continuation seals WITH the
        # chip before the second rotation.
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=(
                        "base answer. "
                        "[STEERING steer-aaa111: first ack] first continuation. "
                        "[STEERING steer-bbb222: second ack] second continuation."
                    ),
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        texts = [t for t, _ in cli.sent]
        assert len(texts) == 3  # base + two steered continuations
        assert "first ack" in texts[1] and "first continuation" in texts[1]
        assert "second ack" in texts[2] and "second continuation" in texts[2]
        assert "STEERING" not in " ".join(texts)

    def test_hr_inside_code_fence_preserved(self) -> None:
        # Codex finding: _strip_hr must not delete a standalone "---" INSIDE a
        # fenced code block (e.g. a YAML document separator).
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text="Config:\n```yaml\na: 1\n---\nb: 2\n```\ndone",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert "---" in cli.final_text()  # YAML separator survives
        # A bare HR OUTSIDE a fence is still stripped.
        cli2 = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="above\n\n---\n\nbelow"),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert "---" not in cli2.final_text()

    def test_live_frames_never_leak_options_markup(self) -> None:
        # Codex finding: a live frame streamed from a chunk that already carries
        # the complete [OPTIONS:] directive must hold it back, not show it.
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text="Pick one.\n\n[OPTIONS: A | B]"),
                OutputEvent(kind=TEXT_CHUNK, text=""),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        frames = [t for t, _ in cli.sent] + [t for _, t, _ in cli.edits]
        assert all("[OPTIONS" not in f for f in frames)
        labels = [b["text"] for row in cli.final_markup()["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_length_rotation_keeps_fences_balanced(self) -> None:
        # Codex finding: length rotation must not cut a fenced code block in
        # half — every sealed message carries balanced fences (via
        # _split_markdown's close-and-reopen).
        big_code = "```python\n" + ("x = 1\n" * 1500) + "```"
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=big_code),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        assert len(cli.sent) >= 2  # rotated at least once
        # Sealed HTML frames render the code as <pre> (fence recognized), and
        # no posted frame carries an odd number of literal fences.
        finals = [t for t, _ in cli.sent]
        assert any("<pre>" in t for t in finals)
        for t in finals:
            assert t.count("```") % 2 == 0

    def test_steer_summary_respects_transport_limit(self) -> None:
        # Codex finding: the no-marker steer summary is prepended BEFORE length
        # rotation, so the final message can never exceed Telegram's cap.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        for i in range(10):
            r.note_steer(f"steer number {i} " + "y" * 100)

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="body " * 780))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        for t, _ in cli.sent:
            assert len(t) <= 4096
        for _, t, _ in cli.edits:
            assert len(t) <= 4096

    def test_oversized_pre_marker_segment_rotates_before_seal(self) -> None:
        # Codex finding: a chunk can deliver over-limit text AND a complete
        # [STEERING] marker together. The pre-marker segment must length-rotate
        # before sealing, or the client truncates it at Telegram's cap.
        big = "word " * 1300  # ~6500 chars > limit
        cli = self._drive(
            [
                OutputEvent(
                    kind=TEXT_CHUNK,
                    text=big + "[STEERING steer-abc123: ack] tail answer.",
                ),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        finals = [t for t, _ in cli.sent]
        assert len(finals) >= 3  # pre-marker rotated into >=2 + continuation
        for t in finals:
            assert len(t) <= 4096  # nothing exceeds the transport cap
        joined = " ".join(finals)
        assert joined.count("word") >= 1290  # no pre-marker content lost
        assert "tail answer" in finals[-1]

    def test_partial_directive_never_split_by_length_rotation(self) -> None:
        # Codex finding: an INCOMPLETE trailing directive crossing the length
        # limit must be detached before splitting and reattached to the tail —
        # never cut in half (which would leak fragments and lose the rotation).
        big = "text " * 1300
        cli = self._drive(
            [
                OutputEvent(kind=TEXT_CHUNK, text=big + "[STEERING steer-ddd444: par"),
                OutputEvent(kind=TEXT_CHUNK, text="tial ack] steered tail."),
                OutputEvent(kind=DONE, stop_reason=""),
            ]
        )
        finals = [t for t, _ in cli.sent]
        joined = " ".join(finals)
        assert "STEERING" not in joined  # directive never leaked, whole or split
        assert "steered tail" in joined  # rotation still happened
        frames = [t for _, t, _ in cli.edits]
        assert all("[STEERING" not in f for f in frames)  # nor on live frames

    def test_seal_resends_when_live_message_deleted(self) -> None:
        # Codex finding: if the user deletes the streamed message mid-turn,
        # both seal edits fail — the final answer (and keyboard) must be
        # re-SENT as a fresh message, never silently lost.
        class _EditsFailClient(FakeClient):
            async def edit_message(self, *a: Any, **kw: Any) -> bool:
                await super().edit_message(*a, **kw)
                return False  # message gone — every edit fails

        cli = _EditsFailClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="final answer"))
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="\n\n[OPTIONS: A | B]"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        # Last SENT message carries the final content + keyboard.
        final_text, final_kb = cli.sent[-1]
        assert "final answer" in final_text
        labels = [b["text"] for row in final_kb["inline_keyboard"] for b in row]
        assert labels == ["A", "B"]

    def test_error_done_renders_error_when_no_text(self) -> None:
        cli = self._drive([OutputEvent(kind=DONE, stop_reason="error")])
        assert "Error" in cli.sent[-1][0]

    def test_close_is_idempotent_after_done(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> int:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))
            n = len(cli.sent)
            await r.close()  # should no-op
            return len(cli.sent) - n

        assert asyncio.run(_go()) == 0

    def test_close_with_failure_reason_replaces_generic_placeholder(self) -> None:
        # A permanent failure's sanitized reason must reach the user instead of
        # the misleading "please try again" text (issue #1831).
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]
        reason = "⚠️ Your account does not have access to model 'x'. Pick one in the picker."

        async def _go() -> None:
            await r.on_turn_start()
            await r.close(failure_reason=reason)

        asyncio.run(_go())
        assert cli.sent[-1][0] == reason
        assert "please try again" not in cli.sent[-1][0]

    def test_close_without_reason_keeps_generic_error_text(self) -> None:
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> None:
            await r.on_turn_start()
            await r.close()

        asyncio.run(_go())
        assert "please try again" in cli.sent[-1][0]

    def test_close_reason_ignored_after_normal_done(self) -> None:
        # A reason arriving after on_done already finalized must not post
        # anything: close() stays a strict no-op post-finalization.
        cli = FakeClient()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES)  # type: ignore[arg-type]

        async def _go() -> int:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hi"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))
            n = len(cli.sent)
            await r.close(failure_reason="⚠️ too late")
            return len(cli.sent) - n

        assert asyncio.run(_go()) == 0


# ── renderer.py: table-aware splitting + rich budget selection ──────────────


class TestTableAwareSplitting:
    def _renderer(self, cli: FakeClient) -> TelegramRenderer:
        return TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]

    def _table(self, rows: int, fill: str = "x", width: int = 80) -> str:
        head = "| id | data |\n| --- | --- |\n"
        return head + "".join(f"| {i:05d} | {fill * width} |\n" for i in range(rows))

    def test_a_table_that_fits_one_rich_message_is_never_split(self) -> None:
        # THE fix for the half-rich/half-pipes defect: a table that overflows
        # the HTML budget used to be cut row-wise, stranding header-less body
        # rows on the literal-pipe path. Sized against the rich budget it is
        # one segment, one sendRichMessage, one rendered table.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(120)
        assert r._limit() < len(table) <= r._rich_limit(), (
            "precondition: overflows the HTML budget, fits the rich budget"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) == 1, "one logical table -> one rich message"
        md = cli.rich_sent[0][0]
        assert "| 00000 |" in md and "| 00119 |" in md, "no row lost"
        assert _has_table(md)

    def test_a_table_over_the_rich_budget_splits_into_table_detected_chunks(self) -> None:
        # When even the rich budget overflows, cuts land at row boundaries and
        # every continuation repeats the header + separator, so EVERY chunk is
        # table-detected and renders rich -- never a ragged pipe-text tail.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(300, fill="y", width=120)
        assert len(table) > r._rich_limit(), "precondition: overflows the rich budget"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) >= 2, "an over-rich-budget table needs several rich sends"
        for md, _, _ in cli.rich_sent:
            assert _has_table(md), "every chunk carries the header, so it seals rich"
            assert len(md) <= r._rich_limit()
        joined = "\n".join(md for md, _, _ in cli.rich_sent)
        for i in (0, 150, 299):
            assert f"| {i:05d} |" in joined, "no row lost across the chunks"

    def test_an_oversize_table_degrades_uniformly_when_rich_is_unavailable(self) -> None:
        # A segment sized against the rich budget can be several HTML messages
        # long. When the rich send fails it must be re-split and shipped whole
        # -- truncation would silently drop rows -- and header repetition keeps
        # every chunk on the <pre> path, so degradation is uniform.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        table = self._table(120, fill="k", width=90)
        assert r._limit() < len(table) <= r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        assert cli.rich_sent == []
        bodies = [e[1] for e in cli.edits if "<pre>" in e[1]]
        bodies += [s[0] for s in cli.sent if "<pre>" in s[0]]
        assert len(bodies) >= 2, "the oversize segment ships as several HTML messages"
        joined = "\n".join(bodies)
        for i in (0, 60, 119):
            assert f"| {i:05d} |" in joined, "no row lost to truncation"
        for b in bodies:
            assert len(b) <= TELEGRAM_MAX_TEXT, "each chunk respects the hard cap"

    def test_split_table_rows_repeats_the_header_on_every_chunk(self) -> None:
        rows = ["| a | b |", "| --- | --- |"] + [f"| {i} | {'z' * 50} |" for i in range(40)]
        chunks = _split_table_rows(rows, 600)
        assert len(chunks) > 1
        for c in chunks:
            assert c.startswith("| a | b |\n| --- | --- |\n")
            assert _has_table(c)
            assert len(c) <= 600
        body = "\n".join(chunks)
        for i in range(40):
            assert body.count(f"| {i} | ") == 1, "each row appears exactly once"

    def test_split_table_rows_cannot_cut_inside_a_single_oversize_row(self) -> None:
        # There is no sub-row boundary that keeps the table valid, so the row
        # splitter returns it oversize; the degraded ladder is what bounds it.
        rows = ["| a |", "| --- |", "| " + "w" * 900 + " |"]
        chunks = _split_table_rows(rows, 400)
        assert len(chunks) == 1
        assert _has_table(chunks[0])

    def test_a_single_monster_row_is_cut_inside_rather_than_truncated(self) -> None:
        # A row bigger than one message has no valid row-boundary cut. The
        # degraded ladder must cut INSIDE it (losing table framing for that row
        # only) rather than shipping an oversize chunk the client backstop
        # would truncate -- the tail of the row has to reach the user.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        marker_head, marker_tail = "ROWSTART", "ROWEND"
        row = f"| {marker_head} {'v' * 6000} {marker_tail} |"
        table = f"| a |\n| --- |\n{row}\n"

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        bodies = [t for _, t, _ in cli.edits] + [t for t, _ in cli.sent]
        joined = "".join(bodies)
        assert marker_head in joined and marker_tail in joined, "both row ends survive"
        assert joined.count("v" * 100) * 100 >= 5900, "the row body ships whole"
        for b in bodies:
            assert len(b) <= TELEGRAM_MAX_TEXT, "no chunk relies on backstop truncation"

    def test_table_aware_split_budgets_prose_against_the_html_cap(self) -> None:
        # Prose around a table seals through the HTML path, so it must keep the
        # rendered budget even while the table beside it rides the rich budget.
        prose = ("lorem ipsum dolor sit amet " * 90).strip()
        table = "| a | b |\n| --- | --- |\n" + "\n".join(
            f"| {i} | {'q' * 100} |" for i in range(40)
        )
        chunks = _split_markdown_table_aware(prose + "\n\n" + table, 1000, len(table) + 10)
        table_chunks = [c for c in chunks if _has_table(c)]
        assert len(table_chunks) == 1, "the table run stays atomic"
        assert table_chunks[0] == table
        prose_chunks = [c for c in chunks if not _has_table(c)]
        assert prose_chunks, "the prose still ships"
        assert all(len(_md_to_telegram_html(c)) <= 1000 for c in prose_chunks)

    def test_table_aware_split_falls_back_to_the_bounded_splitter_for_fences(self) -> None:
        # Fence-bearing text keeps the fence-aware splitter: deciding where a
        # fence ends means growing a second CommonMark parser, and a pipe
        # pattern inside a fence is not a table anyway.
        text = "```\n| a | b |\n| --- | --- |\n" + "x\n" * 500 + "```"
        assert _split_markdown_table_aware(text, 800, 32000) == _split_markdown_bounded(text, 800)

    def test_non_table_content_splits_exactly_as_before(self) -> None:
        # Regression guard for the shared sizing path: replies without a table
        # must take the identical bounded split they always did, sealing each
        # chunk to the same HTML the bounded splitter implies. Markup makes the
        # sealed HTML distinguishable from plaintext live-stream frames.
        text = "para **one**. " * 150 + "\n\n" + "para _two_! " * 250
        assert not _has_table(text)
        cli = FakeClient()
        r = self._renderer(cli)

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(text)
            await r.on_done()

        asyncio.run(_go())

        assert cli.rich_sent == [], "no table -> the rich path is never touched"
        # The seal strips the segment before rendering (pre-existing behavior),
        # so normalize both sides the same way for the comparison.
        expected = [
            _md_to_telegram_html(c.strip())
            for c in _split_markdown_bounded(text, r._rendered_limit())
        ]
        assert len(expected) > 1, "precondition: long enough to actually rotate"
        finals = [t for _, t, _ in cli.edits if "<b>" in t or "<i>" in t]
        finals += [t for t, _ in cli.sent if "<b>" in t or "<i>" in t]
        assert sorted(finals) == sorted(expected), "sealed bodies match the bounded split"

    def test_an_escape_heavy_degraded_table_is_never_truncated(self) -> None:
        # html.escape inflation inside <pre> is multiplicative, so a degraded
        # split that budgets SOURCE chars ships oversize chunks the client
        # backstop truncates -- silent row loss. The re-split must measure the
        # RENDERED form: every shipped chunk fits the cap and every row lands.
        cli = FakeClient()
        cli.rich_fails = True
        r = self._renderer(cli)
        table = self._table(100, fill="<&>", width=25)
        assert r._limit() < len(table) <= r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_done()

        asyncio.run(_go())

        bodies = [t for _, t, _ in cli.edits if "<pre>" in t]
        bodies += [t for t, _ in cli.sent if "<pre>" in t]
        assert len(bodies) >= 2
        for b in bodies:
            assert len(b) <= r._rendered_limit(), "every RENDERED chunk fits the cap"
        joined = "\n".join(bodies)
        for i in range(100):
            assert f"| {i:05d} |" in joined, "no row lost to escape inflation"

    def test_a_row_streamed_after_an_over_budget_rotation_stays_its_own_row(self) -> None:
        # The block splitter joins lines without the buffer's trailing newline.
        # The retained tail keeps streaming, so dropping it would glue the next
        # streamed row onto the previous one and corrupt the table mid-stream.
        cli = FakeClient()
        r = self._renderer(cli)
        table = self._table(300, fill="y", width=120)
        assert len(table) > r._rich_limit()
        assert table.endswith("\n")

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(table)
            await r.on_text_chunk("| 99999 | sentinel |\n")
            await r.on_done()

        asyncio.run(_go())

        lines = [ln for md, _, _ in cli.rich_sent for ln in md.split("\n")]
        assert "| 99999 | sentinel |" in lines, "the streamed row survives as its own line"
        assert not any("||" in ln.replace("| |", "") for ln in lines), "no glued rows"

    def test_a_partial_row_at_rotation_time_is_not_stranded_as_prose(self) -> None:
        # GFM rows need no outer pipe, so a row whose first pipe has not
        # streamed yet reads as prose to the block parser. A rotation firing at
        # that instant must keep the unterminated line with the streaming tail;
        # emitting it as a prose chunk strands it -- and the rows after it --
        # outside the table.
        cli = FakeClient()
        r = self._renderer(cli)
        head = "id | data\n--- | ---\n"
        rows = "".join(f"{i:05d} | {'y' * 120}\n" for i in range(300))
        assert len(head + rows) > r._rich_limit()

        async def _go() -> None:
            await r.on_turn_start()
            # First delivery ends mid-row, BEFORE the row's first pipe.
            await r.on_text_chunk(head + rows + "99999")
            await r.on_text_chunk(" | sentinel\n")
            await r.on_done()

        asyncio.run(_go())

        assert len(cli.rich_sent) >= 2
        lines = [ln for md, _, _ in cli.rich_sent for ln in md.split("\n")]
        assert "99999 | sentinel" in lines, "the partial row finishes inside the table"
        for md, _, _ in cli.rich_sent:
            assert _has_table(md), "every chunk stays table-detected"


# ── renderer.py: interactive approval decider ───────────────────────────────


class TestApprovalDecider:
    def test_resolve_pending(self) -> None:
        async def _go() -> bool:
            d = TelegramApprovalDecider(session_key="telegram:1:0")
            task = asyncio.ensure_future(d(SimpleNamespace(request_id="rq7")))
            await asyncio.sleep(0.02)
            TelegramApprovalDecider.resolve_global("telegram:1:0:rq7", True)
            return await task

        assert asyncio.run(_go()) is True

    def test_resolve_unknown_key_returns_false(self) -> None:
        assert TelegramApprovalDecider.resolve_global("no-such-key", True) is False


# ── transport_dispatch.py: turn + callback routing ─────────────────────────


def _deny_channel_profile(monkeypatch, tmp_path, allow=("slack",)):
    """Point the ProfileStore at a host profile that allows only ``allow`` — so
    any other channel is denied by the inbound gate. Returns nothing; resets the
    store so the next resolve sees the profile."""
    import json

    from kiro_crew.platform import governance_profiles as gp

    pdir = tmp_path / "profiles"
    pdir.mkdir(exist_ok=True)
    monkeypatch.setattr(gp, "_PROFILES_DIR", pdir)
    gp.reset_store()
    (pdir / "host.json").write_text(
        json.dumps(
            {
                "name": "host",
                "bind": {"type": "surface", "id": "host"},
                "channels": {"members": {"mode": "allow", "allow": list(allow)}},
            }
        )
    )


class TestDispatcher:
    def test_channels_deny_drops_inbound_message(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT round-4 #2): a channels DENY must stop handle_message from
        # driving a turn. Regression-locks the Telegram inbound chokepoint —
        # removing the gate makes this test fail (a turn would run).
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, sess = _dispatcher({7})
        try:
            asyncio.run(
                d.handle_message(
                    InboundMessage(
                        channel_type="telegram", user_id="7", conversation_id="7", text="hello"
                    )
                )
            )
            assert cli.final_text() in (None, "")
            assert sess.successes == []
        finally:
            gp.reset_store()

    def test_channels_deny_drops_callback_approval(self, tmp_path, monkeypatch) -> None:
        # HIGH (GPT round-4 #2): a callback press must not resolve a pending tool
        # approval on a denied channel. Regression-locks the on_callback gate.
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, _ = _dispatcher({7})

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq1")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            try:
                cb = SimpleNamespace(
                    callback_query_id="q1",
                    user_id=7,
                    chat_id=7,
                    message_id=100,
                    data="a:rq1:1",
                    label="",
                    chat_type="private",
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done()
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        try:
            assert asyncio.run(_go()) is False, "denied channel must not resolve the tool approval"
        finally:
            gp.reset_store()

    def test_channels_deny_still_resolves_callback_reject(self, tmp_path, monkeypatch) -> None:
        # MEDIUM (GPT round-13 #3): a REJECT callback ("a:...:0") on a denied channel
        # must STILL resolve the pending approval as refused (False) — a reject is a
        # denial, and dropping it would strand the pending future until timeout.
        # Only APPROVE is gated out.
        from kiro_crew.platform import governance_profiles as gp

        _deny_channel_profile(monkeypatch, tmp_path)
        d, cli, _ = _dispatcher({7})

        async def _go() -> "tuple[bool, bool]":
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq1")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            try:
                cb = SimpleNamespace(
                    callback_query_id="q1",
                    user_id=7,
                    chat_id=7,
                    message_id=100,
                    data="a:rq1:0",  # reject (flag 0)
                    label="",
                    chat_type="private",
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done(), (fut.result() if fut.done() else True)
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        try:
            done, result = asyncio.run(_go())
            assert (
                done and result is False
            ), "a reject on a denied channel must resolve the approval as refused"
        finally:
            gp.reset_store()

    def test_full_turn_records_success_and_releases(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello world"
                )
            )

        asyncio.run(_go())
        assert cli.final_text() == "Answer: hello world"
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess.released == ["telegram:kirocrew:direct:7"]

    def test_agent_resolves_to_kirocrew_when_unset(self) -> None:
        # agent=None + empty default_agent must fall back to "kirocrew" so the
        # session loads kirocrew-core (spawn_run), not kiro-cli's bare default.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert sess.last_agent == "kirocrew"

    def test_cold_start_failure_finalizes_and_skips_release(self) -> None:
        # If get_or_create raises (cold-start), the turn must still be finalized
        # (block streaming sends an error block, no silent dead turn) and the
        # semaphore must NOT be released (it was never acquired).
        d, cli, sess = _dispatcher({7}, raise_on_get=True)

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert cli.sent and "Error" in cli.sent[-1][0]  # finalized by close()
        assert sess.released == []  # never acquired -> never released
        assert sess.failures == []  # not acquired -> not recorded as a failed turn

    def test_permanent_acp_error_reason_reaches_user(self) -> None:
        # Issue #1831: a permanent AcpError (model entitlement) must surface
        # its actionable message, not the generic retry advice.
        msg = "Your account does not have access to model 'x'. Available: a, b."

        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AcpError(msg, transient=False)
                yield  # pragma: no cover — makes this an async generator

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        final = cli.sent[-1][0]
        assert msg in final
        assert "please try again" not in final
        assert "\n" not in final  # single-line, bounded output

    def test_transient_acp_error_keeps_retry_text(self) -> None:
        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise AcpError("backend hit a transient error (HTTP 5xx)", transient=True)
                yield  # pragma: no cover

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        assert "please try again" in cli.sent[-1][0]
        assert "5xx" not in cli.sent[-1][0]

    def test_non_acp_error_stays_generic_and_leaks_nothing(self) -> None:
        class _FailingProvider(FakeProvider):
            async def stream(self, message: str) -> Any:
                raise ValueError("secret internal detail /home/alice/.kiro/x")
                yield  # pragma: no cover

        class _Sessions(FakeSessions):
            async def get_or_create(self, key: str, **kw: Any) -> Any:
                return _FailingProvider(), True, False

        d, cli, sess = _dispatcher({7})
        d.sessions = _Sessions()  # type: ignore[assignment]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )

        asyncio.run(_go())
        final = cli.sent[-1][0]
        assert "please try again" in final
        assert "secret internal detail" not in final
        assert "/home/alice" not in final

    def test_new_command_bumps_gen_and_replies(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/new"
                )
            )

        asyncio.run(_go())
        assert d._conv.current_gen(("direct", "7")) == 1
        assert "New conversation" in cli.sent[-1][0]
        assert sess.successes == []  # no turn ran

    def test_help_command_replies(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/help"
                )
            )

        asyncio.run(_go())
        assert "Kiro Crew" in cli.sent[-1][0]

    def test_compact_refused_while_turn_running(self) -> None:
        # /compact must NOT drive the same provider while a turn streams. The
        # guard now atomically try_acquire()s the semaphore: if a turn holds it,
        # acquisition fails and we refuse — no acquire, no concurrent stream.
        d, cli, sess = _dispatcher({7})
        sess._busy = True  # simulate an in-flight turn holding the semaphore

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert any("try /compact" in s[0] for s in cli.sent)  # refused with notice
        assert not any("Compacting" in s[0] for s in cli.sent)  # never started
        assert sess.acquired == []  # semaphore never taken while busy

    def test_compact_when_idle_holds_and_releases_semaphore(self) -> None:
        # When idle, /compact atomically acquires the per-session semaphore for
        # the whole compaction (serializing against a normal turn), then always
        # releases it — so it can't interleave JSON-RPC on the shared provider.
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert sess.acquired == ["telegram:kirocrew:direct:7"]  # acquired the turn semaphore
        assert sess.released == ["telegram:kirocrew:direct:7"]  # and released it in finally
        assert any("Compact" in s[0] for s in cli.sent) or any("Compact" in e[1] for e in cli.edits)

    def test_compact_summary_body_is_not_sent(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _completed(timeout: float = 0.0) -> dict:
            return {"type": "completed", "summary": "## OBJECTIVE\ninternal guidance"}

        sess._gp.wait_for_compaction = _completed

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        visible = " ".join([text for text, _ in cli.sent] + [text for _, text, _ in cli.edits])
        assert "Context compacted" in visible
        assert "OBJECTIVE" not in visible and "internal guidance" not in visible

    def test_compact_timeout_reports_gracefully(self) -> None:
        # Regression: nested 120s timeouts made the graceful-timeout branch
        # unreachable and destroyed a healthy session. A compaction that yields
        # no terminal status must report a timeout and KEEP the session.
        d, cli, sess = _dispatcher({7})

        async def _timeout(timeout: float = 0.0) -> dict:
            return {"type": "timeout"}

        sess._gp.wait_for_compaction = _timeout

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/compact"
                )
            )

        asyncio.run(_go())
        assert any("timed out" in s[0] for s in cli.sent) or any(
            "timed out" in e[1] for e in cli.edits
        )
        assert sess.destroyed == [] and sess.discarded == []  # healthy session preserved

    def test_callback_option_echoes_choice_and_redispatches(self) -> None:
        d, cli, sess = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q1",
            user_id=7,
            chat_id=7,
            message_id=99,
            data="opt:0",
            label="Say Hi",
            chat_type="private",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        # Tapping an option retires the keyboard on the original message WITHOUT
        # overwriting its text, echoes the picked choice as its own block, then
        # re-dispatches the choice so the answer arrives as a NEW message.
        assert cli.markup_edits[-1] == (99, {"inline_keyboard": []})
        assert all(mid != 99 for mid, _, _ in cli.edits)  # original text never clobbered
        assert "Say Hi" in cli.sent[0][0]  # choice echoed as its own block first
        assert cli.final_text() == "Answer: Say Hi"  # answer streamed as a NEW message

    def test_callback_approval_resolves_decider(self) -> None:
        d, cli, _ = _dispatcher({7})

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("direct", "7")), "rq9")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            cb = SimpleNamespace(
                callback_query_id="q2",
                user_id=7,
                chat_id=7,
                message_id=100,
                data="a:rq9:1",
                label="",
                chat_type="private",
            )
            await d.on_callback(cb)  # type: ignore[arg-type]
            return fut.done() and fut.result() is True

        assert asyncio.run(_go()) is True

    def test_callback_approval_expired_shows_expired_not_approved(self) -> None:
        # Post-timeout: no pending future for the key (decider already denied by
        # default and popped it). An "Approve" press must NOT display "Approved".
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q5",
            user_id=7,
            chat_id=7,
            message_id=101,
            data="a:gone:1",
            label="",
            chat_type="private",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.edits, "expected a verdict edit"
        assert "expired" in cli.edits[-1][1].lower()
        assert "Approved" not in cli.edits[-1][1]

    def test_callback_unauthorized_user_ignored(self) -> None:
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q3",
            user_id=999,
            chat_id=999,
            message_id=1,
            data="opt:0",
            label="X",
            chat_type="private",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        # Deny-by-default short-circuits BEFORE the ack: no Bot API round-trip
        # and no edit/redispatch for an unauthorized user.
        assert cli.answered == []
        assert cli.edits == []

    def test_callback_non_private_chat_ignored(self) -> None:
        # Defense-in-depth: even an allow-listed user's press is ignored if the
        # callback isn't from a private chat (mirrors the receive() guard).
        d, cli, _ = _dispatcher({7})
        cb = SimpleNamespace(
            callback_query_id="q4",
            user_id=7,
            chat_id=-100,
            message_id=1,
            data="opt:0",
            label="X",
            chat_type="group",
        )

        async def _go() -> None:
            await d.on_callback(cb)  # type: ignore[arg-type]

        asyncio.run(_go())
        assert cli.answered == []
        assert cli.edits == []


class TestClientSession:
    def test_ensure_session_creates_single_shared_instance(self, monkeypatch: Any) -> None:
        # Concurrent _api callers (polling loop + handler tasks) must share ONE
        # ClientSession — the double-checked lock in _ensure_session prevents a
        # leaked duplicate.
        import kiro_crew.telegram.client as client_mod

        created = {"n": 0}

        class _FakeSession:
            def __init__(self) -> None:
                created["n"] += 1
                self.closed = False

        monkeypatch.setattr(client_mod.aiohttp, "ClientSession", _FakeSession)
        cli = client_mod.TelegramClient(token="x")

        async def _go() -> None:
            await asyncio.gather(
                cli._ensure_session(), cli._ensure_session(), cli._ensure_session()
            )

        asyncio.run(_go())
        assert created["n"] == 1


class TestTelegramTokenRedaction:
    """#1 — a Telegram bot token echoed in output must be scrubbed."""

    def test_bot_token_is_redacted(self) -> None:
        from kiro_crew.security import redact_credentials

        token = "8412345678:AAExampleSecretTokenValue_1234567890abcd"
        cleaned, warnings = redact_credentials(f"my token is {token} ok")
        assert token not in cleaned
        assert "[REDACTED: credential]" in cleaned
        assert warnings  # at least one redaction warning recorded

    def test_benign_short_colon_pairs_not_redacted(self) -> None:
        from kiro_crew.security import redact_credentials

        # Too few digits (<6) and too-short suffix (<30) to match the token
        # shape — must not be over-redacted.
        text = "ratio 12:34, port 8080:abc, time 10:30:00"
        cleaned, _ = redact_credentials(text)
        assert cleaned == text


class TestConfigMasking:
    """#5 — sensitive config fields are masked in the API response only."""

    def test_bot_token_masked_in_response(self) -> None:
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {
                    "telegram": {
                        "bot_token": "8412345678:AAsecretsecretsecret",
                        "enabled": True,
                        "allowed_user_ids": [7],
                    }
                }

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        assert out["telegram"]["bot_token"] == _SENSITIVE_MASK  # secret hidden
        assert out["telegram"]["enabled"] is True  # non-sensitive untouched
        assert out["telegram"]["allowed_user_ids"] == [7]

    def test_empty_token_not_masked(self) -> None:
        from kiro_crew.dashboard.handlers.core import _SENSITIVE_MASK, _masked_config_dict

        class _Cfg:
            def to_dict(self) -> dict:
                return {"telegram": {"bot_token": "", "enabled": False}}

        out = _masked_config_dict(_Cfg())  # type: ignore[arg-type]
        # Unset stays empty (UI shows "not set"), never a fake mask sentinel.
        assert out["telegram"]["bot_token"] == ""
        assert _SENSITIVE_MASK not in str(out)


class TestTelegramMidTurn:
    def test_drain_defers_past_the_attachment_cap(self) -> None:
        """Queue collapse must not exceed the shared ingestion file cap.

        Two queued 10-photo albums. Concatenating them hands 20 attachments to
        one turn and ingest_attachments silently processes only the first
        max_attachments, so the second album vanishes with no indication. The
        drain must defer the overflowing message (and everything behind it, to
        keep FIFO exact) AND then keep pumping so the deferred album runs in a
        second turn -- deferring without looping just strands it until the user
        happens to send something else.
        """
        from kiro_crew.messaging.attachments import IngestLimits

        cap = IngestLimits().max_attachments
        d, cli, sess = _dispatcher({7})
        album_a = [{"file_id": f"a{i}", "file_name": f"a{i}.jpg",
                    "mime_type": "image/jpeg"} for i in range(cap)]
        album_b = [{"file_id": f"b{i}", "file_name": f"b{i}.jpg",
                    "mime_type": "image/jpeg"} for i in range(cap)]
        # Two albums already sitting in the queue when the turn ends.
        sess.queued = [
            (str(1), "album A", {"attachments": album_a}),
            (str(2), "album B", {"attachments": album_b}),
        ]
        sess._busy = False

        seen: list[tuple[str, int]] = []
        original = d.handle_message

        async def _spy(msg, **kw):  # type: ignore[no-untyped-def]
            seen.append((msg.text, len(msg.attachments)))
            return None  # don't run a real turn

        async def _go() -> None:
            d.handle_message = _spy  # type: ignore[assignment]
            try:
                await d._drain_queue("k", 7, 7)
            finally:
                d.handle_message = original  # type: ignore[assignment]

        asyncio.run(_go())

        # Cap first, and over EVERY turn: without this ordering a cap regression
        # merges both albums into one turn and would trip the pump-count
        # assertion instead, leaving the cap itself unpinned.
        assert seen, "the drain must run at least one turn"
        for i, (_t, n) in enumerate(seen):
            assert n <= cap, (
                f"turn {i} carried {n} attachments, over the cap of {cap} -- "
                "ingestion would silently drop the excess"
            )
        assert len(seen) == 2, (
            "the drain must keep pumping: the deferred album has to run in a "
            "SECOND turn of this same drain, not wait for unrelated user input"
        )
        first_text, _ = seen[0]
        second_text, _ = seen[1]
        assert "album A" in first_text and "album B" not in first_text, (
            "album B must be deferred whole, not partially merged"
        )
        assert "album B" in second_text, "album B must drain in the second turn"
        assert not sess.queued, "the queue must be empty once the pump finishes"

    def test_command_caption_on_attachment_is_content_not_a_command(self) -> None:
        """A photo captioned "/new" must be ingested, not intercepted.

        The command intercept returns BEFORE attachment ingestion, so treating a
        caption as a command silently discards the file the user attached to it.
        Attachments make the message content-bearing; Discord already gated on
        this via interpret_as_command.
        """
        d, cli, sess = _dispatcher({7})
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]
        before_gen = d._conv.current_gen(d._route_key(
            chat_type="private", user_id=7, chat_id=7, thread=None,
        ))

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/new",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        route = d._route_key(chat_type="private", user_id=7, chat_id=7, thread=None)
        assert d._conv.current_gen(route) == before_gen, (
            "/new as an attachment caption must NOT start a new conversation -- "
            "that path returns before ingestion and drops the photo"
        )
        assert not any("New conversation started" in t for t, _ in cli.sent), (
            "the command confirmation must not be sent for an attachment caption"
        )

    def test_bare_directive_caption_on_attachment_is_content_not_a_command(
        self,
    ) -> None:
        """A photo captioned "/queue" must be ingested, not answered with usage.

        Same loss as the "/new" caption above and the same cause: the bare
        "/queue" | "/steer" guard returns BEFORE attachment ingestion, so a
        caption read as a directive silently discards the file. ``attachments``
        make the message content-bearing, which is exactly what
        ``interpret_as_command`` encodes.
        """
        d, cli, _ = _dispatcher({7})
        photos = [{"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"}]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/queue",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        assert not any("Those take a message" in t for t, _ in cli.sent), (
            "the bare-directive usage reply must not fire for an attachment "
            "caption -- that path returns before ingestion and drops the photo"
        )

    def test_attachment_message_is_queued_not_steered(self) -> None:
        """A mid-turn message carrying files must NEVER take the steer path.

        ``steer`` forwards TEXT ONLY, so steering a photo/album message would
        deliver its caption and silently discard every attachment. This is the
        exact loss an album hits: a follow-up typed during the debounce window
        starts a turn, so the album's own flush lands mid-turn.

        The queue path is correct because it carries ``attachments`` through the
        drain -- assert they survive, not merely that steer was skipped.
        """
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        # Default (non-queue) mode: without the guard this would steer.
        d.cfg.messaging.queue_mode = "steer"
        photos = [
            {"file_id": "p1", "file_name": "a.jpg", "mime_type": "image/jpeg"},
            {"file_id": "p2", "file_name": "b.jpg", "mime_type": "image/jpeg"},
        ]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="what is wrong here?",
                    attachments=photos,
                )
            )

        asyncio.run(_go())

        assert sess._gp.steered == [], "an attachment message must not be steered"
        assert len(sess.queued) == 1, "it must be queued instead"
        _ts, text, kwargs = sess.queued[0]
        assert text == "what is wrong here?"
        assert kwargs.get("attachments") == photos, (
            "the queue must carry the attachments -- otherwise the images are "
            "silently dropped exactly as steering would have done"
        )

    def test_busy_steer_folds_into_running_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="and also this"
                )
            )

        asyncio.run(_go())
        # Folded into the running turn (steer called), not queued, and NO receipt
        # bubble is posted (M1: the steered continuation threads under the user's
        # message instead — see the renderer reply-linkage test).
        assert sess._gp.steered == ["and also this"]
        assert sess.queued == []
        assert not any("Steered" in t for t, _ in cli.sent)

    def test_busy_steer_skipped_when_turn_already_ended(self) -> None:
        # Race guard: is_busy() stays True through post-turn bookkeeping, but the
        # turn has actually ended (has_active_turn() False). The steer must NOT
        # be treated as terminal -- the message falls through to the queue/handle
        # path so it is never silently swallowed.
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        sess._gp.active_turn = False  # turn ended, semaphore still held

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="landed after turn",
                )
            )

        asyncio.run(_go())
        # Not steered (dead turn); preserved via the queue path instead of lost.
        assert sess._gp.steered == []
        assert [text for _ts, text, _ in sess.queued] == ["landed after turn"]

    def test_busy_steer_reacts_to_user_message(self) -> None:
        # Instant ack: a mid-turn steer reacts to the user's message (no extra
        # bubble) so it isn't silent while it waits for the next generation
        # boundary (the steered continuation only posts at turn end).
        d, cli, sess = _dispatcher({7})
        sess._busy = True

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="stop, only banana",
                    message_id=4242,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == ["stop, only banana"]  # steered
        assert cli.reactions == [(4242, _STEER_ACK_EMOJI)]  # reacted on the user's steer msg
        # No extra receipt/steer bubble is posted (the ack is the reaction only).
        assert not any("Steered" in t or "Queued" in t for t, _ in cli.sent)

    def test_queue_override_forces_queue_in_steer_mode(self) -> None:
        # "/queue …" holds the message even though the global mode is steer;
        # the directive is stripped from the queued text.
        d, cli, sess = _dispatcher({7})
        sess._busy = True  # global queue_mode defaults to "steer"

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/queue check disk after",
                    message_id=11,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == []  # NOT steered
        assert [t for _ts, t, _ in sess.queued] == ["check disk after"]  # queued, stripped

    def test_steer_override_forces_steer_in_queue_mode(self) -> None:
        # "/steer …" folds into the running turn even though the global mode is
        # queue; the directive is stripped from the steered text.
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            await d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/steer stop now",
                    message_id=12,
                )
            )

        asyncio.run(_go())
        assert sess._gp.steered == ["stop now"]  # steered, stripped
        assert sess.queued == []  # NOT queued
        assert cli.reactions == [(12, _STEER_ACK_EMOJI)]  # steer-ack on the steer message

    def test_busy_queue_mode_enqueues(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="later"
                )
            )

        asyncio.run(_go())
        assert [text for _ts, text, _ in sess.queued] == ["later"]
        assert sess._gp.steered == []
        assert any("Queued" in t for t, _ in cli.sent)

    def test_not_busy_runs_a_full_turn(self) -> None:
        d, cli, sess = _dispatcher({7})

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="hello"
                )
            )

        asyncio.run(_go())
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess._gp.steered == []

    def test_drain_collapses_queued_into_one_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.queued = [("t1", "first", {}), ("t2", "second", {})]

        async def _go() -> None:
            await d._drain_queue("telegram:kirocrew:direct:7", 7, 7)

        asyncio.run(_go())
        # All queued messages collapse into ONE combined turn (drain=False ->
        # no recursion), and the queue is emptied.
        assert sess.successes == ["telegram:kirocrew:direct:7"]
        assert sess.queued == []

    def test_queue_receipt_collapses_into_single_bubble(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            for t in ("what time is it", "and the weather?"):
                await d.handle_message(
                    InboundMessage(
                        channel_type="telegram", user_id="7", conversation_id="7", text=t
                    )
                )

        asyncio.run(_go())
        # One receipt bubble is created, then edited in place to grow to (2) --
        # not one fresh "Queued" bubble per message.
        receipts = [t for t, _ in cli.sent if "Queued" in t]
        assert len(receipts) == 1 and "(1)" in receipts[0]
        grows = [txt for _mid, txt, _ in cli.edits if "Queued" in txt]
        assert any("(2)" in g for g in grows)
        assert [text for _ts, text, _ in sess.queued] == [
            "what time is it",
            "and the weather?",
        ]

    def test_stop_cancels_running_turn_and_clears_queue(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        sess.queued = [("t1", "pending", {})]

        async def _go() -> None:
            await d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id="7", conversation_id="7", text="/stop"
                )
            )

        asyncio.run(_go())
        assert sess._gp.cancelled == 1  # in-flight turn aborted
        assert sess.queued == []  # pending queue cleared
        assert any("Stopped" in t for t, _ in cli.sent)

    def test_concurrent_queue_adds_share_one_receipt(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            await asyncio.gather(
                *[
                    d.handle_message(
                        InboundMessage(
                            channel_type="telegram",
                            user_id="7",
                            conversation_id="7",
                            text=f"m{i}",
                        )
                    )
                    for i in range(4)
                ]
            )

        asyncio.run(_go())
        # The receipt lock serializes the check-then-send: exactly ONE receipt
        # bubble is created; the other three grow it via edits (no orphans).
        sends = [t for t, _ in cli.sent if "Queued" in t]
        grows = [txt for _mid, txt, _ in cli.edits if "Queued" in txt]
        assert len(sends) == 1
        assert len(grows) == 3

    def test_drain_caps_collapse_and_drains_remainder_in_order(self) -> None:
        d, cli, sess = _dispatcher({7})
        # 52 queued -> the collapse cap (50) puts 50 into the first turn and
        # defers the 2-message remainder. The drain then keeps pumping, so the
        # remainder runs in a SECOND turn of the same drain rather than waiting
        # for unrelated future input. A 2+ item remainder is what exposes any
        # FIFO reordering of the surplus.
        sess.queued = [(f"t{i}", f"m{i}", {}) for i in range(52)]
        seen: list[str] = []
        original = d.handle_message

        async def _spy(msg, **kw):  # type: ignore[no-untyped-def]
            seen.append(msg.text)
            return None  # don't run a real turn

        async def _go() -> None:
            d.handle_message = _spy  # type: ignore[assignment]
            try:
                await d._drain_queue("telegram:kirocrew:direct:7", 7, 7)
            finally:
                d.handle_message = original  # type: ignore[assignment]

        asyncio.run(_go())

        assert len(seen) == 2, "cap-deferred surplus must drain in a second turn"
        # First turn takes exactly the cap, in order.
        assert seen[0].split("\n\n") == [f"m{i}" for i in range(50)]
        # Surplus drains next, IN ORIGINAL ORDER -- not dropped, not reordered.
        assert seen[1].split("\n\n") == ["m50", "m51"]
        assert not sess.queued, "the queue must be empty once the pump finishes"

    def test_flip_count_reflects_answered_not_full_queue(self) -> None:
        d, cli, sess = _dispatcher({7})
        key = "telegram:kirocrew:direct:7"
        sess._busy = True
        d.cfg.messaging.queue_mode = "queue"

        async def _go() -> None:
            # Build the receipt + queue via the real enqueue path (52 > cap 50).
            for i in range(52):
                await d._enqueue_with_receipt(key, 7, f"m{i}")
            sess._busy = False  # turn finished
            await d._drain_queue(key, 7, 7)

        asyncio.run(_go())
        flips = [txt for _mid, txt, _ in cli.edits if "Now answering" in txt]
        assert flips, "receipt should flip to answering"
        # Count reflects what THIS turn answers (50), not the full 52 queued,
        # and the 2-message remainder is called out rather than silently implied.
        assert "(50)" in flips[-1] and "+2 deferred" in flips[-1]

    def test_no_receipt_when_turn_finished_before_enqueue(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess._busy = False  # turn ended before the mid-turn message could queue

        async def _go() -> bool:
            return await d._enqueue_with_receipt("telegram:kirocrew:direct:7", 7, "late message")

        queued = asyncio.run(_go())
        # enqueue is a no-op once the semaphore is free -> not queued, and no
        # stale "Queued" receipt bubble is posted (the caller runs it fresh).
        assert queued is False
        assert sess.queued == []
        assert not any("Queued" in t for t, _ in cli.sent)


class TestLinkCommand:
    def test_legacy_dashboard_mirror_key_transform(self) -> None:
        # Compat-only spelling: bindings written before session identity was
        # unified live on dashboard:<channel key with non-word chars folded>.
        assert (
            legacy_dashboard_mirror_key("telegram:kirocrew:direct:8743158320:gen3")
            == "dashboard:telegram_kirocrew_direct_8743158320_gen3"
        )
        assert (
            legacy_dashboard_mirror_key("telegram:kirocrew:direct:7")
            == "dashboard:telegram_kirocrew_direct_7"
        )

    def test_parse_link_unlink(self) -> None:
        assert parse_command("/link") == "link"
        assert parse_command("/unlink") == "unlink"
        assert parse_command("/LINK") == "link"
        assert parse_command("/new") == "new"
        assert parse_command("hello") is None

    def test_link_sets_mirror_on_channel_session_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        expected_key = d._session_key(("direct", "7"))
        assert expected_key in sess.mirror_links
        assert legacy_dashboard_mirror_key(expected_key) not in sess.mirror_links
        link = sess.mirror_links[expected_key]
        assert isinstance(link, ChannelLink)
        assert link.channel_type == "telegram"
        assert link.channel_id == "7"
        assert link.thread_id is None  # DM /link carries no Topic thread
        assert any("Linked" in t for t, _ in cli.sent)

    def test_forum_link_carries_topic_thread(self) -> None:
        # Fix 2 (issue #211): /link inside a forum Topic must store the Topic id
        # on the mirror link so dashboard-mirrored replies thread back into the
        # Topic (not the supergroup General).
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        route = ("forum", "-1001234567890:5")
        asyncio.run(d._handle_link(route, -1001234567890))
        link = sess.mirror_links[d._session_key(route)]
        assert link.channel_id == "-1001234567890"
        assert link.thread_id == "5"  # Topic id, as a str

    def test_unlink_clears_legacy_spelling(self) -> None:
        # A binding created before unification must still be clearable in-channel.
        d, cli, sess = _dispatcher({7})
        legacy = legacy_dashboard_mirror_key(d._session_key(("direct", "7")))
        sess.mirror_links[legacy] = ChannelLink("telegram", channel_id="7")
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_forum_unlink_sweeps_the_topic_scoped_location(self) -> None:
        # /link and /unlink must construct the SAME topic-scoped location, and
        # the sweep must not leak across Topics: a General-scoped (threadless)
        # binding in the same supergroup survives an unlink inside a Topic.
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        route = ("forum", "-1001234567890:5")
        asyncio.run(d._handle_link(route, -1001234567890))
        general = ChannelLink("telegram", channel_id="-1001234567890", thread_id=None)
        sess.mirror_links["dashboard:chat-9"] = general
        asyncio.run(d._handle_unlink(route, -1001234567890))
        assert sess.mirror_links == {"dashboard:chat-9": general}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_unlink_clears_existing(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_unlink_when_not_linked(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert any("wasn't linked" in t for t, _ in cli.sent)

    def test_unlink_clears_binding_stranded_under_foreign_spelling(self) -> None:
        # The stale-mirror regression: a binding whose key spelling no longer
        # derives from the current session key (rotated DM generation, or a
        # dashboard session mirroring into this chat) still occupies the
        # location. Unlink must clear it by location value.
        d, cli, sess = _dispatcher({7})
        sess.mirror_links["dashboard:chat-9"] = ChannelLink("telegram", channel_id="7")
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        assert any("Unlinked" in t for t, _ in cli.sent)

    def test_link_batches_its_writes_into_one_map_save(self) -> None:
        # /link makes three mutations. Each would otherwise rewrite the entire
        # session map, stalling the loop three times for one user action.
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        assert sess.batched_writes and all(sess.batched_writes)
        assert sess.batch_depth == 0

    def test_unlink_batches_its_writes_into_one_map_save(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_link(("direct", "7"), 7))
        sess.batched_writes.clear()
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.batched_writes and all(sess.batched_writes)
        assert sess.batch_depth == 0

    def test_unlink_leaves_other_locations_alone(self) -> None:
        # Exact-match sweep: a mirror into a DIFFERENT chat survives, and the
        # reply stays truthful when nothing points at this conversation.
        d, cli, sess = _dispatcher({7})
        other = ChannelLink("telegram", channel_id="8")
        sess.mirror_links["dashboard:chat-9"] = other
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {"dashboard:chat-9": other}
        assert any("wasn't linked" in t for t, _ in cli.sent)


class TestAutomaticOriginMirror:
    """A Telegram conversation mirrors itself, so dashboard turns reach the chat.

    Issue #2959: a session started in Telegram had no ``telegram`` mirror unless
    the user typed ``/link``, so ``_deliver_cross_surface_reply`` found no target
    and a turn taken from the dashboard was never delivered back — the chat read
    as dead while the conversation continued elsewhere.
    """

    @staticmethod
    def _turn(d: Any, uid: str = "7", text: str = "hi") -> None:
        asyncio.run(
            d.handle_message(
                InboundMessage(
                    channel_type="telegram", user_id=uid, conversation_id=uid, text=text
                )
            )
        )

    def test_inbound_turn_binds_this_chat_as_the_mirror(self) -> None:
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        link = sess.mirror_links[d._session_key(("direct", "7"))]
        assert link == ChannelLink("telegram", channel_id="7", thread_id=None)

    def test_forum_turn_binds_the_topic_not_the_supergroup_general(self) -> None:
        # The bind shares _origin_mirror_link with /link, so a forum turn must
        # carry the Topic id — a General-scoped binding would thread dashboard
        # replies into the wrong place.
        d, _cli, sess = _dispatcher(
            {7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890]
        )
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-1001234567890",
                    text="hi",
                    chat_type="supergroup",
                    thread_id="5",
                    message_id=1,
                )
            )
        )
        route = ("forum", "-1001234567890:5")
        link = sess.mirror_links[d._session_key(route)]
        assert link == ChannelLink("telegram", channel_id="-1001234567890", thread_id="5")

    def test_unlink_survives_the_next_message(self) -> None:
        # The load-bearing half of the opt-out: mirroring is re-asserted every
        # turn, so without a PERSISTED refusal "off" would last one message.
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {}
        self._turn(d, text="still off?")
        assert sess.mirror_links == {}

    def test_link_withdraws_the_opt_out_so_binding_resumes(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        asyncio.run(d._handle_link(("direct", "7"), 7))
        sess.mirror_links.clear()  # prove the NEXT turn re-binds, not just /link
        self._turn(d)
        assert sess.mirror_links[d._session_key(("direct", "7"))] == ChannelLink(
            "telegram", channel_id="7", thread_id=None
        )

    def test_steady_state_turn_does_not_rewrite_an_identical_binding(self) -> None:
        # The bind is re-asserted every turn; skipping an unchanged write keeps
        # the per-turn cost a read instead of a session-map save.
        d, _cli, sess = _dispatcher({7})
        self._turn(d)
        writes: list[str] = []
        original = sess.set_mirror_link

        def _counting(key: str, link: Any) -> None:
            writes.append(key)
            original(key, link)

        sess.set_mirror_link = _counting  # type: ignore[method-assign]
        self._turn(d, text="second")
        assert writes == []

    def test_an_explicit_bind_to_a_different_chat_is_not_repointed(self) -> None:
        # Nothing repoints a binding: a swept or rival-claimed one is REMOVED,
        # not moved. So a telegram binding naming another chat is always
        # deliberate (the dashboard can bind a surfaced session anywhere), and
        # re-pointing it at the origin would undo an explicit action silently.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        chosen = ChannelLink("telegram", channel_id="999", thread_id=None)
        sess.mirror_links[key] = chosen
        self._turn(d)
        assert sess.mirror_links == {key: chosen}

    def test_a_binding_for_another_channel_does_not_block_the_bind(self) -> None:
        # set_channel writes the legacy slack_channel_id for a new telegram
        # session, so the first turn can see a synthesized non-telegram link.
        # That says nothing about telegram mirroring and must not suppress it.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        sess.mirror_links[key] = ChannelLink("slack", channel_id="telegram:7")
        self._turn(d)
        assert sess.mirror_links[key] == ChannelLink(
            "telegram", channel_id="7", thread_id=None
        )

    def test_the_refusal_survives_a_generation_rotation(self) -> None:
        # /new and the configured idle/daily reset rotate the :genN suffix. Keyed
        # per generation the flag would expire on rotation — an idle reset would
        # undo the user's /unlink with no action on their part, which is the very
        # failure the persisted flag exists to prevent.
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        before = d._session_key(("direct", "7"))
        d._conv.bump_gen(("direct", "7"))
        after = d._session_key(("direct", "7"))
        assert after != before, "generation did not rotate; test would be vacuous"
        assert sess.mirror_opt_out(after) is True

    def test_a_unified_dm_scope_is_not_auto_bound(self) -> None:
        # dm_scope=unified collapses every allowed user's DMs into one
        # unified:{agent} bucket — channel and user drop out of the key — so a
        # mirror bound there belongs to no particular chat and would deliver one
        # user's dashboard replies into another user's chat.
        d, _cli, sess = _dispatcher({7}, dm_scope="unified")
        self._turn(d)
        assert sess.mirror_links == {}

    def test_a_forum_route_is_still_bound_under_a_unified_scope(self) -> None:
        # A forum route keeps its full bucket under any dm_scope, so it names one
        # Topic and stays unambiguous.
        d, _cli, sess = _dispatcher(
            {7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890], dm_scope="unified"
        )
        asyncio.run(
            d.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="-1001234567890",
                    text="hi",
                    chat_type="supergroup",
                    thread_id="5",
                    message_id=1,
                )
            )
        )
        route = ("forum", "-1001234567890:5")
        assert sess.mirror_links[d._session_key(route)] == ChannelLink(
            "telegram", channel_id="-1001234567890", thread_id="5"
        )

    def test_a_refused_claim_does_not_break_the_turn(self) -> None:
        # set_mirror_link raises ConversationOwnershipConflict when a rival holds
        # the conversation. On the turn path an uncaught raise answers nothing.
        d, cli, sess = _dispatcher({7})

        def _refuse(key: str, link: Any) -> None:
            raise ConversationOwnershipConflict("held by another session")

        sess.set_mirror_link = _refuse  # type: ignore[method-assign]
        self._turn(d, text="hello world")
        assert cli.final_text() == "Answer: hello world"
        assert sess.successes == ["telegram:kirocrew:direct:7"]

    def test_the_binding_write_stays_on_the_loop_thread(self) -> None:
        # The write is BOUNDED — one whole-map rewrite, on a conversation's first
        # turn only — so the loop pays it inline rather than paying a thread hop.
        # Interleaving is not the reason: `session_map._MAP_LOCK` orders every
        # guarded mutation, `os.replace` included, so a worker could not drop a
        # persisted binding. Offloading would therefore be safe but pointless
        # here, and it would put an await between this bind and the turn it
        # belongs to. Pinned so the placement is a decision, not an accident.
        d, _cli, sess = _dispatcher({7})
        wrote_on: list[int] = []
        original = sess.set_mirror_link

        def _recording(key: str, link: Any) -> None:
            wrote_on.append(threading.get_ident())
            original(key, link)

        sess.set_mirror_link = _recording  # type: ignore[method-assign]
        self._turn(d)
        # asyncio.run drives the loop on THIS thread, so the loop's ident is ours.
        assert wrote_on == [threading.get_ident()]

    def test_an_explicit_relink_to_the_opted_out_chat_survives(self) -> None:
        # The user /unlinked, then deliberately rebound this session to this same
        # chat from the dashboard. That is indistinguishable by VALUE from an
        # unlink that half-landed, so a bind that "repaired" the state would
        # delete a link the user just made — re-creating the dead-chat symptom
        # this feature exists to fix, for a user who did everything right.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        explicit = ChannelLink("telegram", channel_id="7", thread_id=None)
        sess.set_mirror_opt_out(key, True)
        sess.mirror_links[key] = explicit
        self._turn(d)
        assert sess.mirror_links == {key: explicit}

    def test_the_opt_out_leaves_an_explicit_mirror_elsewhere_alone(self) -> None:
        # The dashboard can bind a surfaced session to any conversation. An
        # opt-out for THIS chat says nothing about that target, so repairing an
        # interrupted unlink must not double as deleting a link the user chose.
        d, _cli, sess = _dispatcher({7})
        key = d._session_key(("direct", "7"))
        elsewhere = ChannelLink("telegram", channel_id="999", thread_id="4")
        sess.mirror_links[key] = elsewhere
        sess.set_mirror_opt_out(key, True)
        self._turn(d)
        assert sess.mirror_links == {key: elsewhere}


def test_receipt_text_caps_displayed_items() -> None:
    # A large mid-turn burst must not grow the rendered receipt unbounded: only
    # the first _RECEIPT_MAX_ITEMS are listed verbatim; the count stays true.
    from kiro_crew.messaging.queue_receipt import RECEIPT_MAX_ITEMS as _RECEIPT_MAX_ITEMS
    from kiro_crew.messaging.queue_receipt import receipt_text as _receipt_text

    texts = [f"msg number {i}" for i in range(_RECEIPT_MAX_ITEMS + 3)]
    out = _receipt_text(texts)
    surplus = len(texts) - _RECEIPT_MAX_ITEMS
    assert f"({len(texts)})" in out  # count prefix shows the true total
    assert f"…and {surplus} more" in out  # surplus collapsed, not listed verbatim
    assert texts[-1] not in out  # a beyond-cap item is not rendered verbatim


# ── Forum topics (issue #211): per-topic sessions, single-user ──────────────


class TestForumGateOutcome:
    """Direct unit test of the shared fail-closed forum authZ predicate used by
    BOTH transport.receive and dispatcher.on_callback (PR #219 Design #2). One
    predicate → the two call sites can never drift. Only a real forum Topic
    (supergroup + message_thread_id) of an allow-listed chat is authorized;
    ordinary groups and the supergroup General chat (no thread) are DENIED."""

    _LISTED = -1001234567890

    def test_private_is_authorized(self) -> None:
        assert (
            forum_gate_outcome("private", 7, None, allow_forum=False, allowed_forum_chat_ids=[])
            is None
        )

    def test_allowlisted_supergroup_topic_is_authorized(self) -> None:
        # supergroup + a real Topic thread + allow-listed chat_id -> authorized.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            is None
        )

    def test_supergroup_topic_not_allowlisted_denied_forum(self) -> None:
        # Real Topic, but the supergroup's chat_id is NOT allow-listed.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[-1009999999999],
            )
            == "denied_forum_not_allowed"
        )

    def test_supergroup_general_no_thread_denied(self) -> None:
        # General chat (no message_thread_id) is NOT a Topic -> denied even when
        # allow_forum is on and the chat_id IS allow-listed. Fail closed.
        assert (
            forum_gate_outcome(
                "supergroup",
                self._LISTED,
                None,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            == "denied_non_private_chat"
        )

    def test_ordinary_group_denied_even_with_thread(self) -> None:
        # An ordinary group can't have Topics; chat_type "group" is denied even
        # if a (spurious) thread id and allow-listed chat_id are supplied.
        assert (
            forum_gate_outcome(
                "group",
                self._LISTED,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[self._LISTED],
            )
            == "denied_non_private_chat"
        )

    def test_channel_denied_non_private(self) -> None:
        # chat_type dominates: a channel is denied even with a thread + "listed" id.
        assert (
            forum_gate_outcome(
                "channel",
                -100777,
                5,
                allow_forum=True,
                allowed_forum_chat_ids=[-100777],
            )
            == "denied_non_private_chat"
        )


class TestForumClientCapture:
    """The raw ``message_thread_id`` on a supergroup update is normalized onto
    ``TelegramInbound`` so downstream routing can key on the Topic."""

    def _dispatch_and_capture(self, update: dict) -> list[TelegramInbound]:
        captured: list[TelegramInbound] = []

        async def _on(inb: TelegramInbound) -> None:
            captured.append(inb)

        async def _go() -> None:
            cli = TelegramClient(token="x", on_message=_on)
            cli._dispatch(update)
            await asyncio.sleep(0.02)  # let the created handler task run

        asyncio.run(_go())
        return captured

    def test_message_thread_id_captured_into_inbound(self) -> None:
        captured = self._dispatch_and_capture(
            {
                "message": {
                    "message_id": 9,
                    "text": "hi",
                    "message_thread_id": 5,
                    "chat": {"id": -1001234567890, "type": "supergroup"},
                    "from": {"id": 7},
                }
            }
        )
        assert len(captured) == 1
        assert captured[0].message_thread_id == 5
        assert captured[0].chat_type == "supergroup"

    def test_dm_update_has_no_thread_id(self) -> None:
        captured = self._dispatch_and_capture(
            {
                "message": {
                    "message_id": 1,
                    "text": "hi",
                    "chat": {"id": 7, "type": "private"},
                    "from": {"id": 7},
                }
            }
        )
        assert len(captured) == 1
        assert captured[0].message_thread_id is None


class TestForumTransportGate:
    """Forum gating lives in transport.receive(): allow_forum + chat_id list.
    Fail closed — a group/supergroup message is dropped unless BOTH hold."""

    def _run_receive(
        self,
        inbound: TelegramInbound,
        *,
        allow_forum: bool,
        allowed_forum_chat_ids: list[int],
    ) -> list[InboundMessage]:
        dispatched: list[InboundMessage] = []

        async def _dispatch(m: InboundMessage) -> None:
            dispatched.append(m)

        t = TelegramTransport(
            FakeClient(),  # type: ignore[arg-type]
            allowed_user_ids=[7],
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids,
            dispatch=_dispatch,
        )
        asyncio.run(t.receive(inbound))
        return dispatched

    def test_forum_allowed_dispatches_with_thread(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        out = self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        assert len(out) == 1
        assert getattr(out[0], "chat_type", None) == "supergroup"
        assert out[0].thread_id == "5"  # Topic id rides the base thread_id
        assert out[0].conversation_id == "-1001234567890"
        assert out[0].user_id == "7"

    def test_forum_general_denied_no_thread(self) -> None:
        # General chat (no message_thread_id) is NOT a real Topic -> DENIED at
        # the gate even when allow_forum is on and the chat_id is allow-listed.
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=None,
        )
        assert (
            self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
            == []
        )

    def test_forum_denied_when_allow_forum_false(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        assert (
            self._run_receive(inbound, allow_forum=False, allowed_forum_chat_ids=[-1001234567890])
            == []
        )

    def test_forum_denied_when_chat_id_not_allowlisted(self) -> None:
        inbound = TelegramInbound(
            chat_id=-1001234567890,
            user_id=7,
            text="hi",
            chat_type="supergroup",
            message_thread_id=5,
        )
        assert (
            self._run_receive(inbound, allow_forum=True, allowed_forum_chat_ids=[-1009999999999])
            == []
        )


class TestForumDispatchRouting:
    """Per-topic session-key shape + generation isolation (dispatcher level)."""

    def _forum_msg(
        self, thread: str | None, *, chat_id: str = "-1001234567890", text: str = "hello"
    ) -> TelegramInboundMessage:
        return TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id=chat_id,
            text=text,
            chat_type="supergroup",
            thread_id=thread,
            message_id=1,
        )

    def test_forum_topic_session_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("5")))
        assert sess.successes == ["telegram:kirocrew:forum:-1001234567890:5"]

    # NB: there is deliberately NO "General session key" routing test — a
    # threadless supergroup (General) message is denied at the forum gate
    # (see TestForumGateOutcome.test_supergroup_general_no_thread_denied and
    # TestForumTransportGate.test_forum_general_denied_no_thread) and never
    # reaches handle_message, so no General route is ever served.

    def test_private_dm_key_unchanged_regression(self) -> None:
        # HARD INVARIANT: the private-DM key is byte-for-byte unchanged.
        d, cli, sess = _dispatcher({7})
        asyncio.run(
            d.handle_message(
                InboundMessage(channel_type="telegram", user_id="7", conversation_id="7", text="hi")
            )
        )
        assert sess.successes == ["telegram:kirocrew:direct:7"]

    def test_new_in_topic_isolates_generation(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("5", text="/new")))
        # Only THIS topic's generation advanced …
        assert d._conv.current_gen(("forum", "-1001234567890:5")) == 1
        # … a sibling topic and the DM are untouched.
        assert d._conv.current_gen(("forum", "-1001234567890:6")) == 0
        assert d._conv.current_gen(("direct", "7")) == 0

    def test_forum_outbound_send_threads_edit_does_not(self) -> None:
        # A forum renderer threads EVERY send into the Topic; edits carry NO
        # thread (FakeClient.edit_message has no message_thread_id param, so the
        # run completing at all proves edits are unthreaded).
        cli = FakeClient()
        r = TelegramRenderer(
            cli,
            -1001234567890,
            TELEGRAM_CAPABILITIES,  # type: ignore[arg-type]
            session_key="telegram:kirocrew:forum:-1001234567890:5",
            message_thread_id=5,
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text="hello topic"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        assert cli.send_threads  # at least one send happened
        assert all(tid == 5 for tid in cli.send_threads)  # every send threaded
        assert cli.edits  # the seal edited in place (unthreaded)


class TestForumConfig:
    @staticmethod
    def _load(data: dict) -> Any:
        """Load a KiroCrewConfig from an in-memory dict via a temp config file
        (mirrors the canonical loader entrypoint used across the config tests)."""
        import json
        import tempfile
        import unittest.mock
        from pathlib import Path

        from kiro_crew.config.loader import KiroCrewConfig

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(data, f)
            tmp = Path(f.name)
        try:
            with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=tmp):
                return KiroCrewConfig.load()
        finally:
            tmp.unlink(missing_ok=True)

    def test_forum_fields_default_closed(self) -> None:
        cfg = self._load({})
        assert cfg.telegram.allow_forum is False
        assert cfg.telegram.allowed_forum_chat_ids == []

    def test_forum_fields_parse_and_serialize(self) -> None:
        cfg = self._load(
            {
                "telegram": {
                    "allow_forum": True,
                    "allowed_forum_chat_ids": [-1001, "-1002", "bad", 1.5],
                }
            }
        )
        assert cfg.telegram.allow_forum is True
        # _coerce_int_ids keeps clean base-10 ints, drops the rest (fail closed).
        assert cfg.telegram.allowed_forum_chat_ids == [-1001, -1002]
        out = cfg.to_dict()  # asdict round-trips the new fields
        assert out["telegram"]["allow_forum"] is True
        assert out["telegram"]["allowed_forum_chat_ids"] == [-1001, -1002]


class TestForumReplyThreading:
    """Fix A: every dispatcher-originated send in a forum turn threads back into
    the user's Topic (message_thread_id=<topic>), never the supergroup General."""

    @staticmethod
    def _forum_msg(text: str, thread: str | None = "5") -> TelegramInboundMessage:
        return TelegramInboundMessage(
            channel_type="telegram",
            user_id="7",
            conversation_id="-1001234567890",
            text=text,
            chat_type="supergroup",
            thread_id=thread,
            message_id=1,
        )

    def test_new_confirmation_threads_into_topic(self) -> None:
        d, cli, _ = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("/new")))
        assert any("New conversation" in t for t, _ in cli.sent)
        # The confirmation landed IN Topic 5, not the supergroup's General.
        assert cli.send_threads == [5]

    def test_compact_status_threads_into_topic(self) -> None:
        d, cli, _ = _dispatcher({7})
        asyncio.run(d.handle_message(self._forum_msg("/compact")))
        # The "Compacting…" status message threads into the Topic.
        assert cli.send_threads == [5]
        assert any("Compact" in t for t, _ in cli.sent)

    def test_dm_confirmation_unthreaded_regression(self) -> None:
        # HARD INVARIANT: a private-DM /new reply carries NO message_thread_id.
        d, cli, _ = _dispatcher({7})
        asyncio.run(
            d.handle_message(
                InboundMessage(
                    channel_type="telegram",
                    user_id="7",
                    conversation_id="7",
                    text="/new",
                )
            )
        )
        assert cli.send_threads == [None]


class TestForumQueueDrain:
    """Fix B: a message queued mid-turn in a forum Topic drains under the FORUM
    session key, not the DM key."""

    def test_queued_forum_message_drains_under_forum_key(self) -> None:
        d, cli, sess = _dispatcher({7})
        forum_key = "telegram:kirocrew:forum:-1001234567890:5"
        # Simulate one message queued mid-turn for this Topic.
        sess.queued.append(("t0", "queued in the topic", {}))
        asyncio.run(
            d._drain_queue(
                forum_key,
                7,
                -1001234567890,
                chat_type="supergroup",
                thread="5",
            )
        )
        # The drained turn resolved to the FORUM key (carried via chat_type +
        # thread on the synthetic message), NOT the DM key.
        assert sess.successes == [forum_key]
        assert "telegram:kirocrew:direct:7" not in sess.successes

    def test_dm_queue_drains_under_dm_key_regression(self) -> None:
        # HARD INVARIANT: a DM drain still resolves to the DM key (param defaults).
        d, cli, sess = _dispatcher({7})
        sess.queued.append(("t0", "queued dm", {}))
        asyncio.run(d._drain_queue("telegram:kirocrew:direct:7", 7, 7))
        assert sess.successes == ["telegram:kirocrew:direct:7"]


class TestForumCallbackGate:
    """Fix C: forum callbacks are honored ONLY when the same gate
    transport.receive enforces passes (allow_forum AND chat_id allow-listed).
    Fail-closed authZ boundary — never open a callback from a non-allow-listed
    group. DM callbacks are unchanged (covered by TestDispatcher)."""

    @staticmethod
    def _opt_cb() -> Any:
        return SimpleNamespace(
            callback_query_id="qf",
            user_id=7,
            chat_id=-1001234567890,
            message_id=50,
            data="opt:0",
            label="Say Hi",
            chat_type="supergroup",
            message_thread_id=5,
        )

    def test_forum_callback_processed_when_allowlisted(self) -> None:
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        asyncio.run(d.on_callback(self._opt_cb()))  # type: ignore[arg-type]
        # Acked, and the [OPTIONS:] choice re-dispatched under the FORUM key.
        assert cli.answered == ["qf"]
        assert sess.successes == ["telegram:kirocrew:forum:-1001234567890:5"]
        # Every callback-originated send threaded back into the Topic.
        assert cli.send_threads and all(t == 5 for t in cli.send_threads)

    def test_forum_callback_denied_when_chat_id_not_allowlisted(self) -> None:
        # allow_forum on, but the supergroup's chat_id is NOT allow-listed.
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1009999999999])
        asyncio.run(d.on_callback(self._opt_cb()))  # type: ignore[arg-type]
        # Fail closed: not even acked, no keyboard retire, no re-dispatch.
        assert cli.answered == []
        assert cli.markup_edits == []
        assert sess.successes == []

    def test_forum_callback_general_no_thread_denied(self) -> None:
        # A press from the supergroup General chat (no message_thread_id) is NOT
        # a real Topic -> DENIED even when allow_forum is on and the chat_id IS
        # allow-listed. Mirrors the receive() gate exactly (fail closed).
        d, cli, sess = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])
        cb = SimpleNamespace(
            callback_query_id="qg",
            user_id=7,
            chat_id=-1001234567890,
            message_id=51,
            data="opt:0",
            label="Say Hi",
            chat_type="supergroup",
            message_thread_id=None,
        )
        asyncio.run(d.on_callback(cb))  # type: ignore[arg-type]
        assert cli.answered == []
        assert cli.markup_edits == []
        assert sess.successes == []

    def test_forum_callback_approval_resolves_only_when_allowlisted(self) -> None:
        # Allow-listed: the approval decision resolves under the forum key.
        d, cli, _ = _dispatcher({7}, allow_forum=True, allowed_forum_chat_ids=[-1001234567890])

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("forum", "-1001234567890:5")), "rqF")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            try:
                cb = SimpleNamespace(
                    callback_query_id="qF",
                    user_id=7,
                    chat_id=-1001234567890,
                    message_id=60,
                    data="a:rqF:1",
                    label="",
                    chat_type="supergroup",
                    message_thread_id=5,
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done() and fut.result() is True
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        assert asyncio.run(_go()) is True

    def test_forum_callback_not_resolved_when_allow_forum_false(self) -> None:
        # allow_forum OFF -> the identical approval press must NOT resolve the
        # decider (fail closed) and must not even ack.
        d, cli, _ = _dispatcher({7}, allow_forum=False, allowed_forum_chat_ids=[-1001234567890])

        async def _go() -> bool:
            key = TelegramApprovalDecider.key(d._session_key(("forum", "-1001234567890:5")), "rqF")
            fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
            TelegramApprovalDecider._REGISTRY[key] = fut
            try:
                cb = SimpleNamespace(
                    callback_query_id="qF",
                    user_id=7,
                    chat_id=-1001234567890,
                    message_id=61,
                    data="a:rqF:1",
                    label="",
                    chat_type="supergroup",
                    message_thread_id=5,
                )
                await d.on_callback(cb)  # type: ignore[arg-type]
                return fut.done()
            finally:
                TelegramApprovalDecider._REGISTRY.pop(key, None)

        assert asyncio.run(_go()) is False
        assert cli.answered == []


class TestRichMessageAvailabilityLatch:
    """sendRichMessage learns whether the server implements the method."""

    def _client(self):
        from kiro_crew.telegram.client import TelegramClient

        return TelegramClient(token="12345:testtoken")

    def _stub(self, client, monkeypatch, codes: list[int | None]) -> list[str]:
        """Make _api fail with each code in turn; record the methods called."""
        calls: list[str] = []
        seq = list(codes)

        async def _api(method, params, timeout=30, *, record=True, err_out=None):
            calls.append(method)
            code = seq.pop(0) if seq else None
            if code is None:
                return {"message_id": 7}
            if err_out is not None:
                err_out["error_code"] = code
                err_out["description"] = "stub failure"
            return None

        monkeypatch.setattr(client, "_api", _api)
        return calls

    def test_a_404_latches_immediately_and_stops_re_probing(self, monkeypatch) -> None:
        # A server without the method rejects every call the same way, so
        # re-probing per table would waste a round-trip forever.
        c = self._client()
        calls = self._stub(c, monkeypatch, [404])
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is True
        # Second call must short-circuit without touching the API at all.
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert calls == ["sendRichMessage"], "latched: no second request"

    def test_a_429_never_latches(self, monkeypatch) -> None:
        # Rate limiting is transient. Disabling rich rendering for the process
        # because of one 429 would be a permanent penalty for a momentary limit.
        c = self._client()
        calls = self._stub(c, monkeypatch, [429, None])
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is False
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) == 7
        assert len(calls) == 2, "still probing after a transient failure"

    def test_one_400_does_not_latch_but_a_streak_does(self, monkeypatch) -> None:
        # 400 is ambiguous: a wrong payload shape fails EVERY call, while one
        # oversized table fails only itself. Latch on the streak so a single bad
        # message cannot disable rich rendering for the whole process.
        from kiro_crew.telegram.client import _RICH_400_LATCH

        c = self._client()
        self._stub(c, monkeypatch, [400] * _RICH_400_LATCH)
        for _ in range(_RICH_400_LATCH - 1):
            assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
            assert c._rich_unsupported is False, "one bad table must not latch"
        assert asyncio.run(c.send_rich_message(1, "| a |\n| - |")) is None
        assert c._rich_unsupported is True, "a persistent 400 latches"

    def test_a_successful_send_clears_the_400_streak(self, monkeypatch) -> None:
        # Two unrelated bad tables spread over a session must not accumulate
        # into a latch when good tables send in between.
        from kiro_crew.telegram.client import _RICH_400_LATCH

        c = self._client()
        self._stub(c, monkeypatch, [400, None] * _RICH_400_LATCH)
        for _ in range(_RICH_400_LATCH):
            asyncio.run(c.send_rich_message(1, "| a |\n| - |"))  # 400
            asyncio.run(c.send_rich_message(1, "| a |\n| - |"))  # success
        assert c._rich_unsupported is False, "streak reset by each success"


class TestClientHealth:
    """get_me auth gate + on_status polling-health callback."""

    def _client(self):
        from kiro_crew.telegram.client import TelegramClient

        return TelegramClient(token="12345:testtoken")

    def test_get_me_returns_identity(self, monkeypatch) -> None:
        client = self._client()

        async def _call_raw(method, params, timeout=15):
            assert method == "getMe"
            return {"ok": True, "result": {"id": 42, "username": "kirocrew_bot"}}

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        result = asyncio.run(client.get_me())
        assert result["username"] == "kirocrew_bot"

    def test_get_me_raises_auth_error_on_rejection(self, monkeypatch) -> None:
        from kiro_crew.telegram.client import TelegramAuthError

        client = self._client()

        async def _call_raw(method, params, timeout=15):
            return {"ok": False, "error_code": 401, "description": "Unauthorized"}

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        try:
            asyncio.run(client.get_me())
            raise AssertionError("expected TelegramAuthError")
        except TelegramAuthError as exc:
            # The message must stay token-free: it is surfaced in settings.
            assert "testtoken" not in str(exc)
            assert "Unauthorized" in str(exc)

    def test_get_me_propagates_transport_errors(self, monkeypatch) -> None:
        """Offline is NOT a bad token: transport errors must not become
        TelegramAuthError, so the gateway can degrade instead of failing."""
        import aiohttp

        client = self._client()

        async def _call_raw(method, params, timeout=15):
            raise aiohttp.ClientConnectionError("network down")

        monkeypatch.setattr(client, "_call_raw", _call_raw)
        try:
            asyncio.run(client.get_me())
            raise AssertionError("expected ClientConnectionError")
        except aiohttp.ClientConnectionError:
            pass

    def test_notify_status_swallows_callback_errors(self) -> None:
        client = self._client()

        def _boom(healthy, reason):
            raise RuntimeError("callback bug")

        client.on_status = _boom
        client._notify_status(False, "x")  # must not raise

    def test_polling_loop_reports_persistent_failure_and_recovery(self, monkeypatch) -> None:
        """3 consecutive getUpdates failures -> unhealthy; next success -> healthy."""
        client = self._client()
        transitions: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: transitions.append((healthy, reason))

        results: list[Any] = [None, None, None, []]  # 3 failures then success

        async def _get_updates():
            if not results:
                client._closed = True
                return []
            return results.pop(0)

        async def _no_sleep(_delay):
            return None

        monkeypatch.setattr(client, "_get_updates", _get_updates)
        monkeypatch.setattr(asyncio, "sleep", _no_sleep)
        asyncio.run(client._polling_loop())
        assert transitions[0][0] is False  # reported unhealthy at threshold
        assert "getUpdates" in transitions[0][1]
        assert transitions[1] == (True, "")  # recovered on next success

    def test_polling_loop_reports_recovery_from_offline_boot(self, monkeypatch) -> None:
        """When the gateway seeded unhealthy (offline at startup), the FIRST
        successful poll must flip to healthy — no failure threshold applies."""
        client = self._client()
        client._last_status = False  # gateway boot state: unreachable
        transitions: list[tuple[bool, str]] = []
        client.on_status = lambda healthy, reason: transitions.append((healthy, reason))

        results: list[Any] = [[]]  # immediate success

        async def _get_updates():
            if not results:
                client._closed = True
                return []
            return results.pop(0)

        monkeypatch.setattr(client, "_get_updates", _get_updates)
        asyncio.run(client._polling_loop())
        assert transitions and transitions[0] == (True, "")
        assert len(transitions) == 1  # deduped: repeat successes don't re-fire


class TestTelegramSessionPidPublish:
    """#232: a Telegram turn must publish its session identity so managed MCP
    tools (learn_add, cron management, ...) can resolve ``X-Session-Key`` from
    a Telegram-originated turn. Publication is centralized in
    ``messaging.identity.publish_turn_identity``; this asserts Telegram dispatch
    delegates to that shared writer (DM + forum). Regression guard for the
    ``missing X-Session-Key`` 400. The publish semantics themselves (pid guard,
    executor offload) are covered in test_messaging_identity.py.
    """

    def test_telegram_turn_delegates_identity_publish(self) -> None:
        d, _cli, sess = _dispatcher({7})
        sess._pid = 4242  # SessionManager.get_pid -> kiro-cli host PID

        async def _go() -> None:
            with patch("kiro_crew.telegram.transport_dispatch.publish_turn_identity") as pub:
                await d.handle_message(
                    InboundMessage(
                        channel_type="telegram",
                        user_id="7",
                        conversation_id="7",
                        text="hi",
                    )
                )
                pub.assert_awaited_once_with(sess, "telegram:kirocrew:direct:7")

        asyncio.run(_go())


# ── transport_dispatch.py: _user_safe_failure_reason sanitizer ──────────────


class TestUserSafeFailureReason:
    def test_permanent_acp_error_yields_bounded_single_line(self) -> None:
        exc = AcpError("line one\nline two\ttail", transient=False)
        assert _user_safe_failure_reason(exc) == "⚠️ line one line two tail"

    def test_local_paths_are_redacted(self) -> None:
        exc = AcpError("failed reading /home/alice/.kiro/crew/creds", transient=False)
        out = _user_safe_failure_reason(exc)
        assert out is not None
        assert "/home/alice" not in out

    def test_length_hard_cap(self) -> None:
        out = _user_safe_failure_reason(AcpError("x" * 5000, transient=False))
        assert out is not None
        # "⚠️ " prefix + capped body (ellipsis included in the cap).
        assert len(out) <= 500 + len("⚠️ ")
        assert out.endswith("…")

    def test_transient_returns_none(self) -> None:
        assert _user_safe_failure_reason(AcpError("5xx", transient=True)) is None

    def test_unclassified_returns_none(self) -> None:
        assert _user_safe_failure_reason(AcpError("unknown", transient=None)) is None

    def test_non_acp_returns_none(self) -> None:
        assert _user_safe_failure_reason(ValueError("boom")) is None

    def test_empty_message_returns_none(self) -> None:
        assert _user_safe_failure_reason(AcpError("   \n ", transient=False)) is None
# ── /yolo + /model (transport_dispatch.py) ─────────────────────────────────


def _dm(text: str, uid: str = "7") -> InboundMessage:
    return InboundMessage(
        channel_type="telegram", user_id=uid, conversation_id=uid, text=text
    )


def _press(data: str, *, message_id: int = 101, uid: int = 7, label: str = "") -> Any:
    return SimpleNamespace(
        callback_query_id="q1",
        user_id=uid,
        chat_id=uid,
        message_id=message_id,
        data=data,
        label=label,
        chat_type="private",
    )


class TestYoloCommand:
    """/yolo drives the SAME process-wide grant as the dashboard and Slack."""

    def _reset(self) -> Any:
        from kiro_crew.safety_override import safety_override

        so = safety_override()
        if so.is_active():
            so.deactivate("test")
        return so

    def test_bare_yolo_reports_status_without_changing_it(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo")))
            assert "OFF" in cli.sent[-1][0]
            assert "Usage: /yolo on | off | renew" in cli.sent[-1][0]
            assert so.is_active() is False, "a status read must not arm the grant"
        finally:
            self._reset()

    def test_on_then_off_round_trips_the_grant(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo on")))
            assert so.is_active() is True
            assert "ON" in cli.sent[-1][0]

            asyncio.run(d.handle_message(_dm("/yolo off")))
            assert so.is_active() is False
            assert "OFF" in cli.sent[-1][0]
        finally:
            self._reset()

    def test_off_on_a_lapsed_grant_closes_the_renew_grace_window(self) -> None:
        """"/yolo off" must revoke a grant whose TTL already elapsed.

        ``deactivate()`` zeroes the past deadline, and that is what shuts the
        5-minute renew grace window. Skipping the call for a lapsed grant left
        it renewable, so a later "/yolo renew" silently restored auto-approval
        after the operator had been told "YOLO OFF".
        """
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo on")))
            assert so.is_active() is True

            # Lapse the grant but stay inside the renew grace window.
            so._expires_at = time.monotonic() - 10
            assert so.is_active() is False, "precondition: the grant has lapsed"

            asyncio.run(d.handle_message(_dm("/yolo off")))
            assert "OFF" in cli.sent[-1][0]

            assert so.renew("test").renewed is False, (
                "an explicitly revoked grant must not be resurrectable by renew "
                "just because its TTL happened to elapse before the off"
            )
            assert so.is_active() is False
        finally:
            self._reset()

    def test_unknown_argument_falls_back_to_status(self) -> None:
        so = self._reset()
        d, cli, _ = _dispatcher({7})
        try:
            asyncio.run(d.handle_message(_dm("/yolo maybe")))
            assert so.is_active() is False, "an unrecognised verb must never arm the grant"
            assert "Usage:" in cli.sent[-1][0]
        finally:
            self._reset()

    def test_grant_is_read_per_request_not_captured_at_boot(self) -> None:
        # The predicate handed to TurnDriver must consult the LIVE grant, or
        # /yolo would only take effect after a gateway restart. Mutating the
        # grant between calls proves the closure is not a captured snapshot.
        so = self._reset()
        d, _cli, _sess = _dispatcher({7})
        captured: list = []

        class _Recorder:
            def __init__(self, *a: Any, **kw: Any) -> None:
                captured.append(kw.get("auto_approve_session"))

            async def run(self, message: str) -> str:
                return "ok"

        try:
            with patch("kiro_crew.telegram.transport_dispatch.TurnDriver", _Recorder):
                asyncio.run(d.handle_message(_dm("hi")))
            predicate = captured[0]
            assert predicate is not None
            assert predicate() is False
            so.activate("test")
            assert predicate() is True, "the predicate must re-read the grant, not cache it"
        finally:
            self._reset()


class TestModelPicker:
    """/model is button-only: the user picks from what the backend advertised."""

    _MODELS = [
        {"modelId": "claude-opus-5", "name": "Opus 5"},
        {"modelId": "gpt-5.6-sol", "name": "GPT 5.6 Sol"},
    ]

    def _with_models(self, models: list | None = None) -> Any:
        d, cli, sess = _dispatcher({7})
        sess._gp.advertised = list(self._MODELS if models is None else models)
        return d, cli, sess

    def test_posts_one_button_per_model_plus_auto(self) -> None:
        d, cli, _ = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        text, markup = cli.sent[-1]
        rows = markup["inline_keyboard"]
        assert [r[0]["callback_data"] for r in rows] == ["m:0", "m:1", "m:2"]
        assert "Auto" in rows[0][0]["text"]
        assert "Opus 5" in rows[1][0]["text"]
        assert "Current model:" in text

    def test_callback_data_stays_inside_the_64_byte_cap(self) -> None:
        # Telegram rejects callback_data over 64 bytes and real model ids run
        # long, which is why the button carries an index, never the id.
        long_id = "a" * 300
        d, cli, _ = self._with_models([{"modelId": long_id, "name": long_id}])
        asyncio.run(d.handle_message(_dm("/model")))
        for row in cli.sent[-1][1]["inline_keyboard"]:
            assert len(row[0]["callback_data"].encode()) <= 64

    def test_press_switches_the_live_session_and_stores_the_pick(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        picker_mid = cli.sent[-1] and 101
        asyncio.run(d.on_callback(_press("m:2", message_id=picker_mid)))
        assert sess._gp.set_models == ["gpt-5.6-sol"]
        assert d._model_pref[("direct", "7")] == "gpt-5.6-sol"
        assert "Now using gpt-5.6-sol" in cli.edits[-1][1]
        assert cli.edits[-1][2] == {"inline_keyboard": []}, "the keyboard must be retired"

    def test_pick_flows_into_the_next_session(self) -> None:
        # The stored preference is only useful if session creation consumes it.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.handle_message(_dm("hi")))
        assert sess.last_model == "gpt-5.6-sol"

    def test_auto_is_recorded_without_a_wire_call(self) -> None:
        # There is no ACP id meaning "let the backend choose", so Auto can only
        # be recorded — claiming a live switch would be a lie.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:0")))
        assert sess._gp.set_models == []
        assert d._model_pref[("direct", "7")] == ""
        # A session is live here, and get_or_create returns a reused session
        # before it reads `model=`, so the pick CANNOT land on the next message —
        # promising that would be the same lie in different words.
        assert "/new" in cli.edits[-1][1]
        assert "next message" not in cli.edits[-1][1]

    def test_auto_with_no_live_session_does_promise_the_next_message(self) -> None:
        # The mirror case: with nothing live, the next message is what creates
        # the session, so it really does consume the preference then.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        sess._has = False
        asyncio.run(d.on_callback(_press("m:0")))
        assert "next message" in cli.edits[-1][1]

    def test_auto_reaches_session_creation_as_none_not_empty_string(self) -> None:
        # Auto must mean "as if never picked". get_or_create gates its own model
        # resolution on `model is None`, so handing it the Auto row's stored ""
        # would skip that resolution and land on the provider factory's narrower
        # fallback — a different model than an untouched conversation gets.
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:0")))
        asyncio.run(d.handle_message(_dm("hi")))
        assert sess.last_model is None

    def test_failed_switch_does_not_claim_success(self) -> None:
        d, cli, sess = self._with_models()
        sess._gp.set_model_error = RuntimeError("model not available")
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        outcome = cli.edits[-1][1]
        assert "Couldn't switch" in outcome and "Now using" not in outcome

    def test_busy_session_defers_instead_of_racing_the_turn(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        sess._busy = True  # try_acquire refuses -> no JSON-RPC on a live channel
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == []
        assert "still running" in cli.edits[-1][1]
        assert d._model_pref[("direct", "7")] == "gpt-5.6-sol"

    def test_advertised_id_is_sent_verbatim(self) -> None:
        # An advertised id is already what this backend accepts. Routing it
        # through the canonical registry would REWRITE some ids into a different
        # model (model_registry.to_acp_id("opus-4.8-1m") == "claude-opus-4.8"),
        # so the picked id must reach set_model untouched.
        d, cli, sess = self._with_models([{"modelId": "opus-4.8-1m", "name": "Opus 4.8 (1M)"}])
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:1")))
        assert sess._gp.set_models == ["opus-4.8-1m"]

    def test_no_advertised_models_asks_for_a_message_first(self) -> None:
        d, cli, _ = self._with_models([])
        asyncio.run(d.handle_message(_dm("/model")))
        assert "send a message first" in cli.sent[-1][0]
        assert cli.sent[-1][1] is None, "no keyboard when there is nothing to pick"

    def test_expired_picker_refuses_to_act_on_a_stale_list(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        picker = d._model_pickers["7:101"]
        picker.created_at -= _MODEL_PICKER_TTL_SECS + 1
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_unknown_picker_is_reported_not_ignored(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.on_callback(_press("m:0", message_id=999)))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_out_of_range_index_cannot_pick_a_model(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:99")))
        assert sess._gp.set_models == []
        assert "no longer active" in cli.edits[-1][1]

    def test_second_press_cannot_apply_twice(self) -> None:
        d, cli, sess = self._with_models()
        asyncio.run(d.handle_message(_dm("/model")))
        asyncio.run(d.on_callback(_press("m:2")))
        asyncio.run(d.on_callback(_press("m:2")))
        assert sess._gp.set_models == ["gpt-5.6-sol"], "the picker must be single-use"


class TestBareMidTurnDirective:
    def test_bare_queue_gets_usage_not_a_model_turn(self) -> None:
        d, cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue")))
        assert "Those take a message" in cli.sent[-1][0]
        assert sess.successes == [], "the bare token must never reach the model"

    def test_queue_with_a_body_still_runs_as_content(self) -> None:
        d, _cli, sess = _dispatcher({7})
        asyncio.run(d.handle_message(_dm("/queue do the thing")))
        assert sess.successes == ["telegram:kirocrew:direct:7"]


class TestSetMyCommands:
    def test_empty_list_is_refused_so_the_menu_is_never_wiped(self) -> None:
        # Telegram reads an empty array as "this bot has no commands" and clears
        # the menu, so an empty payload must not reach the wire.
        client = TelegramClient(token="t")
        calls: list = []

        async def _api(method: str, params: dict, *a: Any, **kw: Any) -> Any:
            calls.append(method)
            return True

        client._api = _api  # type: ignore[assignment]
        assert asyncio.run(client.set_my_commands([])) is False
        assert calls == []

    def test_publishes_the_catalogue(self) -> None:
        client = TelegramClient(token="t")
        sent: list = []

        async def _api(method: str, params: dict, *a: Any, **kw: Any) -> Any:
            sent.append((method, params))
            return True

        client._api = _api  # type: ignore[assignment]
        assert asyncio.run(client.set_my_commands(bot_command_payload())) is True
        assert sent[0][0] == "setMyCommands"
        assert {"command": "model", "description": "Choose the model from a list"} in sent[0][1][
            "commands"
        ]


# ── context thresholds ───────────────────────────────────────────────────


class TestContextThresholdNotices:
    def test_soft_threshold_nudges(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.check_context_usage = lambda key, provider: 85.0  # >= soft (80)

        asyncio.run(d._maybe_notice(7, ("direct", "7"), "key", object()))

        assert any("/compact" in s[0] for s in cli.sent)

    def test_below_soft_threshold_stays_silent(self) -> None:
        d, cli, sess = _dispatcher({7})
        sess.check_context_usage = lambda key, provider: 10.0

        asyncio.run(d._maybe_notice(7, ("direct", "7"), "key", object()))

        assert cli.sent == []
