"""What a brief makes the wake judge read.

Covers the session and pull-request evidence collectors and their cursors, the brief schema,
the stored opt-out marker, the structured path's refusal of a brief, the single spelling of
each brief bound, and the brief's own target list deciding which pull request is watched.
The shared autouse fixture comes from ``autonudge_wake_judge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

import pytest
from autonudge_wake_judge_helpers import _restore_judge_process_globals  # noqa: F401 - fixture
from autonudge_wake_judge_helpers import _loop

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import (
    AutoNudgeService,
    MonitorState,
    _bounded_judge_spec,
    infer_monitor,
    infer_subject,
    loop_subject,
)
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.irq import Outcome
from kiro_crew.probes import targets as probe_targets
from kiro_crew.validation import JUDGE_OFF_KEY, ValidationError, validate_judge_spec


class TestCollectors:
    """What the collectors read, what they refuse, and what they remember."""

    def test_assistant_rows_only(self) -> None:
        rows = [
            {"role": "assistant", "content": "RULING: need a call", "ts": 100.0},
            {"role": "tool", "content": "a file nobody asked to send", "ts": 101.0},
            {"role": "user", "content": "typed by the owner", "ts": 102.0},
        ]
        items = judge.session_evidence(rows, "chat-2-2", now_ts=110.0)
        assert [i["text"] for i in items] == ["RULING: need a call"]

    def test_a_pr_reading_renders_a_state_row_and_a_checks_row(self) -> None:
        """Two rows, because the char budget gives them up in a different order.

        A state row and a check tally are summaries of facts the watch's own record
        still holds; a remark body is prose nothing else has a copy of. Rendering the
        board into ONE row is what makes a real board affordable beside the bodies.
        """
        items = judge.pr_evidence(
            {
                "state": "open",
                "draft": False,
                "mergeability": "conflicting",
                "review_decision": "changes_requested",
                "blocking_review": "changes_requested",
                "unresolved_review_threads": 3,
                "checks": {
                    "failed": ["lint", "tests-2"],
                    "pending": ["e2e"],
                    "passed": ["a", "b", "c"],
                    "unknown": [],
                },
                "checks_complete": True,
                "review_threads_complete": True,
                "head_revision": "2c0adbc00da0e1878ce44a0eb47a6f02293506ba",
                "observed_at": 100.0,
            },
            "owner/name#1",
            now_ts=110.0,
        )
        assert [row["kind"] for row in items] == [point.KIND_PR_STATE, point.KIND_PR_CHECKS]
        assert {row["source"] for row in items} == {"pr:owner/name#1"}
        assert {row["age_s"] for row in items} == {10.0}, "the reading's own time ages the rows"
        state_text, checks_text = items[0]["text"], items[1]["text"]
        for fragment in (
            "state=open",
            "mergeability=conflicting",
            "review_decision=changes_requested",
            "unresolved_review_threads=3",
            "draft=no",
            "head=2c0adbc00da0",
        ):
            assert fragment in state_text
        assert "failed 2 (lint, tests-2)" in checks_text, "a red bucket is named, not counted"
        assert "pending 1 (e2e)" in checks_text
        assert "passed 3" in checks_text and "passed 3 (" not in checks_text
        assert "lint" not in state_text, "check names belong to the check row, not the state row"

    def test_an_incomplete_reading_says_so(self) -> None:
        """A criterion about red checks means something else on a partial list."""
        items = judge.pr_evidence(
            {
                "state": "open",
                "checks": {"failed": ["lint"]},
                "checks_complete": False,
                "checks_declared": 90,
                "checks_read": 41,
                "review_threads_complete": False,
                "observation_status": "partial",
                "incomplete": ["check runs read 41 of 90"],
            },
            "owner/name#1",
            now_ts=110.0,
        )
        texts = {row["kind"]: row["text"] for row in items}
        assert "review_threads_complete=no" in texts[point.KIND_PR_STATE]
        assert "reading=partial" in texts[point.KIND_PR_STATE]
        assert "not read: check runs read 41 of 90" in texts[point.KIND_PR_STATE]
        assert "board INCOMPLETE, read 41 of 90" in texts[point.KIND_PR_CHECKS]

    def test_the_comment_fingerprint_is_carried_and_no_body_is(self) -> None:
        """The structured reader produces a fingerprint rather than bodies."""
        items = judge.pr_evidence(
            {"state": "open", "pr_comment_body_digest": "a" * 64},
            "owner/name#1",
            now_ts=110.0,
        )
        text = items[0]["text"]
        assert "pr_comments_fingerprint=aaaaaaaaaaaa" in text
        assert len(text) < 200, "a fingerprint is 12 chars of hex, never a body"

    def test_a_long_red_bucket_reports_a_remainder_instead_of_every_name(self) -> None:
        """A board of forty reds names a handful and counts the rest."""
        items = judge.pr_evidence(
            {"state": "open", "checks": {"failed": [f"lane-{n}" for n in range(40)]}},
            "owner/name#1",
            now_ts=110.0,
        )
        text = [row["text"] for row in items if row["kind"] == point.KIND_PR_CHECKS][0]
        assert "failed 40 (" in text
        assert "+32 more" in text
        assert "lane-39" not in text
        assert len(text) <= point.MAX_ITEM_CHARS

    def test_an_observation_with_no_usable_key_yields_nothing(self) -> None:
        """Rather than an empty row: a blank item would read as a calm reading."""
        assert judge.pr_evidence({}, "owner/name#1", now_ts=110.0) == []
        assert judge.pr_evidence(None, "owner/name#1", now_ts=110.0) == []

    def test_whether_a_remark_is_new_to_the_watch_reaches_the_judge_as_words(self) -> None:
        """The default quiet criterion is "nothing new", so the judge must be able to tell.

        The core records the fact on the reading; it is useless there unless the
        rendered row says it, because a judge handed the same sentence every tick has
        no way to answer a criterion about novelty.
        """
        observation = {
            "state": "open",
            "remarks": [
                {
                    "kind": "comment",
                    "author": "ana",
                    "body": "please rename it",
                    "at": 100.0,
                    "first_seen_this_tick": True,
                },
                {
                    "kind": "comment",
                    "author": "bo",
                    "body": "looks good",
                    "at": 90.0,
                    "first_seen_this_tick": False,
                },
            ],
        }
        items = judge.pr_evidence(observation, "owner/name#1", now_ts=110.0)
        rendered = [row["text"] for row in items if row["kind"] == point.KIND_PR_COMMENT]
        assert any("new since the last tick" in text for text in rendered)
        assert any("already seen on an earlier tick" in text for text in rendered)

    def test_a_remark_carrying_no_novelty_flag_claims_neither(self) -> None:
        """An earlier build's reading has no flag, and inventing one asserts a fact."""
        items = judge.pr_evidence(
            {"state": "open", "remarks": [{"kind": "comment", "author": "ana", "at": 100.0}]},
            "owner/name#1",
            now_ts=110.0,
        )
        text = [row["text"] for row in items if row["kind"] == point.KIND_PR_COMMENT][0]
        assert "since the last tick" not in text
        assert "already seen" not in text

    def test_the_default_brief_names_a_failing_check(self) -> None:
        """An owner who arms no criteria expects a red board to reach them.

        Leaving it to the generic "a blocker" rests that on the judge reading one word
        the way the owner meant it, and a red check is the signal a plain watch is for.

        "failing" and not "newly failing": the state carries the current board and no
        prior one, so a brief asking for newness asks what the state cannot answer.
        """
        assert "a failing check" in judge.DEFAULT_WAKE_WHEN
        assert "newly failing" not in judge.DEFAULT_WAKE_WHEN
        assert "not whole" in judge.DEFAULT_WAKE_WHEN

    def test_the_default_brief_wakes_on_a_settled_green_board(self) -> None:
        """An owner who arms no criteria is most often waiting FOR the board going green.

        The wake decision itself is the judge's at runtime, so what a test can pin is
        the criterion the briefless loop runs under: the default names a settled,
        all-passed board as a wake reason rather than leaving it to "a blocker" or "a
        failing check", neither of which a green board answers. A briefless loop that
        misses the settle stays quiet about the very state its owner watches for, so
        the default names it and this test pins that.

        A companion reading proves a fully-passed board renders as one the clause can
        match -- the evidence a briefless loop judges against.
        """
        assert "a fully settled board whose every check has passed" in judge.DEFAULT_WAKE_WHEN

        board = judge.render_pr_checks(
            {"checks": {"passed": ["lint", "tests-2", "e2e"]}, "checks_complete": True}
        )
        assert "passed 3" in board and "passed 3 (" not in board
        assert "failed" not in board
        assert "INCOMPLETE" not in board

    def test_a_mistyped_key_is_skipped_rather_than_coerced(self) -> None:
        items = judge.pr_evidence(
            {"state": "open", "unresolved_review_threads": "three", "draft": "no"},
            "owner/name#1",
            now_ts=110.0,
        )
        text = items[0]["text"]
        assert "state=open" in text
        assert "unresolved_review_threads" not in text
        assert "draft" not in text

    def test_the_real_canonical_object_is_what_the_renderer_reads(self) -> None:
        """Built by the monitor's own projection, not by hand.

        This is the assertion the feature lacked: a hand-written observation can
        agree with the reader while the actual producer emits different keys, which
        is how a collector reading absent fields passed every test it had.
        """
        from kiro_crew.monitoring.pull_request import (
            PullRequestCheck,
            PullRequestFacts,
            canonical_pull_request_facts,
        )

        canonical = canonical_pull_request_facts(
            PullRequestFacts(
                kind="github_pull_request",
                target="owner/name#1",
                state="open",
                draft=False,
                head_revision="2c0adbc00da0e1878ce44a0eb47a6f02293506ba",
                mergeability="blocked",
                review_decision="changes_requested",
                checks=(
                    PullRequestCheck(identity="lint", state="failed"),
                    PullRequestCheck(identity="tests-2", state="passed"),
                ),
                unresolved_review_threads=2,
                review_threads_complete=True,
                pr_comment_body_digest="b" * 64,
            )
        )
        items = judge.pr_evidence(canonical, "owner/name#1", now_ts=110.0)
        assert items, "the canonical projection must yield evidence, not an empty list"
        texts = {row["kind"]: row["text"] for row in items}
        assert "state=open" in texts[point.KIND_PR_STATE]
        assert "review_decision=changes_requested" in texts[point.KIND_PR_STATE]
        assert "pr_comments_fingerprint=bbbbbbbbbbbb" in texts[point.KIND_PR_STATE]
        assert "failed 1 (lint)" in texts[point.KIND_PR_CHECKS]

    def test_a_pr_target_reaches_a_verdict_instead_of_falling_back(self) -> None:
        """Evidence with no producer made every pull-request tick FALLBACK, which fires.

        Driven through the same screen and state builder the tick uses, so this
        fails if the rendered row is dropped by the scrub or the kind check.
        """
        from kiro_crew.monitoring.pull_request import (
            PullRequestCheck,
            PullRequestFacts,
            canonical_pull_request_facts,
        )

        canonical = canonical_pull_request_facts(
            PullRequestFacts(
                kind="github_pull_request",
                target="owner/name#1",
                state="open",
                draft=False,
                head_revision="",
                mergeability="blocked",
                review_decision="review_required",
                checks=(PullRequestCheck(identity="lint", state="failed"),),
                unresolved_review_threads=0,
                review_threads_complete=True,
            )
        )
        evidence = judge.pr_evidence(canonical, "owner/name#1", now_ts=110.0)
        screened, dropped = point.screen_evidence(evidence)
        assert screened and dropped == 0, "the rendered row must survive the scrub"
        state = point.build_state("watch", wake_when="a check goes red", evidence=screened)
        assert state["since_last_tick"], "an empty state is what produced FALLBACK"

    def test_refused_target_is_dropped_and_its_cursor_held(self) -> None:
        async def refuse(target: str, since: int) -> tuple[list[dict], int, int | None]:
            raise PermissionError("not the creator")

        cursors = {"chat-2-2": 7}
        items, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=refuse, cursors=cursors)
        )
        assert items == [] and dropped == 1
        assert cursors == {"chat-2-2": 7}, "a refusal must not advance the cursor"

    def test_an_unusable_cursor_is_cleared_so_the_next_tick_reads_again(self) -> None:
        """A rewound transcript refuses the stored cursor, which must not be kept.

        Holding it makes every later tick re-send the same unusable value and be
        refused again, so the judge stops reading that target for as long as the
        loop is armed.
        """

        class Refusal(Exception):
            code = "cursor_unavailable"

        async def refuse(target: str, since: int) -> tuple[list[dict], int, int | None]:
            raise Refusal("cursor 7 is past the end of this transcript")

        cursors = {"chat-2-2": 7}
        items, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=refuse, cursors=cursors)
        )
        assert items == [] and dropped == 1
        assert cursors == {}, "an unusable cursor is cleared, which makes the next read a tail"

    def test_cursor_advances_on_a_successful_read(self) -> None:
        async def read(target: str, since: int) -> tuple[list[dict], int, int | None]:
            return [{"role": "assistant", "content": "progress", "ts": 1.0}], 12, 12

        cursors: dict[str, int] = {}
        asyncio.run(judge.collect_evidence(["chat-2-2"], read_session=read, cursors=cursors))
        assert cursors == {"chat-2-2": 12}

    @staticmethod
    def _transcript_reader(
        rows: list[dict], *, refuse_from: int | None = None
    ) -> Callable[[str, int], Awaitable[tuple[list[dict], int, int | None]]]:
        """A reader with ``read_messages``' paging: the FIRST page from the cursor."""

        async def read(target: str, since: int) -> tuple[list[dict], int, int | None]:
            if refuse_from is not None and since >= refuse_from:
                raise PermissionError("refused mid-catch-up")
            page = rows[since : since + judge.MAX_ROWS_PER_TARGET]
            return page, since + len(page), len(rows)

        return read

    @staticmethod
    def _busy_transcript(length: int) -> list[dict]:
        rows = [
            {"role": "tool" if n % 2 else "assistant", "content": f"step {n}", "ts": 1.0}
            for n in range(length - 1)
        ]
        rows.append({"role": "assistant", "content": "DONE: shipped", "ts": 2.0})
        return rows

    def test_a_busy_target_is_paged_to_its_end_so_the_last_line_is_read(self) -> None:
        """More new rows than one page must not leave the cursor behind.

        A worker that writes 13+ rows per interval must still have its final
        ``DONE:`` line screened on that tick, not when the quiet-streak floor
        fires hours later.
        """
        rows = self._busy_transcript(judge.MAX_ROWS_PER_TARGET * 2 + 5)
        cursors = {"chat-2-2": 0}
        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["chat-2-2"], read_session=self._transcript_reader(rows), cursors=cursors
            )
        )
        assert dropped == 0
        assert cursors == {"chat-2-2": len(rows)}, "the cursor reaches the transcript's end"
        assert any(item["text"] == "DONE: shipped" for item in items)

    def test_a_target_still_behind_after_the_page_budget_is_dropped(self) -> None:
        """A partial reading must fire, never be judged quiet on the rows it held."""
        budget = judge.MAX_ROWS_PER_TARGET * judge.MAX_PAGES_PER_TARGET
        rows = self._busy_transcript(budget + 5)
        cursors = {"chat-2-2": 0}
        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["chat-2-2"], read_session=self._transcript_reader(rows), cursors=cursors
            )
        )
        assert dropped == 1
        assert cursors == {"chat-2-2": budget}, "the cursor keeps every page it read"
        verdict = asyncio.run(point.judge_tick("watch", evidence=items, dropped=dropped))
        assert verdict.outcome is Outcome.FALLBACK

    def test_a_tail_read_with_no_cursor_drops_the_rows_it_skipped(self) -> None:
        """No stored cursor means the reader serves the tail and skips the rest.

        The gateway reads ``since or None``, so cursor 0 returns only the newest
        page and moves the cursor to the end. A ``BLOCKED:`` row before that page
        is never offered again, so the tick must fire rather than judge it calm.
        """
        rows = [{"role": "assistant", "content": "BLOCKED: need a token", "ts": 1.0}]
        rows += self._busy_transcript(judge.MAX_ROWS_PER_TARGET)

        async def tail(target: str, since: int) -> tuple[list[dict], int, int | None]:
            start = since if since else max(0, len(rows) - judge.MAX_ROWS_PER_TARGET)
            page = rows[start : start + judge.MAX_ROWS_PER_TARGET]
            return page, start + len(page), len(rows)

        cursors: dict[str, int] = {}
        items, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=tail, cursors=cursors)
        )
        assert cursors == {"chat-2-2": len(rows)}
        assert not any(item["text"].startswith("BLOCKED:") for item in items)
        assert dropped == 1
        verdict = asyncio.run(point.judge_tick("watch", evidence=items, dropped=dropped))
        assert verdict.outcome is Outcome.FALLBACK

    def test_a_tail_read_that_covers_the_whole_transcript_drops_nothing(self) -> None:
        rows = self._busy_transcript(judge.MAX_ROWS_PER_TARGET)

        async def tail(target: str, since: int) -> tuple[list[dict], int, int | None]:
            start = since if since else max(0, len(rows) - judge.MAX_ROWS_PER_TARGET)
            page = rows[start : start + judge.MAX_ROWS_PER_TARGET]
            return page, start + len(page), len(rows)

        _, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=tail, cursors={})
        )
        assert dropped == 0

    def test_a_page_that_refuses_keeps_the_pages_already_read(self) -> None:
        rows = self._busy_transcript(judge.MAX_ROWS_PER_TARGET * 3)
        cursors = {"chat-2-2": 0}
        reader = self._transcript_reader(rows, refuse_from=judge.MAX_ROWS_PER_TARGET)
        _, dropped = asyncio.run(
            judge.collect_evidence(["chat-2-2"], read_session=reader, cursors=cursors)
        )
        assert dropped == 1
        assert cursors == {"chat-2-2": judge.MAX_ROWS_PER_TARGET}

    def test_the_page_budget_stays_inside_the_points_item_walk(self) -> None:
        pages = judge.MAX_PAGES_PER_TARGET
        assert pages >= 2, "one page is the lag this budget exists to close"
        assert pages * judge.MAX_ROWS_PER_TARGET <= point.MAX_EVIDENCE_ITEMS

    def test_absent_reader_drops_rather_than_raising(self) -> None:
        items, dropped = asyncio.run(judge.collect_evidence(["chat-2-2"]))
        assert items == [] and dropped == 1

    def test_an_unread_pull_request_is_a_dropped_target(self) -> None:
        """``None`` from the PR reader is "not read", which must not read as "calm".

        The reader answers ``None`` for a loop carrying no monitor, which is every loop
        armed through the plain nudge endpoint, and for a brief naming a pull request
        the loop does not watch. Handing that to ``pr_evidence`` yields an empty list,
        so without the drop the tick holds no evidence and no drop -- the one shape the
        point reads as every target having been read and found quiet, which would
        suppress the turn on a subject nobody looked at.
        """

        async def unread(target: str) -> None:
            return None

        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["https://github.com/o/r/pull/7"],
                read_pr=unread,
            )
        )
        assert items == [] and dropped == 1

    def test_a_read_pull_request_is_not_a_drop(self) -> None:
        """The control: a real observation contributes evidence and drops nothing."""

        async def observed(target: str) -> dict:
            return {"state": "open", "mergeable": "MERGEABLE"}

        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["https://github.com/o/r/pull/7"],
                read_pr=observed,
            )
        )
        assert dropped == 0 and len(items) == 1

    def test_a_probe_covered_subject_contributes_nothing_and_drops_nothing(self) -> None:
        """A factless reading the reader passes through must not fire the tick.

        The reader hands one over for the subject the typed probe just read and found
        quiet, which is the only reason the judge is asked on that path. Counted as a
        drop it would fire a turn the probe had already settled, every interval, for
        the life of the watch -- the inverse of what the screen is for.
        """

        async def blank(target: str) -> dict:
            return {}

        items, dropped = asyncio.run(
            judge.collect_evidence(
                ["https://github.com/o/r/pull/7"],
                read_pr=blank,
            )
        )
        assert items == [] and dropped == 0

    def test_a_reading_is_unread_unless_it_is_whole(self) -> None:
        """The rule the reader applies, in all three directions.

        A drop means "nobody looked at all of it". Facts settle it whichever reader
        produced them; a reading whose own status is not ``ok`` is short, so a quiet
        drawn from it would be a quiet about the half that was read; and a factless
        observation is the shape a record holds before anything writes one.
        """
        assert judge.pr_target_is_unread({})
        assert judge.pr_target_is_unread({"observed_at": 1.0})
        assert judge.pr_target_is_unread(None)
        assert not judge.pr_target_is_unread({"state": "open"})
        assert not judge.pr_target_is_unread({"state": "open", "observation_status": "ok"})
        assert judge.pr_target_is_unread({"state": "open", "observation_status": "partial"})
        assert judge.pr_target_is_unread({"state": "open", "observation_status": "unavailable"})

    def test_one_canonical_fact_is_enough_to_count_as_read(self) -> None:
        """The fact test's own boundary, discounting the age the reader stamps on."""
        assert judge.pr_observation_has_facts({"state": "open"})
        assert judge.pr_observation_has_facts({"observed_at": 1.0, "state": "open"})
        assert not judge.pr_observation_has_facts({})
        assert not judge.pr_observation_has_facts({"observed_at": 1.0})
        assert not judge.pr_observation_has_facts(None)

    def test_targets_from_the_spec_are_deduped_and_screened(self) -> None:
        targets = judge.parse_targets(
            {"targets": ["chat-2-2", "chat-2-2", "not a target at all"]}, ""
        )
        assert targets == ["chat-2-2"]

    def test_session_target_shape(self) -> None:
        assert judge.is_session_target("chat-1751-1790052364")
        assert not judge.is_session_target("../etc/passwd")
        assert not judge.is_session_target("")


