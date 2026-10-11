"""Regression tests for the session-core audit fixes.

Covers three remediations in ``src/kiro_crew/session.py``:

1. ``open_task_session`` re-validates session identity + provider liveness
   AFTER acquiring the per-session semaphore (mirroring ``get_or_create``), so a
   session that is recycled/removed or whose provider process dies while we wait
   on the semaphore is not handed back stale. This holds on BOTH the fast reuse
   path AND the lost-race branch (a step that loses the registration race must
   revalidate the winner's semaphore through ``_reacquire_and_validate``, not a
   bare acquire, then cold-start on a stale winner).
2. ``_resolve_agent_model`` uses mtime + TTL cache invalidation, so the
   ``"auto"`` miss (or any resolution) is not pinned forever — a later
   create/edit of the agent JSON is observed.
3. ``_reacquire_and_validate`` releases the semaphore if cancelled while parked
   on ``self._lock``, so a cancelled caller never leaves the key locked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import FirstTurnState, SessionManager, _Session


@pytest.fixture
def cfg():
    c = KiroCrewConfig()
    c.session.timeout_secs = 2
    return c


def _dead_after_probe_provider() -> AsyncMock:
    """A provider mock that reports alive until flipped dead."""
    p = AsyncMock()
    p._alive = True
    p.is_process_alive = lambda: p._alive
    p.is_alive = lambda: p._alive
    p.shutdown = AsyncMock()
    return p


# --------------------------------------------------------------------------
# Bug 1: open_task_session fast-path post-semaphore re-validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_task_session_revalidates_dead_provider_after_semaphore(cfg):
    """FAILURE SCENARIO (pre-fix): a live session is claimed on the fast path;
    while another turn holds its semaphore the provider process dies. The old
    fast path returned that dead provider unconditionally after acquire().

    POST-FIX: re-validation detects the dead provider, evicts + shuts it down,
    and falls through to cold-start a fresh session.
    """
    sm = SessionManager(cfg)
    key = "task:step1"

    dead_provider = _dead_after_probe_provider()
    sess = _Session(provider=dead_provider, first_turn=FirstTurnState.NOTHING_ARMED)
    sm._sessions[key] = sess
    # Occupy the turn so the fast-path acquire() below blocks.
    await sess.semaphore.acquire()

    # Cold-path fall-through stubs.
    fresh_handle = MagicMock()
    fresh_runtime = MagicMock()
    fresh_runtime.create_session = AsyncMock(return_value=fresh_handle)
    sm._get_or_bootstrap_run_runtime = AsyncMock(return_value=fresh_runtime)  # type: ignore[method-assign]
    fresh_provider = AsyncMock()
    fresh_provider.shutdown = AsyncMock()

    async def _driver() -> None:
        # Let open_task_session block on the semaphore, then kill the provider
        # and release the turn so the fast path proceeds and re-validates.
        while not sess.semaphore.locked():
            await asyncio.sleep(0.01)
        # semaphore is locked (held by us); wait a beat so the coroutine is
        # actually parked inside acquire(), then flip dead + release.
        await asyncio.sleep(0.02)
        dead_provider._alive = False
        sess.semaphore.release()

    with patch(
        "kiro_crew.acp.session_provider.AcpSessionProvider",
        return_value=fresh_provider,
    ):
        _, result = await asyncio.gather(
            _driver(), sm.open_task_session("parent", key)
        )

    provider, is_new, resumed = result
    assert provider is fresh_provider, "must cold-start a fresh session, not reuse the dead one"
    assert is_new is True
    assert resumed is False
    dead_provider.shutdown.assert_awaited_once()
    assert sm._sessions[key].provider is fresh_provider


@pytest.mark.asyncio
async def test_open_task_session_revalidates_removed_session_after_semaphore(cfg):
    """A session removed from the registry (e.g. recycled) while we wait on the
    semaphore must trigger cold-start, not resurrection of the orphaned entry."""
    sm = SessionManager(cfg)
    key = "task:step2"

    old_provider = _dead_after_probe_provider()
    sess = _Session(provider=old_provider, first_turn=FirstTurnState.NOTHING_ARMED)
    sm._sessions[key] = sess
    await sess.semaphore.acquire()

    fresh_runtime = MagicMock()
    fresh_runtime.create_session = AsyncMock(return_value=MagicMock())
    sm._get_or_bootstrap_run_runtime = AsyncMock(return_value=fresh_runtime)  # type: ignore[method-assign]
    fresh_provider = AsyncMock()
    fresh_provider.shutdown = AsyncMock()

    async def _driver() -> None:
        while not sess.semaphore.locked():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)
        # Another coroutine recycled the session: removed from the map.
        sm._sessions.pop(key, None)
        sess.semaphore.release()

    with patch(
        "kiro_crew.acp.session_provider.AcpSessionProvider",
        return_value=fresh_provider,
    ):
        _, result = await asyncio.gather(
            _driver(), sm.open_task_session("parent", key)
        )

    provider, is_new, _resumed = result
    assert provider is fresh_provider
    assert is_new is True
    # The removed session was not our entry after acquire, so we must NOT have
    # shut it down here (its recycler owns teardown).
    old_provider.shutdown.assert_not_awaited()
    assert sm._sessions[key].provider is fresh_provider


@pytest.mark.asyncio
async def test_open_task_session_lost_race_revalidates_recycled_winner(cfg):
    """FAILURE SCENARIO (pre-fix): a task step LOSES the registration race — a
    winner registers the key while we bootstrap the shared runtime. The old
    lost-race branch did a bare ``winner.semaphore.acquire()`` and returned
    ``winner.provider`` with NO identity/liveness re-check. If the winner is
    recycled or its runtime dies while we wait a full turn on its semaphore, the
    step gets multiplexed onto a dead/recycled runtime — the exact stale-provider
    bug class this consolidation kills, surviving via the rarer branch.

    POST-FIX: the lost-race acquire routes through ``_reacquire_and_validate``;
    a stale winner is evicted and the cold start retries, yielding a fresh live
    session instead of the dead winner.
    """
    sm = SessionManager(cfg)
    key = "task:lost-race"

    # The winner registered by another coroutine during our runtime bootstrap.
    winner_provider = _dead_after_probe_provider()  # flips dead below
    winner = _Session(provider=winner_provider, first_turn=FirstTurnState.NOTHING_ARMED)

    fresh_runtime = MagicMock()
    fresh_runtime.create_session = AsyncMock(return_value=MagicMock())

    bootstrap_calls = {"n": 0}

    async def _bootstrap(*_a, **_k):
        bootstrap_calls["n"] += 1
        if bootstrap_calls["n"] == 1:
            # Simulate the winner registering + starting a long turn while we
            # were bootstrapping the shared runtime: it holds its own semaphore.
            sm._sessions[key] = winner
            await winner.semaphore.acquire()
        return fresh_runtime

    sm._get_or_bootstrap_run_runtime = AsyncMock(side_effect=_bootstrap)  # type: ignore[method-assign]

    dup_provider = AsyncMock()          # the redundant provider we lose with
    dup_provider.shutdown = AsyncMock()
    # Synchronous on the real provider; an AsyncMock attribute would hand the
    # registry an un-awaited coroutine when it disowns the lost-race provider.
    dup_provider.disown_work_dir = MagicMock()
    fresh_provider = AsyncMock()        # the winning provider on the retry
    fresh_provider.shutdown = AsyncMock()

    async def _driver() -> None:
        # Wait until open_task_session is parked on the winner's semaphore
        # (lost-race acquire), then recycle the winner: flip its provider dead
        # and release the turn so re-validation runs and finds it stale.
        while not (key in sm._sessions and sm._sessions[key] is winner
                   and winner.semaphore.locked()):
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)  # ensure the loser is parked inside acquire()
        winner_provider._alive = False
        winner.semaphore.release()

    with patch(
        "kiro_crew.acp.session_provider.AcpSessionProvider",
        side_effect=[dup_provider, fresh_provider],
    ):
        _, result = await asyncio.gather(
            _driver(), sm.open_task_session("parent", key)
        )

    provider, is_new, resumed = result
    assert provider is fresh_provider, "loser must revalidate + cold-start, not reuse dead winner"
    assert is_new is True
    assert resumed is False
    # The redundant provider we created before losing the race is torn down.
    dup_provider.shutdown.assert_awaited_once()
    # The stale winner is evicted + shut down by the loser (it was still ours).
    winner_provider.shutdown.assert_awaited_once()
    assert sm._sessions[key].provider is fresh_provider
    assert bootstrap_calls["n"] == 2, "cold start must retry once after the stale winner"


@pytest.mark.asyncio
async def test_open_task_session_reuses_live_session_fast_path(cfg):
    """Happy path: a session that stays live across the semaphore acquire is
    reused (is_new=False), and no cold-start runtime bootstrap happens."""
    sm = SessionManager(cfg)
    key = "task:step3"

    live_provider = _dead_after_probe_provider()  # stays alive
    sess = _Session(provider=live_provider, first_turn=FirstTurnState.NOTHING_ARMED)
    sm._sessions[key] = sess

    sm._get_or_bootstrap_run_runtime = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("cold-start must not run for a live session")
    )

    provider, is_new, resumed = await sm.open_task_session("parent", key)
    assert provider is live_provider
    assert is_new is False
    assert resumed is False
    sm.release(key)


# --------------------------------------------------------------------------
# Bug 2: _resolve_agent_model mtime / TTL invalidation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_agent_model_reresolves_after_json_created(tmp_path, monkeypatch):
    """FAILURE SCENARIO (pre-fix): resolving an agent with no JSON cached
    ``"auto"`` FOREVER. After the agent's JSON is later created with a real
    model, the old code kept returning the stale ``"auto"``.

    POST-FIX: creating the JSON bumps the agents-dir mtime, invalidating the
    cache so the real model is resolved.
    """
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path)
    SessionManager._agent_model_cache = {}  # type: ignore[attr-defined]

    # Miss: no JSON present yet.
    assert SessionManager._resolve_agent_model("myagent") == "auto"

    # Agent config appears later with a pinned model.
    (tmp_path / "myagent.json").write_text(
        json.dumps({"name": "myagent", "model": "claude-sonnet-x"}), encoding="utf-8"
    )
    # Guarantee the dir mtime differs regardless of filesystem timestamp
    # granularity (adding a file already bumps it; make it deterministic).
    st = tmp_path.stat()
    os.utime(tmp_path, (st.st_mtime + 10, st.st_mtime + 10))

    assert SessionManager._resolve_agent_model("myagent") == "claude-sonnet-x"


@pytest.mark.asyncio
async def test_resolve_agent_model_ttl_reresolves_inplace_edit(tmp_path, monkeypatch):
    """An in-place edit of an existing agent JSON does not change the dir mtime,
    so only the TTL can catch it. With TTL elapsed the value is re-resolved."""
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path)
    SessionManager._agent_model_cache = {}  # type: ignore[attr-defined]

    f = tmp_path / "a.json"
    f.write_text(json.dumps({"name": "a", "model": "m1"}), encoding="utf-8")
    assert SessionManager._resolve_agent_model("a") == "m1"

    # Force TTL expiry so the (unchanged-mtime) cache entry is dropped.
    monkeypatch.setattr("kiro_crew.session._AGENT_MODEL_CACHE_TTL", 0.0)
    f.write_text(json.dumps({"name": "a", "model": "m2"}), encoding="utf-8")
    assert SessionManager._resolve_agent_model("a") == "m2"


@pytest.mark.asyncio
async def test_resolve_agent_model_serves_cache_within_ttl(tmp_path, monkeypatch):
    """Within the TTL and with an unchanged dir mtime, a cached resolution is
    served WITHOUT re-globbing the agents dir (proves the cache still works)."""
    monkeypatch.setattr("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path)
    SessionManager._agent_model_cache = {}  # type: ignore[attr-defined]

    (tmp_path / "z.json").write_text(
        json.dumps({"name": "z", "model": "mz"}), encoding="utf-8"
    )
    assert SessionManager._resolve_agent_model("z") == "mz"

    calls = {"n": 0}
    orig_glob = type(tmp_path).glob

    def counting_glob(self, pattern):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return orig_glob(self, pattern)

    monkeypatch.setattr(type(tmp_path), "glob", counting_glob)
    assert SessionManager._resolve_agent_model("z") == "mz"
    assert calls["n"] == 0, "cache hit must not re-glob the agents dir"


# --------------------------------------------------------------------------
# Bug: _reacquire_and_validate must not leak the semaphore on cancellation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reacquire_and_validate_releases_semaphore_on_cancellation(cfg):
    """FAILURE SCENARIO (pre-fix): the caller is cancelled AFTER acquiring the
    per-session semaphore but while still awaiting ``self._lock``. The old code
    let CancelledError propagate out of ``_reacquire_and_validate`` without ever
    releasing the semaphore, so the session key stayed permanently locked and
    every subsequent step for it deadlocked.

    POST-FIX: cancellation while parked on ``self._lock`` releases the semaphore
    before propagating, leaving the key usable.
    """
    sm = SessionManager(cfg)
    key = "sess:reacquire-cancel"

    provider = _dead_after_probe_provider()  # reports alive
    sess = _Session(provider=provider, first_turn=FirstTurnState.NOTHING_ARMED)
    sm._sessions[key] = sess

    # Hold self._lock so the method parks on `async with self._lock:` AFTER it
    # has already acquired sess.semaphore.
    await sm._lock.acquire()
    try:
        task = asyncio.ensure_future(sm._reacquire_and_validate(key, sess))
        # Wait until the method has grabbed the per-session semaphore and is now
        # blocked trying to take self._lock.
        while not sess.semaphore.locked():
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.02)  # ensure it is parked inside the lock acquire

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        sm._lock.release()

    # The semaphore must have been released on cancellation, so the key is free.
    assert not sess.semaphore.locked(), "cancellation must not leak the semaphore"
    # And it is genuinely re-acquirable without blocking.
    await asyncio.wait_for(sess.semaphore.acquire(), timeout=1.0)
    sess.semaphore.release()


# --------------------------------------------------------------------------
# Bug: a reset between the reacquire's permit and its lock wait
# --------------------------------------------------------------------------


async def _until(cond, what: str) -> None:
    """Yield to the loop until *cond* holds; a bounded count, not a wall clock."""
    for _ in range(500):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"timed out waiting for: {what}")


def _live_session() -> _Session:
    provider = _dead_after_probe_provider()  # reports alive
    return _Session(provider=provider, first_turn=FirstTurnState.NOTHING_ARMED)


def _orphans(sm: SessionManager, key: str) -> set:
    return set(sm._lifecycle_boundary().state.orphaned_holders.get(key, set()))


async def _cancel_and_drain(task: asyncio.Task) -> None:
    """Cancel a test's background task and wait for it, so a failed assertion
    never leaves it pending; bounded so a task that ignores the cancel fails
    by name here instead of hanging the worker."""
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)


async def _hand_permit_into_reset_window(sm: SessionManager, key: str, sess: _Session):
    """Drive a turn T, a claimant W parked on its permit, and a reset queued
    on the manager lock, until T's release has handed the permit to W and W
    waits for the lock behind the reset. Returns (t_task, w_task, r_task,
    t_next) with the lock released, so the reset runs next. Setting
    ``t_next`` lets T go on to its next claim on the key.
    """
    t_done = asyncio.Event()
    t_next = asyncio.Event()
    t_claimed_successor = asyncio.Event()

    async def _turn() -> None:
        assert await sm._reacquire_and_validate(key, sess)
        await t_done.wait()
        sm.release(key)
        await t_next.wait()
        successor = sm._sessions[key]
        assert await sm._reacquire_and_validate(key, successor)
        t_claimed_successor.set()
        sm.release(key)

    t_task = asyncio.ensure_future(_turn())
    await _until(lambda: sess.turn_owner is t_task, "T holds the turn")
    w_task = asyncio.ensure_future(sm._reacquire_and_validate(key, sess))
    await _until(lambda: bool(sess.semaphore._waiters), "W parked on the permit")
    await sm._lock.acquire()
    r_task = asyncio.ensure_future(sm.reset(key))
    await _until(lambda: bool(sm._lock._waiters), "the reset queued on the lock")
    t_done.set()
    await _until(lambda: len(sm._lock._waiters or ()) == 2, "W queued behind the reset")
    sm._lock.release()
    return t_task, w_task, r_task, (t_next, t_claimed_successor)


@pytest.mark.asyncio
async def test_a_reset_while_a_woken_claimant_waits_for_the_lock_releases_the_permit_once(cfg):
    """The busy turn's release hands the permit to the parked claimant W, and a
    reset pops the session while W waits for the manager lock. The reset must
    record W, the task holding the permit, and release it once; W then returns
    False without releasing it a second time, instead of raising
    ``ValueError: BoundedSemaphore released too many times``.
    """
    sm = SessionManager(cfg)
    key = "sess:reacquire-reset-window"
    sess = _live_session()
    sm._sessions[key] = sess

    t_task, w_task, r_task, _t_next = await _hand_permit_into_reset_window(sm, key, sess)
    try:
        await asyncio.wait_for(r_task, timeout=5)
        assert await asyncio.wait_for(w_task, timeout=5) is False

        assert t_task not in _orphans(sm, key), "T had already released; it is not the holder"
        assert w_task not in _orphans(sm, key), "W's record is consumed by its own refusal"
        assert not sess.semaphore.locked()
        await asyncio.wait_for(sess.semaphore.acquire(), timeout=1.0)
        sess.semaphore.release()
    finally:
        await _cancel_and_drain(t_task)


@pytest.mark.asyncio
async def test_a_fence_refusal_after_a_reset_in_the_reacquire_window_does_not_over_release(
    cfg, monkeypatch
):
    """Same window, refused at the ending fence instead: the refusal reaches the
    caller as ``SessionEndingError``, not as a ``ValueError`` from releasing a
    permit the reset already released."""
    from kiro_crew.session_allocation import SessionEndingError

    sm = SessionManager(cfg)
    key = "sess:reacquire-reset-fence"
    sess = _live_session()
    sm._sessions[key] = sess
    real_refuse = type(sm._allocation_boundary())._refuse_if_ending

    def _refuse_once_popped(self, k, reservation):
        if k == key and sm._sessions.get(key) is not sess:
            raise SessionEndingError(key)
        return real_refuse(self, k, reservation)

    monkeypatch.setattr(type(sm._allocation_boundary()), "_refuse_if_ending", _refuse_once_popped)

    t_task, w_task, r_task, _t_next = await _hand_permit_into_reset_window(sm, key, sess)
    try:
        await asyncio.wait_for(r_task, timeout=5)
        with pytest.raises(SessionEndingError):
            await asyncio.wait_for(w_task, timeout=5)
        assert not sess.semaphore.locked()
    finally:
        await _cancel_and_drain(t_task)


@pytest.mark.asyncio
async def test_a_reset_in_the_reacquire_window_leaves_the_previous_turns_next_release_genuine(cfg):
    """The turn that handed the permit over is not the holder the reset
    records. Its next claim on the key is a successor's permit, and its release
    of that permit must unlock it rather than be absorbed, or the key stays busy.
    """
    sm = SessionManager(cfg)
    key = "sess:reacquire-reset-leak"
    sess = _live_session()
    sm._sessions[key] = sess

    t_task, w_task, r_task, (t_next, t_claimed) = await _hand_permit_into_reset_window(
        sm, key, sess
    )
    await asyncio.wait_for(r_task, timeout=5)
    await asyncio.gather(asyncio.wait_for(w_task, timeout=5), return_exceptions=True)

    successor = _live_session()
    sm._sessions[key] = successor
    t_next.set()
    await asyncio.wait_for(t_task, timeout=5)
    assert t_claimed.is_set()
    assert not successor.semaphore.locked(), "T's genuine release was absorbed; the key stays busy"
    await asyncio.wait_for(successor.semaphore.acquire(), timeout=1.0)
    successor.semaphore.release()


@pytest.mark.asyncio
async def test_a_reset_while_a_claimant_is_still_parked_wakes_it_and_absorbs_the_holders_release(
    cfg,
):
    """Control for the parked-waiter path: the reset lands while T still holds
    the permit and W is still parked on it. The reset records T and wakes W;
    W finds the session gone and returns False; T's later release is absorbed
    and does not unlock the successor another task now holds."""
    sm = SessionManager(cfg)
    key = "sess:reset-parked-waiter"
    sess = _live_session()
    sm._sessions[key] = sess
    t_done = asyncio.Event()

    async def _turn() -> None:
        assert await sm._reacquire_and_validate(key, sess)
        await t_done.wait()
        sm.release(key)

    t_task = asyncio.ensure_future(_turn())
    await _until(lambda: sess.turn_owner is t_task, "T holds the turn")
    w_task = asyncio.ensure_future(sm._reacquire_and_validate(key, sess))
    await _until(lambda: bool(sess.semaphore._waiters), "W parked on the permit")

    await asyncio.wait_for(sm.reset(key), timeout=5)
    assert t_task in _orphans(sm, key)
    assert await asyncio.wait_for(w_task, timeout=5) is False

    successor = _live_session()
    sm._sessions[key] = successor
    await successor.semaphore.acquire()  # another task's turn on the successor
    t_done.set()
    await asyncio.wait_for(t_task, timeout=5)
    assert successor.semaphore.locked(), "T's release must not unlock the successor's turn"
    assert t_task not in _orphans(sm, key)
    successor.semaphore.release()
