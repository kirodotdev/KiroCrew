"""Goal markers are stripped at the one shared projection boundary.

``strip_control_comments`` is what every plain-text projection already calls
(previews, speech, channel renderers, egress and both mirror backfills), so it
owns the dashboard-only ``[GOAL: ...]`` line as well as the trailing control
tags. ``strip_trailing_control_tags`` keeps the tail-only grammar for callers
that measure a prefix offset or ask whether a tag ended the reply.
"""

from __future__ import annotations

import pytest

from kiro_crew.constants import (
    split_trailing_protocol_suffix,
    strip_control_comments,
    strip_trailing_control_tags,
)
from kiro_crew.dashboard.chat_slack import _format_backfill_parts
from kiro_crew.slack.handler_runtime.stream import _resolve_comment_hold

_BOTH_MODES = pytest.mark.parametrize("hide_partial", [False, True])


@_BOTH_MODES
def test_complete_goal_lines_are_stripped_in_either_mode(hide_partial: bool) -> None:
    # The line's content goes and its terminator stays, as the preview scanner does.
    text = "Ready.\n[GOAL: Deliver the result]\nDetails follow."
    assert strip_control_comments(text, hide_partial=hide_partial) == "Ready.\n\nDetails follow."


@_BOTH_MODES
def test_marker_free_text_is_returned_unchanged(hide_partial: bool) -> None:
    text = "Plain reply with [GOAL: inline prose] and a trailing space "
    assert strip_control_comments(text, hide_partial=hide_partial) is text


@_BOTH_MODES
def test_an_unfinished_goal_line_is_kept_in_either_mode(hide_partial: bool) -> None:
    # Only a COMPLETE line is protocol; a channel never streams a goal marker.
    text = "Ready.\n[GOAL: Deliver the res"
    assert strip_control_comments(text, hide_partial=hide_partial) == text


@_BOTH_MODES
def test_a_goal_line_after_a_tag_does_not_shield_the_tag(hide_partial: bool) -> None:
    text = "Ready.\n<!-- keep-visible -->\n[GOAL: Deliver it]"
    assert strip_control_comments(text, hide_partial=hide_partial) == "Ready."


@_BOTH_MODES
def test_the_tail_grammar_alone_leaves_goal_lines(hide_partial: bool) -> None:
    text = "Ready.\n[GOAL: Deliver it]\n<!-- keep-visible -->"
    assert strip_trailing_control_tags(text, hide_partial=hide_partial) == (
        "Ready.\n[GOAL: Deliver it]"
    )


def test_protocol_suffix_split_stays_a_partition_around_a_goal_line() -> None:
    text = "Ready.\n[GOAL: Deliver it]\nMore.\n[OPTIONS: Ship | Hold]\n<!-- keep-visible -->"
    head, tail = split_trailing_protocol_suffix(text)
    assert head + tail == text
    assert tail.startswith("[OPTIONS:")


def test_slack_comment_hold_reads_a_goal_line_as_content_not_a_tag() -> None:
    # An unfinished tag prefix is the assistant's own prose; a goal line earlier
    # in the reply must not make the hold look like a completed tag.
    accumulated = "Ready.\n[GOAL: Deliver it]\n<!-- keep-vis"
    assert _resolve_comment_hold("<!-- keep-vis", accumulated) == ("", "<!-- keep-vis")


def test_slack_backfill_does_not_post_the_goal_line() -> None:
    parts = _format_backfill_parts("Ready.\n[GOAL: Deliver the result]", "*")
    body = "\n".join(parts)
    assert "[GOAL" not in body
    assert "Ready." in body
