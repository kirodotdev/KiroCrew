"""Per-channel outbound renderers strip recognized control-tag comments.

The channel-NEUTRAL sinks (``messaging.renderer.display_safe``) already strip
``<!-- keep-visible -->`` / ``<!-- deliver:... -->`` / ``<!-- plan_task_id:... -->``
lines. These tests pin the same backstop on the four per-channel STREAMING
renderers — Slack's append-only stream with its bracket-hold, and the live
Discord / Telegram / Webex frames plus their sealed answers — where a tag can
arrive split across chunks and so needs HOLD handling, not a post-hoc strip.

Only RECOGNIZED tags are control tags. A visible quoted comment such as
`` `<!-- ordinary -->` `` in inline code is rendered content and must survive on
every surface: a whole-comment strip is the hazard the scoped grammar exists to
avoid.
"""

from __future__ import annotations

import asyncio

import pytest
from test_discord import FakeClient as DiscordFake
from test_slack_renderer import _RecSlack
from test_telegram import FakeClient as TelegramFake
from test_webex_renderer import FakeClient as WebexFake

from kiro_crew.constants import split_trailing_protocol_suffix, strip_control_comments
from kiro_crew.discord.renderer import DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.slack.handler import _filter_options_brackets, _resolve_comment_hold
from kiro_crew.slack.renderer import SlackRenderer
from kiro_crew.telegram.renderer import TelegramRenderer
from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
from kiro_crew.webex.renderer import WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES

TAG = "<!-- keep-visible -->"
DELIVER = "<!-- deliver:slack -->"
QUOTED = "the `<!-- ordinary -->` comment and the `<!-- keep-visible -->` tag stay"


# ── shared grammar: the streaming (hide_partial) form ──────────────────────


class TestStripControlCommentsHidePartial:
    def test_partial_tail_is_held_back(self) -> None:
        # A live frame renders text still arriving: a tail that can still
        # BECOME a control-tag line is held, like split_options_trailer's
        # hide_partial does for ``[OPTIONS``.
        for partial in (
            "<",
            "<!",
            "<!--",
            "<!-- ",
            "<!-- keep",
            "<!-- keep-visible",
            "<!-- keep-visible --",
        ):
            assert strip_control_comments(f"Report\n{partial}", hide_partial=True) == "Report"
        for partial in (
            "<!-- deliver",
            "<!-- deliver:sl",
            "<!-- deliver:slack --",
            "<!-- plan_task_id:ab",
        ):
            assert strip_control_comments(f"Report\n{partial}", hide_partial=True) == "Report"

    def test_partial_then_complete_tags_all_go(self) -> None:
        text = f"Report\n{TAG}\n<!-- deli"
        assert strip_control_comments(text, hide_partial=True) == "Report"

    def test_grammar_dead_tail_is_prose(self) -> None:
        # Once a byte diverges from every recognized tag the tail is prose (or
        # an ordinary comment) and stays — even on a live frame.
        for dead in (
            "<!-- ordin",
            "<!-- ordinary -->",
            "<div",
            "<!-- keep-visible -->x",
            "<!--keepx",
        ):
            text = f"Report\n{dead}"
            assert strip_control_comments(text, hide_partial=True) == text

    def test_quoted_and_mid_line_tags_survive(self) -> None:
        assert strip_control_comments(QUOTED, hide_partial=True) == QUOTED
        mid = "a < b and <!-- keep"  # not line-leading: prose
        assert strip_control_comments(mid, hide_partial=True) == mid
        indented = "Report\n    <!-- keep-vis"  # 4+ spaces: indented code block
        assert strip_control_comments(indented, hide_partial=True) == indented

    def test_partial_inside_open_fence_is_code(self) -> None:
        text = "```\n<!-- keep-vis"
        assert strip_control_comments(text, hide_partial=True) == text

    def test_default_keeps_a_partial_tail(self) -> None:
        # The BUFFERED form (final answers) never cuts a partial: with the
        # stream over, an unterminated tail is the assistant's own prose.
        text = "Report\n<!-- keep-vis"
        assert strip_control_comments(text) == text


