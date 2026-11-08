"""Tests for the status reactions.

Two halves: Slack's ``StatusReactionController`` (one ack or error reaction, no
phase trail), and the channel-neutral phase ladder in ``messaging.status_reactions``
that the other channels drive through an injected emoji sink.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Generator

import pytest

from kiro_crew.messaging.status_reactions import (
    LadderTimings,
    PhaseReactionLadder,
    StallEmojis,
    format_turn_status,
    merge_phase_emojis,
    phase_for_tool_title,
)
from kiro_crew.messaging.status_reactions import tool_to_phase as shared_tool_to_phase
from kiro_crew.slack import handler as handler_mod
from kiro_crew.slack.handler import StatusReactionController
from kiro_crew.testing.wait import await_until

# ── FakeSlack helper ────────────────────────────────────────────────────


class FakeSlack:
    """Records add/remove reaction calls as (action, ts, emoji) tuples."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    async def add_reaction(self, channel: str, ts: str, emoji: str) -> None:
        self.calls.append(("add", ts, emoji))

    async def remove_reaction(self, channel: str, ts: str, emoji: str) -> None:
        self.calls.append(("remove", ts, emoji))


# ── Fake clock helper ──────────────────────────────────────────────────


class FakeClock:
    """Wraps the event loop to provide instant time advancement.

    Intercepts ``loop.call_later`` so that scheduled callbacks are tracked
    with their target fire-time.  ``advance(dt)`` moves the virtual clock
    forward and fires all callbacks whose deadline has been reached, then
    yields to the event loop so coroutines can process the results.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._now = loop.time()
        self._orig_call_later = loop.call_later
        self._orig_time = loop.time
        loop.time = self._time  # type: ignore[assignment]
        loop.call_later = self._call_later  # type: ignore[assignment]
        self._scheduled: list[tuple[float, asyncio.TimerHandle]] = []

    def _time(self) -> float:
        return self._now

    def _call_later(
        self, delay: float, callback: Any, *args: Any, **kw: Any
    ) -> asyncio.TimerHandle:
        handle = self._loop.call_at(self._now + delay, callback, *args, **kw)
        self._scheduled.append((self._now + delay, handle))
        return handle

    async def advance(self, seconds: float) -> None:
        """Advance the virtual clock by *seconds* and fire due callbacks."""
        target = self._now + seconds
        while True:
            due = [(t, h) for t, h in self._scheduled if t <= target and not h.cancelled()]
            if not due:
                break
            due.sort(key=lambda x: x[0])
            t, h = due[0]
            self._scheduled.remove((t, h))
            self._now = t
            h._run()
            # Cancel AFTER running: ``loop.time`` is frozen at the virtual now,
            # so the real loop would also see this handle as due and run the
            # callback a second time, which turns one scheduled fire into two.
            h.cancel()
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)

    @property
    def armed(self) -> int:
        """Scheduled callbacks still due to fire (a cancelled handle is not).

        Lets a test assert that a teardown left NO timer on the loop, which is
        not the same as asserting that no further edit reached the channel.
        """
        return sum(1 for _, handle in self._scheduled if not handle.cancelled())

    def restore(self) -> None:
        self._loop.call_later = self._orig_call_later  # type: ignore[assignment]
        self._loop.time = self._orig_time  # type: ignore[assignment]


@contextmanager
def fake_clock() -> Generator[FakeClock, None, None]:
    """Context manager that installs a FakeClock on the running loop."""
    loop = asyncio.get_running_loop()
    fc = FakeClock(loop)
    try:
        yield fc
    finally:
        fc.restore()


# ── Fixtures ────────────────────────────────────────────────────────────

_TS = "1234.5678"
_CH = "C123"

_DEBOUNCE = 0.1
_SOFT = 1.0
_HARD = 3.0


# ── Slack's StatusReactionController ────────────────────────────────────


class TestSlackStatusReaction:
    """Slack wears ONE reaction: the ``queued`` ack where no native thread status
    shows, removed at the reply (no ``done`` reaction), or ``error`` on failure."""

    @pytest.mark.asyncio
    async def test_ack_then_removed_with_nothing_in_its_place(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == [("add", _TS, "eyes"), ("remove", _TS, "eyes")]

    @pytest.mark.asyncio
    async def test_native_status_means_no_reaction(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=True)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_error_swaps_the_ack_for_the_error_reaction(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=True)
        await asyncio.sleep(0)
        assert slack.calls == [
            ("add", _TS, "eyes"),
            ("remove", _TS, "eyes"),
            ("add", _TS, "scream"),
        ]

    @pytest.mark.asyncio
    async def test_start_and_finalize_are_idempotent(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        ctrl.start(native_status=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=False)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == [("add", _TS, "eyes"), ("remove", _TS, "eyes")]

    @pytest.mark.asyncio
    async def test_an_error_after_a_clean_finalize_upgrades_once(self) -> None:
        """The reply failed to post after the turn ended: the error still shows."""
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=True)
        ctrl.finalize(error=True)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == [
            ("add", _TS, "eyes"),
            ("remove", _TS, "eyes"),
            ("add", _TS, "scream"),
        ]

    @pytest.mark.asyncio
    async def test_disabled_never_calls_slack(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
        ctrl.start(native_status=False)
        ctrl.finalize(error=True)
        await asyncio.sleep(0)
        assert slack.calls == []


class TestSlackReactionSuppression:
    """``slack.reactions`` may set the ack or the error reaction to ``null``."""

    def test_build_phase_emojis_accepts_none(self) -> None:
        result, unknown = handler_mod._build_phase_emojis({"queued": None, "error": "boom"})
        assert result == {"queued": None, "error": "boom"}
        assert unknown == []

    @pytest.mark.asyncio
    async def test_suppressed_ack_adds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(handler_mod._PHASE_EMOJIS, "queued", None)
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_suppressed_error_still_takes_the_ack_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(handler_mod._PHASE_EMOJIS, "error", None)
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS)
        ctrl.start(native_status=False)
        await asyncio.sleep(0)
        ctrl.finalize(error=True)
        await asyncio.sleep(0)
        assert slack.calls == [("add", _TS, "eyes"), ("remove", _TS, "eyes")]


# ── Channel-neutral ladder (messaging.status_reactions) ─────────────────

#: Deliberately not emoji: the ladder is vocabulary-agnostic, and single letters
#: make an assertion read as the transition it is checking.
_SHARED_EMOJIS: dict[str, str | None] = {
    "queued": "Q",
    "thinking": "T",
    "coding": "C",
    "browsing": "B",
    "tool": "W",
    "done": "D",
    "error": "X",
}
_SHARED_STALL = StallEmojis(soft="s", hard="h")


def _timings(**kw: float) -> LadderTimings:
    """Fast, deterministic durations (consumed by ``FakeClock.advance``)."""
    return LadderTimings(
        debounce=kw.get("debounce", _DEBOUNCE),
        stall_soft=kw.get("stall_soft", _SOFT),
        stall_hard=kw.get("stall_hard", _HARD),
        close_drain=kw.get("close_drain", 0.05),
    )


class RecordingSink:
    """Records ``(action, emoji)`` edits; optionally fails like a real channel."""

    def __init__(self, *, fail_add: bool = False, fail_remove: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail_add = fail_add
        self._fail_remove = fail_remove

    async def add(self, emoji: str) -> None:
        self.calls.append(("add", emoji))
        if self._fail_add:
            raise RuntimeError("channel refused the reaction")

    async def remove(self, emoji: str) -> None:
        self.calls.append(("remove", emoji))
        if self._fail_remove:
            raise RuntimeError("channel refused the reaction")

    @property
    def adds(self) -> list[str]:
        return [emoji for action, emoji in self.calls if action == "add"]

    @property
    def removes(self) -> list[str]:
        return [emoji for action, emoji in self.calls if action == "remove"]


class BlockingSink:
    """A sink whose edits never return, standing in for a wedged channel."""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def add(self, emoji: str) -> None:
        self.started.set()
        await self.release.wait()

    async def remove(self, emoji: str) -> None:
        self.started.set()
        await self.release.wait()


def _ladder(sink: Any, **kw: Any) -> PhaseReactionLadder:
    kw.setdefault("timings", _timings())
    kw.setdefault("stall", _SHARED_STALL)
    return PhaseReactionLadder(sink, emojis=_SHARED_EMOJIS, **kw)


async def _settle() -> None:
    """Let every spawned sink task run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)


