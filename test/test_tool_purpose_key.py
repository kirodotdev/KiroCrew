"""The reserved tool-purpose argument is read by SHAPE, not by an allowlist.

kiro-cli injects a ``__tool_use_purpose`` property into every tool schema it
exposes, so each tool call carries the agent's own one-line reason for the call
— which the dashboard paints as the concise tool pill label. Nothing validates
that key, though: it is a synthetic parameter the model fills in from prose, and
models paraphrase the name. Real transcripts carry ``__purpose``,
``__thinking_purpose`` and ``__woohoo_purpose``.

Matching a fixed pair of literals dropped every paraphrase, which showed up as
the pill silently falling back to the raw command line (and the stray key
leaking into the arguments view as if it were a real parameter). These tests
lock the shape match in, and lock OUT the two ways it could over-reach: a
tool's own non-reserved ``purpose`` argument, and a name that merely contains
the word.
"""

from __future__ import annotations

import pytest

from kiro_crew.acp._dispatch import extract_tool_purpose, is_tool_purpose_key, parse_session_update
from kiro_crew.acp.types import EVENT_TOOL_CALL_UPDATE, TOOL_PURPOSE_KEYS


@pytest.mark.parametrize("key", list(TOOL_PURPOSE_KEYS))
def test_canonical_spellings_match(key: str) -> None:
    """The declared property and its camelCased echo both read."""
    assert is_tool_purpose_key(key)
    assert extract_tool_purpose({key: "Read the failing job log"}) == "Read the failing job log"


@pytest.mark.parametrize(
    "key",
    [
        "__purpose",  # observed in real transcripts (190+ calls in one session)
        "__thinking_purpose",
        "__woohoo_purpose",
        "__toolPurpose",
        "__tool-use-purpose",
    ],
)
def test_paraphrased_spellings_match(key: str) -> None:
    """A model-paraphrased name still yields the purpose line."""
    assert is_tool_purpose_key(key)
    assert extract_tool_purpose({key: "Read the failing job log", "command": "gh run view"}) == (
        "Read the failing job log"
    )


@pytest.mark.parametrize(
    "key",
    [
        "purpose",  # not reserved: could be a tool's own functional argument
        "tool_use_purpose",
        "_purpose",  # single underscore is not the reserved dunder prefix
        "__purpose_of_the_call",  # does not END in "purpose"
        "__purposefully",
        "__command",
    ],
)
def test_non_purpose_keys_do_not_match(key: str) -> None:
    """Only reserved dunder names ending in "purpose" are claimed."""
    assert not is_tool_purpose_key(key)
    assert extract_tool_purpose({key: "not a purpose line"}) == ""


def test_a_tools_own_purpose_argument_is_left_alone() -> None:
    """A tool legitimately taking a ``purpose`` string is not misread as the
    reserved argument — the pill would otherwise show a functional parameter."""
    assert extract_tool_purpose({"purpose": "billing", "amount": 12}) == ""


def test_canonical_spelling_wins_over_a_paraphrase() -> None:
    """When both are present the declared property is authoritative."""
    args = {"__purpose": "paraphrased", "__tool_use_purpose": "canonical"}
    assert extract_tool_purpose(args) == "canonical"


def test_multiple_paraphrases_resolve_deterministically() -> None:
    """Sorted-key order, so the reading does not depend on wire order."""
    forward = {"__woohoo_purpose": "second", "__alpha_purpose": "first"}
    reverse = {"__alpha_purpose": "first", "__woohoo_purpose": "second"}
    assert extract_tool_purpose(forward) == "first"
    assert extract_tool_purpose(reverse) == "first"


@pytest.mark.parametrize("value", ["", "   ", None, 42, ["a"], {"a": 1}])
def test_blank_and_non_string_values_yield_nothing(value: object) -> None:
    """A blank or non-string value is no purpose at all — the pill must fall
    back to the raw label rather than render an empty prose label."""
    assert extract_tool_purpose({"__purpose": value}) == ""


def test_blank_canonical_falls_through_to_a_populated_paraphrase() -> None:
    """A present-but-blank canonical key must not shadow a real purpose."""
    args = {"__tool_use_purpose": "   ", "__purpose": "the real reason"}
    assert extract_tool_purpose(args) == "the real reason"


@pytest.mark.parametrize("raw", [None, "a string", 42, ["__purpose"], object()])
def test_non_dict_params_yield_nothing(raw: object) -> None:
    """Tool params arrive from the wire — a non-object payload is not a crash."""
    assert extract_tool_purpose(raw) == ""


def test_non_string_keys_are_tolerated() -> None:
    """A JSON payload cannot produce them, but an internal caller could."""
    assert not is_tool_purpose_key(42)
    assert extract_tool_purpose({42: "nope", "__purpose": "yes"}) == "yes"


# ── the purpose must survive the second-phase refinement ──


def _refinement_events(update: dict) -> list:
    """Dispatch a ``tool_call_update`` and return its refinement events."""
    events = parse_session_update(update)
    return [e for e in events if e.kind == EVENT_TOOL_CALL_UPDATE]


def test_refinement_carries_the_purpose_from_its_raw_input() -> None:
    """A refinement's ``rawInput`` is the COMPLETE params object, so it holds the
    reserved argument. Dropping it here loses the purpose entirely on any backend
    whose initial ``tool_call`` streams an empty ``rawInput``, and makes a
    consumer that falls back on an empty purpose paint the raw command instead."""
    (event,) = _refinement_events(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-1",
            "title": "ls /tmp",
            "kind": "execute",
            "rawInput": {"command": "ls /tmp", "__tool_use_purpose": "List the temp dir"},
        }
    )
    assert event.tool_purpose == "List the temp dir"


def test_refinement_reads_a_paraphrased_spelling_too() -> None:
    """Same shape match as the initial ``tool_call`` — one rule, both phases."""
    (event,) = _refinement_events(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-2",
            "rawInput": {"command": "gh pr view", "__woohoo_purpose": "Check the PR"},
        }
    )
    assert event.tool_purpose == "Check the PR"


def test_refinement_without_a_purpose_reports_empty() -> None:
    """Consumers read an empty purpose as "keep what the initial tool_call
    supplied", so a kind-only refinement must not invent one."""
    (event,) = _refinement_events(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-3",
            "kind": "execute",
        }
    )
    assert event.tool_purpose == ""


def test_refinement_purpose_is_redacted() -> None:
    """LLM-influenced text reaching the dashboard is scrubbed on every path.

    Asserts the value is POPULATED as well as scrubbed: an empty purpose would
    satisfy a bare "no credential in it" check, so the presence of the line has
    to be part of the failing condition."""
    (event,) = _refinement_events(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc-4",
            "rawInput": {
                "command": "aws s3 ls",
                "__tool_use_purpose": "Use AKIAIOSFODNN7EXAMPLE to list buckets",
            },
        }
    )
    assert event.tool_purpose.startswith("Use ")
    assert event.tool_purpose.endswith("to list buckets")
    assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_purpose
