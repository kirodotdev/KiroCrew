"""Tests for the phase-aware status reaction ladder.

Two halves: Slack's ``StatusReactionController`` as the channel that shipped it
first, and the channel-neutral ladder in ``messaging.status_reactions`` that any
channel drives through an injected emoji sink.
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
from kiro_crew.slack.handler import (
    StatusReactionController,
    _tool_to_phase,
)

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


@pytest.fixture(autouse=True)
def _fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set deterministic timer values (consumed by FakeClock.advance)."""
    monkeypatch.setattr(handler_mod, "_PHASE_DEBOUNCE_SECS", _DEBOUNCE)
    monkeypatch.setattr(handler_mod, "_STALL_SOFT_SECS", _SOFT)
    monkeypatch.setattr(handler_mod, "_STALL_HARD_SECS", _HARD)


# ── Core phase transition tests ─────────────────────────────────────────


class TestPhaseTransitions:
    """Basic phase lifecycle."""

    @pytest.mark.asyncio
    async def test_queued_adds_eyes(self) -> None:
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            assert ("add", _TS, "eyes") in slack.calls

    @pytest.mark.asyncio
    async def test_thinking_swaps_eyes_to_thinking_face(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.set_phase("thinking")
            await clock.advance(_DEBOUNCE + 0.01)
            assert ("remove", _TS, "eyes") in slack.calls
            assert ("add", _TS, "thinking_face") in slack.calls

    @pytest.mark.asyncio
    async def test_finalize_done_is_immediate(self) -> None:
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.finalize(error=False)
            await asyncio.sleep(0)
            assert ("remove", _TS, "eyes") in slack.calls
            assert ("add", _TS, "lobster") in slack.calls

    @pytest.mark.asyncio
    async def test_finalize_error(self) -> None:
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.finalize(error=True)
            await asyncio.sleep(0)
            assert ("remove", _TS, "eyes") in slack.calls
            assert ("add", _TS, "scream") in slack.calls

    @pytest.mark.asyncio
    async def test_debounce_suppresses_rapid_transitions(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.set_phase("thinking")
            ctrl.set_phase("coding")
            ctrl.set_phase("browsing")

            await clock.advance(_DEBOUNCE + 0.01)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "thinking_face" not in add_emojis
            assert "man_technologist" not in add_emojis
            assert "globe_with_meridians" in add_emojis


class TestToolToPhase:
    """_tool_to_phase mapping."""

    def test_coding_tool_by_name(self) -> None:
        assert _tool_to_phase("Bash") == "coding"
        assert _tool_to_phase("Edit") == "coding"

    def test_web_tool_by_name(self) -> None:
        assert _tool_to_phase("WebFetch") == "browsing"

    def test_unknown_tool(self) -> None:
        assert _tool_to_phase("SomethingElse") == "tool"

    def test_kind_preferred_over_name(self) -> None:
        assert _tool_to_phase("UnknownTool", tool_kind="bash") == "coding"

    def test_web_kind(self) -> None:
        assert _tool_to_phase("X", tool_kind="webfetch") == "browsing"

    def test_mcp_tool_extracts_base(self) -> None:
        assert _tool_to_phase("mcp__builder-mcp__Bash") == "coding"

    def test_mcp_web_tool_extracts_base(self) -> None:
        assert _tool_to_phase("mcp__some-server__WebFetch") == "browsing"


# ── Stall detection tests ──────────────────────────────────────────────


class TestStallDetection:
    """Stall watchdog fires soft/hard reactions and can be paused/reset."""

    @pytest.mark.asyncio
    async def test_stall_soft_fires_after_delay(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            await clock.advance(_SOFT + 0.01)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" in add_emojis

    @pytest.mark.asyncio
    async def test_stall_hard_replaces_soft(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            await clock.advance(_HARD + 0.01)
            assert ("remove", _TS, "yawning_face") in slack.calls
            assert ("add", _TS, "fearful") in slack.calls

    @pytest.mark.asyncio
    async def test_progress_resets_stall(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            await clock.advance(_SOFT - 0.2)
            ctrl.on_progress()

            await clock.advance(0.4)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" not in add_emojis

            await clock.advance(_SOFT)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" in add_emojis

    @pytest.mark.asyncio
    async def test_pause_prevents_stall(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.pause_stall_watchdog()

            await clock.advance(_SOFT + 0.5)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" not in add_emojis

    @pytest.mark.asyncio
    async def test_resume_restarts_stall_watchdog(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.pause_stall_watchdog()

            await clock.advance(_SOFT + 0.5)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" not in add_emojis

            ctrl.resume_stall_watchdog()
            await clock.advance(_SOFT + 0.01)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "yawning_face" in add_emojis

    @pytest.mark.asyncio
    async def test_finalize_cleans_up_stall(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)

            await clock.advance(_SOFT + 0.01)
            assert ("add", _TS, "yawning_face") in slack.calls
            slack.calls.clear()

            ctrl.finalize(error=False)
            await asyncio.sleep(0)
            assert ("remove", _TS, "yawning_face") in slack.calls
            assert ("add", _TS, "lobster") in slack.calls


# ── Disabled reactions tests ─────────────────────────────────────────────


class TestDisabledReactions:
    """When enabled=False, no reactions should be added or removed."""

    @pytest.mark.asyncio
    async def test_disabled_set_phase_no_ops(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
            ctrl.set_phase("queued")
            await clock.advance(0.5)
            assert slack.calls == []

    @pytest.mark.asyncio
    async def test_disabled_finalize_no_ops(self) -> None:
        slack = FakeSlack()
        ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
        ctrl.finalize(error=False)
        await asyncio.sleep(0)
        assert slack.calls == []

    @pytest.mark.asyncio
    async def test_disabled_full_lifecycle_no_ops(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
            ctrl.set_phase("queued")
            ctrl.set_phase("thinking")
            ctrl.on_progress()
            ctrl.set_phase("coding")
            ctrl.finalize(error=False)
            await clock.advance(0.5)
            assert slack.calls == []

    @pytest.mark.asyncio
    async def test_enabled_true_still_works(self) -> None:
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS, enabled=True)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            assert ("add", _TS, "eyes") in slack.calls

    @pytest.mark.asyncio
    async def test_disabled_resume_stall_watchdog_no_ops(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
            ctrl.pause_stall_watchdog()
            ctrl.resume_stall_watchdog()
            await clock.advance(_SOFT + 0.5)
            assert slack.calls == []

    @pytest.mark.asyncio
    async def test_disabled_on_progress_no_stall(self) -> None:
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS, enabled=False)
            ctrl.on_progress()
            await clock.advance(_SOFT + 0.5)
            assert slack.calls == []


# ── Per-phase suppression tests (slack.reactions with null values) ──────


class TestPhaseSuppression:
    """When a phase emoji is set to ``None`` in ``_PHASE_EMOJIS``, that phase
    must neither add a new reaction nor emit a stray ``add`` call. Transitions
    into and out of a suppressed phase should still clean up any prior emoji.
    """

    @pytest.mark.asyncio
    async def test_build_phase_emojis_accepts_none(self) -> None:
        result, unknown = handler_mod._build_phase_emojis({"done": None, "error": "boom"})
        assert result["done"] is None
        assert result["error"] == "boom"
        # Other defaults untouched
        assert result["queued"] == "eyes"
        assert unknown == []

    @pytest.mark.asyncio
    async def test_suppressed_queued_adds_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A suppressed immediate phase makes no API calls on entry."""
        suppressed = dict(handler_mod._PHASE_EMOJIS)
        suppressed["queued"] = None
        monkeypatch.setattr(handler_mod, "_PHASE_EMOJIS", suppressed)
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            add_calls = [c for c in slack.calls if c[0] == "add"]
            assert add_calls == []

    @pytest.mark.asyncio
    async def test_suppressed_intermediate_phase_no_add(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suppressed intermediate phase still clears the prior emoji but adds nothing."""
        suppressed = dict(handler_mod._PHASE_EMOJIS)
        suppressed["thinking"] = None
        monkeypatch.setattr(handler_mod, "_PHASE_EMOJIS", suppressed)
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.set_phase("thinking")
            await clock.advance(_DEBOUNCE + 0.01)
            # Old emoji removed, no new one added
            assert ("remove", _TS, "eyes") in slack.calls
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "thinking_face" not in add_emojis
            assert add_emojis == []

    @pytest.mark.asyncio
    async def test_suppressed_done_finalize_no_lobster(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The original motivating case: suppressing `done` leaves no terminal emoji
        but still cleans up any prior phase emoji."""
        suppressed = dict(handler_mod._PHASE_EMOJIS)
        suppressed["done"] = None
        monkeypatch.setattr(handler_mod, "_PHASE_EMOJIS", suppressed)
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.finalize(error=False)
            await asyncio.sleep(0)
            assert ("remove", _TS, "eyes") in slack.calls
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "lobster" not in add_emojis
            assert add_emojis == []

    @pytest.mark.asyncio
    async def test_suppressed_done_still_cleans_up_stall(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Stall emoji still removed on finalize even when `done` is suppressed."""
        suppressed = dict(handler_mod._PHASE_EMOJIS)
        suppressed["done"] = None
        monkeypatch.setattr(handler_mod, "_PHASE_EMOJIS", suppressed)
        with fake_clock() as clock:
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)

            await clock.advance(_SOFT + 0.01)
            assert ("add", _TS, "yawning_face") in slack.calls
            slack.calls.clear()

            ctrl.finalize(error=False)
            await asyncio.sleep(0)
            assert ("remove", _TS, "yawning_face") in slack.calls
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert add_emojis == []

    @pytest.mark.asyncio
    async def test_suppressed_error_finalize_no_scream(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`error` can be suppressed too (symmetry with `done`)."""
        suppressed = dict(handler_mod._PHASE_EMOJIS)
        suppressed["error"] = None
        monkeypatch.setattr(handler_mod, "_PHASE_EMOJIS", suppressed)
        with fake_clock():
            slack = FakeSlack()
            ctrl = StatusReactionController(slack, _CH, _TS)
            ctrl.set_phase("queued")
            await asyncio.sleep(0)
            slack.calls.clear()

            ctrl.finalize(error=True)
            await asyncio.sleep(0)
            add_emojis = [e for a, _, e in slack.calls if a == "add"]
            assert "scream" not in add_emojis
            assert add_emojis == []


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
        await sink.started.wait()

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
    """The hoisted mapping answers exactly as Slack's own copy does."""

    @pytest.mark.parametrize(
        "name,kind",
        [
            ("Bash", ""),
            ("WebFetch", ""),
            ("SomethingElse", ""),
            ("UnknownTool", "bash"),
            ("X", "webfetch"),
            ("mcp__builder-mcp__Bash", ""),
            ("mcp__some-server__WebFetch", ""),
            ("Read", "other"),
        ],
    )
    def test_matches_the_slack_mapping(self, name: str, kind: str) -> None:
        assert shared_tool_to_phase(name, kind) == _tool_to_phase(name, kind)

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
        await sink.first_remove.wait()
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