class TestSchema:
    """What ``monitor_start`` / ``monitor_update`` accept as a brief."""

    def test_accepts_and_normalises(self) -> None:
        out = validate_judge_spec(
            {"targets": ["chat-2-2", "chat-2-2"], "wake_when": "RULING", "quiet_when": "WORKING"}
        )
        assert out["targets"] == ["chat-2-2"]
        assert out["wake_when"] == "RULING"

    def test_absent_and_empty_are_both_legal(self) -> None:
        assert validate_judge_spec(None) == {}
        assert validate_judge_spec({}) == {}

    @pytest.mark.parametrize(
        "bad",
        [
            "a string",
            5,
            {"wake_whn": "a typo nobody would notice"},
            {"targets": "chat-2-2"},
            {"targets": [1]},
            {"targets": ["chat-x"] * 9},
            {"targets": ["c" * 500]},
            {"wake_when": "x" * 501},
            {"quiet_when": 5},
        ],
    )
    def test_refuses(self, bad: Any) -> None:
        with pytest.raises(ValidationError):
            validate_judge_spec(bad)

    def test_false_normalises_to_the_opt_out_marker(self) -> None:
        assert validate_judge_spec(False) == {JUDGE_OFF_KEY: True}

    def test_true_is_refused_rather_than_read_as_the_default(self) -> None:
        # The default already applies to every gated loop that names no brief, so
        # accepting `true` would give one meaning two spellings -- and an owner who
        # typed it more likely meant the opt-out.
        with pytest.raises(ValidationError):
            validate_judge_spec(True)

    def test_an_owner_cannot_spell_the_marker_by_hand(self) -> None:
        # The reserved key is deliberately absent from JUDGE_SPEC_KEYS: `judge: false`
        # is the only surface for the opt-out, and the dict form is an unknown key.
        with pytest.raises(ValidationError):
            validate_judge_spec({JUDGE_OFF_KEY: True})

    def test_an_empty_object_is_not_the_opt_out(self) -> None:
        # It drops the owner's own criteria; a gated loop then runs under the default.
        assert validate_judge_spec({}) == {}