class TestSplitTrailingProtocolSuffixControlTags:
    def test_trailing_tag_is_protocol(self) -> None:
        assert split_trailing_protocol_suffix(f"body\n{TAG}") == ("body", f"\n{TAG}")

    def test_partial_tag_is_not_protocol(self) -> None:
        # A consumer that sends once (WhatsApp's final render) discards the
        # detached suffix, and an unfinished tag is prose under the buffered
        # rule -- so it stays with the body.
        text = "body\n<!-- keep-vis"
        assert split_trailing_protocol_suffix(text) == (text, "")

    def test_tag_before_options_rides_with_the_options(self) -> None:
        text = f"body\n{TAG}\n[OPTIONS: a | b]"
        assert split_trailing_protocol_suffix(text) == ("body", f"\n{TAG}\n[OPTIONS: a | b]")

    def test_tag_after_options_rides_with_the_options(self) -> None:
        text = f"body\n[OPTIONS: a | b]\n{TAG}"
        assert split_trailing_protocol_suffix(text) == ("body\n", f"[OPTIONS: a | b]\n{TAG}")

    def test_quoted_tag_is_not_protocol(self) -> None:
        assert split_trailing_protocol_suffix(QUOTED) == (QUOTED, "")


# ── Slack: append-only stream, hold equivalent to the [OPTIONS:] bracket-hold ──


class TestSlackStreamHold:
    def test_chunk_split_tag_is_held_then_dropped_at_the_seal(self) -> None:
        hold, buf = _filter_options_brackets("Report\n<!-- keep-", "", "")
        assert "keep" not in buf, "a possible tag must be HELD, not streamed"
        hold, buf = _filter_options_brackets("visible -->", hold, buf)
        # Still held: an append-only stream learns whether a complete tag is
        # the TAIL only from what follows it.
        assert hold == TAG and buf == "Report\n"
        hold, release = _resolve_comment_hold(hold, f"Report\n{TAG}")
        assert hold == "" and release == ""

    def test_deliver_tag_is_dropped_at_the_seal(self) -> None:
        hold, buf = _filter_options_brackets(f"Report\n{DELIVER}\n", "", "")
        assert hold == f"{DELIVER}\n" and buf == "Report\n"
        assert _resolve_comment_hold(hold, f"Report\n{DELIVER}\n") == ("", "")

    def test_content_after_a_complete_tag_releases_it(self) -> None:
        # A tag mid-message (a fenced example, prose after it) is visible
        # content, exactly as the buffered tail grammar reads it.
        fenced = "```md\n<!-- keep-visible -->\n```\ndone"
        hold, buf = _filter_options_brackets(fenced, "", "")
        assert hold == "" and buf == fenced

    def test_byte_that_ends_a_hold_is_still_judged(self) -> None:
        # The ``[`` that proves a tag is not the tail must still open the
        # bracket-hold, or the OPTIONS marker after it would stream raw.
        hold, buf = _filter_options_brackets(f"Report\n{TAG}\n[OPTIONS: a | b]", "", "")
        assert hold == "" and buf == f"Report\n{TAG}\n"

    def test_tail_inside_an_open_fence_is_released_at_the_seal(self) -> None:
        text = "```\n<!-- keep-visible -->"
        hold, buf = _filter_options_brackets(text, "", "")
        assert hold == TAG and buf == "```\n"
        assert _resolve_comment_hold(hold, text) == ("", TAG)

    def test_never_completed_prefix_is_released_at_the_seal(self) -> None:
        text = "Report\n<!-- keep-vis"
        hold, buf = _filter_options_brackets(text, "", "")
        assert hold == "<!-- keep-vis"
        assert _resolve_comment_hold(hold, text) == ("", "<!-- keep-vis")

    def test_three_maximal_stacked_tags_stay_held(self) -> None:
        # Every tail the grammar admits fits the hold: three families stacked
        # once each at the 256-byte body bound.
        tail = (
            "<!-- keep-visible -->\n"
            + f"<!-- deliver:{'d' * 249} -->\n"
            + f"<!-- plan_task_id:{'p' * 243} -->"
        )
        hold, buf = _filter_options_brackets(f"Report\n{tail}", "", "")
        assert hold == tail and buf == "Report\n"
        assert _resolve_comment_hold(hold, f"Report\n{tail}") == ("", "")

    def test_options_hold_is_not_resolved_here(self) -> None:
        assert _resolve_comment_hold("[OPTI", "x [OPTI") == ("[OPTI", "")

    def test_ordinary_comment_is_released_verbatim(self) -> None:
        hold, buf = _filter_options_brackets("Report\n<!-- ordinary -->", "", "")
        assert hold == "" and buf == "Report\n<!-- ordinary -->"

    def test_non_comment_angle_is_released(self) -> None:
        hold, buf = _filter_options_brackets("<div>\na < b", "", "")
        assert hold == "" and buf == "<div>\na < b"

    def test_mid_line_quoted_tag_is_prose(self) -> None:
        hold, buf = _filter_options_brackets(QUOTED, "", "")
        assert hold == "" and buf == QUOTED

    def test_line_break_inside_hold_releases(self) -> None:
        hold, buf = _filter_options_brackets("<!-- keep\nvisible -->", "", "")
        assert hold == "" and buf == "<!-- keep\nvisible -->"

    def test_options_hold_still_works(self) -> None:
        hold, buf = _filter_options_brackets("hi [OPTIONS: a | b] bye", "", "")
        assert hold == "" and buf == "hi  bye"


