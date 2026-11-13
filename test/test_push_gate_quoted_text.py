"""The push floor reads a quoted command substitution the way bash does.

A ``"$(...)"`` in an EARLIER command (an assignment value) was refused as an
unverifiable push target although the push itself names a plain feature branch,
because the floor walked the substitution body with the double quote still open
and returned the whole remainder with an unclosed quote.

The relaxed reading is taken only for a FLAT body that is NOT in program
position: a ``"$(echo git)"`` that stands where the command word goes still
reads as the whole remainder, so a quoted expansion that runs ``git push`` to a
protected branch stays refused.
"""

from __future__ import annotations

import pytest

from kiro_crew.security import is_denied

ALLOWED = [
    # a quoted substitution in an assignment value before the push (recorded blocks)
    'B="$(pwd)"; git push -q -u origin feat/x',
    'B="$(git rev-parse --absolute-git-dir)/body.md" && git push -q -u origin feat/x',
    'X="$(echo ")")"; git push origin feat/x',
]

DENIED = [
    # the substitution expands to the push target
    'B="$(pwd)"; git push origin main',
    'X="$(git push origin main)"',
    'git push origin "$(echo main)"',
    'git push origin feat/x "$(echo main)"',
    # a quoted substitution body that other floors must still read as a command
    'B="$(pkill -f kirocrew)"; git push origin feat/x',
    'X="$(kirocrew token)"; git push origin feat/x',
    # a nested quoted substitution keeps the whole-remainder reading
    'echo "$(echo "$(echo ")")"; P=pkill; $P -f kirocrew)"',
    # a heredoc body ')' is not the substitution closer
    'X="$(cat <<EOF\n)\nEOF\nkirocrew restart)"',
    "echo \"$(cat <<'EOF'\n)\nEOF\nP=pkill; eval $P -f kirocrew)\"",
    # a quoted command substitution in PROGRAM position expands to git -> publish
    '"$(echo git)" push origin main',
    '"$(which git)" push --force origin main',
    '"$(echo git)" -c color.ui=false push origin main',
]


@pytest.mark.parametrize("command", ALLOWED)
def test_quoted_substitution_value_before_a_feature_push_is_allowed(command: str) -> None:
    assert is_denied(command) is None


@pytest.mark.parametrize("command", DENIED)
def test_protected_or_program_position_substitution_stays_denied(command: str) -> None:
    assert is_denied(command) is not None
