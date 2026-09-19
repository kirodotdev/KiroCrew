"""The compaction method ladder (``CompactionCoordinator._run_method_ladder``).

``session.compaction_method`` sets the ceiling walked down when the auto-compact threshold
fires. ``native`` is the in-place ``/compact`` the coordinator always ran; a
rotation method recycles the session on purpose so the successor is re-seeded
from the transcript, and hands the turn on when it cannot run (``unavailable``)
or would not clear the threshold (``insufficient``). The first test pins the
contract everything else rests on: with the default method, dispatch is the
pre-ladder dispatch exactly, and no rotation seam is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager

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
    """A ``SeedWriter`` fake: ``supports`` answers *supports*, the write answers *answer*.

    The write is an ``AsyncMock`` so a test can assert whether, and with what,
    the ladder awaited it; *side_effect* makes the write raise instead.
    """

    def __init__(
        self, answer: str = "written", *, supports: bool = True, side_effect: Any = None
    ) -> None:
        self.write = AsyncMock(return_value=answer, side_effect=side_effect)
        self.supported = supports
        self.asked: list[str] = []

    def supports(self, key: str) -> bool:
        self.asked.append(key)
        return self.supported

    async def __call__(self, key: str, method: str, window_tokens: int) -> str:
        return await self.write(key, method, window_tokens)


def _provider_factory(
    *, pct: float = 92.0, window: int = 1_000_000, successor_pct: float | None = None
):
    """Provider at *pct* on a *window*-token model that compacts in place on request.

    ``context_window_tokens`` is a sync ``MagicMock`` (not an ``AsyncMock``
    attribute, which would hand the ladder a coroutine) so a test can both set
    the window and assert whether the ladder read it. *successor_pct*, when
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


def _callback_recorder() -> tuple[list[tuple[str, float, bool]], Any]:
    calls: list[tuple[str, float, bool]] = []

    async def _cb(key: str, pct: float, *, success: bool) -> None:
        calls.append((key, pct, success))

    return calls, _cb


class TestDefaultOrderIsNative:
    @pytest.mark.asyncio
    async def test_native_only_dispatches_in_place_and_reads_no_rotation_seam(self, cfg) -> None:
        """The default order is ``native`` alone: ``/compact`` is streamed under
        the semaphore, the window is never read, and no seed writer is consulted."""
        assert cfg.session.compaction_method == "native"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.context_window_tokens.assert_not_called()
            assert spy.acquires == 1
            assert KEY in mgr._sessions, "an in-place compaction keeps the session"


