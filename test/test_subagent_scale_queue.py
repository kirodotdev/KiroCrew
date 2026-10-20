"""Retry gating and the durable task queue at scale.

``POST /api/spawn/{id}/retry`` respawns only a terminal FAILED agent; 2000 accepted spawns
are 2000 rows in ``tasks.db`` behind a bounded in-memory window, and a fresh manager drains
them in order; a run id is 16 random hex characters drawn at one site; and the
``subagent_queued`` depth a parent receives ends at 0. The mocks and the autouse manager
close come from ``subagent_scale_helpers.py``.
"""

from __future__ import annotations

import asyncio
import pathlib
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import settle_depth_emits, settle_store_writes
from subagent_scale_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _close_subagent_managers,
    _mock_ctx,
    _mock_sessions,
)

from kiro_crew import subagent as subagent_module
from kiro_crew.subagent import _RUN_ID_HEX_CHARS, SubagentInfo, SubagentManager
from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── 5. Retry endpoint gating ─────────────────────────────────────────


class TestRetryGating:
    def _mgr_with(self, info: SubagentInfo) -> MagicMock:
        mgr = MagicMock()
        mgr.get = MagicMock(return_value=info)
        return mgr

    def _request(self, mgr, agent_id: str):
        req = MagicMock()
        req.app = {"state": MagicMock(subagents=mgr)}
        req.match_info = {"agent_id": agent_id}
        return req

    @pytest.mark.asyncio
    async def test_retry_rejects_running_and_stopped(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry

        running = SubagentInfo(id="r1", task="t")
        resp = await api_spawn_retry(self._request(self._mgr_with(running), "r1"))
        assert resp.status == 409

        stopped = SubagentInfo(id="r2", task="t")
        stopped.done = True
        stopped.user_stopped = True
        resp = await api_spawn_retry(self._request(self._mgr_with(stopped), "r2"))
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_retry_respawns_failed_with_original_task(self):
        from kiro_crew.dashboard.handlers.messaging import api_spawn_retry
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        failed = SubagentInfo(id="f1", task="redacted task", parent_session_key="dashboard:m")
        failed.done = True
        failed.error = "boom"
        failed._raw_task = "original raw task"
        failed.execution_context = ExecutionContext(
            None, MemoryStoreRef("default"), "template", "kirocrew"
        )
        mgr = self._mgr_with(failed)
        new_info = SubagentInfo(id="n1", task="original raw task")
        mgr.spawn = MagicMock(return_value=new_info)
        resp = await api_spawn_retry(self._request(mgr, "f1"))
        assert resp.status == 200
        assert mgr.spawn.call_args.args[0] == "original raw task"
        assert mgr.spawn.call_args.kwargs["parent_session_key"] == "dashboard:m"


# ── 6. Durable task queue at scale ───────────────────────────────────

# Ceiling for the whole-queue drain below. The drain is a THROUGHPUT budget,
# not a race: every row costs one claim + finalize round trip through the
# store's writer thread, so the wall clock tracks the runner's per-row cost and
# nothing about ordering. Measured on this Linux host: 0.58-0.60 s for 199 rows
# idle and 0.86 s worst with eight copies of the test running at once — ~3-4 ms
# per row, ~9 rows landing per poll wake. The Windows CI shard that reported
# this drained 165 rows in under 30 s, i.e. ~180 ms per row, ~50x this host, so
# all 199 want ~36 s there. 150 s is the measured worst case x 175, which is
# also ~4x the shard's own projected time. It is a LOST-RUN guard, never the
# barrier — the wait returns the instant the last row lands, so a healthy run
# pays only what the drain costs.
_DRAIN_CEILING_SECS = 150.0


async def _await_drained(mgr, store, started: list[str], expected: int) -> None:
    """Wait until *mgr* has drained *expected* queued rows, or fail saying so.

    The completion of the drain is its OWN assertion. A bounded loop that
    exits silently on its ceiling hands the next assertion a SHORT list, and
    the run then fails as ``assert started == ids[1:]`` — "Right contains 34
    more items" — which reads as a queue that lost or reordered rows when
    every row it did drain was in FIFO order and the drain was merely
    unfinished. So the ceiling raises here instead, naming how many of the rows
    landed and how long it waited, and the order assertion runs only on a
    complete drain.

    What is waited on is the conjunction the assertions then read: rows
    started, the window emptied, the running count back to zero and the
    store's own DONE count. ``_release_slot`` → ``_drain_queue`` is what
    advances the queue in production (the pump coalesces a request that lands
    mid-pass into one more pass), so this polls that path rather than pumping
    it — measured identical, 23 wakes and 0.6 s either way.
    """
    from kiro_crew.taskq import model

    await async_wait_until(
        lambda: len(started) >= expected
        and not mgr._queue
        and mgr._running_count == 0
        and store.count(state=model.DONE) >= expected,
        timeout=_DRAIN_CEILING_SECS,
        describe=lambda: (
            f"drain unfinished after {_DRAIN_CEILING_SECS:.1f}s: {len(started)} of {expected} rows started, "
            f"{store.count(state=model.DONE)} DONE, "
            f"{len(mgr._queue)} still in the window, "
            f"running_count={mgr._running_count}"
        ),
    )


class TestDurableQueueScale:
    """The in-memory ``_queue`` is a bounded window over ``tasks.db``.

    2000 accepted spawns are 2000 committed rows and at most
    ``agent.task_dispatch_window`` dicts in memory; every row survives the
    manager being thrown away, and a fresh manager drains them in order.
    """

    @pytest.mark.asyncio
    async def test_2000_spawns_are_2000_rows_and_at_most_64_dicts(self):
        from kiro_crew.taskq import model

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=4)
        await mgr.wait_taskq_ready()
        mgr._spawn_stagger_secs = 0.0
        store = mgr._taskq
        assert store is not None and store.window == 64
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            ids = [
                mgr.spawn(f"task {i}", parent_session_key="dashboard:s1").id for i in range(2000)
            ]
        assert len(set(ids)) == 2000
        assert store.count() == 2000
        assert store.count(state=model.STARTING) == 4
        assert store.count(state=model.QUEUED) == 1996
        assert len(mgr._queue) == 64
        assert mgr.queued_count_for("dashboard:s1") == 1996
        assert mgr.has_pending_work_for("dashboard:s1") is True
        # the window holds the OLDEST queued rows, in submission order
        assert [p["_preassigned_id"] for p in mgr._queue] == ids[4:68]

    @pytest.mark.asyncio
    async def test_queue_survives_manager_loss_and_drains_fifo(self):
        from kiro_crew.taskq import model

        first = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1
        )
        await first.wait_taskq_ready()
        first._spawn_stagger_secs = 0.0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            ids = [first.spawn(f"t{i}", parent_session_key="dashboard:s1").id for i in range(200)]
        first._taskq.close()
        del first
        second = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=3
        )
        await second.wait_taskq_ready()
        second._spawn_stagger_secs = 0.0
        second._last_spawn_ts = 0.0
        store = second._taskq
        assert store.count(state=model.QUEUED) == 199
        started: list[str] = []

        async def run(self, info):
            started.append(info.id)
            info.done = True
            second._claim_finalize(info)
            if second._release_slot(info):
                second._running_count -= 1
                second._drain_queue()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=run),
        ):
            second._drain_queue()  # the recovery kick; releases pump the rest
            await _await_drained(second, store, started, 199)
        assert started == ids[1:]  # FIFO across the window boundary, none lost
        assert store.count(state=model.DONE) == 199
        assert len(mgr_queue := second._queue) == 0, mgr_queue
        assert second._running_count == 0


