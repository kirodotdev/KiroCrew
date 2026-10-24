"""The task store never blocks the event loop.

Every store read and write runs on the writer thread: ``TaskStore.run`` and the strict
guard that refuses a direct call on the loop, the spawn and drain paths, the attached
children guard, the pump's resume grant, the lanes endpoint, the wave sweeps and the first
dependency coordinator, each pinned by ``loop_thread_calls`` as well as by the armed guard,
because a caller inside ``except Exception`` swallows the guard's raise. A closed admission
neither refills the window nor grants a resume, and both land once it reopens. The shared
fixtures live in ``overload_integration_glue_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from overload_fakes import settle_store_writes
from overload_integration_glue_helpers import (  # noqa: F401 -- fixtures are requested by name
    clock_fixture,
    store_fixture,
)

from kiro_crew.on_loop_db import OnLoopStoreError
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.subagent_manager import admission as admission_mod
from kiro_crew.taskq import dependency as dep_mod
from kiro_crew.taskq import model
from kiro_crew.taskq import store as store_mod
from kiro_crew.taskq.store import TaskStore
from kiro_crew.taskq.waits import WaitRecord
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── store off-loop: the store never blocks the event loop ────────────────────


class TestStoreOffLoop:
    @pytest.mark.asyncio
    async def test_run_executes_on_the_writer_thread(self, store: TaskStore) -> None:
        import threading

        seen: dict[str, Any] = {}

        def _probe() -> int:
            seen["thread"] = threading.current_thread().name
            seen["loop_running"] = TaskStore._on_running_loop_thread()
            return store.count_pending(model.KIND_SUBAGENT)

        assert await store.run(_probe) == 0
        assert seen["thread"].startswith("taskq-writer")
        assert seen["loop_running"] is False
        assert store.loop_thread_calls == 0

    @pytest.mark.asyncio
    async def test_run_does_not_take_the_connection_lock_on_the_loop(
        self, store: TaskStore
    ) -> None:
        """``run`` reaches its executor without ``_lock``, so the seam every
        loop caller is told to use cannot itself freeze the loop.

        ``_lock`` is held by the writer thread across ``BEGIN IMMEDIATE``'s busy
        wait -- exactly the wait ``run`` exists to move off the loop -- so a
        ``run`` that took it to fetch its executor handed the loop that wait
        anyway, and invisibly: the take is not a :meth:`TaskStore._c`, so
        ``loop_thread_calls`` shows nothing.

        Pinned by WHO released the holder, not by a wall clock: the holder is
        released by a coroutine scheduled before the ``run``, so a loop that
        kept control releases it at once and a frozen loop leaves it to time
        out on its own. The timeout is the failure's cost, never the pin.
        """
        import threading

        holding = threading.Event()
        release = threading.Event()
        released_by_the_loop: list[bool] = []

        def _hold() -> None:
            with store._lock:
                holding.set()
                released_by_the_loop.append(release.wait(3.0))

        holder = threading.Thread(target=_hold, name="lock-holder", daemon=True)
        holder.start()
        try:
            await asyncio.get_running_loop().run_in_executor(None, holding.wait, 5.0)

            async def _unblock() -> None:
                # flake-ok: one loop turn so store.run parks before the release; the holder's own 3 s wait bounds a regression; pre-existing, moved verbatim
                await asyncio.sleep(0)
                release.set()

            asyncio.get_running_loop().create_task(_unblock())
            await store.run(lambda: None)
        finally:
            release.set()
            holder.join(5.0)
        assert released_by_the_loop == [True], "the loop was frozen behind the connection lock"

    @pytest.mark.asyncio
    async def test_strict_guard_refuses_a_direct_call_on_the_loop(
        self, store: TaskStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Negative control for the guard the async paths are pinned against."""
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        with pytest.raises(OnLoopStoreError, match="taken on the event loop"):
            store.count_pending(model.KIND_SUBAGENT)
        # The same call through run() is fine.
        assert await store.run(store.count_pending, model.KIND_SUBAGENT) == 0

    @pytest.mark.asyncio
    async def test_spawn_async_writes_the_row_off_loop_before_starting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``/api/spawn`` -> ``spawn_async``: the accept (``BEGIN IMMEDIATE``) runs
        on the writer thread, the row exists BEFORE the run starts, and the
        started run carries the id the row was accepted under."""
        import threading
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")
        sessions.has_session = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        await mgr.wait_taskq_ready()
        mgr._should_use_session_sharing = MagicMock(return_value=False)
        mgr._spawn_stagger_secs = 0.0
        real_store = mgr._admission.taskq_store()
        assert real_store is not None

        accepts: list[dict[str, Any]] = []
        original_accept = real_store.accept_one

        def _spy(record: Any) -> str:
            accepts.append(
                {
                    "id": record.id,
                    "thread": threading.current_thread().name,
                    "loop_running": TaskStore._on_running_loop_thread(),
                    "started_before_accept": record.id in mgr._agents,
                }
            )
            return original_accept(record)

        monkeypatch.setattr(real_store, "accept_one", _spy)
        # End to end through the ``/api/spawn`` glue, with the strict guard
        # ARMED: the accept, the window decision, the claim and the
        # registration writes all run on the writer thread; a store call on
        # the loop raises here instead of passing silently.
        from kiro_crew.dashboard.handlers import messaging

        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        before = real_store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        state = SimpleNamespace(subagents=mgr)
        info = await messaging._spawn_on_loop(state, "hello", parent_session_key="web-1", agent="")
        await async_wait_until(
            lambda: asyncio.to_thread(lambda: real_store.get(info.id) is not None),
            describe=lambda: "registration write not yet landed",
        )
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        # (The fake session makes the run itself fail right after it starts;
        # what this pins is the accept path, not the run.)
        assert info is not None and info.id and not info.queued
        assert accepts and accepts[0]["id"] == info.id
        assert accepts[0]["thread"].startswith("taskq-writer")
        assert accepts[0]["loop_running"] is False
        assert accepts[0]["started_before_accept"] is False
        assert real_store.loop_thread_calls == before  # the test's own reads come after
        assert real_store.get(info.id) is not None
        await mgr.cancel_all()

    @staticmethod
    async def _manager_with_store(monkeypatch: pytest.MonkeyPatch) -> Any:
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")
        sessions.has_session = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        await mgr.wait_taskq_ready()
        mgr._should_use_session_sharing = MagicMock(return_value=False)
        mgr._spawn_stagger_secs = 0.0
        assert mgr._admission.taskq_store() is not None
        return mgr

    @pytest.mark.asyncio
    async def test_drain_refill_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pump on a running loop: every store read of the window refill and
        the wait-expiry sweep happens on the writer thread. Pinned with the
        strict guard armed, so a read that slipped back onto the loop raises."""
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        # Two rows waiting on disk: the pump refills the window
        # (pending_lanes / fetch_dispatchable_fair) off-loop, then picks.
        for i in range(2):
            rec = mgr._admission.taskq_build_record(
                f"row{i}",
                {"task": f"t{i}", "parent_session_key": "web-1"},
                parent_session_key="web-1",
                memory_store="",
                app="",
                model="",
                allowed_tools=None,
                approval_mode=None,
            )
            assert mgr._admission.taskq_accept_record(rec) is None
        # Nothing is stubbed: the pump's own reads (expiry sweep, pending_lanes,
        # fetch), the dispatch (``store.claim`` via ``ClaimPoint``), the
        # registration writes (``taskq_mark``), the run's terminal write
        # (``taskq_settle``) and the queue-depth chip's ``count_pending`` all
        # run under the strict guard, which raises on any loop-thread call.
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        before = store.loop_thread_calls
        mgr._drain_queue()
        task = getattr(mgr, "_drain_task", None)
        assert task is not None, "on a running loop with a store the pump is a coroutine"
        # flake-ok: the pump coroutine ends after its pass over the seeded rows, inside the suite timeout; pre-existing, moved verbatim
        await task

        # Wait for the point, not toward it: the started runs settle through the
        # writer thread, and how long that takes is the host's business -- two
        # fixed 200 ms sleeps here read ``starting`` for both rows on a loaded
        # Windows worker (one round of five in a full-suite sweep). The reads
        # are taken OFF the loop because the strict guard is still armed.
        async def _states() -> dict[str, str]:
            return {rid: await asyncio.to_thread(store.state_of, rid) for rid in ("row0", "row1")}

        async def _any_terminal() -> bool:
            states = await _states()
            return any(st in model.TERMINAL for st in states.values())

        await async_wait_until(_any_terminal, describe=lambda: "no terminal row yet")
        await mgr._drain_queue_async()
        await async_wait_until(
            lambda: asyncio.to_thread(
                lambda: any(store.state_of(rid) in model.TERMINAL for rid in ("row0", "row1"))
            ),
            describe=lambda: "rows not yet terminal",
        )
        assert store.loop_thread_calls == before
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        states = {rid: store.state_of(rid) for rid in ("row0", "row1")}
        # Both rows were claimed and started (the fake session makes the run
        # fail, which is itself a settle through the writer thread).
        assert all(st not in (model.QUEUED, model.ADMITTED) for st in states.values()), states
        assert any(st in model.TERMINAL for st in states.values()), states
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_pick_resolves_a_lane_off_loop_not_on_the_parent_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The drain's LANE question is answered off the loop.

        A window entry that carries no ``_lane`` resolves its parent chain, and a
        parent with no live run is read from the store -- which the pick, a
        coroutine, would otherwise do on the loop. The entry here is the shape
        that forces it: a nested key whose parent exists only as a row.

        Counted, not raised: the pump body is wrapped in ``except Exception``,
        so the strict guard's raise would be swallowed and the pass would look
        green over the freeze.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        parent = mgr._admission.taskq_build_record(
            "ghostparent",
            {"task": "a parent this process never ran", "parent_session_key": "web-7"},
            parent_session_key="web-7",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(parent) is None
        mgr._agents.pop("ghostparent", None)
        # A nested window entry with NO ``_lane``: the pick has to resolve
        # ``subagent:ghostparent`` to its root's lane, and the only place that
        # answer lives is the parent's row.
        mgr._queue.clear()
        mgr._queue.append(
            {"task": "nested, lane unresolved", "parent_session_key": "subagent:ghostparent"}
        )
        before = store.loop_thread_calls
        resolved = await mgr._admission.resolve_window_lanes_async()
        assert resolved == {"subagent:ghostparent": "web-7"}, resolved
        await mgr._drain_queue_async()
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @staticmethod
    def _low_memory(*_a: Any, **_k: Any) -> tuple[bool, float]:
        """The floor's reading on a host with 1.2 GB free: below any bar."""
        return (False, 1.2)

    @pytest.mark.asyncio
    async def test_the_drained_defer_writes_off_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A drained row the pressure gate parks: the ``store.defer`` runs on the
        writer thread, and its BOOLEAN still decides the answer.

        Counted, not raised: this write sits inside the pump's
        ``except Exception``, which swallows the armed guard's ``OnLoopStoreError``
        and would report a green pass over a two-second freeze.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "held0",
            {"task": "parked under pressure", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        monkeypatch.setattr("kiro_crew.subagent.check_memory_available", self._low_memory)
        before = store.loop_thread_calls
        await mgr._drain_queue_async()
        await settle_store_writes(store)
        # The row is parked, not refused: the defer landed and moved the row's
        # eligibility out, which is what the boolean reported. Read through
        # ``run``, so these checks leave the counter below where the pump left it.
        row = await store.run(store.get, "held0")
        assert row is not None and row.state == model.QUEUED, row
        assert row.next_run_at > store.now(), row.next_run_at
        kinds = [e.kind for e in await store.run(store.events, "held0")]
        assert kinds.count("deferred") == 1, kinds
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_drained_defer_refuses_a_legacy_window_entry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A window entry with no row behind it is REFUSED, never parked.

        ``_from_queue`` does not prove a row exists, so the defer's boolean is
        what separates the two; a parked handle for a row nothing will ever
        dispatch is a spawn the requester never hears about again. Off-loop for
        this answer too -- a missing row still takes ``BEGIN IMMEDIATE``.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        announced: list[Any] = []

        async def _collect(info: Any) -> None:
            announced.append(info)

        mgr._on_done = _collect
        mgr._queue = [
            {
                "task": "no row behind it",
                "parent_session_key": "web-1",
                "_preassigned_id": "ghost0",
            }
        ]
        monkeypatch.setattr("kiro_crew.subagent.check_memory_available", self._low_memory)
        before = store.loop_thread_calls
        await mgr._drain_queue_async()
        await settle_store_writes(store)
        for _ in range(5):
            # flake-ok: five bounded loop turns for the announce callbacks after settle_store_writes drained the writes; pre-existing, moved verbatim
            await asyncio.sleep(0)
        assert await store.run(store.get, "ghost0") is None
        assert [i.id for i in announced] == ["ghost0"], announced
        assert announced[0].done is True and not announced[0].queued
        # The store's verdict, not a capacity one: it says so and carries the
        # store's retry code, with the memory figures only as context.
        error = announced[0].error or ""
        assert error.startswith("spawn refused: the task store could not record"), error
        assert "1.2 GB available" in error, error
        assert announced[0].error_code == admission_mod.TASK_STORE_UNAVAILABLE_CODE
        # Last, and through ``run`` above, so the only thing this can catch is
        # the defer sliding back onto the loop.
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_continuation_and_app_spawn_accept_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The two remaining accept paths -- ``continue_conversation_async`` and
        the app ``SpawnSDK`` -- write their row through ``spawn_async``."""
        import threading

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        threads: list[str] = []
        original_accept = store.accept_one

        def _spy(record: Any) -> str:
            threads.append(threading.current_thread().name)
            return original_accept(record)

        monkeypatch.setattr(store, "accept_one", _spy)
        # continuation: the prelude's own checks pass for an unknown conversation
        # only through the manager's recorded state; stub it to the spawn step.
        monkeypatch.setattr(
            mgr,
            "_continue_prelude",
            lambda *a, **k: {"task": "again", "parent_session_key": "web-1"},
        )
        info = await mgr.continue_conversation_async("conv-1", "again", parent_session_key="web-1")
        assert info is not None and not info.error, info
        # app SpawnSDK
        from kiro_crew.apps import spawn_sdk

        class _Agent:
            name = "demo--worker"
            filename = "demo--worker.json"

        monkeypatch.setattr(spawn_sdk, "list_agents", lambda: [_Agent()])
        impl = spawn_sdk.build_spawn_impl(mgr)
        agent_id = await impl("do it", "demo--worker", True, "", "demo")
        assert agent_id
        assert threads and all(t.startswith("taskq-writer") for t in threads), threads
        assert len(threads) == 2
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_channel_keyword_spawn_door_accepts_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FOURTH accept path: ``spawn <task>`` typed into a channel.

        Slack's ``_handle_spawn_command`` and Telegram's ``spawn_task_reply``
        both run inside an async channel handler, so the ``BEGIN IMMEDIATE`` the
        accept takes must land on the writer thread. Nothing is stubbed and the
        strict guard is armed, so a spawn that fell back to the synchronous
        ``spawn`` raises here.
        """
        import threading

        from kiro_crew.messaging import commands as messaging_commands
        from kiro_crew.slack import handler as slack_handler

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        threads: list[str] = []
        original_accept = store.accept_one

        def _spy(record: Any) -> str:
            threads.append(threading.current_thread().name)
            return original_accept(record)

        monkeypatch.setattr(store, "accept_one", _spy)
        before = store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            slack_reply = await slack_handler._handle_spawn_command(
                "spawn index the corpus", mgr, "slack:C1:1"
            )
            telegram_reply = await messaging_commands.spawn_task_reply(
                "index it again", mgr, "telegram:kirocrew:direct:7"
            )
            await async_wait_until(
                lambda: asyncio.to_thread(lambda: len(threads) >= 2),
                describe=lambda: f"spawn registration writes not yet landed: {len(threads)}",
            )
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

        assert slack_reply and "Spawned subagent" in slack_reply
        assert telegram_reply and "Spawned subagent" in telegram_reply
        assert len(threads) == 2 and all(t.startswith("taskq-writer") for t in threads), threads
        assert store.loop_thread_calls == before  # the reads below are the test's own
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_queue_depth_and_parent_cancel_read_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The queue-depth reads made from coroutines: the cron reset-deferral
        guards (``queued_count_for`` / ``has_pending_work_for``), the chat
        slot-mode gate, and ``cancel_for_parent``'s pending-id read. Each one
        counts rows that live only on disk, so each has to ask the store, and
        under the armed guard it has to ask it off the loop.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        for i in range(2):
            rec = mgr._admission.taskq_build_record(
                f"depth{i}",
                {"task": f"t{i}", "parent_session_key": "cron:j1:planner"},
                parent_session_key="cron:j1:planner",
                memory_store="",
                app="",
                model="",
                allowed_tools=None,
                approval_mode=None,
            )
            assert mgr._admission.taskq_accept_record(rec) is None

        before = store.loop_thread_calls
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            assert await mgr.queued_count_for_async("cron:j1:planner") == 2
            assert await mgr.has_pending_work_for_async("cron:j1:planner") is True
            assert await mgr.has_pending_work_for_async("cron:j1:idle") is False
            pending = await mgr._admission.taskq_pending_ids_for_async("cron:j1:planner")
            assert sorted(pending) == ["depth0", "depth1"]
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
        assert store.loop_thread_calls == before

        # ``cancel_for_parent`` reads the same ids through that same seam and
        # then stops each row. Its remaining on-loop take is ``_unqueue``'s
        # ``taskq_cancel_queued``, which is NOT a drop-in offload: the row has to
        # be cancelled before a drain can claim it AND out of the window before
        # a stagger timer can start it, and an await between those two is a race
        # in either order. Tracked in ``taskq.store``'s backlog note, so this
        # call runs unarmed -- with the SYNC read wrapped, so a regression that
        # takes it there again is caught rather than merely warned about.
        sync_reads: list[str] = []
        bridge = type(mgr._admission)
        original_sync = bridge.taskq_pending_ids_for
        monkeypatch.setattr(
            bridge,
            "taskq_pending_ids_for",
            lambda self, key: sync_reads.append(key) or original_sync(self, key),
        )
        assert await mgr.cancel_for_parent("cron:j1:planner") == (0, 2)
        assert sync_reads == [], "the parent cancel must take the off-loop read"
        assert store.state_of("depth0") == model.CANCELLED
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_has_pending_store_children_sees_a_durable_only_waiter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deferral's store-side half, read off the loop against a real store.

        A durable-only spawn (accepted into the store, no in-memory run) is
        invisible to the live/queued sets ``has_live_or_queued_children`` reads
        in process, yet the identity-retire teardown cancels it. So the sweep asks
        the store too, through ``has_pending_store_children`` -- on the writer
        thread (``store.run``), BEFORE it takes the registry locks, so no store
        read runs on the event loop. ``STRICT_ON_LOOP_ENV`` is armed to prove the
        read goes off-loop.
        """
        mgr = await self._manager_with_store(monkeypatch)
        bridge = mgr._admission

        # No rows for this parent yet.
        assert await mgr.has_pending_store_children("cron:j1:planner") is False
        assert await mgr.has_pending_store_children("") is False

        rec = bridge.taskq_build_record(
            "durable0",
            {"task": "t0", "parent_session_key": "cron:j1:planner"},
            parent_session_key="cron:j1:planner",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert bridge.taskq_accept_record(rec) is None

        # The store-only waiter is now seen, and only for its own parent. The read
        # goes off the loop: ``STRICT_ON_LOOP_ENV`` is armed around it to prove so.
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        try:
            assert await mgr.has_pending_store_children("cron:j1:planner") is True
            assert await mgr.has_pending_store_children("cron:j1:idle") is False
        finally:
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

        # A failed read propagates rather than answering False: the sweep records
        # the parent as a defer, so an accepted durable child is never read as
        # absent and cancelled on an unreadable store.
        def _boom(*_a, **_k):
            raise RuntimeError("store read failed")

        monkeypatch.setattr(bridge.taskq_store(), "list_pending", _boom)
        with pytest.raises(RuntimeError):
            await mgr.has_pending_store_children("cron:j1:planner")
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_identity_sweep_fence_records_but_does_not_suppress_delivery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The identity sweep's tentative fence records admissions for the
        retire-vs-defer re-check, but does NOT suppress an expiring row's delivery.

        A committed teardown fence (``open_teardown_fence`` was once the same thing)
        makes ``accepted_before_open_teardown`` drop a row's delivery as "accepted
        for a conversation that ended". The identity sweep opens its fence BEFORE it
        decides retire-vs-defer, and may spare the parent -- so its fence must not
        suppress delivery. It is a separate, tentative fence: it records admissions
        (``has_fenced_children`` sees them) but ``accepted_before_open_teardown``
        ignores it. A committed teardown fence still suppresses.
        """
        mgr = await self._manager_with_store(monkeypatch)
        canc = mgr._cancellation
        key = "cron:j1:planner"

        # Tentative identity-sweep fence open: records admissions, no suppression.
        fence = mgr.open_teardown_fence(key)
        assert fence is not None
        canc.note_teardown_store_accept(key, "child-1")
        assert mgr.has_fenced_children(key) is True
        # A row NOT in the fence must still deliver -- the parent may be spared.
        assert canc.accepted_before_open_teardown(key, "other-row") is False
        mgr.release_teardown_fence(key, fence)
        assert mgr.has_fenced_children(key) is False

        # A COMMITTED teardown fence (the real cancel) DOES suppress a non-fenced row.
        canc.note_teardown_snapshot(key)
        assert canc.accepted_before_open_teardown(key, "other-row") is True
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_commit_identity_sweep_retire_is_atomic_against_admission(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final commit-vs-defer and the committed-fence open are atomic.

        A child admitted into the tentative fence after the sweep's childless read
        must defer, not be cancelled; and a commit must promote the tentative fence
        to the committed one so a concurrent admission already recorded is carried
        into the cancel's ``accepted_since``.
        """
        mgr = await self._manager_with_store(monkeypatch)
        canc = mgr._cancellation
        key = "cron:j1:planner"

        # A child arrived in the tentative fence -> commit refuses (defer).
        fence_a = mgr.open_teardown_fence(key)
        assert fence_a is not None
        canc.note_teardown_store_accept(key, "late-child")
        assert mgr.commit_identity_sweep_retire(key, fence_a) is False
        mgr.release_teardown_fence(key, fence_a)

        # Empty tentative fence -> commit promotes it to the committed fence, and a
        # row recorded concurrently rides in it (so the cancel spares it).
        fence_b = mgr.open_teardown_fence(key)
        assert fence_b is not None
        assert mgr.commit_identity_sweep_retire(key, fence_b) is True
        # It is now the COMMITTED fence: an admission recorded into it is in
        # accepted_since (not swept), i.e. accepted_before_open_teardown is False.
        canc.note_teardown_store_accept(key, "rode-in")
        assert canc.accepted_before_open_teardown(key, "rode-in") is False
        # The tentative fence is gone (promoted), so has_fenced_children is False.
        assert mgr.has_fenced_children(key) is False
        await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_attached_children_guard_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``chat_utils``' attached-children guard, the predicate every session
        teardown and dispatch route shares.

        Its queued half counts this parent's rows that live only on disk -- the
        term that makes a reset after a restart see children the registry cannot
        -- so it has to ask the store, and every caller of it (a reset consume,
        a 409 route, the RSS sweep, the completion frame) is a coroutine on the
        gateway loop.
        """
        from kiro_crew.dashboard import chat_utils

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        rec = mgr._admission.taskq_build_record(
            "ondisk0",
            {"task": "left by the previous incarnation", "parent_session_key": "dashboard:chat-1"},
            parent_session_key="dashboard:chat-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        # No live run and nothing in the window: the store row is the ONLY
        # evidence, so a guard that skipped the read would answer "no children".
        mgr._agents.pop("ondisk0", None)
        mgr._queue.clear()
        state = SimpleNamespace(subagents=mgr, _slots={})

        # Counted, NEVER armed: this guard wraps its queued probe in
        # ``except Exception``, so the strict guard's raise would come back as
        # ``queued = 1`` -- a fail-closed ANSWER for every session, which is
        # both a green ``pytest.raises`` and a wrong verdict. The counter is the
        # only signal that cannot be swallowed.
        before = store.loop_thread_calls
        attached = await chat_utils.subagents_attached_async(state, None, "dashboard:chat-1", "pin")
        elsewhere = await chat_utils.subagents_attached_async(
            state, None, "dashboard:chat-9", "pin"
        )
        assert attached is True, "the store-only row is a child the guard must see"
        assert elsewhere is False, "another session's row is not this session's child"
        assert store.loop_thread_calls == before
        await mgr.cancel_all()

    def test_no_coroutine_calls_the_synchronous_attached_children_guard(self) -> None:
        """The guard has a sync entry for sync callers; no ``async def`` may use it.

        A ratchet over the whole package, not a list of the call sites this pass
        converted: the sync entry reads the task store on whatever thread calls
        it, so ONE coroutine left on it puts the busy wait back on the loop, and
        the next such caller would be added without a second thought.

        ``_queued_depth`` is covered by the same walk, with exactly one
        coroutine allowed to name it: ``_queued_depth_off_loop``, which reaches
        it only for a manager double that has no async sibling and therefore no
        store to block on.
        """
        import ast

        from source_corpus import parsed_candidates

        allowed = {("chat_utils_completion.py", "_queued_depth_off_loop", "_queued_depth")}
        offenders: list[str] = []
        # A flagged call spells one of the two names as its Name or Attribute, so only
        # the files whose text holds one are parsed.
        for path, _text, tree in parsed_candidates(
            require_any=("subagents_attached", "_queued_depth"), skip_syntax_errors=False
        ):

            class _Visitor(ast.NodeVisitor):
                def __init__(self) -> None:
                    self.frames: list[tuple[str, bool]] = []

                def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                    self.frames.append((node.name, False))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                    self.frames.append((node.name, True))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_Lambda(self, node: ast.Lambda) -> None:
                    self.frames.append(("<lambda>", False))
                    self.generic_visit(node)
                    self.frames.pop()

                def visit_Call(self, node: ast.Call) -> None:
                    name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                    frame = self.frames[-1] if self.frames else ("<module>", False)
                    if (
                        name in ("subagents_attached", "_queued_depth")
                        and frame[1]
                        and (path.name, frame[0], name) not in allowed
                    ):
                        offenders.append(f"{path.name}:{node.lineno} {frame[0]}() calls {name}()")
                    self.generic_visit(node)

            _Visitor().visit(tree)
        assert offenders == [], offenders

    @pytest.mark.asyncio
    async def test_the_pump_grants_a_resume_off_loop_and_releases_a_refused_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The pump's resume seam: the lane slot is RESERVED on the loop (where
        the pump's own capacity re-check reads it), ``wake_wait`` runs on the
        writer thread, and the run state plus the resume signal are published
        only from its result.

        ``_drain_queue`` is deliberately NOT stubbed: a mock there is exactly
        what hides the write the wake triggers.
        """
        import threading

        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        threads: list[str] = []
        original_wake = store.wake_wait

        def _spy(*a: Any, **kw: Any) -> Any:
            threads.append(threading.current_thread().name)
            return original_wake(*a, **kw)

        monkeypatch.setattr(store, "wake_wait", _spy)

        async def _park(task_id: str) -> tuple[SubagentInfo, int]:
            rec = model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={"task": task_id})
            await store.run(store.accept_one, rec)
            claimed = await store.run(store.claim, task_id)
            gen = claimed.generation
            await store.run(store.transition, task_id, model.STARTING, generation=gen)
            await store.run(store.transition, task_id, model.RUNNING, generation=gen)
            record = WaitRecord.input("call-1", since=store.now())
            assert await store.run(store.enter_wait, task_id, record.to_dict(), generation=gen)
            info = SubagentInfo(id=task_id, task="t")
            info._taskq_generation = gen
            info._slot_released = True
            info._wait_record = record.to_dict()
            info._resume_event = asyncio.Event()
            mgr._agents[task_id] = info
            return info, gen

        try:
            granted, gen = await _park("resume-ok")
            before = store.loop_thread_calls
            assert mgr._admission.request_resume(granted, reason="answered") is True
            # flake-ok: the pump task ends after its pass over the parked runs, inside the suite timeout; pre-existing, moved verbatim
            await mgr._drain_task
            assert store.loop_thread_calls == before
            assert threads and all(t.startswith("taskq-writer") for t in threads), threads
            row = await store.run(store.get, granted.id)
            assert row.state == model.RUNNING
            assert granted._taskq_generation == gen + 1 == row.generation
            assert granted._slot_released is False and granted._resume_event is None

            # A refused wake gives the reservation back and leaves the run parked.
            refused, _gen = await _park("resume-refused")
            count_before = mgr._running_count
            monkeypatch.setattr(store, "wake_wait", lambda *a, **kw: None)
            assert mgr._admission.request_resume(refused, reason="answered") is True
            # flake-ok: the pump task ends after its pass over the parked runs, inside the suite timeout; pre-existing, moved verbatim
            await mgr._drain_task
            assert mgr._running_count == count_before
            assert refused._slot_released is True and refused._resume_pending is False
            assert not refused._resume_event.is_set()
            assert (await store.run(store.get, refused.id)).state == model.WAITING_INPUT
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_run_that_ends_inside_its_reservation_window_gives_the_slot_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A reservation is not a grant, so the publish re-tests the run.

        The pump reserves EVERY queued resume in one synchronous pass and only
        then grants them one at a time with an await between, so a run whose
        entry sits behind another's ends while its own slot is already reserved.
        ``resume_reserve``'s liveness gate ran on the near side of that await and
        ``yield_slot`` already spent the run's one-shot release token, so a
        publish onto a finished run charges a lane slot no terminal path can hand
        back -- the cap falls by one per such resume until the lane is dead.

        Two entries, and the ending is driven from inside the LEAD's ``wake_wait``
        so it lands in the real window rather than in a race between two clocks.
        The victim's wake still lands (the store is reached and its row is
        waiting), which is why the generation it produced has to be adopted even
        though the grant is refused: the run's own terminal write is fenced by
        the generation the ROW now carries, not by the stale one it went in with.
        """
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)

        async def _park(task_id: str) -> SubagentInfo:
            rec = model.TaskRecord(id=task_id, kind=model.KIND_SUBAGENT, params={"task": task_id})
            await store.run(store.accept_one, rec)
            claimed = await store.run(store.claim, task_id)
            gen = claimed.generation
            await store.run(store.transition, task_id, model.STARTING, generation=gen)
            await store.run(store.transition, task_id, model.RUNNING, generation=gen)
            record = WaitRecord.input("call-1", since=store.now())
            assert await store.run(store.enter_wait, task_id, record.to_dict(), generation=gen)
            info = SubagentInfo(id=task_id, task="t")
            info._taskq_generation = gen
            info._wait_record = record.to_dict()
            info._slot_released = True  # the token yield_slot spends
            info._resume_event = asyncio.Event()
            mgr._agents[task_id] = info
            return info

        try:
            lead = await _park("lead-1")
            victim = await _park("victim-1")
            assert mgr._running_count == 0
            for info in (lead, victim):
                assert mgr._admission.request_resume(info, reason="answered") is True

            original_wake = store.wake_wait
            ended = False

            def _end_the_victim_inside_the_window(*a: Any, **kw: Any) -> Any:
                nonlocal ended
                if not ended:
                    ended = True
                    # Exactly what the run's own terminal path does for a run
                    # that ended while parked: the release token is already
                    # spent, so nothing is given back, and the resume entry it
                    # withdraws was popped by the pump one step ago.
                    victim.done = True
                    assert mgr._release_slot(victim) is False
                    victim._resume_pending = False
                    victim._resume_event = None
                return original_wake(*a, **kw)

            monkeypatch.setattr(store, "wake_wait", _end_the_victim_inside_the_window)
            assert mgr._drain_task is not None
            # flake-ok: the pump task ends after its pass over the parked runs, inside the suite timeout; pre-existing, moved verbatim
            await mgr._drain_task
            for _ in range(5):  # the refusal's own re-drain, if it scheduled one
                # flake-ok: five bounded loop turns for the refusal's own re-drain, each followed by a check of the task; pre-existing, moved verbatim
                await asyncio.sleep(0)
                if mgr._drain_task is not None and not mgr._drain_task.done():
                    # flake-ok: the pump task ends after its pass over the parked runs, inside the suite timeout; pre-existing, moved verbatim
                    await mgr._drain_task
            monkeypatch.setattr(store, "wake_wait", original_wake)

            assert ended, "the lead's wake never ran: this pin never reached the window"
            assert lead._slot_released is False, "the live run was granted its slot"
            assert victim._slot_released is True, "a finished run was published as a slot holder"
            assert mgr._running_count == 1, "the finished run's reservation was never given back"
            # The landed wake is adopted even on the refusal, so the victim's own
            # terminal write is not fenced out by the row it left behind.
            row = await store.run(store.get, victim.id)
            assert victim._taskq_generation == row.generation
            mgr._admission.taskq_settle(victim)
            await async_wait_until(
                lambda: asyncio.to_thread(lambda: store.state_of(victim.id) in model.TERMINAL),
                describe=lambda: "victim not yet in terminal state",
            )
            assert (await store.run(store.get, victim.id)).state in model.TERMINAL
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_closed_admission_neither_refills_the_window_nor_grants_a_resume(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The updater's boundary reaches the pump's two re-registrations.

        While admission is CLOSED the window takes no store row (it would only be
        refused by the spawn gate) and a resume reserves no slot (the run stays
        parked, its wait intact). Both are HELD, not dropped: the same two steps
        land once admission reopens, which is what keeps the guard from trading a
        loosened census for stranded work.
        """
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "closed-row",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        parked = SubagentInfo(id="parked-1", task="t")
        parked._slot_released = True
        parked._resume_event = asyncio.Event()
        mgr._agents["parked-1"] = parked
        try:
            mgr._sessions.admission_closed = True
            running_before = mgr._running_count
            assert await mgr._admission.taskq_refill_window_async() >= 1
            assert mgr._queue == []  # the row stayed on disk
            assert mgr._admission.resume_reserve({"_resume_id": "parked-1"}) is False
            assert mgr._running_count == running_before
            assert parked._slot_released is True and parked._resume_pending is False
            assert not parked._resume_event.is_set()
            assert (await store.run(store.get, "closed-row")).state == model.QUEUED

            mgr._sessions.admission_closed = False
            assert await mgr._admission.taskq_refill_window_async() >= 1
            assert [p.get("_preassigned_id") for p in mgr._queue] == ["closed-row"]
            assert mgr._admission.resume_reserve({"_resume_id": "parked-1"}) is True
            assert mgr._running_count == running_before + 1
        finally:
            mgr._queue.clear()
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_lanes_endpoint_reads_the_store_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``GET /api/spawn/lanes``: BOTH store halves of the snapshot run on the
        writer thread -- the rows waiting per lane, and the lane a live run whose
        parent is absent from this process resolves to
        (``lane_for_session`` -> ``store.get``).

        Pinned two ways because one is not enough anywhere near this guard: the
        counter (which no ``except`` can swallow) and the armed guard (this route
        has no exception arm, so an on-loop take would answer 500).
        """
        from kiro_crew.dashboard.handlers import spawn_resume
        from kiro_crew.subagent import SubagentInfo

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        # One row waiting on disk, outside the window: the ``pending_lanes`` read.
        rec = mgr._admission.taskq_build_record(
            "lane-row",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        # One live run under a parent this manager never knew: the ``store.get``.
        mgr._agents["kid-1"] = SubagentInfo(
            id="kid-1", task="t", parent_session_key="subagent:ghost-1"
        )
        app = web.Application()
        app["state"] = SimpleNamespace(subagents=mgr)
        request = make_mocked_request("GET", "/api/spawn/lanes", app=app)
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            before = store.loop_thread_calls
            response = await spawn_resume.api_spawn_lanes(request)
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            body = json.loads(response.text)
            # Both reads are IN the answer, so the offload is of live work.
            assert body["lanes"]["web-1"]["queued"] == 1
            assert sum(lane["running"] for lane in body["lanes"].values()) == 1
            assert body["capacity"]["cap_total"] >= 1
        finally:
            mgr._agents.clear()
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_wave_reads_run_off_loop_for_every_coroutine_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``batch_members_pending`` and the reaper's two wave sweeps: the
        store-only membership read (``fetch_pending_by_batch``) runs on the
        writer thread.

        The COUNTER is the pin here, not the raise: every coroutine caller of
        these sits inside ``except Exception`` -- the reaper's per-sweep guard and
        the gateway consumer's ``_last`` fallback -- so an armed guard would only
        flip a wave to "not pending" and skip the sweep silently.
        """
        import inspect

        from kiro_crew.subagent import SubagentInfo
        from kiro_crew.subagent_manager import monitoring as monitoring_mod

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)
        rec = mgr._admission.taskq_build_record(
            "wave-row",
            {"task": "t", "parent_session_key": "web-1", "batch_id": "wv"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        rec.params["batch_id"] = "wv"
        assert mgr._admission.taskq_accept_record(rec) is None
        # ``wv``: submissions outstanding and no in-memory trace, so the sweep
        # reaches the store. ``held``: a finished member still holding its digest.
        mgr._batch_submitted["wv"] = [1, 2]
        mgr._batch_progress_ts["wv"] = 0.0
        held = SubagentInfo(id="held-1", task="t", batch_id="held", done=True)
        held._digest_held_at = 1.0
        mgr._agents["held-1"] = held
        mgr._on_done = lambda info: None
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            before = store.loop_thread_calls
            assert await mgr.batch_members_pending_async("wv") is True
            assert await mgr.batch_members_pending_async("held") is False
            await mgr._sweep_stuck_waves_async(1e9)
            await mgr._sweep_digest_holds_async(1e9)
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            # The store-only member held the wave open, so nothing was reconciled.
            assert mgr._batch_submitted["wv"] == [1, 2]
            # And the two call sites take the awaited form, not the sync entry.
            reaper = inspect.getsource(monitoring_mod.OrphanStallMonitor._reaper_loop_impl)
            assert "await self._manager._sweep_stuck_waves_async(mono_now)" in reaper
            assert "await self._manager._sweep_digest_holds_async(mono_now)" in reaper
            consumer = inspect.getsource(GatewayOrchestrator._init_subagents)
            assert "await _subagent_batch_pending(self.subagent_mgr, _batch_id)" in consumer
        finally:
            mgr._agents.clear()
            mgr._on_done = None
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_the_first_dependency_coordinator_is_built_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The FIRST coordinator rebuilds the schedule from every waiting row, so
        a coroutine takes ``dependency_coordinator_async``.

        The sync entry is the negative control, and it is only a control because
        ``taskq_coordinator`` re-raises ``OnLoopStoreError`` out of its rebuild
        guard: while that arm swallowed it, an armed run reported a failed rebuild
        and restored NOTHING instead of naming the caller.
        """
        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        record = WaitRecord.dependency("github:api", since=store.now(), retry_at=store.now() + 30)
        rec = model.TaskRecord(id="dep-1", kind=model.KIND_SUBAGENT, params={"task": "t"})
        await store.run(store.accept_one, rec)
        claimed = await store.run(store.claim, "dep-1")
        await store.run(store.transition, "dep-1", model.STARTING, generation=claimed.generation)
        await store.run(store.transition, "dep-1", model.RUNNING, generation=claimed.generation)
        assert await store.run(
            store.enter_wait, "dep-1", record.to_dict(), generation=claimed.generation
        )
        try:
            monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
            with pytest.raises(OnLoopStoreError, match="taken on the event loop"):
                mgr.dependency_coordinator()
            assert getattr(mgr, "_taskq_dependency_coordinator", None) is None
            before = store.loop_thread_calls
            coordinator = await mgr.dependency_coordinator_async()
            assert coordinator is not None
            assert store.loop_thread_calls == before
            monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)
            # The waiting row rejoined its scope, which is what the rebuild is for.
            assert coordinator.next_deadline() is not None
        finally:
            dep_mod.register_coordinator(None)
            await mgr.cancel_all()

    @pytest.mark.asyncio
    async def test_a_cancel_inside_the_parked_defers_window_is_announced_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Taking the defer off the loop opens a window; one id still gets ONE
        terminal event.

        The window is real, not simulated: the writer thread is held inside
        ``store.defer`` while the loop runs the cancel a dashboard Stop runs, so
        the write returns False for a row that EXISTS and is cancelled. That is a
        different fact from the write's other False -- no row at all, a ``_queue``
        entry that never reached the store -- and only the second owes the
        requester a rejection. Announcing one for the first would tell a single id
        both that the user stopped it and that the host refused it, which is what
        the pre-existing ``ClaimPoint`` window already avoids.
        """
        import threading

        mgr = await self._manager_with_store(monkeypatch)
        store = mgr._admission.taskq_store()
        assert store is not None
        rec = mgr._admission.taskq_build_record(
            "row0",
            {"task": "t", "parent_session_key": "web-1"},
            parent_session_key="web-1",
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        store.accept([rec])
        announced: list[Any] = []
        monkeypatch.setattr(
            type(mgr), "_announce_rejection", lambda self, info: announced.append(info) or info
        )
        # Low memory defers every spawn, so the drain takes the parked-defer path.
        from kiro_crew import subagent as subagent_mod

        monkeypatch.setattr(subagent_mod, "check_memory_available", lambda *a, **k: (False, 1.2))
        monkeypatch.setattr(admission_mod.SpawnAdmissionCoordinator, "pump_off_loop", True)

        parked = threading.Event()
        release = threading.Event()
        real_defer = store.defer

        def _park_inside_the_write(*a: Any, **kw: Any) -> bool:
            parked.set()
            assert release.wait(10)
            return bool(real_defer(*a, **kw))

        monkeypatch.setattr(store, "defer", _park_inside_the_write)
        try:
            drain = asyncio.ensure_future(mgr._drain_queue_async())
            assert await asyncio.to_thread(parked.wait, 10)
            assert await mgr.cancel("row0") is True
            release.set()
            await await_until(drain, what="drain completed after cancellation")
            assert store.state_of("row0") == model.CANCELLED
            assert announced == [], (
                "a row the canceller already reported was announced again, so one id "
                "carries two contradictory terminal events"
            )
        finally:
            release.set()
            await mgr.cancel_all()