def _appended(rec: _RecSlack) -> str:
    return "".join(kw["text"] for name, kw in rec.calls if name == "append_stream")


class TestSlackRenderer:
    def _renderer(self) -> tuple[SlackRenderer, _RecSlack]:
        rec = _RecSlack()
        r = SlackRenderer(rec, "C1", "t1", reactions_enabled=False)
        r._now = lambda: 1e9  # every chunk is past the edit throttle
        return r, rec

    def test_stream_never_carries_a_split_tag(self) -> None:
        async def scenario() -> None:
            r, rec = self._renderer()
            await r.on_text_chunk("Report\n<!-- keep-")
            await r.on_text_chunk("visible -->")
            await r.on_done()
            shown = _appended(rec)
            assert "keep-visible" not in shown
            assert "Report" in shown

        asyncio.run(scenario())

    def test_stream_keeps_a_quoted_ordinary_comment(self) -> None:
        async def scenario() -> None:
            r, rec = self._renderer()
            await r.on_text_chunk(QUOTED)
            await r.on_done()
            assert QUOTED in _appended(rec)

        asyncio.run(scenario())

    def test_stream_keeps_a_fenced_example_of_the_tag(self) -> None:
        async def scenario() -> None:
            r, rec = self._renderer()
            await r.on_text_chunk("Use it like this:\n```md\n<!-- keep-visible -->\n```\n")
            await r.on_text_chunk("That is all.")
            await r.on_done()
            assert "```md\n<!-- keep-visible -->\n```" in _appended(rec)

        asyncio.run(scenario())

    def test_no_stream_final_text_is_stripped(self) -> None:
        # Both the stream start and the placeholder failed -> on_done posts the
        # final answer from the accumulated text.
        async def scenario() -> None:
            r, rec = self._renderer()
            r._accumulated = f"Report\n{DELIVER}"
            await r.on_done()
            posted = [kw["text"] for name, kw in rec.calls if name == "post_message"]
            assert posted and all("deliver:" not in t for t in posted)
            assert any("Report" in t for t in posted)

        asyncio.run(scenario())


# ── Discord ────────────────────────────────────────────────────────────────


def _discord_texts(cli: DiscordFake) -> list[str]:
    return [t for t, _ in cli.sent] + [t for _m, t, _c in cli.edits]


class TestDiscordRenderer:
    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[DiscordRenderer, DiscordFake]:
        cli = DiscordFake()
        r = DiscordRenderer(cli, "chan1", DISCORD_CAPABILITIES, session_key="sk")  # type: ignore[arg-type]
        monkeypatch.setattr("kiro_crew.discord.renderer._EDIT_THROTTLE_S", 0.0)
        return r, cli

    @pytest.mark.asyncio
    async def test_split_tag_never_reaches_a_frame_or_the_seal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_text_chunk("Report\n<!-- keep-")
        await r.on_text_chunk("visible -->")
        await r.on_done()
        texts = _discord_texts(cli)
        assert texts and all("keep-visible" not in t for t in texts), texts
        assert any("Report" in t for t in texts)

    @pytest.mark.asyncio
    async def test_deliver_tag_is_stripped_from_the_seal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_text_chunk(f"Report\n{DELIVER}")
        await r.on_done()
        assert all("deliver:" not in t for t in _discord_texts(cli))

    @pytest.mark.asyncio
    async def test_quoted_tag_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_text_chunk(QUOTED)
        await r.on_done()
        assert any(
            "`<!-- ordinary -->`" in t and "`<!-- keep-visible -->`" in t
            for t in _discord_texts(cli)
        )


