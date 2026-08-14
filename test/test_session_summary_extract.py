"""Tests for session-summary transcript extraction and payload shaping.

The trap fixtures here are the load-bearing part. Each one is a shape that
produced a confidently wrong summary when the prompt was prototyped against real
transcripts, and each is now caught mechanically rather than left to the model's
judgement.
"""

from __future__ import annotations

import json

from kiro_crew.session_summary import (
    STATE_DONE,
    STATE_DROPPED,
    STATE_IN_PROGRESS,
    STATE_NEEDS_YOU,
    count_user_turns,
    derive_state,
    extract_turns,
    last_activity_ts,
    normalize_payload,
    render_input,
)


def _rec(role, content, ts=None, **extra):
    rec = {"role": role, "content": content}
    if ts:
        rec["ts"] = ts
    rec.update(extra)
    return rec


class TestExtractionReadsOnlyWhatItNeeds:
    def test_tool_and_error_rows_are_dropped(self):
        turns = extract_turns(
            [
                _rec("user", "do the thing"),
                _rec("tool", "ran a command"),
                _rec("error", "boom"),
                _rec("assistant", "done"),
            ]
        )
        assert [t.role for t in turns] == ["user", "assistant"]

    def test_meta_blob_is_ignored_not_excerpted(self):
        """``meta`` can be most of a transcript's bytes and never reaches the model."""
        big = "x" * 100_000
        turns = extract_turns([_rec("assistant", "short answer", meta={"payload": big})])
        assert turns[0].text == "short answer"
        assert big not in render_input(turns)

    def test_blank_and_malformed_rows_are_skipped(self):
        turns = extract_turns(["not a dict", {}, _rec("user", "   "), _rec("user", "real")])
        assert len(turns) == 1
        assert turns[0].text == "real"

    def test_user_text_is_kept_whole(self):
        text = "a" * 500
        turns = extract_turns([_rec("user", text)], assistant_excerpt_chars=10)
        assert turns[0].text == text
        assert turns[0].truncated is False

    def test_assistant_text_is_excerpted_from_both_ends(self):
        text = "HEAD" + ("m" * 5000) + "TAIL"
        turns = extract_turns([_rec("assistant", text)], assistant_excerpt_chars=50)
        assert turns[0].text.startswith("HEAD")
        assert turns[0].text.endswith("TAIL")
        assert turns[0].truncated is True
        assert len(turns[0].text) < len(text)

    def test_short_assistant_text_is_not_marked_truncated(self):
        turns = extract_turns([_rec("assistant", "brief")], assistant_excerpt_chars=400)
        assert turns[0].truncated is False

    def test_a_huge_user_paste_is_capped(self):
        turns = extract_turns([_rec("user", "z" * 20_000)], max_user_chars=100)
        assert len(turns[0].text) == 100
        assert turns[0].truncated is True


class TestTrapAutomationUnderUserRole:
    """Automation posts under role "user"; counting it invents a goal."""

    def test_injected_rows_are_flagged_and_not_counted(self):
        turns = extract_turns(
            [
                _rec("user", "real request"),
                _rec("user", "[Subagent completion event] Agent X completed"),
                _rec("user", "[Cron notification from \"nightly\"] ran"),
                _rec("user", "[Tool refusal -- automatic recovery] blocked"),
                _rec("user", "=== Restored Context (from prior session) ==="),
            ]
        )
        assert count_user_turns(turns) == 1
        assert [t.injected for t in turns] == [False, True, True, True, True]

    def test_injected_rows_do_not_consume_user_turn_numbers(self):
        turns = extract_turns(
            [
                _rec("user", "first"),
                _rec("user", "[auto-nudge cycle 3]"),
                _rec("user", "second"),
            ]
        )
        numbered = [t.user_turn for t in turns if not t.injected]
        assert numbered == [1, 2]

    def test_render_labels_automation_explicitly(self):
        turns = extract_turns([_rec("user", "[Subagent completion event] done")])
        assert "[automation, not the user]" in render_input(turns)

    def test_detection_is_case_insensitive_and_tolerates_leading_space(self):
        turns = extract_turns([_rec("user", "  [SUBAGENT COMPLETION EVENT] x")])
        assert turns[0].injected is True


