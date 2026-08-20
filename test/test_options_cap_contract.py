"""Cross-channel contract: ``max_buttons`` is ENFORCED, per channel.

The capability ledger (``test_capability_ledger.py``) says the field is
enforced; THIS file is what makes that claim unforgeable. For every channel
declaring ``max_buttons > 0`` it drives the real options path with an
over-cap list and pins:

1. exactly ``max_buttons`` choices render interactively, and
2. the overflow degrades to a numbered text list (numbering continues after
   the widget slots) instead of being silently dropped — the pre-enforcement
   behavior lost choices without any user-visible signal.

``test_every_widget_channel_is_pinned_here`` is the ratchet: a channel that
starts declaring ``max_buttons > 0`` without a pin in this file fails it.
"""

from __future__ import annotations

import asyncio

from kiro_crew.messaging.renderer import apply_options_cap, cap_choices
from kiro_crew.messaging.transport import TransportCapabilities

#: channel_type -> the test class below that pins its enforcement.
PINNED_WIDGET_CHANNELS = {"slack", "discord", "telegram"}


def _all_channel_capabilities() -> dict[str, TransportCapabilities]:
    from kiro_crew.discord.transport import DISCORD_CAPABILITIES
    from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
    from kiro_crew.slack.transport import SLACK_CAPABILITIES
    from kiro_crew.teams.transport import TEAMS_CAPABILITIES
    from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
    from kiro_crew.webex.transport import WEBEX_CAPABILITIES
    from kiro_crew.wecom.transport import WECOM_CAPABILITIES
    from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES

    return {
        "slack": SLACK_CAPABILITIES,
        "discord": DISCORD_CAPABILITIES,
        "telegram": TELEGRAM_CAPABILITIES,
        "teams": TEAMS_CAPABILITIES,
        "webex": WEBEX_CAPABILITIES,
        "wecom": WECOM_CAPABILITIES,
        "weixin": WEIXIN_CAPABILITIES,
        "feishu": FEISHU_CAPABILITIES,
    }


class TestRatchet:
    def test_every_widget_channel_is_pinned_here(self) -> None:
        widget_channels = {
            name for name, caps in _all_channel_capabilities().items() if caps.max_buttons > 0
        }
        assert widget_channels == PINNED_WIDGET_CHANNELS, (
            "A channel's max_buttons declaration changed. Every channel "
            "declaring max_buttons > 0 must have an enforcement pin in this "
            f"file. unpinned={widget_channels - PINNED_WIDGET_CHANNELS} "
            f"stale={PINNED_WIDGET_CHANNELS - widget_channels}"
        )


