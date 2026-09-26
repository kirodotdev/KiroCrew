"""Cross-process exclusion for history consolidation.

``HistoryConsolidator._running`` is an in-memory set, so it excludes a second
pass inside one process and says nothing about another one. ``kirocrew
consolidate`` IS another process, and it runs against the same transcripts while
the gateway's 60s idle sweep is live. Both would snapshot the same span, both
would spend a provider turn on it, and both would write the history entry,
preferences and lessons that turn produced. Nothing detects it afterwards: the
marker write is idempotent, so the damage is the duplicate spend and the doubled
memory, not a corrupted offset.

These tests pin the durable lease that closes it — who may take it, when a dead
holder's claim stops counting, and that a refused pass leaves every piece of
"this span has been processed" bookkeeping untouched.
"""

import asyncio
import contextlib
import os
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_BUSY,
    _CONSOLIDATION_LEASE_CEILING_SECS,
    _CONSOLIDATION_LEASE_RENEW_SECS,
    _LEASE_AT,
    _LEASE_PID,
    _LEASE_START,
    _LEASE_TOKEN,
    _lease_holder_is_live,
    _no_pass_ran,
)

KEY = "dashboard:chat-lease"


def _seed_log(tmp_path, count: int = 3) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path / "sessions")
    log.init()
    with history_mod.allow_on_loop_persist():
        for i in range(count):
            log.append(KEY, "user", f"m{i}")
    return log


def _make_consolidator(log: ConversationLog, **kw: Any) -> HistoryConsolidator:
    memory = MagicMock()
    memory.read_preferences.return_value = ""
    memory.read_projects.return_value = ""
    kw.setdefault("history_idle_secs", 0)
    kw.setdefault("sessions", None)
    return HistoryConsolidator(log=log, memory=memory, migrated=True, **kw)


def _live_lease(token: str = "someone-else", at: float | None = None) -> dict:
    """A lease record held by a process that demonstrably exists — this one."""
    pid = os.getpid()
    from kiro_crew import platform_compat

    record = {
        _LEASE_TOKEN: token,
        _LEASE_PID: pid,
        _LEASE_AT: time.time() if at is None else at,
    }
    start = platform_compat.get_process_start_id(pid)
    if start:
        record[_LEASE_START] = start
    return record


class TestWhoStillHoldsALease:
    def test_a_record_without_a_pid_is_not_a_lease(self) -> None:
        """An unreadable or half-written record must not wedge a session."""
        now = time.time()
        assert not _lease_holder_is_live({}, now=now)
        assert not _lease_holder_is_live({_LEASE_PID: 0, _LEASE_AT: now}, now=now)
        assert not _lease_holder_is_live({_LEASE_PID: "junk", _LEASE_AT: now}, now=now)

    def test_a_running_holder_still_holds_it(self) -> None:
        assert _lease_holder_is_live(_live_lease(), now=time.time())

    def test_a_dead_holder_does_not(self) -> None:
        with patch("kiro_crew.platform_compat.pid_exists", return_value=False):
            assert not _lease_holder_is_live(_live_lease(), now=time.time())

    def test_a_recycled_pid_does_not(self) -> None:
        """The PID is alive, but it is not the process that took the lease."""
        record = _live_lease()
        record[_LEASE_START] = "a-different-process"
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value="live-token"):
            assert not _lease_holder_is_live(record, now=time.time())

    def test_an_unknowable_identity_is_not_read_as_a_mismatch(self) -> None:
        """An unreadable start identity must not steal a live lease."""
        record = _live_lease()
        record.pop(_LEASE_START, None)
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=None):
            assert _lease_holder_is_live(record, now=time.time())

    def test_the_ceiling_releases_a_holder_that_never_finishes(self) -> None:
        """The backstop for a wedged holder, and for a platform without identity."""
        stale = _live_lease(at=time.time() - _CONSOLIDATION_LEASE_CEILING_SECS - 1)
        assert not _lease_holder_is_live(stale, now=time.time())


