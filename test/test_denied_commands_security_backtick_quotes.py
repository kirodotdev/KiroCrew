"""Tests that a live backtick inside a message value is read as a substitution.

Which backtick bash runs in a ``git commit -m`` / ``gh ... --body`` value is decided
by bash's own quoting: an escaped ``\\``` and every backtick inside single quotes is
literal, while a bare backtick inside double quotes is a live command substitution. A
reader that paired an escaped backtick with a live one (making it the closer), or that
treated a single-quoted backtick as an opener, misread which one bash runs. An
interpreter argument (``ruby -e``, ``bash -c``) is re-evaluated one level down, so its
single-quoted content is NOT inert and is read quote-blind. Every row here is checked
through ``is_denied`` only: no row is ever executed.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401

from kiro_crew.security import is_denied

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


@pytest.mark.parametrize(
    "cmd",
    [
        # An escaped backtick next to a live one, inside double quotes.
        'git commit -m "\\`a\\` `kirocrew restart`"',
        'gh pr create --title t --body "\\`a\\` `kirocrew restart`"',
        'git commit -m "`kirocrew restart` \\`a\\`"',
        # Inside double quotes ``\\\\`` is one literal backslash, so the ``\\`` + LF
        # after it IS a continuation that folds away, leaving a clean ``restart``
        # subcommand that bash runs.
        'gh pr create --title t --body "\\\\\n`kirocrew restart\\\\\n`"',
        'git commit -m "\\\\\n`kirocrew restart\\\\\n`"',
        'gh pr create --title t --body "\\`x\\` \\\\\n`kirocrew restart\\\\\n`"',
    ],
    ids=[
        "commit-escaped-then-live",
        "gh-escaped-then-live",
        "commit-live-then-escaped",
        "gh-literal-backslash-lf",
        "commit-literal-backslash-lf",
        "gh-escaped-pair-then-backslash-lf",
    ],
)
def test_a_live_backtick_in_a_message_value_is_denied(cmd):
    assert is_denied(cmd) is not None, repr(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # A ``ruby -e`` argument is not a stored message value: ruby's ``%Q{}`` turns
        # the escaped backtick back into a LIVE one and hands ``kirocrew restart`` to
        # ``bash -c``. Reading it quote-blind (never skipping an escaped opener here)
        # keeps the restart denied.
        'ruby -e \'system("bash","-c", %Q{echo \\`kirocrew restart \\`}); %q{\\`}\'',
    ],
    ids=["ruby-e-reconstructs-live-backtick"],
)
def test_a_live_backtick_in_an_interpreter_payload_is_denied(cmd):
    assert is_denied(cmd) is not None, repr(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        # These are executable payloads, not stored message values. ``bash -c`` and
        # ``sh -c`` re-lex their argument and ``echo``'s backtick substitution runs
        # its body, so each shell folds the ``\<newline>`` continuation and rejoins
        # ``kiro\<newline>crew restart`` into the ``kirocrew restart`` it runs. The
        # over-approximating continuation fold keeps these fail-closed.
        'bash -c "kiro\\\\\ncrew restart"',
        'sh -c "kiro\\\\\ncrew restart"',
        'echo "`kiro\\\\\ncrew restart`"',
    ],
    ids=[
        "bash-c-split-name",
        "sh-c-split-name",
        "echo-subst-split-name",
    ],
)
def test_a_split_self_restart_in_an_executable_payload_is_denied(cmd):
    assert is_denied(cmd) is not None, repr(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        'gh pr create --title t --body "run \\`kirocrew restart\\` now"',
        'git commit -m "docs: say \\`kirocrew restart\\` and \\`kirocrew status\\`"',
        'gh pr create --title t --body "a \\\\\\`x\\\\\\` b"',
        # Every backtick inside single quotes is literal, so bash runs nothing.
        "git commit -m 'docs: `a` then `kirocrew restart`'",
        "git commit -m 'docs: \\`a\\` then `kirocrew restart`'",
        # bash does NOT fold ``\\`` + CR + LF: it reads the ``\\`` as escaping the CR
        # into a literal CR and the LF ends the command (see ``_continuation_width``),
        # so the operand keeps a trailing CR (``restart\r``) and is not the ``restart``
        # subcommand -- bash runs no self-restart.
        'gh pr create --title t --body "\\\\\r\n`kirocrew restart\\\\\r\n`"',
    ],
    ids=[
        "gh-escaped-pair",
        "commit-two-escaped-pairs",
        "gh-backslash-then-escaped",
        "commit-single-quoted-live",
        "commit-single-quoted-escaped-and-live",
        "gh-literal-backslash-crlf-not-folded",
    ],
)
def test_escaped_or_single_quoted_backticks_in_a_message_value_stay_allowed(cmd):
    assert is_denied(cmd) is None, repr(cmd)