class TestSharedHelper:
    def test_under_cap_is_byte_identical(self) -> None:
        caps = TransportCapabilities(max_buttons=3)
        body, kept = apply_options_cap("Choose.", ["A", "B"], caps)
        assert body == "Choose."
        assert kept == ["A", "B"]

    def test_overflow_degrades_to_numbered_text_continuing_the_widget_slots(self) -> None:
        caps = TransportCapabilities(max_buttons=2)
        body, kept = apply_options_cap("Pick one.", ["A", "B", "C", "D"], caps)
        assert kept == ["A", "B"]
        assert body == "Pick one.\n\n3. C\n4. D"

    def test_zero_cap_keeps_nothing_and_leaves_body_alone(self) -> None:
        caps = TransportCapabilities(max_buttons=0)
        body, kept = apply_options_cap("Text.", ["A", "B"], caps)
        assert body == "Text."
        assert kept == []

    def test_cap_choices_splits_without_formatting(self) -> None:
        caps = TransportCapabilities(max_buttons=1)
        kept, overflow = cap_choices(["A", "B", "C"], caps)
        assert kept == ["A"]
        assert overflow == ["B", "C"]

    def test_overflow_neutralizes_mass_mention_syntax(self) -> None:
        # Regression (review round 2): overflow lands in the message BODY
        # where platforms parse mentions — unlike widget labels, which render
        # as plain text. A prompt-injected choice must not mass-notify.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["ping @everyone now", "or <!channel> maybe"], start=1)
        assert "@everyone" not in out
        assert "<!channel>" not in out
        # The text stays human-readable — only the trigger syntax is broken.
        assert "everyone" in out and "channel" in out

    def test_overflow_redacts_credentials_in_their_DISPLAY_form(self) -> None:
        # Regression (review round 5): overflow lands in the markdown-parsed
        # BODY, so a key split by a code span or emphasis is broken to every
        # byte-level scan (the driver's stream redactor included) and WHOLE on
        # screen once the platform drops the delimiters. Slack's widget path
        # already routes choices through the display redactor for exactly this
        # reason; the shared sink has to close the same hole for telegram and
        # discord, which have no display-state pass of their own.
        from kiro_crew.messaging.renderer import format_overflow

        split = "AKIA`" + "`IOSFODNN7EXAMPLE"
        out = format_overflow([f"Retry with {split}"], start=1)
        assert "IOSFODNN7EXAMPLE" not in out, (
            "a backtick-split key survived the overflow sink — the platform "
            "strips the delimiters and shows the reader an intact credential"
        )

    def test_overflow_redaction_runs_before_mention_defanging(self) -> None:
        # Both sanitisations transform the text; if the ZWSP went in first it
        # could split a key so the regex stops matching while the platform
        # still renders it whole. Pin the order with a choice that needs both.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["@everyone use AKIA*IOSFODNN7EXAMPLE*"], start=0)
        assert "@everyone" not in out
        assert "IOSFODNN7EXAMPLE" not in out

    def test_overflow_redacts_a_spoiler_split_key(self) -> None:
        # Regression (review round 6): ``||…||`` is Discord's spoiler. The
        # reader clicks it, the delimiters vanish and the halves join — the
        # same splitter property as ``**``, but it was missing from the
        # canonicaliser's delimiter run, so round 5's fix had a hole exactly
        # one delimiter family wide.
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["Retry with AKIA||IOSFODNN7EXAMPLE||"], start=0)
        assert "IOSFODNN7EXAMPLE" not in out, (
            "a spoiler-split key survived — Discord joins the halves when the "
            "reader reveals the spoiler"
        )

    def test_overflow_redacts_an_invisible_character_split_key(self) -> None:
        # The invisible half of the same hazard, and worse than the markup half:
        # a zero-width character renders as NOTHING, so the reader sees an
        # intact key with no click and no markup while every literal scan sees
        # it broken. Pre-existing in the display redactor; closed here because
        # this sink is what puts LLM-authored choice text into the body.
        from kiro_crew.messaging.renderer import format_overflow

        for name, ch in (
            ("ZWSP", "\u200b"),
            ("ZWNJ", "\u200c"),
            ("word joiner", "\u2060"),
            ("BOM", "\ufeff"),
            ("soft hyphen", "\u00ad"),
        ):
            out = format_overflow([f"Retry with AKIA{ch}IOSFODNN7EXAMPLE"], start=0)
            assert "IOSFODNN7EXAMPLE" not in out, f"{name} split the key past the scan"

    def test_non_ascii_text_is_not_mangled(self) -> None:
        """The format-character filter must not touch visible non-ASCII text."""
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["重新部署到主分支", "café — naïve"], start=0)
        assert out == "1. 重新部署到主分支\n2. café — naïve"

    def test_a_lone_pipe_is_left_alone(self) -> None:
        """The pipe counts only in pairs — pinned so the boundary is deliberate.

        A single ``|`` is literal on every channel here, so collapsing it would
        widen the canonical form with no rendering that matches it. This also
        keeps ordinary table-ish text intact.
        """
        from kiro_crew.messaging.display_safety import canonicalize_display

        assert canonicalize_display("a|b") == "a|b"
        assert canonicalize_display("a||b") == "ab"

    def test_clean_choices_are_untouched_by_the_redactor(self) -> None:
        """The sink must not mangle ordinary text — no false-positive damage."""
        from kiro_crew.messaging.renderer import format_overflow

        out = format_overflow(["Rebase onto main", "Skip the `--force` flag"], start=2)
        assert out == "3. Rebase onto main\n4. Skip the `--force` flag"


