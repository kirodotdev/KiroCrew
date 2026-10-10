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
import json
import os
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import history as history_mod
from kiro_crew import history_consolidation
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.history_consolidation import (
    _CONSOLIDATION_BUSY,
    _CONSOLIDATION_LEASE_CEILING_SECS,
    _CONSOLIDATION_LEASE_RENEW_SECS,
    _LEASE_AT,
    _LEASE_INCARNATION,
    _LEASE_MONOTONIC,
    _LEASE_PID,
    _LEASE_START,
    _LEASE_TOKEN,
    _lease_holder_is_live,
    _LeaseLostMidRun,
    _no_pass_ran,
    _RunCommitState,
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
        _LEASE_AT: time.time(),
        _LEASE_MONOTONIC: time.monotonic() if at is None else at,
        _LEASE_INCARNATION: platform_compat.process_incarnation_id(pid),
    }
    start = platform_compat.get_process_start_id(pid)
    if start:
        record[_LEASE_START] = start
    return record


class TestWhoStillHoldsALease:
    def test_a_record_without_a_pid_is_not_a_lease(self) -> None:
        """An unreadable or half-written record must not wedge a session."""
        now = time.monotonic()
        assert not _lease_holder_is_live({}, now=now)
        assert not _lease_holder_is_live({_LEASE_PID: 0, _LEASE_AT: now}, now=now)
        assert not _lease_holder_is_live({_LEASE_PID: "junk", _LEASE_AT: now}, now=now)

    def test_a_running_holder_still_holds_it(self) -> None:
        assert _lease_holder_is_live(_live_lease(), now=time.monotonic())

    def test_a_dead_holder_does_not(self) -> None:
        with patch("kiro_crew.platform_compat.pid_exists", return_value=False):
            assert not _lease_holder_is_live(_live_lease(), now=time.monotonic())

    def test_a_recycled_pid_does_not(self) -> None:
        """The PID is alive, but it is not the process that took the lease."""
        record = _live_lease()
        record[_LEASE_START] = "a-different-process"
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value="live-token"):
            assert not _lease_holder_is_live(record, now=time.monotonic())

    def test_an_unknowable_identity_is_not_read_as_a_mismatch(self) -> None:
        """An unreadable start identity must not steal a live lease."""
        record = _live_lease()
        record.pop(_LEASE_START, None)
        with patch("kiro_crew.platform_compat.get_process_start_id", return_value=None):
            assert _lease_holder_is_live(record, now=time.monotonic())

    def test_the_ceiling_releases_a_holder_that_never_finishes(self) -> None:
        """The backstop for a wedged holder, and for a platform without identity."""
        stale = _live_lease(at=time.monotonic() - _CONSOLIDATION_LEASE_CEILING_SECS - 1)
        assert not _lease_holder_is_live(stale, now=time.monotonic())


