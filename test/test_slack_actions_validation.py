"""Tests for Slack text / Block Kit business validation.

Asserts the text limit is the SHIPPED send-path split point (SLACK_MSG_LIMIT,
3900), never the platform ~40000 ceiling, and that structural block caps (50
blocks, section/header text, over-cap element counts, malformed payloads) are
hard errors. Failure paths dominate.
"""

from __future__ import annotations

import pytest

from kiro_crew.connections.vendors.slack.validation import (
    MAX_BLOCKS_PER_MESSAGE,
    MAX_BLOCKS_PER_VIEW,
    MAX_SECTION_TEXT_CHARS,
    TEXT_SPLIT_LIMIT,
    validate_blocks,
    validate_message,
    validate_text,
)
from kiro_crew.slack.format import SLACK_MSG_LIMIT


def test_text_limit_is_the_shipped_split_point_not_the_platform_ceiling():
    # The whole point: the validator's ceiling is the renderer's split point.
    assert TEXT_SPLIT_LIMIT == SLACK_MSG_LIMIT == 3900
    # And it is NOT the ~40000 platform max.
    assert TEXT_SPLIT_LIMIT < 40000


def test_text_over_split_limit_needs_split_but_is_not_an_error():
    r = validate_text("x" * (TEXT_SPLIT_LIMIT + 1))
    assert r.needs_split is True
    assert r.ok is True  # long text is normal, handled by a split, not rejected


def test_text_at_limit_does_not_need_split():
    r = validate_text("x" * TEXT_SPLIT_LIMIT)
    assert r.needs_split is False


def test_message_with_neither_text_nor_blocks_is_no_text_error():
    r = validate_message(text="", blocks=None)
    assert r.ok is False
    assert any("no_text" in e for e in r.errors)


def test_blocks_must_be_a_list():
    r = validate_blocks({"type": "section"})  # a dict, not a list
    assert r.ok is False
    assert any("must be a list" in e for e in r.errors)


def test_empty_blocks_list_is_an_error():
    r = validate_blocks([])
    assert r.ok is False
    assert any("at least one block" in e for e in r.errors)


def test_over_50_blocks_is_an_error_for_a_message():
    blocks = [{"type": "divider"} for _ in range(MAX_BLOCKS_PER_MESSAGE + 1)]
    r = validate_blocks(blocks, surface="message")
    assert r.ok is False
    assert any("too many blocks" in e for e in r.errors)


def test_exactly_50_blocks_is_allowed():
    blocks = [{"type": "divider"} for _ in range(MAX_BLOCKS_PER_MESSAGE)]
    r = validate_blocks(blocks, surface="message")
    assert r.ok is True


def test_view_surface_allows_up_to_100_blocks():
    blocks = [{"type": "divider"} for _ in range(MAX_BLOCKS_PER_VIEW)]
    assert validate_blocks(blocks, surface="view").ok is True
    over = [{"type": "divider"} for _ in range(MAX_BLOCKS_PER_VIEW + 1)]
    assert validate_blocks(over, surface="view").ok is False


def test_block_without_type_is_an_error():
    r = validate_blocks([{"text": {"type": "mrkdwn", "text": "hi"}}])
    assert r.ok is False
    assert any("missing a string 'type'" in e for e in r.errors)


def test_non_object_block_is_an_error():
    r = validate_blocks([{"type": "divider"}, "not-a-block"])
    assert r.ok is False
    assert any("must be an object" in e for e in r.errors)


def test_section_text_over_cap_is_an_error():
    big = {
        "type": "section",
        "text": {"type": "mrkdwn", "text": "x" * (MAX_SECTION_TEXT_CHARS + 1)},
    }
    r = validate_blocks([big])
    assert r.ok is False
    assert any("section text" in e for e in r.errors)


def test_header_text_over_cap_is_an_error():
    big = {"type": "header", "text": {"type": "plain_text", "text": "x" * 200}}
    r = validate_blocks([big])
    assert r.ok is False
    assert any("header text" in e for e in r.errors)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "section", "text": "not-an-object"},
        {"type": "section", "text": {"type": "mrkdwn", "text": 7}},
        {"type": "header", "text": "not-an-object"},
        {"type": "header", "text": {"type": "plain_text", "text": 7}},
    ],
)
def test_present_malformed_required_text_is_an_error(block):
    r = validate_blocks([block])
    assert r.ok is False
    assert any("block[0]" in e and "required field 'text'" in e for e in r.errors)


def test_header_without_text_is_an_error():
    r = validate_blocks([{"type": "header"}])
    assert r.ok is False
    assert any("block[0]" in e and "required field 'text'" in e for e in r.errors)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "section"},
        {"type": "section", "fields": []},
        {"type": "section", "fields": "not-a-list"},
    ],
)
def test_section_requires_valid_text_or_non_empty_fields(block):
    r = validate_blocks([block])
    assert r.ok is False
    assert any("block[0]" in e and "section" in e for e in r.errors)


def test_section_without_text_accepts_valid_fields():
    block = {"type": "section", "fields": [{"type": "mrkdwn", "text": "value"}]}
    assert validate_blocks([block]).ok is True


@pytest.mark.parametrize(
    "block",
    [
        {"type": "context"},
        {"type": "context", "elements": "not-a-list"},
        {"type": "actions"},
        {"type": "actions", "elements": {}},
    ],
)
def test_context_and_actions_require_an_elements_list(block):
    r = validate_blocks([block])
    assert r.ok is False
    assert any("block[0]" in e and "elements" in e for e in r.errors)


def test_actions_over_element_cap_is_an_error():
    actions = {"type": "actions", "elements": [{"type": "button"} for _ in range(26)]}
    r = validate_blocks([actions])
    assert r.ok is False
    assert any("actions has" in e for e in r.errors)


def test_context_over_element_cap_is_an_error():
    ctx = {"type": "context", "elements": [{"type": "mrkdwn"} for _ in range(11)]}
    r = validate_blocks([ctx])
    assert r.ok is False
    assert any("context has" in e for e in r.errors)


def test_unknown_block_type_is_not_rejected():
    # Slack adds block types over time; unknown types must not be blocked.
    r = validate_blocks([{"type": "some_future_block", "foo": "bar"}])
    assert r.ok is True


def test_message_with_blocks_may_have_empty_text():
    r = validate_message(text="", blocks=[{"type": "divider"}])
    assert r.ok is True


def test_message_merges_split_signal_and_block_errors():
    r = validate_message(
        text="x" * (TEXT_SPLIT_LIMIT + 1),
        blocks=[{"type": "divider"} for _ in range(MAX_BLOCKS_PER_MESSAGE + 1)],
    )
    assert r.needs_split is True  # from text
    assert r.ok is False  # from blocks
    assert any("too many blocks" in e for e in r.errors)
