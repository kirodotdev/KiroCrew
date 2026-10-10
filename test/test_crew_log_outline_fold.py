"""The ``outline`` fold: one row per turn, the first user line and the settled reply.

Three groups of claims, and they are separated because they fail for different reasons.

``TestTheRowPopulation`` is about WHICH rows exist -- one per turn, including the
refused turns a boundary-anchored fold would drop, and never a row out of order.

``TestWhatARowCarries`` is about the two free-text columns: the first-user-line rule,
the settled-reply rule and its retry arm, and the clamps.

``TestWhatNeverReachesARow`` is the negative contract, and it is the group that must
not be able to pass vacuously: it drives the fold with the exact entries that carry
injected context and tool output and asserts the rows are unmoved, then proves the
same probe CAN move a row when it is a real message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import Entry

GATEWAY = "gateway"
ACP = "acp"


class _Log:
    """A crew log's entries in writer order, with the seq the writer would assign.

    The seqs matter: the fold refuses a seq at or below the one it last folded, so a
    test that reuses one is testing that guard by accident instead of the rule it meant
    to. Handing out seqs from one counter makes every test here write in log order.
    """

    def __init__(self) -> None:
        self.state = crew_log._outline_start()
        self._seq = 0

    def add(self, entry_type: str, data: dict[str, Any], *, src: str = GATEWAY) -> int:
        self._seq += 1
        crew_log._outline_step(
            self.state,
            Entry(seq=self._seq, time=1_000 + self._seq, type=entry_type, src=src, data=data),
        )
        return self._seq

    # -- the writer's own order, named once ---------------------------------- #
    #
    # `chat_runner` writes `message/received` BEFORE the dispatch gates and
    # `turn/started` after them, so these helpers are deliberately in that order and
    # not in the order a reader would guess. A test that wrote the boundary first
    # would be testing a log this product never produces.

    def prompt(self, turn: int, text: str, *, role: str = "user") -> int:
        return self.add(
            "message/received", {"turn": turn, "role": role, "source": "dashboard", "text": text}
        )

    def started(self, turn: int, *, actor: str = "user", attempt: int | None = None) -> int:
        data: dict[str, Any] = {"turn": turn, "actor": actor, "depth": 0}
        if attempt is not None:
            data["attempt"] = attempt
        return self.add("turn/started", data)

    def reply(self, turn: int, step: int, text: str, *, interrupted: bool = False) -> int:
        data: dict[str, Any] = {"turn": turn, "step": step, "text": text}
        if interrupted:
            data["interrupted"] = True
        return self.add("message/sent", data, src=ACP)

    def completed(self, turn: int, *, stop_reason: str = "end_turn") -> int:
        return self.add("turn/completed", {"turn": turn, "stop_reason": stop_reason})

    def refused(self, turn: int, reason: str, *, actor: str = "user") -> int:
        return self.add(
            "turn/refused", {"turn": turn, "actor": actor, "reason": reason, "depth": 0}
        )

    # -- reading ------------------------------------------------------------- #

    @property
    def rows(self) -> list[dict[str, Any]]:
        value: list[dict[str, Any]] = crew_log._outline_render(self.state)["turns"]
        return value

    def row(self, turn: int) -> dict[str, Any]:
        for row in self.rows:
            if row["turn"] == turn:
                return row
        raise AssertionError(f"no row for turn {turn}; rows are {[r['turn'] for r in self.rows]}")


def _settled_turn(log: _Log, turn: int, prompt: str, reply: str) -> None:
    """One ordinary turn, start to finish, in the writer's order."""
    log.prompt(turn, prompt)
    log.started(turn)
    log.reply(turn, 1, reply)
    log.completed(turn)


# --------------------------------------------------------------------------- #
# which rows exist
# --------------------------------------------------------------------------- #


