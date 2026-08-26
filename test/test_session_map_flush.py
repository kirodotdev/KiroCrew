"""SessionMap's deferred snapshot flush: off-loop writes that lose nothing.

Issue #2405. On the event loop a mutation marks the map dirty and a debounced
flush task hands an ALREADY SERIALIZED snapshot to a worker thread; ``_data``
never crosses the thread boundary and ``_MAP_LOCK`` is never held across the
await (ratcheted in ``test_session_map_locking.py``). Off the loop, writes stay
inline. Pinned here:

1. a burst of loop-side mutations pays ZERO inline writes and coalesces to one
   file write (this is the fails-before test: pre-deferral, every mutation
   called ``os.replace`` synchronously on the loop);
2. a mutation landing while a flush is in flight is still on disk after the
   map settles — coalescing never drops the trailing write;
3. the payload handed to the worker thread is an immutable snapshot, not
   ``_data``;
4. sync contexts (no running loop) keep writing immediately;
5. ``get()``'s stale-entry prune returns ``None`` immediately and reaches disk
   after the flush, not inline;
6. ``flush()`` is deterministic, and a stale in-flight payload can never land
   over a newer forced write.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import patch

import pytest

from kiro_crew.session_map import SESSION_MAP_FILENAME, SessionMap


@pytest.fixture
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


def _map_file(tmp_path) -> dict:
    return json.loads((tmp_path / SESSION_MAP_FILENAME).read_text(encoding="utf-8"))


async def _settle(sm: SessionMap) -> None:
    """Wait until every scheduled flush has retired (deterministic, no sleeps)."""
    while True:
        task = sm._flush_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _await_event(event: threading.Event, what: str) -> None:
    """Await a thread-side event with a deadline, failing on the point at issue.

    An unbounded spin would hang until the repo-wide ``--timeout`` fires with
    an unhelpful stack instead of failing on the assertion the test makes.
    """

    async def _poll() -> None:
        while not event.is_set():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(_poll(), timeout=10)
    except asyncio.TimeoutError:
        pytest.fail(f"timed out waiting for {what}")


class TestDeferredFlush:
    @pytest.mark.asyncio
    async def test_burst_pays_no_inline_write_and_coalesces_to_one(self, session_map, tmp_path):
        """The win, proven not asserted: pre-deferral this counts one
        ``os.replace`` per mutation, all synchronous on the loop thread; now the
        burst itself performs ZERO renames and the settle performs exactly one,
        off the loop thread.
        """
        import kiro_crew.session_map as mod

        loop_thread = threading.current_thread()
        renames: list[threading.Thread] = []
        # NOTE: mod.os IS the os module, so this patch is process-global, not
        # module-local. Harmless under xdist process isolation; do not widen.
        real_replace = mod.os.replace

        def counting_replace(src, dst):
            renames.append(threading.current_thread())
            return real_replace(src, dst)

        with patch.object(mod.os, "replace", counting_replace):
            for i in range(20):
                session_map.set(f"dashboard:burst-{i}", f"sid-{i}")
            inline_renames = len(renames)
            await _settle(session_map)

        assert inline_renames == 0, "a loop-side mutation still paid the file write inline"
        assert len(renames) == 1, "one debounce window must produce exactly one write"
        assert renames[0] is not loop_thread, "the write ran on the event-loop thread"
        persisted = _map_file(tmp_path)
        assert {k: v["sid"] for k, v in persisted.items()} == {
            f"dashboard:burst-{i}": f"sid-{i}" for i in range(20)
        }

    @pytest.mark.asyncio
    async def test_trailing_mutation_survives_inflight_flush(self, session_map, tmp_path):
        """A mutation arriving while a flush is writing is not dropped."""
        entered = threading.Event()
        release = threading.Event()
        real = SessionMap._write_payload

        def gated(self, payload, seq):
            entered.set()
            assert release.wait(timeout=10)
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", gated):
            session_map.set("dashboard:first", "sid-first")
            await _await_event(entered, "the gated flush write to start")
            # The flush thread is mid-write with a snapshot that predates this:
            session_map.set("dashboard:trailing", "sid-trailing")
            release.set()
            await _settle(session_map)

        persisted = _map_file(tmp_path)
        assert persisted["dashboard:trailing"]["sid"] == "sid-trailing"
        assert persisted["dashboard:first"]["sid"] == "sid-first"

    @pytest.mark.asyncio
    async def test_payload_is_a_snapshot_not_data(self, session_map, tmp_path):
        """Mutating ``_data`` after the handoff must not change what lands."""
        entered = threading.Event()
        release = threading.Event()
        handed: list[object] = []
        real = SessionMap._write_payload

        def gated(self, payload, seq):
            handed.append(payload)
            entered.set()
            assert release.wait(timeout=10)
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", gated):
            session_map.set("dashboard:snap", "sid-original")
            await _await_event(entered, "the gated flush write to start")
            # Serialization already happened; poke the live structure directly
            # (bypassing _save so no second flush is owed).
            session_map._data["dashboard:snap"]["sid"] = "MUTATED-AFTER-HANDOFF"
            release.set()
            await _settle(session_map)

        assert handed and not isinstance(
            handed[0], dict
        ), "the executor was handed a live structure, not a serialized payload"
        assert json.loads(handed[0])["dashboard:snap"]["sid"] == "sid-original"
        assert _map_file(tmp_path)["dashboard:snap"]["sid"] == "sid-original"

    def test_no_running_loop_writes_immediately(self, session_map, tmp_path):
        """Sync contexts (CLI, worker threads, plain tests) keep inline writes."""
        session_map.set("dashboard:sync", "sid-sync")
        assert _map_file(tmp_path)["dashboard:sync"]["sid"] == "sid-sync"

    @pytest.mark.asyncio
    async def test_get_defers_the_prune_write(self, session_map, tmp_path, monkeypatch):
        """A stale entry: ``None`` now, removal in memory now, on disk after."""
        sessions_dir = tmp_path / "kiro-sessions"
        sessions_dir.mkdir()
        monkeypatch.setattr("kiro_crew.session_map._KIRO_SESSIONS_DIR", sessions_dir)
        session_map.set("dashboard:stale", "sid-gone")
        session_map.flush()
        assert _map_file(tmp_path)["dashboard:stale"]["sid"] == "sid-gone"

        entered = threading.Event()
        release = threading.Event()
        real = SessionMap._write_payload

        def gated(self, payload, seq):
            entered.set()
            assert release.wait(timeout=10)
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", gated):
            # sid-gone has no session file → get prunes. The return and the
            # in-memory removal are immediate; the write is not.
            assert session_map.get("dashboard:stale") is None
            assert "dashboard:stale" not in session_map._data
            assert (
                _map_file(tmp_path)["dashboard:stale"]["sid"] == "sid-gone"
            ), "the prune paid its file write inline on the loop"
            release.set()
            await _settle(session_map)
        assert "dashboard:stale" not in _map_file(tmp_path)

    @pytest.mark.asyncio
    async def test_flush_is_deterministic_durability(self, session_map, tmp_path):
        """After ``flush()`` returns, the file is current — no awaiting needed."""
        session_map.set("dashboard:durable", "sid-durable")
        session_map.flush()
        assert _map_file(tmp_path)["dashboard:durable"]["sid"] == "sid-durable"
        await _settle(session_map)  # the scheduled task finds a clean map and retires
        assert session_map._flush_task is None

    def test_stale_inflight_payload_cannot_regress_a_newer_write(self, session_map, tmp_path):
        """The ticket check: an older snapshot landing late is dropped."""
        newer, newer_seq = session_map._serialize()
        session_map._data["dashboard:late"] = {"sid": "sid-late"}
        older = json.dumps(session_map._data)
        session_map._data.pop("dashboard:late")
        session_map._write_payload(newer, newer_seq)
        # A slow in-flight flush delivering an OLDER ticket after the newer
        # write must be refused, not land last-writer-wins.
        session_map._write_payload(older, newer_seq - 1)
        assert "dashboard:late" not in _map_file(tmp_path)

    @pytest.mark.parametrize("mode", ["deferred", "flush", "aflush"])
    @pytest.mark.asyncio
    async def test_write_failure_restores_dirty_and_retries(self, session_map, tmp_path, mode):
        """Every flush path re-owes a failed snapshot; the next mutation lands both."""
        real = SessionMap._write_payload
        boom = {"armed": True}

        def flaky(self, payload, seq):
            if boom.pop("armed", False):
                raise OSError("disk full")
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", flaky):
            session_map.set("dashboard:one", "sid-one")
            if mode == "deferred":
                await _settle(session_map)
            else:
                task = session_map._flush_task
                assert task is not None
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with pytest.raises(OSError, match="disk full"):
                    if mode == "flush":
                        await asyncio.to_thread(session_map.flush)
                    else:
                        await session_map.aflush()
            assert session_map._dirty
            session_map.set("dashboard:two", "sid-two")
            await _settle(session_map)

        persisted = _map_file(tmp_path)
        assert persisted["dashboard:one"]["sid"] == "sid-one"
        assert persisted["dashboard:two"]["sid"] == "sid-two"

    @pytest.mark.asyncio
    async def test_cancellation_reowes_and_aflush_lands_it(self, session_map, tmp_path):
        """A cancelled flush task must not lose state — and must not write.

        Writing from a cancellation path would block the loop mid-teardown, so
        the contract is re-owe: the state stays pending and the shutdown path's
        awaited ``aflush`` lands it off-loop.
        """
        session_map.set("dashboard:shutdown", "sid-shutdown")
        task = session_map._flush_task
        assert task is not None
        # Park the task in its debounce sleep, then cancel mid-flight.
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session_map._dirty, "cancellation must re-owe the pending write"
        await session_map.aflush()
        assert _map_file(tmp_path)["dashboard:shutdown"]["sid"] == "sid-shutdown"

    @pytest.mark.asyncio
    async def test_cancel_before_first_step_reowes_and_aflush_lands_it(self, session_map, tmp_path):
        """A task cancelled before EVER running must not strand pending state.

        A cancelled-before-start coroutine body never executes, so nothing has
        consumed the dirty mark: the state is still owed, the next mutation
        would reschedule (the task is done), and the shutdown ``aflush`` lands
        it. This is the loop-shutdown shape: create_task, then teardown
        cancels everything before the debounce elapses.
        """
        session_map.set("dashboard:early", "sid-early")
        task = session_map._flush_task
        assert task is not None
        task.cancel()  # deliberately NO yield first — the body never runs
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session_map._dirty, "an unstarted task must leave the write owed"
        await session_map.aflush()
        assert _map_file(tmp_path)["dashboard:early"]["sid"] == "sid-early"

    @pytest.mark.asyncio
    async def test_aflush_covers_a_claimed_but_unwritten_snapshot(self, session_map, tmp_path):
        """aflush() must not report clean while a claimed snapshot is unwritten."""
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        real = SessionMap._write_payload

        def gated(self, payload, seq):
            calls.append(seq)
            if len(calls) == 1:
                entered.set()
                assert release.wait(timeout=10)
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", gated):
            session_map.set("dashboard:aclaimed", "sid-aclaimed")
            await _await_event(entered, "the flush task to claim the snapshot")
            release.set()  # aflush's own write queues behind this on _io_lock
            await session_map.aflush()
            assert _map_file(tmp_path)["dashboard:aclaimed"]["sid"] == "sid-aclaimed"
            await _settle(session_map)

    @pytest.mark.asyncio
    async def test_flush_covers_a_claimed_but_unwritten_snapshot(self, session_map, tmp_path):
        """flush() during an in-flight claimed snapshot must still write.

        The flush task consumes the dirty mark when it CLAIMS a snapshot, so a
        dirty-only predicate would let flush() return while the worker thread
        has not landed anything — a stale file behind a "deterministic
        durability point". The file must be current before flush() returns,
        with the gated task write still unreleased.
        """
        entered = threading.Event()
        release = threading.Event()
        calls: list[int] = []
        real = SessionMap._write_payload

        def gated(self, payload, seq):
            calls.append(seq)
            if len(calls) == 1:
                # Only the task's write blocks; flush()'s inline write passes.
                entered.set()
                assert release.wait(timeout=10)
            return real(self, payload, seq)

        with patch.object(SessionMap, "_write_payload", gated):
            session_map.set("dashboard:claimed", "sid-claimed")
            await _await_event(entered, "the flush task to claim the snapshot")
            # Dirty is consumed, nothing is on disk yet. flush() must not no-op.
            session_map.flush()
            assert (
                _map_file(tmp_path)["dashboard:claimed"]["sid"] == "sid-claimed"
            ), "flush() returned while the claimed snapshot was still unwritten"
            release.set()
            await _settle(session_map)
        # The task's older ticket must not have regressed the forced write.
        assert _map_file(tmp_path)["dashboard:claimed"]["sid"] == "sid-claimed"

    @pytest.mark.asyncio
    async def test_batched_save_still_writes_once_on_exit(self, session_map, tmp_path):
        """A batch on the loop stays one inline write; no redundant flush after."""
        renames: list[int] = []
        import kiro_crew.session_map as mod

        # Process-global patch, same caveat as above.
        real_replace = mod.os.replace

        def counting_replace(src, dst):
            renames.append(1)
            return real_replace(src, dst)

        with patch.object(mod.os, "replace", counting_replace):
            with session_map.batched_save():
                session_map.set("dashboard:a", "sid-a")
                session_map.set("dashboard:b", "sid-b")
            batch_renames = len(renames)
            await _settle(session_map)
        assert batch_renames == 1
        assert len(renames) == 1, "a flush task rewrote state the batch exit already landed"
        persisted = _map_file(tmp_path)
        assert persisted["dashboard:a"]["sid"] == "sid-a"
        assert persisted["dashboard:b"]["sid"] == "sid-b"
