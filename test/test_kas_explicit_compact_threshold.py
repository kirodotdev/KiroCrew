"""An explicit compaction threshold is honoured on a harness-managed backend.

KAS summarizes on its own (``ACP_BACKENDS_HARNESS_MANAGED_COMPACTION``) and also
serves a manual ``/compact`` (``ACP_BACKENDS_COMPACT``). At the global
``session.autocompact_pct`` the gate leaves it to its own summarization. A
threshold chosen for the session (the slider's override) or declared by its agent
(``agents.<name>.autocompact_pct``) is Crew's to apply, so the gate compacts there
through the ordinary in-place path, with the later rungs still in force.

Invariant: with an explicit threshold a compactable harness-managed backend is
compacted at that threshold exactly once per crossing; without one it is declined
as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until, finished

KEY = "dashboard:kas-explicit-threshold"
CREW = "orch"


@pytest.fixture
def cfg() -> KiroCrewConfig:
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    c.session.autocompact_pct = 90.0
    return c


class _Kas:
    """Knobs one test turns on its KAS-shaped provider."""

    def __init__(self, pct: float) -> None:
        self.pct = pct
        #: When set, the ``/compact`` stream parks on it after its first frame,
        #: which keeps the compaction in flight until the test releases it.
        self.hold: asyncio.Event | None = None
        self.parked = asyncio.Event()
        #: Compaction status frames the ``/compact`` stream yields, in order.
        self.statuses: list[str] = ["completed"]
        #: Whether the engine listed ``_kiro/session/compact`` at ``initialize``.
        self.verb_advertised = True


def _factory(kas: _Kas):
    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        m.context_usage_pct = lambda: kas.pct
        m.context_usage_unknown = lambda: False
        # KAS's capability answers, set explicitly: a bare mock attribute would
        # read as no answer at all.
        m.manual_compact_unsupported_backend = None
        m.harness_managed_compaction_backend = ACP_BACKEND_KAS
        m.compaction_unmanaged_backend = None
        m.compaction_self_managed = True
        m.harness_compact_verb_advertised = kas.verb_advertised

        async def _stream(_cmd):
            for i, status in enumerate(kas.statuses):
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status)
                if i == 0 and kas.hold is not None:
                    kas.parked.set()
                    await kas.hold.wait()
            kas.pct = 5.0

        m.stream_command = MagicMock(side_effect=_stream)
        m.wait_for_compaction = AsyncMock(return_value={"type": "completed"})
        return m

    return factory


async def _drain_background(mgr: SessionManager) -> None:
    """Await the manager's background tasks under the lost-run bound.

    A compaction that never finishes fails here by name, rather than waiting out
    its own backstop past the runner's per-test timeout. ``finished`` cancels the
    gather when the bound expires, which cancels the tasks it holds.
    """
    pending = [t for t in mgr._background_tasks if not t.done()]
    if pending:
        await finished(
            asyncio.gather(*pending, return_exceptions=True),
            what="the manager's background compaction tasks",
        )


@contextlib.asynccontextmanager
async def _managed(cfg: KiroCrewConfig, kas: _Kas):
    """Manager on *kas*'s provider whose teardown always runs, even on a failed assert.

    Teardown releases a ``/compact`` stream parked on ``kas.hold`` and then closes
    the manager, whose ``close_all`` cancels and awaits every background task. A
    test that fails before releasing the hold, or while a compaction hangs, is
    torn down at once rather than after a second wait. Tests that need a
    compaction to finish drain it themselves.
    """
    mgr = SessionManager(cfg, provider_factory=_factory(kas))
    try:
        yield mgr
    finally:
        if kas.hold is not None:
            kas.hold.set()
        await mgr.close_all()


async def _live(mgr: SessionManager) -> Any:
    provider, _, _ = await mgr.get_or_create(KEY)
    mgr.release(KEY)
    return provider


def _declare_agent_pct(cfg: KiroCrewConfig, mgr: SessionManager, pct: float) -> None:
    cfg.agents = {CREW: KiroCrewAgentConfig(kiro_agent="kirocrew", autocompact_pct=pct)}
    mgr._sessions[KEY].capability_member = CREW


class TestExplicitThresholdCompacts:
    @pytest.mark.asyncio
    async def test_override_compacts_above_it(self, cfg) -> None:
        kas = _Kas(21.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            callback.assert_awaited_once()
            assert callback.await_args.kwargs["success"] is True

    @pytest.mark.asyncio
    async def test_override_does_not_compact_below_it(self, cfg) -> None:
        kas = _Kas(19.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)

            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_turn_end_trigger_compacts_at_the_override(self, cfg) -> None:
        """The path that runs on every turn, drained before asserting."""
        kas = _Kas(21.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)

            assert mgr.compaction_due_after_turn(KEY, provider) is True
            mgr.check_context_usage(KEY, provider)
            await _drain_background(mgr)

            provider.stream_command.assert_called_once_with("/compact")
            assert KEY not in mgr._compacting

    @pytest.mark.asyncio
    async def test_agent_declared_threshold_compacts_above_it(self, cfg) -> None:
        kas = _Kas(31.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            _declare_agent_pct(cfg, mgr, 30.0)

            assert await mgr.compact_if_needed(KEY) == "ok"
            provider.stream_command.assert_called_once_with("/compact")


class TestGlobalThresholdUnchanged:
    @pytest.mark.asyncio
    async def test_no_explicit_threshold_declines(self, cfg, caplog) -> None:
        """Global 90, no override, no agent default: KAS keeps its own summarization."""
        kas = _Kas(91.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

            provider.stream_command.assert_not_called()
            assert mgr.compaction_due_after_turn(KEY, provider) is False
        loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert not loud, [r.getMessage() for r in loud]

    @pytest.mark.asyncio
    async def test_below_the_global_is_below_threshold(self, cfg) -> None:
        kas = _Kas(85.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)

            assert await mgr.compact_if_needed(KEY) == "below_threshold"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_clearing_the_override_restores_the_decline(self, cfg) -> None:
        kas = _Kas(91.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            mgr.set_autocompact_pct(KEY, None)

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"
            provider.stream_command.assert_not_called()


class TestLaterRungsStillApply:
    @pytest.mark.asyncio
    async def test_cooldown_declines(self, cfg) -> None:
        kas = _Kas(21.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999

            assert await mgr.compact_if_needed(KEY) == "cooldown"
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_progress_declines(self, cfg) -> None:
        kas = _Kas(21.0)
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            mgr._compacting.add(KEY)
            try:
                assert await mgr.compact_if_needed(KEY) == "in_progress"
            finally:
                mgr._compacting.discard(KEY)
            provider.stream_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_summarization_during_crew_compaction_compacts_once(self, cfg) -> None:
        """KAS's own ``summarization_completed`` lands while Crew's ``/compact`` is
        in flight, and a reading is taken in between: one compaction is recorded
        and ``compacting`` is empty afterwards."""
        kas = _Kas(21.0)
        kas.hold = asyncio.Event()
        kas.statuses = ["completed", "completed"]
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert mgr._trigger_compaction(KEY, "test", 21.0, provider) is None
            await await_until(kas.parked, what="the /compact stream to park")

            assert mgr._trigger_compaction(KEY, "again", 21.0, provider) == "in_progress"
            kas.hold.set()
            await _drain_background(mgr)

            provider.stream_command.assert_called_once_with("/compact")
            callback.assert_awaited_once()
            assert callback.await_args.kwargs["success"] is True
            assert KEY not in mgr._compacting


class TestFailedCompactionKeepsTheSession:
    @pytest.mark.asyncio
    async def test_failed_status_arms_cooldown_without_recycling(self, cfg) -> None:
        """KAS refuses the verb while it summarizes. The harness still bounds the
        context, so the session and its conversation stay."""
        kas = _Kas(21.0)
        kas.statuses = ["failed"]
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            session = mgr._sessions[KEY]
            mgr.set_autocompact_pct(KEY, 20.0)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert await mgr.compact_if_needed(KEY) == "failed"

            assert mgr._sessions.get(KEY) is session, "not recycled"
            provider.shutdown.assert_not_awaited()
            assert mgr._compact_cooldown_until[KEY] > time.monotonic()
            callback.assert_awaited_once()
            assert callback.await_args.kwargs["success"] is False
            assert KEY not in mgr._compacting
            assert not session.semaphore.locked()
            # A reported status means KAS is done with this ``/compact``.
            assert mgr.consume_needs_reinjection(KEY) is False

    @pytest.mark.asyncio
    async def test_timed_out_wait_rearms_the_startup_context(self, cfg) -> None:
        """The wait for a status times out, so KAS can still finish the ``/compact``
        after the next prompt is built: that prompt carries the startup context."""
        kas = _Kas(21.0)
        kas.statuses = []
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            provider.wait_for_compaction.return_value = {"type": "timeout"}
            session = mgr._sessions[KEY]
            mgr.set_autocompact_pct(KEY, 20.0)

            assert await mgr.compact_if_needed(KEY) == "failed"

            assert mgr._sessions.get(KEY) is session, "not recycled"
            assert mgr.consume_needs_reinjection(KEY) is True

    @pytest.mark.asyncio
    async def test_threshold_cleared_mid_compaction_still_keeps_the_session(self, cfg) -> None:
        """The slider is reset while ``/compact`` runs, then KAS fails it."""
        kas = _Kas(21.0)
        kas.hold = asyncio.Event()
        kas.statuses = ["started", "failed"]
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            session = mgr._sessions[KEY]
            mgr.set_autocompact_pct(KEY, 20.0)

            assert mgr._trigger_compaction(KEY, "test", 21.0, provider) is None
            await await_until(kas.parked, what="the /compact stream to park")
            mgr.set_autocompact_pct(KEY, None)
            kas.hold.set()
            await _drain_background(mgr)

            assert mgr._sessions.get(KEY) is session, "not recycled"
            provider.shutdown.assert_not_awaited()
            assert KEY not in mgr._compacting


class TestUnadvertisedVerb:
    @pytest.mark.asyncio
    async def test_override_declines_when_the_engine_lacks_the_verb(self, cfg) -> None:
        """An engine that did not list ``_kiro/session/compact`` at ``initialize``
        fails every ``/compact`` at once. The explicit threshold is declined as at
        the global one, so no failed compaction repeats after each cooldown."""
        kas = _Kas(21.0)
        kas.verb_advertised = False
        async with _managed(cfg, kas) as mgr:
            provider = await _live(mgr)
            mgr.set_autocompact_pct(KEY, 20.0)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert mgr.compaction_due_after_turn(KEY, provider) is False
            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

            provider.stream_command.assert_not_called()
            callback.assert_not_awaited()
            assert KEY not in mgr._compacting


class TestProviderAnswersTheAdvertisement:
    """The two ACP providers answer ``harness_compact_verb_advertised`` from the KAS
    handle's ``initialize`` record, and only a literal ``True`` passes."""

    @staticmethod
    def _session_provider(backend: str, advertised: Any) -> Any:
        from kiro_crew.acp.session_provider import AcpSessionProvider

        provider = object.__new__(AcpSessionProvider)
        provider._runtime = SimpleNamespace(acp_backend=backend)
        provider._handle = SimpleNamespace(_kas_compact_advertised=lambda: advertised)
        return provider

    def test_session_provider_reads_the_kas_handle(self) -> None:
        assert self._session_provider(ACP_BACKEND_KAS, True).harness_compact_verb_advertised
        assert not self._session_provider(ACP_BACKEND_KAS, False).harness_compact_verb_advertised
        assert not self._session_provider(
            ACP_BACKEND_KAS, MagicMock()
        ).harness_compact_verb_advertised

    def test_session_provider_answers_false_for_another_backend(self) -> None:
        assert not self._session_provider(ACP_BACKEND_KIRO, True).harness_compact_verb_advertised

    def test_wrapper_forwards_only_a_real_true(self) -> None:
        from kiro_crew.providers.acp import AcpProvider

        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert provider.harness_compact_verb_advertised is False
        provider._client = SimpleNamespace(harness_compact_verb_advertised=True)
        assert provider.harness_compact_verb_advertised is True
        provider._client = MagicMock()
        assert provider.harness_compact_verb_advertised is False
