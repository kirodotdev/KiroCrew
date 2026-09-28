"""``looks_like_plan`` must still recognise the plans users actually get.

The pre-filter was tightened to stop it buying a 2-8s LLM rephrase on numbered
prose that is not a plan. Three of those tightenings cut into real plans, and a
plan the filter misses is not rephrased, not validated, and never becomes a
staged run at all -- so the cost of a miss is the whole feature, while the cost
of a false positive is one background call that answers ``NOT_A_PLAN``.

What each test here pins:

* **Case** -- ``1. **setup the repo**`` is as common as ``1. **Setup the
  repo**``. The pattern pair lost the ``re.IGNORECASE`` its single-alternation
  predecessor carried, so every lowercase-led bold plan went unseen.
* **Position** -- the run is the longest one starting at 1 ANYWHERE in the text,
  not the run at its head. One stray ``Step 3:``-shaped sentence above the plan
  (a line about the code, a quoted log) zeroed the score of the plan below it.
* **Shape mixing** -- a model writes one plan in both shapes at once
  (``Stage 1: Survey`` then ``2. **Build**``). Scored per shape, neither half
  reaches the threshold while the whole plainly is a plan.

The negative cases the tightening was for are pinned in
``test_context_management.py`` and must keep passing: those are what stop this
from becoming "any numbered text is a plan".
"""

from __future__ import annotations

from kiro_crew.context_management import looks_like_plan


class TestCaseIsNotPartOfThePlanShape:
    def test_lowercase_bold_list_is_a_plan(self):
        """RED BEFORE: ``[A-Z]`` without IGNORECASE refused every lowercase plan."""
        text = (
            "1. **setup the repo**: clone and install\n"
            "2. **build it**: run the gate\n"
            "3. **verify**: read the log\n"
        )
        assert looks_like_plan(text) is True

    def test_mixed_case_bold_list_is_a_plan(self):
        """A model is not consistent inside one list; the filter must not care."""
        text = "1. **survey** the callers\n2. **Patch** the helper\n3. **verify** the gate"
        assert looks_like_plan(text) is True

    def test_lowercase_stage_lines_are_a_plan(self):
        """Preservation: the stage-line shape always carried the flag."""
        assert looks_like_plan("stage 1: survey\nstage 2: build") is True


class TestAStrayNumberAboveThePlanDoesNotHideIt:
    def test_stage_line_run_is_found_below_a_stray_number(self):
        """RED BEFORE: the scan broke at the first mismatch, scoring this 0."""
        text = (
            "I read the failure again -- Step 3: the parser is where it dies.\n\n"
            "📋 Plan for: fix the parser\n\n"
            "Stage 1: Reproduce\n  - run the failing case\n"
            "Stage 2: Fix\n  - patch the parser\n"
        )
        assert looks_like_plan(text) is True

    def test_bold_list_run_is_found_below_a_stray_number(self):
        """Same defect on the other shape: prose numbering above a real plan."""
        text = (
            "The old write-up had it as 7. **Ship** which never happened.\n\n"
            "1. **Survey** the callers\n2. **Patch** the helper\n3. **Verify** the gate\n"
        )
        assert looks_like_plan(text) is True

    def test_the_longest_run_wins_not_the_first(self):
        """A one-item run at the head must not settle the answer."""
        text = "1. **Context** for what follows\n\nfoo\n\nStage 1: Do it\nStage 2: Check it\n"
        assert looks_like_plan(text) is True

    def test_a_stray_number_alone_is_still_not_a_plan(self):
        """The loosening must not turn "there is a 1 somewhere" into a plan."""
        assert looks_like_plan("Step 3: the parser dies.\n\n1. **Ship** it and see.") is False