class TestTheStoredOptOutSurvivesAReload:
    """The loader keeps the one key the arming validator refuses from a caller."""

    def test_the_marker_round_trips(self) -> None:
        assert _bounded_judge_spec({JUDGE_OFF_KEY: True}) == {JUDGE_OFF_KEY: True}

    def test_the_marker_is_returned_alone(self) -> None:
        # An opted-out loop has no criteria to carry, so pairing the marker with a
        # brief would describe a state no arming call can produce.
        stored = _bounded_judge_spec({JUDGE_OFF_KEY: True, "wake_when": "ignored"})
        assert stored == {JUDGE_OFF_KEY: True}

    def test_a_false_marker_is_not_an_opt_out(self) -> None:
        assert _bounded_judge_spec({JUDGE_OFF_KEY: False, "wake_when": "x"}) == {"wake_when": "x"}


class TestTheStructuredPathRefusesABrief:
    """A structured monitor is probe-first and holds no brief, so it must REFUSE one.

    The schema offers ``judge`` on every ``monitor_update``, so arming a structured
    monitor and then sending a brief is two ordinary steps. Dropping it and returning
    success tells an owner their criterion is armed while every tick keeps firing on
    the typed probe alone, which the acknowledgement gives them no way to discover.
    """

    @staticmethod
    def _refusal_set() -> set[str]:
        from kiro_crew.dashboard import session_directive_apply_monitor

        source = inspect.getsource(session_directive_apply_monitor)
        line = next(ln for ln in source.splitlines() if "legacy_only = sorted(" in ln)
        return {
            token.strip().strip('"')
            for token in line[line.index("{") + 1 : line.rindex("}")].split(",")
        }

    def test_judge_is_refused_not_dropped(self) -> None:
        assert "judge" in self._refusal_set()

    def test_the_precedent_field_is_still_there(self) -> None:
        # ``banner`` is the field this refusal was built for, and the same silent-no-op
        # argument covers both. Asserted so a future edit cannot trade one for the other.
        assert "banner" in self._refusal_set()