# ── 7. Run-id minting ────────────────────────────────────────────────


class TestRunIdMinting:
    """The width of a run id is what keeps it unique, and one draw site owns it.

    At 8 hex characters an id was 32 bits, so 2000 spawns on one host collided
    about once in 2,100 times, and the collision did not read as one: identity
    is assigned before registration, so the caller was handed the id and the
    accept then failed on the duplicate primary key, reaching the user as
    ``task store write failed``. 16 characters is 64 bits, which puts the same
    2000 draws at about 1 in 10**13 -- no registry to seed, nothing to read, and
    no place for a spawn path to forget the check.
    """

    @pytest.mark.asyncio
    async def test_a_minted_id_is_sixteen_hex_characters(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=4)
        await mgr.wait_taskq_ready()
        assert _RUN_ID_HEX_CHARS == 16
        for _ in range(50):
            assert re.fullmatch(r"[0-9a-f]{16}", mgr._mint_agent_id())

    @pytest.mark.asyncio
    async def test_every_spawned_id_carries_the_full_width(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        mgr._spawn_stagger_secs = 0.0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            ids = [mgr.spawn(f"t{i}", parent_session_key="dashboard:s1").id for i in range(40)]
        # Running and queued alike: the gate assigns one identity and every exit
        # path carries it, so a queued row must not be narrower than a started one.
        assert len(set(ids)) == 40
        assert [i for i in ids if not re.fullmatch(r"[0-9a-f]{16}", i)] == []

    def test_the_width_is_the_uniqueness_argument(self):
        # 2000 draws, birthday bound n**2 / (2 * space). The 8-character id this
        # replaces sat at ~1/2100, which CI hit as a red 2000-spawn shard.
        space = 2 ** (4 * _RUN_ID_HEX_CHARS)
        assert 2000**2 / (2 * space) < 1e-12
        assert 2000**2 / (2 * 2**32) > 1e-4

    @pytest.mark.asyncio
    async def test_every_character_of_the_id_is_random(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=4)
        await mgr.wait_taskq_ready()
        drawn = [mgr._mint_agent_id() for _ in range(200)]
        # A v4 UUID spends its 13th hex character on the fixed version digit, so
        # the first 16 characters of one carry 60 bits, not the 64 the width
        # advertises. Every position has to vary or the bound above is wrong.
        for pos in range(_RUN_ID_HEX_CHARS):
            assert len({d[pos] for d in drawn}) > 1, pos

    def test_one_draw_site_owns_the_width(self):
        import inspect

        import kiro_crew.subagent_manager as manager_pkg

        package = pathlib.Path(manager_pkg.__file__).parent
        component_draws = [
            f"{path.relative_to(package)}:{lineno}"
            for path in sorted(package.rglob("*.py"))
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if "urandom" in line or "uuid4()" in line
        ]
        # The gate, the continuation coordinator and the wave digest all ask the
        # manager for an id. A new spawn path that draws its own would reopen the
        # collision at whatever width it picked, so none of them may draw.
        assert component_draws == []

        facade = pathlib.Path(subagent_module.__file__)
        family = "".join(
            path.read_text(encoding="utf-8")
            for path in (facade, *sorted(facade.with_name("subagent_parts").glob("*.py")))
        )
        assert family.count("os.urandom(") == 1
        assert "uuid4()" not in family
        mint = inspect.getsource(subagent_module.SubagentManager._mint_agent_id)
        assert "os.urandom(_RUN_ID_HEX_CHARS // 2).hex()" in mint


# ── 8. Queued-depth events against the durable store ─────────────────────


class TestQueuedDepthReachesZero:
    """The ``subagent_queued`` depth a parent receives must end at 0.

    The pump emits the parent's depth right after popping a row from the
    window. That row's durable state is still QUEUED until its claim lands,
    and it is in none of the store count's exclusion sets (not windowed, not
    registered, not admitting). Counting it there makes every emit between
    pop and claim one higher than the window: a 4-cap, 5-spawn wave reads
    4, 3, 2, 1 and stops, and the dashboard chip clears its "waiting" count
    only on 0, so one agent stays "waiting" under a finished turn. The pump
    marks the popped row dispatching and the count excludes it until
    ``spawn`` has answered for it.
    """

    @pytest.mark.asyncio
    async def test_last_pop_publishes_zero_with_a_store_attached(self):
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=4)
        await mgr.wait_taskq_ready()
        assert mgr._taskq is not None, "this test is about the store-backed count"
        mgr._spawn_stagger_secs = 0.0
        depths: list[int] = []
        real_fire = mgr._fire_event

        async def fire(etype, info, extra=None):
            if etype == "subagent_queued" and info.parent_session_key == "dashboard:s1":
                depths.append(int((extra or {}).get("queued", -1)))
            return await real_fire(etype, info, extra)

        mgr._fire_event = fire  # type: ignore[method-assign]
        started: list[str] = []

        async def run(self, info):
            started.append(info.id)
            # flake-ok: pre-existing 50 ms stand-in for a worker's run time -- moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms stand-in for a worker's run time moved verbatim
            await asyncio.sleep(0.05)
            info.done = True
            self._claim_finalize(info)
            if self._release_slot(info):
                self._running_count -= 1
                self._drain_queue()

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=run),
        ):
            for i in range(5):
                mgr.spawn(f"t{i}", parent_session_key="dashboard:s1", batch_id="b1")
            await async_wait_until(
                lambda: len(started) == 5 and mgr._running_count == 0 and not mgr._queue,
                timeout=15.0,
                describe=lambda: f"started={len(started)}, running={mgr._running_count}, queued={len(mgr._queue)}",
            )
            await settle_depth_emits(mgr)
        assert len(started) == 5
        assert mgr.queued_count_for("dashboard:s1") == 0
        assert mgr._dispatching_ids == set()
        assert depths, "no subagent_queued event reached the parent"
        assert depths[-1] == 0, f"depth sequence never reached 0: {depths}"
        # Once the last waiting row is popped, nothing may report it as waiting again.
        first_zero = depths.index(0)
        assert all(d == 0 for d in depths[first_zero:]), depths

    def test_dispatching_rows_are_excluded_from_dispatch_reads_only(self):
        """Two exclusion sets, on purpose. The pump's refill (and, through its
        own set, the depth the chip shows) leaves a popped row out (it is being
        started); a parent's Stop and every other pending-work read keep seeing
        it, because a row in exactly that popped-unclaimed state is the one a
        Stop must still reach."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=4)
        mgr._dispatching_ids.add("popped-row")
        assert "popped-row" in mgr._admission.taskq_dispatch_excluded_ids()
        assert "popped-row" not in mgr._admission.taskq_excluded_ids()
        mgr._dispatching_ids.discard("popped-row")
        assert "popped-row" not in mgr._admission.taskq_dispatch_excluded_ids()

    @pytest.mark.asyncio
    async def test_a_popped_but_unclaimed_row_still_counts_as_pending_work(self):
        """The reset-deferral guards read ``has_pending_work_for`` /
        ``queued_count_for``. While the pump holds a popped row between the pop
        and its claim, the parent still has accepted work that has not run: a
        cron teardown that read "nothing pending" here would reset the parent
        before the child starts. Only the chip's depth leaves the row out."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        assert mgr._taskq is not None
        mgr._spawn_stagger_secs = 0.0
        hold = asyncio.Event()
        released = asyncio.Event()
        depths: list[int] = []
        real_fire = mgr._fire_event

        async def fire(etype, info, extra=None):
            if etype == "subagent_queued" and info.parent_session_key == "dashboard:s1":
                depths.append(int((extra or {}).get("queued", -1)))
            return await real_fire(etype, info, extra)

        mgr._fire_event = fire  # type: ignore[method-assign]

        async def slow_policy(fn, *a, **kw):
            hold.set()
            # flake-ok: pre-existing park until the test releases the pump's spec read -- moved verbatim; tracked for a de-flake pass
            await released.wait()
            return fn(*a, **kw)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
            patch("asyncio.to_thread", new=slow_policy),
            patch.object(type(mgr._admission), "pump_off_loop", True),
        ):
            first = mgr.spawn("t0", parent_session_key="dashboard:s1")
            second = mgr.spawn("t1", parent_session_key="dashboard:s1")
            assert second.queued
            first.done = True
            mgr._claim_finalize(first)
            assert mgr._release_slot(first)
            mgr._running_count -= 1
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
            popped_at = len(depths)
            mgr._drain_queue()
            await await_until(hold)
            assert second.id in mgr._dispatching_ids and not mgr._queue
            # The guards' reading: the row is still this parent's work.
            assert await mgr.has_pending_work_for_async("dashboard:s1") is True
            assert mgr.queued_count_for("dashboard:s1") == 1
            assert await mgr.queued_count_for_async("dashboard:s1") == 1
            # The chip's reading, as published: the row is being started.
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
            assert depths[popped_at:] == [0], depths
            released.set()
            # The claim and the start write are each shielded loop hops, so
            # wait for the registration itself rather than counting barriers.
            await async_wait_until(
                lambda: second.id in mgr._agents, describe=lambda: "the claimed row registered"
            )
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
        assert second.id in mgr._agents
        assert mgr._dispatching_ids == set()
        # Nothing published after the pop shows the started row as waiting.
        assert depths[popped_at:] and all(d == 0 for d in depths[popped_at:]), depths

    @pytest.mark.asyncio
    async def test_stop_all_reaches_a_popped_but_unclaimed_row(self):
        """The pump has popped the row and is between the pop and the claim
        (its policy read is on a thread) when the parent stops everything. The
        row is in no window and has no ``_agents`` record, so the store sweep
        is the only one that can see it -- and it must."""
        from kiro_crew.taskq import model

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        assert mgr._taskq is not None
        mgr._spawn_stagger_secs = 0.0
        hold = asyncio.Event()
        released = asyncio.Event()

        async def slow_policy(fn, *a, **kw):
            # The pump's off-loop spec read: park here so the popped row sits
            # unclaimed while the parent's Stop runs.
            hold.set()
            # flake-ok: pre-existing park until the test releases the pump's spec read -- moved verbatim; tracked for a de-flake pass
            await released.wait()
            return fn(*a, **kw)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
            patch("asyncio.to_thread", new=slow_policy),
            patch.object(type(mgr._admission), "pump_off_loop", True),
        ):
            first = mgr.spawn("t0", parent_session_key="dashboard:s1")
            second = mgr.spawn("t1", parent_session_key="dashboard:s1")
            assert second.queued
            first.done = True
            mgr._claim_finalize(first)
            assert mgr._release_slot(first)
            mgr._running_count -= 1
            mgr._drain_queue()
            await await_until(hold)
            # Popped, marked, unclaimed, and invisible to the in-memory sweeps.
            assert second.id in mgr._dispatching_ids
            assert not mgr._queue
            assert second.id not in mgr._agents or mgr._agents[second.id].queued
            _running, queued_stopped = await mgr.cancel_for_parent("dashboard:s1")
            released.set()
            await settle_store_writes(mgr._taskq, rounds=4)
        assert queued_stopped == 1, "Stop all did not reach the popped row"
        row = mgr._taskq.get(second.id)
        assert row is not None and row.state not in (
            model.QUEUED,
            model.ADMITTED,
            model.STARTING,
            model.RUNNING,
        ), row.state
        assert not (
            second.id in mgr._agents and not mgr._agents[second.id].done
        ), "row started after Stop all"
        assert mgr._dispatching_ids == set()

    @pytest.mark.asyncio
    async def test_an_unavailable_claim_publishes_the_row_as_waiting(self):
        """A claim the store cannot take leaves the row QUEUED. The re-entry's
        depth emit is the one the chip reads next, so it must count the row
        (1), never a dispatching-masked 0."""
        from kiro_crew import taskq as _taskq

        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        assert mgr._taskq is not None
        mgr._spawn_stagger_secs = 0.0
        depths: list[tuple[int, frozenset[str]]] = []
        real_fire = mgr._fire_event

        async def fire(etype, info, extra=None):
            if etype == "subagent_queued" and info.parent_session_key == "dashboard:s1":
                depths.append(
                    (int((extra or {}).get("queued", -1)), frozenset(mgr._dispatching_ids))
                )
            return await real_fire(etype, info, extra)

        mgr._fire_event = fire  # type: ignore[method-assign]
        store = mgr._taskq
        real_claim = store.claim
        outage: dict[str, str] = {}

        def claim(agent_id, *a, **kw):
            if agent_id == outage.get("id"):
                raise _taskq.TaskStoreUnavailable("probe outage")
            return real_claim(agent_id, *a, **kw)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
            patch.object(store, "claim", new=claim),
        ):
            first = mgr.spawn("t0", parent_session_key="dashboard:s1")
            second = mgr.spawn("t1", parent_session_key="dashboard:s1")
            assert second.queued
            outage["id"] = second.id
            # The first run finishes and releases its slot; the drain pops the
            # queued row and its claim hits the outage.
            first.done = True
            mgr._claim_finalize(first)
            if mgr._release_slot(first):
                mgr._running_count -= 1
                mgr._drain_queue()
            await async_wait_until(
                lambda: bool(depths and depths[-1][0] == 1 and not mgr._dispatching_ids),
                describe=lambda: f"depths={depths[-3:] if depths else []}, dispatching={mgr._dispatching_ids}",
            )
        assert mgr._dispatching_ids == set(), "mark leaked past a failed claim"
        assert depths and depths[-1][0] == 1, f"failed claim published {depths[-3:]}"
        assert mgr.queued_count_for("dashboard:s1") == 1

    @pytest.mark.asyncio
    async def test_a_dispatch_that_raises_leaves_no_mark(self):
        """A leaked mark would exclude the durable row from every refill, so a
        spawn that raises while dispatching must not leave one behind."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        mgr._spawn_stagger_secs = 0.0
        real_spawn = mgr.spawn
        boom: dict[str, str] = {}

        def spawn(*a, **kw):
            if kw.get("_from_queue") and kw.get("_preassigned_id") == boom.get("id"):
                raise RuntimeError("probe: dispatch raised")
            return real_spawn(*a, **kw)

        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(SubagentManager, "_run", new=AsyncMock()),
        ):
            first = mgr.spawn("t0", parent_session_key="dashboard:s1")
            second = mgr.spawn("t1", parent_session_key="dashboard:s1")
            assert second.queued
            boom["id"] = second.id
            mgr.spawn = spawn  # type: ignore[method-assign]
            first.done = True
            mgr._claim_finalize(first)
            assert mgr._release_slot(first)
            mgr._running_count -= 1
            # The inline pump does not catch a raising ``spawn``; the raise
            # reaches the caller, and the mark must already be gone by then.
            with pytest.raises(RuntimeError, match="probe: dispatch raised"):
                mgr._drain_queue()
        assert mgr._dispatching_ids == set(), "mark leaked past a raising dispatch"
        assert second.id not in mgr._admission.taskq_excluded_ids()

    @pytest.mark.asyncio
    async def test_a_raise_before_the_pick_is_logged_not_rethrown(self, caplog):
        """A store error in the awaits ahead of the pick reaches the pass's own
        handler and is logged; the mark cleanup after it walks an empty pick
        instead of raising over the handler."""
        mgr = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx(), max_concurrent=1)
        await mgr.wait_taskq_ready()
        assert mgr._taskq is not None

        async def boom(*_args):
            raise RuntimeError("probe: store read failed before the pick")

        with (
            patch.object(type(mgr._admission), "ensure_coordinator_async", new=boom),
            caplog.at_level("ERROR"),
        ):
            await mgr._drain_queue_pass()  # must not raise
        assert any("drain pump failed" in r.getMessage() for r in caplog.records)
        assert not any("UnboundLocalError" in (r.exc_text or "") for r in caplog.records)