# ── Telegram ───────────────────────────────────────────────────────────────


def _telegram_texts(cli: TelegramFake) -> list[str]:
    return (
        [t for t, _ in cli.sent]
        + [t for _m, t, _k in cli.edits]
        + [t for _d, t in cli.drafts]
        + [t for t, _k, _th in cli.rich_sent]
    )


class TestTelegramRenderer:
    def _renderer(self, monkeypatch: pytest.MonkeyPatch) -> tuple[TelegramRenderer, TelegramFake]:
        cli = TelegramFake()
        r = TelegramRenderer(cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0")  # type: ignore[arg-type]
        monkeypatch.setattr("kiro_crew.telegram.renderer._EDIT_THROTTLE_S", 0.0)
        return r, cli

    @pytest.mark.asyncio
    async def test_split_tag_never_reaches_a_frame_or_the_seal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        await r.on_text_chunk("Report\n<!-- keep-")
        await r.on_text_chunk("visible -->")
        await r.on_done()
        texts = _telegram_texts(cli)
        assert texts and all("keep-visible" not in t for t in texts), texts
        assert any("Report" in t for t in texts)

    @pytest.mark.asyncio
    async def test_deliver_tag_is_stripped_from_the_seal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        await r.on_text_chunk(f"Report\n{DELIVER}")
        await r.on_done()
        assert all("deliver:" not in t for t in _telegram_texts(cli))

    @pytest.mark.asyncio
    async def test_quoted_tag_survives(self, monkeypatch: pytest.MonkeyPatch) -> None:
        r, cli = self._renderer(monkeypatch)
        await r.on_turn_start()
        await r.on_text_chunk(QUOTED)
        await r.on_done()
        final = cli.final_text()
        assert "ordinary" in final and "keep-visible" in final


# ── Webex ──────────────────────────────────────────────────────────────────


class TestWebexRenderer:
    def _renderer(self) -> tuple[WebexRenderer, WebexFake]:
        cli = WebexFake()
        r = WebexRenderer(cli, "ROOM", WEBEX_CAPABILITIES, thread_id="", uploads_allowed=False)
        return r, cli

    @pytest.mark.asyncio
    async def test_status_frame_hides_a_partial_tag(self) -> None:
        r, _cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Report\n<!-- keep-vis")
        assert r.text() == "Report"

    @pytest.mark.asyncio
    async def test_final_answer_is_stripped(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("Report\n<!-- keep-")
        await r.on_text_chunk("visible -->")
        await r.on_done()
        assert cli.edits == [("MSG1", "ROOM", "Report")]

    @pytest.mark.asyncio
    async def test_deliver_variant_is_stripped(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk(f"Report\n{DELIVER}")
        await r.on_done()
        assert cli.edits == [("MSG1", "ROOM", "Report")]

    @pytest.mark.asyncio
    async def test_quoted_tag_survives(self) -> None:
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk(QUOTED)
        await r.on_done()
        assert cli.edits == [("MSG1", "ROOM", QUOTED)]

    @pytest.mark.asyncio
    async def test_indented_code_reply_is_not_protocol(self) -> None:
        # A reply that IS a 4-space-indented tag line is an indented code
        # block: the strip has to see the indent before the whitespace trim
        # removes it, on the status frame and on the answer alike.
        r, cli = self._renderer()
        await r.on_turn_start()
        await r.on_text_chunk("    <!-- keep-visible -->")
        assert r.text() == "<!-- keep-visible -->"
        await r.on_done()
        assert cli.edits == [("MSG1", "ROOM", "<!-- keep-visible -->")]
