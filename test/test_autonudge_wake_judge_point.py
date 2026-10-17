"""The wake judge's decision point: its mapping table, its questions and its bounds.

Covers the answer-mapping table, the three questions, the state and evidence bounds with the
body-stash loss notices, and ``point.judge_tick``'s empty-delta, shed and fall-open verdicts.
No Jev key is spent: the provider is stubbed at ``decisions.decide``, the seam the point
calls. The shared autouse fixture comes from ``autonudge_wake_judge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from typing import Any
from unittest.mock import patch

import pytest
from autonudge_wake_judge_helpers import _restore_judge_process_globals  # noqa: F401 - fixture
from autonudge_wake_judge_helpers import answers

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.irq import Outcome


def _refused_row(first_seen: bool | None) -> dict[str, object]:
    """One evidence row whose text the per-item scrub refuses.

    A JWT-shaped literal, which is a thing a third-party commenter pastes into a pull
    request without meaning anything by it -- the point being that the trigger is
    ordinary, not exotic.
    """
    row: dict[str, object] = {
        "source": "pr:owner/repo#1 by someone",
        "kind": "pr_comment",
        "age_s": 1.0,
        "text": "eyJhbGciOiJIUzI1NiJ9." + "A" * 60 + "." + "B" * 43,
    }
    if first_seen is not None:
        row["first_seen_this_tick"] = first_seen
    return row


class TestMappingTable:
    """Every branch of :func:`nudge_wake.map_answers`, including the failure ones."""

    def test_no_answer_is_fallback(self) -> None:
        assert point.map_answers(None).outcome is Outcome.FALLBACK

    def test_missing_question_is_fallback(self) -> None:
        partial = answers()
        del partial[point.Q_OUTCOME]
        assert point.map_answers(partial).outcome is Outcome.FALLBACK

    def test_out_of_domain_outcome_is_fallback(self) -> None:
        assert point.map_answers(answers(outcome="banana")).outcome is Outcome.FALLBACK

    def test_low_confidence_wakes(self) -> None:
        verdict = point.map_answers(answers(outcome_p=point.OUTCOME_MIN_P - 0.01))
        assert verdict.outcome is Outcome.WAKE

    def test_needs_owner_wake_at_bar(self) -> None:
        verdict = point.map_answers(
            answers(owner=point.NEEDS_OWNER_WAKE, owner_p=point.NEEDS_OWNER_MIN_P)
        )
        assert verdict.outcome is Outcome.WAKE

    def test_needs_owner_wake_below_bar_does_not_fire_on_that_rule(self) -> None:
        """The threshold is applied literally, as the design specifies.

        Only reachable from a provider that returns a non-argmax choice: with two
        options the chosen one carries at least half the mass.
        """
        verdict = point.map_answers(
            answers(owner=point.NEEDS_OWNER_WAKE, owner_p=point.NEEDS_OWNER_MIN_P - 0.01)
        )
        assert verdict.outcome is Outcome.QUIET

    @pytest.mark.parametrize("value", sorted(point.ACTION_OUTCOMES))
    def test_action_outcomes_wake_even_when_owner_says_quiet(self, value: str) -> None:
        verdict = point.map_answers(answers(owner=point.NEEDS_OWNER_QUIET, outcome=value))
        assert verdict.outcome is Outcome.WAKE

    @pytest.mark.parametrize("value", sorted(point.QUIET_OUTCOMES))
    def test_quiet_outcomes_are_the_only_quiet(self, value: str) -> None:
        assert point.map_answers(answers(outcome=value)).outcome is Outcome.QUIET

    def test_quiet_is_exactly_the_two_rules_that_produce_it(self) -> None:
        """Which answers are silent, as an if-and-only-if over the WHOLE answer space.

        This replaces an earlier sweep that asserted one direction only -- no outcome
        outside :data:`QUIET_OUTCOMES` may be silent -- and it is deliberately STRONGER
        than what it replaces, in three ways. It sweeps both ``needs_owner`` answers and
        both probabilities independently rather than the outcome's alone; it asserts
        equality rather than non-membership, so an answer that SHOULD be silent and
        wakes fails here too; and it carries no allowlist, exception or skip, so a
        future rule that quietly widens what may be suppressed cannot slip through as
        one more admitted case.

        The predicate below is the mapping's quiet condition written out. Two rules
        produce silence and nothing else may:

        * a :data:`QUIET_OUTCOMES` outcome at or above :data:`OUTCOME_MIN_P`;
        * an :data:`ACTION_OUTCOMES` outcome under :data:`ACTION_OVERRIDE_MIN_P` whose
          ``needs_owner`` answered ``quiet``, cleared :data:`NEEDS_OWNER_MIN_P`, AND was
          the more confident of the two -- the owner's own criterion outranking a
          question that carries no criterion.

        So ``finished`` and ``broken`` are silent at no confidence and under no
        ``needs_owner`` answer, an unsure outcome always wakes, an unsure quiet never
        suppresses an action outcome, and neither does a quiet the outcome outranks.
        """

        def expect_quiet(owner: str, owner_p: float, value: str, outcome_p: float) -> bool:
            if outcome_p < point.OUTCOME_MIN_P:
                return False  # rule 2: an unsure judge hands the tick over
            if owner == point.NEEDS_OWNER_WAKE and owner_p >= point.NEEDS_OWNER_MIN_P:
                return False  # rule 3: the owner's criterion asked for the turn
            if value in point.ACTION_OUTCOMES:
                return (
                    owner == point.NEEDS_OWNER_QUIET
                    and owner_p >= point.NEEDS_OWNER_MIN_P
                    and owner_p > outcome_p
                    and outcome_p < point.ACTION_OVERRIDE_MIN_P
                )
            return value in point.QUIET_OUTCOMES

        probabilities = (0.0, 0.39, 0.4, 0.41, 0.49, 0.5, 0.59, 0.6, 0.75, 1.0)
        seen_quiet = 0
        for value in point.OUTCOME_OPTIONS:
            for owner in point.NEEDS_OWNER_OPTIONS:
                for owner_p in probabilities:
                    for outcome_p in probabilities:
                        verdict = point.map_answers(
                            answers(
                                owner=owner,
                                owner_p=owner_p,
                                outcome=value,
                                outcome_p=outcome_p,
                            )
                        )
                        want = expect_quiet(owner, owner_p, value, outcome_p)
                        assert (verdict.outcome is Outcome.QUIET) is want, (
                            value,
                            owner,
                            owner_p,
                            outcome_p,
                            verdict.outcome,
                        )
                        seen_quiet += int(want)
        # A control on the sweep itself: a predicate that never expects silence would
        # make every assertion above trivially true.
        assert seen_quiet > 0

    def test_a_confident_owner_quiet_survives_a_barely_confident_action_outcome(self) -> None:
        """The defect this bar exists for, at the numbers it was measured at.

        Eleven wakes in one logged window came only from the ``ACTION_OUTCOMES``
        clause, every one of them over a ``needs_owner`` that answered ``quiet``, and
        eight of those had ``outcome`` under 0.52 -- barely over the shared floor of
        0.40. A red pull request under repair answers ``needs_action`` every tick, so
        the veto fired for the whole repair.
        """
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.9,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=0.45,
            )
        )
        assert verdict.outcome is Outcome.QUIET
        # Both answers in the body: the reader of a suppressed tick has to be able to
        # see WHICH two readings disagreed, or the suppression is unexplainable.
        assert "0.90" in verdict.body and "0.45" in verdict.body
        assert point.OUTCOME_NEEDS_ACTION in verdict.body

    def test_an_action_outcome_at_the_override_bar_still_wakes(self) -> None:
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.9,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=point.ACTION_OVERRIDE_MIN_P,
            )
        )
        assert verdict.outcome is Outcome.WAKE

    def test_an_unsure_outcome_still_wakes_under_a_confident_owner_quiet(self) -> None:
        """Rule 2 is untouched: below the shared floor the call goes to the session.

        The new bar narrows one backstop. It must not reach underneath
        :data:`OUTCOME_MIN_P`, where an unsure judge has always handed the tick to the
        main session rather than guessing quiet.
        """
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.9,
                outcome=point.OUTCOME_NEEDS_HUMAN,
                outcome_p=point.OUTCOME_MIN_P - 0.05,
            )
        )
        assert verdict.outcome is Outcome.WAKE
        assert "unsure" in verdict.body

    def test_a_below_bar_owner_wake_is_not_a_quiet_answer(self) -> None:
        """Only ``needs_owner``'s VALUE being ``quiet`` may withhold the backstop.

        A ``wake`` answer that missed :data:`NEEDS_OWNER_MIN_P` fires no rule of its
        own, but nobody asserted the owner can be left alone either, so the
        unconditional action backstop still applies to it.
        """
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_WAKE,
                owner_p=point.NEEDS_OWNER_MIN_P - 0.01,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=0.45,
            )
        )
        assert verdict.outcome is Outcome.WAKE

    def test_the_override_bar_sits_above_the_confidence_floor(self) -> None:
        """A bar at or under the floor would be unreachable and change nothing."""
        assert point.ACTION_OVERRIDE_MIN_P > point.OUTCOME_MIN_P

    def test_the_override_bar_covers_the_measured_range_at_literal_numbers(self) -> None:
        """The bar's VALUE, pinned at literal probabilities rather than through itself.

        Every other assertion here spells the threshold as ``ACTION_OVERRIDE_MIN_P``, so
        all of them stay green if the constant is lowered -- and a drop to 0.5 would let
        back exactly the 0.50-0.59 band the logged verdicts were found in. These
        numbers are literal so that a change to the constant has to be made here too,
        in the open.
        """
        assert point.ACTION_OVERRIDE_MIN_P == 0.6
        confident_quiet = {"owner": point.NEEDS_OWNER_QUIET, "owner_p": 0.94}
        for probability in (0.41, 0.48, 0.5, 0.52, 0.59):
            verdict = point.map_answers(
                answers(
                    outcome=point.OUTCOME_NEEDS_ACTION, outcome_p=probability, **confident_quiet
                )
            )
            assert verdict.outcome is Outcome.QUIET, probability
        for probability in (0.6, 0.61, 0.9):
            verdict = point.map_answers(
                answers(
                    outcome=point.OUTCOME_NEEDS_ACTION, outcome_p=probability, **confident_quiet
                )
            )
            assert verdict.outcome is Outcome.WAKE, probability

    def test_the_quiet_answer_must_be_the_more_confident_of_the_two(self) -> None:
        """The veto is a RANKING, not just two independent floors.

        :data:`NEEDS_OWNER_MIN_P` cannot carry this rule on its own: with two options
        the chosen answer is the argmax, so it clears 0.5 by construction. Without the
        comparison a ``quiet`` at 0.51 would silence a ``needs_action`` at 0.59 -- the
        exact inversion this exception exists to prevent, since its justification is
        that a barely-confident answer must not beat a confident one.
        """
        loses = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.51,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=0.59,
            )
        )
        assert loses.outcome is Outcome.WAKE
        wins = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.62,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=0.55,
            )
        )
        assert wins.outcome is Outcome.QUIET
        assert "0.62" in wins.body and "0.55" in wins.body

    def test_an_equal_confidence_tie_wakes(self) -> None:
        """Strictly more confident, so a tie does not buy silence.

        The comparison is the owner's answer OUTRANKING what it overrides; equal
        readings rank neither, and the direction this whole point fails in is toward
        spending the turn.
        """
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.55,
                outcome=point.OUTCOME_NEEDS_ACTION,
                outcome_p=0.55,
            )
        )
        assert verdict.outcome is Outcome.WAKE

    def test_an_unsure_owner_quiet_cannot_suppress_an_action_outcome(self) -> None:
        """The quiet answer must clear its OWN bar before it withholds a wake.

        ``NEEDS_OWNER_MIN_P`` is enforced literally rather than assumed from a
        two-option argmax, precisely because a provider may return a non-argmax choice.
        Such a ``quiet`` answer asserts nothing anyone was sure of, so it must not buy
        silence about a ``needs_human`` tick -- the owner would otherwise hear nothing
        until the quiet-streak floor.
        """
        verdict = point.map_answers(
            answers(
                owner=point.NEEDS_OWNER_QUIET,
                owner_p=0.01,
                outcome=point.OUTCOME_NEEDS_HUMAN,
                outcome_p=0.59,
            )
        )
        assert verdict.outcome is Outcome.WAKE

    def test_terminal_bar_is_above_the_confidence_floor(self) -> None:
        """Ordering still matters, for the wording rather than for the outcome.

        Below :data:`OUTCOME_MIN_P` every answer takes the unsure branch, so a
        terminal bar underneath that floor would make the confident wording
        unreachable for part of its own range.
        """
        assert point.TERMINAL_MIN_P > point.OUTCOME_MIN_P

    def test_the_judge_can_never_end_a_loop(self) -> None:
        """No answer, at any confidence, may produce TERMINAL.

        The judge reads prose. If prose could end a watch, one hostile or mistaken
        comment in a watched transcript would buy permanent silence, so ending a loop
        stays with the typed probe layer. Swept across the whole answer space rather
        than asserted on the two obvious outcomes, because the guarantee is about
        every reachable answer and not about the cases someone remembered.
        """
        for outcome in point.OUTCOME_OPTIONS:
            for owner in point.NEEDS_OWNER_OPTIONS:
                for probability in (0.0, 0.39, 0.4, 0.5, 0.59, 0.6, 0.75, 1.0):
                    verdict = point.map_answers(
                        answers(
                            owner=owner, owner_p=probability, outcome=outcome, outcome_p=probability
                        )
                    )
                    assert verdict.outcome is not Outcome.TERMINAL, (outcome, owner, probability)

    @pytest.mark.parametrize("value", sorted(point.TERMINAL_OUTCOMES))
    def test_finished_and_broken_wake_at_every_confidence(self, value: str) -> None:
        for probability in (0.4, 0.59, 0.6, 0.95, 1.0):
            verdict = point.map_answers(answers(outcome=value, outcome_p=probability))
            assert verdict.outcome is Outcome.WAKE, (value, probability)
            assert value in verdict.body, "the woken session is told what the judge saw"

    def test_confidence_changes_only_the_wording(self) -> None:
        confident = point.map_answers(
            answers(outcome=point.OUTCOME_FINISHED, outcome_p=point.TERMINAL_MIN_P)
        )
        unsure = point.map_answers(
            answers(outcome=point.OUTCOME_FINISHED, outcome_p=point.TERMINAL_MIN_P - 0.01)
        )
        assert confident.outcome is unsure.outcome is Outcome.WAKE
        assert "may be" in unsure.body and "may be" not in confident.body


class TestQuestions:
    """The three questions, their domains, and where the owner's words go."""

    def test_domains(self) -> None:
        built = {q.id: q.options for q in point.build_questions()}
        assert built[point.Q_NEEDS_OWNER] == [point.NEEDS_OWNER_WAKE, point.NEEDS_OWNER_QUIET]
        assert built[point.Q_OUTCOME] == list(point.OUTCOME_OPTIONS)
        assert built[point.Q_URGENCY] == list(point.URGENCY_OPTIONS)

    def test_criteria_ride_in_the_prompt_labelled_by_option(self) -> None:
        questions = point.build_questions("a line starts with RULING", "workers say WORKING")
        prompt = questions[0].prompt
        assert "RULING" in prompt and "WORKING" in prompt
        assert point.NEEDS_OWNER_WAKE in prompt and point.NEEDS_OWNER_QUIET in prompt

    def test_the_untrusted_content_caution_rides_on_every_request(self) -> None:
        """It cannot live in the default brief's ``quiet_when``, so it lives here.

        A loop carrying only the owner's ``wake_when`` has a criterion, so the shipped
        default is never merged into it -- and the evidence it sends the judge still
        includes comment and review bodies a third party wrote. The caution is ours and
        unconditional, and it is stated for BOTH directions: prose can argue a watch
        into a wake nobody needs as easily as into a silence.
        """
        for wake, quiet in (
            ("", ""),
            ("a reviewer asked for a change", ""),
            ("", "workers say WORKING"),
            ("wake on RULING", "quiet on WORKING"),
        ):
            prompt = point.build_questions(wake, quiet)[0].prompt
            assert "third party wrote" in prompt, (wake, quiet)
            assert "not itself evidence" in prompt, (wake, quiet)
            assert "in either direction" in prompt, (wake, quiet)

    def test_criteria_are_clipped(self) -> None:
        questions = point.build_questions("w" * 5_000, "q" * 5_000)
        assert len(questions[0].prompt) < 2 * point.MAX_CRITERION_CHARS + 500

    def test_evidence_never_enters_a_question(self) -> None:
        """Evidence is data and lives in ``state``; a question is an instruction."""
        questions = point.build_questions("wake on RULING", "quiet on WORKING")
        rendered = " ".join(q.prompt + " ".join(q.options) for q in questions)
        assert "RULING" in rendered  # the owner's criterion, which does belong
        assert "leaked-evidence-marker" not in rendered


