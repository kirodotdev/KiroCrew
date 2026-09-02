"""WhatsApp command table: routing, whole-message matching, derived help.

The two properties worth pinning are that the table is the single source of
truth (a row is routable, documented and classified by construction) and that
matching stays whole-message: ``whatsapp/transport.py`` drops a non-operator
group message whenever ``parse_command`` is truthy, so a prefix match would
silently swallow ordinary group chatter that opens with a slash.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from kiro_crew.whatsapp import commands
from kiro_crew.whatsapp.commands import (
    COMMANDS,
    WhatsAppCommand,
    command_argument,
    help_text,
    is_operator_only,
    parse_command,
)

#: The surface this channel commits to. Spelled out here rather than derived
#: from the table so that dropping or renaming a row fails a test instead of
#: quietly shrinking what a phone user can do.
EXPECTED_NAMES = ("new", "compact", "help", "status", "stop")

#: Commands that only the linked account may run. ``help`` discloses the
#: command list and nothing else, so it is the one public row.
EXPECTED_OPERATOR_ONLY = {
    "new": True,
    "compact": True,
    "help": False,
    "status": True,
    "stop": True,
}


class TestTable:
    def test_ships_exactly_the_committed_surface(self):
        assert tuple(c.name for c in COMMANDS) == EXPECTED_NAMES

    def test_every_row_is_documented_and_has_a_canonical_alias(self):
        for command in COMMANDS:
            assert command.summary.strip(), command.name
            assert command.aliases, command.name
            assert all(a == a.lower() for a in command.aliases), command.name

    def test_aliases_are_unique_across_the_table(self):
        seen: list[str] = []
        for command in COMMANDS:
            seen.extend(command.aliases)
        assert len(seen) == len(set(seen)), seen

    def test_rows_are_frozen(self):
        # The table is shared module state read on every inbound message, so a
        # row must not be mutable from a call site.
        with pytest.raises(FrozenInstanceError):
            COMMANDS[0].name = "other"  # type: ignore[misc]


class TestParseCommand:
    def test_every_alias_resolves_to_its_command(self):
        for command in COMMANDS:
            for alias in command.aliases:
                assert parse_command(alias) == command.name, alias

    def test_unknown_slash_token_is_not_a_command(self):
        # None keeps the message OUT of the transport's non-operator drop path,
        # so an unrecognized slash word still reaches the model.
        assert parse_command("/model") is None
        assert parse_command("/yolo on") is None
        assert parse_command("/") is None

    def test_matching_is_whole_message_not_a_prefix(self):
        assert parse_command("/new plan for the trip") is None
        assert parse_command("/stop the deploy please") is None
        assert parse_command("/helpful") is None
        # A slash mid-message is ordinary text on either side of the token.
        assert parse_command("run /new later") is None

    def test_case_and_surrounding_whitespace_are_tolerated(self):
        assert parse_command("  /COMPACT ") == "compact"
        assert parse_command("\n/New\t") == "new"
        assert parse_command("/Cancel") == "stop"

    def test_plain_text_and_empty_input(self):
        assert parse_command("hello") is None
        assert parse_command("") is None
        assert parse_command("   ") is None


class TestCommandArgument:
    def test_returns_the_text_after_the_token(self):
        assert command_argument("/status verbose") == "verbose"
        assert command_argument("  /new  a fresh start  ") == "a fresh start"

    def test_no_argument_is_the_empty_string(self):
        assert command_argument("/status") == ""
        assert command_argument("") == ""


class TestHelpText:
    def test_lists_every_command_in_the_table(self):
        rendered = help_text()
        for command in COMMANDS:
            assert command.aliases[0] in rendered, command.name
            assert command.summary in rendered, command.name

    def test_alternate_aliases_are_shown(self):
        rendered = help_text()
        assert "/cancel" in rendered
        assert "/start" in rendered

    def test_is_derived_from_the_table(self, monkeypatch):
        extra = WhatsAppCommand(
            name="ping",
            aliases=("/ping",),
            summary="Check the wiring",
            operator_only=True,
        )
        monkeypatch.setattr(commands, "COMMANDS", COMMANDS + (extra,))
        rendered = commands.help_text()
        assert "/ping - Check the wiring" in rendered
        # Same table drives routing and classification, so the added row is
        # reachable and classified without touching either function.
        assert commands.parse_command("/ping") == "ping"
        assert commands.is_operator_only("ping") is True

    def test_help_card_is_one_message(self):
        # WhatsApp's own per-message ceiling; the renderer would split a longer
        # card mid-list, which reads as a truncated menu.
        from kiro_crew.whatsapp.transport import WHATSAPP_CAPABILITIES

        assert len(help_text()) <= WHATSAPP_CAPABILITIES.max_message_chars


class TestOperatorOnly:
    def test_classification_matches_the_table(self):
        for command in COMMANDS:
            assert command.operator_only is EXPECTED_OPERATOR_ONLY[command.name], command.name
            assert is_operator_only(command.name) is command.operator_only

    def test_unknown_name_fails_closed(self):
        assert is_operator_only("restart") is True
        assert is_operator_only("") is True
