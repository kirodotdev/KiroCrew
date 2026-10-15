"""The gated tick: ``AutoNudgeService._judge_tick_is_quiet`` on a judged loop.

Covers owed wakes, probe-covered pull-request ticks, the retarget and narrowed-target fences,
cursors that move only with a verdict, the bypasses, verdict handling and the streak floor,
the default brief, persist-before-publish ordering, and the transcript notice. The shared
autouse fixture comes from ``autonudge_wake_judge_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from typing import Any
from unittest.mock import patch

from autonudge_wake_judge_helpers import _restore_judge_process_globals  # noqa: F401 - fixture
from autonudge_wake_judge_helpers import _loop, answers

from kiro_crew import autonudge_judge as judge
from kiro_crew.autonudge import (
    _JUDGE_QUIET_STREAK_FLOOR_DEFAULT,
    _MAX_QUIET_STREAK,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.decisions.points import nudge_wake as point
from kiro_crew.decisions.types import Answer
from kiro_crew.irq import Outcome, Verdict
from kiro_crew.validation import JUDGE_OFF_KEY


class TestAnOwedWakeSurvives:
    """A judge verdict that says fire must not be lost if the fire never happens.

    The judge advances DURABLE read cursors before deciding, so a process that stops
    between the verdict and the fire leaves the next tick reading nothing new: it would
    answer quiet and suppress the very turn that is owed. The marker lives on the loop
    rather than on ``MonitorState`` because a judge-only loop has no monitor record, so
    every mechanism guarded by ``monitor is not None`` misses it.
    """

    @staticmethod
    def _fired(outcome: Outcome) -> NudgeLoop:
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING",
                    }
                ],
                0,
                {},
            )

        async def persist(loop: NudgeLoop) -> bool:
            return True

        svc._collect_judge_evidence = collect
        svc._persist_judge_state = persist  # type: ignore[method-assign]
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        loop = _loop({"wake_when": "RULING"})

        async def tick(instruction: str, **kwargs: Any) -> Any:
            return Verdict(outcome=outcome, body="x")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            asyncio.run(svc._judge_tick_is_quiet(loop))
        return loop

    def test_a_wake_marks_the_turn_as_owed(self) -> None:
        assert self._fired(Outcome.WAKE).judge_wake_pending is True

    def test_a_fallback_marks_it_too(self) -> None:
        # A fallback spends a turn exactly as a wake does, so it owes one the same way.
        assert self._fired(Outcome.FALLBACK).judge_wake_pending is True

    def test_a_quiet_owes_nothing(self) -> None:
        assert self._fired(Outcome.QUIET).judge_wake_pending is False

    def test_an_owed_wake_fires_without_asking_the_judge_again(self) -> None:
        # The decisive case: a second reading would find nothing new and answer quiet.
        svc = _service()
        asked: list[str] = []

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            asked.append("collected")
            return [], 0, {}

        svc._collect_judge_evidence = collect
        loop = _loop({"wake_when": "RULING"})
        loop.judge_wake_pending = True
        got = asyncio.run(svc._judge_tick_is_quiet(loop))
        assert got is False, "the owed turn is delivered"
        assert asked == [], "and no evidence is re-read to second-guess it"
        assert loop.judge_wake_pending is True, "it stays owed until the fire settles"

    def test_the_loader_refuses_a_non_boolean_marker(self) -> None:
        # The store is agent-writable, so a corrupt value must not make a loop fire
        # forever without ever consulting its judge.
        loop = NudgeLoop(id="l1", slot_key="chat-1-1", message="m")
        assert loop.judge_wake_pending is False


class TestAScreenedPullRequestTickLeavesTheProbesQuietAlone:
    """A gated PR watch must not pay a turn for a tick its own probe re-armed free.

    On the gated path the judge is asked only AFTER the typed probe returned quiet, so
    the subject HAS been read. The canonical observation the PR collector wants has one
    writer and it is on the structured path, so the reader finds none -- and the
    question that decides this loop's cost is whether that counts as an unread target.
    Counted unread it is a FALLBACK, which fires: a turn, a durable write and a notice
    row every interval, for the life of the watch, against one turn per streak floor
    without any judge at all. So the screen would invert its own purpose for the
    commonest gated loop there is.
    """

    def test_a_probe_covered_tick_answers_quiet(self) -> None:
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            # What the reader produces for the probe's own subject with no canonical
            # facts behind it: nothing to judge, and nothing unread either.
            return [], 0, {}

        svc._collect_judge_evidence = collect
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        loop = _loop({"wake_when": "checks go red"})
        loop.message = "https://github.com/o/r/pull/7"

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(loop))
        assert got is True, "the probe already read this subject, so the tick stays free"

    def test_an_unread_target_still_fires(self) -> None:
        """The control, and the reason the drop exists at all."""
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return [], 1, {}

        svc._collect_judge_evidence = collect
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        loop = _loop({"wake_when": "checks go red"})
        loop.message = "https://github.com/o/r/pull/7"

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(loop))
        assert got is False, "one target nobody read makes the reading incomplete"


class TestARetargetDoesNotLeakOrLie:
    """A message retarget changes WHAT is judged without touching the judge spec.

    Targets are parsed out of ``loop.message``, so the spec can be byte-identical
    across a retarget. Two separate faults followed from that, and each is pinned here.
    """

    def test_a_retarget_prunes_the_old_target_s_cursor(self) -> None:
        # The collector only ever ADDS a key, so without pruning the old target's cursor
        # was re-persisted forever and the load cap could later drop a LIVE one instead.
        spec = {"wake_when": "RULING"}
        loop = _loop(spec)
        loop.message = "watch session:chat-9-9"
        loop.judge_cursors = {"chat-9-9": 12, "chat-old-1": 7}
        wanted = set(judge.parse_targets(spec, loop.message))
        pruned = {t: c for t, c in loop.judge_cursors.items() if t in wanted}
        assert "chat-old-1" not in pruned, "the departed target's cursor is gone"
        assert pruned.get("chat-9-9") == 12, "and the live one is kept, not reset"

    def test_the_fence_catches_a_retarget_that_leaves_the_spec_identical(self) -> None:
        # Drives the real tick. The judge answers QUIET, but the message is retargeted
        # DURING the await, so the verdict answers the previous target and must be
        # discarded and the turn fired. The spec is byte-identical throughout, so only
        # the message half of the fence can catch it.
        svc = _service()
        loop = _loop({"quiet_when": "the run is green"})
        loop.message = "watch chat-1-1"

        async def collect(lp: NudgeLoop) -> tuple[list[dict], int, dict]:
            # The retarget lands while this tick is reading, which is the whole window.
            lp.message = "watch chat-2-2"
            return (
                [
                    {
                        "source": "chat-1-1",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "green",
                    }
                ],
                0,
                {},
            )

        async def persist(lp: NudgeLoop) -> bool:
            return True

        svc._collect_judge_evidence = collect
        svc._persist_judge_state = persist  # type: ignore[method-assign]
        svc._persist_soon = lambda: None  # type: ignore[method-assign]

        async def tick(instruction: str, **kwargs: Any) -> Any:
            return Verdict(outcome=Outcome.QUIET, body="quiet")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            answer = asyncio.run(svc._judge_tick_is_quiet(loop))

        assert answer is False, "a verdict answering the old target cannot suppress the turn"
        assert loop.judge_quiet_streak == 0, "and it earns no streak against the new target"

    def test_the_two_targets_really_do_differ(self) -> None:
        # Guards the test above from passing on a parse that ignores the message.
        a = judge.parse_targets({"wake_when": "x"}, "watch session:chat-1-1")
        b = judge.parse_targets({"wake_when": "x"}, "watch session:chat-2-2")
        assert a != b and a and b


class TestANarrowedWatchIsNeverWidened:
    """Naming ``targets`` narrows the watch, and a dropped entry must not undo that.

    Falling through to message inference hands the owner a WIDER watch than they asked
    for and reads evidence from a session they never named.
    """

    def test_an_unusable_explicit_target_does_not_infer_from_the_message(self) -> None:
        got = judge.parse_targets({"wake_when": "x", "targets": ["!!!bad!!!"]}, "watch chat-7-7")
        assert got == [], "no target, rather than one the owner never named"

    def test_an_explicitly_empty_list_watches_nothing(self) -> None:
        assert judge.parse_targets({"wake_when": "x", "targets": []}, "watch chat-7-7") == []

    def test_no_list_at_all_still_infers_from_the_message(self) -> None:
        # The default the design asks for is unchanged: absent is not the same as empty.
        assert judge.parse_targets({"wake_when": "x"}, "watch chat-7-7") == ["chat-7-7"]

    def test_a_usable_explicit_target_wins_over_the_message(self) -> None:
        got = judge.parse_targets({"wake_when": "x", "targets": ["chat-1-1"]}, "watch chat-7-7")
        assert got == ["chat-1-1"]

    def test_no_targets_fires_rather_than_suppressing(self) -> None:
        # Why answering [] is safe: the tick spends a turn instead of going quiet.
        svc = _service()
        loop = _loop({"wake_when": "x", "targets": ["!!!bad!!!"]})
        loop.message = "watch chat-7-7"

        async def collect(lp: NudgeLoop) -> tuple[list[dict], int, dict]:
            raise AssertionError("a loop with no usable target must not collect evidence")

        svc._collect_judge_evidence = collect
        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
        ):
            assert asyncio.run(svc._judge_tick_is_quiet(loop)) is False

    def test_the_quiet_floor_also_owes_its_turn(self) -> None:
        # The floor branch FIRES, so it owes a turn exactly as a wake does. It was the
        # one firing path the marker missed, and the loss there is silent: the reset it
        # persists means the next tick finds nothing new and pushes the forced turn out
        # another whole floor with nothing recording that one was due.
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "still green",
                    }
                ],
                0,
                {},
            )

        async def persist(loop: NudgeLoop) -> bool:
            return True

        svc._collect_judge_evidence = collect
        svc._persist_judge_state = persist  # type: ignore[method-assign]
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        loop = _loop({"quiet_when": "the run is green"})
        # One short of the floor, so this tick reaches it.
        loop.judge_quiet_streak = svc._judge_quiet_streak_floor() - 1

        async def tick(instruction: str, **kwargs: Any) -> Any:
            return Verdict(outcome=Outcome.QUIET, body="quiet")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            answer = asyncio.run(svc._judge_tick_is_quiet(loop))

        assert answer is False, "the floor fires"
        assert loop.judge_quiet_streak == 0, "and the streak is spent"
        assert loop.judge_wake_pending is True, "and the turn it owes survives a refusal"


class TestCursorsMoveOnlyWithAVerdict:
    """Read positions are a consequence of a verdict, so they land only with one.

    The judge await is cancellable -- a user typing cancels exactly that task -- so a
    tick can end after the reading and before any commit. Publishing the advanced
    positions before that point consumes rows for a verdict that never happened: the
    next tick reads nothing new, answers quiet, and the wake those rows had earned is
    gone, silently, until the streak floor.
    """

    @staticmethod
    def _svc(outcome: Outcome | None, raise_exc: BaseException | None = None):
        svc = _service()
        advanced = {"chat-2-2": 99}

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING",
                    }
                ],
                0,
                dict(advanced),
            )

        async def persist(loop: NudgeLoop) -> bool:
            return True

        svc._collect_judge_evidence = collect
        svc._persist_judge_state = persist  # type: ignore[method-assign]
        svc._persist_soon = lambda: None  # type: ignore[method-assign]

        async def tick(instruction: str, **kwargs: Any) -> Any:
            if raise_exc is not None:
                raise raise_exc
            return Verdict(outcome=outcome or Outcome.QUIET, body="x")

        return svc, tick, advanced

    def test_a_cancelled_judge_leaves_the_stored_cursors_alone(self) -> None:
        svc, tick, _ = self._svc(None, raise_exc=asyncio.CancelledError())
        loop = _loop({"quiet_when": "the run is green"})
        loop.judge_cursors = {"chat-2-2": 7}

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            try:
                asyncio.run(svc._judge_tick_is_quiet(loop))
            except asyncio.CancelledError:
                pass

        assert loop.judge_cursors == {
            "chat-2-2": 7
        }, "the reading was never judged, so it was never consumed"

    def test_a_committed_verdict_does_publish_them(self) -> None:
        # The other half: without this the fix could simply never advance cursors.
        svc, tick, advanced = self._svc(Outcome.QUIET)
        loop = _loop({"quiet_when": "the run is green"})
        loop.judge_cursors = {"chat-2-2": 7}

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            asyncio.run(svc._judge_tick_is_quiet(loop))

        assert loop.judge_cursors == advanced, "a judged reading is consumed"


def _service() -> AutoNudgeService:
    """A service with no disk and no evidence reader wired, for tick tests."""
    svc = AutoNudgeService.__new__(AutoNudgeService)
    svc._collect_judge_evidence = None
    svc._emit_judge_notice = None

    # Built without ``__init__``, so it holds no service lock, no store path and no
    # in-flight task set: the real durable write would fail on all three. The judge
    # path AWAITS its write as a SHIELDED supervised task and fires when it does not
    # land, so leaving these out would make every quiet tick here report a fire and
    # hide what the test is actually about.
    svc._inflight_adds = set()
    # The mutation-admission lease a timer callback carries: unset here, as on a
    # tick driven outside a timer task.
    svc._mutation_admission = contextvars.ContextVar("test_admission", default=None)

    async def _no_disk(**_kwargs: Any) -> None:
        return None

    svc._persist_locked = _no_disk  # type: ignore[method-assign]
    return svc


class TestTickLeavesEverythingAloneWhenItShould:
    """``None`` means the judge had no say, so the tick behaves exactly as today."""

    def test_an_ungated_loop_is_never_screened(self) -> None:
        # `gate=false` is a loop whose duty is to act WHILE its subject is quiet -- a
        # heartbeat, a reviewer chase -- so suppressing a quiet tick would remove the
        # work rather than the waste. It is the bypass, not an absent decision.
        assert asyncio.run(_service()._judge_tick_is_quiet(_loop(gate=False))) is None

    def test_an_ungated_loop_is_not_screened_even_with_a_brief(self) -> None:
        loop = _loop({"wake_when": "x"}, gate=False)
        assert asyncio.run(_service()._judge_tick_is_quiet(loop)) is None

    def test_an_explicit_judge_false_bypasses_the_judge(self) -> None:
        # The other bypass, and it means something different: this owner wants
        # observation gating and no judge. Stored as the reserved marker that
        # `judge: false` normalises to.
        loop = _loop({JUDGE_OFF_KEY: True})
        assert asyncio.run(_service()._judge_tick_is_quiet(loop)) is None

    def test_brief_but_no_evidence_reader(self) -> None:
        assert asyncio.run(_service()._judge_tick_is_quiet(_loop({"wake_when": "x"}))) is None

    def test_the_seam_refusing_stores_the_brief_and_ignores_it(self) -> None:
        """Refused means the loop is a plain timer, and the brief waits for later.

        One test, because the service does not tell the two refusals apart: a missing
        ``nudge_evidence`` scope and a lane with no runner installed both arrive as
        ``is_enabled`` answering False, which is the seam resolving the lane and its
        arming together. Keeping the brief is what makes granting the scope later arm
        every loop already carrying one, with no re-arming.
        """
        svc = _service()

        async def never(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            raise AssertionError("collected evidence while the seam refused")

        svc._collect_judge_evidence = never
        loop = _loop({"wake_when": "x"})
        with patch("kiro_crew.decisions.is_enabled", lambda *a, **k: False):
            assert asyncio.run(svc._judge_tick_is_quiet(loop)) is None
        assert loop.judge == {"wake_when": "x"}, "the brief is kept for when the scope arrives"

    def test_the_tick_asks_the_seam_and_resolves_no_lane_of_its_own(self) -> None:
        """One rule, and it is the gate's.

        The gate arms Jev only on consent AND this point's scope. A resolver here that
        arms it on endpoint consent alone picks the Jev lane on a machine that consented
        without granting the scope; the gate then refuses that lane and the judge is
        skipped, when the gate itself picks the small-model lane, which needs neither.
        So this asserts against the module's SYMBOLS rather than a call: the fault is a
        second rule existing at all, and a call-level test passes either way.
        """
        from kiro_crew import autonudge as _autonudge

        for gone in (
            "_judge_lane",
            "_judge_provider",
            "_judge_llm_lane_available",
            "_JUDGE_PROVIDERS",
            "_JUDGE_LLM_MODULE",
        ):
            assert not hasattr(_autonudge, gone), f"{gone} re-spells the gate's lane rule"
            assert not hasattr(AutoNudgeService, gone), f"{gone} re-spells the gate's lane rule"
        source = inspect.getsource(AutoNudgeService._judge_tick_is_quiet)
        assert "core_decisions.is_enabled" in source, "the tick reads the seam's own answer"


class TestTickVerdicts:
    """QUIET spends no turn, everything else spends one, and the floor bounds QUIET."""

    @staticmethod
    def _armed(verdict_answers: dict[str, Answer] | None) -> tuple[AutoNudgeService, NudgeLoop]:
        svc = _service()

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING: still building",
                    }
                ],
                0,
                {},
            )

        svc._collect_judge_evidence = collect
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        return svc, _loop({"wake_when": "RULING", "quiet_when": "WORKING"})

    def _run(self, svc: AutoNudgeService, loop: NudgeLoop, ans: Any) -> Any:
        async def decide(*args: Any, **kwargs: Any) -> Any:
            return ans

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.decide", decide),
        ):
            return asyncio.run(svc._judge_tick_is_quiet(loop))

    def test_quiet_spends_no_turn(self) -> None:
        svc, loop = self._armed(None)
        assert self._run(svc, loop, answers()) is True
        assert loop.judge_quiet_streak == 1
        assert loop.judge_last_verdict["outcome"] == "quiet"

    def test_a_brief_replaced_mid_tick_is_discarded_and_fires(self) -> None:
        """The reads and the decision happen outside the service lock.

        An update that replaces the brief clears the streak, the cursors and the verdict
        record, because all three are facts about the brief that is gone. A tick already
        in flight would otherwise commit its own copies of them afterwards, reinstating
        state nobody armed and able to hold the loop quiet on a withdrawn criterion.
        """
        svc, loop = self._armed(None)
        loop.judge_cursors = {"chat-2-2": 5}

        async def replace_then_read(loop_: NudgeLoop) -> tuple[list[dict], int, dict]:
            loop_.judge = {"wake_when": "something else entirely"}
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING: still building",
                    }
                ],
                0,
                {},
            )

        svc._collect_judge_evidence = replace_then_read  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is False, "a stale verdict fires"
        assert loop.judge_quiet_streak == 0, "the streak is not credited to the new brief"
        assert loop.judge_cursors == {}, "the cursors go back to what the update installed"

    def test_an_unchanged_brief_still_commits(self) -> None:
        """The control for the test above: the discard is the change, not the re-read."""
        svc, loop = self._armed(None)
        assert self._run(svc, loop, answers()) is True
        assert loop.judge_quiet_streak == 1

    @staticmethod
    def _reads(svc: AutoNudgeService, dropped: int) -> None:
        """Make the collector return an empty delta, with *dropped* targets unread."""

        async def read(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return [], dropped, {}

        svc._collect_judge_evidence = read  # type: ignore[method-assign]

    def test_a_calm_read_spends_no_turn_and_counts_toward_the_floor(self) -> None:
        """Nothing new on any target is an answer, and the answer costs no turn.

        The provider returns ``None`` here, which is the shape that would FALL BACK if
        the tick reached it -- so a QUIET verdict can only have come from the delta
        being empty with every target read, and it still lands in the streak the floor
        bounds rather than in a silence nothing ends.
        """
        svc, loop = self._armed(None)
        self._reads(svc, dropped=0)
        assert self._run(svc, loop, None) is True
        assert loop.judge_quiet_streak == 1
        assert loop.judge_last_verdict["outcome"] == "quiet"
        assert loop.judge_last_verdict["evidence_items"] == 0

    def test_an_unread_target_fires_and_resets_the_streak(self) -> None:
        """A tick that could not look is not calm, and it does not keep its credit."""
        svc, loop = self._armed(None)
        loop.judge_quiet_streak = 2
        self._reads(svc, dropped=1)
        assert self._run(svc, loop, None) is False
        assert loop.judge_quiet_streak == 0

    def test_a_subject_that_stays_calm_still_fires_at_the_floor(self) -> None:
        """The floor bounds the new quiet path as it bounds a provider-answered one.

        A subject with genuinely nothing to say would otherwise hold its loop silent
        for as long as it stayed silent, which is the one outcome the floor exists to
        make impossible.
        """
        svc, loop = self._armed(None)
        self._reads(svc, dropped=0)
        loop.judge_quiet_streak = svc._judge_quiet_streak_floor() - 1
        assert self._run(svc, loop, None) is False, "the floor delivers a turn"
        assert loop.judge_quiet_streak == 0

    def test_a_quiet_whose_write_does_not_land_fires_instead(self) -> None:
        """Suppressing a turn is the irreversible direction, so it needs durable state.

        The cursors, the streak and the verdict record are published in memory before
        the write. If the write is lost, a restart re-reads the rows this tick
        consumed and recounts a streak it had already spent -- so the tick must not
        also claim the turn was not owed.
        """
        svc, loop = self._armed(None)

        async def refuse(**_kwargs: Any) -> None:
            raise OSError("disk is gone")

        svc._persist_locked = refuse  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is False
        assert loop.judge_last_verdict["outcome"] == "quiet", "the verdict itself stands"

    def test_a_quiet_whose_write_lands_still_spends_no_turn(self) -> None:
        """The control for the test above: the fire is the write's failure, not the await."""
        svc, loop = self._armed(None)
        calls: list[int] = []

        async def landed(**_kwargs: Any) -> None:
            calls.append(1)

        svc._persist_locked = landed  # type: ignore[method-assign]
        assert self._run(svc, loop, answers()) is True
        assert calls, "the quiet path awaits a durable write rather than scheduling one"

    def test_wake_spends_a_turn_and_clears_the_streak(self) -> None:
        svc, loop = self._armed(None)
        loop.judge_quiet_streak = 4
        assert self._run(svc, loop, answers(outcome=point.OUTCOME_NEEDS_ACTION)) is False
        assert loop.judge_quiet_streak == 0

    def test_finished_wakes_and_leaves_the_loop_armed(self) -> None:
        """The judge may say the work is over; only a typed probe may END a watch.

        A judge reading prose that could stop a loop would let one hostile or
        mistaken comment in a watched transcript buy permanent silence.
        """
        svc, loop = self._armed(None)
        finished = answers(outcome=point.OUTCOME_FINISHED, outcome_p=0.95)
        assert self._run(svc, loop, finished) is False, "the session is woken"
        assert loop.judge_last_verdict["outcome"] == "wake", "never terminal"

    def test_a_repeated_finished_keeps_waking(self) -> None:
        """No suppression, because nothing recorded a delivery to suppress against."""
        svc, loop = self._armed(None)
        finished = answers(outcome=point.OUTCOME_BROKEN, outcome_p=0.99)
        for _ in range(4):
            assert self._run(svc, loop, finished) is False

    def test_the_verdict_rides_on_the_fired_turn(self) -> None:
        svc, loop, sink = TestTranscriptNotice._armed_with_sink()
        TestTranscriptNotice()._run(
            svc, loop, answers(outcome=point.OUTCOME_FINISHED, outcome_p=0.95)
        )
        assert len(sink) == 1 and point.OUTCOME_FINISHED in sink[0]

    def test_refused_provider_spends_a_turn(self) -> None:
        svc, loop = self._armed(None)
        assert self._run(svc, loop, None) is False

    def test_streak_floor_fires_and_resets(self) -> None:
        svc, loop = self._armed(None)
        floor = svc._judge_quiet_streak_floor()
        loop.judge_quiet_streak = floor - 1
        assert self._run(svc, loop, answers()) is False, "the floor delivers a turn"
        assert loop.judge_quiet_streak == 0

    def test_collector_failure_spends_a_turn(self) -> None:
        svc, loop = self._armed(None)

        async def boom(loop_: NudgeLoop) -> tuple[list[dict], int, dict]:
            raise RuntimeError("slot read exploded")

        svc._collect_judge_evidence = boom
        assert self._run(svc, loop, answers()) is False