class TestTrapResends:
    """A resent message is not insistence, and not evidence of being ignored."""

    def test_verbatim_repeat_is_marked(self):
        turns = extract_turns([_rec("user", "make a build"), _rec("user", "make a build")])
        assert turns[0].repeat_of is None
        assert turns[1].repeat_of == 1

    def test_whitespace_only_difference_still_counts_as_a_repeat(self):
        turns = extract_turns([_rec("user", "make a build"), _rec("user", "make  a\nbuild")])
        assert turns[1].repeat_of == 1

    def test_a_genuinely_different_message_is_not_marked(self):
        turns = extract_turns([_rec("user", "make a build"), _rec("user", "now ship it")])
        assert turns[1].repeat_of is None

    def test_render_labels_the_resend_so_it_is_not_read_as_a_new_request(self):
        turns = extract_turns([_rec("user", "same"), _rec("user", "same")])
        assert "resend of turn 1, not a new request" in render_input(turns)


class TestTimestampsAndCounts:
    def test_last_activity_uses_the_most_recent_row_with_a_timestamp(self):
        turns = extract_turns(
            [
                _rec("user", "a", ts="2026-08-01T10:00:00+00:00"),
                _rec("assistant", "b", ts="2026-08-02T10:00:00+00:00"),
                _rec("user", "c"),
            ]
        )
        assert last_activity_ts(turns) == "2026-08-02T10:00:00+00:00"

    def test_no_timestamps_yields_none(self):
        assert last_activity_ts(extract_turns([_rec("user", "a")])) is None

    def test_user_turn_numbers_appear_in_the_rendered_input(self):
        turns = extract_turns([_rec("user", "one"), _rec("assistant", "x"), _rec("user", "two")])
        rendered = render_input(turns)
        assert "USER (turn 1)" in rendered
        assert "USER (turn 2)" in rendered


class TestDerivedState:
    def test_completed_but_unverified_needs_you(self):
        """The prototype's sharpest case: discussion ended, goal never reached."""
        assert derive_state("completed", False) == STATE_NEEDS_YOU

    def test_completed_and_verified_is_done(self):
        assert derive_state("completed", True) == STATE_DONE

    def test_completed_with_verification_not_applicable_is_done(self):
        assert derive_state("completed", None) == STATE_DONE

    def test_active_is_in_progress_regardless_of_verification(self):
        assert derive_state("active", None) == STATE_IN_PROGRESS
        assert derive_state("active", False) == STATE_IN_PROGRESS

    def test_abandoned_is_dropped(self):
        assert derive_state("abandoned", False) == STATE_DROPPED