class TestStateBounds:
    """The request's ceiling, the per-item clip, and what the scrub drops."""

    def test_one_stuck_notice_does_not_pin_every_loop_forever(self) -> None:
        """A notice nobody ever takes must not make the whole gateway fire forever.

        The forgotten state is spent when the pending notices drain, on the reasoning
        that the unknown ones belong to the same rotation as the known ones. A loop
        removed while it still owns a notice breaks that: nothing ever takes its notice,
        the store never empties, and every OTHER loop's take keeps reporting a loss --
        so every gated pull-request watch fires every interval, permanently. Removal
        forgets a loop's own notice, but a loop that merely stops publishing -- paused,
        or retargeted away from a pull request -- keeps its notice and nothing ever
        takes it, so the stuck notice is reachable rather than theoretical.

        A rotation's worth of takes is the bound: past it, the unknown notices cannot
        still belong to the rotation the reasoning appeals to.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        judge._PR_BODIES_FORGOTTEN = 0
        cap = judge.MAX_BODY_STASHES

        # Overflow the notice store, which is what sets the forgotten state.
        for i in range(cap * 2 + 2):
            judge.publish_pr_bodies(f"loop-{i}", {"comment:1": "prose"})
        assert judge._PR_BODIES_FORGOTTEN > 0, "the notice store overflowed"

        # Every notice is taken EXCEPT one, whose loop was removed and never returns.
        pending = list(judge._PR_BODIES_DROPPED)
        assert len(pending) > 1, "more than one notice is pending"
        for key in pending[1:]:
            judge.take_pr_bodies(key)
        assert pending[0] in judge._PR_BODIES_DROPPED, "one notice is stuck"

        # A rotation's worth of takes by unrelated loops. Past that, no take may still
        # be answering for a loss whose rotation is long over.
        for i in range(cap + 1):
            judge.take_pr_bodies(f"unrelated-{i}")

        _, dropped = judge.take_pr_bodies("an-innocent-loop")
        assert dropped is False, (
            "a single stuck notice must not keep every loop's reading short -- that is "
            "every gated watch firing every interval for the life of the process"
        )

    def test_a_publish_with_no_bodies_also_spends_the_loss_notice(self) -> None:
        """Being heard from at all spends the notice, prose or no prose.

        A loop whose stash was evicted keeps its marker until it publishes again. If the
        empty-bodies path skipped the clearing, every later tick whose remarks carry no
        prose would claim a loss that did not happen -- a fabricated "not whole" reading,
        which the default brief turns into a delivered turn on evidence of nothing.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        judge._PR_BODIES_FORGOTTEN = 0
        cap = judge.MAX_BODY_STASHES

        judge.publish_pr_bodies("victim", {"review:R1": "prose"})
        for i in range(cap):
            judge.publish_pr_bodies(f"other-{i}", {"comment:1": "x"})
        assert "victim" in judge._PR_BODIES_DROPPED, "it was evicted"

        # A tick whose remarks carry no bodies at all.
        judge.publish_pr_bodies("victim", {})

        stashed, dropped = judge.take_pr_bodies("victim")
        assert stashed == {}, "there was nothing to stash"
        assert (
            dropped is False
        ), "and nothing was lost this tick, so the reading must not be stamped short"

    def test_a_dropped_stash_withholds_the_payload_from_the_judge(self) -> None:
        """The caller's wholeness guard already ran, against the reading as FETCHED.

        A payload that only becomes short at the merge would sail past that gate, and a
        QUIET drawn from it suppresses a wake that was owed. The decision lives in this
        module rather than at the call site because the call site is a closure inside
        the gateway's wiring that no test can reach.
        """
        observation = {"observation_status": "ok", "remarks": [{"id": "review:R1"}]}

        whole = judge.payload_for_judge(observation, {"review:R1": "please guard it"}, False)
        assert whole is not None, "a whole reading is handed to the judge"
        assert whole["remarks"][0]["body"] == "please guard it"

        assert judge.payload_for_judge(observation, {}, True) is None, (
            "a dropped stash withholds the payload instead of judging prose the judge "
            "never received"
        )
        assert observation["observation_status"] == "ok", "the durable record is untouched"

    def test_a_notice_the_store_could_not_keep_still_reports_a_loss(self) -> None:
        """The notice store is bounded too, and its overflow discards a loss record.

        A forgotten record would let its loop read missing prose as a whole reading,
        which is the one thing the record exists to prevent. The owner is unknown once
        the record is gone, so every take reports a loss until the pending notices
        drain: the identity is lost, the fact is not, and firing is the only direction
        that cannot withhold a wake from whoever it was.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        judge._PR_BODIES_FORGOTTEN = 0
        cap = judge.MAX_BODY_STASHES

        # Enough distinct loops that the notice store itself has to overflow.
        for i in range(cap * 2 + 2):
            judge.publish_pr_bodies(f"loop-{i}", {"comment:1": "prose"})
        assert judge._PR_BODIES_FORGOTTEN > 0, "the notice store overflowed"

        # A loop with no notice of its own still hears about a loss.
        _, dropped = judge.take_pr_bodies("a-loop-with-no-notice")
        assert dropped is True, (
            "with a record forgotten the owner is unknown, so no take may claim it was " "whole"
        )

    def test_the_forgotten_state_is_spent_once_the_notices_drain(self) -> None:
        """It must terminate, or every loop fires for the life of the process."""
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        judge._PR_BODIES_FORGOTTEN = 0
        cap = judge.MAX_BODY_STASHES

        for i in range(cap * 2 + 2):
            judge.publish_pr_bodies(f"loop-{i}", {"comment:1": "prose"})
        assert judge._PR_BODIES_FORGOTTEN > 0

        # Drain every pending notice.
        for key in list(judge._PR_BODIES_DROPPED):
            judge.take_pr_bodies(key)

        _, dropped = judge.take_pr_bodies("someone-else")
        assert dropped is False, "once nothing is pending the unknown set is spent too"
        assert judge._PR_BODIES_FORGOTTEN == 0

    def test_the_body_stash_evicts_by_publish_recency_not_by_first_sight(self) -> None:
        """A dict keeps a key's original position, so assigning would pick one victim forever.

        The loop this process saw first would sit at the front of the queue for the
        life of the gateway and lose its stash to every eviction, however recently it
        published -- while the genuinely abandoned stashes the cap exists to reclaim
        sat behind it untouched.

        The republish has to happen while the key is STILL PRESENT. Re-adding a key
        that was already evicted lands it at the back either way, so a sequence that
        evicts first cannot tell the two behaviours apart.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        cap = judge.MAX_BODY_STASHES

        judge.publish_pr_bodies("first-ever", {"comment:1": "the earliest loop"})
        for i in range(cap - 1):
            judge.publish_pr_bodies(f"other-{i}", {"comment:1": "x"})
        assert len(judge._PR_BODIES) == cap, "exactly at the cap, so nothing has been dropped"
        assert "first-ever" in judge._PR_BODIES, "and it is still present, at the front"

        # Republished while present: its position must follow this publish, not its first.
        judge.publish_pr_bodies("first-ever", {"comment:1": "still here"})
        # One more entry forces exactly one eviction, taken from the front.
        judge.publish_pr_bodies("newcomer", {"comment:1": "y"})

        stashed, dropped = judge.take_pr_bodies("first-ever")
        assert stashed == {"comment:1": "still here"}, (
            "a loop that republished must not be the victim -- its position has to "
            "follow its last publish, not its first"
        )
        assert dropped is False
        assert "other-0" not in judge._PR_BODIES, "the least recently published one went instead"
        assert len(judge._PR_BODIES) <= cap, "and the cap still holds"

    def test_a_dropped_stash_is_counted_and_makes_the_next_tick_read_unwhole(self) -> None:
        """Prose the judge never saw must not reach it as a remark with nothing in it.

        A silent eviction is indistinguishable from a review that carried no body, so
        the loop that lost its stash would be screened quiet on evidence it never got.
        The loss is expressed as a reading that is not whole, which already forces a
        fire, and it is counted so the overflow is a number rather than an inference.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        before = judge._PR_BODIES_DROPPED_TOTAL
        cap = judge.MAX_BODY_STASHES

        judge.publish_pr_bodies("victim", {"review:R1": "please guard the windows branch"})
        for i in range(cap):
            judge.publish_pr_bodies(f"other-{i}", {"comment:1": "x"})

        stashed, dropped = judge.take_pr_bodies("victim")
        assert stashed == {}, "its bodies are gone"
        assert dropped is True, "and the loss is reported rather than looking like silence"
        assert judge._PR_BODIES_DROPPED_TOTAL == before + 1, "the overflow is counted"

        observation = {"observation_status": "ok", "remarks": [{"id": "review:R1"}]}
        payload = judge.with_remark_bodies(observation, stashed, dropped)
        assert judge.pr_target_is_unread(payload) is True, "so the tick fires"
        assert any("stash cap" in str(r) for r in payload["incomplete"]), "and says why"
        assert observation["observation_status"] == "ok", "the durable record is untouched"

    def test_a_republish_spends_the_notice_about_an_earlier_loss(self) -> None:
        """A loop holding fresh prose is owed no forced fire for a stash it replaced."""
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        cap = judge.MAX_BODY_STASHES

        judge.publish_pr_bodies("victim", {"review:R1": "old prose"})
        for i in range(cap):
            judge.publish_pr_bodies(f"other-{i}", {"comment:1": "x"})
        assert "victim" in judge._PR_BODIES_DROPPED, "it was evicted"

        judge.publish_pr_bodies("victim", {"review:R2": "new prose"})
        stashed, dropped = judge.take_pr_bodies("victim")
        assert stashed == {"review:R2": "new prose"}
        assert dropped is False, "the publish repaired the loss, so nothing is owed"

    @staticmethod
    def _two_loops_with_judge_state(loop_a: str, loop_b: str) -> int:
        """Both loops hold a stash AND a notice, under an owner-unknown loss mid-rotation.

        Seeded directly rather than through the cap, because the cap never leaves a loop
        holding both at once and a forget must clear whichever the loop has. Returns the
        dropped total so a test can assert it did not move.
        """
        judge._PR_BODIES.clear()
        judge._PR_BODIES_DROPPED.clear()
        judge.publish_pr_bodies(loop_a, {"comment:1": "a's prose"})
        judge.publish_pr_bodies(loop_b, {"comment:1": "b's prose"})
        judge._PR_BODIES_DROPPED[loop_a] = None
        judge._PR_BODIES_DROPPED[loop_b] = None
        judge._PR_BODIES_FORGOTTEN = 1
        judge._PR_BODIES_FORGOTTEN_TAKES = 3
        return judge._PR_BODIES_DROPPED_TOTAL

    @staticmethod
    def _assert_only_a_was_forgotten(loop_a: str, loop_b: str, dropped_total: int) -> None:
        assert loop_a not in judge._PR_BODIES, "the removed loop's stash is gone"
        assert loop_a not in judge._PR_BODIES_DROPPED, "and so is its loss notice"
        assert judge._PR_BODIES[loop_b] == {"comment:1": "b's prose"}, "the other stash stays"
        assert loop_b in judge._PR_BODIES_DROPPED, "and so does the other notice"
        assert judge._PR_BODIES_FORGOTTEN == 1, (
            "the forgotten state answers for a notice whose owner is unknown; a forget "
            "that names its loop must not spend it"
        )
        assert judge._PR_BODIES_FORGOTTEN_TAKES == 3, "nor advance the rotation that ends it"
        assert judge._PR_BODIES_DROPPED_TOTAL == dropped_total, "a count of losses is history"

    def test_forgetting_a_loop_drops_its_stash_and_notice_and_nothing_else(self) -> None:
        """The remover knows the owner, so it clears exactly that owner's two entries.

        A removed loop never takes again, so its stash would sit in the queue until the
        cap evicted it and its notice would count as pending until the store rotated
        past it -- the dropped total overstating real losses along the way. Neither
        the forgotten state nor its take counter may move: both exist for a notice
        whose owner is unknown, and a forget that names the loop is the one case that
        has no use for them.
        """
        total = self._two_loops_with_judge_state("loop-a", "loop-b")

        judge.forget_pr_bodies("loop-a")

        self._assert_only_a_was_forgotten("loop-a", "loop-b", total)
        # A loop this module never heard of, and no loop at all, are both no-ops.
        judge.forget_pr_bodies("never-published")
        judge.forget_pr_bodies("")
        self._assert_only_a_was_forgotten("loop-a", "loop-b", total)

    @staticmethod
    def _assert_nothing_was_forgotten(loop_a: str, loop_b: str, dropped_total: int) -> None:
        for key in (loop_a, loop_b):
            assert key in judge._PR_BODIES, f"{key} keeps its stash"
            assert key in judge._PR_BODIES_DROPPED, f"{key} keeps its loss notice"
        assert judge._PR_BODIES_FORGOTTEN == 1
        assert judge._PR_BODIES_FORGOTTEN_TAKES == 3
        assert judge._PR_BODIES_DROPPED_TOTAL == dropped_total

    def test_a_committed_removal_forgets_the_loop_s_judge_state(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Both removers forget once the store has committed: the sync one after its
        own save, the async one after the write it offloads."""

        async def main() -> None:
            svc = AutoNudgeService(base_dir=tmp_path)
            sync_gone = await svc.add("chat-1-1", "watch it", idle_secs=60)
            async_gone = await svc.add("chat-3-3", "watch this", idle_secs=60)
            kept = await svc.add("chat-2-2", "watch that", idle_secs=60)

            total = self._two_loops_with_judge_state(sync_gone.id, kept.id)
            assert svc.remove_sync(sync_gone.id, persist=True) is sync_gone
            self._assert_only_a_was_forgotten(sync_gone.id, kept.id, total)

            total = self._two_loops_with_judge_state(async_gone.id, kept.id)
            assert await svc.remove(async_gone.id) is True
            self._assert_only_a_was_forgotten(async_gone.id, kept.id, total)

        asyncio.run(main())

    def test_a_removal_whose_write_fails_keeps_the_loop_s_judge_state(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Persistence is the commit point, and a loop put back is not gone.

        The async remover pops the row first and writes second; a failed write restores
        the row. Its stash must come back with it, or the restored loop's next tick
        takes ``({}, False)`` -- bodyless remarks under a reading still stamped whole,
        and a QUIET drawn from that withholds a wake that was owed.
        """

        async def main() -> None:
            svc = AutoNudgeService(base_dir=tmp_path)
            stays = await svc.add("chat-1-1", "watch it", idle_secs=60)
            kept = await svc.add("chat-2-2", "watch that", idle_secs=60)
            total = self._two_loops_with_judge_state(stays.id, kept.id)

            def _wedged(payload: Any) -> None:
                raise OSError("disk wedged")

            monkeypatch.setattr(svc, "_write_state", _wedged)
            with pytest.raises(OSError):
                await svc.remove(stays.id)

            assert svc._loops[stays.id] is stays, "the failed removal put the row back"
            self._assert_nothing_was_forgotten(stays.id, kept.id, total)

        asyncio.run(main())

    def test_an_arm_whose_write_fails_keeps_the_displaced_loop_s_judge_state(
        self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A replacing arm pops the loop it displaces before the write that commits it."""

        async def main() -> None:
            svc = AutoNudgeService(base_dir=tmp_path)
            existing = await svc.add("chat-1-1", "watch it", idle_secs=60)
            kept = await svc.add("chat-2-2", "watch that", idle_secs=60)
            total = self._two_loops_with_judge_state(existing.id, kept.id)

            def _wedged(payload: Any) -> None:
                raise OSError("disk wedged")

            monkeypatch.setattr(svc, "_write_state", _wedged)
            with pytest.raises(OSError):
                await svc.add("chat-1-1", "watch it again", idle_secs=60)

            assert svc._loops[existing.id] is existing, "the displaced row came back"
            self._assert_nothing_was_forgotten(existing.id, kept.id, total)

        asyncio.run(main())

    def test_a_deferred_replacement_forgets_the_prior_only_when_it_commits(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The prior row of a deferred replacement can still be rolled back to.

        While the replacement is pending the prior is out of the live map but not out
        of reach, so its judge state has to wait with it: a rollback restores the prior
        and forgets the replacement, a commit forgets the prior.
        """
        from kiro_crew.monitoring.models import MonitorBudgets, MonitorCreationSurface

        async def replace(svc: AutoNudgeService, prior: NudgeLoop, loop_id: str) -> NudgeLoop:
            assert prior.monitor is not None
            return await svc.add_monitor(
                slot_key=prior.slot_key,
                kind=prior.monitor.kind,
                target=prior.monitor.target,
                objective=prior.monitor.objective,
                cadence_secs=prior.monitor.cadence_secs,
                budgets=prior.monitor.budgets,
                expected_existing_monitor_id=prior.id,
                expected_existing_config_generation=prior.monitor.config_generation,
                loop_id=loop_id,
                defer_replaced_trust_revocation=True,
                creation_surface=MonitorCreationSurface.DASHBOARD,
            )

        async def main() -> None:
            svc = AutoNudgeService(base_dir=tmp_path)
            prior = await svc.add_monitor(
                slot_key="chat-1-1",
                kind="gh-pr",
                target="acme/widgets#42",
                objective="review_ready",
                cadence_secs=60,
                budgets=MonitorBudgets(max_runtime_secs=600),
                creation_surface=MonitorCreationSurface.DASHBOARD,
            )
            kept = await svc.add("chat-2-2", "watch that", idle_secs=60)

            # Rolled back: the prior returns with its state, the replacement is gone.
            total = self._two_loops_with_judge_state(prior.id, kept.id)
            await replace(svc, prior, "rolled-back")
            self._assert_nothing_was_forgotten(prior.id, kept.id, total)
            judge.publish_pr_bodies("rolled-back", {"comment:1": "never read"})
            assert await svc.rollback_monitor_replacement("rolled-back") is True
            assert prior.id in svc._loops, "the prior row is live again"
            self._assert_nothing_was_forgotten(prior.id, kept.id, total)
            assert "rolled-back" not in judge._PR_BODIES, "the replacement's stash went"

            # Committed: now the prior is gone for good, and so is its state.
            total = self._two_loops_with_judge_state(prior.id, kept.id)
            await replace(svc, prior, "committed")
            svc.commit_monitor_replacement("committed")
            self._assert_only_a_was_forgotten(prior.id, kept.id, total)

        asyncio.run(main())

    def test_state_fits_the_ceiling_and_drops_oldest_first(self) -> None:
        evidence = [
            {
                "source": f"session:chat-{i}",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(i),
                "text": "x" * 900,
            }
            for i in range(40)
        ]
        trace: dict[str, Any] = {}
        state = point.build_state("watch the workers", evidence=evidence, trace=trace)
        assert trace["state_chars"] <= point.MAX_STATE_CHARS
        ages = [row["age_s"] for row in state["since_last_tick"]]
        assert ages == sorted(ages), "newest first"
        assert ages and max(ages) < 39.0, "the oldest items were the ones dropped"
        assert trace["dropped"] > 0

    def test_per_item_clip(self) -> None:
        state = point.build_state(
            "w",
            evidence=[
                {
                    "source": "s",
                    "kind": point.KIND_TRANSCRIPT_TAIL,
                    "age_s": 1.0,
                    "text": "y" * 9_000,
                }
            ],
        )
        assert len(state["since_last_tick"][0]["text"]) == point.MAX_ITEM_CHARS

    def test_instruction_is_clipped(self) -> None:
        state = point.build_state("i" * 9_000)
        assert len(state["loop"]["instruction"]) == point.MAX_INSTRUCTION_CHARS

    def test_last_verdict_is_carried(self) -> None:
        state = point.build_state(
            "w",
            evidence=[
                {
                    "source": "s",
                    "kind": point.KIND_PR_CHECKS,
                    "age_s": 1.0,
                    "text": "checks pending",
                }
            ],
            last_verdict={"outcome": "quiet", "evidence_items": 2},
        )
        assert state["last_verdict"]["outcome"] == "quiet"

    def test_scrub_drops_an_item_carrying_a_credential(self) -> None:
        assert point.evidence_item("pr:x#1", point.KIND_PR_CHECKS, 1.0, "AKIA" + "A" * 16) is None

    def test_unknown_kind_is_dropped(self) -> None:
        assert point.evidence_item("s", "not-a-kind", 1.0, "hello") is None

    def test_scrub_drop_everything_leaves_no_evidence(self) -> None:
        screened, dropped = point.screen_evidence(
            [
                {
                    "source": "s",
                    "kind": point.KIND_PR_CHECKS,
                    "age_s": 1.0,
                    "text": "AKIA" + "B" * 16,
                }
            ]
        )
        assert screened == [] and dropped == 1

    def test_the_cap_retains_the_newest_rows_across_targets(self) -> None:
        """The cap lands after the sort, so a later target keeps its newest rows.

        The collector groups rows by target and each group is chronological
        within itself, so a 2-target input is not globally newest-first. A cap
        taken off the front of that list would keep every stale row of the first
        target and shed the freshest rows of the second.
        """
        stale = [
            {
                "source": "chat-1",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"stale {age}",
            }
            for age in range(300, 270, -1)
        ]
        fresh = [
            {
                "source": "chat-2",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"fresh {age}",
            }
            for age in range(30, 0, -1)
        ]
        screened, dropped = point.screen_evidence([*stale, *fresh])
        assert len(screened) == point.MAX_EVIDENCE_ITEMS
        assert screened[0]["text"] == "fresh 1"
        texts = {row["text"] for row in screened}
        assert {f"fresh {age}" for age in range(1, 31)} <= texts
        assert dropped == len(stale) + len(fresh) - point.MAX_EVIDENCE_ITEMS

    def test_rows_the_cap_sheds_are_counted(self) -> None:
        items = [
            {
                "source": "chat-1",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": float(age),
                "text": f"row {age}",
            }
            for age in range(point.MAX_EVIDENCE_ITEMS + 5)
        ]
        screened, dropped = point.screen_evidence(items)
        assert len(screened) == point.MAX_EVIDENCE_ITEMS
        assert dropped == 5

    def test_a_fresh_scrub_refusal_is_counted_as_fresh(self) -> None:
        """The case the FALLBACK branch exists for: a loss arriving now may be actionable.

        Gating the branch on freshness must not weaken this direction. An item refused on
        the tick it first arrives is evidence the judge never saw, so the tick holds
        something it cannot send and must not read as calm.
        """
        refusals: dict[str, int] = {}
        point.screen_evidence([_refused_row(first_seen=True)], refusals)
        assert refusals.get("scrubbed") == 1, "the scrub refused the item"
        assert refusals.get("scrubbed_fresh") == 1, "and it counts as a fresh loss"

    def test_a_repeated_scrub_refusal_is_not_counted_as_fresh(self) -> None:
        """The defect this closes: the same refused body must not fire for hours.

        A remark stays inside the horizon for the full remark window, and the scrub
        refuses its body on every tick it is read. The pinned board and state rows keep
        the delta non-empty, so the refusal branch is reached every tick. Counting a
        repeated refusal as fresh made one commenter's long URL spend the caller's whole
        budget re-reporting a loss the loop had already fired for.
        """
        refusals: dict[str, int] = {}
        point.screen_evidence([_refused_row(first_seen=False)], refusals)
        assert refusals.get("scrubbed") == 1, "still counted in the total, for the trace"
        assert not refusals.get("scrubbed_fresh"), (
            "a refusal on prose already answered is not a fresh loss -- counting it fires "
            "the loop every interval for as long as the remark stays in the horizon"
        )

    def test_an_unstamped_row_is_treated_as_fresh(self) -> None:
        """An absent flag means the reading could not say, and firing is the safe way.

        An older build's reading carries no flag at all. Reading that as 'already seen'
        would withhold a wake on evidence that may be new, so an unknown resolves to
        fresh.
        """
        refusals: dict[str, int] = {}
        point.screen_evidence([_refused_row(first_seen=None)], refusals)
        assert refusals.get("scrubbed_fresh") == 1, "an unknown stamp reads as fresh"

    def test_the_remark_item_carries_the_stamp_as_a_flag(self) -> None:
        """The screen acts on the flag, so rendering it into prose alone is not enough."""
        items = judge._remark_items(
            {
                "remarks": [
                    {"kind": "comment", "author": "a", "body": "x", "first_seen_this_tick": False},
                    {"kind": "comment", "author": "b", "body": "y", "first_seen_this_tick": True},
                    {"kind": "comment", "author": "c", "body": "z"},
                ]
            },
            "owner/repo#1",
            1_800_000_000.0,
        )
        assert [i.get("first_seen_this_tick") for i in items] == [False, True, None], (
            "the flag rides on the row, and an absent one stays absent rather than being "
            "asserted either way"
        )

    def test_a_pinned_summary_outlives_a_comment_body(self) -> None:
        """The order the char budget gives items up in, and why it is not age.

        A check tally is observed on the tick that sends it, so by age it is always the
        newest item present; a comment body is as old as the comment. Shedding by age
        alone would keep the tally and drop the reviewer's words. The two summaries are
        also what the built-in criteria are asked against -- a failing check, a reading
        that is not whole -- so they are pinned and the prose is what goes.
        """
        items = [
            {
                "source": "pr:o/r#1",
                "kind": point.KIND_PR_CHECKS,
                "age_s": 1.0,
                "text": "failed 0, pending 0, passed 92",
            },
            {
                "source": "pr:o/r#1",
                "kind": point.KIND_PR_STATE,
                "age_s": 2.0,
                "text": "state=open mergeability=mergeable",
            },
            {
                "source": "pr:o/r#1 by a-reviewer",
                "kind": point.KIND_PR_COMMENT,
                "age_s": 3_600.0,
                "text": "please add a guard " + "p" * 900,
            },
        ]
        original = point.MAX_STATE_CHARS
        try:
            point.MAX_STATE_CHARS = 600
            state = point.build_state("watch o/r#1", evidence=items)
        finally:
            point.MAX_STATE_CHARS = original
        kinds = sorted(row["kind"] for row in state["since_last_tick"])
        assert kinds == sorted(
            [point.KIND_PR_CHECKS, point.KIND_PR_STATE]
        ), "the evidence the criteria are asked against is pinned; the prose is shed"

    def test_a_pinned_summary_is_shed_last_rather_than_never(self) -> None:
        """The char ceiling still holds when the pinned rows alone exceed it.

        A state past the bound is refused whole rather than trimmed, so a pin that
        never yields would lose the entire reading instead of one row of it. Within
        the pinned tier the oldest goes first, so the freshest summary survives.
        """
        items = [
            {
                "source": "pr:o/r#1",
                "kind": point.KIND_PR_CHECKS,
                "age_s": 1.0,
                "text": "c" * 700,
            },
            {
                "source": "pr:o/r#1",
                "kind": point.KIND_PR_STATE,
                "age_s": 2.0,
                "text": "s" * 700,
            },
        ]
        original = point.MAX_STATE_CHARS
        try:
            point.MAX_STATE_CHARS = 900
            state = point.build_state("watch o/r#1", evidence=items)
            rendered = point._rendered_len(state)
        finally:
            point.MAX_STATE_CHARS = original
        kinds = [row["kind"] for row in state["since_last_tick"]]
        assert kinds == [point.KIND_PR_CHECKS], "the oldest pinned row is the one given up"
        assert rendered <= 900, "the ceiling holds even when only pinned rows are left"

    def test_the_oldest_item_goes_once_only_bodies_are_left(self) -> None:
        """With no summaries to shed, age decides again -- oldest first."""
        items = [
            {
                "source": "pr:o/r#1 by one",
                "kind": point.KIND_PR_COMMENT,
                "age_s": 10.0,
                "text": "newest " + "n" * 700,
            },
            {
                "source": "pr:o/r#1 by two",
                "kind": point.KIND_PR_COMMENT,
                "age_s": 9_000.0,
                "text": "oldest " + "o" * 700,
            },
        ]
        original = point.MAX_STATE_CHARS
        try:
            point.MAX_STATE_CHARS = 900
            state = point.build_state("watch o/r#1", evidence=items)
        finally:
            point.MAX_STATE_CHARS = original
        texts = [row["text"] for row in state["since_last_tick"]]
        assert any(text.startswith("newest") for text in texts)
        assert not any(text.startswith("oldest") for text in texts)

    def test_a_body_the_scrub_refuses_is_dropped_rather_than_redacted(self) -> None:
        """A remark body is third-party prose, so it goes through the same screen.

        A redaction that left part of a credential in place would be worse than losing
        one observation on a path whose failure direction is to spend the turn anyway.
        """
        assert (
            point.evidence_item(
                "pr:o/r#1 by a-reviewer",
                point.KIND_PR_COMMENT,
                10.0,
                "use ghp_" + "A" * 36 + " to reproduce",
            )
            is None
        )
        assert (
            point.evidence_item(
                "pr:o/r#1 by a-reviewer", point.KIND_PR_COMMENT, 10.0, "please add a guard"
            )
            is not None
        )

    def test_a_body_longer_than_the_item_bound_is_clipped(self) -> None:
        item = point.evidence_item(
            "pr:o/r#1 by a-reviewer", point.KIND_PR_COMMENT, 10.0, "x" * 5_000
        )
        assert item is not None
        assert len(item["text"]) == point.MAX_ITEM_CHARS


class TestEmptyDelta:
    """An empty delta is QUIET only when every target was READ and had nothing new.

    The three causes of an empty delta are not one fact. A target that could not be
    read leaves the tick blind, and evidence the scrub shed leaves it unable to send
    what it found; answering either as calm would suppress a turn nobody established
    was unwanted. Each branch is told apart by its body, not only by its outcome, so
    a test cannot pass by reaching the right verdict for the wrong reason.
    """

    def test_every_target_read_and_nothing_new_is_quiet(self) -> None:
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=0))
        assert verdict.outcome is Outcome.QUIET
        assert "no new evidence" in verdict.body

    def test_targets_past_the_cap_count_as_dropped(self) -> None:
        """A bound that silently shrinks the reading must not make it look complete.

        The cap limits how many authorizations and reads one tick performs, which is
        its job. What it must not do is leave ``dropped`` at zero, because a confident
        quiet about the targets that FIT would then suppress a turn the omitted one
        might have needed -- the same fault as an unreadable target, arriving through a
        bound instead of a failure.
        """
        over = [f"chat-{n}-{n}" for n in range(judge.MAX_TARGETS + 2)]

        async def read_session(target: str, since: int) -> tuple[list[dict], int, int | None]:
            return [], since, since

        _, dropped = asyncio.run(
            judge.collect_evidence(over, cursors={}, read_session=read_session, read_pr=None)
        )
        assert dropped >= 2, "both targets past the cap are counted"

    def test_a_dropped_target_is_fallback_not_quiet(self) -> None:
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=1))
        assert verdict.outcome is Outcome.FALLBACK
        assert "could not read every target" in verdict.body

    def test_a_dropped_target_fires_even_when_another_target_yielded_rows(self) -> None:
        """One unread target is enough, whatever the readable ones produced.

        The blind-tick check has to sit ABOVE the empty-delta branch: a tick with one
        refused target and one talkative one has a NONEMPTY delta, so a branch reached
        only on an empty one never sees the drop, and a confident quiet about the
        target that WAS read would suppress the turn the unread one might have needed.
        """
        rows = [
            {
                "source": "session:chat-2-2",
                "kind": point.KIND_TRANSCRIPT_TAIL,
                "age_s": 1.0,
                "text": "WORKING",
            }
        ]
        verdict = asyncio.run(point.judge_tick("watch", evidence=rows, dropped=1))
        assert verdict.outcome is Outcome.FALLBACK
        assert "could not read every target" in verdict.body

    def test_a_fresh_scrub_refusal_fires_the_tick(self) -> None:
        """A loss arriving now may be the actionable half, so the tick must not read calm.

        A survivor keeps the delta non-empty, so the empty-delta branch above cannot
        answer this one -- it reaches the scrub branch, which fires.
        """
        rows = [
            _refused_row(first_seen=True),
            {
                "source": "pr:owner/repo#1",
                "kind": point.KIND_PR_STATE,
                "age_s": 1.0,
                "text": "open, mergeable",
            },
        ]
        verdict = asyncio.run(point.judge_tick("watch", evidence=rows, dropped=0))
        assert verdict.outcome is Outcome.FALLBACK
        assert "could not send every evidence item" in verdict.body

    def test_a_repeated_scrub_refusal_does_not_fire_the_tick(self) -> None:
        """The defect this closes, pinned at the verdict rather than at the counter.

        The refused body is prose the judge was already asked about on an earlier tick.
        The pinned state row keeps the delta non-empty every tick, so gating this branch
        on the TOTAL refusal count fires every cadence interval for as long as the remark
        stays in the horizon -- one commenter's long URL draining the caller's whole
        budget re-reporting a loss it already fired for.

        Dropping the freshness gate makes this test fail, which is the point of it.
        """
        rows = [
            _refused_row(first_seen=False),
            {
                "source": "pr:owner/repo#1",
                "kind": point.KIND_PR_STATE,
                "age_s": 1.0,
                "text": "open, mergeable",
            },
        ]
        trace: dict[str, object] = {}
        verdict = asyncio.run(point.judge_tick("watch", evidence=rows, dropped=0, trace=trace))
        # The body, not the outcome: with no provider configured this tick answers
        # FALLBACK for its own reason further down, and that is fine. What must not
        # happen is taking the PARTIAL-SHED exit, and the body is what names it.
        assert "could not send every evidence item" not in verdict.body, (
            "a refusal on already-answered prose must not take the partial-shed exit -- "
            "the pinned rows make this branch reachable on every tick for hours"
        )

    def test_a_quiet_empty_delta_reports_zero_items_and_no_answers(self) -> None:
        """The notice and the verdict record are built from the trace on this path too.

        A quiet tick writes one transcript row, so the count it reports has to come
        from the same out-parameter every other path fills; a caller left reading the
        list it passed in would render a judged-on count for a tick with nothing in it.
        """
        trace: dict[str, Any] = {}
        verdict = asyncio.run(point.judge_tick("watch", evidence=[], dropped=0, trace=trace))
        assert verdict.outcome is Outcome.QUIET
        assert trace["evidence_items"] == 0
        assert trace["answers"] is None

    def test_the_tick_hands_the_point_its_dropped_count(self) -> None:
        """The caller holds the count and the point decides on it.

        A collector reporting an unreadable target and a caller that does not forward
        it produce the one indistinguishable failure on this path: the point sees an
        empty delta with nothing dropped and rules the tick calm, so a blind tick is
        suppressed and reads in the transcript exactly like a quiet one.
        """
        source = inspect.getsource(AutoNudgeService._judge_tick_is_quiet)
        assert "dropped=dropped" in source, "the collector's count reaches the point"


def _session_row(age: float, text: str, *, seen: bool | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "source": "session:chat-2-2",
        "kind": point.KIND_TRANSCRIPT_TAIL,
        "age_s": age,
        "text": text,
    }
    if seen is not None:
        row["first_seen_this_tick"] = not seen
    return row


class TestNothingNewIsLostAndJudgedCalm:
    """A bound that sheds a FRESH item makes the tick fire.

    Already-seen items are context and go first; a fresh item is the delta, and a
    session row past the read cursor is never offered again.
    """

    def test_the_item_cap_sheds_seen_items_before_fresh_ones(self) -> None:
        seen = [_session_row(float(age), f"seen {age}", seen=True) for age in range(10)]
        fresh = [
            _session_row(float(age), f"fresh {age}")
            for age in range(100, 100 + point.MAX_EVIDENCE_ITEMS)
        ]
        refusals: dict[str, int] = {}
        screened, dropped = point.screen_evidence([*seen, *fresh], refusals)
        assert {row["text"] for row in screened} == {row["text"] for row in fresh}
        assert dropped == len(seen)
        assert not refusals.get("shed_fresh"), "only already-seen context was given up"

    def test_the_item_cap_counts_every_fresh_item_it_sheds(self) -> None:
        fresh = [
            _session_row(float(age), f"row {age}") for age in range(point.MAX_EVIDENCE_ITEMS + 3)
        ]
        refusals: dict[str, int] = {}
        point.screen_evidence(fresh, refusals)
        assert refusals.get("shed_fresh") == 3

    def test_the_char_budget_sheds_seen_items_first(self) -> None:
        items = [
            _session_row(1.0, "DONE: shipped"),
            _session_row(500.0, "s" * 900, seen=True),
            _session_row(600.0, "t" * 900, seen=True),
        ]
        trace: dict[str, Any] = {}
        original = point.MAX_STATE_CHARS
        try:
            point.MAX_STATE_CHARS = 600
            state = point.build_state("watch", evidence=items, trace=trace)
        finally:
            point.MAX_STATE_CHARS = original
        assert [row["text"] for row in state["since_last_tick"]] == ["DONE: shipped"]
        assert trace["shed_fresh"] == 0

    def test_the_char_budget_counts_a_fresh_item_it_sheds(self) -> None:
        items = [_session_row(float(age), f"step {age} " + "x" * 900) for age in range(1, 4)]
        trace: dict[str, Any] = {}
        original = point.MAX_STATE_CHARS
        try:
            point.MAX_STATE_CHARS = 1_500
            point.build_state("watch", evidence=items, trace=trace)
        finally:
            point.MAX_STATE_CHARS = original
        assert trace["shed_fresh"] >= 1

    def test_a_busy_worker_whose_rows_do_not_fit_fires_the_tick(self) -> None:
        """More fresh text than the budget holds must wake, not be judged on a slice."""
        rows = [_session_row(float(age), f"step {age} " + "x" * 900) for age in range(2, 14)]
        rows.append(_session_row(1.0, "DONE: shipped"))
        verdict = asyncio.run(point.judge_tick("watch", evidence=rows, dropped=0))
        assert verdict.outcome is Outcome.FALLBACK
        assert "could not send every evidence item" in verdict.body

    def test_shedding_only_seen_context_does_not_take_the_shed_exit(self) -> None:
        rows = [_session_row(float(age), "y" * 900, seen=True) for age in range(2, 14)]
        rows.append(_session_row(1.0, "WORKING"))
        verdict = asyncio.run(point.judge_tick("watch", evidence=rows, dropped=0))
        assert "could not send every evidence item" not in verdict.body


class TestJudgeTickFallsOpen:
    """Every failure path reaches FALLBACK, which fires the loop."""

    def test_a_partial_scrub_shed_falls_back_rather_than_asking_a_half_question(self) -> None:
        """The surviving summaries are exactly the evidence that answers "nothing to do".

        A scrub that takes one comment body leaves the pinned board and state rows
        standing, so the delta is non-empty and the tick would put its question to a
        judge with the actionable half missing. The judge can then answer QUIET on
        evidence that never included the request.
        """
        trace: dict[str, Any] = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch",
                evidence=[
                    {
                        "source": "pr:acme/widgets#1",
                        "kind": point.KIND_PR_CHECKS,
                        "age_s": 1.0,
                        "text": "failed 1 (CI / Lint) passed 40",
                    },
                    {
                        "source": "pr:acme/widgets#1",
                        "kind": point.KIND_PR_COMMENT,
                        "age_s": 2.0,
                        "text": "please rotate AKIA" + "D" * 16,
                    },
                ],
                trace=trace,
            )
        )
        assert (
            verdict.outcome is Outcome.FALLBACK
        ), "something got through, so the total-shed branch cannot catch this"
        assert "every evidence item" in verdict.body
        assert trace["evidence_items"] == 1, "and the count reports only what could be sent"

    def test_a_fallback_publishes_the_screened_count(self) -> None:
        """A scrubbed-away tick reports zero items, not the input's length.

        The caller renders the notice and the verdict record from this count, so
        an unfilled trace would leave it reading the list it passed in -- which
        holds the rows the scrub rejected.
        """
        trace: dict[str, Any] = {}
        verdict = asyncio.run(
            point.judge_tick(
                "watch",
                evidence=[
                    {
                        "source": "s",
                        "kind": point.KIND_PR_CHECKS,
                        "age_s": 1.0,
                        "text": "AKIA" + "C" * 16,
                    }
                ],
                trace=trace,
            )
        )
        assert verdict.outcome is Outcome.FALLBACK
        assert "no evidence it could send" in verdict.body
        assert trace["evidence_items"] == 0
        assert trace["answers"] is None

    def test_refused_decision_is_fallback(self) -> None:
        async def refuse(*args: Any, **kwargs: Any) -> None:
            return None

        evidence = [
            {"source": "s", "kind": point.KIND_PR_CHECKS, "age_s": 1.0, "text": "checks pending"}
        ]
        with patch("kiro_crew.decisions.decide", refuse):
            verdict = asyncio.run(point.judge_tick("watch", evidence=evidence))
        assert verdict.outcome is Outcome.FALLBACK

    def test_raising_provider_is_fallback(self) -> None:
        async def boom(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("provider exploded")

        evidence = [
            {"source": "s", "kind": point.KIND_PR_CHECKS, "age_s": 1.0, "text": "checks pending"}
        ]
        with patch("kiro_crew.decisions.decide", boom):
            verdict = asyncio.run(point.judge_tick("watch", evidence=evidence))
        assert verdict.outcome is Outcome.FALLBACK