class TestTheHeartbeatKeepsALiveHolder:
    """Renewal keeps a working pass excluded without a maximum pass duration."""

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
                KEY, {_LEASE_MONOTONIC: time.monotonic() - _CONSOLIDATION_LEASE_CEILING_SECS - 1}
            )
        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.monotonic())

        assert c._renew_consolidation_lease(KEY, token) is True

        assert _lease_holder_is_live(log.get_metadata(KEY), now=time.monotonic())
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
        loop = asyncio.get_running_loop()
        in_flight = asyncio.Event()
        completed = asyncio.Event()
        release = threading.Event()

        def renew(k, t):
            beats.append(t)
            if len(beats) == 4:
                loop.call_soon_threadsafe(in_flight.set)
                assert release.wait(5), "test did not release in-flight renewal"
                loop.call_soon_threadsafe(completed.set)
            return True

        with patch.object(c, "_renew_consolidation_lease", side_effect=renew):
            task = asyncio.ensure_future(c._heartbeat_consolidation_lease(KEY, token))
            try:
                await asyncio.wait_for(in_flight.wait(), timeout=5)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                release.set()
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await asyncio.wait_for(completed.wait(), timeout=5)

        assert beats == [token] * 4, "the heartbeat outlived its own cancellation"

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

        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.monotonic())
        assert c._acquire_consolidation_lease(KEY)

    @pytest.mark.asyncio
    async def test_a_raising_pass_frees_the_session(self, tmp_path) -> None:
        """The release is in a finally, so a crash mid-pass is not a wedge."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(side_effect=RuntimeError("boom"))):
            with pytest.raises(RuntimeError):
                await c._consolidate(KEY, include_history=True)

        assert not _lease_holder_is_live(log.get_metadata(KEY), now=time.monotonic())


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


@pytest.mark.parametrize("operation", ["acquire", "renew", "release", "cas"])
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
    before_mtime = log._path(KEY).stat().st_mtime_ns
    peer.update_metadata(KEY, peer_claim)
    assert log._path(KEY).stat().st_mtime_ns == before_mtime
    # Model a stat identity collision while retaining this process's stale view.
    identity = log._cache_identity(log._path(KEY).stat())
    log._meta_cache[KEY] = (identity, log._cache_gen(KEY), stale)
    assert log.get_metadata(KEY) == stale

    if operation == "acquire":
        assert c._acquire_consolidation_lease(KEY) is None
    elif operation == "renew":
        assert c._renew_consolidation_lease(KEY, token) is False
    elif operation == "release":
        c._release_consolidation_lease(KEY, token)
    else:
        assert not log.update_metadata_if(
            KEY, {_LEASE_TOKEN: "incorrect"}, lambda meta: meta[_LEASE_TOKEN] == token
        )

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


@pytest.mark.parametrize("jump", [-86400, 86400])
def test_wall_clock_correction_does_not_expire_live_lease(tmp_path, monkeypatch, jump):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    token = c._acquire_consolidation_lease(KEY)
    assert token
    wall = time.time()
    monkeypatch.setattr(
        history_consolidation,
        "_time",
        SimpleNamespace(time=lambda: wall + jump, monotonic=time.monotonic, sleep=time.sleep),
    )
    assert c._acquire_consolidation_lease(KEY) is None
    assert c._renew_consolidation_lease(KEY, token) is True
    assert c._acquire_consolidation_lease(KEY) is None


@pytest.mark.parametrize("heartbeat", [None, "bad", float("nan"), float("inf")])
def test_legacy_or_invalid_heartbeat_retains_live_holder(heartbeat):
    record = _live_lease()
    record[_LEASE_AT] = 1
    if heartbeat is None:
        record.pop(_LEASE_MONOTONIC)
    else:
        record[_LEASE_MONOTONIC] = heartbeat
    assert _lease_holder_is_live(record, now=time.monotonic())
    with patch("kiro_crew.platform_compat.pid_exists", return_value=False):
        assert not _lease_holder_is_live(record, now=time.monotonic())


def test_row_first_file_never_grants_an_unpersisted_lease(tmp_path):
    # A session whose first line is JSON but not a metadata record (a legacy
    # row-first file) reads as an EMPTY metadata record. The locked write then
    # skips the line, so the CAS must report failure: two processes answering
    # "success" would both hold a lease that exists nowhere on disk and
    # consolidate the same span twice.
    log = _seed_log(tmp_path)
    path = log._path(KEY)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[0] = json.dumps(["legacy-row"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log._meta_cache.pop(KEY, None)

    assert not log.update_metadata_if(
        KEY,
        {_LEASE_TOKEN: "ghost"},
        lambda meta: not _lease_holder_is_live(meta, now=time.monotonic()),
        require_write=True,
    )
    c = _make_consolidator(log)
    assert c._acquire_consolidation_lease(KEY) is None
    # The row-first line is still there, untouched.
    assert json.loads(path.read_text(encoding="utf-8").splitlines()[0]) == ["legacy-row"]


@pytest.mark.parametrize("now", [10, 10000])
def test_reboot_releases_reused_pid_and_start_ticks(now):
    record = _live_lease(at=100)
    record[_LEASE_START] = "123"
    record[_LEASE_INCARNATION] = "123:previous-boot"
    with (
        patch("kiro_crew.platform_compat.get_process_start_id", return_value="123"),
        patch("kiro_crew.platform_compat.process_incarnation_id", return_value="123:new-boot"),
    ):
        assert not _lease_holder_is_live(record, now=now)


def test_unknown_reboot_identity_and_future_stamp_retain_holder():
    record = _live_lease(at=10000)
    with patch("kiro_crew.platform_compat.process_incarnation_id", return_value=None):
        assert _lease_holder_is_live(record, now=10)


@pytest.mark.asyncio
async def test_busy_pass_does_not_snapshot_or_copy(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    token = c._acquire_consolidation_lease(KEY)
    assert token
    with (
        patch.object(log, "snapshot_for_consolidation") as snapshot,
        patch("kiro_crew.history_consolidation.copy.deepcopy") as copy_span,
    ):
        assert await c._consolidate(KEY) is _CONSOLIDATION_BUSY
    snapshot.assert_not_called()
    copy_span.assert_not_called()
    c._release_consolidation_lease(KEY, token)


@pytest.mark.asyncio
async def test_snapshot_follows_peer_completion_before_acquisition(tmp_path):
    log = _seed_log(tmp_path)
    peer = ConversationLog(base_dir=tmp_path / "sessions")
    c = _make_consolidator(log)
    acquire = c._acquire_consolidation_lease

    def handoff(key):
        peer.mark_consolidated(key, 3)
        return acquire(key)

    with (
        patch.object(c, "_acquire_consolidation_lease", handoff),
        patch.object(c, "_call_llm", new_callable=AsyncMock) as turn,
    ):
        assert await c._consolidate(KEY) is None
    turn.assert_not_awaited()
    assert log.get_metadata(KEY)[_LEASE_TOKEN] is None


@pytest.mark.parametrize("exit_path", ["backoff", "snapshot_error", "cancel"])
@pytest.mark.asyncio
async def test_lease_released_on_early_exit(tmp_path, exit_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    if exit_path == "backoff":
        await asyncio.to_thread(
            log.update_metadata, KEY, {"consolidation_retry_at": time.time() + 100}
        )
        assert _no_pass_ran(await c._consolidate(KEY))
    elif exit_path == "snapshot_error":
        with patch.object(log, "snapshot_for_consolidation", side_effect=OSError("snapshot")):
            with pytest.raises(OSError, match="snapshot"):
                await c._consolidate(KEY)
    else:
        started = asyncio.Event()

        async def turn(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        with patch.object(c, "_call_llm", turn):
            task = asyncio.create_task(c._consolidate(KEY))
            try:
                await asyncio.wait_for(started.wait(), timeout=5)
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
    assert log.get_metadata(KEY)[_LEASE_TOKEN] is None
    assert KEY not in c._running


@pytest.mark.asyncio
async def test_cancellation_during_acquisition_releases_worker_claim(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    acquire = c._acquire_consolidation_lease
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked_acquire(key):
        token = acquire(key)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        return token

    with patch.object(c, "_acquire_consolidation_lease", blocked_acquire):
        task = asyncio.create_task(c._consolidate(KEY))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            task.cancel()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
    assert log.get_metadata(KEY)[_LEASE_TOKEN] is None


@pytest.mark.asyncio
async def test_billed_source_recheck_refusal_leaves_completion_unrecorded(tmp_path):
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)

    async def turn(*args, **kwargs):
        await asyncio.to_thread(log.append, KEY, "user", "new user turn")
        return {"history_entry": "obsolete"}

    with patch.object(c, "_call_llm", side_effect=turn) as billed:
        assert _no_pass_ran(await c._consolidate(KEY))
    billed.assert_awaited_once()
    c._memory.append_history.assert_not_called()
    assert log.unconsolidated_count(KEY) == 4
    assert log.get_metadata(KEY)[_LEASE_TOKEN] is None


class TestATakenOverLeaseCannotPublish:
    """A holder that overran the ceiling must not publish the span it lost.

    Renewal failures lasting past the ceiling let a peer claim the same
    unconsolidated span and begin its own pass. The old holder's heartbeat only
    notices at its next renewal, so both passes reach publication — and
    a token fence alone cannot recognise an earlier committed output. Receipts
    prevent the replacement holder from publishing that output twice.
    """

    @pytest.mark.asyncio
    async def test_a_stale_holder_publishes_nothing(self, tmp_path) -> None:
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)
        peer = ConversationLog(base_dir=tmp_path / "sessions")
        peer_claim = _live_lease("replacement-token")

        async def turn(*args: Any, **kwargs: Any) -> dict:
            # The takeover lands during the provider turn, exactly where the
            # holder is least able to notice it: the next renewal is minutes away.
            await asyncio.to_thread(peer.update_metadata, KEY, peer_claim)
            return {"history_entry": "duplicate", "preferences_update": "prefs"}

        with (
            patch.object(c, "_call_llm", turn),
            patch.object(log, "mark_consolidated") as mark,
        ):
            outcome = await c._consolidate(KEY, include_history=True)

        assert _no_pass_ran(outcome), "a pass that published nothing is not a completed pass"
        c._memory.append_history.assert_not_called()
        mark.assert_not_called()
        assert log.unconsolidated_count(KEY) == 3, "the span belongs to its new holder"
        peer._meta_cache.pop(KEY, None)
        assert peer.get_metadata(KEY)[_LEASE_TOKEN] == "replacement-token"
        assert KEY not in c._running
        assert log.consolidation_retry_state(KEY) == (0, 0.0), "a takeover is not a failure"

    @pytest.mark.asyncio
    async def test_the_new_holder_can_still_publish(self, tmp_path) -> None:
        """The fence refuses the stale holder, not the session."""
        log = _seed_log(tmp_path)
        c = _make_consolidator(log)

        with patch.object(c, "_call_llm", AsyncMock(return_value={"history_entry": "e"})):
            assert not _no_pass_ran(await c._consolidate(KEY, include_history=True))

        c._memory.append_history.assert_called_once()
        assert log.unconsolidated_count(KEY) == 0


@pytest.mark.parametrize("write", ["publication", "marker"])
def test_the_lease_fence_reads_the_line_not_the_cache(tmp_path, write):
    """A peer takeover must not be hidden by this process's cached metadata.

    The cache is keyed on a stat identity, and an mtime-preserving rewrite is
    exactly what a peer's metadata write looks like. The fence therefore drops the
    entry before reading, the same precaution ``update_metadata_if`` takes for its
    guard.
    """
    log = _seed_log(tmp_path)
    c = _make_consolidator(log)
    token = c._acquire_consolidation_lease(KEY)
    assert token
    stale = log.get_metadata(KEY)
    peer = ConversationLog(base_dir=tmp_path / "sessions")
    peer.update_metadata(KEY, _live_lease("replacement-token"))
    # Model a stat identity collision while retaining this process's stale view.
    identity = log._cache_identity(log._path(KEY).stat())
    log._meta_cache[KEY] = (identity, log._cache_gen(KEY), stale)
    assert log.get_metadata(KEY) == stale

    if write == "publication":
        with pytest.raises(_LeaseLostMidRun):
            with c._publication_hold_checked(KEY, _RunCommitState(token)):
                pass
    else:
        with pytest.raises(_LeaseLostMidRun):
            c._mark_consolidated_fenced(KEY, 1, None, token)

    assert log.unconsolidated_count(KEY) == 3
    peer._meta_cache.pop(KEY, None)
    assert peer.get_metadata(KEY)[_LEASE_TOKEN] == "replacement-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("append_tail", [False, True])
async def test_takeover_after_history_commit_does_not_republish(tmp_path, monkeypatch, append_tail):
    from kiro_crew.memory import MemoryStore

    monkeypatch.setattr(history_consolidation, "_CONSOLIDATION_LEASE_RENEW_SECS", 60)
    log = _seed_log(tmp_path)
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.init()
    holder = _make_consolidator(log)
    holder._memory = memory
    peer_log = ConversationLog(base_dir=tmp_path / "sessions")
    peer = _make_consolidator(peer_log)
    peer._memory = MemoryStore(workspace=tmp_path / "workspace")
    dated_path = memory._history_dir / "2026-10-07.md"
    monkeypatch.setattr(memory, "_today_history_file", lambda: dated_path)
    monkeypatch.setattr(peer._memory, "_today_history_file", lambda: dated_path)
    loop = asyncio.get_running_loop()
    published = asyncio.Event()
    release = threading.Event()
    real_mark = holder._mark_consolidated_fenced

    def paused_mark(*args):
        loop.call_soon_threadsafe(published.set)
        assert release.wait(5), "test did not release old holder"
        return real_mark(*args)

    monkeypatch.setattr(holder, "_mark_consolidated_fenced", paused_mark)
    holder._call_llm = AsyncMock(return_value={"history_entry": "first publication"})
    peer._call_llm = AsyncMock(return_value={"history_entry": "peer replay"})
    task = asyncio.create_task(holder._consolidate(KEY))
    try:
        await asyncio.wait_for(published.wait(), 5)
        if append_tail:
            await asyncio.to_thread(peer_log.append, KEY, "user", "new tail")
        await asyncio.to_thread(
            peer_log.update_metadata,
            KEY,
            {_LEASE_MONOTONIC: time.monotonic() - _CONSOLIDATION_LEASE_CEILING_SECS - 1},
        )
        assert not _no_pass_ran(await asyncio.wait_for(peer._consolidate(KEY), 5))
    finally:
        release.set()
    assert _no_pass_ran(await asyncio.wait_for(task, 5))
    history = memory._today_history_file().read_text()
    assert history.count("first publication") == 1
    assert "peer replay" not in history
    assert peer_log.unconsolidated_count(KEY) == int(append_tail)
    if append_tail:
        peer._call_llm.return_value = {"history_entry": "new tail publication"}
        assert not _no_pass_ran(await peer._consolidate(KEY))
        assert "new tail publication" in memory._today_history_file().read_text()
        assert peer_log.unconsolidated_count(KEY) == 0


@pytest.mark.parametrize("failure_stage", ["replace", "index"])
def test_history_receipt_is_atomic_with_output(tmp_path, monkeypatch, failure_stage):
    from kiro_crew.memory import MemoryStore

    memory = MemoryStore(workspace=tmp_path)
    memory.init()
    dated_path = memory._history_dir / "2026-10-07.md"
    monkeypatch.setattr(memory, "_today_history_file", lambda: dated_path)
    method = "_atomic_write_text" if failure_stage == "replace" else "_index_file"
    with monkeypatch.context() as scoped:
        scoped.setattr(memory, method, MagicMock(side_effect=OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            memory.append_history("committed once", publication_id="a" * 64)
    peer = MemoryStore(workspace=tmp_path)
    monkeypatch.setattr(peer, "_today_history_file", lambda: dated_path)
    peer.append_history("committed once", publication_id="a" * 64)
    assert peer._today_history_file().read_text().count("committed once") == 1
    assert peer.search("committed once")


def test_history_receipt_survives_midnight_and_distinguishes_spans(tmp_path, monkeypatch):
    from kiro_crew.memory import MemoryStore

    memory = MemoryStore(workspace=tmp_path)
    memory.init()
    yesterday = memory._history_dir / "2026-10-06.md"
    monkeypatch.setattr(memory, "_today_history_file", lambda: yesterday)
    memory.append_history("same words", publication_id="a" * 64)
    peer = MemoryStore(workspace=tmp_path)
    today = peer._history_dir / "2026-10-07.md"
    monkeypatch.setattr(peer, "_today_history_file", lambda: today)
    peer.append_history("same words", publication_id="a" * 64)
    assert not today.exists()
    peer.append_history("same words", publication_id="b" * 64)
    assert yesterday.read_text().count("same words") == 1
    assert today.read_text().count("same words") == 1


@pytest.mark.parametrize("document", ["preferences", "projects"])
def test_document_receipt_preserves_owner_edit_on_replay(tmp_path, document):
    from kiro_crew.memory import MemoryStore

    memory = MemoryStore(workspace=tmp_path)
    memory.init()
    read = getattr(memory, f"read_{document}")
    write = getattr(memory, f"write_{document}")
    baseline = read()
    assert write(baseline + "\n- extracted\n", expected_baseline=baseline, publication_id="a" * 64)
    owner_edit = read().replace("extracted", "owner edit")
    assert write(owner_edit)
    assert write(baseline + "\n- replay\n", expected_baseline=baseline, publication_id="a" * 64)
    assert read() == owner_edit
    assert "consolidation-publication" not in read()
    assert write(read() + "\n- next span\n", expected_baseline=read(), publication_id="b" * 64)


def test_receipt_refuses_replaced_source_rows(tmp_path):
    from kiro_crew.memory import MemoryStore

    memory = MemoryStore(workspace=tmp_path)
    memory.init()
    rows = [{"role": "user", "content": "original"}]
    memory.append_history("entry", publication_id="a" * 64, source_messages=rows)
    assert memory.consolidation_history_prefix("a" * 64, rows) == 1
    with pytest.raises(ValueError, match="source changed"):
        memory.consolidation_history_prefix("a" * 64, [{"role": "user", "content": "changed"}])


@pytest.mark.asyncio
async def test_empty_result_after_takeover_cannot_abandon_peer_span(tmp_path, monkeypatch):
    log = _seed_log(tmp_path)
    holder = _make_consolidator(log)
    peer = ConversationLog(base_dir=tmp_path / "sessions")
    monkeypatch.setattr(history_consolidation, "_CONSOLIDATION_LEASE_RENEW_SECS", 60)
    monkeypatch.setattr(history_consolidation, "_CONSOLIDATION_MAX_ATTEMPTS", 1)

    async def turn(*args, **kwargs):
        await asyncio.to_thread(peer.update_metadata, KEY, _live_lease("replacement-token"))
        return {}

    holder._call_llm = turn
    await holder._consolidate(KEY)
    assert peer.unconsolidated_count(KEY) == 3
    assert peer.get_metadata(KEY)[_LEASE_TOKEN] == "replacement-token"
    holder._memory.append_history.assert_not_called()


@pytest.mark.asyncio
async def test_history_receipt_distinguishes_recreated_transcript(tmp_path, monkeypatch):
    from kiro_crew.memory import MemoryStore

    log = _seed_log(tmp_path)
    memory = MemoryStore(workspace=tmp_path / "workspace")
    memory.init()
    dated_path = memory._history_dir / "2026-10-07.md"
    monkeypatch.setattr(memory, "_today_history_file", lambda: dated_path)
    holder = _make_consolidator(log)
    holder._memory = memory
    holder._call_llm = AsyncMock(return_value={"history_entry": "original transcript"})
    await asyncio.to_thread(log.update_metadata, KEY, {"created_at": "2026-10-06T12:00:00+00:00"})
    assert not _no_pass_ran(await holder._consolidate(KEY))
    assert await asyncio.to_thread(log.delete_session, KEY)
    for i in range(3):
        await asyncio.to_thread(log.append, KEY, "user", f"replacement {i}")
    await asyncio.to_thread(log.update_metadata, KEY, {"created_at": "2026-10-07T12:00:00+00:00"})
    holder._call_llm.return_value = {"history_entry": "recreated transcript"}
    assert not _no_pass_ran(await holder._consolidate(KEY))
    assert log.unconsolidated_count(KEY) == 0
    history = dated_path.read_text()
    assert history.count("original transcript") == 1
    assert history.count("recreated transcript") == 1


@pytest.mark.parametrize("document", ["preferences", "projects"])
def test_document_receipts_are_bounded_and_hidden_from_injection(tmp_path, document):
    from kiro_crew.memory import (
        _CONSOLIDATION_RECEIPT_LIMIT,
        _CONSOLIDATION_RECEIPT_RE,
        MemoryStore,
    )

    memory = MemoryStore(workspace=tmp_path)
    memory.init()
    read = getattr(memory, f"read_{document}")
    write = getattr(memory, f"write_{document}")
    path = getattr(memory, f"_{document}_file")
    content = "# Active Projects\n\n## Test Project\n- extracted\n"
    for i in range(_CONSOLIDATION_RECEIPT_LIMIT + 2):
        assert write(content, publication_id=f"{i:064x}")
    receipts = _CONSOLIDATION_RECEIPT_RE.findall(path.read_text())
    assert len(receipts) == _CONSOLIDATION_RECEIPT_LIMIT
    assert f"{0:064x}" not in path.read_text()
    assert f"{_CONSOLIDATION_RECEIPT_LIMIT + 1:064x}" in path.read_text()
    assert "consolidation-publication" not in read()
    assert "consolidation-publication" not in memory._guarded_entry(path)["content"]
    if document == "projects":
        assert "consolidation-publication" not in memory._projects_section(20000)
        assert "consolidation-publication" not in memory.activity_index(cap=20000)
    assert write(content + "replay", publication_id=f"{_CONSOLIDATION_RECEIPT_LIMIT + 1:064x}")
    assert "replay" not in read()