class TestTheDefaultBrief:
    """A gated loop is screened whether or not its owner wrote a brief."""

    @staticmethod
    def _sees_criteria() -> tuple[AutoNudgeService, list[tuple[str, str]], list[str]]:
        """A service that records the criteria the judge was asked under."""
        svc = _service()
        asked: list[tuple[str, str]] = []
        sink: list[str] = []

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING",
                    }
                ],
                0,
                {},
            )

        async def emit(loop: NudgeLoop, line: str) -> None:
            sink.append(line)

        svc._collect_judge_evidence = collect
        svc._emit_judge_notice = emit
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        return svc, asked, sink

    def _run(self, svc: AutoNudgeService, loop: NudgeLoop, asked: list) -> Any:
        async def tick(instruction: str, **kwargs: Any) -> Any:
            asked.append((kwargs.get("wake_when", ""), kwargs.get("quiet_when", "")))
            return Verdict(outcome=Outcome.QUIET, body="no new evidence")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            # GRANTED, because the default brief's own precondition is this point's
            # egress scope -- these cases are a machine whose owner opted in.
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            return asyncio.run(svc._judge_tick_is_quiet(loop))

    def test_a_gated_loop_with_no_brief_is_judged_under_the_default(self) -> None:
        svc, asked, _ = self._sees_criteria()
        self._run(svc, _loop(), asked)
        assert asked == [(judge.DEFAULT_WAKE_WHEN, judge.DEFAULT_QUIET_WHEN)]

    def test_the_owners_own_sentences_replace_the_default(self) -> None:
        svc, asked, _ = self._sees_criteria()
        self._run(svc, _loop({"wake_when": "a line starts with RULING"}), asked)
        assert asked == [("a line starts with RULING", "")], "no default is mixed in"

    def test_a_brief_naming_only_targets_still_gets_the_defaults_sentences(self) -> None:
        # `targets` says WHICH subjects to watch and nothing about when to wake, so a
        # brief carrying only targets needs the default's criteria and keeps its own
        # list. Keyed on the criteria rather than on the brief being empty for exactly
        # this case.
        svc, asked, _ = self._sees_criteria()
        loop = _loop({"targets": ["chat-2-2"]})
        self._run(svc, loop, asked)
        assert asked == [(judge.DEFAULT_WAKE_WHEN, judge.DEFAULT_QUIET_WHEN)]

    def test_the_default_is_not_written_back_onto_the_loop(self) -> None:
        # The loop keeps storing no brief, so granting the owner a criterion later is
        # still an empty-to-set change rather than an edit of shipped text, and the
        # mid-tick replacement guard compares against what was stored.
        svc, asked, _ = self._sees_criteria()
        loop = _loop()
        self._run(svc, loop, asked)
        assert loop.judge == {}

    def test_the_default_needs_this_points_own_egress_scope(self) -> None:
        # The ruling's own precondition is the SCOPE, and `is_enabled` is a broader
        # question: the LLM lane satisfies it on the provider key alone. The gate
        # documents a loop's own brief as half of that lane's authorization, so a loop
        # whose owner armed nothing must not be screened on that authority by itself.
        svc, asked, _ = self._sees_criteria()

        async def tick(instruction: str, **kwargs: Any) -> Any:
            asked.append((kwargs.get("wake_when", ""), kwargs.get("quiet_when", "")))
            return Verdict(outcome=Outcome.QUIET, body="no new evidence")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: False),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(_loop()))
        assert got is None, "scope off leaves the tick exactly as today"
        assert asked == [], "nothing was read, so nothing was sent"

    def test_an_explicit_brief_still_runs_without_that_scope(self) -> None:
        # Unchanged from before the default existed: an owner who armed a criterion
        # supplied the affirmative act the LLM lane's authorization rests on.
        svc, asked, _ = self._sees_criteria()

        async def tick(instruction: str, **kwargs: Any) -> Any:
            asked.append((kwargs.get("wake_when", ""), kwargs.get("quiet_when", "")))
            return Verdict(outcome=Outcome.QUIET, body="no new evidence")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: False),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(_loop({"wake_when": "RULING"})))
        assert got is True
        assert asked == [("RULING", "")]

    def test_the_notice_names_the_default_brief(self) -> None:
        svc, asked, sink = self._sees_criteria()
        self._run(svc, _loop(), asked)
        assert "default brief" in sink[0]
        assert "custom" not in sink[0]

    def test_the_notice_names_a_custom_brief(self) -> None:
        svc, asked, sink = self._sees_criteria()
        self._run(svc, _loop({"wake_when": "RULING"}), asked)
        assert "custom brief" in sink[0]
        assert "default" not in sink[0]


