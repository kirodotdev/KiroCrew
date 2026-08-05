"""Tests for the shutdown/restart drain of in-flight prompts.

Covers SessionManager.drain_active_turns() and its wiring into close_all() —
the fix for the empty-response-after-Make-Live incident (#200), where a slot
killed mid-prompt left its kiro-cli native-session lock held so the next
gateway's session/load hit "active in another process".
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import kiro_crew.session as session_mod
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import (
    SessionClosingError,
    SessionManager,
    _provider_has_active_turn,
    _provider_has_unfinished_turn,
    _Session,
)


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


class _FakeProvider:
    """Minimal LLMProvider stand-in with controllable turn/cancel behavior."""

    def __init__(
        self,
        *,
        active: bool = False,
        cancel_mode: str = "ack",
        unfinished: bool | None = None,
        cancel_returns: str | None = None,
    ) -> None:
        # cancel_mode: "ack" -> resolves quickly; "block" -> blocks until cancelled.
        self._active = active
        # has_unfinished_turn is INDEPENDENT of cancel state; default it to
        # mirror `active` (an in-flight turn is also unfinished) unless a test
        # pins it — e.g. an already-cancelled-but-not-acked turn is
        # active=False, unfinished=True.
        self._unfinished = active if unfinished is None else unfinished
        self._cancel_mode = cancel_mode
        # When set, cancel() returns this WITHOUT acking — simulates the
        # already-cancelled turn where provider.cancel() is a no-op "no_turn".
        self._cancel_returns = cancel_returns
        self.cancel_calls: list[float] = []
        self.wait_turn_done_calls: list[float] = []
        self.shutdown_called = False
        self.cancel_finished = False
        self.cancel_started_at: float | None = None
        self.shutdown_at: float | None = None

    async def start(self) -> None:  # pragma: no cover - not exercised
        return None

    def has_active_turn(self) -> bool:
        return self._active

    def has_unfinished_turn(self) -> bool:
        return self._unfinished

    def is_process_alive(self) -> bool:
        return True

    def is_alive(self) -> bool:
        return True

    def context_usage_pct(self) -> float:
        return 0.0

    @property
    def cwd(self) -> str:
        return "/tmp"

    async def cancel(self, *, wait_ack_timeout: float = 0.0):
        self.cancel_calls.append(wait_ack_timeout)
        self.cancel_started_at = time.monotonic()
        if self._cancel_returns is not None:
            # Already-cancelled turn: provider.cancel() is a no-op that reports
            # "no_turn"; the drain must then wait_turn_done() for the pending ack.
            return self._cancel_returns
        if self._cancel_mode == "block":
            # Never acks on its own — only the drain's bounded wait_for should
            # tear this down. sleep long enough to outlast any test budget.
            await asyncio.sleep(3600)
        # Graceful ack: the native turn reached a safe boundary.
        self._active = False
        self._unfinished = False
        self.cancel_finished = True
        return "acked"

    async def wait_turn_done(self, timeout: float = 0.0) -> str:
        # Drain calls this for an already-cancelled-but-unfinished turn; the
        # native ack now arrives and the turn is finished.
        self.wait_turn_done_calls.append(timeout)
        self._unfinished = False
        return "cancelled"

    async def shutdown(self) -> None:
        self.shutdown_called = True
        self.shutdown_at = time.monotonic()


def _inject(mgr: SessionManager, key: str, provider: _FakeProvider) -> None:
    mgr._sessions[key] = _Session(provider=provider)


# ── _provider_has_active_turn helper ──


def test_helper_true_only_for_real_bool_true():
    assert _provider_has_active_turn(_FakeProvider(active=True)) is True
    assert _provider_has_active_turn(_FakeProvider(active=False)) is False


def test_helper_false_when_method_missing():
    class _NoMethod:
        pass

    assert _provider_has_active_turn(_NoMethod()) is False


def test_helper_false_and_no_warning_for_async_double():
    """AsyncMock.has_active_turn() returns a coroutine — the helper must treat
    it as 'no active turn' and close it so no un-awaited-coroutine warning leaks."""
    m = AsyncMock()  # m.has_active_turn() returns a coroutine
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any RuntimeWarning becomes an error
        assert _provider_has_active_turn(m) is False


# ── drain_active_turns ──


@pytest.mark.asyncio
async def test_drain_cancels_in_flight_turn_then_proceeds(cfg):
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="ack")
    _inject(mgr, "s1", p)

    n = await mgr.drain_active_turns(timeout=2.0)

    assert n == 1
    # Graceful cancel issued with the drain budget, and the turn was drained.
    assert p.cancel_calls == [2.0]
    assert p.cancel_finished is True
    assert p.has_active_turn() is False


@pytest.mark.asyncio
async def test_drain_noop_when_no_active_turn(cfg):
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=False)
    _inject(mgr, "s1", p)

    n = await mgr.drain_active_turns(timeout=2.0)

    assert n == 0
    assert p.cancel_calls == []  # idle sessions are never cancelled


@pytest.mark.asyncio
async def test_drain_disabled_when_timeout_nonpositive(cfg):
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True)
    _inject(mgr, "s1", p)

    n = await mgr.drain_active_turns(timeout=0.0)

    assert n == 0
    assert p.cancel_calls == []


@pytest.mark.asyncio
async def test_drain_times_out_but_stays_bounded(cfg):
    """A turn that never reaches a safe boundary must not hang the drain —
    it returns within ~timeout+buffer and reports the stuck turn."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="block")
    _inject(mgr, "s1", p)

    t0 = time.monotonic()
    n = await mgr.drain_active_turns(timeout=0.3)
    elapsed = time.monotonic() - t0

    assert n == 1
    # Bounded: outer cap is timeout + 1.0s; must be well under the 3600s block.
    assert elapsed < 2.5
    assert p.cancel_finished is False  # never acked — timed out


