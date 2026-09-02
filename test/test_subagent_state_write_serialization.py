"""``update_state`` serializes its read/merge/rewrite for OFF-LOOP writers.

``update_state`` is a read / merge / rewrite whose two halves are separated by a
blocking ``_atomic_write`` (fsync + rename). A run writes ``state.json`` from
BOTH the event loop (PID, session id, provider, retention ``keep``) and the
thread pool (model provenance, CC-path model refinement, per-turn diagnostics --
all via ``asyncio.to_thread``). Without serialization the second writer's read
predates the first writer's write, so its rewrite restores a stale WHOLE-FILE
snapshot and silently rolls back every field the first writer had just landed.

The lock is taken by OFF-LOOP callers only: making an on-loop caller wait on a
pool thread's fsync would be a blocking call on the event loop. So these tests
pin two things -- that two pool writers do serialize, and that an on-loop caller
does NOT wait on the lock (the on-loop interleave stays open, tracked separately
as the loop-side offload).
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from kiro_crew import subagent_persistence as sp

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: How long a parked writer waits for the other writer to finish. Unserialized,
#: the other writer lands immediately and the wait returns early, which is what
#: produces the clobber. Serialized, the other writer is blocked on the lock, so
#: this wait is expected to time out -- that timeout is the fix working, and it
#: bounds the test instead of deadlocking it.
_PARK_TIMEOUT = 1.0


def _new_agent(agent_id: str) -> None:
    sp.create_agent_folder(
        agent_id,
        task="t",
        agent="kirocrew",
        parent_session="dashboard:default",
        max_turns=10,
    )


@pytest.fixture()
def agent_root(tmp_path, monkeypatch):
    """Point persistence at a temp directory."""
    monkeypatch.setattr("kiro_crew.subagent_persistence._SUBAGENTS_DIR", tmp_path)
    return tmp_path


def _park_first_writer(monkeypatch, released: threading.Event, inside: threading.Event):
    """Patch ``_atomic_write`` so the FIRST writer parks before it writes.

    Only the first call parks; every later writer goes straight through, so the
    second writer in each test is never itself delayed.
    """
    real_atomic_write = sp._atomic_write
    seen: list[str] = []
    guard = threading.Lock()

    def instrumented(path, data):
        with guard:
            first = not seen
            if first:
                seen.append("parked")
        if first:
            inside.set()
            released.wait(timeout=_PARK_TIMEOUT)
        real_atomic_write(path, data)

    monkeypatch.setattr(sp, "_atomic_write", instrumented)


class TestPoolWriterAgainstPoolWriter:
    """Two off-loop writers on one agent: neither may lose the other's fields.

    A run has three pool writers -- pre-spawn provenance, CC-path model
    refinement, and the per-turn diagnostics write -- and the last two overlap
    in time during a run.
    """

    def test_provenance_write_survives_a_concurrent_pool_write(self, agent_root, monkeypatch):
        _new_agent("a1")
        released = threading.Event()
        inside = threading.Event()
        _park_first_writer(monkeypatch, released, inside)

        # Writer A: the pre-spawn provenance write, parked mid transaction.
        pool_a = threading.Thread(
            target=lambda: sp.update_state(
                "a1", requested_model="req-model", resolved_model="res-model"
            )
        )
        pool_a.start()
        assert inside.wait(timeout=5.0), "writer A never reached its write"

        # Writer B: the per-turn diagnostics write, also off-loop.
        pool_b = threading.Thread(
            target=lambda: sp.update_state("a1", turns=7, last_tool="fs_read")
        )
        pool_b.start()
        pool_b.join(timeout=10.0)
        assert not pool_b.is_alive()

        released.set()
        pool_a.join(timeout=10.0)
        assert not pool_a.is_alive()

        state = sp.read_state("a1")
        assert state is not None
        assert state["requested_model"] == "req-model"
        assert state["resolved_model"] == "res-model"
        # Writer B's fields: rolled back by A's stale whole-file rewrite when the
        # two are not serialized.
        assert state["turns"] == 7
        assert state["last_tool"] == "fs_read"

    def test_a_detached_worker_cannot_roll_back_another_pool_write(self, agent_root, monkeypatch):
        """Cancelling a ``to_thread`` await detaches the worker, it does not stop it.

        A detached worker keeps an already-stale read; the lock stops its rewrite
        restoring the snapshot around it.
        """
        _new_agent("a2")
        released = threading.Event()
        inside = threading.Event()
        _park_first_writer(monkeypatch, released, inside)

        detached = threading.Thread(
            target=lambda: sp.update_state("a2", resolved_model="stale-worker-model")
        )
        detached.start()
        assert inside.wait(timeout=5.0), "detached worker never reached its write"

        recovery = threading.Thread(
            target=lambda: sp.update_state("a2", turns=3, last_tool="recovered")
        )
        recovery.start()
        recovery.join(timeout=10.0)
        assert not recovery.is_alive()

        released.set()
        detached.join(timeout=10.0)
        assert not detached.is_alive()

        state = sp.read_state("a2")
        assert state is not None
        assert state["resolved_model"] == "stale-worker-model"
        assert state["turns"] == 3
        assert state["last_tool"] == "recovered"


class TestOnLoopCallerDoesNotWait:
    """The split: an on-loop caller must not block on the per-agent lock.

    Serializing it too would mean the event loop waiting on a pool thread's
    fsync, which the repo's no-blocking-call-on-event-loop anchor forbids. The
    cost is that an interleave with one on-loop participant stays open; that is
    the loop-side offload, tracked separately.
    """

    def test_a_coroutine_does_not_wait_on_a_held_lock(self, agent_root):
        _new_agent("a3")
        held = sp._lock_for_agent("a3")
        # The holder must be ANOTHER thread. Holding it here would make the
        # coroutine re-acquire a non-reentrant lock on its own thread, so a
        # regression would DEADLOCK instead of failing -- a useless signal. With
        # a separate holder that releases on a timer, a regression instead shows
        # up as elapsed time and the test always terminates.
        holder_release = threading.Event()
        holding = threading.Event()

        def holder() -> None:
            held.lock.acquire()
            holding.set()
            holder_release.wait(timeout=10.0)
            held.lock.release()

        keeper = threading.Thread(target=holder, daemon=True)
        keeper.start()
        assert holding.wait(timeout=5.0), "holder never took the lock"

        # Release the lock well after the assertion window: if the on-loop
        # caller wrongly waits, it waits this long and fails on elapsed.
        timer = threading.Timer(1.5, holder_release.set)
        timer.daemon = True
        timer.start()
        try:

            async def on_loop_write() -> tuple[bool, float]:
                begin = time.monotonic()
                ok = sp.update_state("a3", keep=True)
                return ok, time.monotonic() - begin

            wrote, elapsed = asyncio.run(on_loop_write())
        finally:
            timer.cancel()
            holder_release.set()
            keeper.join(timeout=10.0)

        # Did not queue behind the holder.
        assert elapsed < 0.5, f"on-loop caller waited {elapsed:.2f}s on the lock"
        assert wrote is True
        state = sp.read_state("a3")
        assert state is not None
        assert state["keep"] is True

    def test_an_off_loop_caller_does_wait(self, agent_root):
        """The mirror: the same call from a plain thread DOES serialize.

        Pins that the seam is the running loop and not something incidental, so
        a change making every caller skip the lock cannot pass.
        """
        _new_agent("a3b")
        held = sp._lock_for_agent("a3b")
        held.lock.acquire()
        entered = threading.Event()

        def off_loop_write() -> None:
            entered.set()
            sp.update_state("a3b", keep=True)

        worker = threading.Thread(target=off_loop_write)
        worker.start()
        assert entered.wait(timeout=5.0)
        # Still blocked while we hold the lock.
        worker.join(timeout=0.5)
        assert worker.is_alive(), "off-loop caller did not wait on the lock"

        held.lock.release()
        worker.join(timeout=10.0)
        assert not worker.is_alive()
        state = sp.read_state("a3b")
        assert state is not None
        assert state["keep"] is True


class TestConcurrentWriters:
    def test_no_off_loop_writer_loses_its_field(self, agent_root, monkeypatch):
        """Every concurrent pool writer's field survives the merge.

        All writers are released from a barrier so their reads coincide, which
        is the interleaving the production writers hit by accident.
        """
        _new_agent("a4")
        writers = 8
        real_atomic_write = sp._atomic_write

        def slow(path, data):
            # Widen the read -> write window every writer already has.
            threading.Event().wait(timeout=0.005)
            real_atomic_write(path, data)

        monkeypatch.setattr(sp, "_atomic_write", slow)
        barrier = threading.Barrier(writers)

        def writer(n: int) -> None:
            barrier.wait(timeout=10.0)
            sp.update_state("a4", **{f"field_{n}": n})

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15.0)
            assert not t.is_alive()

        state = sp.read_state("a4")
        assert state is not None
        missing = [f"field_{n}" for n in range(writers) if f"field_{n}" not in state]
        assert not missing, f"writers lost their fields: {missing}"


class TestLockRegistry:
    def test_lock_is_per_agent_and_stable_while_referenced(self, agent_root):
        """One lock per agent id -- not one global lock for every run.

        A single process-wide lock would also pass the races above while making
        every unrelated run queue behind one agent's fsync.
        """
        from kiro_crew.subagent_persistence import _lock_for_agent

        a = _lock_for_agent("agent-a")
        assert _lock_for_agent("agent-a") is a
        assert _lock_for_agent("agent-b") is not a

    def test_entry_disappears_once_no_writer_holds_it(self, agent_root):
        """The registry is self-cleaning: no explicit eviction anywhere.

        Agent ids are per-run uuids, so an entry that outlived its writers would
        leak one lock per subagent ever spawned.
        """
        _new_agent("selfclean1")
        sp.update_state("selfclean1", pid=1)
        # The writer has returned, so nothing references the holder any more.
        assert "selfclean1" not in sp._STATE_LOCKS

        # Same after the folder is deleted, and for an id with no folder at all
        # -- neither needs a hook, because neither leaves a reference behind.
        sp.delete_agent_folder("selfclean1")
        assert sp.update_state("selfclean1", turns=9) is False
        assert "selfclean1" not in sp._STATE_LOCKS
        assert sp.update_state("never-existed", turns=1) is False
        assert "never-existed" not in sp._STATE_LOCKS

    def test_entry_survives_while_a_writer_still_references_it(self, agent_root):
        """The property that makes explicit eviction unnecessary AND unsafe.

        While any writer holds or is queued on the lock, the entry must stay --
        otherwise the next caller mints a FRESH lock and enters state.json
        alongside the writer still inside it. Dropping an entry explicitly (a
        folder delete, the tombstone pruner) is exactly that bug, which is why
        the registry holds its values weakly instead.
        """
        _new_agent("held1")
        holder = sp._lock_for_agent("held1")
        holder.lock.acquire()
        entered = threading.Event()
        try:

            def queued_writer() -> None:
                entered.set()
                sp.update_state("held1", turns=2)

            worker = threading.Thread(target=queued_writer, daemon=True)
            worker.start()
            assert entered.wait(timeout=5.0)
            worker.join(timeout=0.5)
            assert worker.is_alive(), "writer should be queued on the held lock"

            # Referenced by us and by the queued writer -> same object, so the
            # writer cannot be stranded on an orphaned lock.
            assert "held1" in sp._STATE_LOCKS
            assert sp._lock_for_agent("held1") is holder

            # Even a folder delete mid-flight must not orphan it.
            sp.delete_agent_folder("held1")
            assert sp._lock_for_agent("held1") is holder
        finally:
            holder.lock.release()
            worker.join(timeout=10.0)
        assert not worker.is_alive()


def _park_first_writer_late(monkeypatch, inside: threading.Event, delay: float) -> None:
    """Patch ``_atomic_write`` so the FIRST writer announces itself, then lands
    *delay* seconds later.

    The announcement marks the point where the writer's READ has already
    happened, so anything written after it is what a stale rewrite would roll
    back. The delay is what puts the writer's WRITE after the on-loop write
    under test -- unserialized and undrained, that ordering is the clobber.
    """
    real_atomic_write = sp._atomic_write
    seen: list[str] = []
    guard = threading.Lock()

    def instrumented(path, data):
        with guard:
            first = not seen
            if first:
                seen.append("parked")
        if first:
            inside.set()
            time.sleep(delay)
        real_atomic_write(path, data)

    monkeypatch.setattr(sp, "_atomic_write", instrumented)


def _mock_sessions_for_run(served_model: str):
    """Minimal SessionManager double: enough to drive ``_run_inner`` to its
    pre-spawn provenance write and no further."""
    from unittest.mock import AsyncMock, MagicMock

    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.context_used_tokens = lambda: 0
    provider.context_window_tokens = lambda: 0
    provider.client = None
    provider.served_model = served_model

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable -- makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    return sessions


def _mock_ctx_builder_for_run():
    from unittest.mock import MagicMock

    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


class TestOnLoopKeepWriteAgainstACancelledRunsWorker:
    """#6298: the on-loop retention ``keep`` write must not be rolled back.

    ``_promote_conversation`` / ``release_conversation`` write ``keep`` from the
    event loop, where ``update_state`` deliberately takes no lock -- so a
    concurrent pool writer's stale whole-file rewrite erases it. Both are reached
    only through the ``_conversation_busy`` gate, which refuses while a run is in
    flight, so the one writer that can still be concurrent is a DETACHED worker:
    one whose ``to_thread`` await was cancelled while the write was in flight.
    Draining every off-loop writer on cancellation (#6308) removes that
    population, which closes this interleave too -- a pool writer can no longer
    outlive the run it belongs to.
    """

    @pytest.mark.asyncio
    async def test_keep_survives_a_cancelled_runs_provenance_worker(self, agent_root, monkeypatch):
        from unittest.mock import patch

        from kiro_crew.subagent import SubagentInfo, SubagentManager

        conv_id = "keep01"
        _new_agent(conv_id)
        inside = threading.Event()
        # Long enough that an UNDRAINED cancellation reliably reaches the on-loop
        # keep write first, short enough to stay well inside the 5s drain bound.
        _park_first_writer_late(monkeypatch, inside, delay=0.4)

        manager = SubagentManager(
            sessions=_mock_sessions_for_run("model-served"),
            ctx_builder=_mock_ctx_builder_for_run(),
            is_yolo=lambda: True,
        )
        info = SubagentInfo(id=conv_id, task="keep vs zombie", model="model-req")
        manager._agents[info.id] = info

        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            task = asyncio.ensure_future(manager._run_inner(info, f"subagent:{conv_id}"))
            assert await asyncio.to_thread(inside.wait, 5.0), "provenance write never started"
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # The run's cancellation has settled. This is where a release or a
            # continuation promotes retention on the loop; nothing may still be
            # holding a pre-promote snapshot of state.json.
            manager._agents.pop(info.id, None)
            assert manager._conversation_busy(f"subagent:{conv_id}") is None
            manager._promote_conversation(conv_id, f"subagent:{conv_id}")

        # Give any worker that was NOT drained time to land its stale rewrite.
        await asyncio.sleep(0.6)
        state = sp.read_state(conv_id)
        assert state is not None
        assert state.get("keep") is True, (
            "the on-loop keep write was rolled back by a detached worker's stale "
            "whole-file rewrite -- retention is lost, so the tombstone pruner "
            "deletes session files the conversation needs (#6298)"
        )
        # The worker's own field still landed: the drain waits for it, it is not
        # discarded.
        assert state.get("requested_model") == "model-req"