class TestTheHeartbeatKeepsALiveHolder:
    """The ceiling measures SILENCE, not how long the pass has run.

    Measured from acquisition, it expired under a holder that was alive and
    working — a long transcript on a slow provider, which is the case that most
    needs the exclusion. A wedged holder stops renewing, so the ceiling still
    bounds the thing it was written to bound.
    """

    def test_the_renew_interval_leaves_room_for_missed_beats(self) -> None:
        assert _CONSOLIDATION_LEASE_RENEW_SECS * 4 <= _CONSOLIDATION_LEASE_CEILING_SECS

    def test_a_renewal_carries_the_holder_past_the_ceiling(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        token = c._acquire_consolidation_lease(KEY)
        assert token
        # Backdate the acquisition past the ceiling, as a pass longer than an
        # hour would leave it.
        with history_mod.allow_on_loop_persist():
            log.update_metadata(
                KEY, {_LEASE_AT: time.time() - _CONSOLIDATION_LEASE_CEILING_SECS - 1}
            )
        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.time())

        assert c._renew_consolidation_lease(KEY, token) is True

        assert _lease_holder_is_live(log.get_metadata(KEY), now=time.time())
        assert c._acquire_consolidation_lease(KEY) is None, "a peer took a live holder's lease"

    def test_a_stale_token_cannot_renew_someone_elses_lease(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        token = c._acquire_consolidation_lease(KEY)
        assert token

        assert c._renew_consolidation_lease(KEY, "not-our-token") is False
        assert log.get_metadata(KEY)[_LEASE_TOKEN] == token

    @pytest.mark.asyncio
    async def test_the_heartbeat_renews_until_cancelled(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.history_consolidation._CONSOLIDATION_LEASE_RENEW_SECS", 0.001
        )
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        token = c._acquire_consolidation_lease(KEY)
        assert token
        beats: list[str] = []

        with patch.object(
            c, "_renew_consolidation_lease", side_effect=lambda k, t: beats.append(t) or True
        ):
            task = asyncio.ensure_future(c._heartbeat_consolidation_lease(KEY, token))
            for _ in range(500):
                if len(beats) >= 3:
                    break
                await asyncio.sleep(0.005)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert beats[:3] == [token, token, token]
        before = len(beats)
        await asyncio.sleep(0.02)
        assert len(beats) == before, "the heartbeat outlived its own cancellation"

    @pytest.mark.asyncio
    async def test_the_heartbeat_stops_once_the_lease_is_someone_elses(
        self, tmp_path, monkeypatch
    ) -> None:
        """A lost lease stops renewal rather than spinning."""
        monkeypatch.setattr(
            "kiro_crew.history_consolidation._CONSOLIDATION_LEASE_RENEW_SECS", 0.001
        )
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_renew_consolidation_lease", return_value=False):
            await asyncio.wait_for(c._heartbeat_consolidation_lease(KEY, "gone"), timeout=5)

    @pytest.mark.asyncio
    async def test_a_pass_renews_while_the_provider_turn_is_in_flight(
        self, tmp_path, monkeypatch
    ) -> None:
        """The renewal has to land during the one await the pass spends."""
        monkeypatch.setattr(
            "kiro_crew.history_consolidation._CONSOLIDATION_LEASE_RENEW_SECS", 0.001
        )
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        renewed: list[str] = []

        async def _turn(_prompt, session_key=None):
            # Stand where the provider turn stands, long enough for a beat.
            for _ in range(200):
                if renewed:
                    break
                await asyncio.sleep(0.005)
            return {"history_entry": "e"}

        with (
            patch.object(c, "_call_llm", _turn),
            patch.object(
                c, "_renew_consolidation_lease", side_effect=lambda k, t: renewed.append(t) or True
            ),
        ):
            await c._consolidate(KEY, include_history=True)

        assert renewed, "the pass never proved its holder was still working"


class TestTakingAndDroppingTheLease:
    def test_a_free_session_can_be_claimed(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        token = c._acquire_consolidation_lease(KEY)
        assert token
        assert log.get_metadata(KEY)[_LEASE_TOKEN] == token

    def test_a_deleted_session_cannot_be_recreated_by_a_lease(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        assert log.delete_session(KEY)

        assert c._acquire_consolidation_lease(KEY) is None
        assert not log._path(KEY).exists()

    @pytest.mark.asyncio
    async def test_persistence_failure_refuses_the_lease_and_the_pass(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        call = AsyncMock()

        with (
            patch.object(log, "_update_metadata_locked", side_effect=OSError("write failed")),
            patch.object(c, "_call_llm", call),
        ):
            assert await asyncio.to_thread(c._acquire_consolidation_lease, KEY) is None
            outcome = await c._consolidate(KEY, include_history=True)

        assert outcome is _CONSOLIDATION_BUSY
        call.assert_not_awaited()
        assert KEY not in c._running
        assert log.unconsolidated_count(KEY) == 3
        assert log.consolidation_retry_state(KEY) == (0, 0.0)

    def test_a_held_session_cannot(self, tmp_path) -> None:
        """Same process, second acquisition: the token is what distinguishes them."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        assert c._acquire_consolidation_lease(KEY)
        assert c._acquire_consolidation_lease(KEY) is None

    def test_releasing_frees_it_for_the_next_pass(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        token = c._acquire_consolidation_lease(KEY)
        assert token
        c._release_consolidation_lease(KEY, token)
        assert c._acquire_consolidation_lease(KEY)

    def test_a_stale_token_cannot_release_someone_elses_lease(self, tmp_path) -> None:
        """A pass that overran the ceiling must not clear the claim that replaced it."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        c._release_consolidation_lease(KEY, "a-token-that-was-never-held")
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, _live_lease(token="the-current-holder"))
        c._release_consolidation_lease(KEY, "a-token-that-was-never-held")

        assert log.get_metadata(KEY)[_LEASE_TOKEN] == "the-current-holder"
        assert c._acquire_consolidation_lease(KEY) is None

    def test_a_dead_holders_lease_is_taken_over(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, _live_lease(token="crashed-mid-pass"))

        with patch("kiro_crew.platform_compat.pid_exists", return_value=False):
            token = c._acquire_consolidation_lease(KEY)

        assert token and token != "crashed-mid-pass"


class TestAPassRefusedByTheLeaseCostsNothing:
    @pytest.mark.asyncio
    async def test_a_held_session_never_reaches_the_provider(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, _live_lease(token="the-other-process"))
        call = AsyncMock(return_value={"history_entry": "e"})

        with patch.object(c, "_call_llm", call):
            outcome = await c._consolidate(KEY, include_history=True)

        assert outcome is _CONSOLIDATION_BUSY
        assert _no_pass_ran(outcome)
        call.assert_not_awaited()
        assert log.unconsolidated_count(KEY) == 3, "the span must stay unconsolidated"
        assert log.consolidation_retry_state(KEY) == (0, 0.0), "a refusal is not an attempt"

    @pytest.mark.asyncio
    async def test_the_holders_lease_survives_the_refusal(self, tmp_path) -> None:
        """The loser's finally block must not release a lease it never took."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, _live_lease(token="the-other-process"))

        with patch.object(c, "_call_llm", AsyncMock(return_value=None)):
            await c._consolidate(KEY, include_history=True)

        assert log.get_metadata(KEY)[_LEASE_TOKEN] == "the-other-process"

    @pytest.mark.asyncio
    async def test_a_prefs_window_is_not_marked_covered(self, tmp_path) -> None:
        """The offset that says "these messages were extracted" must not move.

        ``maybe_consolidate``'s window is an in-memory offset with no channel
        back from the pass. Advancing it for a pass that never ran would skip
        that window until a whole new threshold accumulated, dropping its
        preference and project extraction outright.
        """
        log = _seed_log(tmp_path, count=40)
        c = _make_consolidator(log)
        with history_mod.allow_on_loop_persist():
            log.update_metadata(KEY, _live_lease(token="the-other-process"))

        with patch.object(c, "_call_llm", AsyncMock(return_value={})):
            c.maybe_consolidate(KEY)
            for task in list(c._tasks):
                await task

        assert c._prefs_offset.get(KEY, 0) == 0


class TestTheLeaseDoesNotOutliveItsPass:
    @pytest.mark.asyncio
    async def test_a_completed_pass_frees_the_session(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            await c._consolidate(KEY, include_history=True)

        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.time())
        assert c._acquire_consolidation_lease(KEY)

    @pytest.mark.asyncio
    async def test_a_raising_pass_frees_the_session(self, tmp_path) -> None:
        """The release is in a finally, so a crash mid-pass is not a wedge."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(RuntimeError):
                await c._consolidate(KEY, include_history=True)

        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.time())


@pytest.mark.parametrize("failure", ["unreadable", "write_error", "unicode_error"])
@pytest.mark.asyncio
async def test_transient_renewal_failure_keeps_heartbeat_running(tmp_path, monkeypatch, failure):
    monkeypatch.setattr("kiro_crew.history_consolidation._CONSOLIDATION_LEASE_RENEW_SECS", 0)
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    token = c._acquire_consolidation_lease(KEY)
    assert token
    original_read = log._read_metadata_status
    original_write = log._update_metadata_locked
    calls = 0
    renewed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def read(key):
        nonlocal calls
        calls += 1
        if calls == 1 and failure == "unreadable":
            return {}, False
        return original_read(key)

    def write(key, fields):
        if calls == 1:
            if failure == "write_error":
                raise OSError("temporary write failure")
            if failure == "unicode_error":
                raise UnicodeError("temporary encoding failure")
        original_write(key, fields)
        loop.call_soon_threadsafe(renewed.set)

    with (
        patch.object(log, "_read_metadata_status", read),
        patch.object(log, "_update_metadata_locked", write),
    ):
        task = asyncio.create_task(c._heartbeat_consolidation_lease(KEY, token))
        try:
            await asyncio.wait_for(renewed.wait(), timeout=5)
            assert calls >= 2
            assert not task.done()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def test_acquisition_clears_previous_start_identity(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    log.update_metadata(KEY, {_LEASE_START: "previous-holder"})
    with patch("kiro_crew.platform_compat.get_process_start_id", return_value=None):
        token = c._acquire_consolidation_lease(KEY)
    assert token
    assert log.get_metadata(KEY)[_LEASE_START] is None
    with patch("kiro_crew.platform_compat.get_process_start_id", return_value="recovered"):
        assert c._acquire_consolidation_lease(KEY) is None


@pytest.mark.parametrize("operation", ["acquire", "renew", "release"])
def test_lease_cas_ignores_cached_metadata_after_peer_write(tmp_path, operation):
    log = _seed_log(tmp_path)
    peer = ConversationLog(base_dir=tmp_path / "sessions")
    c = _make_consolidator(log)
    token = c._acquire_consolidation_lease(KEY)
    assert token
    if operation == "acquire":
        c._release_consolidation_lease(KEY, token)
    stale = log.get_metadata(KEY).copy()
    peer_claim = _live_lease("peer-token")
    peer.update_metadata(KEY, peer_claim)
    # Model a stat identity collision while retaining this process's stale view.
    identity = log._cache_identity(log._path(KEY).stat())
    log._meta_cache[KEY] = (identity, log._cache_gen(KEY), stale)
    assert log.get_metadata(KEY) == stale

    if operation == "acquire":
        assert c._acquire_consolidation_lease(KEY) is None
    elif operation == "renew":
        assert c._renew_consolidation_lease(KEY, token) is False
    else:
        c._release_consolidation_lease(KEY, token)

    peer._meta_cache.pop(KEY, None)
    observed = peer.get_metadata(KEY)
    for field, value in peer_claim.items():
        assert observed[field] == value


@pytest.mark.parametrize("loss", ["peer", "heartbeat_error"])
@pytest.mark.asyncio
async def test_lease_loss_cancels_provider_before_durable_writes(tmp_path, monkeypatch, loss):
    monkeypatch.setattr("kiro_crew.history_consolidation._CONSOLIDATION_LEASE_RENEW_SECS", 0)
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    peer = ConversationLog(base_dir=tmp_path / "sessions")
    peer_claim = _live_lease("replacement-token")
    started = asyncio.Event()
    cancelled = asyncio.Event()
    original_renew = c._renew_consolidation_lease

    async def turn(*args, **kwargs):
        await asyncio.to_thread(peer.update_metadata, KEY, peer_claim)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    def renew(key, token):
        if not started.is_set():
            return True
        if loss == "heartbeat_error":
            raise RuntimeError("unexpected heartbeat failure")
        return original_renew(key, token)

    with (
        patch.object(c, "_call_llm", turn),
        patch.object(c, "_renew_consolidation_lease", renew),
        patch.object(log, "mark_consolidated") as mark,
        patch.object(c, "_note_failed_attempt", new_callable=AsyncMock) as failed,
        patch.object(c, "_note_environment_failure", new_callable=AsyncMock) as environment,
    ):
        outcome = await asyncio.wait_for(c._consolidate(KEY), timeout=5)
    assert _no_pass_ran(outcome)
    assert cancelled.is_set()
    c._memory.append_history.assert_not_called()
    c._memory.write_preferences.assert_not_called()
    mark.assert_not_called()
    failed.assert_not_awaited()
    environment.assert_not_awaited()
    peer._meta_cache.pop(KEY, None)
    assert peer.get_metadata(KEY)[_LEASE_TOKEN] == peer_claim[_LEASE_TOKEN]
    assert KEY not in c._running
