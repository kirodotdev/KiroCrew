"""Discord command catalogue: parsing, the help card, and the slash payload.

Lives apart from ``test_discord.py`` because it covers ``discord/commands.py``
as a catalogue (one table feeding the ``!help`` card and the bulk-overwrite
array Discord accepts or rejects whole), not the dispatcher wiring.
"""

from __future__ import annotations

import pytest

from kiro_crew.discord import commands as dc
from kiro_crew.discord.client import _APP_COMMAND_DESC_LIMIT, _APP_COMMAND_NAME_RE
from kiro_crew.discord.commands import (
    COMMAND_SPEC,
    application_command_payload,
    build_help_text,
    parse_command,
)

_CHAT_INPUT = 1
_OPTION_STRING = 3
_CONTEXTS = [0, 1]
_MAX_CHOICES = 25


# ── parse_command: the added commands ──


class TestAddedCommands:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("!model", "model"),
            ("/model", "model"),
            ("!models", "model"),
            ("/models", "model"),
            ("!status", "status"),
            ("/status", "status"),
        ],
    )
    def test_both_prefixes_resolve(self, text: str, expected: str) -> None:
        assert parse_command(text) == expected

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("/model opus", "model"),
            ("!status please", "status"),
        ],
    )
    def test_argument_does_not_break_the_match(self, text: str, expected: str) -> None:
        assert parse_command(text) == expected

    def test_case_and_whitespace(self) -> None:
        assert parse_command("  /MODEL  ") == "model"
        assert parse_command("\t!Status  ") == "status"

    def test_near_misses_are_not_commands(self) -> None:
        # A prefixless word is chat text, and an unknown token stays chat text
        # rather than resolving to the nearest command.
        assert parse_command("status") is None
        assert parse_command("!statuses") is None
        assert parse_command("!modeler") is None


# ── parse_command: the pre-existing commands still resolve ──


class TestExistingCommandsUnchanged:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("!new", "new"),
            ("/new", "new"),
            ("!start", "new"),
            ("!compact", "compact"),
            ("/compact", "compact"),
            ("!link", "link"),
            ("/link", "link"),
            ("!unlink", "unlink"),
            ("/unlink", "unlink"),
            ("!sessions", "sessions"),
            ("/sessions", "sessions"),
            ("!session", "sessions"),
            ("!help", "help"),
            ("/help", "help"),
            ("!stop", "stop"),
            ("!cancel", "stop"),
            ("/cancel", "stop"),
        ],
    )
    def test_still_resolves(self, text: str, expected: str) -> None:
        assert parse_command(text) == expected

    def test_plain_text_is_not_a_command(self) -> None:
        assert parse_command("hello there") is None
        assert parse_command("") is None
        assert parse_command("!unknown") is None


# ── COMMAND_SPEC ──


class TestCommandSpec:
    def test_descriptions_fit_discord_ceiling(self) -> None:
        for name, desc in COMMAND_SPEC:
            assert desc, f"{name} has no description"
            assert len(desc) <= _APP_COMMAND_DESC_LIMIT, f"{name}: {len(desc)} chars"

    def test_names_match_discord_name_rule(self) -> None:
        for name, _desc in COMMAND_SPEC:
            assert _APP_COMMAND_NAME_RE.match(name), name

    def test_no_duplicate_names(self) -> None:
        names = [name for name, _ in COMMAND_SPEC]
        assert len(names) == len(set(names))

    def test_carries_the_catalogued_commands(self) -> None:
        assert {name for name, _ in COMMAND_SPEC} == {
            "new",
            "compact",
            "model",
            "status",
            "sessions",
            "link",
            "unlink",
            "stop",
            "help",
        }

    def test_every_row_is_reachable_as_text(self) -> None:
        # A menu entry the text parser does not recognize is a dead command.
        for name, _desc in COMMAND_SPEC:
            assert parse_command(f"!{name}") == name
            assert parse_command(f"/{name}") == name


# ── application_command_payload ──


def _rows_by_name() -> dict[str, dict]:
    return {row["name"]: row for row in application_command_payload()}