class TestNormalizePayload:
    def test_orders_intents_most_recently_touched_first(self):
        payload = normalize_payload(
            {
                "intents": [
                    {"title": "old", "ranges": [[1, 4]]},
                    {"title": "newest", "ranges": [[20, 25]]},
                    {"title": "middle", "ranges": [[10, 12]]},
                ]
            }
        )
        assert [i["title"] for i in payload["intents"]] == ["newest", "middle", "old"]

    def test_ranges_may_be_multiple_and_overlapping(self):
        """Dormancy plus resumption, and one intent inside another's span."""
        payload = normalize_payload(
            {"intents": [{"title": "resumed", "ranges": [[1, 14], [77, 100]]}]}
        )
        intent = payload["intents"][0]
        assert intent["ranges"] == [[1, 14], [77, 100]]
        assert intent["last_touched_turn"] == 100

    def test_a_bare_integer_range_is_accepted_as_a_single_turn(self):
        payload = normalize_payload({"intents": [{"title": "t", "ranges": [7]}]})
        assert payload["intents"][0]["ranges"] == [[7, 7]]

    def test_malformed_ranges_are_dropped_not_fatal(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[5, 2], ["a", "b"], [0, 3], [2, 4]]}]}
        )
        assert payload["intents"][0]["ranges"] == [[2, 4]]

    def test_intents_are_capped(self):
        raw = {"intents": [{"title": f"i{n}", "ranges": [[n, n]]} for n in range(1, 21)]}
        payload = normalize_payload(raw, max_intents=3)
        assert len(payload["intents"]) == 3
        # The cap keeps the most recently touched, not the first generated.
        assert [i["title"] for i in payload["intents"]] == ["i20", "i19", "i18"]

    def test_constraints_are_capped_and_cleaned(self):
        payload = normalize_payload(
            {
                "intents": [{"title": "t", "ranges": [[1, 1]]}],
                "constraints": ["  a  ", "", "b", "c", "d", "e", "f"],
            },
            max_constraints=3,
        )
        assert payload["constraints"] == ["a", "b", "c"]

    def test_zero_constraints_cap_suppresses_the_section(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[1, 1]]}], "constraints": ["a"]},
            max_constraints=0,
        )
        assert payload["constraints"] == []

    def test_next_steps_accept_strings_and_objects(self):
        payload = normalize_payload(
            {
                "intents": [
                    {
                        "title": "t",
                        "ranges": [[1, 1]],
                        "next_steps": [
                            "bare string",
                            {"what": "structured", "why": "because", "expect": "this"},
                            {"why": "no what -- dropped"},
                        ],
                    }
                ]
            }
        )
        steps = payload["intents"][0]["next_steps"]
        assert [s["what"] for s in steps] == ["bare string", "structured"]
        assert steps[1]["why"] == "because"

    def test_unknown_status_falls_back_to_active(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[1, 1]], "status": "nonsense"}]}
        )
        assert payload["intents"][0]["status"] == "active"
        assert payload["intents"][0]["state"] == STATE_IN_PROGRESS

    def test_state_is_stored_so_both_surfaces_agree(self):
        payload = normalize_payload(
            {
                "intents": [
                    {"title": "t", "ranges": [[1, 1]], "status": "completed", "verified": False}
                ]
            }
        )
        assert payload["intents"][0]["state"] == STATE_NEEDS_YOU

    def test_origin_turn_is_kept_when_plausible(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[23, 76]], "origin_turn": 20}]}
        )
        assert payload["intents"][0]["origin_turn"] == 20

    def test_implausible_origin_turn_is_dropped(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[1, 2]], "origin_turn": 0}]}
        )
        assert payload["intents"][0]["origin_turn"] is None

    def test_untitled_intents_are_dropped(self):
        payload = normalize_payload(
            {"intents": [{"ranges": [[1, 2]]}, {"title": "   "}, {"title": "keep"}]}
        )
        assert [i["title"] for i in payload["intents"]] == ["keep"]

    def test_nothing_usable_returns_none_so_the_cache_is_left_alone(self):
        assert normalize_payload({"intents": []}) is None
        assert normalize_payload({"intents": "nope"}) is None
        assert normalize_payload("not a dict") is None
        assert normalize_payload(None) is None


class TestPayloadRedaction:
    """The payload is LLM output derived from the transcript, so it can echo a
    secret the user pasted. Redaction happens before the sidecar write, because
    the sidecar is read straight back to the panel."""

    def test_a_credential_in_a_title_is_redacted(self):
        payload = normalize_payload(
            {"intents": [{"title": "rotate ghp_abcdefghijklmnopqrstuvwxyz0123456789"}]}
        )
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in payload["intents"][0]["title"]

    def test_redaction_reaches_nested_fields(self):
        secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
        payload = normalize_payload(
            {
                "intents": [
                    {
                        "title": "t",
                        "initial_intent": f"use {secret}",
                        "progress": [f"exported {secret}"],
                        "next_steps": [
                            {"what": f"revoke {secret}", "why": f"{secret} leaked", "expect": "ok"}
                        ],
                    }
                ],
                "constraints": [f"never log {secret}"],
            }
        )
        assert secret not in json.dumps(payload)

    def test_ordinary_text_is_left_alone(self):
        payload = normalize_payload(
            {
                "intents": [{"title": "set up auth", "progress": ["ran the test suite"]}],
                "constraints": ["restart the worker after a config change"],
            }
        )
        assert payload["intents"][0]["title"] == "set up auth"
        assert payload["intents"][0]["progress"] == ["ran the test suite"]
        assert payload["constraints"] == ["restart the worker after a config change"]

    def test_non_string_fields_survive_untouched(self):
        payload = normalize_payload(
            {"intents": [{"title": "t", "ranges": [[1, 4]], "verified": True, "origin_turn": 2}]}
        )
        intent = payload["intents"][0]
        assert intent["ranges"] == [[1, 4]]
        assert intent["verified"] is True
        assert intent["origin_turn"] == 2
        assert intent["last_touched_turn"] == 4
