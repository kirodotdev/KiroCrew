"""The configurable compaction method (``CompactionCoordinator._run_method_ladder``).

``session.compaction_method`` names what runs when the auto-compact threshold
fires. ``native`` is the in-place ``/compact`` the coordinator always ran; the
rotation method ``soft`` recycles the session on purpose so the successor is
re-seeded from the transcript, and hands the turn to ``native`` when it cannot
run (``unavailable``) or would not clear the threshold (``insufficient``). The
default-method tests pin the contract everything else rests on: with
``native``, dispatch is the dispatch without a rotation, exactly, and no
rotation seam is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.messaging.queue_drain import QUEUED_CHANNEL_KEY
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import (
    COMPACT_OUTCOME_CANCELLED,
    COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS,
)
from kiro_crew.testing.wait import async_wait_until, await_until

KEY = "dashboard:compaction-ladder"


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


async def _drain_background(mgr: SessionManager) -> None:
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@contextlib.asynccontextmanager
async def _managed(cfg: KiroCrewConfig, provider_factory: Any):
    """Manager whose teardown always runs, even on a failed assert."""
    mgr = SessionManager(cfg, provider_factory=provider_factory)
    try:
        yield mgr
    finally:
        await _drain_background(mgr)
        await mgr.close_all()


class _Writer:
    """A ``SeedWriter`` fake: ``supports`` answers *supports*, or raises *raises*."""

    def __init__(self, *, supports: bool = True, raises: BaseException | None = None) -> None:
        self.supported = supports
        self.raises = raises
        self.asked: list[str] = []

    def supports(self, key: str) -> bool:
        self.asked.append(key)
        if self.raises is not None:
            raise self.raises
        return self.supported


def _provider_factory(
    *, pct: float = 92.0, window: int = 1_000_000, successor_pct: float | None = None
):
    """Provider at *pct* on a *window*-token model that compacts in place on request.

    ``context_window_tokens`` is a sync ``MagicMock`` (not an ``AsyncMock``
    attribute, which would hand the coordinator a coroutine) so a test can both set
    the window and assert whether the coordinator read it. *successor_pct*, when
    given, is what every provider after the first reports: the successor of a
    rotation starts at its real usage, not at the predecessor's.
    """
    created = {"n": 0}

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        created["n"] += 1
        my_pct = pct if created["n"] == 1 or successor_pct is None else successor_pct
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}
        m.context_usage_pct = lambda: 0.0 if state["compacted"] else my_pct
        m.context_usage_unknown = lambda: bool(state["compacted"])
        m.manual_compact_unsupported_backend = None
        m.context_window_tokens = MagicMock(return_value=window)

        async def _stream(_cmd):
            for ev in []:
                yield ev  # pragma: no cover - status arrives async, not inline

        m.stream_command = MagicMock(side_effect=_stream)

        async def _wait(timeout=None):
            state["compacted"] = True
            return {"type": "completed"}

        m.wait_for_compaction = AsyncMock(side_effect=_wait)
        return m

    return factory


class _SemaphoreSpy:
    """Counts acquisitions while behaving exactly like the real semaphore."""

    def __init__(self, inner: asyncio.Semaphore) -> None:
        self._inner = inner
        self.acquires = 0

    async def acquire(self) -> bool:
        self.acquires += 1
        return await self._inner.acquire()

    def release(self) -> None:
        self._inner.release()

    def locked(self) -> bool:
        return self._inner.locked()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *exc: object) -> None:
        self.release()


async def _live_session(mgr: SessionManager, key: str = KEY):
    """Register a live session for *key* and return (provider, session, spy)."""
    provider, _, _ = await mgr.get_or_create(key)
    mgr.release(key)
    session = mgr._sessions[key]
    spy = _SemaphoreSpy(session.semaphore)
    session.semaphore = spy
    return provider, session, spy


def _callback_recorder() -> tuple[list[tuple[str, float, bool, str, str]], Any]:
    """Record every callback call as ``(key, pct, success, outcome, method)``."""
    calls: list[tuple[str, float, bool, str, str]] = []

    async def _cb(
        key: str,
        pct: float,
        *,
        success: bool,
        outcome: str = "compacted",
        method: str,
    ) -> None:
        calls.append((key, pct, success, outcome, method))

    return calls, _cb


class _SharedSubagent:
    """One sub-agent live on the parent's process until ``gate`` is set: the slice
    of ``SubagentManager`` the restart's cotenant wait reads and ends."""

    def __init__(self, key: str, gate: asyncio.Event) -> None:
        self.gate = gate
        self.child = SimpleNamespace(
            id="a1",
            parent_session_key=key,
            conversation_key="",
            _stop_origin="",
            _reap_reason="",
            _reap_started=False,
            done=False,
        )

    @property
    def running(self) -> list[Any]:
        return [] if self.gate.is_set() else [self.child]

    def has_live_shared_session(self, _key: str) -> bool:
        return not self.gate.is_set()

    def snapshot_teardown_children(self, _parent: str) -> tuple[()]:
        return ()

    async def cancel(self, _id: str) -> bool:
        self.gate.set()
        return True

    async def cancel_for_teardown(
        self, ids: Any, *, parent_session_key: str, verb: str = ""
    ) -> int:
        return len(ids)