class TestTheRowPopulation:
    def test_an_empty_fold_renders_its_whole_shape(self) -> None:
        """Every key present with a value, because the fold catalogue reads this.

        ``scripts/fold_catalogue.py`` types a fold's fields from its EMPTY render, so a
        key missing here is a field no dashboard author can learn exists.
        """
        value = crew_log._outline_render(crew_log._outline_start())
        assert value == {
            "turns": [],
            "dropped": 0,
            "limit": crew_log.OUTLINE_TURN_LIMIT,
            "prompt_chars": crew_log.OUTLINE_PROMPT_CHARS,
            "reply_chars": crew_log.OUTLINE_REPLY_CHARS,
            "first_turn": None,
            "last_turn": None,
        }

    def test_one_row_per_turn_however_many_entries_the_turn_wrote(self) -> None:
        """THE row rule. Three turns of differing length produce exactly three rows."""
        log = _Log()
        _settled_turn(log, 1, "first question", "first answer")
        # A turn with several model calls and a steer inside it is still ONE row.
        log.prompt(2, "second question")
        log.started(2)
        log.reply(2, 1, "partial")
        log.prompt(2, "actually, do this instead")
        log.reply(2, 2, "second answer")
        log.completed(2)
        _settled_turn(log, 3, "third question", "third answer")
        assert [row["turn"] for row in log.rows] == [1, 2, 3]
        assert [row["prompt"] for row in log.rows] == [
            "first question",
            "second question",
            "third question",
        ]
        assert [row["reply"] for row in log.rows] == [
            "first answer",
            "second answer",
            "third answer",
        ]

    def test_a_refused_turn_still_gets_its_own_row(self) -> None:
        """The anchoring rule, and the reason it is not the turn boundary.

        A refused turn carries ``message/received`` and ``turn/refused`` and NEVER a
        ``turn/started`` -- the gate returns above it. A fold anchored on the boundary
        would render no row here at all.
        """
        log = _Log()
        _settled_turn(log, 1, "first question", "first answer")
        log.prompt(2, "the refused question")
        log.refused(2, "not_authorized")
        assert [row["turn"] for row in log.rows] == [1, 2]
        refused = log.row(2)
        assert refused["prompt"] == "the refused question"
        assert refused["reply"] == ""
        assert refused["refused"] == "not_authorized"

    def test_a_refused_turns_prompt_never_lands_on_the_previous_row(self) -> None:
        """The failure the ordinal anchor exists to prevent.

        Without it, turn 2's question fills turn 1's empty prompt and the outline
        reports one turn's question against another turn's answer. Turn 1 is left
        prompt-less on purpose so that mis-attribution has somewhere to land.
        """
        log = _Log()
        log.started(1)
        log.reply(1, 1, "answer to a question this row never recorded")
        log.completed(1)
        log.prompt(2, "the refused question")
        log.refused(2, "stopped_before_dispatch")
        assert log.row(1)["prompt"] == ""
        assert log.row(2)["prompt"] == "the refused question"

    def test_the_anchor_seq_is_the_first_entry_that_named_the_turn(self) -> None:
        """The load-through target: paging back through it loads the whole turn.

        In this log that entry is the PROMPT, not the boundary, which is what makes it
        a better target than the reference implementation's.
        """
        log = _Log()
        prompt_seq = log.prompt(1, "a question")
        boundary_seq = log.started(1)
        log.reply(1, 1, "an answer")
        log.completed(1)
        assert log.row(1)["seq"] == prompt_seq
        assert prompt_seq < boundary_seq

    def test_a_regressive_boundary_is_skipped_rather_than_unsorting_the_outline(self) -> None:
        log = _Log()
        _settled_turn(log, 5, "fifth question", "fifth answer")
        _settled_turn(log, 6, "sixth question", "sixth answer")
        # A boundary for a turn already behind the newest row. Placing it would unsort
        # the rows; it is dropped, and the standing rows are untouched.
        log.started(5, actor="crew")
        log.reply(5, 1, "a late reply to a turn already past")
        log.completed(5)
        assert [row["turn"] for row in log.rows] == [5, 6]
        assert log.row(5)["reply"] == "fifth answer"
        assert log.row(6)["reply"] == "sixth answer"

    def test_an_entry_naming_no_usable_turn_places_no_row(self) -> None:
        """A row is placed BY its ordinal, so an entry naming none names no row.

        ``True`` is included because ``bool`` is an ``int`` subclass in Python, so a
        planted ``turn: true`` would otherwise open a row at ordinal 1.
        """
        log = _Log()
        for planted in ({}, {"turn": "3"}, {"turn": None}, {"turn": -1}, {"turn": True}):
            log.add("message/received", {**planted, "role": "user", "text": "planted"})
        assert log.rows == []

    def test_the_window_drops_the_oldest_rows_and_says_how_many(self) -> None:
        log = _Log()
        over = crew_log.OUTLINE_TURN_LIMIT + 5
        for turn in range(1, over + 1):
            _settled_turn(log, turn, f"question {turn}", f"answer {turn}")
        value = crew_log._outline_render(log.state)
        assert len(value["turns"]) == crew_log.OUTLINE_TURN_LIMIT
        assert value["dropped"] == 5
        assert value["first_turn"] == 6
        assert value["last_turn"] == over

    def test_a_seq_at_or_below_the_folds_own_is_refused_loudly(self) -> None:
        """The one-succession-unit guard, the same the ``timeline`` fold states.

        A slot-keyed read would fold a second unit whose seq restarts at 1; two units
        interleaved as one conversation is a wrong answer, so it raises.
        """
        log = _Log()
        _settled_turn(log, 1, "a question", "an answer")
        with pytest.raises(CrewLogError) as caught:
            crew_log._outline_step(
                log.state,
                Entry(seq=1, time=9_999, type="turn/started", src=GATEWAY, data={"turn": 9}),
            )
        assert "one succession unit" in str(caught.value)