class TestSoft:
    @pytest.mark.asyncio
    async def test_soft_recycles_without_a_model_call(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        calls, cb = _callback_recorder()
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compact_callback(cb)
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            assert KEY not in mgr._sessions, "the successor is created lazily"
            assert spy.acquires == 1, "rotation excludes turns like the in-place path"
            assert not spy.locked(), "the semaphore is released after the recycle"
            assert calls == [(KEY, 92.0, True)]

    @pytest.mark.asyncio
    async def test_soft_falls_through_to_native_when_the_window_is_unknown(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
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
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_native_always_runs_last_when_every_rotation_steps_aside(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    async def test_a_held_turn_defers_the_whole_ladder(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "soft"
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.01)
        async with _managed(cfg, _provider_factory()) as mgr:
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
            await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                await mgr.compact_if_needed(KEY)
        messages = [rec.getMessage() for rec in caplog.records]
        assert any("compaction method soft: unavailable" in m for m in messages)
        assert any(
            "compaction method native: ok" in m for m in messages
        ), "native is an attempted method too and logs its outcome"

    @pytest.mark.asyncio
    async def test_an_unavailable_shake_hands_straight_to_native(self, cfg, caplog) -> None:
        """No window figure: shake cannot write a digest, and the walk does not
        try soft in its place (soft would drop the pre-tail history the operator
        asked to keep); native runs next."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory(window=0)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]
        provider.stream_command.assert_called_once_with("/compact")

    @pytest.mark.asyncio
    async def test_an_insufficient_shake_degrades_through_soft_to_native(self, cfg, caplog) -> None:
        """One scalar expresses the whole ladder when each rotation would not
        clear the threshold: shake (tail + digest share) steps aside, soft
        (tail alone, at a 100K window still inside the 15% band) steps aside,
        native runs."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer()
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "soft", "native"]
        writer.write.assert_not_awaited()
        assert writer.asked == [KEY], "the surface was asked before the projection"
        provider.stream_command.assert_called_once_with("/compact")


class TestRotationVerdict:
    """A rotation is judged before it runs, by a projection; the successor's
    first confirmed reading is the check after the fact, through the same
    deferred verdict the native path uses."""

    @pytest.mark.asyncio
    async def test_a_rotation_arms_the_deferred_verdict(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory()) as mgr:
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            state = mgr._compaction.state
            assert state.pending_verdict[KEY] == 92.0, "the pre-rotation reading is the baseline"
            assert state.pending_rotation[KEY] == "soft"
            assert KEY not in state.rotation_hold

    @pytest.mark.asyncio
    async def test_an_effective_rotation_settles_clean_on_the_successor(self, cfg) -> None:
        """The successor starts well under the threshold: the verdict settles,
        no cooldown is armed, nothing is held."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=30.0)) as mgr:
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            await _live_session(mgr)  # the successor's first turn
            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict
            assert KEY not in state.pending_rotation
            assert KEY not in state.rotation_hold
            assert KEY not in state.cooldown_until

    @pytest.mark.asyncio
    async def test_an_ineffective_rotation_cools_down_then_runs_native(self, cfg, caplog) -> None:
        """The successor starts inside the band the projection said it would
        clear (the projection omits provider-side overhead the real figure
        includes). Rotating again would repeat the same arithmetic every turn,
        so the verdict arms the failure cooldown and the walk after it runs
        native outright, once."""
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            successor, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "cooldown"
            state = mgr._compaction.state
            assert KEY not in state.pending_verdict and KEY not in state.pending_rotation
            assert KEY in state.rotation_hold
            assert state.cooldown_until[KEY] > 0
            assert any(
                "rotation soft left context at 92.0%" in r.getMessage() for r in caplog.records
            )
            # The cooldown expires: the next compaction skips every rotation
            # method and compacts in place, clearing the one-shot hold.
            state.cooldown_until.pop(KEY)
            caplog.clear()
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            successor.stream_command.assert_called_once_with("/compact")
            successor.shutdown.assert_not_awaited()
            assert KEY not in state.rotation_hold
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["native"], "the held walk tries no rotation"
        assert any("last rotation judged ineffective" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_the_hold_is_one_shot(self, cfg) -> None:
        cfg.session.compaction_method = "soft"
        async with _managed(cfg, _provider_factory(successor_pct=92.0)) as mgr:
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "recycled"
            await _live_session(mgr)
            assert await mgr.compact_if_needed(KEY) == "cooldown"
            state = mgr._compaction.state
            state.cooldown_until.pop(KEY)
            assert await mgr.compact_if_needed(KEY) == "ok"  # native, hold consumed
            # The in-place compact settled its own verdict on this provider;
            # a later compaction walks the ladder again.
            state.cooldown_until.pop(KEY, None)
            state.pending_verdict.pop(KEY, None)
            provider = mgr._sessions[KEY].provider
            provider.context_usage_pct = lambda: 92.0
            provider.context_usage_unknown = lambda: False
            assert await mgr.compact_if_needed(KEY) == "recycled"


class TestShake:
    @pytest.mark.asyncio
    async def test_shake_without_a_seed_writer_hands_straight_to_native(self, cfg) -> None:
        """No surface can write a digest here (channels register no writer), so
        the model call runs: the tail-only rotation would drop the pre-tail
        history undigested where users cannot see the loss."""
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shake_without_a_seed_writer_is_unavailable_at_any_window(
        self, cfg, caplog
    ) -> None:
        """Availability is a property of the surface and is answered before the
        projection: at a window where the tail alone would not clear the band,
        a no-writer shake still says unavailable, so soft is skipped."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            provider, _, _ = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.shutdown.assert_not_awaited()
            provider.stream_command.assert_called_once_with("/compact")
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]
        assert any(
            "compaction method shake: unavailable" in m
            for m in (rec.getMessage() for rec in caplog.records)
        )

    @pytest.mark.asyncio
    async def test_shake_advances_to_soft_when_the_writer_saves_nothing(self, cfg) -> None:
        """``nothing`` means nothing is older than the tail the successor
        carries, so a tail-only rotation loses no history: soft may run."""
        cfg.session.compaction_method = "shake"
        writer = _Writer("nothing")
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "recycled"

            writer.write.assert_awaited_once_with(KEY, "shake", 1_000_000)
            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            assert spy.acquires == 2, "shake and soft each take and give back the semaphore"
            assert not spy.locked()

    @pytest.mark.asyncio
    async def test_a_surface_the_writer_does_not_support_hands_straight_to_native(
        self, cfg, caplog
    ) -> None:
        """The one registered writer holds no transcript for this key (a
        channel-born session, a key with no tab): asked BEFORE the projection,
        it answers ``supports`` False, the method is unavailable, soft is
        skipped and the write is never attempted. At this window the shake
        projection is insufficient, so this pin holds only if the surface
        question is asked first: an insufficient projection alone would walk to
        soft with the writer never asked."""
        cfg.session.compaction_method = "shake"
        cfg.session.autocompact_pct = 15.0
        writer = _Writer(supports=False)
        async with _managed(cfg, _provider_factory(window=100_000)) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert not spy.locked()
        assert writer.asked == [KEY]
        writer.write.assert_not_awaited()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"]

    @pytest.mark.asyncio
    async def test_a_surface_gone_at_write_time_hands_to_native_not_soft(self, cfg, caplog) -> None:
        """``supports`` said yes, then the tab closed under the rotation and the
        write answers ``unsupported``: no digest exists, so the walk skips soft
        (which would drop the pre-tail history undigested) and native keeps it."""
        cfg.session.compaction_method = "shake"
        writer = _Writer("unsupported")
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level("INFO", logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "ok"
            writer.write.assert_awaited_once_with(KEY, "shake", 1_000_000)
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()
        attempted = [
            m.split("compaction method ")[1].split(":")[0]
            for m in (rec.getMessage() for rec in caplog.records)
            if "compaction method " in m
        ]
        assert attempted == ["shake", "native"], "soft never ran"
        assert any("compaction method shake: unavailable" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_answer_the_contract_does_not_name_is_read_as_unavailable(self, cfg) -> None:
        """Only the literal ``nothing`` lets the walk reach soft. A writer that
        answers with a truthy stray value (a bare ``True``, a typo) has written
        no digest the coordinator can vouch for, so the fail-safe reading is
        the one that keeps the history: native."""
        cfg.session.compaction_method = "shake"
        writer = _Writer(True)  # type: ignore[arg-type]  # a bool, not a SEED_* answer
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert KEY in mgr._sessions, "no rotation ran, so the session was not recycled"
            assert not spy.locked()

    @pytest.mark.asyncio
    async def test_a_raising_rotation_method_still_reaches_native(self, cfg, caplog) -> None:
        # The promise is that a session at the threshold is always compacted:
        # a rotation that raises (an unreadable transcript under the writer)
        # is one more way of stepping aside, not the end of the walk.
        cfg.session.compaction_method = "shake"
        writer = _Writer(side_effect=OSError("transcript unreadable"))
        async with _managed(cfg, _provider_factory()) as mgr:
            mgr.set_compaction_seed_writer(writer)
            provider, _, spy = await _live_session(mgr)
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
                # shake raises -> no digest could be written -> the walk skips
                # soft (it would drop the pre-tail history undigested) and
                # native, the last step, compacts in place.
                assert await mgr.compact_if_needed(KEY) == "ok"
            writer.write.assert_awaited_once()
            provider.stream_command.assert_called_once_with("/compact")
            provider.shutdown.assert_not_awaited()
            assert any(
                "compaction method shake raised" in r.getMessage() for r in caplog.records
            ), "the raise is logged, not swallowed"
            assert not spy.locked(), "the semaphore is released on the raising path"

    @pytest.mark.asyncio
    async def test_a_raise_in_every_rotation_step_ends_in_native(self, cfg, monkeypatch) -> None:
        cfg.session.compaction_method = "shake"
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, session, spy = await _live_session(mgr)
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
    async def test_shake_writes_the_seed_under_the_semaphore_then_recycles(self, cfg) -> None:
        cfg.session.compaction_method = "shake"
        held_during_write: list[bool] = []
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, session, spy = await _live_session(mgr)

            class _Observing(_Writer):
                async def __call__(self, key: str, method: str, window_tokens: int) -> str:
                    held_during_write.append(spy.locked())
                    assert KEY in mgr._sessions, "the seed is written before the recycle"
                    return "written"

            mgr.set_compaction_seed_writer(_Observing())

            assert await mgr.compact_if_needed(KEY) == "recycled"

            assert held_during_write == [True]
            provider.stream_command.assert_not_called()
            provider.shutdown.assert_awaited_once()
            assert KEY not in mgr._sessions
            assert spy.acquires == 1
            assert not spy.locked()


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