@pytest.mark.asyncio
async def test_close_all_drains_then_kills_on_timeout(cfg, monkeypatch):
    """close_all must drain first, then STILL kill even when the drain times out."""
    # Shrink the default drain budget so close_all's internal drain is fast.
    monkeypatch.setattr(session_mod, "_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS", 0.2)

    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="block")
    _inject(mgr, "s1", p)

    t0 = time.monotonic()
    await mgr.close_all()
    elapsed = time.monotonic() - t0

    # Drain was attempted (cancel called) and then the kill path was reached.
    assert p.cancel_calls  # graceful cancel attempted
    assert p.shutdown_called is True  # fell through to kill/shutdown
    assert mgr.count == 0
    # Bounded by the shrunken drain budget (0.2 + 1.0) + fast shutdown, not 3600s.
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_close_all_drains_before_shutdown(cfg):
    """Ordering: the in-flight turn is cancelled (safe boundary) BEFORE the
    provider is shut down, so kiro-cli can release its lock before the kill."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="ack")
    _inject(mgr, "s1", p)

    await mgr.close_all()

    assert p.cancel_started_at is not None
    assert p.shutdown_at is not None
    assert p.cancel_started_at <= p.shutdown_at
    assert p.shutdown_called is True
    assert mgr.count == 0


# ── _provider_has_unfinished_turn helper (cancel/ack race) ──


def test_unfinished_helper_true_independent_of_cancel():
    # active False but unfinished True (already-cancelled, ack pending) -> True,
    # so the drain still fires. active/unfinished both False -> False.
    assert _provider_has_unfinished_turn(_FakeProvider(active=False, unfinished=True)) is True
    assert _provider_has_unfinished_turn(_FakeProvider(active=False, unfinished=False)) is False
    # A normal in-flight turn (active True) is also unfinished.
    assert _provider_has_unfinished_turn(_FakeProvider(active=True)) is True


def test_unfinished_helper_false_when_method_missing():
    class _NoMethod:
        pass

    assert _provider_has_unfinished_turn(_NoMethod()) is False


def test_unfinished_helper_false_and_no_warning_for_async_double():
    """AsyncMock.has_unfinished_turn() returns a coroutine — treat as 'no
    unfinished turn' and close it so no un-awaited-coroutine warning leaks."""
    import warnings

    m = AsyncMock()
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert _provider_has_unfinished_turn(m) is False


