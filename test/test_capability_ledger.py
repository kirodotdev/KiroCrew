"""The capability ledger: every TransportCapabilities field is classified.

``TransportCapabilities`` drifted into being false documentation: flags were
declared, docstrings described gates, and nothing read most of the fields.
Measured 2026-08-02: 7 of 9 flags had ZERO read sites, five channel
declarations were provably wrong against their own code, and one docstring
promised a ``max_buttons`` degradation no renderer implements.

This module is the ratchet against that recurring. Two rules:

1. Every field is either ENFORCED (something behaves differently) or
   ASPIRATIONAL (declared honestly, consumed by nothing yet). Adding a field
   without classifying it here fails the mirror test.
2. A field moving from ASPIRATIONAL to ENFORCED must move sets here in the
   same change, so the ledger stays the map of what a declaration actually
   buys.

The declaration pins below assert the CORRECTED values, with each correction's
reason inline — they are honesty regressions, not tautologies.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from kiro_crew.messaging.transport import TransportCapabilities

#: Something behaves differently when the value changes. Cite the behaviour.
ENFORCED = {
    # Mirror-leg chunking (dashboard/chat_runner.py) + five renderers' chunk
    # size. CHARACTER count — byte-capped platforms must declare a byte-safe
    # char value (see webex).
    "max_message_chars",
    # Gates mirror-link creation (HTTP 400) and the outbound mirror leg.
    "supports_proactive_send",
    # Gates whether a dashboard connect marks the binding as an inbound resume
    # target (``accepts_inbound``) in dashboard/chat_mirror.py, and therefore
    # whether the slot row reports ``direction: both``. Only a transport whose
    # inbound path resolves the mirror binding may declare it.
    "supports_session_resume",
    # TOTAL interactive choices per [OPTIONS:] prompt. Widget-capable
    # renderers (slack/discord/telegram) route the parsed list through
    # messaging.renderer.apply_options_cap / cap_choices; overflow degrades
    # to a numbered text list instead of being silently dropped. Pinned per
    # channel by test_options_cap_contract.py. Channels declaring 0 render
    # no widget (trailer stripped; their text fallback is the
    # approval-ladder work).
    "max_buttons",
    # Gates whether a renderer extracts local image references out of a sealed
    # segment and uploads them (discord/renderer.py::_uploads_enabled ->
    # client.send_message_with_files). A channel declaring False keeps printing
    # the markdown path, which is the honest degradation -- never a silent drop.
    "files_outbound",
    # Read by Renderer.render_tables_for_target at every opted-in outbound
    # boundary. ``table_mode`` selects off/cards/grid/native/auto and
    # ``native_tables`` prevents an unsupported native claim from leaking raw
    # pipes. Pinned per channel by test_channel_table_rendering.py.
    "table_mode",
    "native_tables",
    # Decides whether an EMPTY message id from ``send_message`` means "refused"
    # (most platforms) or "delivered, this platform returns no id" (WeCom's
    # proactive command, Feishu's reply -- both RAISE on failure instead). Read
    # through messaging.transport.delivery_confirmed at all three proactive-send
    # call sites: slack/gateway.py's channel reply leg and both legs in
    # dashboard/handlers/messaging.py. Declaring it wrongly is silent in both
    # directions -- a lost message reported as delivered, or a delivered cron
    # result reported as lost with its dedup hash left unadvanced, repeating the
    # same result every tick. Pinned against each transport's own send_message,
    # by AST and in both directions, in
    # test_channel_transport_outbound_authz.py::TestTheMessageIdConventionIsDeclared.
    "returns_message_id",
}

#: Declared honestly, read by nothing yet. The capability-gated interface
#: work consumes these; until a field moves to ENFORCED, no code may assume
#: a gate exists behind it.
ASPIRATIONAL = {
    "streaming",
    "edit",
    "reactions",
    "files_inbound",
    "rich_blocks",
    "threads",
}


class TestLedgerCoversEveryField:
    def test_every_field_is_classified_exactly_once(self) -> None:
        declared = {f.name for f in fields(TransportCapabilities)}
        assert ENFORCED & ASPIRATIONAL == set(), "a field cannot be both"
        assert declared == ENFORCED | ASPIRATIONAL, (
            "TransportCapabilities changed without updating the ledger. "
            f"unclassified={declared - (ENFORCED | ASPIRATIONAL)} "
            f"stale={(ENFORCED | ASPIRATIONAL) - declared}. Classify the "
            "field above (and if you are enforcing one, move it to ENFORCED "
            "in the same change)."
        )

    def test_to_dict_stays_in_sync_with_the_fields(self) -> None:
        declared = {f.name for f in fields(TransportCapabilities)}
        assert set(TransportCapabilities().to_dict().keys()) == declared


class TestCorrectedDeclarations:
    """Pin each fixed declaration to the evidence that made it wrong."""

    def test_telegram_declares_the_threading_it_performs(self) -> None:
        # send_message forwards message_thread_id, receive() populates
        # InboundMessage.thread_id, forum_gate_outcome authorizes on it.
        # Declared False until 2026-08 while threading end to end.
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        assert TELEGRAM_CAPABILITIES.threads is True

    def test_telegram_declares_the_outbound_files_it_uploads(self) -> None:
        # renderer._send_uploads extracts local image references through the
        # shared messaging/outbound_files.py and uploads them via multipart
        # sendPhoto / sendMediaGroup. Declared False while the channel printed
        # filesystem paths; the declaration and the upload path move together.
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        assert TELEGRAM_CAPABILITIES.files_outbound is True

    def test_telegram_declares_the_rich_rendering_it_performs(self) -> None:
        # sendRichMessage carries every table-bearing seal (renderer._seal_text)
        # and renders structured markdown natively; inline keyboards carry the
        # interactive half. Declared False while doing both.
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        assert TELEGRAM_CAPABILITIES.rich_blocks is True

    def test_slack_declares_its_shipped_send_limit_not_the_platform_ceiling(self) -> None:
        # slack/format.py splits at SLACK_MSG_LIMIT (3900). The old 40000
        # would have let a capability-aware caller emit messages 10x larger
        # than the renderer ever sends.
        from kiro_crew.slack.format import SLACK_MSG_LIMIT
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        assert SLACK_CAPABILITIES.max_message_chars == SLACK_MSG_LIMIT

    def test_slack_has_exactly_one_declaration(self) -> None:
        # renderer.py used to carry a second literal copy; two literals for
        # one fact is how the 40000/3900 divergence survived.
        from kiro_crew.slack import renderer as slack_renderer
        from kiro_crew.slack import transport as slack_transport

        assert slack_renderer.SLACK_CAPABILITIES is slack_transport.SLACK_CAPABILITIES

    def test_webex_char_declaration_is_safe_under_its_byte_cap(self) -> None:
        # Webex caps messages in UTF-8 BYTES (WEBEX_MAX_TEXT) and its client
        # tail-truncates overflow. The declared CHAR count must be safe at
        # 4 bytes/char, or the mirror leg silently loses data on CJK text.
        from kiro_crew.webex.client import WEBEX_MAX_TEXT
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES

        assert WEBEX_CAPABILITIES.max_message_chars * 4 <= WEBEX_MAX_TEXT

    def test_teams_char_declaration_is_safe_under_its_byte_cap(self) -> None:
        # A Teams activity is JSON on the wire, so the cap is effectively in BYTES.
        # The declared CHAR count must survive the worst case (an astral codepoint
        # is 4 UTF-8 bytes) or the mirror leg silently loses a long non-ASCII reply
        # and a renderer chunk comes back 413.
        from kiro_crew.teams.client import (
            _MAX_UTF8_BYTES_PER_CHAR,
            TEAMS_MAX_ACTIVITY_TEXT_BYTES,
        )
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES

        assert (
            TEAMS_CAPABILITIES.max_message_chars * _MAX_UTF8_BYTES_PER_CHAR
            <= TEAMS_MAX_ACTIVITY_TEXT_BYTES
        )

    @pytest.mark.asyncio
    async def test_teams_serializes_without_ascii_escaping(self) -> None:
        # The multiplier above is only true with ensure_ascii=False. aiohttp's
        # default escapes every non-ASCII codepoint to \\uXXXX -- 6 bytes each, 12
        # for an astral pair -- which triples the worst case and breaks the pin
        # above without changing any number it reads. Asserted on the session the
        # client really builds, not on the literal passed to it.
        from kiro_crew.teams.client import TeamsClient

        client = TeamsClient(app_id="a", app_password="p")
        try:
            session = await client._ensure_session()
            assert session._json_serialize("✅🙂") == '"✅🙂"'
        finally:
            await client.close()

    def test_wecom_char_declaration_is_safe_under_its_byte_cap(self) -> None:
        # WeCom caps `stream.content` / `markdown.content` in UTF-8 BYTES
        # (20480). The declared CHARACTER count must be safe at 4 bytes/char, or
        # a CJK reply sits under the character cap and ~3x over the byte cap --
        # and WeCom rejects the whole frame, so the user gets nothing at all.
        # Declared 20000 CHARS until this was corrected.
        from kiro_crew.wecom.client import WECOM_MAX_REPLY_BYTES
        from kiro_crew.wecom.transport import WECOM_CAPABILITIES

        assert WECOM_CAPABILITIES.max_message_chars * 4 <= WECOM_MAX_REPLY_BYTES

    def test_the_file_directions_are_declared_separately(self) -> None:
        # One boolean was undecidable: the two directions land per channel and in
        # different changes, so a gate reading a single `files` flag got the wrong
        # answer for one of them.
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        assert DISCORD_CAPABILITIES.files_inbound is True
        assert DISCORD_CAPABILITIES.files_outbound is True
        assert SLACK_CAPABILITIES.files_inbound is True
        assert SLACK_CAPABILITIES.files_outbound is True

    def test_max_buttons_declares_totals_the_renderers_ship(self) -> None:
        # The field is TOTAL choices, not a per-row layout number. The old
        # values mixed the two: slack declared 5 (a buttons-per-actions-row
        # limit that does not govern its checkboxes widget) while shipping
        # 10; discord declared 5 (per row) while shipping 25 total; telegram
        # declared 8 (a mislabeled per-row number) while enforcing nothing.
        # Declare what ships: slack/discord keep their shipped maxima, and
        # telegram gets the same platform-practical 25 so previously-working
        # 9-25 choice keyboards keep working — only the genuinely unbounded
        # tail (the API-400 defect) degrades to text.
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES
        from kiro_crew.slack.transport import SLACK_CAPABILITIES
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES

        assert SLACK_CAPABILITIES.max_buttons == 10  # checkboxes options[] cap
        assert DISCORD_CAPABILITIES.max_buttons == 25  # 5 rows x 5 buttons
        assert TELEGRAM_CAPABILITIES.max_buttons == 25  # 2/row, parity with discord


class TestSessionResumeIsDeclaredOnlyWhereItIsHonoured:
    """``supports_session_resume`` is a promise the INBOUND path has to keep.

    A dashboard connect on a transport declaring it marks the binding
    ``accepts_inbound``, and the slot row then reports ``direction: both`` — the
    dashboard is telling the user that replies come back here. That is only true
    where the transport's inbound path resolves the mirror binding. Discord's
    does (``DiscordSessionResume.resumed_session``); every other transport builds
    a session key from the route alone and never looks the binding up, so a reply
    there runs in a SEPARATE session with none of this conversation's history.

    This pins the current set. A new transport declaring the flag fails here, and
    that is the point: the author has to come and confirm its inbound path really
    resolves the binding rather than inheriting a promise the code cannot keep.
    """

    def test_discord_declares_it(self) -> None:
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES

        assert DISCORD_CAPABILITIES.supports_session_resume is True, (
            "Discord stopped declaring session resume — dashboard connects there "
            "would silently become outbound-only and replies would stop resuming"
        )

    def test_no_other_transport_declares_it(self) -> None:
        from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
        from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
        from kiro_crew.slack.transport import SLACK_CAPABILITIES
        from kiro_crew.teams.transport import TEAMS_CAPABILITIES
        from kiro_crew.telegram.transport import TELEGRAM_CAPABILITIES
        from kiro_crew.webex.transport import WEBEX_CAPABILITIES
        from kiro_crew.wecom.transport import WECOM_CAPABILITIES
        from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
        from kiro_crew.whatsapp.transport import WHATSAPP_CAPABILITIES

        others = {
            "slack": SLACK_CAPABILITIES,
            "teams": TEAMS_CAPABILITIES,
            "telegram": TELEGRAM_CAPABILITIES,
            "webex": WEBEX_CAPABILITIES,
            "wecom": WECOM_CAPABILITIES,
            "weixin": WEIXIN_CAPABILITIES,
            "imessage": IMESSAGE_CAPABILITIES,
            "whatsapp": WHATSAPP_CAPABILITIES,
            "feishu": FEISHU_CAPABILITIES,
        }
        claiming = [name for name, caps in others.items() if caps.supports_session_resume]
        assert claiming == [], (
            f"{claiming} declare session resume, but their inbound paths derive a "
            "session key from the route and never resolve the mirror binding — the "
            "dashboard would promise a two-way link that drops replies. Slack is "
            "separate: it routes inbound through its own thread index and never "
            "sets `accepts_inbound`."
        )

    def test_the_default_is_off(self) -> None:
        """A transport that forgets the flag must degrade to outbound-only."""
        assert TransportCapabilities().supports_session_resume is False