class TestSharedLadderPhases:
    """The ladder walks phases, debouncing everything but queued and terminals."""

    @pytest.mark.asyncio
    async def test_queued_is_immediate(self) -> None:
        with fake_clock():
            sink = RecordingSink()
            _ladder(sink).set_phase("queued")
            await _settle()
            assert sink.calls == [("add", "Q")]

    @pytest.mark.asyncio
    async def test_intermediate_phase_waits_for_the_debounce(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.set_phase("coding")
            await clock.advance(_DEBOUNCE / 2)
            assert sink.calls == []  # still held

            await clock.advance(_DEBOUNCE)
            assert sink.calls == [("remove", "Q"), ("add", "C")]

    @pytest.mark.asyncio
    async def test_debounce_collapses_a_burst_to_the_last_phase(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.set_phase("thinking")
            ladder.set_phase("coding")
            ladder.set_phase("browsing")
            await clock.advance(_DEBOUNCE + 0.01)
            assert sink.adds == ["B"]

    @pytest.mark.asyncio
    async def test_terminal_phase_finalizes_at_once(self) -> None:
        with fake_clock():
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.set_phase("error")
            await _settle()
            assert sink.calls == [("remove", "Q"), ("add", "X")]

    @pytest.mark.asyncio
    async def test_finalize_is_idempotent(self) -> None:
        with fake_clock():
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.finalize()
            ladder.finalize(error=True)
            ladder.set_phase("coding")
            await _settle()
            assert sink.adds == ["D"]

    @pytest.mark.asyncio
    async def test_suppressed_phase_clears_without_replacing(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            table = dict(_SHARED_EMOJIS)
            table["coding"] = None
            ladder = PhaseReactionLadder(sink, emojis=table, timings=_timings())
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.set_phase("coding")
            await clock.advance(_DEBOUNCE + 0.01)
            assert sink.calls == [("remove", "Q")]

    @pytest.mark.asyncio
    async def test_disabled_ladder_touches_the_channel_never(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink, enabled=False)
            ladder.set_phase("queued")
            ladder.set_phase("coding")
            ladder.on_progress()
            ladder.finalize()
            await clock.advance(_HARD + 1.0)
            await _settle()
            assert sink.calls == []

    @pytest.mark.asyncio
    async def test_emoji_table_is_snapshotted(self) -> None:
        """A table mutated after construction cannot desync the ladder."""
        with fake_clock():
            sink = RecordingSink()
            table = dict(_SHARED_EMOJIS)
            ladder = PhaseReactionLadder(sink, emojis=table, timings=_timings())
            table["queued"] = "MUTATED"
            ladder.set_phase("queued")
            await _settle()
            assert sink.adds == ["Q"]


class TestSharedLadderStall:
    """The watchdog marks a quiet turn, upgrades, resets, and pauses."""

    @pytest.mark.asyncio
    async def test_soft_mark_after_the_soft_window(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            _ladder(sink).set_phase("queued")
            await _settle()
            sink.calls.clear()

            await clock.advance(_SOFT + 0.01)
            await _settle()
            assert sink.calls == [("add", "s")]

    @pytest.mark.asyncio
    async def test_hard_mark_replaces_the_soft_one(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            _ladder(sink).set_phase("queued")
            await _settle()
            sink.calls.clear()

            await clock.advance(_HARD + 0.01)
            await _settle()
            assert sink.calls == [("add", "s"), ("remove", "s"), ("add", "h")]

    @pytest.mark.asyncio
    async def test_progress_clears_the_mark_and_restarts_the_window(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()

            await clock.advance(_SOFT + 0.01)
            await _settle()
            assert sink.adds == ["Q", "s"]
            sink.calls.clear()

            ladder.on_progress()
            await _settle()
            assert sink.removes == ["s"]

            await clock.advance(_SOFT - 0.1)
            await _settle()
            assert sink.adds == []  # window restarted, not resumed
            await clock.advance(0.2)
            await _settle()
            assert sink.adds == ["s"]

    @pytest.mark.asyncio
    async def test_pause_holds_the_watchdog_and_resume_restarts_it(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.pause_stall_watchdog()
            await clock.advance(_HARD + 1.0)
            await _settle()
            assert sink.calls == []
            ladder.on_progress()  # inert while paused
            await clock.advance(_SOFT + 0.01)
            await _settle()
            assert sink.calls == []

            ladder.resume_stall_watchdog()
            await clock.advance(_SOFT + 0.01)
            await _settle()
            assert sink.adds == ["s"]

    @pytest.mark.asyncio
    async def test_finalize_clears_the_stall_mark_before_the_terminal(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            await clock.advance(_SOFT + 0.01)
            await _settle()
            sink.calls.clear()

            ladder.finalize()
            await _settle()
            assert sink.calls == [("remove", "s"), ("remove", "Q"), ("add", "D")]

    @pytest.mark.asyncio
    async def test_no_stall_vocabulary_schedules_no_watchdog(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = PhaseReactionLadder(
                sink, emojis=_SHARED_EMOJIS, stall=StallEmojis(), timings=_timings()
            )
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            await clock.advance(_HARD + 1.0)
            await _settle()
            assert sink.calls == []


class TestSharedLadderClose:
    """``close`` cancels every timer and leaves no task behind."""

    @pytest.mark.asyncio
    async def test_close_cancels_the_pending_debounce_and_watchdog(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            ladder.set_phase("coding")  # debounce + both stall timers armed
            sink.calls.clear()
            assert clock.armed == 3

            await ladder.close()
            # The timers are gone from the loop, not merely inert: a handle left
            # armed keeps its callback (and this ladder) alive until it fires.
            assert clock.armed == 0
            await clock.advance(_HARD + 1.0)
            await _settle()
            assert sink.calls == []

    @pytest.mark.asyncio
    async def test_close_is_idempotent_and_refuses_later_work(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            await ladder.close()
            await ladder.close()
            ladder.set_phase("coding")
            ladder.finalize(error=True)
            ladder.on_progress()
            ladder.resume_stall_watchdog()
            await clock.advance(_HARD + 1.0)
            await _settle()
            assert sink.calls == []

    @pytest.mark.asyncio
    async def test_close_awaits_an_in_flight_edit(self) -> None:
        class SlowSink(RecordingSink):
            async def add(self, emoji: str) -> None:
                await asyncio.sleep(0)  # yields, like a real network round trip
                await super().add(emoji)

        sink = SlowSink()
        ladder = _ladder(sink)
        ladder.set_phase("queued")  # spawned, not yet run
        assert sink.calls == []

        await ladder.close()
        assert sink.calls == [("add", "Q")]

    @pytest.mark.asyncio
    async def test_close_leaks_no_task_when_the_channel_wedges(self) -> None:
        baseline = asyncio.all_tasks()
        sink = BlockingSink()
        ladder = _ladder(sink, timings=_timings(close_drain=0.05))
        ladder.set_phase("queued")
        # Bounded: only the ladder actually reaching ``sink.add`` sets ``started``,
        # so a queued phase that stops dispatching -- a new guard above the spawn,
        # or a ``ReactionSink`` protocol change whose AttributeError ``_sink_call``
        # swallows -- would park here forever instead of failing at the two
        # assertions below. This runs on the real clock, so the ceiling is generous
        # against the 0.1s debounce and 1.0s soft-stall timers left armed.
        await await_until(sink.started, what="the reaction sink started")

        await ladder.close()

        assert not sink.release.is_set()  # the edit never completed
        assert asyncio.all_tasks() - baseline == set()

    @pytest.mark.asyncio
    async def test_close_leaks_no_task_after_a_normal_turn(self) -> None:
        baseline = asyncio.all_tasks()
        with fake_clock() as clock:
            sink = RecordingSink()
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            ladder.set_phase("coding")
            await clock.advance(_DEBOUNCE + 0.01)
            await clock.advance(_SOFT + 0.01)
            ladder.finalize()
            await ladder.close()
        assert sink.adds == ["Q", "C", "s", "D"]
        assert asyncio.all_tasks() - baseline == set()


class TestSharedLadderSinkFailures:
    """A channel that refuses a reaction must not cost the turn."""

    @pytest.mark.asyncio
    async def test_failing_add_is_swallowed_and_the_ladder_continues(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink(fail_add=True)
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            ladder.set_phase("coding")
            await clock.advance(_DEBOUNCE + 0.01)
            await _settle()
            ladder.finalize()
            await ladder.close()
            assert sink.adds == ["Q", "C", "D"]

    @pytest.mark.asyncio
    async def test_failing_remove_still_applies_the_new_emoji(self) -> None:
        with fake_clock() as clock:
            sink = RecordingSink(fail_remove=True)
            ladder = _ladder(sink)
            ladder.set_phase("queued")
            await _settle()
            sink.calls.clear()

            ladder.set_phase("coding")
            await clock.advance(_DEBOUNCE + 0.01)
            await _settle()
            assert sink.calls == [("remove", "Q"), ("add", "C")]


class TestSharedToolClassification:
    """The shared tool-to-phase mapping the channel-neutral ladder reads."""

    @pytest.mark.parametrize(
        "name,kind,phase",
        [
            ("Bash", "", "coding"),
            ("WebFetch", "", "browsing"),
            ("SomethingElse", "", "tool"),
            ("UnknownTool", "bash", "coding"),
            ("X", "webfetch", "browsing"),
            ("mcp__builder-mcp__Bash", "", "coding"),
            ("mcp__some-server__WebFetch", "", "browsing"),
            ("Read", "other", "coding"),
        ],
    )
    def test_classifies_tools(self, name: str, kind: str, phase: str) -> None:
        assert shared_tool_to_phase(name, kind) == phase

    def test_display_title_prefix_is_stripped_for_classification(self) -> None:
        assert shared_tool_to_phase("Running: Bash") == "tool"
        assert phase_for_tool_title("Running: Bash") == "coding"
        assert phase_for_tool_title("Bash") == "coding"
        assert phase_for_tool_title("Running: ls -la") == "tool"


class TestMergePhaseEmojis:
    """The hoisted override merge, shared by every channel with a table."""

    def test_override_applies_and_none_suppresses(self) -> None:
        table, unknown = merge_phase_emojis(_SHARED_EMOJIS, {"queued": "!", "done": None})
        assert table["queued"] == "!"
        assert table["done"] is None
        assert table["error"] == "X"  # untouched
        assert unknown == []

    def test_unknown_keys_are_reported_not_applied(self) -> None:
        table, unknown = merge_phase_emojis(_SHARED_EMOJIS, {"qeued": "!"})
        assert unknown == ["qeued"]
        assert "qeued" not in table

    def test_defaults_are_not_mutated(self) -> None:
        original = dict(_SHARED_EMOJIS)
        merge_phase_emojis(_SHARED_EMOJIS, {"queued": "!"})
        assert _SHARED_EMOJIS == original

    def test_no_overrides_returns_the_defaults(self) -> None:
        table, unknown = merge_phase_emojis(_SHARED_EMOJIS)
        assert table == _SHARED_EMOJIS
        assert unknown == []


class TestFormatTurnStatus:
    """The turn-end line, shaped exactly as Slack's timing footer text."""

    @pytest.mark.parametrize(
        "elapsed,expected",
        [
            (0.0, "Finished in 0s"),
            (-1.0, "Finished in 0s"),
            (12.7, "Finished in 12s"),
            (59.9, "Finished in 59s"),
            (60.0, "Finished in 1m 0s"),
            (64.2, "Finished in 1m 4s"),
            (3661.0, "Finished in 61m 1s"),
        ],
    )
    def test_elapsed_shape(self, elapsed: float, expected: str) -> None:
        assert format_turn_status(elapsed) == expected

    @pytest.mark.parametrize(
        "pct,icon",
        [(0.0, "🟢"), (29.9, "🟢"), (30.0, "🟡"), (49.0, "🟡"), (50.0, "🟠"), (70.0, "🔴")],
    )
    def test_context_bands(self, pct: float, icon: str) -> None:
        assert format_turn_status(5.0, pct) == f"Finished in 5s · {icon} ctx {round(pct)}%"

    def test_unknown_usage_carries_no_chip(self) -> None:
        assert format_turn_status(5.0, None) == "Finished in 5s"

    def test_matches_the_slack_footer_text(self) -> None:
        """One wording for both channels: Slack's block text is this string."""
        _, footer_text = handler_mod.build_timing_footer(64.2)
        assert footer_text == format_turn_status(64.2)


class GatedReactionSink:
    """A sink whose FIRST ``remove`` blocks until released.

    A reaction swap is remove-then-add with a real round-trip in between, and each
    swap runs as its own task. Holding one swap open inside that window is what
    makes the interleaving observable at all -- with real network timing it is a
    race that reproduces rarely and leaves a permanent wrong reaction when it does.
    """

    def __init__(self) -> None:
        #: The emojis currently on the message, as the channel would hold them.
        self.live: set[str] = set()
        #: Every edit in the order it landed, so the LAST one can be named.
        self.order: list[str] = []
        self.first_remove = asyncio.Event()
        self.release = asyncio.Event()
        self._removes = 0

    async def add(self, emoji: str) -> None:
        self.live.add(emoji)
        self.order.append(f"+{emoji}")

    async def remove(self, emoji: str) -> None:
        self._removes += 1
        if self._removes == 1:
            self.first_remove.set()
            await self.release.wait()
        self.live.discard(emoji)
        self.order.append(f"-{emoji}")


class TestConcurrentSwapsAreSerialized:
    """Two swaps in flight must not reorder into a stale final reaction.

    Each phase transition is spawned as a task, so a burst -- a tool starting as
    the turn finishes -- puts two swaps in the remove/add window at once. Applied
    in whichever order the network returns, the obsolete emoji can be added AFTER
    the terminal one, and the turn then reads as permanently in progress with no
    later event to correct it.
    """

    @pytest.mark.asyncio
    async def test_the_terminal_emoji_is_the_one_left_on_the_message(self) -> None:
        sink = GatedReactionSink()
        ladder = PhaseReactionLadder(sink, emojis={})
        # Start from a reaction already on the message: the swap's whole job is
        # replacing one, and with none there the remove leg never runs.
        ladder._current_emoji = "eyes"
        sink.live.add("eyes")

        working = asyncio.create_task(ladder._swap_emoji("hourglass"))
        # Bounded for the same reason as the wedged-channel test above: only the
        # swap's remove leg sets ``first_remove``, and ``_sink_call`` swallows the
        # exception a protocol change would raise, so a swap that stops removing
        # would park here rather than fail at the two assertions below.
        await await_until(sink.first_remove, what="the first reaction removal")
        # The turn finishes while the first swap is still mid-flight.
        done = asyncio.create_task(ladder._swap_emoji("white_check_mark"))
        await asyncio.sleep(0)
        sink.release.set()
        await asyncio.gather(working, done)

        assert sink.live == {"white_check_mark"}
        assert sink.order[-1] == "+white_check_mark"

    @pytest.mark.asyncio
    async def test_a_suppressed_phase_still_clears_and_adds_nothing(self) -> None:
        """``None`` means "no emoji for this phase", not "leave the old one"."""
        sink = GatedReactionSink()
        sink.release.set()
        ladder = PhaseReactionLadder(sink, emojis={})
        ladder._current_emoji = "eyes"
        sink.live.add("eyes")
        await ladder._swap_emoji(None)
        assert sink.live == set()


class TestImportDoesNotCreateTheDataHome:
    """Importing the handler reads reaction overrides but must not ``mkdir`` the home.

    ``_PHASE_EMOJIS`` is built at import from ``slack.reactions``. Loading the
    config resolves ``config_dir()``, which CREATES ``~/.kiro/crew`` -- so a plain
    import (every test collector does one, through ``test/conftest.py``) wrote to the
    operator's real home. With no ``config.json`` there are no overrides to apply, so
    the import peeks for the file and loads only when it already exists.
    """

    _PROBE = (
        "import os, sys, traceback\n"
        "def hook(ev, args):\n"
        "    if ev == 'os.mkdir' and str(args[0]).startswith(sys.argv[1]):\n"
        "        sys.stderr.write('mkdir %s\\n' % args[0])\n"
        "        sys.stderr.write(''.join(traceback.format_stack(limit=12)[:-1]))\n"
        "sys.addaudithook(hook)\n"
        "import json\n"
        "import kiro_crew.slack.handler as h\n"
        "from kiro_crew.config import paths\n"
        "print(json.dumps(h._PHASE_EMOJIS))\n"
        "sys.exit(1 if paths._default_home().exists() != (sys.argv[2] == 'seeded') else 0)\n"
    )

    def _import_in_a_fresh_interpreter(self, tmp_path, *, seeded: bool):
        import json
        import os
        import subprocess
        import sys

        home = tmp_path / "home"
        home.mkdir()
        if seeded:
            data = home / ".kiro" / "crew"
            data.mkdir(parents=True)
            (data / "config.json").write_text(
                json.dumps({"slack": {"reactions": {"queued": "brain"}}}), encoding="utf-8"
            )
        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_HOME"}
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)
        proc = subprocess.run(
            # ``-B``: the child imports the source package; without it the import
            # leaves ``__pycache__`` in the checkout.
            [sys.executable, "-B", "-c", self._PROBE, str(home), "seeded" if seeded else "bare"],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout.strip().splitlines()[-1]), home

    def test_a_fresh_interpreter_import_leaves_an_absent_home_absent(self, tmp_path):
        emojis, home = self._import_in_a_fresh_interpreter(tmp_path, seeded=False)
        assert not (home / ".kiro").exists()
        assert emojis["queued"] == handler_mod._DEFAULT_PHASE_EMOJIS["queued"]

    def test_an_existing_config_still_supplies_the_overrides(self, tmp_path):
        emojis, _ = self._import_in_a_fresh_interpreter(tmp_path, seeded=True)
        assert emojis["queued"] == "brain"
