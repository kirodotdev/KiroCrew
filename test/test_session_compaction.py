"""SessionManager context usage and compaction.

Among them: ``check_context_usage`` thresholds and prompt counting, the compact callback,
``_trigger_compaction`` and ``_compact_session``, in-place compaction on the Claude backend and on
kiro-cli (with recycle as the kiro-cli fallback), a ``get_or_create`` that meets a compaction, the
compact timeout, and the cooldowns after a failed or an ineffective compaction.
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from session_helpers import (  # noqa: F401 -- the fixture is requested by name
    _mock_provider_factory,
    cfg_fixture,
)

from kiro_crew.session import SessionManager
from kiro_crew.testing.wait import await_until


class TestCompactCallback:
    """Tests for the compact callback wiring on SessionManager.

    Covers set_compact_callback registration, pct threading through
    check_context_usage -> _trigger_compaction -> _compact_session, and
    callback fault isolation.
    """

    @pytest.mark.asyncio
    async def test_set_compact_callback_registers_handler(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()

        mgr.set_compact_callback(cb)

        assert mgr._on_compacted is cb
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_none_clears(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        mgr.set_compact_callback(None)

        assert mgr._on_compacted is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_set_compact_callback_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())

        assert any("Compact callback already registered" in r.message for r in caplog.records)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_invokes_callback_with_key_and_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # This fixture's provider serves no native compaction, so the in-place
        # attempt falls through to the recycle. The callback reports the arm that
        # ran; the key/pct/success threading this case exists for is unchanged.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_skips_callback_when_session_absent(self, cfg):
        """No session means no recycle happened, so the callback must not fire."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:missing", 91.0)

        cb.assert_not_awaited()
        assert not mgr.has_session("dashboard:missing"), "compaction must not create a session"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_waits_for_inflight_turn(self, cfg):
        """kiro-cli recycle must drain the in-flight turn: while the session
        semaphore is held (turn active) it blocks instead of SIGKILL'ing."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # get_or_create returns HOLDING the semaphore -> simulates an active turn.
        await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        # flake-ok: the compaction parks on the held turn semaphore before this sleep returns; a slow host only weakens the negative asserts; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the compaction parks on the held turn semaphore before this sleep returns so the negative asserts hold on any host and a slow host only weakens them
        await asyncio.sleep(0.05)
        # Still draining: not recycled, callback not fired.
        assert not task.done()
        assert "dashboard:chat-1" in mgr._sessions
        cb.assert_not_awaited()

        # Turn finishes -> semaphore released -> recycle proceeds.
        mgr.release("dashboard:chat-1")
        await await_until(task, what="the compact-session task finishing")
        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        # This fixture's provider serves no native compaction, so the in-place
        # attempt falls through to the recycle. The callback reports the arm that
        # ran; the key/pct/success threading this case exists for is unchanged.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_defers_when_turn_never_drains(self, cfg, caplog, monkeypatch):
        """A still-running turn (semaphore held) must NEVER be killed for
        compaction: after COMPACT_WAIT_TIMEOUT_SECS the attempt is deferred —
        session intact, no callback — and re-triggered at the next turn end."""
        # Only the outer cap is scaled: the inner status wait clamps to
        # _COMPACT_RESULT_WAIT_FLOOR_SECS (5s) — patch that too if a test
        # needs the inner wait itself to time out quickly.
        # flake-ok: the 0.1 s outer budget is meant to expire: the test holds the turn semaphore throughout so the wait defers on any host; moved verbatim
        monkeypatch.setattr("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.1)
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Hold the semaphore and never release -> simulates a long-running turn.
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await await_until(
                asyncio.ensure_future(mgr._compact_session("dashboard:chat-1", 92.0)),
                what="the deferred compaction returning",
            )

        # Session survives, the live turn was not killed, and nothing was
        # reported to the user (a deferral is not a failure).
        assert "dashboard:chat-1" in mgr._sessions
        provider.shutdown.assert_not_awaited()
        cb.assert_not_awaited()
        assert any("compaction deferred" in r.message for r in caplog.records)
        # _compacting cleared so the next turn-end check can re-trigger.
        assert "dashboard:chat-1" not in mgr._compacting
        mgr.release("dashboard:chat-1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_callback_exception_is_logged(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock(side_effect=RuntimeError("boom"))
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.ERROR, logger="kiro_crew.session"):
            await mgr._compact_session("dashboard:chat-1", 95.0)

        cb.assert_awaited_once()
        assert any("Compact callback failed" in r.message for r in caplog.records)
        # Session still recycled, compacting flag cleared
        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        assert "dashboard:chat-1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_threads_pct_through(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-2")
        mgr.release("dashboard:chat-2")
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        mgr._trigger_compaction("dashboard:chat-2", "context at 92%", 92.0, provider)
        # _trigger_compaction schedules the work as a background task
        # flake-ok: unbounded gather over scheduled tasks that drive only mock providers whose awaits return at once; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert captured == [("dashboard:chat-2", 92.0, True)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_check_context_usage_fires_callback_with_observed_pct(self, cfg):
        """High pct should flow from check_context_usage through to the callback."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-3")
        mgr.release("dashboard:chat-3")
        provider.context_usage_pct = lambda: 93.0
        captured: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            captured.append((key, pct, success))

        mgr.set_compact_callback(cb)

        pct = mgr.check_context_usage("dashboard:chat-3", provider)
        # flake-ok: unbounded gather over scheduled tasks that drive only mock providers whose awaits return at once; no timeout; moved verbatim; tracked for a de-flake pass
        await asyncio.gather(*mgr._background_tasks, return_exceptions=True)

        assert pct == 93.0
        assert captured == [("dashboard:chat-3", 93.0, True)]
        await mgr.close_all()