@pytest.mark.asyncio
async def test_drain_waits_on_already_cancelled_but_unfinished_turn(cfg):
    """Codex HIGH — cancel/ack race. A turn already session/cancel'd
    (has_active_turn False) but whose native turn-done ack has not arrived
    (has_unfinished_turn True) must STILL be drained: cancel() reports
    "no_turn", so the drain waits directly on the pending ack via
    wait_turn_done() before teardown. Filtering on has_active_turn would have
    skipped it → process killed with the native lock held (the bug)."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=False, unfinished=True, cancel_returns="no_turn")
    _inject(mgr, "s1", p)

    n = await mgr.drain_active_turns(timeout=1.0)

    assert n == 1                            # selected despite has_active_turn False
    assert p.cancel_calls == [1.0]           # graceful cancel still attempted
    assert p.wait_turn_done_calls == [1.0]   # and we waited for the pending ack
    assert p.has_unfinished_turn() is False  # ack arrived -> turn finished


@pytest.mark.asyncio
async def test_drain_does_not_wait_turn_done_on_normal_cancel(cfg):
    """A normal in-flight turn that cancel() acks directly must NOT trigger the
    extra wait_turn_done() fallback (that path is only for the no_turn race)."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="ack")
    _inject(mgr, "s1", p)

    n = await mgr.drain_active_turns(timeout=1.0)

    assert n == 1
    assert p.cancel_calls == [1.0]
    assert p.wait_turn_done_calls == []  # acked directly, no fallback wait


# ── close_all deadline / cancel handling (Design F1: Slack-restart regression) ──


@pytest.mark.asyncio
async def test_close_all_drain_plus_kill_fit_tight_deadline(cfg):
    """With a small drain_timeout the drain+kill complete well inside a caller's
    tight outer deadline (Slack wraps close_all in wait_for 5s)."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="ack")
    _inject(mgr, "s1", p)

    t0 = time.monotonic()
    await asyncio.wait_for(mgr.close_all(drain_timeout=0.2), timeout=1.0)
    elapsed = time.monotonic() - t0

    assert p.cancel_calls == [0.2]      # drain used the passed budget
    assert p.shutdown_called is True    # kill path ran
    assert mgr.count == 0
    assert elapsed < 1.0                # fit inside the tight deadline


@pytest.mark.asyncio
async def test_close_all_propagates_outer_cancel_to_keep_deadline_honest(cfg):
    """Codex HIGH2 — close_all must NOT swallow a cancel from an outer deadline.
    Slack wraps close_all in wait_for(..., 5s); the cap is enforced by
    cancelling close_all. If close_all ate that cancel, wait_for would block
    until close_all finished on its own, so a slow later teardown phase could
    overrun the 5s cap and prevent os._exit(1) — wedging the restart. So a
    blocking drain that outlasts the outer deadline MUST let the cancel
    propagate; the outer wait_for then raises on time. (A drain cut short this
    way skips the in-line kill path; the next-startup orphan reaper reclaims any
    still-held process/lock.)"""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=True, cancel_mode="block")  # drain never acks -> hangs
    _inject(mgr, "s1", p)

    t0 = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        # drain_timeout=5.0 (internal cap 6s) >> the 0.3s outer deadline; the
        # cancel fires mid-drain and MUST propagate so the deadline is enforced.
        await asyncio.wait_for(mgr.close_all(drain_timeout=5.0), timeout=0.3)
    elapsed = time.monotonic() - t0

    assert p.cancel_calls              # drain was attempted before the cancel
    assert elapsed < 1.0               # the 0.3s deadline was honored, not ~6s
    # The cancel propagated instead of being swallowed: close_all did not run the
    # in-line kill to completion on this path (that is the reaper's job).
    assert p.shutdown_called is False


@pytest.mark.asyncio
async def test_get_or_create_refused_while_closing(cfg):
    """Codex HIGH — drain-window race. Once close_all() has entered the closing
    state, get_or_create must refuse (raise) so no new turn begins during the
    multi-second drain window — such a turn would be absent from the drain
    snapshot and get killed mid-turn with its native lock held."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    mgr._closing = True
    with pytest.raises(RuntimeError, match="closing"):
        await mgr.get_or_create("s-new")


