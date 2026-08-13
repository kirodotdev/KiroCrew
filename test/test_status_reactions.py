"""Tests for StatusReactionController phase-aware reactions."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import Any, Generator

import pytest

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
            due = [
                (t, h) for t, h in self._scheduled if t <= target and not h.cancelled()
            ]
            if not due:
                break
            due.sort(key=lambda x: x[0])
            t, h = due[0]
            self._scheduled.remove((t, h))
            self._now = t
            h._run()
            await asyncio.sleep(0)
        self._now = target
        await asyncio.sleep(0)

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
    async def test_suppressed_queued_adds_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