def _cotenant_wait(mgr: SessionManager, wait_secs: float) -> None:
    mgr._compaction._deps = dataclasses.replace(
        mgr._compaction._deps, cotenant_wait_secs=wait_secs, cotenant_poll_secs=0.01
    )


class TestRotationRestart:
    """A rotation ends the process the way every other compaction restart does."""

    @pytest.mark.asyncio
    async def test_a_rotation_waits_for_a_shared_subagent_then_restarts(self, cfg) -> None:
        """The process also hosts the session's sharing sub-agents. The rotation
        holds for them first, as the in-place failure path does, and stops them
        only past the wait budget, so none of them ends with no report."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)
            runs = _SharedSubagent(KEY, gate)
            mgr.set_child_teardown_handler(runs)
            _cotenant_wait(mgr, 0.3)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            assert gate.is_set(), "the shared sub-agent was stopped at the deadline"
            provider.shutdown.assert_awaited_once()
            assert [(c[2], c[3]) for c in calls] == [
                (False, COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS),
                (True, "recycled"),
            ]
            assert calls[-1][4] == "soft"
            # The wait is a rotation's, not a failed compaction's: the waiting
            # notice and the stopped sub-agent's reason say so.
            assert calls[0][4] == "soft"
            assert runs.child._stop_origin == (
                "stopped when an auto-compaction rotated the session (soft)"
            )

    @pytest.mark.asyncio
    async def test_a_hard_stop_during_the_wait_settles_cancelled_without_a_restart(
        self, cfg
    ) -> None:
        """A force Stop inside the cotenant wait already reset the session and
        handed its permit on. The rotation settles cancelled with no restart,
        leaves the sub-agent to the Stop (which stopped the one sharing its
        process), and does not release the permit a second time (the turn
        semaphore is bounded, so a double release raises)."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            await mgr.get_or_create(KEY)
            mgr.release(KEY)
            runs = _SharedSubagent(KEY, gate)
            mgr.set_child_teardown_handler(runs)
            _cotenant_wait(mgr, 30.0)

            task = asyncio.ensure_future(mgr.compact_if_needed(KEY))
            await async_wait_until(
                lambda: any(c[3] == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS for c in calls),
                describe=lambda: calls,
            )
            assert await mgr.stop_turn(KEY, force=True) == "hard"

            assert await await_until(task, what="the rotation settling") == "cancelled"
            # The one stop is the Stop's own; the stale rotation adds none.
            assert runs.child._stop_origin == "stopped by a hard Stop of the parent"
            assert (calls[-1][2], calls[-1][3]) == (False, COMPACT_OUTCOME_CANCELLED)
            # The Stop ended a rotation, so its notice names the rotation and
            # carries the marker the successor's replay leaves out.
            assert calls[-1][4] == "soft"
            assert KEY not in mgr._compaction.state.pending_verdict
            assert KEY not in mgr._compaction.state.rotating
            gate.set()

    @pytest.mark.asyncio
    async def test_a_reset_during_the_wait_answers_absent(self, cfg) -> None:
        """A reset that is not a Stop (a rewind, a model switch) replaces the
        session while the rotation waits for its sub-agents. The rotation
        answers absent: no restart of the stale session, no rotation notice, and
        no verdict armed on a successor the rotation never measured."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            _, session, _ = await _live_session(mgr)
            mgr.set_child_teardown_handler(_SharedSubagent(KEY, gate))
            _cotenant_wait(mgr, 30.0)

            task = asyncio.ensure_future(mgr.compact_if_needed(KEY))
            await async_wait_until(
                lambda: any(c[3] == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS for c in calls),
                describe=lambda: calls,
            )
            assert await mgr.reset(KEY) is True
            gate.set()

            assert await await_until(task, what="the rotation settling") == "absent"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict
            assert KEY not in state.rotating
            assert [c[3] for c in calls] == [
                COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS
            ], "no notice for a rotation that did not run"
            assert mgr._sessions.get(KEY) is not session

    @pytest.mark.asyncio
    async def test_a_message_queued_behind_the_rotation_moves_to_the_successor(self, cfg) -> None:
        """A message queued while the rotation holds the session lands in the
        successor's queue, as it does behind every other compaction restart."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        gate = asyncio.Event()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            _, session, _ = await _live_session(mgr)
            mgr.set_child_teardown_handler(_SharedSubagent(KEY, gate))
            _cotenant_wait(mgr, 30.0)

            task = asyncio.ensure_future(mgr.compact_if_needed(KEY))
            await async_wait_until(
                lambda: any(c[3] == COMPACT_OUTCOME_WAITING_FOR_SUBAGENTS for c in calls),
                describe=lambda: calls,
            )
            assert mgr.enqueue(KEY, "ts-1", "sent mid-rotation", **{QUEUED_CHANNEL_KEY: "slack"})
            gate.set()

            assert await await_until(task, what="the rotation settling") == "recycled"
            successor = mgr._sessions[KEY]
            assert successor is not session
            assert [entry[0] for entry in successor.queue] == ["ts-1"]