# --------------------------------------------------------------------------- #
# what a row carries
# --------------------------------------------------------------------------- #


class TestWhatARowCarries:
    def test_the_first_user_line_wins_and_a_steer_does_not_replace_it(self) -> None:
        """THE first-user-line rule. Three human messages in one turn, the first kept."""
        log = _Log()
        log.prompt(1, "the line that opened the turn")
        log.started(1)
        log.reply(1, 1, "working on it")
        log.prompt(1, "no, do it differently")
        log.prompt(1, "and also this")
        log.reply(1, 2, "done")
        log.completed(1)
        assert log.row(1)["prompt"] == "the line that opened the turn"

    def test_a_body_written_under_another_role_never_fills_the_prompt(self) -> None:
        """The role gate. Both writers pass ``role="user"`` today; this keeps that true.

        The second half is what makes the first non-vacuous: the same turn then takes a
        real user line, so the empty prompt above is the gate's doing and not the
        fixture's.
        """
        log = _Log()
        log.prompt(1, "from the system, not the human", role="system")
        log.prompt(1, "from an app", role="app")
        log.started(1)
        assert log.row(1)["prompt"] == ""
        log.prompt(1, "the human line")
        assert log.row(1)["prompt"] == "the human line"

    def test_the_newest_uncut_reply_is_the_settled_one(self) -> None:
        """Settled rule 2: a multi-step turn settles on its last model call."""
        log = _Log()
        log.prompt(1, "a question")
        log.started(1)
        log.reply(1, 1, "first thought")
        log.reply(1, 2, "second thought")
        log.reply(1, 3, "the settled answer")
        log.completed(1)
        assert log.row(1)["reply"] == "the settled answer"

    def test_a_steered_reply_is_cut_and_never_becomes_the_settled_one(self) -> None:
        """Settled rule 1, and the half that matters: a cut reply does not CLEAR.

        The turn's last entry is the interrupted one, so a fold that merely preferred
        the newest reply would settle on it, and one that let a cut reply blank the
        draft would settle on nothing. Both are wrong: the standing uncut answer is
        what the turn settled on.
        """
        log = _Log()
        log.prompt(1, "a question")
        log.started(1)
        log.reply(1, 1, "the uncut answer")
        log.reply(1, 2, "half a sentence that a steer cut off", interrupted=True)
        log.completed(1)
        assert log.row(1)["reply"] == "the uncut answer"

    def test_a_turn_whose_only_reply_was_cut_settles_on_nothing(self) -> None:
        log = _Log()
        log.prompt(1, "a question")
        log.started(1)
        log.reply(1, 1, "cut before it said anything useful", interrupted=True)
        log.completed(1)
        assert log.row(1)["reply"] == ""

    def test_an_open_turn_shows_no_reply_until_its_closer_lands(self) -> None:
        """Settled rule 3. A reply shown mid-turn reads a turn that has not happened."""
        log = _Log()
        log.prompt(1, "a question")
        log.started(1)
        log.reply(1, 1, "streamed but unsettled")
        assert log.row(1)["reply"] == ""
        assert log.state["draft"] == "streamed but unsettled"
        log.completed(1)
        assert log.row(1)["reply"] == "streamed but unsettled"

    def test_a_retry_retracts_the_previous_attempts_answer(self) -> None:
        """Settled rule 4, the divergence from the reference implementation.

        There a non-advancing boundary is skipped and the standing reply survives until
        something overwrites it. Here the rerun is a recorded fact (``attempt``), so the
        fold says the answer was retracted instead of serving a stale one. The prompt
        and the anchor survive: the rerun re-ran the same question.
        """
        log = _Log()
        anchor = log.prompt(1, "the question")
        log.started(1)
        log.reply(1, 1, "the first attempt's answer")
        log.completed(1)
        assert log.row(1)["reply"] == "the first attempt's answer"

        log.started(1, attempt=2)
        assert log.row(1) == {
            "turn": 1,
            "seq": anchor,
            "time": 1_000 + anchor,
            "actor": "user",
            "prompt": "the question",
            "reply": "",
            "attempt": 2,
        }
        log.reply(1, 1, "the rerun's answer")
        log.completed(1)
        assert log.row(1)["reply"] == "the rerun's answer"
        assert log.row(1)["prompt"] == "the question"

    def test_a_restart_clears_a_refusal_even_when_the_rerun_is_not_numbered(self) -> None:
        """The retraction keys on the START, not on the attempt number.

        A rerun is numbered only when the writer can number it, and for a refused turn
        it cannot: ``on_turn_refused`` does not advance the attempt counter, so a
        rewind or a regenerate of a refused turn starts again at attempt 1 and the
        field is omitted. Keying on the number alone would leave one try's refusal
        standing beside the next try's answer, which reads as a turn that was both
        blocked and answered.
        """
        log = _Log()
        log.prompt(3, "the question a gate stopped")
        log.refused(3, "not_authorized")
        assert log.row(3)["refused"] == "not_authorized"

        # No ``attempt``: this is what the writer produces for a refused turn rerun.
        log.started(3)
        log.reply(3, 1, "the answer the rerun gave")
        log.completed(3)

        row = log.row(3)
        assert "refused" not in row, row
        assert row["reply"] == "the answer the rerun gave"
        assert row["prompt"] == "the question a gate stopped"

    def test_a_restart_clears_the_previous_answer_even_when_the_rerun_is_not_numbered(
        self,
    ) -> None:
        """``reply`` is the row's other terminal marker, and the same rule governs it.

        Rule 4 retracts a numbered rerun's answer. An UNnumbered restart is the same
        event with the number missing, so the standing answer goes then too -- otherwise
        the row serves the previous try's answer for as long as the new try runs.
        """
        log = _Log()
        _settled_turn(log, 4, "the question", "the first answer")
        assert log.row(4)["reply"] == "the first answer"

        log.started(4)
        row = log.row(4)
        assert row["reply"] == "", row
        # Not NUMBERED, because the writer did not number it: the retraction and the
        # record of which try this is are separate facts.
        assert "attempt" not in row, row

    def test_a_restart_that_is_refused_again_shows_only_the_new_reason(self) -> None:
        """Both markers under one sequence: the row carries this try's outcome alone."""
        log = _Log()
        log.prompt(5, "the question")
        log.refused(5, "not_authorized")
        log.started(5)
        log.refused(5, "budget_exhausted")

        row = log.row(5)
        assert row["refused"] == "budget_exhausted"
        assert row["reply"] == ""

    def test_a_retry_that_is_cut_short_leaves_no_stale_answer_standing(self) -> None:
        """The combination the two rules have to survive together.

        Attempt 1 answered, attempt 2 was retried and then only cut -- so the row must
        be empty rather than re-serving attempt 1's retracted answer.
        """
        log = _Log()
        _settled_turn(log, 1, "the question", "attempt one's answer")
        log.started(1, attempt=2)
        log.reply(1, 1, "cut off again", interrupted=True)
        log.completed(1)
        assert log.row(1)["reply"] == ""

    def test_a_refusal_drops_a_draft_rather_than_committing_it(self) -> None:
        log = _Log()
        log.prompt(1, "a question")
        log.started(1)
        log.reply(1, 1, "a draft from before the refusal")
        log.refused(1, "gateway_closing")
        assert log.state["draft"] == ""
        assert log.row(1)["reply"] == ""
        assert log.row(1)["refused"] == "gateway_closing"

    def test_an_unclosed_turns_draft_is_never_committed_onto_the_next_turns_row(self) -> None:
        """Why the draft carries its own ordinal rather than riding on the newest row.

        This log shape is ordinary, not fabricated: ``turn/completed`` has a
        crash-repair writer, so a turn whose closer never landed is a real file. Turn 1
        answered and was never closed, then turn 2 opened and closed. Without the
        ordinal on the draft, turn 1's answer lands in turn 2's row -- one turn's answer
        reported against another turn's question, which is the mis-attribution this
        guard exists for.

        The row-opening rule cannot decide this case, which is the point: a closer for a
        turn ABOVE the newest row opens its own row and a regressive one is dropped
        before the commit is reached, so only a closer for the newest row -- this one --
        reaches the guard at all.
        """
        log = _Log()
        log.prompt(1, "turn one's question")
        log.started(1)
        log.reply(1, 1, "turn one's answer, never closed")
        log.prompt(2, "turn two's question")
        log.started(2)
        log.completed(2)
        assert log.row(1)["reply"] == ""
        assert log.row(2)["reply"] == ""
        assert log.row(2)["prompt"] == "turn two's question"

    @pytest.mark.parametrize(
        ("field", "limit"),
        [("prompt", crew_log.OUTLINE_PROMPT_CHARS), ("reply", crew_log.OUTLINE_REPLY_CHARS)],
    )
    def test_a_preview_is_clipped_at_its_boundary_in_characters(
        self, field: str, limit: int
    ) -> None:
        """The clamp, measured AT the boundary in both directions.

        Exactly *limit* characters survive whole with no ellipsis; one more is clipped
        to *limit* with an ellipsis as its last character. Characters and not bytes,
        which the CJK case below is what actually proves.
        """
        at_limit = "x" * limit
        over_limit = "y" * (limit + 1)
        for text, expected_clip in ((at_limit, False), (over_limit, True)):
            log = _Log()
            log.prompt(1, text if field == "prompt" else "a question")
            log.started(1)
            log.reply(1, 1, text if field == "reply" else "an answer")
            log.completed(1)
            value = log.row(1)[field]
            assert len(value) == limit, (field, text[:1], len(value))
            assert value.endswith("\u2026") is expected_clip, (field, text[:1])

    def test_the_clamp_counts_characters_so_a_wide_body_is_not_cut_short(self) -> None:
        """The unit the clamp counts in, which is the whole point of stating it.

        A 4-byte character fills four times the bytes of an ASCII one, so a clamp that
        counted bytes would return a quarter of the characters here. The byte cost is
        then read OFF the character clamp, which is the direction the row-cost
        measurements in this module are taken in.
        """
        wide = "\u6f22" * (crew_log.OUTLINE_PROMPT_CHARS + 40)
        log = _Log()
        log.prompt(1, wide)
        log.started(1)
        value = log.row(1)["prompt"]
        assert len(value) == crew_log.OUTLINE_PROMPT_CHARS
        assert value.count("\u6f22") == crew_log.OUTLINE_PROMPT_CHARS - 1
        assert len(value.encode("utf-8")) > crew_log.OUTLINE_PROMPT_CHARS

    def test_whitespace_is_collapsed_to_one_line(self) -> None:
        log = _Log()
        log.prompt(1, "  a question\n\nover\tseveral   lines  ")
        log.started(1)
        assert log.row(1)["prompt"] == "a question over several lines"

    def test_a_whitespace_only_body_leaves_the_column_empty_and_retryable(self) -> None:
        """Empty is not "filled with nothing": the next real line still lands."""
        log = _Log()
        log.prompt(1, "   \n\t ")
        log.started(1)
        assert log.row(1)["prompt"] == ""
        log.prompt(1, "the real line")
        assert log.row(1)["prompt"] == "the real line"

    def test_a_body_whose_unread_tail_is_whitespace_still_says_it_was_cut(self) -> None:
        """The slice-before-collapse case the ellipsis has to survive.

        The read window is wider than the clamp, so a whitespace-heavy body collapses
        to UNDER the clamp while real text sits past the window unread. Reporting that
        as a whole line would be the one wrong answer here.
        """
        limit = crew_log.OUTLINE_PROMPT_CHARS
        airy = ("word" + " " * 40) * ((limit * 4) // 44 + 2)
        log = _Log()
        log.prompt(1, airy + "TAIL")
        log.started(1)
        value = log.row(1)["prompt"]
        assert value.endswith("\u2026")
        assert "TAIL" not in value

    def test_the_actor_that_caused_the_turn_rides_on_the_row(self) -> None:
        """So a row with no human line is still labelled rather than blank."""
        log = _Log()
        log.started(1, actor="cron")
        log.reply(1, 1, "a scheduled turn's answer")
        log.completed(1)
        row = log.row(1)
        assert row["actor"] == "cron"
        assert row["prompt"] == ""
        assert row["reply"] == "a scheduled turn's answer"

    def test_attempt_is_absent_on_a_first_attempt(self) -> None:
        """Absent rather than 1: the writer omits it at 1 and the row follows suit."""
        log = _Log()
        _settled_turn(log, 1, "a question", "an answer")
        assert "attempt" not in log.row(1)
        assert "refused" not in log.row(1)


# --------------------------------------------------------------------------- #
# what never reaches a row
# --------------------------------------------------------------------------- #


class TestWhatNeverReachesARow:
    #: The entries that carry injected context and tool output. Each is driven at the
    #: ordinal of a LIVE row, so a leak has an open row to land in.
    LEAKY: tuple[tuple[str, dict[str, Any]], ...] = (
        (
            "context/composed",
            {
                "turn": 1,
                "sources": [
                    {"kind": "user-rule", "chars": 4_000, "tokens": 1_000},
                    {"kind": "memory", "chars": 9_000, "tokens": 2_250},
                    {"kind": "environment", "chars": 600, "tokens": 150},
                    {"kind": "hook", "chars": 300, "tokens": 75},
                ],
                "chars": 13_900,
                "tokens": 3_475,
                "tokens_estimated": True,
                "phase": "per_turn",
            },
        ),
        ("tool/called", {"turn": 1, "call_id": "c1", "tool": "execute_bash", "step": 1}),
        (
            "tool/completed",
            {"turn": 1, "call_id": "c1", "tool": "execute_bash", "ok": True, "ms": 12},
        ),
        ("step/started", {"turn": 1, "step": 1}),
        ("step/completed", {"turn": 1, "step": 1, "ms": 12}),
        ("request/configured", {"turn": 1, "model": "a-model", "provider": "p", "system": "sha"}),
        ("message/chunk", {"turn": 1, "step": 1, "delta": "a slice of an oversize body"}),
        ("plan/updated", {"turn": 1, "count": 3}),
        ("approval/requested", {"turn": 1, "approval_id": "a1", "tool": "execute_bash"}),
        ("object/observed", {"turn": 1, "kind": "file", "count": 1}),
    )

    def test_no_entry_carrying_injected_context_or_tool_output_moves_a_row(self) -> None:
        """THE negative contract, driven against an open row rather than an empty fold."""
        log = _Log()
        log.prompt(1, "the question")
        log.started(1)
        log.reply(1, 1, "the answer")
        before = json.dumps(crew_log._outline_render(log.state), sort_keys=True)
        for entry_type, data in self.LEAKY:
            log.add(entry_type, data)
        log.completed(1)
        row = log.row(1)
        assert row["prompt"] == "the question"
        assert row["reply"] == "the answer"
        # Nothing the leaky entries carried is anywhere in the rendered value.
        rendered = json.dumps(crew_log._outline_render(log.state), sort_keys=True)
        for needle in ("user-rule", "memory", "environment", "hook", "execute_bash", "oversize"):
            assert needle not in rendered, needle
        # And they moved nothing at all before the closer committed the reply.
        assert before == json.dumps(
            {
                **crew_log._outline_render(log.state),
                "turns": [{**row, "reply": ""}],
            },
            sort_keys=True,
        )

    def test_the_probe_above_is_not_vacuous(self) -> None:
        """Proves the fold CAN be moved by the entries it does read.

        Without this, "the rows did not move" reads the same whether the filter works
        or the harness never reached the fold.
        """
        log = _Log()
        log.prompt(1, "the question")
        log.started(1)
        assert log.row(1)["prompt"] == "the question"

    def test_the_folds_affects_set_names_none_of_them(self) -> None:
        """The structural half: a type absent from ``affects`` never reaches ``step``.

        The kernel skips both the copy and the step for a type a fold does not name, so
        this is what makes the assertion above a property of the registry rather than of
        ``_outline_step``'s first line.
        """
        affects = crew_log._FOLDS["outline"].affects
        assert affects == crew_log.OUTLINE_TYPES
        assert affects is not None
        for entry_type, _ in self.LEAKY:
            assert entry_type not in affects, entry_type
            assert not crew_log._FOLDS["outline"].touched_by_type(entry_type), entry_type
        for entry_type in crew_log.OUTLINE_TYPES:
            assert crew_log._FOLDS["outline"].touched_by_type(entry_type), entry_type


# --------------------------------------------------------------------------- #
# registry wiring and cost
# --------------------------------------------------------------------------- #


class TestTheRegistryWiring:
    def test_the_fold_is_registered_session_keyed_and_unadvertised(self) -> None:
        """The two choices the fold had to make, pinned where a reader can read them.

        Session-keyed because a turn ordinal and a seq both restart in the next
        succession unit. Unadvertised because no side panel draws it, and the comment
        at ``PROJECTION_NAMES`` forbids pushing a projection with no reader to every
        owner socket on every log growth.
        """
        assert "outline" in crew_log._FOLDS
        assert "outline" in crew_log.FOLD_NAMES
        assert "outline" in crew_log.INTERNAL_PROJECTION_NAMES
        assert "outline" in crew_log.SESSION_FOLD_NAMES
        assert "outline" not in crew_log.PROJECTION_NAMES
        assert "outline" not in crew_log.SLOT_PROJECTION_NAMES

    def test_a_session_keyed_fold_needs_no_slot_row_cost(self) -> None:
        """It has no warm SLOT cell, so the row-bytes budget does not describe it.

        Its own memo weighs it by serialized size instead. Asserting the absence is
        what keeps a later move to slot-keyed from silently skipping the measurement
        that budget requires.
        """
        assert "outline" not in crew_log._SLOT_FOLD_ROW_BYTES
        assert crew_log._FOLDS["outline"].count_rows is None

    def test_the_fold_stands_at_the_unmoved_state_version(self) -> None:
        """It is new, so it has never had a stored meaning to retire."""
        assert crew_log._FOLDS["outline"].state_version == crew_log._FOLD_STATE_VERSION_BASE

    def test_the_step_cannot_reach_the_state_it_was_handed(self) -> None:
        """``copy_state`` is spelled out, so this is what proves it deep enough.

        A row left shared would show up here as the prior state moving -- the same
        check ``test_a_fold_never_reaches_into_the_state_it_was_handed`` makes of
        every fold, driven here against the row list and the rows inside it.
        """
        log = _Log()
        _settled_turn(log, 1, "the question", "the answer")
        snapshot = crew_log._outline_copy(log.state)
        _settled_turn(log, 2, "a second question", "a second answer")
        log.started(1)  # regressive; cannot reach the snapshot either
        assert [row["turn"] for row in snapshot["turns"]] == [1]
        assert snapshot["turns"][0]["reply"] == "the answer"
        assert snapshot["turns"][0] is not log.state["turns"][0]

    def test_a_full_window_of_widest_rows_stays_a_bounded_value(self) -> None:
        """The byte cost of the choice to give a row TWO free-text columns.

        Measured at the CAP in 4-byte characters, which is the direction every row-cost
        figure in this module is taken in: a realistic ASCII window is about a quarter
        of this. Reported as a plain assertion against the entry ceiling's own order of
        magnitude so that raising a clamp without re-reading this fails here.
        """
        log = _Log()
        wide_prompt = "\U0001f600" * (crew_log.OUTLINE_PROMPT_CHARS + 10)
        wide_reply = "\U0001f600" * (crew_log.OUTLINE_REPLY_CHARS + 10)
        for turn in range(1, crew_log.OUTLINE_TURN_LIMIT + 1):
            _settled_turn(log, turn, wide_prompt, wide_reply)
        value = crew_log._outline_render(log.state)
        assert len(value["turns"]) == crew_log.OUTLINE_TURN_LIMIT
        measured = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        per_row = measured // crew_log.OUTLINE_TURN_LIMIT
        # One row's widest form, and the window built from them. The figures are the
        # point of the test: a clamp raised without re-measuring moves them.
        assert per_row < 1_600, per_row
        assert measured < 320_000, measured