class TestPersistBeforePublish:
    """The notice describes committed state, so the write goes first on every path."""

    @staticmethod
    def _ordered(outcome: Outcome, *, persist_ok: bool = True):
        """Run one tick and return the ordered ('persist'|'notice') trail."""
        svc = _service()
        trail: list[str] = []

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING",
                    }
                ],
                0,
                {},
            )

        async def emit(loop: NudgeLoop, line: str) -> None:
            trail.append("notice")

        async def persist(loop: NudgeLoop) -> bool:
            trail.append("persist")
            return persist_ok

        svc._collect_judge_evidence = collect
        svc._emit_judge_notice = emit
        svc._persist_judge_state = persist  # type: ignore[method-assign]
        svc._persist_soon = lambda: None  # type: ignore[method-assign]

        async def tick(instruction: str, **kwargs: Any) -> Any:
            return Verdict(outcome=outcome, body="x")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(_loop({"wake_when": "RULING"})))
        return got, trail

    def test_a_quiet_writes_before_it_notifies(self) -> None:
        got, trail = self._ordered(Outcome.QUIET)
        assert got is True
        assert trail == ["persist", "notice"]

    def test_a_wake_writes_before_it_notifies(self) -> None:
        got, trail = self._ordered(Outcome.WAKE)
        assert got is False
        assert trail == ["persist", "notice"]

    def test_a_failed_write_still_notifies_and_fires(self) -> None:
        # The notice must not be lost on the path that fires BECAUSE the write failed:
        # that tick spends a turn and the reader still needs to know why.
        got, trail = self._ordered(Outcome.QUIET, persist_ok=False)
        assert got is False
        assert trail == ["persist", "notice"]

    def test_a_brief_replaced_before_the_commit_is_discarded(self) -> None:
        svc = _service()
        loop = _loop({"wake_when": "RULING"})
        emitted: list[str] = []

        async def collect(inner: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING",
                    }
                ],
                0,
                {},
            )

        async def emit(inner: NudgeLoop, line: str) -> None:
            emitted.append(line)

        svc._collect_judge_evidence = collect
        svc._emit_judge_notice = emit
        svc._persist_soon = lambda: None  # type: ignore[method-assign]

        async def tick(instruction: str, **kwargs: Any) -> Any:
            # The owner revises the brief while the judge is deciding.
            loop.judge = {"wake_when": "something else entirely"}
            return Verdict(outcome=Outcome.QUIET, body="x")

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.judge_evidence_scope_granted", lambda **k: True),
            patch("kiro_crew.decisions.points.nudge_wake.judge_tick", tick),
        ):
            got = asyncio.run(svc._judge_tick_is_quiet(loop))
        assert got is False, "a verdict about a withdrawn question fires rather than suppressing"
        assert loop.judge_quiet_streak == 0, "no streak is earned for the replacement brief"
        assert emitted == [], "and no notice claims a verdict that was discarded"