class TestRotationIdentity:
    @pytest.mark.asyncio
    async def test_reset_session_cannot_be_rotated_after_the_wait(self, cfg, monkeypatch) -> None:
        """A reset can pop the held session while a rotation waits for its permit."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(_Writer())
            provider, session, _ = await _live_session(mgr)
            await session.semaphore.acquire()
            # Named the way a real turn names itself, so the reset records this
            # task as the holder and hands the permit to the parked rotation.
            session.turn_owner = asyncio.current_task()
            acquire_started = asyncio.Event()
            acquire = session.semaphore.acquire

            async def acquire_after_signal() -> bool:
                acquire_started.set()
                return await acquire()

            monkeypatch.setattr(session.semaphore, "acquire", acquire_after_signal)
            rotation = asyncio.create_task(mgr.compact_if_needed(KEY))
            try:
                await await_until(acquire_started, what="the rotation's permit wait")
                assert await mgr.reset(KEY)
                reset_shutdowns = provider.shutdown.await_count
                mgr.release(KEY)

                assert await await_until(rotation, what="the rotation settling") == "absent"
                state = mgr._compaction.state
                assert KEY not in state.pending_verdict
                assert calls == []
                assert provider.shutdown.await_count == reset_shutdowns == 1
            finally:
                if not rotation.done():
                    rotation.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await rotation
                if mgr._sessions.get(KEY) is session and session.semaphore.locked():
                    mgr.release(KEY)


class TestDefaultOrderIsNative:
    @pytest.mark.asyncio
    async def test_native_only_dispatches_in_place_and_reads_no_rotation_seam(self, cfg) -> None:
        """The default is ``native`` alone: ``/compact`` is streamed under the
        semaphore, the window is never read, and no seed writer is consulted."""
        assert cfg.session.compaction_method == "native"
        writer = _Writer()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.context_window_tokens.assert_not_called()
            assert spy.acquires == 1
            assert KEY in mgr._sessions, "an in-place compaction keeps the session"
            assert writer.asked == [], "native asks the rotation surface nothing"

    @pytest.mark.asyncio
    async def test_native_compaction_names_itself_to_the_callback(self, cfg) -> None:
        """The callback learns which method ran: an in-place summary says ``native``
        (and ``compacted``), so the notice writers can tell it from a rotation
        (below) that reaches them through the same ``success=True`` call."""
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            assert calls == [(KEY, 92.0, True, "compacted", "native")]


class TestSoft:
    @pytest.mark.asyncio
    async def test_an_idle_compaction_never_rotates(self, cfg) -> None:
        """The idle trigger takes only the non-destructive branch, and a rotation
        is a restart: with ``soft`` configured it still compacts in place and asks
        the rotation surface nothing."""
        cfg.session.compaction_method = "soft"
        writer = _Writer()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, session, _ = await _live_session(mgr)

            outcome = await mgr._compaction._run_method_ladder(KEY, session, 92.0, idle=True)

            assert outcome == "ok"
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert writer.asked == [], "an idle compaction asks the rotation surface nothing"
            assert mgr._sessions[KEY] is session

    @pytest.mark.asyncio
    async def test_soft_recycles_without_a_model_call(self, cfg) -> None:
        """A dashboard surface (the registered writer answers ``supports``) is the
        one place soft may run: the successor is re-seeded from that transcript,
        so the rotation spends no model call."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        writer = _Writer()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(writer)
            provider, session, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            # The restart every compaction path takes: the successor starts
            # first and holds the key, but no turn has claimed it yet, so its
            # first real turn still replays the transcript.
            successor = mgr._sessions[KEY]
            assert successor is not session
            assert successor.first_turn.is_new, "the successor replays on its first turn"
            assert spy.acquires == 1, "rotation excludes turns like the in-place path"
            assert not spy.locked(), "the semaphore is released after the recycle"
            assert session.turn_owner is None, "the release clears the holder record"
            assert writer.asked == [KEY], "the surface was asked before the projection"
            # The callback is told it was a rotation: the successor carries only
            # the tail, and the notice has to say so rather than claim a summary.
            # A rotation is a recycle to the outcome arm as well; ``method`` is
            # what tells the surface it was the intended kind.
            assert calls == [(KEY, 92.0, True, "recycled", "soft")]

    @pytest.mark.asyncio
    async def test_soft_without_a_seed_writer_hands_straight_to_native(self, cfg, caplog) -> None:
        """No writer is registered (a gateway with no dashboard), so no surface
        holds a transcript the successor could be re-seeded from: a soft recycle
        would come back with zero turns. The method is unavailable and native
        summarizes in place; the session is never recycled."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["soft", "native"]
        assert any("compaction method soft: unavailable" in r.getMessage() for r in caplog.records)
        assert calls == [(KEY, 92.0, True, "compacted", "native")]

    @pytest.mark.asyncio
    async def test_soft_on_a_surface_the_writer_does_not_support_hands_straight_to_native(
        self, cfg, caplog
    ) -> None:
        """The one registered writer holds no transcript for this key (a cron
        job with no tab, a hook, a subagent, a channel-born session): the
        dashboard replay is the only thing that re-seeds a soft successor.
        Asked BEFORE the projection, answered False, the method is unavailable
        and native keeps the history."""
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        writer = _Writer(supports=False)
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()
        assert writer.asked == [KEY]
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["soft", "native"]
        assert any("compaction method soft: unavailable" in r.getMessage() for r in caplog.records)
        assert calls == [(KEY, 92.0, True, "compacted", "native")]

    @pytest.mark.asyncio
    async def test_soft_without_a_seed_writer_is_unavailable_at_any_window(
        self, cfg, caplog
    ) -> None:
        """Availability is a property of the surface and is answered before the
        projection: at a window where the tail alone would not clear the band,
        a no-writer soft still says unavailable, never insufficient, so the log
        names the reason that would have held at every window."""
        cfg.session.compaction_method = "soft"
        cfg.session.autocompact_pct = 15.0
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.shutdown.assert_not_awaited()
            provider.stream_command.assert_called_once_with("/compact")
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("compaction method soft: unavailable" in m for m in messages)
        assert not any("compaction method soft: insufficient" in m for m in messages)

    @pytest.mark.asyncio
    async def test_soft_falls_through_to_native_when_the_window_is_unknown(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_soft_falls_through_when_the_tail_would_not_clear_the_threshold(
        self, cfg
    ) -> None:
        """A small window: the replay tail plus startup context leaves the successor in the band.

        The carried tail is the replay's own budget for the window (~4K tokens
        at 100K, floored) and the startup block ~8K more, so the successor would
        start near 12%: inside a 15% threshold's 5-point margin.
        """
        cfg.session.compaction_method = "soft"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer()
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert writer.asked == [KEY], "the surface was asked before the projection"

    @pytest.mark.asyncio
    async def test_native_always_runs_last_when_every_rotation_steps_aside(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    async def test_a_held_turn_defers_the_whole_compaction(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "soft"
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.01)
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            provider, session, _ = await _live_session(mgr)
            await session.semaphore.acquire()
            try:
                assert await mgr.compact_if_needed(KEY) == "busy"
            finally:
                session.semaphore.release()

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions

    @pytest.mark.asyncio
    async def test_each_attempted_method_logs_its_outcome(self, cfg, caplog) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.compact_if_needed(KEY)
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("compaction method soft: unavailable" in m for m in messages)
        assert any(
            "compaction method native: ok" in m for m in messages
        ), "native is an attempted method too and logs its outcome"

    @pytest.mark.asyncio
    async def test_a_raising_rotation_still_reaches_native(self, cfg, caplog) -> None:
        # The promise is that a session at the threshold is always compacted: a
        # rotation that raises (a surface hook that fails) is one more way of
        # stepping aside, not the end of the walk.
        cfg.session.compaction_method = "soft"
        writer = _Writer(raises=OSError("slot registry unreadable"))
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert any(
                "compaction method soft raised" in r.getMessage() for r in caplog.records
            ), "the raise is logged, not swallowed"
            assert not spy.locked()

    @pytest.mark.asyncio
    async def test_a_raise_inside_the_rotation_ends_in_native(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory()) as mgr:
            await _live_session(mgr)
            coordinator = mgr._compaction

            async def _boom(method, key, session, pct):
                raise RuntimeError(f"{method} exploded")

            monkeypatch.setattr(coordinator, "_rotate", _boom)
            in_place = AsyncMock(return_value="compacted")
            monkeypatch.setattr(mgr, "_compact_in_place", in_place)

            assert await mgr.compact_if_needed(KEY) == "compacted"

            in_place.assert_awaited_once()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"

    @pytest.mark.asyncio
    async def test_a_raise_after_the_successor_started_fails_without_native(
        self, cfg, monkeypatch
    ) -> None:
        """The successor is registered, then the restart raises (a recycle whose
        shutdown fails). The key then maps to another session than the one the
        walk began with, so native must not run on it: the walk reports failed, as the
        native restart does when it raises."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            _, session, spy = await _live_session(mgr)
            coordinator = mgr._compaction
            real_restart = coordinator._restart_held

            async def _restart_then_raise(key, held, pct):
                assert await real_restart(key, held, pct) == "recycled"
                raise RuntimeError("recycle shutdown failed")

            monkeypatch.setattr(coordinator, "_restart_held", _restart_then_raise)
            in_place = AsyncMock(return_value="compacted")
            monkeypatch.setattr(mgr, "_compact_in_place", in_place)

            assert await mgr.compact_if_needed(KEY) == "failed"

            in_place.assert_not_awaited()
            assert mgr._sessions[KEY] is not session, "the successor keeps the key"
            assert KEY not in coordinator.state.rotating
            assert not spy.locked()


