"""Automatic compaction is gated on backend capability (#7812).

The defect these pin, and it is worse than the manual one #7800 fixed because
no user action is needed to reach it: a KAS session crossing
``effective_autocompact_pct`` fell through ``_compact_session``'s
claude-vs-everything-else branch into ``_compact_in_place``, which ACQUIRED THE
TURN SEMAPHORE, streamed the ``/compact`` prompt KAS treats as ordinary text,
and then waited for a compaction status KAS never sends for a client-initiated
compaction -- its ``summarization_*`` frames fire only for KAS's own
auto-summarization. The outer backstop burned the whole
``COMPACT_WAIT_TIMEOUT_SECS`` budget with the semaphore still held, and the
resulting timeout took the ``_recycle_held`` path: session popped, resume sid
cleared, provider shut down. So the user lost the live conversation as well as
five minutes of it.

The fix reads the capability #7800 introduced from the gate ladder instead, and
that placement is the substance of it rather than an implementation detail:
declining in ``_compaction_gate_decision`` happens before ``_compact_session``
is ever scheduled, so there is no ``/compact`` dispatch, no ``compacting``
entry, no background task, and no semaphore acquisition at all -- as opposed to
a check inside ``_compact_in_place``, which could only shorten a hold it had
already taken.

Declining rather than recycling sooner is deliberate. KAS summarizes on its own
initiative and its ``summarization_completed`` frame calls
``reset_after_compaction()`` on the meter, so the reading that opened this gate
falls back below the threshold without us acting -- the same relationship
``cc_managed`` already encodes for Claude-Code sessions. Recycling faster would
have kept the half of the defect that destroys the conversation.

Found by the Opus review lane during the #7800 drive.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KAS,
    ACP_BACKENDS_COMPACT,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.session import SessionManager
from kiro_crew.session_compaction import _compact_unsupported_backend

KEY = "dashboard:auto-compact-gate"


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
    """Manager whose teardown always runs, even on a failed assert.

    An async CONTEXT MANAGER rather than an async fixture: the CI-pinned
    ``pytest-asyncio==0.20.3`` is incompatible with pytest 8 for async
    fixtures, so the suite avoids the decorator by convention.
    """
    mgr = SessionManager(cfg, provider_factory=provider_factory)
    try:
        yield mgr
    finally:
        await _drain_background(mgr)
        await mgr.close_all()


def _provider_factory(
    *,
    pct: float = 92.0,
    pct_after: float = 0.0,
    unknown: bool = False,
    unsupported: Any = ACP_BACKEND_KAS,
):
    """Provider at *pct* naming *unsupported* as its uncompactable backend.

    ``unsupported`` is passed through untouched so a test can hand over a
    non-string (the shape a bare mock produces) as well as a backend id.
    """

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.is_process_alive = lambda: True
        m.has_active_turn = lambda: False
        state = {"compacted": False}
        m.context_usage_pct = lambda: pct_after if state["compacted"] else pct
        # kiro-cli's real shape: the terminal status arrives async and
        # reset_after_compaction leaves the fresh stats unconfirmed.
        m.context_usage_unknown = lambda: (True if state["compacted"] else unknown)
        m.manual_compact_unsupported_backend = unsupported

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
    """Records acquisitions while behaving exactly like the real semaphore.

    Proving "the semaphore was never held" by timing would be flaky and
    proving it by ``locked()`` afterwards proves nothing -- a completed
    acquire/release pair reads unlocked too. Counting acquisitions is the
    only assertion that separates "never taken" from "taken and given back".
    """

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


class TestAutoCompactDeclinesAnUncompactableBackend:
    @pytest.mark.asyncio
    async def test_no_dispatch_no_semaphore_no_recycle(self, cfg) -> None:
        """RED on main: this dispatched /compact, held the semaphore for the
        whole wait budget, and then recycled the session."""
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, session, spy = await _live_session(mgr)
            callback = AsyncMock()
            mgr.set_compact_callback(callback)

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

            provider.stream_command.assert_not_called()
            provider.wait_for_compaction.assert_not_awaited()
            provider.compact.assert_not_awaited()
            assert spy.acquires == 0, "the wait budget was never entered"
            # Not recycled: same session object, provider still alive, sid intact.
            assert mgr._sessions.get(KEY) is session
            provider.shutdown.assert_not_awaited()
            # A decline is not a failure: no cooldown, no failure callback, and
            # no reinjection flag (nothing was compacted to reinject after).
            assert KEY not in mgr._compact_cooldown_until
            callback.assert_not_awaited()
            assert not session.needs_context_reinjection

    @pytest.mark.asyncio
    async def test_trigger_path_schedules_no_compaction_task(self, cfg) -> None:
        """The fire-and-forget entry point declines identically -- a gate added
        to the awaited seam alone would leave the path that actually fires in
        production (``check_context_usage`` on every turn) still broken."""
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, spy = await _live_session(mgr)
            before = len(mgr._background_tasks)

            assert mgr._trigger_compaction(KEY, "test", 92.0, provider) == "compact_unsupported"

            assert len(mgr._background_tasks) == before, "no compaction task scheduled"
            assert KEY not in mgr._compacting, "the dedup commit never happened"
            provider.stream_command.assert_not_called()
            assert spy.acquires == 0

    @pytest.mark.asyncio
    async def test_turn_end_check_dispatches_nothing_even_after_draining(self, cfg) -> None:
        """``check_context_usage`` is what runs on every turn, and it schedules
        the compaction rather than awaiting it -- so this DRAINS the background
        tasks before asserting. Without the drain the assertion passes whether
        or not the gate exists (measured: the mutation that removes the rung
        left this test green), which would make it a test of nothing.

        Deliberately asserts no outcome string: the pin here is the absence of
        the dispatch and of the semaphore hold, so this is the test that fails
        on that assertion rather than on a decline value."""
        async with _managed(cfg, _provider_factory(pct=92.0)) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert mgr.check_context_usage(KEY, provider) == 92.0
            await _drain_background(mgr)

            provider.stream_command.assert_not_called()
            provider.wait_for_compaction.assert_not_awaited()
            assert spy.acquires == 0
            assert mgr._sessions.get(KEY) is not None, "not recycled"

    @pytest.mark.asyncio
    async def test_declined_repeatedly_without_arming_a_cooldown(self, cfg) -> None:
        """The decline is a standing property of the backend, not a failure to
        damp: a second crossing must reach the same answer rather than be
        masked by a failure cooldown that a recycle-instead fix would have
        armed."""
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"
            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

            assert KEY not in mgr._compact_cooldown_until
            assert spy.acquires == 0
            provider.stream_command.assert_not_called()


class TestGateOrder:
    """Where the rung sits is load-bearing in both directions."""

    @pytest.mark.asyncio
    async def test_below_threshold_still_wins(self, cfg) -> None:
        """Above the threshold check the new rung would swallow the ordinary
        below-threshold decline, and with it the per-turn context-usage log
        ``check_context_usage`` emits for that branch."""
        async with _managed(cfg, _provider_factory(pct=10.0)) as mgr:
            provider, _, _ = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "below_threshold"

    @pytest.mark.asyncio
    async def test_capability_wins_over_unconfirmed_telemetry(self, cfg) -> None:
        """An uncompactable backend is uncompactable whether or not its
        percentage is confirmed, so the standing reason is reported in
        preference to the transient one."""
        async with _managed(cfg, _provider_factory(unknown=True)) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"
            assert spy.acquires == 0

    @pytest.mark.asyncio
    async def test_cooldown_from_an_earlier_era_does_not_hide_the_reason(self, cfg) -> None:
        """A session carrying a cooldown from before the fix (or from a backend
        switch) still reports the capability, so the diagnosis in the log is
        the real one."""
        async with _managed(cfg, _provider_factory()) as mgr:
            provider, _, _ = await _live_session(mgr)
            mgr._compact_cooldown_until[KEY] = time.monotonic() + 999

            assert await mgr.compact_if_needed(KEY) == "compact_unsupported"


class TestDeclineIsObservable:
    """A correct decision made SILENTLY is its own defect, so the decline's
    one operator-facing signal is a contract, not an incidental.

    It also has to be the SAME channel the existing declines use rather than a
    second spelling of one: ``unconfirmed``, ``in_progress`` and ``cooldown``
    all log from inside the gate, which is what this asserts.
    """

    @pytest.mark.asyncio
    async def test_the_decline_logs_the_reading_and_the_backend(self, cfg, caplog) -> None:
        async with _managed(cfg, _provider_factory(pct=92.0)) as mgr:
            provider, _, _ = await _live_session(mgr)
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

        lines = [r.getMessage() for r in caplog.records]
        hit = [m for m in lines if "manages compaction itself" in m]
        assert hit, f"the decline produced no operator-facing line: {lines!r}"
        # Carries strictly more than cc_managed's line: the key, the reading,
        # and WHICH backend answered -- without the backend id the operator
        # cannot tell this decline from a misconfiguration.
        assert KEY in hit[0]
        assert "92" in hit[0]
        assert ACP_BACKEND_KAS in hit[0]

    @pytest.mark.asyncio
    async def test_the_decline_is_not_logged_as_an_anomaly(self, cfg, caplog) -> None:
        """A standing, correct condition must not page anyone: no WARNING and no
        ERROR on the declined path. On main this same crossing emitted a
        compacting WARNING, a failed-compact WARNING with a traceback, and a
        recycle -- so the absence of those IS the fix, and pinning it stops a
        later change from reintroducing the noise."""
        async with _managed(cfg, _provider_factory(pct=92.0)) as mgr:
            provider, _, _ = await _live_session(mgr)
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="kiro_crew.session"):
                assert await mgr.compact_if_needed(KEY) == "compact_unsupported"

        loud = [
            (r.levelname, r.getMessage())
            for r in caplog.records
            if r.levelno >= logging.WARNING and "compact" in r.getMessage().lower()
        ]
        assert not loud, f"the declined path should be quiet, got: {loud!r}"


class TestMemberBackendsUnchanged:
    @pytest.mark.asyncio
    async def test_kiro_in_place_compact_still_dispatches(self, cfg) -> None:
        """A member backend keeps the pre-fix path exactly: the property
        answers None, ``/compact`` is streamed under the semaphore, and the
        async status wait settles it."""
        async with _managed(cfg, _provider_factory(unsupported=None)) as mgr:
            provider, _, spy = await _live_session(mgr)

            assert await mgr.compact_if_needed(KEY) == "ok"

            provider.stream_command.assert_called_once_with("/compact")
            provider.wait_for_compaction.assert_awaited()
            assert spy.acquires == 1, "the in-place path still excludes turns"

    @pytest.mark.asyncio
    async def test_claude_native_compact_still_dispatches(self, cfg, monkeypatch) -> None:
        """The claude branch of ``_compact_session`` calls ``provider.compact()``
        rather than streaming a prompt, and the gate must not intercept it."""
        async with _managed(cfg, _provider_factory(unsupported=None)) as mgr:
            provider, _, spy = await _live_session(mgr)
            monkeypatch.setattr("kiro_crew.session._is_claude_backend", lambda p: True)

            assert await mgr.compact_if_needed(KEY) in ("ok", "reset")

            provider.compact.assert_awaited_once()
            provider.stream_command.assert_not_called()
            assert spy.acquires == 1

    def test_gate_cannot_decline_a_real_member_provider(self) -> None:
        """Read through the real provider rather than a double: every member of
        ``ACP_BACKENDS_COMPACT`` answers None, so the rung is unreachable for
        kiro-cli and claude alike."""
        assert ACP_BACKEND_CLAUDE in ACP_BACKENDS_COMPACT
        for backend in ACP_BACKENDS_COMPACT:
            assert _compact_unsupported_backend(AcpProvider(acp_backend=backend)) is None

    def test_gate_declines_a_real_kas_provider(self) -> None:
        """The other half of the same read: a non-member real provider names
        itself, so the rung is reachable for exactly the backend that needs it."""
        provider = AcpProvider(acp_backend=ACP_BACKEND_KAS)
        assert _compact_unsupported_backend(provider) == ACP_BACKEND_KAS


class TestCapabilityReadContract:
    """Only a positively named backend id declines -- the ABC says so, and the
    compaction suite depends on it."""

    def test_absent_attribute_passes(self) -> None:
        """A provider predating the capability (and every ``object()`` the
        existing gate tests pass) inherits no refusal."""
        assert _compact_unsupported_backend(object()) is None

    def test_bare_mock_attribute_is_not_a_refusal(self) -> None:
        """The load-bearing case: the compaction suite's providers are bare
        ``AsyncMock``/``MagicMock`` objects whose attribute read returns a
        truthy child mock. A truthiness test here would decline compaction for
        every one of them and silently disable the tests that pin the
        in-place path."""
        assert _compact_unsupported_backend(MagicMock()) is None
        assert _compact_unsupported_backend(AsyncMock()) is None

    def test_none_and_empty_string_pass(self) -> None:
        """``None`` is the ABC default. The empty string is the KIRO backend's
        own id -- a member -- so it must never read as a refusal either."""
        none_provider = MagicMock(manual_compact_unsupported_backend=None)
        empty_provider = MagicMock(manual_compact_unsupported_backend="")
        assert _compact_unsupported_backend(none_provider) is None
        assert _compact_unsupported_backend(empty_provider) is None

    def test_named_backend_is_a_refusal(self) -> None:
        provider = MagicMock(manual_compact_unsupported_backend=ACP_BACKEND_KAS)
        assert _compact_unsupported_backend(provider) == ACP_BACKEND_KAS
