"""Tests that a live backtick inside a message value is read as a substitution.

Inside double quotes ``\\``` is a literal backtick, but a backtick with an even run of
backslashes before it is live. A reader that ignored those rules paired an escaped
backtick with a live one, or folded ``\\\\`` + newline into ``\\```, so the live
substitution after it was never read. Every row here is checked through ``is_denied``
only: no row is ever executed.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew.security import is_denied

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


@pytest.mark.parametrize(
    "cmd",
    [
        # An escaped backtick next to a live one.
        'git commit -m "\\`a\\` `kirocrew restart`"',
        'gh pr create --title t --body "\\`a\\` `kirocrew restart`"',
        'git commit -m "`kirocrew restart` \\`a\\`"',
        # Inside double quotes ``\\\\`` is one literal backslash, so the newline after
        # it is no continuation and the backtick on the next line is live.
        'gh pr create --title t --body "\\\\\n`kirocrew restart\\\\\n`"',
        'gh pr create --title t --body "\\\\\r\n`kirocrew restart\\\\\r\n`"',
        'git commit -m "\\\\\n`kirocrew restart\\\\\n`"',
        'gh pr create --title t --body "\\`x\\` \\\\\n`kirocrew restart\\\\\n`"',
    ],
    ids=[
        "commit-escaped-then-live",
        "gh-escaped-then-live",
        "commit-live-then-escaped",
        "gh-literal-backslash-lf",
        "gh-literal-backslash-crlf",
        "commit-literal-backslash-lf",
        "gh-escaped-pair-then-backslash-lf",
    ],
)
def test_a_live_backtick_in_a_message_value_is_denied(cmd):
    assert is_denied(cmd) is not None, repr(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        'gh pr create --title t --body "run \\`kirocrew restart\\` now"',
        'git commit -m "docs: say \\`kirocrew restart\\` and \\`kirocrew status\\`"',
        'gh pr create --title t --body "a \\\\\\`x\\\\\\` b"',
    ],
    ids=["gh-escaped-pair", "commit-two-escaped-pairs", "gh-backslash-then-escaped"],
)
def test_escaped_backticks_in_a_message_value_stay_allowed(cmd):
    assert is_denied(cmd) is None, repr(cmd)