class TestAnOverlongNumberCannotCrashTheFilter:
    """The step number is captured with a length bound, and it has to be.

    ``int()`` refuses a string of more than 4300 digits -- CPython's
    integer-string conversion limit -- and this filter runs on every assistant
    turn over text the model wrote. An unbounded capture therefore let a line
    numbered with 4301+ digits raise out of ``looks_like_plan``, turning a
    response that had completed into an error turn.

    The bound is the fix rather than a try/except, because a 4301-digit step
    number is not a plan step under any reading: refusing to match it is the
    honest answer, and it costs the conversion nothing.
    """

    #: Assembled at runtime. A literal of this size in the source would bloat the
    #: file and tell a reader less than the construction does.
    _OVERLONG = "1" + "0" * 5000

    def test_a_bold_item_numbered_with_5000_digits_does_not_raise(self):
        """RED BEFORE: ValueError out of looks_like_plan, mid-turn."""
        text = f"{self._OVERLONG}. **Boom**\n2. **Next**\n3. **Third**\n"
        assert looks_like_plan(text) is False

    def test_a_stage_line_numbered_with_5000_digits_does_not_raise(self):
        """Both patterns capture a number, so both need the bound."""
        text = f"Step {self._OVERLONG}:\nStage 1: Survey\nStage 2: Build\n"
        # The real plan below the unparseable line is still found: the run scan
        # takes the longest run starting at 1, wherever it sits.
        assert looks_like_plan(text) is True

    def test_a_four_digit_number_still_parses(self):
        """The bound must not refuse a number a real plan could carry."""
        assert looks_like_plan("Stage 1000: A\nStage 1001: B") is False
        assert looks_like_plan("Stage 1: A\nStage 2: B") is True


class TestSubstepsDoNotBreakTheStageRun:
    """A plan numbers its substeps under its stages, and both count from 1.

    ``Stage 1: Setup`` / ``1. **Install**`` / ``2. **Configure**`` /
    ``Stage 2: Build`` is an ordinary informal plan. Read as ONE merged sequence
    the substeps carry the count to 3, so ``Stage 2`` fails to continue its own
    run and the whole scores 1 -- a plan written plainly as a plan, refused. The
    stage shape therefore keeps a reading of its own, where substep numbering
    cannot reach it, and the bold shape keeps one for the mirror case.
    """

    def test_a_stage_plan_with_bold_substeps_is_a_plan(self):
        """RED BEFORE: the merged run scored this 1 and the plan degraded to chat."""
        text = (
            "Stage 1: Setup\n"
            "1. **Install** the deps\n"
            "2. **Configure** the env\n"
            "Stage 2: Build\n"
        )
        assert looks_like_plan(text) is True

    def test_deep_substeps_under_several_stages_are_a_plan(self):
        """The same shape at the length a real plan actually runs to."""
        text = (
            "Stage 1: Survey\n"
            "1. **Read** the callers\n"
            "2. **List** the sites\n"
            "Stage 2: Patch\n"
            "1. **Edit** the helper\n"
            "2. **Run** the gate\n"
            "Stage 3: Verify\n"
        )
        assert looks_like_plan(text) is True

    def test_stage_vocabulary_does_not_break_a_bold_run(self):
        """The mirror: a stage line among bold items must not zero their run."""
        text = (
            "1. **Survey** the callers\n"
            "Step 9: (an aside about the old process)\n"
            "2. **Patch** the helper\n"
            "3. **Verify** the gate\n"
        )
        assert looks_like_plan(text) is True

    def test_substeps_alone_under_one_stage_are_not_enough(self):
        """The boundary: one stage line plus its own substeps is a run of one.

        A single stage is not a plan, and the bold pair under it is the
        two-option shape that has never counted. What makes the cases above
        plans is a SECOND stage.
        """
        assert looks_like_plan("Stage 1: Setup\n1. **Install**\n2. **Configure**") is False


class TestBothShapesFeedOneRun:
    def test_stage_line_then_bold_item_is_a_two_stage_plan(self):
        """RED BEFORE: one of each scored 1 and 1, so neither shape qualified."""
        text = "Stage 1: Survey the callers\n2. **Patch** the helper\n"
        assert looks_like_plan(text) is True

    def test_bold_item_then_stage_line_is_a_two_stage_plan(self):
        """Order of the shapes is not the signal; the numbering is."""
        text = "1. **Survey** the callers\nStage 2: Patch the helper\n"
        assert looks_like_plan(text) is True

    def test_a_bold_only_run_of_two_is_still_refused(self):
        """The boundary of the summing: a bold-only run still needs three.

        Mixing the shapes is what buys the shorter threshold, and the reason is
        the stage line: it names a stage explicitly, which is the vocabulary a
        bold-only list has none of. Two bold items is the commonest shape of
        ordinary prose, so this one costs nothing and is meant to.
        ``docs/system-specs/modules/autopilot.md`` records both thresholds.
        """
        text = "1. **Implement** the fix\n2. **Verify** with the gate\n"
        assert looks_like_plan(text) is False

    def test_two_shapes_repeating_one_number_is_not_a_run(self):
        """Mixing shapes must not let the same number count twice."""
        assert looks_like_plan("Stage 1: Survey\n1. **Survey** the callers") is False