class TestEachBriefBoundHasOneSpelling:
    """The brief's shape bounds resolve to ``validation``, not to copied literals.

    The arming surface refuses an oversized brief and this loader trims one. Two
    numbers would disagree silently in the worst direction: the arming call accepts
    a brief the loader then clips, so an owner's criterion is honoured at 500 chars
    on the way in and something else on the way out.
    """

    def test_the_target_count_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert judge.MAX_TARGETS is _validation.MAX_JUDGE_TARGETS
        assert _autonudge._JUDGE_MAX_TARGETS is _validation.MAX_JUDGE_TARGETS

    def test_the_criterion_length_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert point.MAX_CRITERION_CHARS is _validation.MAX_JUDGE_CRITERION_CHARS
        assert _autonudge._JUDGE_MAX_CRITERION_CHARS is _validation.MAX_JUDGE_CRITERION_CHARS

    def test_the_target_length_is_spelled_once(self) -> None:
        from kiro_crew import autonudge as _autonudge
        from kiro_crew import validation as _validation

        assert _autonudge._JUDGE_MAX_TARGET_CHARS is _validation.MAX_JUDGE_TARGET_CHARS


class TestTheBriefsOwnTargetListDecidesTheWatchedSubject:
    """A loop naming its pull request in ``judge.targets`` and not in its message is watched.

    Observed on a live gateway: a gated loop whose brief named its pull request by
    full URL, while its own message named only the number, had NO monitor at all.
    Nothing was ever fetched, the tick recorded zero evidence items and fell back, and
    every interval fired on the plain timer -- so no pull-request comment reached the
    judge, which is what reading the bodies exists for.

    The two halves have to be about ONE pull request. ``parse_targets`` reads the
    brief's list first and returns nothing else once it is present, so a monitor
    resolved from the message publishes a reading the reader drops as "a pull request
    this loop does not watch". Both halves read the brief first, or the watch reads
    nothing.
    """

    URL = "https://github.com/kirodotdev/KiroCrew/pull/13936"
    MESSAGE = "Babysit PR 13936 until every check is green; stop when it merges."
    SUBJECT = "kirodotdev/KiroCrew#13936"

    def _spec(self) -> dict[str, Any]:
        return {
            "wake_when": "a reviewer asked for a change",
            "quiet_when": "checks are still running",
            "targets": [self.URL],
        }

    def _monitor(self) -> MonitorState:
        monitor = infer_monitor(self.MESSAGE, now=1_000.0, judge=self._spec())
        assert monitor is not None
        return monitor

    def _reading(self) -> dict[str, Any]:
        """The reading the gh probe publishes, in ``PrObservation.as_facts`` shape."""
        return {
            "kind": "gh-pr",
            "target": self.SUBJECT,
            "observation_status": "ok",
            "observed_at": 1_000.0,
            "state": "OPEN",
            "mergeability": "MERGEABLE",
            "review_decision": "",
            "head_revision": "a" * 40,
            "checks": {"passed": ["build"], "failed": [], "pending": []},
            "checks_complete": True,
            "remarks": [
                {
                    "kind": "comment",
                    "id": "IC_1",
                    "author": "buluoray",
                    "at": "2026-09-27T10:40:00+00:00",
                    "age_s": 60.0,
                    "verdict": "",
                    "body_digest": "d" * 12,
                    "clipped": False,
                    "first_seen_this_tick": True,
                }
            ],
            "remarks_total": 1,
        }

    def test_the_message_alone_names_no_subject(self) -> None:
        """The premise: a bare ``PR <number>`` is a shorthand, and inference refuses it."""
        assert infer_monitor(self.MESSAGE, now=1_000.0) is None

    def test_the_brief_names_it_so_the_loop_gets_a_monitor(self) -> None:
        monitor = self._monitor()
        assert (monitor.kind, monitor.target) == ("gh-pr", self.SUBJECT)

    def test_the_probe_config_the_tick_derives_names_that_same_subject(self) -> None:
        """A tick whose own derivation disagrees fires WITHOUT observing."""
        monitor = self._monitor()
        loop = _loop(self._spec())
        loop.message = self.MESSAGE
        loop.monitor = monitor
        target = loop_subject(loop)
        assert target is not None
        assert (target.kind, target.subject) == (monitor.kind, monitor.target)

    def test_the_target_the_collector_asks_about_is_accepted(self) -> None:
        monitor = self._monitor()
        asked = judge.parse_targets(self._spec(), self.MESSAGE)
        assert asked == [self.URL], "the brief's list is what the collector asks about"
        assert (
            judge.pr_observation_is_about(
                self.URL,
                monitor_kind=monitor.kind,
                monitor_target=monitor.target,
                observation=self._reading(),
            )
            is True
        )

    @pytest.mark.asyncio
    async def test_a_comment_on_that_pull_request_reaches_the_judge(self, tmp_path) -> None:
        """End to end over the stored loop: a fresh comment becomes a ``pr_comment`` item."""
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add(
            slot_key="chat-1-123",
            message=self.MESSAGE,
            idle_secs=300,
            judge=self._spec(),
            gate=True,
        )
        svc._cancel_timer(loop.id)
        assert loop.monitor is not None, "the arm path stores the brief's subject"
        assert loop.monitor.target == self.SUBJECT

        # What the tick does with a reading: the facts on the record, the bodies in the
        # process-local stash. Both, because a payload missing its bodies is counted
        # unread rather than judged as prose nobody wrote.
        reading = self._reading()
        loop.monitor.last_observation = reading
        judge.publish_pr_bodies(loop.id, {"IC_1": "please rebase before this lands"})

        bodies, stash_dropped = judge.take_pr_bodies(loop.id)
        payload = judge.payload_for_judge(reading, bodies, stash_dropped)
        assert payload is not None

        async def read_pr(target: str) -> dict[str, Any]:
            assert target == self.URL
            return payload

        items, dropped = await judge.collect_evidence(
            judge.parse_targets(judge.spec_of(loop), loop.message),
            read_pr=read_pr,
            now_ts=1_060.0,
        )
        assert dropped == 0, "the watched target is read, not dropped"
        comments = [item for item in items if item["kind"] == point.KIND_PR_COMMENT]
        assert len(comments) == 1
        assert "please rebase before this lands" in comments[0]["text"]
        assert "new since the last tick" in comments[0]["text"]

    @pytest.mark.asyncio
    async def test_a_replaced_brief_moves_the_watch_with_it(self, tmp_path) -> None:
        """The collector asks about the NEW list from the next tick, so the monitor follows."""
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add(
            slot_key="chat-1-123",
            message=self.MESSAGE,
            idle_secs=300,
            judge=self._spec(),
            gate=True,
        )
        svc._cancel_timer(loop.id)
        other = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        updated = await svc.update(loop.id, judge={"targets": [other]})
        svc._cancel_timer(loop.id)
        assert updated.monitor is not None
        assert updated.monitor.target == "kirodotdev/KiroCrew#14017"

    @pytest.mark.asyncio
    async def test_a_brief_retarget_advances_the_config_generation(self, tmp_path) -> None:
        """A changed SUBJECT is a changed configuration, even when the instruction stands.

        A structural-terminal verdict is captured against the generation at fire time.
        Without advancing it, a verdict recorded for the OLD subject still reads as
        current and deactivates the watch the retarget just rebound -- and the stale
        flag sits on the slot until the next genuine turn, so the window is a whole
        interval rather than one in-flight turn.
        """
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add(
            slot_key="chat-1-123",
            message=self.MESSAGE,
            idle_secs=300,
            judge=self._spec(),
            gate=True,
        )
        svc._cancel_timer(loop.id)
        before = loop.config_generation
        other = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        updated = await svc.update(loop.id, judge={"targets": [other]})
        svc._cancel_timer(loop.id)
        assert updated.monitor is not None and updated.monitor.target.endswith("#14017")
        assert updated.config_generation == before + 1

    @pytest.mark.asyncio
    async def test_an_unchanged_subject_spends_no_generation(self, tmp_path) -> None:
        """Only a real change spends one, so a settings save does not burn a generation."""
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add(
            slot_key="chat-1-123",
            message=self.MESSAGE,
            idle_secs=300,
            judge=self._spec(),
            gate=True,
        )
        svc._cancel_timer(loop.id)
        before = loop.config_generation
        updated = await svc.update(loop.id, judge=self._spec())
        svc._cancel_timer(loop.id)
        assert updated.config_generation == before

    def test_two_pull_requests_in_the_brief_leave_the_message_deciding(self) -> None:
        """A loop holds one monitor, so a list of two selects none -- and takes nothing away."""
        spec = {
            "targets": [self.URL, "https://github.com/kirodotdev/KiroCrew/pull/14017"],
        }
        assert infer_monitor(self.MESSAGE, now=1_000.0, judge=spec) is None
        message = f"Watch {self.URL} until it merges."
        monitor = infer_monitor(message, now=1_000.0, judge=spec)
        assert monitor is not None and monitor.target == self.SUBJECT

    def test_a_shorthand_in_the_brief_still_selects_nothing(self) -> None:
        """One grammar: the collector drops ``owner/name#N`` too, so it cannot be watched."""
        spec = {"targets": ["kirodotdev/KiroCrew#13936"]}
        assert judge.pr_targets_of(spec) == []
        assert infer_monitor(self.MESSAGE, now=1_000.0, judge=spec) is None

    def test_a_session_only_brief_leaves_the_message_deciding(self) -> None:
        """A conductor patrolling sessions keeps the subject its instruction names."""
        spec = {"targets": ["chat-2-2"]}
        message = f"Patrol chat-2-2 while {self.URL} is in flight."
        monitor = infer_monitor(message, now=1_000.0, judge=spec)
        assert monitor is not None and monitor.target == self.SUBJECT

    def test_a_brief_naming_a_different_pull_request_keeps_the_instructions(self) -> None:
        """The brief NARROWS a watch; one entry cannot overrule the instruction's own subject.

        The ordinary "blocked on" brief: the loop owns one pull request and the judge
        is pointed at the blocker to read evidence from. The watch stays on the loop's
        OWN pull request, which is what base did -- so it is still polled and the loop
        still retires when it merges. Watching the brief's entry instead would let the
        BLOCKER merging retire a loop whose own work was never observed.
        """
        blocker = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        spec = {"wake_when": "the blocker moves", "targets": [blocker]}
        message = f"Babysit {self.URL} to green."
        subject = infer_subject(message, spec)
        assert subject is not None, "the instruction's own pull request still decides"
        assert subject.subject == self.SUBJECT

    def test_a_work_ledger_watch_ignores_a_brief_that_names_a_pull_request(self) -> None:
        """A ``watch="work-ledger"`` subject is the conductor's own session, not a brief PR.

        The judge brief may still point a collector at a pull request, but that must not
        retarget the watch: without the early return the one-entry brief would be read as
        the subject and the loop would arm on -- and poll -- the PR instead of the
        conductor's ledger, silently, with no error to the caller. The watch's subject is
        resolved from ``slot_key`` and nothing else.
        """
        conductor = "chat-conductor-7"
        spec = {"wake_when": "a worker reports", "targets": [self.URL]}
        # The instruction even NAMES the pull request; the watch must still be the ledger.
        message = f"Patrol the fleet; worker is driving {self.URL}."
        subject = infer_subject(message, spec, watch=probe_targets.WORK_LEDGER, slot_key=conductor)
        assert subject is not None, "a work-ledger watch always resolves its own session"
        assert subject.kind == probe_targets.WORK_LEDGER
        assert subject.subject == conductor, "the subject is the conductor's slot, not the PR"

    def test_a_work_ledger_watch_with_no_brief_still_resolves_its_session(self) -> None:
        """The early return holds with no brief at all -- the common conductor shape."""
        conductor = "chat-conductor-9"
        subject = infer_subject(
            "Patrol the fleet.", None, watch=probe_targets.WORK_LEDGER, slot_key=conductor
        )
        assert subject is not None and subject.subject == conductor
        assert subject.kind == probe_targets.WORK_LEDGER

    def test_the_blocker_is_never_the_watched_subject(self) -> None:
        """Named separately because watching the blocker is the specific harm."""
        blocker = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        for message in (
            f"Drive {self.URL} to green.",
            "Babysit PR #13936 until every check is green.",
            "Drive kirodotdev/KiroCrew#13936 to green.",
        ):
            monitor = infer_monitor(message, now=1_000.0, judge={"targets": [blocker]})
            assert monitor is None or monitor.target != "kirodotdev/KiroCrew#14017", message

    def test_a_shorthand_instruction_refuses_the_briefs_entry(self) -> None:
        """The predicate must use ``infer``'s own wider notion of naming a pull request.

        ``PR #42`` and ``owner/name#42`` select nothing themselves, but they DO name a
        pull request, so the brief must not step in. A URL-only predicate read these as
        "names none" and handed the watch to the blocker.
        """
        blocker = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        for message in (
            "Babysit PR #13936 until every check is green.",
            "Drive kirodotdev/KiroCrew#13936 to green.",
        ):
            assert probe_targets.names_pull_request(message) is True, message
            assert infer_subject(message, {"targets": [blocker]}) is None, message

    def test_the_motivating_spelling_still_gates(self) -> None:
        """A bare ``PR <number>`` with no ``#`` names nothing, so the brief decides."""
        assert probe_targets.names_pull_request(self.MESSAGE) is False
        monitor = self._monitor()
        assert (monitor.kind, monitor.target) == ("gh-pr", self.SUBJECT)

    def test_a_source_location_is_not_a_pull_request(self) -> None:
        """A cited line number must not read as a second subject and strip the gate."""
        message = "Babysit PR 13936; the site is src/kiro_crew/autonudge.py#1751."
        assert probe_targets.names_pull_request(message) is False
        monitor = infer_monitor(message, now=1_000.0, judge=self._spec())
        assert monitor is not None and monitor.target == self.SUBJECT

    def test_a_brief_agreeing_with_the_instruction_still_gates(self) -> None:
        """One subject named twice is the ordinary phrasing and must keep its monitor."""
        message = f"Babysit {self.URL} until every check is green."
        monitor = infer_monitor(message, now=1_000.0, judge=self._spec())
        assert monitor is not None and monitor.target == self.SUBJECT

    def test_a_stored_loop_keeps_the_monitor_its_instruction_named(self) -> None:
        """A stored loop in the blocked-on shape must not lose the watch base gave it."""
        blocker = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        loop = _loop({"targets": [blocker]})
        loop.message = f"Babysit {self.URL} to green."
        loop.monitor = self._monitor()
        subject = loop_subject(loop)
        assert subject is not None and subject.subject == self.SUBJECT

    def test_the_brief_does_not_resolve_an_ambiguous_instruction(self) -> None:
        """An instruction naming TWO pull requests infers nothing, and stays unresolved.

        ``targets.infer`` refuses this text deliberately, so letting the brief pick one
        of the two named subjects would resolve that ambiguity by the side door -- and
        the one it picks is the blocker, which is the harm.
        """
        blocker = "https://github.com/kirodotdev/KiroCrew/pull/14017"
        message = f"Babysit {self.URL}; blocked on {blocker} landing first."
        assert probe_targets.infer(message) is None, "the premise: the message is ambiguous"
        assert probe_targets.names_pull_request(message) is True
        assert infer_subject(message, {"targets": [blocker]}) is None

    @pytest.mark.asyncio
    async def test_an_ungated_loop_gets_no_monitor_from_a_brief(self, tmp_path) -> None:
        """``gate=False`` is a cadence contract, and a brief must not gate it by the side door."""
        svc = AutoNudgeService(base_dir=tmp_path)
        loop = await svc.add(
            slot_key="chat-1-123",
            message=self.MESSAGE,
            idle_secs=300,
            judge=self._spec(),
            gate=False,
        )
        svc._cancel_timer(loop.id)
        assert loop.monitor is None
