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

from kiro_crew.messaging.transport import TransportCapabilities

#: Something behaves differently when the value changes. Cite the behaviour.
ENFORCED = {
    # Mirror-leg chunking (dashboard/chat_runner.py) + five renderers' chunk
    # size. CHARACTER count — byte-capped platforms must declare a byte-safe
    # char value (see webex).
    "max_message_chars",
    # Gates mirror-link creation (HTTP 400) and the outbound mirror leg.
    "supports_proactive_send",
}

#: Declared honestly, read by nothing yet. The capability-gated interface
#: work consumes these; until a field moves to ENFORCED, no code may assume
#: a gate exists behind it.
ASPIRATIONAL = {
    "streaming",
    "edit",
    "reactions",
    "files_inbound",
    "files_outbound",
    "rich_blocks",
    "threads",
    "max_buttons",
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

    def test_the_file_directions_are_declared_separately(self) -> None:
        # One boolean was undecidable: discord ingests but cannot upload,
        # slack does both. A gate reading a single `files` flag got the
        # wrong answer for one of them.
        from kiro_crew.discord.transport import DISCORD_CAPABILITIES
        from kiro_crew.slack.transport import SLACK_CAPABILITIES

        assert DISCORD_CAPABILITIES.files_inbound is True
        assert DISCORD_CAPABILITIES.files_outbound is False
        assert SLACK_CAPABILITIES.files_inbound is True
        assert SLACK_CAPABILITIES.files_outbound is True