class TestRotationVerdict:
    """A rotation is judged before it runs, by a projection; the successor's
    first confirmed reading is the check after the fact, through the same
    deferred verdict the native path uses."""

    @pytest.mark.asyncio
    async def test_a_rotation_arms_the_deferred_verdict(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            state = mgr._compaction.state
            assert state.pending_verdict[KEY] == 92.0, "the pre-rotation reading is the baseline"

    @pytest.mark.asyncio
    async def test_an_effective_rotation_settles_clean_on_the_successor(self, cfg) -> None:
        """The successor starts well under the threshold: the verdict settles
        and no cooldown is armed."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=30.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            await _live_session(mgr)  # the successor's first turn
            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict
            assert KEY not in state.cooldown_until

    @pytest.mark.asyncio
    async def test_an_ineffective_rotation_cools_down_then_may_rotate_again(self, cfg) -> None:
        """The successor's first confirmed reading sits inside the band the
        projection said it would clear. That reading includes the successor's
        first turn, so it only damps: the verdict arms the failure cooldown, as
        it does for native, and the walk after the cooldown rotates again."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            successor, _, spy = await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "cooldown"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict
            assert state.cooldown_until[KEY] > 0
            _cross_again(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled", "the rotation runs again"
            successor.stream_command.assert_not_called()
            successor.shutdown.assert_awaited_once()
            assert state.pending_verdict[KEY] == 92.0
            assert not spy.locked()


def _cross_again(mgr: SessionManager) -> Any:
    """Expire the cooldown and put the live session back over the threshold.

    The provider reads 92% until its next in-place compaction completes, then
    an unconfirmed 0%, as ``_provider_factory`` does for a first crossing.
    """
    state = mgr._compaction.state
    state.cooldown_until.pop(KEY, None)
    state.pending_verdict.pop(KEY, None)
    provider = mgr._sessions[KEY].provider
    compacted = {"done": False}
    provider.context_usage_pct = lambda: 0.0 if compacted["done"] else 92.0
    provider.context_usage_unknown = lambda: compacted["done"]

    async def _wait(timeout=None):
        compacted["done"] = True
        return {"type": "completed"}

    provider.wait_for_compaction = AsyncMock(side_effect=_wait)
    return provider


class TestSeedWriterRegistration:
    @pytest.mark.asyncio
    async def test_replacing_a_registered_writer_warns(self, cfg, caplog) -> None:
        async with _managed(cfg, _provider_factory()) as mgr:
            first = _Writer()
            second = _Writer()
            mgr.set_compaction_seed_writer(first)
            with caplog.at_level("WARNING", logger="kiro_crew.session"):
                mgr.set_compaction_seed_writer(second)
            assert mgr._compaction.state.seed_writer is second
        assert any("seed writer already registered" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_clearing_the_writer_does_not_warn(self, cfg, caplog) -> None:
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(_Writer())
            with caplog.at_level("WARNING", logger="kiro_crew.session"):
                mgr.set_compaction_seed_writer(None)
            assert mgr._compaction.state.seed_writer is None
        assert not [r for r in caplog.records if "seed writer" in r.getMessage()]