class TestSlackEnforcement:
    def _choices(self, n: int) -> list[str]:
        return [f"Choice {i}" for i in range(1, n + 1)]

    def test_widget_caps_at_declared_and_overflow_is_visible(self) -> None:
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        blocks = build_options_blocks(self._choices(n + 3))
        actions = next(b for b in blocks if b["type"] == "actions")
        opts = actions["elements"][0]["options"]
        assert len(opts) == n
        overflow = next(b for b in blocks if b["type"] == "context")
        text = overflow["elements"][0]["text"]
        # Numbering continues after the widget slots; every dropped choice shows.
        assert f"{n + 1}. Choice {n + 1}" in text
        assert f"{n + 3}. Choice {n + 3}" in text

    def test_under_cap_emits_no_overflow_block(self) -> None:
        from kiro_crew.slack.format import build_options_blocks

        blocks = build_options_blocks(self._choices(2))
        assert [b["type"] for b in blocks] == ["actions"]

    def test_huge_overflow_is_chunked_not_sliced(self) -> None:
        # Regression (review round 1): a single [:2900] slice re-created the
        # silent data loss the cap exists to remove. Every overflow choice
        # must reach the wire, across as many context blocks as needed.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        long = [f"Choice {i} " + "x" * 140 for i in range(1, n + 41)]
        blocks = build_options_blocks(long)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) >= 2, "one sliced block would drop tail choices"
        joined = "".join(b["elements"][0]["text"] for b in ctx)
        assert f"{n + 40}." in joined, "the LAST overflow choice must survive"

    def test_pathological_overflow_is_bounded_with_visible_truncation(self) -> None:
        # Regression (review round 3): unbounded context blocks blow Slack's
        # 50-block message limit — the API rejects the WHOLE message and every
        # choice disappears. The block budget is capped and the tail drop is
        # VISIBLE (counted marker), never silent.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        huge = [f"Choice {i} " + "x" * 140 for i in range(1, n + 201)]
        blocks = build_options_blocks(huge)
        ctx = [b for b in blocks if b["type"] == "context"]
        assert len(ctx) <= 4, "block budget must be bounded"
        assert len(blocks) <= 5
        marker = ctx[-1]["elements"][0]["text"]
        assert "omitted" in marker
        # The marker counts what was dropped — no silent loss.
        assert any(ch.isdigit() for ch in marker)

    def test_single_oversized_choice_truncates_with_visible_marker(self) -> None:
        # Regression (review round 4): one absurd >2900-char choice was
        # sliced with no signal. The cut must be visible.
        from kiro_crew.slack.format import build_options_blocks
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        n = SLACK_CAPABILITIES.max_buttons
        choices = [f"Choice {i}" for i in range(1, n + 1)] + ["y" * 4000]
        blocks = build_options_blocks(choices)
        ctx = [b for b in blocks if b["type"] == "context"]
        text = ctx[0]["elements"][0]["text"]
        assert len(text) <= 2900
        assert text.endswith("…"), "truncation must be visible, not silent"


