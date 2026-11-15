"""A ``session_send`` hand-back cleans its ``decision`` like the record route does.

``action=decide`` through the record route runs ``validate_tool_args``, which
calls ``sanitize_string`` on every string field: hidden and bidi characters are
dropped and Unicode is normalised. The hand-back stamp only stripped and
redacted, so a zero-width or right-to-left override in a conductor's message
reached the stored ``decision`` -- the one field a worker's brief presents as an
instruction. ``_handback_decision`` now runs the same ``sanitize_string``.
"""

from __future__ import annotations

from kiro_crew.dashboard.handlers.work_ledger_handback import _handback_decision


def test_a_handback_decision_drops_hidden_and_bidi_characters():
    # U+202E RIGHT-TO-LEFT OVERRIDE and U+200B ZERO WIDTH SPACE must not survive.
    decision = _handback_decision("ok\u202e\u200bgo")
    assert decision is not None
    assert "\u202e" not in decision
    assert "\u200b" not in decision
    assert decision == "okgo"


def test_a_plain_handback_decision_is_unchanged():
    # Ordinary text is kept as is (edge whitespace trimmed, as .strip() did).
    assert _handback_decision("  look at the second file first  ") == (
        "look at the second file first"
    )


def test_an_all_hidden_handback_decision_is_none():
    # Nothing left after cleaning -> no decision, the same as an empty message.
    assert _handback_decision("\u200b\u202e") is None