@pytest.mark.asyncio
async def test_close_all_sets_closing_state(cfg):
    """close_all enters the closing state under the lock (before the drain
    snapshot) so the get_or_create gate shuts the drain-window race."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    assert mgr._closing is False
    await mgr.close_all()
    assert mgr._closing is True


# ── begin_turn: lease-dispatch race (Codex HIGH) ──


def test_session_closing_error_is_runtime_error():
    """SessionClosingError subclasses RuntimeError so existing broad handlers
    (and the get_or_create entry-gate tests) still catch it."""
    assert issubclass(SessionClosingError, RuntimeError)


@pytest.mark.asyncio
async def test_begin_turn_noop_while_open(cfg):
    """begin_turn is a cheap synchronous no-op while the manager is open."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    _inject(mgr, "s1", _FakeProvider(active=False))
    assert mgr.begin_turn("s1") is None


@pytest.mark.asyncio
async def test_begin_turn_rejects_lease_holder_after_closing(cfg):
    """Codex HIGH — lease-dispatch race. A caller can acquire a session lease
    (get_or_create returns while holding the per-session semaphore) BEFORE
    close_all sets _closing, then only reach dispatch AFTER. The already-issued
    lease cannot be revoked, so the caller re-checks via begin_turn() right
    before starting the turn; once _closing is set that gate rejects, so no turn
    opens during the drain window — a turn that opened there would be absent
    from the drain snapshot and killed mid-turn with its native lock held."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    p = _FakeProvider(active=False)
    _inject(mgr, "s1", p)

    # Caller acquires the lease while the manager is still open; dispatch allowed.
    await mgr._sessions["s1"].semaphore.acquire()
    mgr.begin_turn("s1")  # open → no raise

    # Shutdown begins: close_all sets _closing under the lock BEFORE draining.
    async with mgr._lock:
        mgr._closing = True

    # Lease is still held, but the pre-dispatch gate now rejects → no turn opens,
    # so drain_active_turns never has to (and cannot) catch one that opened late.
    with pytest.raises(SessionClosingError):
        mgr.begin_turn("s1")
    assert p.has_active_turn() is False  # never dispatched

    mgr._sessions["s1"].semaphore.release()


@pytest.mark.asyncio
async def test_get_or_create_entry_gate_raises_typed_error(cfg):
    """The get_or_create entry gate raises the typed SessionClosingError (still a
    RuntimeError) once closing — the same signal the begin_turn gate uses."""
    mgr = SessionManager(cfg, provider_factory=lambda **k: _FakeProvider())
    mgr._closing = True
    with pytest.raises(SessionClosingError):
        await mgr.get_or_create("s-new")


# ── graceful terminate (Goal 2): SIGTERM-first grace preserved ──


def test_acp_runtime_kill_is_sigterm_first_with_grace():
    """The teardown kill must send SIGTERM with a non-trivial grace window
    before escalating to SIGKILL, so kiro-cli can release its native-session
    lock. Guards against a regression that shortens the grace to ~0."""
    from kiro_crew.acp.runtime import AcpRuntime

    # SIGTERM grace is generous enough for lock release, and a separate reap
    # window follows the SIGKILL escalation.
    assert AcpRuntime._KILL_TERM_TIMEOUT >= 3.0
    assert AcpRuntime._KILL_REAP_TIMEOUT > 0.0


def test_acp_client_kill_is_sigterm_first():
    """The legacy AcpClient path also SIGTERM-then-SIGKILLs (grace hardcoded
    3.0s in _kill_process). Assert the source keeps a SIGTERM-first wait so a
    refactor can't silently drop straight to SIGKILL."""
    import inspect as _inspect

    from kiro_crew.acp import client as _client

    src = _inspect.getsource(_client.AcpClient._kill_process)
    assert "SIGTERM" in src
    assert "wait_for" in src  # bounded wait after SIGTERM before force kill