class TestCheckContextUsage:
    """Tests for check_context_usage thresholds and prompt counting."""

    @pytest.mark.asyncio
    async def test_increments_prompt_count(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        assert mgr._sessions["k1"].prompt_count == 0
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 1
        mgr.check_context_usage("k1", provider)
        assert mgr._sessions["k1"].prompt_count == 2
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 42.5
        result = mgr.check_context_usage("k1", provider)
        assert result == 42.5
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_warning_fires_one_margin_below_the_threshold(self, cfg, caplog):
        """The warn arm opens exactly at ``threshold - CONTEXT_WARN_MARGIN_PCT``.

        Derived from the constant rather than restating a percentage: the warn
        level is relative to whatever the operator configured, so a literal here
        would pin the test to one threshold and go stale the next time either
        number moves.
        """
        from kiro_crew.config.loader import CONTEXT_WARN_MARGIN_PCT

        cfg.session.autocompact_pct = 90.0
        warn_at = 90.0 - CONTEXT_WARN_MARGIN_PCT
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: warn_at
        with (
            patch("kiro_crew.session.published_autocompact_pct", return_value=90.0),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            mgr.check_context_usage("k1", provider)
        assert any(
            f"{warn_at:.0f}%" in r.message for r in caplog.records if r.name == "kiro_crew.session"
        )
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_warning_just_below_the_margin(self, cfg, caplog):
        """One point under the warn level takes the info arm, not the warn arm.

        Pins the boundary from the other side: without this, a margin widened
        to cover the whole window would still satisfy the test above.
        """
        from kiro_crew.config.loader import CONTEXT_WARN_MARGIN_PCT

        cfg.session.autocompact_pct = 90.0
        below = 90.0 - CONTEXT_WARN_MARGIN_PCT - 1.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: below
        with (
            patch("kiro_crew.session.published_autocompact_pct", return_value=90.0),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            mgr.check_context_usage("k1", provider)
        # Scoped to this logger: caplog captures the whole root hierarchy, so an
        # unrelated library record (asyncio's "Task was destroyed but it is
        # pending!" fires here on Windows) would otherwise read as a context
        # warning and fail a test that is only about this arm.
        assert not [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and r.name == "kiro_crew.session"
        ]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_triggered_at_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 92.0
        with patch.object(mgr, "_trigger_compaction") as mock_trigger:
            mgr.check_context_usage("k1", provider)
            # The trigger seam receives the provider the reading was observed
            # on — the gate ladder inside it evaluates against that provider.
            mock_trigger.assert_called_once_with("k1", "context at 92%", 92.0, provider)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_compaction_below_threshold(self, cfg):
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 50.0
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            mgr.check_context_usage("k1", provider)
        compact.assert_not_called()
        assert "k1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_no_compaction_when_pct_unconfirmed(self, cfg):
        """Defensive gate: a pct above threshold that no telemetry has
        confirmed for the CURRENT session binding must NOT trigger compaction
        (compacting an empty just-claimed session, then overflowing)."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 95.0
        provider.context_usage_unknown = lambda: True
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            pct = mgr.check_context_usage("k1", provider)
        compact.assert_not_called()
        assert "k1" not in mgr._compacting
        assert pct == 95.0  # reading is still returned, only the trigger is gated
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compaction_fires_when_pct_confirmed(self, cfg):
        """Twin of the gate test: the same pct WITH confirmed telemetry
        (context_usage_unknown False) still compacts — the gate must not
        suppress legitimate triggers."""
        cfg.session.autocompact_pct = 90.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 95.0
        provider.context_usage_unknown = lambda: False
        with patch.object(mgr, "_compact_session", new_callable=AsyncMock) as compact:
            mgr.check_context_usage("k1", provider)
            # flake-ok: gathers the scheduled compaction whose _compact_session is patched to an AsyncMock that returns at once; moved verbatim
            await asyncio.gather(*mgr._background_tasks, return_exceptions=True)
        compact.assert_awaited_once_with("k1", 95.0)
        await mgr.close_all()

    def test_missing_session_still_returns_pct(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mock_p = AsyncMock()
        mock_p.context_usage_pct = lambda: 55.0
        result = mgr.check_context_usage("nonexistent", mock_p)
        assert result == 55.0


class TestCompaction:
    """Tests for _trigger_compaction and _compact_session."""

    @pytest.mark.asyncio
    async def test_trigger_compaction_duplicate_is_noop(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        # First trigger starts compaction
        assert mgr._trigger_compaction("k1", "test", 92.0, provider) is None
        assert "k1" in mgr._compacting
        # Second trigger on same key is a no-op (already in progress)
        assert mgr._trigger_compaction("k1", "test again", 95.0, provider) == "in_progress"
        # flake-ok: settle sleep with no assert after it; close_all cancels and gathers any task still running so a slow host changes nothing; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- no assert follows the sleep and close_all cancels and gathers the compaction task so timing cannot change the result
        await asyncio.sleep(0.1)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_session_calls_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)
        await mgr._compact_session("k1", 92.0)
        provider.shutdown.assert_awaited_once()
        assert callback_args == [("k1", 92.0, True)]
        assert mgr._sessions["k1"].first_turn.is_new

    @pytest.mark.asyncio
    async def test_compact_session_missing_key_is_safe(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr._compacting.add("gone")
        await mgr._compact_session("gone", 90.0)
        assert "gone" not in mgr._compacting


class TestClaudeBackendCompaction:
    """Claude-agent-acp autocompact runs /compact in place — no recycle."""

    @pytest.mark.asyncio
    async def test_compact_session_claude_runs_in_place(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        callback_args: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_args.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        provider.compact.assert_awaited_once()
        provider.shutdown.assert_not_awaited()
        assert mgr.has_session("k1")
        assert callback_args == [("k1", 92.0, True)]
        assert "k1" not in mgr._compacting

    @pytest.mark.asyncio
    async def test_compact_session_claude_failure_keeps_session(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            caplog.at_level(logging.ERROR, logger="kiro_crew.session"),
        ):
            await mgr._compact_session("k1", 92.0)

        assert mgr.has_session("k1")
        provider.shutdown.assert_not_awaited()
        # Failure callback fires with success=False so the dashboard can
        # show a "compact failed" banner. (Behavior changed in the I2 fix.)
        cb.assert_awaited_once_with("k1", 92.0, success=False, outcome="compacted")
        assert "k1" not in mgr._compacting
        assert any("Compact failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_check_context_usage_triggers_for_claude(self, cfg):
        """Autocompact threshold must apply to claude."""
        cfg.session.autocompact_pct = 20.0
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.context_usage_pct = lambda: 40.0

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            patch.object(mgr, "_trigger_compaction") as mock_trigger,
        ):
            mgr.check_context_usage("k1", provider)

        mock_trigger.assert_called_once_with("k1", "context at 40%", 40.0, provider)
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_claude_compact_reuses_session(self, cfg):
        """Concurrent get_or_create while claude compact is in flight must
        return the existing session, not cold-start a duplicate provider that
        would later overwrite _sessions[key] and leak the original process."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            mgr._compacting.add("k1")
            try:
                provider2, is_new, _ = await mgr.get_or_create("k1")
            finally:
                mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        assert mgr.count == 1
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_recycle_teardown_cold_starts(self, cfg):
        """While the failure recycle is tearing the entry down (_recycling
        holds the exact object still in the map), get_or_create must not
        reuse the doomed entry — fall through to cold-start."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._recycling["k1"] = mgr._sessions["k1"]
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        # New provider: we did not short-circuit to the doomed entry.
        assert provider2 is not provider
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_reuses_healthy_replacement_during_recycle(self, cfg):
        """The _recycling marker is object-aware: when the map already holds a
        healthy REPLACEMENT for a key whose OLD session is still being torn
        down, get_or_create must reuse the replacement — not exile it and
        cold-start a duplicate provider that would overwrite and leak it."""
        from kiro_crew.session import FirstTurnState, _Session

        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        old_provider, _, _ = await mgr.get_or_create("k1")
        old_sess = mgr._sessions["k1"]
        mgr.release("k1")

        # Recycle of the OLD object is in flight; a racing cold-start has
        # already registered a fresh replacement under the same key.
        replacement_provider = AsyncMock()
        replacement_provider.shutdown = AsyncMock()
        replacement_provider.memory_mode = "persistent"
        replacement_provider.is_process_alive = lambda: True
        replacement = _Session(
            provider=replacement_provider, first_turn=FirstTurnState.NOTHING_ARMED
        )
        mgr._sessions["k1"] = replacement
        mgr._recycling["k1"] = old_sess
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._recycling.pop("k1", None)

        assert provider2 is replacement_provider
        assert is_new is False
        assert mgr._sessions["k1"] is replacement
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_during_inplace_compact_reuses_session(self, cfg):
        """An in-place compact (kiro or claude) keeps the entry healthy:
        concurrent get_or_create must reuse it — queueing on the session
        semaphore — instead of cold-starting a duplicate provider."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        mgr._compacting.add("k1")  # in-place compact in flight; NOT recycling
        try:
            provider2, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert provider2 is provider
        assert is_new is False
        mgr.release("k1")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_get_or_create_after_kiro_pop_cold_starts(self, cfg):
        """Real kiro recycle pops _sessions[key] before adding to _compacting.
        Concurrent get_or_create must cold-start fresh (is_new=True)."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Pop is the post-recycle reality: entry gone from _sessions.
        mgr._compacting.add("k1")
        try:
            provider, is_new, _ = await mgr.get_or_create("k1")
        finally:
            mgr._compacting.discard("k1")

        assert is_new is True
        assert provider is not None
        mgr.release("k1")
        await mgr.close_all()


class TestKiroInPlaceCompaction:
    """kiro-cli auto-compaction runs /compact IN PLACE first, so the session
    (and any queued/agentic work) continues automatically — recycle is only
    the fallback. Fix for 'session stops after auto-compaction'."""

    @staticmethod
    def _inplace_provider_factory(result: dict, *, stream_events: list | None = None):
        """Provider whose native compaction reports *result*.

        ``stream_command("/compact")`` yields *stream_events* (default: none —
        the terminal status arrives async via ``wait_for_compaction``,
        mirroring kiro-cli's post-end_turn status emission).
        """

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                for ev in stream_events or []:
                    yield ev

            m.stream_command = MagicMock(side_effect=_stream)
            m.wait_for_compaction = AsyncMock(return_value=result)
            return m

        return factory

    @pytest.mark.asyncio
    async def test_inplace_success_keeps_session_and_process(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Session survives in place: same entry, same provider, no SIGKILL.
        assert "dashboard:chat-1" in mgr._sessions
        assert mgr._sessions["dashboard:chat-1"].provider is provider
        provider.stream_command.assert_called_once_with("/compact")
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        # Semaphore released: the next turn can proceed immediately.
        assert not mgr._sessions["dashboard:chat-1"].semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_failed_result_falls_back_to_recycle(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "failed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Fallback recycle: the process is killed and a fresh successor takes the
        # key, so the context is guaranteed to clear.
        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        assert "dashboard:chat-1" not in mgr._recycling
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_no_native_support_falls_back_to_recycle(self, cfg):
        """Base LLMProvider.wait_for_compaction returns {'type': 'timeout'} —
        providers without native compaction must keep today's recycle path."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "timeout"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_success_clears_failure_cooldown(self, cfg):
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_midstream_terminal_status_skips_async_wait(self, cfg):
        """kiro-cli may emit the terminal compaction status MID-TURN (before
        end_turn). The stream watcher must latch it so the blind drain never
        eats it — otherwise wait_for_compaction would stall to timeout and
        wrongly recycle a just-compacted healthy session."""
        from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, AcpEvent

        mid = AcpEvent(kind=EVENT_COMPACTION_STATUS, text="completed", title="sum")
        mgr = SessionManager(
            cfg,
            provider_factory=self._inplace_provider_factory(
                {"type": "timeout"},  # async wait would FAIL if consulted
                stream_events=[mid],
            ),
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # Mid-stream status decided the outcome; async wait never consulted.
        assert "dashboard:chat-1" in mgr._sessions
        provider.wait_for_compaction.assert_not_awaited()
        provider.shutdown.assert_not_awaited()
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_never_uses_commands_execute(self, cfg):
        """Sending /compact
        via the string form of _kiro.dev/commands/execute makes kiro-cli
        2.14.0 exit rc=0. The auto-compact path must use the prompt
        transport (stream_command), never send_command."""
        mgr = SessionManager(
            cfg, provider_factory=self._inplace_provider_factory({"type": "completed"})
        )
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        provider.send_command = AsyncMock()
        mgr.release("dashboard:chat-1")

        outcome = await mgr._compact_session("dashboard:chat-1", 92.0)

        assert outcome == "ok"
        assert mgr._sessions["dashboard:chat-1"].provider is provider
        provider.stream_command.assert_called_once_with("/compact")
        provider.send_command.assert_not_awaited()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_inplace_holds_semaphore_so_queued_turns_wait(self, cfg):
        """While the in-place compact runs, the session semaphore is held —
        a queued turn waits behind it and then continues on the compacted
        session instead of interleaving with the compaction."""
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                # flake-ok: gated double: the compaction task parks on gate.wait() and the test sets the gate before awaiting the task through await_until; moved verbatim
                await gate.wait()
                return {"type": "completed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        task = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        # flake-ok: the compaction task takes the free semaphore and parks on the gate in its first step which runs before this sleep can wake; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the compaction task takes the free semaphore and parks on the gate in its first step which runs before this sleep wakes; moved verbatim
        await asyncio.sleep(0.05)
        assert not task.done()
        assert sess.semaphore.locked()  # queued turn would wait here

        gate.set()
        await await_until(task, what="the compact-session task finishing")
        assert "dashboard:chat-1" in mgr._sessions
        assert not sess.semaphore.locked()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_failure_recycle_never_yields_semaphore_to_queued_turn(self, cfg):
        """The failure recycle must not
        open a window in which a queued turn is dispatched into a session that
        is still compacting.

        The old code released the turn semaphore as soon as the in-place
        compact reported failure and let the CALLER re-acquire it for the
        recycle. A queued turn won that gap: it was sent to a kiro-cli that was
        still finishing its ``/compact``, its stream received the late
        ``completed`` status instead of an ``end_turn``, and the turn hung
        holding the semaphore until the 2h prompt timeout — while the recycle
        that would have rescued it gave up at its own acquire timeout. The kill
        must therefore land BEFORE any queued turn can run.
        """
        order: list[str] = []
        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _shutdown() -> None:
                order.append("shutdown")

            m.shutdown = AsyncMock(side_effect=_shutdown)

            async def _wait(timeout=120.0):
                # Mirrors production: the async wait gives up while the
                # compaction is in fact still running on the backend.
                # flake-ok: gated double: the compaction task parks on gate.wait() and the test sets the gate before awaiting the task through await_until; moved verbatim
                await gate.wait()
                return {"type": "timeout"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        sess = mgr._sessions["dashboard:chat-1"]

        compact = asyncio.create_task(mgr._compact_session("dashboard:chat-1", 92.0))
        # flake-ok: sleep lets the compaction task take the free semaphore before the locked() assert; it acquires with no earlier suspension; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- lets the compaction task take the free semaphore before the locked assert; it acquires with no earlier suspension; moved verbatim
        await asyncio.sleep(0.05)
        assert sess.semaphore.locked()

        # A queued turn parks on the turn semaphore, exactly as a real
        # dispatch does while a compaction holds it.
        async def _queued_turn() -> None:
            async with sess.semaphore:
                order.append("turn")

        turn = asyncio.create_task(_queued_turn())
        # flake-ok: the turn parks on the permit the gated compaction holds in its first step before this sleep ends so the not-done assert is exact; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the turn parks on the held permit in its first step before this sleep ends so the not-done assert does not depend on the sleep length
        await asyncio.sleep(0.05)
        assert not turn.done()

        gate.set()  # compact reports timeout -> recycle, semaphore still held
        await await_until(compact, what="the compaction recycle finishing")
        await await_until(turn, what="the queued turn running")

        # Kill first, queued turn second: the semaphore was never handed back
        # while the backend could still have been compacting.
        assert order == ["shutdown", "turn"]
        # A fresh successor holds the key.
        assert mgr._sessions["dashboard:chat-1"].first_turn.is_new
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_recycle_pop_by_identity_spares_replacement(self, cfg):
        """If a racing cold-start replaced the entry while the in-place
        compact was still running, the failure recycle kills only the OLD
        session; the fresh replacement (and its session_map entry) survives."""
        from kiro_crew.session import FirstTurnState, _Session

        gate = asyncio.Event()

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            # Sync on the real provider; the replaced-recycle arm calls it.
            m.disown_work_dir = MagicMock()
            m.context_usage_pct = lambda: 0.0

            async def _stream(_cmd):
                if False:  # pragma: no cover - empty async generator
                    yield

            m.stream_command = MagicMock(side_effect=_stream)

            async def _wait(timeout=120.0):
                # flake-ok: gated double: the compaction task parks on gate.wait() and the test sets the gate before awaiting the task through await_until; moved verbatim
                await gate.wait()
                return {"type": "failed"}

            m.wait_for_compaction = _wait
            return m

        mgr = SessionManager(cfg, provider_factory=factory)
        old_provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        task = asyncio.create_task(mgr._compact_session("k1", 92.0))
        # flake-ok: the compaction task reads the old session in its first step which runs before this sleep returns; the unset gate keeps it parked; moved verbatim
        # nosemgrep: kirocrew.test-sleep-as-barrier -- the compaction task reads the old session in its first step which runs before this sleep returns and the unset gate keeps it parked; moved verbatim
        await asyncio.sleep(0.05)
        assert not task.done()

        # A racing cold-start replaces the entry with a fresh session.
        new_provider = AsyncMock()
        new_provider.shutdown = AsyncMock()
        mgr._sessions["k1"] = _Session(
            provider=new_provider, first_turn=FirstTurnState.NOTHING_ARMED
        )

        gate.set()  # compact fails -> recycle pops by identity
        await await_until(task, what="the compact-session task finishing")

        # Replacement untouched; old provider reaped.
        assert mgr._sessions["k1"].provider is new_provider
        new_provider.shutdown.assert_not_awaited()
        old_provider.shutdown.assert_awaited_once()
        # The provider was REPLACED, not summarized, so the callback
        # reports that arm -- what the notice needs to tell the user.
        cb.assert_awaited_once_with("k1", 92.0, success=True, outcome="recycled")
        await mgr.close_all()


class TestCompactFailureCooldown:
    """After a compact failure, subsequent triggers within the cooldown
    window must be skipped to avoid hammering the provider on every turn."""

    @pytest.mark.asyncio
    async def test_compact_failure_sets_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        # flake-ok: the awaited failure arms the cooldown at monotonic plus 60 s and the real clock is read right after; only a 60 s stall flips it; moved verbatim
        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_trigger_compaction_skipped_during_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Set cooldown 60s in the future.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        assert mgr._trigger_compaction("k1", "context at 90%", 90.0, AsyncMock()) == "cooldown"

        # No background task scheduled, no compacting marker set.
        assert "k1" not in mgr._compacting
        assert not mgr._background_tasks
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_compact_success_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        # Pre-existing cooldown from an earlier failure.
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 60.0

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_expired_cooldown_allows_retrigger(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        # Cooldown already in the past.
        mgr._compact_cooldown_until["k1"] = time.monotonic() - 1.0

        assert mgr._trigger_compaction("k1", "context at 90%", 90.0, AsyncMock()) is None

        # Background task scheduled and compacting marker set.
        assert "k1" in mgr._compacting
        assert len(mgr._background_tasks) == 1
        # Snapshot to a list — `add_done_callback` discard mutates the set
        # during await, which would break direct iteration.
        for t in list(mgr._background_tasks):
            await t
        await mgr.close_all()


class TestCompactTimeout:
    """provider.compact() must be wrapped in a timeout so a stuck compact
    cannot hold session.semaphore forever and block concurrent gets."""

    @pytest.mark.asyncio
    async def test_compact_timeout_sets_cooldown_and_fires_failure(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")

        async def _hang(*_a, **_kw):
            # flake-ok: stand-in compact that outlasts the patched 0.05 s budget; wait_for cancels it at that deadline so the test never waits 10 s; moved verbatim
            # nosemgrep: kirocrew.test-sleep-as-barrier -- stand-in compact that the patched 0.05 s budget cancels so the test never waits the full 10 s
            await asyncio.sleep(10.0)

        provider.compact = _hang
        callback_calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            callback_calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            # Only the outer cap is scaled — see _COMPACT_RESULT_WAIT_FLOOR_SECS
            # note above if the inner wait must time out quickly.
            # flake-ok: the 0.05 s budget is meant to expire: the stand-in compact sleeps 10 s so the timeout arm runs on any host; moved verbatim
            patch("kiro_crew.session.COMPACT_WAIT_TIMEOUT_SECS", 0.05),
        ):
            await mgr._compact_session("k1", 92.0)

        # flake-ok: the timed-out compaction arms the cooldown at monotonic plus 60 s and the real clock is read right after; only a 60 s stall flips it; moved verbatim
        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        assert callback_calls == [("k1", 92.0, False)]
        assert "k1" not in mgr._compacting
        await mgr.close_all()


class TestCompactCallbackSuccessFlag:
    """The compact callback must receive ``success=False`` on failure so the
    dashboard can show a different banner."""

    @pytest.mark.asyncio
    async def test_keyword_callback_fires_with_success_true_on_success(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()

        calls: list[tuple[str, float, bool]] = []

        async def cb(key, pct, *, success, outcome="compacted"):
            calls.append((key, pct, success))

        mgr.set_compact_callback(cb)

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert calls == [("k1", 92.0, True)]
        await mgr.close_all()


class TestSetCompactCallback:
    """Tests for set_compact_callback."""

    def test_sets_callback(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        cb = AsyncMock()
        mgr.set_compact_callback(cb)
        assert mgr._on_compacted is cb

    def test_warns_on_replace(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        mgr.set_compact_callback(AsyncMock())
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.set_compact_callback(AsyncMock())
        assert any("already registered" in r.message for r in caplog.records)


class TestIneffectiveCompactionCooldown:
    """A compaction that completes but frees no meaningful headroom keeps the
    failure cooldown instead of clearing it — otherwise every "successful"
    no-progress attempt re-triggers on the next turn end and each retry pays
    another model-generated summarization."""

    @staticmethod
    def _inplace_factory(pct_after: float):
        """kiro-cli-style provider whose /compact completes and whose
        post-compaction ``context_usage_pct()`` reads *pct_after*."""

        def factory(session_key=None, agent=None, channel_id=None, **kwargs):
            m = AsyncMock()
            m.start = AsyncMock()
            m.shutdown = AsyncMock()
            m.context_usage_pct = lambda: pct_after

            async def _stream(_cmd):
                return
                yield  # pragma: no cover — make this an async generator

            m.stream_command = MagicMock(side_effect=_stream)
            m.wait_for_compaction = AsyncMock(return_value={"type": "completed"})
            return m

        return factory

    # ── (a) effective compaction clears the cooldown ──

    @pytest.mark.asyncio
    async def test_inplace_effective_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=40.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_effective_clears_cooldown(self, cfg):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 40.0
        mgr._compact_cooldown_until["k1"] = time.monotonic() + 999

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_unknown_post_compaction_pct_defers_verdict(self, cfg):
        """kiro-cli's mid-turn terminal status resets the stats to 0.0/unknown
        before any post-compaction metadata lands. An unknown reading must not
        be judged (a 0.0 would read as a huge drop and mask the defect entirely);
        the verdict is deferred to the first confirmed reading."""
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=0.0))
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        provider.context_usage_unknown = lambda: True
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999

        await mgr._compact_session("dashboard:chat-1", 92.0)

        # No verdict yet: the running cooldown is left to expire on its own
        # and the trigger pct is stashed for the next confirmed reading.
        assert "dashboard:chat-1" in mgr._compact_cooldown_until
        assert mgr._compact_pending_verdict["dashboard:chat-1"] == 92.0
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_effective_clears_cooldown(self, cfg):
        """First confirmed reading shows a real drop: the deferred verdict is
        effective and the cooldown clears."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        mgr._compact_cooldown_until["dashboard:chat-1"] = time.monotonic() + 999
        provider.context_usage_pct = lambda: 40.0

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert "dashboard:chat-1" not in mgr._compact_pending_verdict
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_a_deferred_verdict_records_the_compaction_when_it_settles(
        self, cfg, monkeypatch
    ):
        """A compaction whose effect was not measurable at the time still reaches
        the session ledger.

        ``_settle_compact_cooldown`` records only the immediately-confirmed case.
        Without this emit the deferred half -- the reading kiro-cli reset to
        unknown mid-turn -- would settle here and be recorded nowhere, so the
        ledger would be missing exactly the compactions that were hardest to
        measure."""
        from kiro_crew.crew_log import emit as crew_log_emit

        seen: list[tuple[float, float]] = []
        monkeypatch.setattr(
            crew_log_emit,
            "on_compaction_applied",
            lambda sid, *, pct_before, pct_after: seen.append((pct_before, pct_after)),
        )
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 40.0

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert seen == [(92.0, 40.0)]
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_ineffective_arms_cooldown_and_suppresses_trigger(
        self, cfg, caplog
    ):
        """First confirmed reading is still within the no-progress band: the
        deferred verdict arms the cooldown BEFORE the same call's trigger
        decision, so the immediate re-trigger is suppressed."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 91.0  # >= autocompact_pct (90)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            mgr.check_context_usage("dashboard:chat-1", provider)

        assert "dashboard:chat-1" not in mgr._compact_pending_verdict
        # flake-ok: check_context_usage arms the cooldown at monotonic plus 60 s and the real clock is read right after; only a 60 s stall flips it; moved verbatim
        assert mgr._compact_cooldown_until.get("dashboard:chat-1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        # The 91% reading is above the trigger threshold, but the just-armed
        # cooldown suppressed the re-trigger: no compaction task started.
        assert "dashboard:chat-1" not in mgr._compacting
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_deferred_verdict_waits_for_confirmed_reading(self, cfg):
        """An unknown reading does not consume the pending verdict."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        mgr._compact_pending_verdict["dashboard:chat-1"] = 92.0
        provider.context_usage_pct = lambda: 0.0
        provider.context_usage_unknown = lambda: True

        mgr.check_context_usage("dashboard:chat-1", provider)

        assert mgr._compact_pending_verdict["dashboard:chat-1"] == 92.0
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_stale_reading_defers_instead_of_damping_success(self, cfg):
        """The claude branch can return from compact() before any telemetry
        refresh, so the re-read still shows the PRE-compaction value. Judging
        that stale reading would arm the cooldown on a compaction that in
        fact succeeded — it must defer instead, and the next confirmed
        reading (showing the real drop) must clear cleanly."""
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 92.0  # unchanged: stats never reset

        with patch("kiro_crew.session._is_claude_backend", return_value=True):
            await mgr._compact_session("k1", 92.0)

        # Deferred, not damped: a successful compaction must not be punished.
        assert "k1" not in mgr._compact_cooldown_until
        assert mgr._compact_pending_verdict["k1"] == 92.0
        # Next turn's confirmed reading shows the real drop — verdict clears.
        provider.context_usage_pct = lambda: 40.0
        mgr.check_context_usage("k1", provider)
        assert "k1" not in mgr._compact_pending_verdict
        assert "k1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    # ── (b) effective but still above the trigger threshold is NOT ineffective ──

    @pytest.mark.asyncio
    async def test_inplace_effective_above_threshold_not_damped(self, cfg):
        """A good compaction of a very long turn can land above
        ``autocompact_pct`` while still having freed real headroom. The
        ineffective test is the measured drop, not the absolute level."""
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=92.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        assert 92.0 >= cfg.session.autocompact_pct  # still above the trigger

        await mgr._compact_session("dashboard:chat-1", 99.0)

        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()

    # ── (c) ineffective compaction arms the cooldown and suppresses the next trigger ──

    @pytest.mark.asyncio
    async def test_inplace_ineffective_sets_cooldown_and_suppresses_retrigger(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=91.0))
        provider, _, _ = await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.session"):
            await mgr._compact_session("dashboard:chat-1", 92.0)

        # flake-ok: the in-place compaction arms the cooldown at monotonic plus 60 s and the real clock is read right after; only a 60 s stall flips it; moved verbatim
        assert mgr._compact_cooldown_until.get("dashboard:chat-1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        # The compaction DID complete and rewrote the conversation: the
        # callback stays success=True (reinjection must run; the failure
        # notice would misdescribe a completed attempt).
        cb.assert_awaited_once_with("dashboard:chat-1", 92.0, success=True, outcome="compacted")
        # The immediate next trigger is suppressed by the cooldown.
        assert mgr._trigger_compaction("dashboard:chat-1", "context 92%", 92.0, provider) == (
            "cooldown"
        )
        assert "dashboard:chat-1" not in mgr._compacting
        provider.stream_command.assert_called_once()
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_claude_ineffective_sets_cooldown(self, cfg, caplog):
        mgr = SessionManager(cfg, provider_factory=_mock_provider_factory())
        provider, _, _ = await mgr.get_or_create("k1")
        mgr.release("k1")
        provider.compact = AsyncMock()
        provider.context_usage_pct = lambda: 91.0
        cb = AsyncMock()
        mgr.set_compact_callback(cb)

        with (
            patch("kiro_crew.session._is_claude_backend", return_value=True),
            caplog.at_level(logging.WARNING, logger="kiro_crew.session"),
        ):
            await mgr._compact_session("k1", 92.0)

        # flake-ok: the awaited Claude branch arms the cooldown at monotonic plus 60 s and the real clock is read right after; only a 60 s stall flips it; moved verbatim
        assert mgr._compact_cooldown_until.get("k1", 0.0) > time.monotonic()
        assert any("ineffective" in r.message for r in caplog.records)
        cb.assert_awaited_once_with("k1", 92.0, success=True, outcome="compacted")
        assert mgr.has_session("k1")  # in place: the session survives
        await mgr.close_all()

    # ── (d) repeated ineffective compactions and the circuit breaker ──

    @pytest.mark.asyncio
    async def test_repeated_ineffective_keeps_damping_until_breaker_resets(self, cfg):
        """Each ineffective attempt re-arms the one existing cooldown (no
        second counter), so nothing masks the repetition from the existing
        circuit breaker: when the stuck session's turns keep failing,
        ``record_failure`` trips at ``_CIRCUIT_BREAKER_THRESHOLD`` and the
        forced reset clears the cooldown along with the session."""
        from kiro_crew.session import _CIRCUIT_BREAKER_THRESHOLD

        mgr = SessionManager(cfg, provider_factory=self._inplace_factory(pct_after=91.0))
        await mgr.get_or_create("dashboard:chat-1")
        mgr.release("dashboard:chat-1")

        # Two ineffective attempts (the second simulating a post-cooldown
        # retry) each re-arm the same cooldown.
        await mgr._compact_session("dashboard:chat-1", 92.0)
        first = mgr._compact_cooldown_until["dashboard:chat-1"]
        await mgr._compact_session("dashboard:chat-1", 92.0)
        assert mgr._compact_cooldown_until["dashboard:chat-1"] >= first

        # The session is still stuck at high context, so its turns fail; the
        # existing breaker observes that repetition and force-resets.
        tripped = False
        for _ in range(_CIRCUIT_BREAKER_THRESHOLD):
            tripped = await mgr.record_failure("dashboard:chat-1")
        assert tripped
        assert not mgr.has_session("dashboard:chat-1")
        # The forced reset clears the cooldown too — the fresh session starts
        # with no inherited damping.
        assert "dashboard:chat-1" not in mgr._compact_cooldown_until
        await mgr.close_all()
