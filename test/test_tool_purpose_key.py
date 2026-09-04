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

from kiro_crew.acp._dispatch import (
    extract_tool_purpose,
    is_tool_purpose_key,
    parse_session_update,
    select_tool_title,
)
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


class TestSelectToolTitle:
    """The pill label falls back sensibly when a backend omits the SDK title."""

    def test_description_is_preferred(self) -> None:
        raw = {"description": "List temp", "command": "ls /tmp"}
        assert select_tool_title("ls /tmp", raw) == "List temp"

    def test_description_outranks_the_command_for_a_shell_tool(self) -> None:
        """The prose label is the one thing that beats the command."""
        raw = {"description": "List temp", "command": "ls /tmp"}
        assert select_tool_title("Run Command", raw, "execute") == "List temp"

    def test_command_outranks_a_generic_shell_title(self) -> None:
        """A backend whose shell ``title`` is a kind label ("Run Command") names
        no command, and treating it as a usable title leaves every pill in a
        transcript identical. The command is the ground truth of the call."""
        assert select_tool_title("Run Command", {"command": "ls /tmp"}, "execute") == "ls /tmp"

    def test_command_matches_a_title_that_already_held_it(self) -> None:
        """Backends whose shell ``title`` IS the invocation are unaffected: both
        fields carry the same bytes, so preferring the command is a no-op."""
        assert select_tool_title("ls /tmp", {"command": "ls /tmp"}, "execute") == "ls /tmp"

    def test_unknown_sentinel_falls_through_to_command_for_shell(self) -> None:
        # A backend that omits the title leaves the flat field at the "unknown"
        # sentinel; a shell call still shows its command instead of a bare kind.
        assert select_tool_title("unknown", {"command": "git status"}, "execute") == "git status"

    def test_none_title_falls_through_to_command_for_shell(self) -> None:
        assert select_tool_title(None, {"command": "git status"}, "execute") == "git status"

    def test_command_not_used_for_non_shell_kind(self) -> None:
        # For an fs edit tool, rawInput.command is the operation name
        # ("strReplace"), not a shell command — it must not become the label.
        assert select_tool_title("unknown", {"command": "strReplace"}, "edit") is None

    def test_non_shell_title_is_kept_over_its_operation_name(self) -> None:
        """The command bypass is shell-only, so an fs tool keeps its own title."""
        assert select_tool_title("Write File", {"command": "create"}, "edit") == "Write File"

    def test_command_ignored_without_kind(self) -> None:
        # Without a kind we cannot confirm a shell tool, so no command fallback.
        assert select_tool_title("unknown", {"command": "ls"}) is None

    def test_blank_title_no_command_returns_none(self) -> None:
        assert select_tool_title("", {}, "execute") is None

    def test_resolved_shell_flag_recovers_the_command_without_a_kind(self) -> None:
        """A caller holding a resolved classification (the refinement path, whose
        frame may omit ``kind``) gets the command anyway."""
        got = select_tool_title("Run Command", {"command": "ls /tmp"}, None, is_shell=True)
        assert got == "ls /tmp"

    def test_resolved_non_shell_flag_overrides_a_shell_kind(self) -> None:
        """The explicit flag wins in both directions, so a caller that resolved
        "not shell" cannot be talked into reading an operation name."""
        got = select_tool_title("Write File", {"command": "create"}, "execute", is_shell=False)
        assert got == "Write File"


class TestGenericShellTitleAcrossBothEvents:
    """A backend may label a shell call ``Run Command`` on BOTH events.

    The refinement overwrites the pill, so the command rule has to hold there as
    well — and a refinement frame commonly omits ``kind``, which is exactly the
    input that used to fall through to the generic title.
    """

    @staticmethod
    def _sequence(refinement: dict) -> tuple[str, str]:
        """Dispatch a generic-titled shell ``tool_call`` then ``refinement``.

        Returns both titles. The caches are the caller-owned maps the real
        dispatch loop keeps for the life of a turn, which is what carries the
        initial call's classification and params into the refinement.
        """
        shell_cache: dict[str, bool] = {}
        raw_params_cache: dict[str, dict] = {}
        caches = {"shell_cache": shell_cache, "raw_params_cache": raw_params_cache}
        (call_event,) = parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-generic",
                "title": "Run Command",
                "kind": "execute",
                "rawInput": {"command": "git status"},
            },
            **caches,  # type: ignore[arg-type]
        )
        refine_events = [
            e
            for e in parse_session_update({**refinement}, **caches)  # type: ignore[arg-type]
            if e.kind == EVENT_TOOL_CALL_UPDATE
        ]
        assert len(refine_events) == 1
        return call_event.title, refine_events[0].title

    def test_initial_call_shows_the_command(self) -> None:
        call_title, _ = self._sequence(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-generic", "kind": "execute"}
        )
        assert call_title == "git status"

    def test_kindless_refinement_keeps_the_command(self) -> None:
        """The refinement repeats the generic title and omits ``kind``; without
        the cached classification it would repaint the pill ``Run Command``."""
        _, refine_title = self._sequence(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-generic",
                "title": "Run Command",
                "rawInput": {"command": "git status"},
            }
        )
        assert refine_title == "git status"

    def test_refinement_without_raw_input_reads_the_cached_params(self) -> None:
        """A title-only refinement carries no command of its own, so the label
        comes from the params the initial call cached."""
        _, refine_title = self._sequence(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-generic",
                "title": "Run Command",
            }
        )
        assert refine_title == "git status"