class TestApplicationCommandPayload:
    def test_one_row_per_spec_row_in_order(self) -> None:
        assert [row["name"] for row in application_command_payload()] == [
            name for name, _ in COMMAND_SPEC
        ]

    def test_every_row_is_a_chat_input_command_in_both_contexts(self) -> None:
        for row in application_command_payload():
            assert row.get("type") == _CHAT_INPUT, row["name"]
            assert row.get("contexts") == _CONTEXTS, row["name"]

    def test_deprecated_dm_permission_is_never_emitted(self) -> None:
        for row in application_command_payload():
            assert "dm_permission" not in row, row["name"]

    def test_every_row_is_locally_valid(self) -> None:
        # Discord rejects the whole array on one bad row, so validity is a
        # per-row property, not a property of the array as a whole.
        for row in application_command_payload():
            assert _APP_COMMAND_NAME_RE.match(row["name"])
            assert 1 <= len(row["description"]) <= _APP_COMMAND_DESC_LIMIT

    def test_options_only_where_an_argument_is_taken(self) -> None:
        rows = _rows_by_name()
        assert {name for name, row in rows.items() if row.get("options")} == {"sessions"}

    @pytest.mark.parametrize("name,option", [("sessions", "query")])
    def test_free_text_options(self, name: str, option: str) -> None:
        (opt,) = _rows_by_name()[name]["options"]
        assert opt["name"] == option
        assert opt["type"] == _OPTION_STRING
        assert opt["required"] is False
        assert "choices" not in opt

    def test_every_option_is_locally_valid(self) -> None:
        for row in application_command_payload():
            for opt in row.get("options", []):
                assert _APP_COMMAND_NAME_RE.match(opt["name"]), opt
                assert 1 <= len(opt["description"]) <= _APP_COMMAND_DESC_LIMIT, opt
                assert opt["type"] == _OPTION_STRING, opt
                choices = opt.get("choices", [])
                assert len(choices) <= _MAX_CHOICES, opt
                for choice in choices:
                    assert choice["name"] and choice["value"]

    def test_mid_turn_prefixes_are_absent(self) -> None:
        # A tap sends the bare token with no message body, so a queue/steer entry
        # would be a menu row that cannot do anything.
        names = {row["name"] for row in application_command_payload()}
        assert "queue" not in names
        assert "steer" not in names

    def test_malformed_spec_row_is_skipped_not_sent(self, monkeypatch) -> None:
        monkeypatch.setattr(
            dc,
            "COMMAND_SPEC",
            (("new", "Start a fresh conversation"), ("Bad Name", "x"), ("empty", "")),
        )
        assert [row["name"] for row in application_command_payload()] == ["new"]

    def test_overlong_description_is_truncated(self, monkeypatch) -> None:
        monkeypatch.setattr(dc, "COMMAND_SPEC", (("new", "x" * 140),))
        (row,) = application_command_payload()
        assert len(row["description"]) == _APP_COMMAND_DESC_LIMIT

    def test_payload_rows_are_copies(self) -> None:
        # The option table is module state shared by every registration, so a
        # caller editing one payload must not change what the next one sends.
        first = _rows_by_name()["sessions"]["options"][0]
        first["name"] = "clobbered"
        first["description"] = "clobbered"
        (opt,) = _rows_by_name()["sessions"]["options"]
        assert opt["name"] == "query"


# ── build_help_text ──


class TestBuildHelpText:
    def test_lists_every_catalogued_command(self) -> None:
        card = build_help_text()
        for name, desc in COMMAND_SPEC:
            assert f"`!{name}" in card, name
            assert desc in card, name

    def test_names_both_accepted_prefixes(self) -> None:
        card = build_help_text()
        assert "`!`" in card and "`/`" in card

    def test_shows_argument_placeholders_from_the_option_table(self) -> None:
        card = build_help_text()
        assert "`!sessions [query]`" in card

    def test_a_required_argument_renders_as_mandatory(self, monkeypatch) -> None:
        monkeypatch.setattr(dc, "COMMAND_SPEC", (("model", "Choose the model from a list"),))
        monkeypatch.setattr(
            dc,
            "_COMMAND_OPTIONS",
            {"model": ({"type": 3, "name": "id", "description": "d", "required": True},)},
        )
        assert "`!model <id>`" in build_help_text()

    def test_footer_documents_the_mid_turn_prefixes(self) -> None:
        card = build_help_text()
        assert "`!queue <msg>`" in card
        assert "`!steer <msg>`" in card

    def test_mid_turn_prefixes_are_not_listed_as_commands(self) -> None:
        # They appear only in the footer, with a message body, so a reader does
        # not learn a bare "!queue" is a command.
        card = build_help_text()
        assert "`!queue`" not in card
        assert "`!steer`" not in card

    def test_keeps_the_frame_around_the_rows(self) -> None:
        # The rows are only the middle of the card a user reads: a heading naming
        # the product and the surface, then the mid-turn footer behind a blank
        # line, then the closing line telling a reader plain text is enough. All
        # three are droppable without any row assertion noticing.
        card = build_help_text()
        head = card.split("\n")[0]
        assert head == dc._HELP_HEADER
        assert "Kiro Crew" in head and "Discord" in head
        assert "\n\nWhile a reply is running" in card
        closing = "Just send a message to chat. Replies stream in real-time."
        assert card.rstrip().endswith(closing)

    def test_tracks_the_spec_rather_than_a_frozen_string(self, monkeypatch) -> None:
        monkeypatch.setattr(dc, "COMMAND_SPEC", (("new", "Start a fresh conversation"),))
        card = build_help_text()
        assert "`!new` — Start a fresh conversation" in card
        assert "!compact" not in card
