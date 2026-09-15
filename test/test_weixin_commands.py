"""Contract tests for Weixin's command GRAMMAR (``weixin/commands.py``).

The module's whole point is that the matcher and the help card read ONE table, so
a command cannot exist without being discoverable. The per-channel half of that
promise — every alias in a channel's spec really is reachable, and really does
appear in its help — is pinned in each channel's own suite.
"""

from __future__ import annotations

from kiro_crew.weixin.commands import CommandSpec, build_help_text, match_command

SPECS = (
    CommandSpec("new", "Start fresh", aliases=("新对话", "清空")),
    CommandSpec("stop", "Stop the reply", aliases=("/cancel",)),
)


class TestMatchCommand:
    def test_the_canonical_spelling_matches(self) -> None:
        assert match_command("/new", SPECS) == "new"

    def test_an_alias_matches(self) -> None:
        assert match_command("新对话", SPECS) == "new"
        assert match_command("/cancel", SPECS) == "stop"

    def test_matching_is_case_insensitive_for_the_ascii_form(self) -> None:
        # A phone keyboard capitalises the first letter unprompted.
        assert match_command("/New", SPECS) == "new"
        assert match_command("/CANCEL", SPECS) == "stop"

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert match_command("  /new \n", SPECS) == "new"

    def test_a_message_that_merely_STARTS_with_a_command_is_not_one(self) -> None:
        # The whole message must be the command, or "/new plan for the migration"
        # would silently wipe the session instead of being answered.
        assert match_command("/new plan for the migration", SPECS) is None
        assert match_command("新对话的想法", SPECS) is None

    def test_unknown_and_empty_text_match_nothing(self) -> None:
        assert match_command("hello", SPECS) is None
        assert match_command("", SPECS) is None
        assert match_command("   ", SPECS) is None

    def test_only_the_slash_prefix_matches(self) -> None:
        # The prefix is hardcoded: both adopters use "/". Discord's "!" forms are
        # its own module's business until it migrates onto this one.
        assert match_command("!new", SPECS) is None


class TestBuildHelpText:
    def test_every_visible_command_appears_with_its_aliases(self) -> None:
        out = build_help_text("HEADER", SPECS, "FOOTER")
        assert out.startswith("HEADER")
        assert out.endswith("FOOTER")
        assert "/new (新对话 / 清空) — Start fresh" in out
        assert "/stop (/cancel) — Stop the reply" in out

    def test_the_footer_is_optional(self) -> None:
        assert build_help_text("H", SPECS).rstrip().endswith("Stop the reply")

    def test_the_rendered_names_carry_the_slash(self) -> None:
        assert "/new" in build_help_text("H", SPECS)
