"""Unit tests for the Telegram channel on the messaging-transport abstraction.

Covers: command parsing + conversation state (commands.py), text chunking +
[OPTIONS:] extraction + inline keyboards (renderer.py), deny-by-default auth +
capabilities + inbound normalization (transport.py), streaming render +
finalization (renderer.py), the interactive approval decider, and the dispatch
turn + callback routing (transport_dispatch.py).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.messaging.link import ChannelLink, legacy_dashboard_mirror_key
from kiro_crew.messaging.renderer import (
    DONE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    TOOL_CALL,
    OutputEvent,
)
from kiro_crew.messaging.transport import InboundMessage
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
    ConversationState,
    parse_command,
    parse_mid_turn_override,
)
from kiro_crew.telegram.renderer import (
    TelegramApprovalDecider,
    TelegramRenderer,
    _extract_options,
    _may_exceed_rendered,
    _md_to_telegram_html,
    _rendered_len,
    _split_markdown,
    _split_markdown_bounded,
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
from kiro_crew.telegram.transport_dispatch import _STEER_ACK_EMOJI, TelegramDispatcher

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

    def __init__(self, reply: str = "Answer") -> None:
        self._reply = reply
        self.steered: list = []
        self.cancelled = 0
        self.active_turn = True  # gates _handle_busy's live-turn steer check

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
        self.successes: list[str] = []
        self.failures: list[str] = []
        self.last_agent: Any = None
        self.raise_on_get = raise_on_get
        self._busy = False
        self._has = True
        self.queued: list = []
        self._gp = FakeProvider()
        self.mirror_links: dict[str, Any] = {}
        self._pid: Any = None

    async def get_or_create(self, key: str, *, agent: Any = None, channel_id: Any = None) -> Any:
        self.last_agent = agent
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

    def set_mirror_link(self, key: str, link: Any) -> None:
        self.mirror_links[key] = link

    def clear_mirror_link(self, key: str, channel_type: str = "") -> bool:
        # Interface parity: a real clear is channel-scoped, because a session can
        # hold one binding per channel. This fake models a single binding per key,
        # so honouring the scope means only clearing when the channel matches.
        existing = self.mirror_links.get(key)
        if channel_type and (
            existing is None or getattr(existing, "channel_type", None) != channel_type
        ):
            return False
        return self.mirror_links.pop(key, None) is not None

    def clear_mirror_links_at(self, link: Any) -> list[str]:
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
) -> Any:
    return SimpleNamespace(
        telegram=SimpleNamespace(
            soft_threshold_pct=soft,
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids or [],
        ),
        agent=SimpleNamespace(default_agent=default_agent),
        messaging=SimpleNamespace(
            dm_scope="per-channel-peer",
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
) -> tuple[TelegramDispatcher, FakeClient, FakeSessions]:
    sess = FakeSessions(raise_on_get=raise_on_get)
    d = TelegramDispatcher(
        sessions=sess,  # type: ignore[arg-type]
        ctx_builder=FakeCtx(),  # type: ignore[arg-type]
        cfg=_cfg(
            default_agent=default_agent,
            allow_forum=allow_forum,
            allowed_forum_chat_ids=allowed_forum_chat_ids,
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
        assert TELEGRAM_CAPABILITIES.max_buttons == 8


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
        assert sess.destroyed == []  # healthy session preserved

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

    def test_drain_caps_collapse_and_defers_remainder(self) -> None:
        d, cli, sess = _dispatcher({7})
        # 52 queued -> the collapse cap (50) drains 50 into one turn and leaves
        # the 2-message remainder queued IN ORDER for the next turn (a 2+ item
        # remainder is what exposes any FIFO reordering of the surplus).
        sess.queued = [(f"t{i}", f"m{i}", {}) for i in range(52)]

        async def _go() -> None:
            await d._drain_queue("telegram:kirocrew:direct:7", 7, 7)

        asyncio.run(_go())
        # surplus (m50, m51) is deferred IN ORIGINAL ORDER, not dropped/reordered
        assert [text for _ts, text, _ in sess.queued] == ["m50", "m51"]
        assert sess.successes == ["telegram:kirocrew:direct:7"]  # one combined turn

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

    def test_unlink_leaves_other_locations_alone(self) -> None:
        # Exact-match sweep: a mirror into a DIFFERENT chat survives, and the
        # reply stays truthful when nothing points at this conversation.
        d, cli, sess = _dispatcher({7})
        other = ChannelLink("telegram", channel_id="8")
        sess.mirror_links["dashboard:chat-9"] = other
        asyncio.run(d._handle_unlink(("direct", "7"), 7))
        assert sess.mirror_links == {"dashboard:chat-9": other}
        assert any("wasn't linked" in t for t, _ in cli.sent)


def test_receipt_text_caps_displayed_items() -> None:
    # A large mid-turn burst must not grow the rendered receipt unbounded: only
    # the first _RECEIPT_MAX_ITEMS are listed verbatim; the count stays true.
    from kiro_crew.telegram.transport_dispatch import _RECEIPT_MAX_ITEMS, _receipt_text

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
