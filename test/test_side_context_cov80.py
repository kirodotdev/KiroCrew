"""Coverage for the side-chat history block in :mod:`kiro_crew.dashboard.side_context`.

``test_side_context.py`` covers the PARENT snapshot. What is untested there is
the sibling block: the prior *side* turns replayed on a cold-start first turn,
its trailing-user exclusion, its char cap, and the follow-up path that sends the
bare question instead of the whole envelope.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.dashboard import side_context as sc
from kiro_crew.dashboard.side_state import SideState


def _slot(*, messages=None, side_messages=None, side=True):
    """Minimal slot stub: build_side_message only reads .messages and ._side."""
    side_state = None
    if side:
        side_state = SideState(open=True)
        side_state.messages.extend(side_messages or [])
    return SimpleNamespace(
        messages=list(messages or []),
        _side=side_state,
        key="parent",
        agent=None,
    )


def _msg(role, content):
    return {"role": role, "content": content}


def test_follow_up_turn_sends_the_bare_stripped_question():
    """Turn 2+ relies on the kiro-cli session for framing, so no envelope."""
    slot = _slot(
        messages=[_msg("user", "parent turn")],
        side_messages=[_msg("user", "earlier side question")],
    )
    out = sc.build_side_message(slot, "  and the region?  ", is_first_turn=False)
    assert out == "and the region?"
    assert sc._PARENT_SNAPSHOT_HEADER not in out
    assert sc._SIDE_HISTORY_HEADER not in out


def test_first_turn_replays_prior_side_turns_in_order():
    slot = _slot(
        messages=[_msg("user", "parent turn")],
        side_messages=[
            _msg("user", "which region is beta in?"),
            _msg("assistant", "beta runs in eu-west-1"),
        ],
    )
    out = sc.build_side_message(slot, "and gamma?", is_first_turn=True)
    assert sc._SIDE_HISTORY_HEADER in out
    assert sc._SIDE_HISTORY_FOOTER in out
    assert "User: which region is beta in?" in out
    assert "Assistant: beta runs in eu-west-1" in out
    assert out.index("which region is beta in?") < out.index("beta runs in eu-west-1")
    # The current question is appended separately, after the history block.
    assert out.index(sc._SIDE_HISTORY_FOOTER) < out.index("User: and gamma?")


def test_trailing_user_turn_is_excluded_from_the_history_block():
    """The in-flight question is appended by build_side_message, not replayed."""
    slot = _slot(
        side_messages=[
            _msg("user", "first side question"),
            _msg("assistant", "first side answer"),
            _msg("user", "duplicate of the live question"),
        ],
    )
    out = sc.build_side_message(
        slot, "duplicate of the live question", is_first_turn=True
    )
    assert out.count("duplicate of the live question") == 1
    assert "User: first side question" in out


def test_no_side_state_omits_the_history_block():
    slot = _slot(messages=[_msg("user", "parent turn")], side=False)
    out = sc.build_side_message(slot, "cold start", is_first_turn=True)
    assert sc._SIDE_HISTORY_HEADER not in out
    assert out.endswith("User: cold start")


def test_non_dialogue_side_rows_are_skipped_and_yield_no_block():
    """A side buffer holding only tool/permission rows produces no block."""
    slot = _slot(
        side_messages=[
            _msg("tool", "ran a search"),
            _msg("permission", "approve?"),
        ],
    )
    out = sc.build_side_message(slot, "anything there?", is_first_turn=True)
    assert sc._SIDE_HISTORY_HEADER not in out


def test_non_dialogue_rows_interleaved_do_not_break_the_transcript():
    slot = _slot(
        side_messages=[
            _msg("user", "question one"),
            _msg("tool", "ran a search"),
            _msg("assistant", "answer one"),
        ],
    )
    out = sc.build_side_message(slot, "next", is_first_turn=True)
    assert "ran a search" not in out
    assert out.index("User: question one") < out.index("Assistant: answer one")


def test_long_side_history_retains_the_most_recent_turns():
    """Over the cap, keep the tail: a follow-up concerns the recent turns."""
    side_messages = []
    for i in range(200):
        side_messages.append(_msg("user", f"stale side question {i} " + "x" * 400))
        side_messages.append(_msg("assistant", f"stale side answer {i} " + "y" * 400))
    side_messages.append(_msg("assistant", "the sentinel value is zqx-4417"))
    slot = _slot(side_messages=side_messages)

    out = sc.build_side_message(slot, "repeat the sentinel", is_first_turn=True)

    assert "zqx-4417" in out
    assert "stale side question 0" not in out
    # Cap is enforced on the block, not silently on the whole envelope.
    assert len(out) < 2 * sc._MAX_SIDE_HISTORY_CHARS


def test_side_turn_content_is_truncated_per_line():
    slot = _slot(side_messages=[_msg("assistant", "z" * 900)])
    out = sc.build_side_message(slot, "go on", is_first_turn=True)
    assert "z" * sc._PARENT_LINE_TRUNCATE in out
    assert "z" * (sc._PARENT_LINE_TRUNCATE + 1) not in out


def test_none_content_on_a_side_turn_renders_as_empty():
    """A row whose content is None must not crash the transcript render."""
    slot = _slot(side_messages=[{"role": "assistant", "content": None}])
    out = sc.build_side_message(slot, "still there?", is_first_turn=True)
    assert "Assistant: " in out