class TestTelegramEnforcement:
    def test_steer_seal_near_limit_with_overflow_stays_under_transport_cap(self) -> None:
        # Regression (review round 1): on_steer_consumed ran _rotate_on_length
        # BEFORE apply_options_cap expanded the body with numbered overflow, so
        # a near-limit pre-steer answer sealed past the transport cap.
        from test_telegram import FakeClient

        from kiro_crew.messaging.renderer import STEER_CONSUMED, TEXT_CHUNK, OutputEvent
        from kiro_crew.telegram.client import TELEGRAM_CHUNK_LIMIT
        from kiro_crew.telegram.renderer import TelegramRenderer
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        n = TELEGRAM_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice number {i} with a long label" for i in range(1, n + 9))
        near_limit = "x" * (TELEGRAM_CHUNK_LIMIT - 60)
        cli = FakeClient()
        r = TelegramRenderer(  # type: ignore[arg-type]
            cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(
                OutputEvent(kind=TEXT_CHUNK, text=f"{near_limit}\n\n[OPTIONS: {trailer}]")
            )
            await r.dispatch(OutputEvent(kind=STEER_CONSUMED, text="steered"))

        asyncio.run(_go())
        for text, _ in cli.sent:
            assert len(text) <= TELEGRAM_CHUNK_LIMIT
        for _, text, _ in cli.edits:
            assert len(text) <= TELEGRAM_CHUNK_LIMIT

    def test_keyboard_caps_at_declared_and_overflow_is_visible(self) -> None:
        from test_telegram import FakeClient

        from kiro_crew.messaging.renderer import DONE, TEXT_CHUNK, OutputEvent
        from kiro_crew.telegram.renderer import TelegramRenderer
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        n = TELEGRAM_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))
        cli = FakeClient()
        r = TelegramRenderer(  # type: ignore[arg-type]
            cli, 55, TELEGRAM_CAPABILITIES, session_key="telegram:1:0"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.dispatch(OutputEvent(kind=TEXT_CHUNK, text=f"Pick.\n\n[OPTIONS: {trailer}]"))
            await r.dispatch(OutputEvent(kind=DONE, stop_reason=""))

        asyncio.run(_go())
        kb = cli.final_markup()
        labels = [b["text"] for row in kb["inline_keyboard"] for b in row]
        assert len(labels) == n, "telegram keyboard was uncapped before enforcement"
        assert labels == [f"Choice {i}" for i in range(1, n + 1)]
        final = cli.final_text()
        assert f"{n + 1}. Choice {n + 1}" in final
        assert f"{n + 3}. Choice {n + 3}" in final


class TestDiscordEnforcement:
    def test_buttons_cap_at_declared_and_overflow_is_visible(self) -> None:
        import pytest_asyncio  # noqa: F401  (asyncio runner parity with test_discord)
        from test_discord import FakeClient

        from kiro_crew.discord.renderer import DiscordRenderer
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES

        n = DISCORD_CAPABILITIES.max_buttons
        trailer = " | ".join(f"Choice {i}" for i in range(1, n + 4))
        cli = FakeClient()
        r = DiscordRenderer(  # type: ignore[arg-type]
            cli, "chan1", DISCORD_CAPABILITIES, session_key="discord:u:c"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk(f"Pick.\n\n[OPTIONS: {trailer}]")
            await r.on_done()

        asyncio.run(_go())
        comps = cli.final_components()
        labels = [b["label"] for row in comps for b in row["components"]]
        assert len(labels) == n
        final = cli.final_text()
        assert f"{n + 1}. Choice {n + 1}" in final
        assert f"{n + 3}. Choice {n + 3}" in final

    def test_overflow_credential_is_redacted_on_the_real_render_path(self) -> None:
        """End-to-end: discord has no display-state pass of its own.

        Before enforcement the 26th+ choices were dropped entirely, so there
        was no exposure; routing them into the parsed body is what opened the
        surface this closes.
        """
        import pytest_asyncio  # noqa: F401  (asyncio runner parity with test_discord)
        from test_discord import FakeClient

        from kiro_crew.discord.renderer import DiscordRenderer
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES

        n = DISCORD_CAPABILITIES.max_buttons
        leaked = "AKIA`" + "`IOSFODNN7EXAMPLE"
        choices = [f"Choice {i}" for i in range(1, n + 1)] + [f"Retry with {leaked}"]
        cli = FakeClient()
        r = DiscordRenderer(  # type: ignore[arg-type]
            cli, "chan1", DISCORD_CAPABILITIES, session_key="discord:u:c"
        )

        async def _go() -> None:
            await r.on_turn_start()
            await r.on_text_chunk("Pick.\n\n[OPTIONS: " + " | ".join(choices) + "]")
            await r.on_done()

        asyncio.run(_go())
        assert "IOSFODNN7EXAMPLE" not in cli.final_text()