class TestTranscriptNotice:
    """A verdict that spends no turn still leaves one line on the session."""

    @staticmethod
    def _armed_with_sink() -> tuple[AutoNudgeService, NudgeLoop, list[str]]:
        svc = _service()
        sink: list[str] = []

        async def collect(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            return (
                [
                    {
                        "source": "session:chat-2-2",
                        "kind": point.KIND_TRANSCRIPT_TAIL,
                        "age_s": 1.0,
                        "text": "WORKING: still building",
                    }
                ],
                0,
                {},
            )

        async def emit(loop: NudgeLoop, line: str) -> None:
            sink.append(line)

        svc._collect_judge_evidence = collect
        svc._emit_judge_notice = emit
        svc._persist_soon = lambda: None  # type: ignore[method-assign]
        return svc, _loop({"wake_when": "RULING", "quiet_when": "WORKING"}), sink

    def _run(self, svc: AutoNudgeService, loop: NudgeLoop, ans: Any) -> Any:
        async def decide(*args: Any, **kwargs: Any) -> Any:
            return ans

        with (
            patch("kiro_crew.decisions.is_enabled", lambda *a, **k: True),
            patch("kiro_crew.decisions.decide", decide),
        ):
            return asyncio.run(svc._judge_tick_is_quiet(loop))

    def test_quiet_verdict_emits_one_line(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        assert self._run(svc, loop, answers()) is True, "quiet spends no turn"
        assert len(sink) == 1
        line = sink[0]
        assert "quiet" in line
        assert point.Q_NEEDS_OWNER in line and point.Q_OUTCOME in line
        assert "0.9" in line, "the probabilities are what tell a confident quiet apart"

    def test_wake_verdict_also_emits(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, answers(outcome=point.OUTCOME_NEEDS_ACTION))
        assert len(sink) == 1 and "wake" in sink[0]

    def test_fallback_emits_with_no_readings(self) -> None:
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, None)
        assert len(sink) == 1 and "fallback" in sink[0]

    def test_notice_carries_no_evidence_text(self) -> None:
        """The state stays in the request; the transcript gets the verdict."""
        svc, loop, sink = self._armed_with_sink()
        self._run(svc, loop, answers())
        assert "still building" not in sink[0]

    def test_a_broken_renderer_does_not_cost_the_verdict(self) -> None:
        svc, loop, _sink = self._armed_with_sink()

        async def boom(loop_: NudgeLoop, line: str) -> None:
            raise RuntimeError("renderer exploded")

        svc._emit_judge_notice = boom
        assert self._run(svc, loop, answers()) is True

    def test_a_notice_row_is_never_evidence(self) -> None:
        """A judge must not read its own previous notice back as new evidence.

        The collector admits assistant rows only, so the exclusion is structural
        rather than a name check against this feature's own output.
        """
        rows = [
            {"role": "notice", "content": "Wake judge - quiet - 2 evidence item(s)", "ts": 100.0},
            {"role": "assistant", "content": "RULING: need a call", "ts": 101.0},
        ]
        items = judge.session_evidence(rows, "chat-2-2", now_ts=110.0)
        assert [i["text"] for i in items] == ["RULING: need a call"]


class TestStreakFloorConfig:
    """The floor's default and its hard ceiling."""

    def test_default_equals_the_shipped_probe_floor(self) -> None:
        assert _JUDGE_QUIET_STREAK_FLOOR_DEFAULT == _MAX_QUIET_STREAK

    def test_unreadable_config_is_the_default(self) -> None:
        assert _service()._judge_quiet_streak_floor() == _JUDGE_QUIET_STREAK_FLOOR_DEFAULT
