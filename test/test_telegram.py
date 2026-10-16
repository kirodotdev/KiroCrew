"""Unit tests for the Telegram channel on the messaging-transport abstraction.

Covers: command parsing + conversation state (commands.py), text chunking +
tag-safe truncation + the Bot API duration metric + [OPTIONS:] extraction +
inline keyboards (renderer.py, client.py), and deny-by-default auth +
capabilities + inbound normalization (transport.py). The rest of the channel's
tests are the ``test_telegram_*.py`` files beside this one; their shared doubles
live in ``telegram_helpers``, and the ones other test files import from here
are re-exported below.
"""

from __future__ import annotations

import asyncio
from typing import Any

from telegram_helpers import FakeCtx  # noqa: F401
from telegram_helpers import FakeProvider  # noqa: F401
from telegram_helpers import _cfg  # noqa: F401
from telegram_helpers import _dispatcher  # noqa: F401
from telegram_helpers import _dm  # noqa: F401
from telegram_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _Ev  # noqa: F401
from telegram_helpers import _origin  # noqa: F401
from telegram_helpers import _press  # noqa: F401
from telegram_helpers import _prime_live  # noqa: F401
from telegram_helpers import FakeClient

from conftest import assert_rejected_without_backtracking
from kiro_crew.dashboard.token_auth import parse_duration
from kiro_crew.messaging.commands import parse_dashboard_ttl
from kiro_crew.messaging.renderer import session_provenance_tag
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.telegram.client import (
    TELEGRAM_CHUNK_LIMIT,
    TELEGRAM_MAX_TEXT,
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
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_dashboard_argument,
    parse_mid_turn_override,
)
from kiro_crew.telegram.renderer import TelegramRenderer  # noqa: F401
from kiro_crew.telegram.renderer import (
    _extract_options,
    _may_exceed_rendered,
    _md_to_telegram_html,
    _rendered_len,
    _split_markdown,
    _split_markdown_bounded,
    build_inline_keyboard,
)
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES, TelegramTransport

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
        assert parse_command("/session@KiroCrewBot launch", "KiroCrewBot") == "sessions"

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
    """Telegram's half of ``/kirocrew dashboard [<ttl>]`` is the WORD COUNT.

    The TTL vocabulary, the default and the formatter are channel-neutral and live
    in ``messaging/commands.py`` (pinned in ``test_messaging_commands.py``); what
    stays here is that the argument starts after BOTH command tokens, and that the
    composition the dispatcher performs still resolves every duration the way it
    did when one function did both jobs.
    """

    def _ttl(self, text: str) -> int:
        return parse_dashboard_ttl(parse_dashboard_argument(text), parse_duration=parse_duration)

    def test_default_ttl(self) -> None:
        """Default is 1 hour when no TTL specified."""
        assert self._ttl("/kirocrew dashboard") == 3600

    def test_hours(self) -> None:
        assert self._ttl("/kirocrew dashboard 2h") == 7200
        assert self._ttl("/kirocrew dashboard 5H") == 18000

    def test_minutes(self) -> None:
        assert self._ttl("/kirocrew dashboard 30m") == 1800
        assert self._ttl("/kirocrew dashboard 90M") == 5400

    def test_invalid_ttl_uses_default(self) -> None:
        """Invalid TTL format falls back to 1 hour."""
        assert self._ttl("/kirocrew dashboard xyz") == 3600
        assert self._ttl("/kirocrew dashboard") == 3600

    def test_the_argument_starts_after_both_command_tokens(self) -> None:
        # A channel whose dashboard command is ONE token must not inherit this
        # offset, which is why the shared parser takes the argument, not the text.
        assert parse_dashboard_argument("/kirocrew dashboard 2h") == "2h"
        # Telegram's clients append @BotUsername to a slash command in any chat
        # with more than one participant, which must not shift the argument.
        assert parse_dashboard_argument("/kirocrew@KiroCrewBot dashboard 2h") == "2h"
        assert parse_dashboard_argument("/kirocrew dashboard") == ""
        assert parse_dashboard_argument("/kirocrew") == ""


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
    """Retargeted at ``_split_markdown``, the only entry point left.

    The channel-local ``_split_text`` was removed with the backtick-parity
    rebalancer it fed; ``_split_markdown`` now delegates to the shared
    ``split_markdown_safe``. These cases carry over unchanged because they cover
    fence-free text, where the shared splitter uses the same cut ladder.
    """

    def test_short_text_single_chunk(self) -> None:
        assert _split_markdown("hello", TELEGRAM_CHUNK_LIMIT) == ["hello"]

    def test_long_text_chunks_within_limit(self) -> None:
        text = "\n\n".join("para " + "x" * 500 for _ in range(20))
        chunks = _split_markdown(text, TELEGRAM_CHUNK_LIMIT)
        assert len(chunks) > 1
        assert all(len(c) <= TELEGRAM_CHUNK_LIMIT for c in chunks)

    def test_no_content_lost_when_hard_split(self) -> None:
        text = "y" * (TELEGRAM_CHUNK_LIMIT * 2 + 100)  # no break points
        chunks = _split_markdown(text, TELEGRAM_CHUNK_LIMIT)
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
        # Backing out of a raw `&` in an attribute value must not drag the cut
        # into the middle of a COMPLETE tag (emitting `<a href="u?x=1`).
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
        # A transport failure must record an outcome; recording NOTHING hides the longest stalls.
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
        # The gate must not model only html.escape + links: those shapes return
        # False ("provably fits") while rendering far past the cap, so oversize
        # HTML reaches the client and loses its tail.
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
    _SESSION_KEY = "telegram:kirocrew:direct:7"

    def test_none_when_no_options(self) -> None:
        assert build_inline_keyboard([], self._SESSION_KEY) is None

    def test_callback_data_is_session_tagged_and_byte_safe(self) -> None:
        # Multi-byte (CJK) labels never enter callback_data. The compact digest
        # binds each index to the session that posted it while staying below the
        # Bot API's 64-byte ceiling.
        kb = build_inline_keyboard(
            ["开始实现 Tier 0 的完整方案很长的选项文字", "B"], self._SESSION_KEY
        )
        assert kb is not None
        tag = session_provenance_tag(self._SESSION_KEY)
        data = [btn["callback_data"] for row in kb["inline_keyboard"] for btn in row]
        assert data == [f"opt:0:{tag}", f"opt:1:{tag}"]
        assert all(len(value.encode("utf-8")) <= 64 for value in data)

    def test_two_buttons_per_row(self) -> None:
        kb = build_inline_keyboard(["a", "b", "c"], self._SESSION_KEY)
        assert kb is not None
        assert len(kb["inline_keyboard"][0]) == 2
        assert len(kb["inline_keyboard"][1]) == 1

    def test_long_labels_get_one_button_per_row(self) -> None:
        # Labels that differ only late in the text would be cut to the same
        # visible prefix on a half-width phone button.
        opts = ["Samstag 10:00 Uhr buchen", "Sonntag 10:00 Uhr buchen"]
        kb = build_inline_keyboard(opts, self._SESSION_KEY)
        assert kb is not None
        rows = kb["inline_keyboard"]
        assert [len(row) for row in rows] == [1, 1]
        assert [row[0]["text"] for row in rows] == opts

    def test_one_long_label_puts_every_button_on_its_own_row(self) -> None:
        opts = ["Yes", "No", "Show me the full diff first", "Skip"]
        kb = build_inline_keyboard(opts, self._SESSION_KEY)
        assert kb is not None
        rows = kb["inline_keyboard"]
        assert [len(row) for row in rows] == [1, 1, 1, 1]
        tag = session_provenance_tag(self._SESSION_KEY)
        assert [row[0]["callback_data"] for row in rows] == [
            f"opt:{i}:{tag}" for i in range(len(opts))
        ]

    def test_label_at_half_width_limit_keeps_two_per_row(self) -> None:
        opts = ["x" * 16, "y" * 16]
        kb = build_inline_keyboard(opts, self._SESSION_KEY)
        assert kb is not None
        assert [len(row) for row in kb["inline_keyboard"]] == [2]
        kb = build_inline_keyboard(["x" * 17, "y"], self._SESSION_KEY)
        assert kb is not None
        assert [len(row) for row in kb["inline_keyboard"]] == [1, 1]

    def test_cjk_label_is_measured_by_display_width(self) -> None:
        # Eight ideographs fill 16 cells; nine overflow a half-width button
        # even though they are only nine code points.
        kb = build_inline_keyboard(["显示完整差异然后", "跳过"], self._SESSION_KEY)
        assert kb is not None
        assert [len(row) for row in kb["inline_keyboard"]] == [2]
        kb = build_inline_keyboard(["显示完整差异然后再", "跳过"], self._SESSION_KEY)
        assert kb is not None
        assert [len(row) for row in kb["inline_keyboard"]] == [1, 1]


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
        # and many repeated "[OPTIONS:" prefixes (the real pump) must both be
        # rejected in CPU time linear in the pump -- see
        # conftest.assert_rejected_without_backtracking for why this is not a
        # 1.0 s wall-clock bound.
        def reject(text: str) -> None:
            body, opts = _extract_options(text)
            assert opts == []

        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" + ("\t" * n) + "x")
        assert_rejected_without_backtracking(reject, lambda n: "[OPTIONS:" * n + "x")


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
