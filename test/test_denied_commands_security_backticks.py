"""Tests that a command nested in escaped backticks is judged as bash runs it.

Bash removes one ``\\`` before ``\\``, ``` ` `` and ``$`` each time it reads a
backtick body, so a substitution nested N levels deep is written with more
backslashes at every level. The deny gate must see the command bash runs at
every depth, and must not refuse a harmless command for being nested.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew import security
from kiro_crew.security import is_denied

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

DEPTHS = range(1, 7)


def _escape_for_backticks(text: str) -> str:
    """*text* written so one backtick level of bash reads it back unchanged."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("$", "\\$")


def _nest(command: str, depth: int) -> str:
    """*command* run from inside *depth* nested backtick substitutions."""
    line = command
    for level in range(depth):
        body = _escape_for_backticks(line) if level else line
        line = f"echo x; `{body}`"
    return line


def _nest_quoted(command: str, depth: int) -> str:
    """*command* run from *depth* nested backticks, each inside double quotes.

    Inside double quotes bash also removes the ``\\`` before ``"`` in a
    backtick body, so each level escapes ``"`` as well.
    """
    line = command
    for level in range(depth):
        body = _escape_for_backticks(line).replace('"', '\\"') if level else line
        line = f'echo x "`{body}`"'
    return line


class TestBacktickBodyUnescape:
    def test_body_loses_one_backslash_before_the_three_special_characters(self):
        (body,) = security._substitution_bodies("echo `a \\` b \\\\ c \\$d e \\f`")
        assert body == "a ` b \\ c $d e \\f", body

    def test_unproven_remainder_is_unescaped_too(self):
        (body,) = security._substitution_bodies("echo `a \\$b \\\\c")
        assert body == "a $b \\c", body

    def test_an_escaped_opener_keeps_its_body_as_written(self):
        (body,) = security._substitution_bodies("echo \\`a \\$b\\`")
        assert body == "a \\$b\\`", body

    def test_a_single_quoted_opener_keeps_its_body_as_written(self):
        (body,) = security._substitution_bodies("echo 'x `a \\$b`'")
        assert body == "a \\$b", body

    def test_each_level_spends_one_pass(self):
        line = _nest("printf hi", 3)
        first = security._substitution_bodies(line)[0]
        assert first == _nest("printf hi", 2), first
        second = security._substitution_bodies(first)[0]
        assert second == _nest("printf hi", 1), second


class TestNestedBackticksMeetTheFloor:
    @pytest.mark.parametrize("depth", DEPTHS)
    def test_restart_is_refused_at_every_depth(self, depth):
        line = _nest("kirocrew restart", depth)
        reason = is_denied(line)
        assert reason, line
        assert reason.startswith(f"{security.DENY_REASON_PREFIX}self-protection-restart"), reason

    @pytest.mark.parametrize("depth", DEPTHS)
    def test_a_backslash_spent_by_the_backtick_level_is_refused(self, depth):
        # ``re\\start`` reaches the inner shell as ``re\start``, which runs ``restart``.
        line = _nest("kirocrew re\\\\start", depth)
        assert is_denied(line), line

    @pytest.mark.parametrize("depth", DEPTHS)
    @pytest.mark.parametrize(
        "command", ["kirocrew restart", 'kirocrew re""start', 'kirocrew "restart"']
    )
    def test_restart_in_double_quoted_backticks_is_refused_at_every_depth(self, command, depth):
        line = _nest_quoted(command, depth)
        assert is_denied(line), line

    @pytest.mark.parametrize("depth", DEPTHS)
    def test_token_is_refused_at_every_depth(self, depth):
        line = _nest("kirocrew token", depth)
        assert is_denied(line), line

    @pytest.mark.parametrize(
        "line",
        [
            'git commit -m "docs: \\`kirocrew restart\\` now waits for the drain"',
            "git commit -m 'docs: escape \\`kirocrew restart\\` in the guide'",
            'echo "\\`" foo kirocrew re\\\\start',
            "gh pr comment 1 --body 'The gate now reads `kirocrew re\\\\start` as bash does'",
            "git commit -m 'docs: `kirocrew doctor \\$HOME` prints it'",
            "echo $'x `kirocrew re\\\\start`'",
        ],
    )
    def test_an_escaped_backtick_opens_nothing_new(self, line):
        # An escaped or single-quoted backtick is a literal to bash: its body is
        # read as written.
        assert is_denied(line) is None, line

    @pytest.mark.parametrize(
        "line",
        ["echo 'a' `kirocrew re\\\\start`", 'echo "`kirocrew re\\\\start`"'],
    )
    def test_a_real_opener_after_a_quote_is_unescaped(self, line):
        assert is_denied(line), line

    def test_the_same_spelling_outside_backticks_stays_allowed(self):
        # With no backtick level bash keeps one backslash: the verb is ``re\start``.
        assert is_denied("kirocrew re\\\\start") is None

    @pytest.mark.parametrize("depth", DEPTHS)
    @pytest.mark.parametrize("command", ["kirocrew status", "echo restart", "git status"])
    def test_harmless_commands_stay_allowed(self, command, depth):
        line = _nest(command, depth)
        assert is_denied(line) is None, line
        quoted = _nest_quoted(command, depth)
        assert is_denied(quoted) is None, quoted
