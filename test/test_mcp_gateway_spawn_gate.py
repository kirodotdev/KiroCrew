"""The daemon-global spawn gate and the ``spawn_queue`` protocol around it.

Mirrors ``test_mcp_gateway_breaker.py`` in spirit: the gate is a small
in-process object, so its contract is pinned directly -- FIFO order, a permit
whose ``settle`` is exactly-once and separate from ``release``, cancellation
that is neutral at every boundary, a capacity seam that admits waiters when
raised and revokes nothing when cut, and a drain that fails every waiter and
releases every watcher. Time is injected wherever the gate reads it; the one
real ``asyncio`` timing this file relies on is sub-100 ms.

The second half drives ``gatewayd._handle_connection`` with the same scripted
reader / recording writer the coverage suites use, so the WIRE is pinned too:
no ``capacity`` refusal carries ``fallback`` on EITHER wire shape -- the legacy
bare ``ensure_backend`` included, singly and eight at a time, because that tag
authorised an exec the daemon could not charge and the failure mode was a rate --
while a target-shaped ``compat`` refusal still does and is still charged; an old
stub never sees ``queued``; a new stub gets ``queued`` keepalives and
``retry_after_secs``; pings are answered while an acquire waits; the frames
parked during that wait are bounded in both count and bytes (a burst one under
the bound is parked whole; either bound exceeded drops that one connection); and
a fallback charges the host budget until the connection closes.

Three more properties are timing rather than shape, so no side of them is a
double. That the daemon's queue wait ends before the stub's -- a stub whose own
budget expires first runs the unaccounted ``fallback_exec`` the refusal exists to
withhold -- is measured with the real stub pre-flight and the real handler on
either end of a REAL endpoint, one budget serving both as the shipped defaults
do. That a waiter in the gate holds no host charge is measured with a real pool
and a real budget whose ceiling is the number of waiters. That a prewarm pass
stands down per key when a live stub queues during it runs a real
``run_gatewayd``, because the yield and the bounded wait are arguments that pass
supplies.
"""

from __future__ import annotations

import asyncio
import errno
import json
import re
import sys
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.config.sections import McpGatewayConfig
from kiro_crew.mcp_gateway import admission as adm
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import host_budget as hb
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import BackendUnavailable, PoolAtCapacity, PoolKey

_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="runs a real gatewayd against an AF_UNIX endpoint under a temp dir; "
    "Windows uses a named pipe whose name is not a filesystem path",
)

# --- clock -------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _settle() -> None:
    """Let every ready callback run (a few loop turns)."""
    for _ in range(5):
        await asyncio.sleep(0)


# --- SpawnGate unit --------------------------------------------------------


class TestPermit:
    @pytest.mark.asyncio
    async def test_settle_is_exactly_once_and_release_is_idempotent(self) -> None:
        gate = adm.SpawnGate(2)
        permit = await gate.acquire(label="a")
        permit.settle(adm.OUTCOME_SUCCESS)
        permit.settle(adm.OUTCOME_FAILURE)  # ignored: the first outcome sticks
        assert permit.outcome == adm.OUTCOME_SUCCESS
        permit.release()
        permit.release()
        assert gate.in_flight == 0
        assert gate.snapshot()["outcomes"] == {"success": 1, "failure": 0, "neutral": 0}

    @pytest.mark.asyncio
    async def test_release_without_settle_records_neutral(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="a")
        permit.release()
        assert permit.outcome == adm.OUTCOME_NEUTRAL
        assert gate.snapshot()["outcomes"]["neutral"] == 1

    @pytest.mark.asyncio
    async def test_unknown_outcome_is_refused(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="a")
        with pytest.raises(ValueError):
            permit.settle("maybe")
        permit.release()

    @pytest.mark.asyncio
    async def test_on_settle_seam_sees_each_outcome_once(self) -> None:
        seen: list[str] = []
        gate = adm.SpawnGate(2, on_settle=seen.append)
        a = await gate.acquire(label="a")
        b = await gate.acquire(label="b")
        a.settle(adm.OUTCOME_FAILURE)
        a.release()
        b.release()
        assert seen == [adm.OUTCOME_FAILURE, adm.OUTCOME_NEUTRAL]


class TestFifo:
    @pytest.mark.asyncio
    async def test_waiters_are_admitted_in_arrival_order(self) -> None:
        gate = adm.SpawnGate(1)
        first = await gate.acquire(label="first")
        order: list[str] = []

        async def wait(label: str) -> adm.Permit:
            permit = await gate.acquire(label=label)
            order.append(label)
            return permit

        tasks = [asyncio.create_task(wait(f"w{i}")) for i in range(3)]
        await _settle()
        assert gate.queued == 3 and gate.in_flight == 1
        first.release()
        await _settle()
        assert order == ["w0"]
        (await tasks[0]).release()
        await _settle()
        (await tasks[1]).release()
        await _settle()
        (await tasks[2]).release()
        assert order == ["w0", "w1", "w2"]
        assert gate.in_flight == 0 and gate.queued == 0

    @pytest.mark.asyncio
    async def test_a_newcomer_never_jumps_the_queue(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        queued = asyncio.create_task(gate.acquire(label="queued"))
        await _settle()
        held.release()
        # The slot went to the queued waiter, not to whoever asks next.
        late = asyncio.create_task(gate.acquire(label="late"))
        await _settle()
        assert queued.done() and not late.done()
        (await queued).release()
        await _settle()
        (await late).release()


class TestCancellation:
    @pytest.mark.asyncio
    async def test_a_cancelled_waiter_leaves_the_queue_and_counts_nothing(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        waiter = asyncio.create_task(gate.acquire(label="w"))
        await _settle()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate.queued == 0
        held.release()
        assert gate.in_flight == 0
        assert gate.snapshot()["outcomes"] == {"success": 0, "failure": 0, "neutral": 1}
        assert gate.snapshot()["cancelled"] == 1

    @pytest.mark.asyncio
    async def test_cancel_after_grant_but_before_resume_hands_the_slot_back(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        waiter = asyncio.create_task(gate.acquire(label="w"))
        await _settle()
        # Grant lands (release wakes the waiter) and the cancel arrives on the
        # same turn, before the waiter's coroutine resumes.
        held.release()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert gate.in_flight == 0, "the granted slot must be handed back"
        # And the next arrival gets it at once.
        nxt = await gate.acquire(label="next")
        nxt.release()


class TestCapacitySeam:
    @pytest.mark.asyncio
    async def test_raising_capacity_admits_queued_waiters(self) -> None:
        gate = adm.SpawnGate(1, floor=1, ceiling=8)
        held = await gate.acquire(label="held")
        waiters = [asyncio.create_task(gate.acquire(label=f"w{i}")) for i in range(3)]
        await _settle()
        assert gate.queued == 3
        assert gate.set_capacity(3) == 3
        await _settle()
        assert sum(w.done() for w in waiters) == 2 and gate.in_flight == 3
        held.release()
        await _settle()
        assert all(w.done() for w in waiters)
        for w in waiters:
            (await w).release()

    @pytest.mark.asyncio
    async def test_cutting_capacity_revokes_nothing_and_admits_less_afterwards(self) -> None:
        gate = adm.SpawnGate(4, floor=1, ceiling=8)
        permits = [await gate.acquire(label=str(i)) for i in range(4)]
        assert gate.set_capacity(1) == 1
        assert gate.in_flight == 4, "in-flight spawns finish; nothing is revoked"
        waiter = asyncio.create_task(gate.acquire(label="w"))
        for p in permits[:3]:
            p.release()
            await _settle()
            assert not waiter.done(), "still over the new capacity"
        permits[3].release()
        await _settle()
        assert waiter.done()
        (await waiter).release()

    def test_capacity_is_clamped_to_the_band(self) -> None:
        gate = adm.SpawnGate(100, floor=2, ceiling=6)
        assert gate.capacity == 6
        assert gate.set_capacity(0) == 2
        assert gate.set_capacity(4) == 4
        with pytest.raises(ValueError):
            adm.SpawnGate(1, floor=0)
        with pytest.raises(ValueError):
            adm.SpawnGate(1, floor=3, ceiling=2)


class TestDeadlineAndKeepalive:
    @pytest.mark.asyncio
    async def test_keepalive_reports_position_and_the_deadline_times_out(self) -> None:
        clock = _Clock()
        gate = adm.SpawnGate(1, clock=clock)
        held = await gate.acquire(label="held")
        ticks: list[adm.QueuePosition] = []

        async def on_queued(pos: adm.QueuePosition) -> None:
            ticks.append(pos)
            clock.now += 5.0  # each keepalive tick costs one interval

        with pytest.raises(adm.SpawnGateTimeout) as excinfo:
            await gate.acquire(
                label="w", deadline=clock.now + 12.0, on_queued=on_queued, keepalive_secs=0.01
            )
        assert len(ticks) >= 2
        assert ticks[0].position == 1 and ticks[0].capacity == 1
        assert ticks[0].frame()["type"] == "queued"
        assert excinfo.value.position == 1
        assert gate.queued == 0 and gate.snapshot()["timeouts"] == 1
        held.release()

    @pytest.mark.asyncio
    async def test_positions_are_one_based_and_skip_finished_waiters(self) -> None:
        clock = _Clock()
        gate = adm.SpawnGate(1, clock=clock)
        held = await gate.acquire(label="held")
        seen: dict[str, int] = {}

        def _cb(label: str) -> Any:
            async def on_queued(pos: adm.QueuePosition) -> None:
                seen.setdefault(label, pos.position)

            return on_queued

        w1 = asyncio.create_task(gate.acquire(label="w1", on_queued=_cb("w1"), keepalive_secs=0.01))
        await _settle()
        w2 = asyncio.create_task(gate.acquire(label="w2", on_queued=_cb("w2"), keepalive_secs=0.01))
        await asyncio.sleep(0.05)
        assert seen == {"w1": 1, "w2": 2}
        held.release()
        (await w1).release()
        (await w2).release()


class TestDrain:
    @pytest.mark.asyncio
    async def test_close_fails_waiters_refuses_newcomers_and_releases_watchers(self) -> None:
        gate = adm.SpawnGate(1)
        held = await gate.acquire(label="held")
        waiter = asyncio.create_task(gate.acquire(label="w"))
        await _settle()
        # A watcher parked on an initialize that will never come, holding the
        # permit that is blocking the waiter.
        done = asyncio.Event()
        watched = held
        task = gate.watch_initialize(
            watched,
            init_done=done,
            init_state=lambda: "unsent",
            process_exited=AsyncMock(),
            timeout=60.0,
        )
        await gate.close()
        with pytest.raises(adm.SpawnGateClosed):
            await waiter
        assert task.done() and watched.released and watched.outcome == adm.OUTCOME_NEUTRAL
        with pytest.raises(adm.SpawnGateClosed):
            await gate.acquire(label="late")
        assert gate.snapshot()["closed"] is True and gate.queued == 0


class TestInitializeWatcher:
    @staticmethod
    def _state_holder(initial: str) -> list[str]:
        return [initial]

    @pytest.mark.asyncio
    async def test_ready_settles_success_and_releases(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="p")
        done = asyncio.Event()
        state = self._state_holder("in_flight")
        exited = AsyncMock()
        task = gate.watch_initialize(
            permit, init_done=done, init_state=lambda: state[0], process_exited=exited, timeout=5.0
        )
        state[0] = "ready"
        done.set()
        await task
        assert permit.outcome == adm.OUTCOME_SUCCESS and permit.released
        assert gate.in_flight == 0
        exited.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_settles_failure_and_holds_until_the_process_is_reaped(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="p")
        done = asyncio.Event()
        state = self._state_holder("in_flight")
        reaped = asyncio.Event()

        async def exited() -> None:
            await reaped.wait()

        task = gate.watch_initialize(
            permit, init_done=done, init_state=lambda: state[0], process_exited=exited, timeout=5.0
        )
        state[0] = "failed"
        done.set()
        await _settle()
        assert permit.outcome == adm.OUTCOME_FAILURE
        assert not permit.released, "a failed backend may survive SIGKILL; hold until reaped"
        reaped.set()
        await task
        assert permit.released and gate.in_flight == 0

    @pytest.mark.asyncio
    async def test_no_initialize_at_all_is_neutral(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="p")
        task = gate.watch_initialize(
            permit,
            init_done=asyncio.Event(),
            init_state=lambda: "unsent",
            process_exited=AsyncMock(),
            timeout=0.01,
        )
        await task
        assert permit.outcome == adm.OUTCOME_NEUTRAL and permit.released

    @pytest.mark.asyncio
    async def test_a_handshake_still_in_flight_at_the_deadline_gets_one_more_window(self) -> None:
        gate = adm.SpawnGate(1)
        permit = await gate.acquire(label="p")
        done = asyncio.Event()
        state = self._state_holder("in_flight")
        task = gate.watch_initialize(
            permit,
            init_done=done,
            init_state=lambda: state[0],
            process_exited=AsyncMock(),
            timeout=0.02,
        )
        await asyncio.sleep(0.03)
        assert not task.done(), "in flight at the deadline: the backend's own timer fires next"
        state[0] = "ready"
        done.set()
        await task
        assert permit.outcome == adm.OUTCOME_SUCCESS


class TestAdmissionBundle:
    @pytest.mark.asyncio
    async def test_a_host_charge_follows_the_process_not_the_shutdown(self) -> None:
        budget = hb.HostBudget(hb.HostBudgetLimits(max_procs=2))
        admission = adm.Admission(
            gate=adm.SpawnGate(1),
            budget=budget,
            initialize_timeout_secs=1.0,
            spawn_queue_wait_secs=5.0,
        )
        charge = budget.reserve(label="b")
        exited = asyncio.Event()

        async def wait() -> None:
            await exited.wait()

        admission.track_process(charge, wait)
        await _settle()
        assert budget.procs_in_use == 1, "still charged while the process lives"
        exited.set()
        await _settle()
        assert budget.procs_in_use == 0 and charge.released

    @pytest.mark.asyncio
    async def test_close_drops_every_charge_and_cancels_reapers(self) -> None:
        budget = hb.HostBudget(hb.HostBudgetLimits(max_procs=2))
        admission = adm.Admission(
            gate=adm.SpawnGate(1),
            budget=budget,
            initialize_timeout_secs=1.0,
            spawn_queue_wait_secs=5.0,
        )
        charge = budget.reserve(label="b")
        admission.track_process(charge, asyncio.Event().wait)
        await admission.close()
        assert budget.procs_in_use == 0 and admission.gate.closed
        assert admission.snapshot()["host_budget"]["charges"] == 0


# --- daemon protocol -----------------------------------------------------------

_STUB = "stub-admission-0001"


class _FakeWriter:
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        return None

    def frames(self) -> list[Any]:
        return [json.loads(p.decode("utf-8")) for p in self.writes]


class _ScriptedReader:
    """Queued frames, then EOF. An ``asyncio.Event`` item blocks the read until
    the event is set, which is how a test holds the socket open while an
    acquire is pending and then hangs up at a chosen moment."""

    def __init__(self, *items: Any) -> None:
        self._items = list(items)

    @property
    def remaining(self) -> int:
        return len(self._items)

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        while self._items and isinstance(self._items[0], asyncio.Event):
            await self._items[0].wait()
            self._items.pop(0)
        if not self._items:
            raise asyncio.IncompleteReadError(b"", None)
        item = self._items.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, bytes):
            return item
        return json.dumps(item).encode("utf-8") + b"\n"


def _padded_frame(size: int, seq: int) -> bytes:
    """One non-ping stub frame whose wire length is exactly ``size`` bytes.

    Padding rather than a real payload keeps the byte-bound test off a 64 MiB
    allocation: the bound is moved to meet the frames instead.
    """
    head = b'{"jsonrpc":"2.0","id":%d,"method":"tools/list","params":{"pad":"' % seq
    tail = b'"}}\n'
    return head + b"A" * (size - len(head) - len(tail)) + tail


def _register_frame(**overrides: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "type": "register",
        "stub_uuid": _STUB,
        "poolable": True,
        "server_name": "demo-mcp",
        "agent_name": "adm-agent",
        "command_args_hash": "a" * 8,
        "effective_env_hash": "e" * 8,
        "work_dir": "/tmp/adm",
        "binary_version": "1.0",
        "os_uid": 1000,
        "sandbox_mode": "none",
        "autoapprove_set_hash": "b" * 8,
        "approval_mode": "reads",
        "trust_all_tools": False,
        "config_snapshot_hash": "c" * 8,
        "session_key": "sess-adm",
        "session_type": "dashboard",
        "ancestor_pids": [4242],
    }
    frame.update(overrides)
    return frame


def _fake_pool() -> MagicMock:
    pool = MagicMock()
    pool.unreserve = MagicMock()
    pool.reserve = MagicMock()
    pool.release_exclusive = AsyncMock(return_value=None)
    pool.get = AsyncMock(return_value=None)
    pool.all_backends = MagicMock(return_value=[])
    pool.metrics_snapshot_async = AsyncMock(return_value={"backends": 0})
    return pool


def _resolver(pool_key: PoolKey) -> tuple[str, list[str], dict[str, str], str]:
    return "demo-mcp-server", [], {}, pool_key.work_dir


async def _noop_pump() -> None:
    return None


def _fake_backend() -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    backend = Backend(
        pool_key=PoolKey.from_register(_register_frame()),
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    return backend


def _admission(**kw: Any) -> adm.Admission:
    return adm.Admission(
        gate=adm.SpawnGate(kw.pop("capacity", 1)),
        budget=hb.HostBudget(hb.HostBudgetLimits(max_procs=kw.pop("max_procs", 0))),
        initialize_timeout_secs=kw.pop("initialize_timeout_secs", 1.0),
        spawn_queue_wait_secs=kw.pop("spawn_queue_wait_secs", 30.0),
    )


async def _handle(reader: Any, writer: Any, pool: Any, admission: adm.Admission | None) -> None:
    await gw._handle_connection(
        reader,
        writer,
        pool,
        _resolver,
        Path("/tmp/adm.sock"),
        None,
        admission=admission,
    )


@pytest.fixture(autouse=True)
def _isolate_module_globals():
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._STUB_PROBES.clear()


@pytest.fixture
def peer_ok(monkeypatch):
    monkeypatch.setattr(gw.socketsec, "PEER_IDENTITY_SUPPORTED", True)
    monkeypatch.setattr(
        gw.socketsec, "check_peer_is_self", lambda w: gw.socketsec.PeerCredResult.MATCH
    )
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda w: None)
    monkeypatch.setattr(gw, "_audit_pool_fallback", lambda *a: None)
    monkeypatch.setattr(gw, "_audit_pool_rejected", lambda *a: None)


class TestWaitBudgetNegotiation:
    def test_absent_or_malformed_budget_means_an_old_stub(self) -> None:
        admission = _admission(spawn_queue_wait_secs=600.0)
        for frame in (
            {"type": "ensure_backend"},
            {"type": "ensure_backend", "wait_budget_secs": "600"},
            {"type": "ensure_backend", "wait_budget_secs": True},
            {"type": "ensure_backend", "wait_budget_secs": 0},
            {"type": "ensure_backend", "wait_budget_secs": -3},
            {"type": "ensure_backend", "wait_budget_secs": float("inf")},
            {"type": "ensure_backend", "wait_budget_secs": float("nan")},
        ):
            assert gw._negotiated_wait_budget(frame, admission) is None, frame

    def test_the_daemon_budget_caps_the_stub_budget(self) -> None:
        admission = _admission(spawn_queue_wait_secs=100.0)
        margin = gw._QUEUE_REFUSAL_MARGIN_SECS
        assert gw._negotiated_wait_budget({"wait_budget_secs": 600}, admission) == 100.0 - margin
        assert gw._negotiated_wait_budget({"wait_budget_secs": 42.5}, admission) == 42.5 - margin
        assert gw._negotiated_wait_budget({"wait_budget_secs": 600}, None) is None

    @pytest.mark.parametrize(
        "asked,ceiling",
        [
            # The shipped pair: the config default and the stub constant pinned
            # equal to it by ``test_stub_reconnect_budget.py`` -- i.e. the case
            # every default install runs.
            (
                stub_mod._SPAWN_QUEUE_WAIT_BUDGET_SECS,
                float(McpGatewayConfig().spawn_queue_wait_secs),
            ),
            (600.0, 100.0),  # an operator who lowered the key
            (30.0, 600.0),  # a stub that asked for less than the ceiling
            (4.0, 600.0),  # ...for less than the margin itself
            (0.05, 600.0),  # ...for less than a socket round trip
        ],
    )
    def test_the_daemon_gives_up_strictly_before_the_stub_does(
        self, asked: float, ceiling: float
    ) -> None:
        """The daemon's wait must END FIRST, at every budget, not tie.

        A tie is not a tie: the stub starts its timer before it writes
        ``ensure_backend`` and the daemon starts its own only after reading the
        frame, so at equal budgets the stub is always the one that gives up --
        and a stub whose budget expires runs ``fallback_exec``, one more process
        on the host the daemon was refusing one for, charged to nothing. The
        margin is what turns that into the ``capacity`` refusal that authorises
        no exec, so it is load-bearing for the same property
        ``_LEGACY_SPAWN_WAIT_SECS`` is: the answer has to ARRIVE in time.
        """
        armed = gw._negotiated_wait_budget(
            {"wait_budget_secs": asked}, _admission(spawn_queue_wait_secs=ceiling)
        )
        assert armed is not None
        assert armed < min(asked, ceiling), "the daemon must not wait as long as the stub"
        assert armed > 0, "...and must still spend a real wait in the queue"


#: Every acquire failure that means the HOST or the moment, with the
#: ``retry_after_secs`` hint each carries. Shared by the classifier table and by
#: the wire pin below, so a class added to one is measured on both.
#: The pre-`spawn_queue` stub's own pre-flight wait, read from that binary rather
#: than from this daemon: a refusal later than this reaches a stub that has already
#: exec'd, so it is the window the daemon's own wait has to fit inside.
_PRE_UPGRADE_STUB_PREFLIGHT_SECS = 25.0

_CAPACITY_FAILURES: list[tuple[str, BaseException, int]] = [
    ("pool full", PoolAtCapacity("full"), 30),
    ("host budget", hb.HostBudgetExhausted("procs", 1, 4, 4), 30),
    ("gate wait spent", adm.SpawnGateTimeout(3, 4, 600.0), 30),
    ("gate closed", adm.SpawnGateClosed("drain"), 5),
    ("breaker OPEN", BackendUnavailable("breaker OPEN"), 60),
    *[
        (f"pressure errno {name}", OSError(getattr(errno, name), name), 30)
        for name in ("ENOMEM", "EAGAIN", "EMFILE", "ENFILE", "ENOSPC")
    ],
]


class TestRejectionClasses:
    @pytest.mark.parametrize(
        "exc,cls,fallback",
        [
            (gw._TargetUnknown("no target mapping"), "compat", True),
            (OSError(2, "ENOENT"), "compat", True),
            *[(exc, "capacity", False) for _, exc, _ in _CAPACITY_FAILURES],
        ],
    )
    def test_each_failure_has_one_class(self, exc: BaseException, cls: str, fallback: bool) -> None:
        verdict = gw._classify_rejection(exc, exclusive=False)
        assert verdict is not None
        assert (verdict.cls, verdict.fallback) == (cls, fallback)
        if cls == "capacity":
            assert verdict.retry_after_secs is not None
            assert "retry_after_secs" in verdict.frame("r")
        else:
            assert "retry_after_secs" not in verdict.frame("r")

    @pytest.mark.parametrize("label,exc,retry", _CAPACITY_FAILURES)
    def test_a_capacity_verdict_authorises_no_exec_on_any_wire(
        self, label: str, exc: BaseException, retry: int
    ) -> None:
        """No stub shape earns a fallback from a capacity refusal.

        The classifier takes no "what did this stub negotiate" input at all, so
        there is no argument that turns a ``capacity`` verdict into an exec
        authorisation -- which is the point: the exec a pre-``spawn_queue`` stub
        would run is one the daemon cannot charge (it closes its socket first),
        so N concurrent refusals would leave N backends the host budget never
        sees. The retry hint is asserted beside it because the hint is what the
        refusal offers INSTEAD of the exec.
        """
        verdict = gw._classify_rejection(exc, exclusive=False)
        assert verdict is not None, label
        assert verdict.cls == "capacity", label
        assert verdict.fallback is False, label
        assert "fallback" not in verdict.frame("r"), label
        assert verdict.retry_after_secs == retry, label
        # Same answer for a connection-private target: exclusivity re-routes a
        # LAUNCH failure to ``isolation``, never a host-pressure one.
        private = gw._classify_rejection(exc, exclusive=True)
        assert private is not None and private.fallback is False, label

    def test_the_pressure_table_covers_every_errno_the_daemon_calls_pressure(self) -> None:
        """A pressure errno added to the daemon has to be measured above.

        Without this the table is a hand-kept list: a sixth errno routed to
        ``capacity`` would pass through untested, and the one thing that must
        hold for it -- no exec authorisation -- would be unasserted.
        """
        assert {
            errno.ENOMEM,
            errno.EAGAIN,
            errno.EMFILE,
            errno.ENFILE,
            errno.ENOSPC,
        } == set(gw._PRESSURE_ERRNOS)
        measured = {
            exc.errno for _, exc, _ in _CAPACITY_FAILURES if isinstance(exc, OSError) and exc.errno
        }
        assert measured == set(gw._PRESSURE_ERRNOS)

    def test_the_capacity_table_covers_every_exception_the_daemon_calls_capacity(self) -> None:
        """The EXCEPTION dimension is held equal too, not just the errno one.

        The errno set above is a module constant this table is pinned against; the
        exception family has to be the same kind of thing, or a sixth capacity
        exception reaches ``capacity`` with nothing asserting the one property that
        must hold for it -- that no ``capacity`` answer authorises an exec.
        """
        assert set(gw._CAPACITY_FAILURES) == {
            PoolAtCapacity,
            hb.HostBudgetExhausted,
            adm.SpawnGateTimeout,
            adm.SpawnGateClosed,
            BackendUnavailable,
        }
        measured = {type(exc) for _, exc, _ in _CAPACITY_FAILURES if not isinstance(exc, OSError)}
        assert measured == set(gw._CAPACITY_FAILURES)

    def test_the_legacy_gate_wait_stays_under_the_pre_upgrade_stubs_own_window(self) -> None:
        """The capacity guarantee holds only for a refusal that ARRIVES in time.

        A pre-upgrade stub gives up after its own 25 s pre-flight and execs on a
        path that reads no frame, so a later refusal is unaccounted however it is
        tagged. Keeping the daemon's legacy gate wait strictly under that window is
        what makes the untagged refusal reach a stub still listening; the constant
        is therefore load-bearing for a SECURITY property, not a tuning knob.

        It bounds the gate wait only: a refusal raised past the permit can still
        exceed the window, which is the residual `mcp.md` states rather than a hole
        this pin closes.
        """
        assert gw._LEGACY_SPAWN_WAIT_SECS < _PRE_UPGRADE_STUB_PREFLIGHT_SECS

    def test_a_private_target_the_daemon_cannot_launch_is_isolation(self) -> None:
        verdict = gw._classify_rejection(OSError(13, "EACCES"), exclusive=True)
        assert verdict is not None and (verdict.cls, verdict.fallback) == ("isolation", True)

    def test_an_internal_error_has_no_class(self) -> None:
        assert gw._classify_rejection(RuntimeError("bug"), exclusive=False) is None


class TestEnsureBackendWire:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("label,exc,retry", _CAPACITY_FAILURES)
    async def test_an_old_stub_is_refused_capacity_with_no_fallback(
        self, peer_ok, monkeypatch, label: str, exc: BaseException, retry: int
    ) -> None:
        """The legacy WIRE -- a bare ``ensure_backend`` -- earns no exec either.

        Driven through ``_handle_connection`` rather than the classifier because
        the frame is what a pre-upgrade stub reads, and the frame is what
        authorised the leak: on ``fallback: true`` that stub closes its socket
        and execs, so the daemon's charge is released before the process exists
        and nothing bounds the fan-out. A ``retry_after_secs`` with no
        ``fallback`` key is the whole contract on this wire.
        """
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=exc))
        writer = _FakeWriter()
        admission = _admission()
        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend"}),
            writer,
            _fake_pool(),
            admission,
        )
        frames = writer.frames()
        assert [f["type"] for f in frames] == ["registered", "rejected"], label
        assert "spawn_queue" in frames[0]["capabilities"], label
        assert frames[1]["class"] == "capacity", label
        assert "fallback" not in frames[1], label
        assert frames[1]["retry_after_secs"] == retry, label
        assert not any(f["type"] == "queued" for f in frames), label
        # Nothing was charged as a fallback either: a charge taken here and
        # released at the socket close is what made the leak invisible.
        assert admission.budget.snapshot()["by_kind"] == {}, label

    @pytest.mark.asyncio
    async def test_concurrent_old_stub_refusals_authorise_no_exec_at_all(
        self, peer_ok, monkeypatch
    ) -> None:
        """The fan-out shape: many legacy refusals at once, zero exec authorisations.

        One refusal being clean says nothing about the failure mode, which is a
        RATE -- each pre-upgrade stub that is told ``fallback: true`` execs a
        backend the budget released at its socket close, so the ceiling is never
        reached however many arrive. Eight simultaneous refusals against a
        budget of one must hand out no authorisation at all.
        """
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=PoolAtCapacity("full")))
        admission = _admission(max_procs=1)
        pool = _fake_pool()
        writers = [_FakeWriter() for _ in range(8)]
        await asyncio.gather(
            *[
                _handle(
                    _ScriptedReader(
                        _register_frame(stub_uuid=f"fanout-{i}"), {"type": "ensure_backend"}
                    ),
                    writer,
                    pool,
                    admission,
                )
                for i, writer in enumerate(writers)
            ]
        )
        rejections = [w.frames()[1] for w in writers]
        assert all(f["class"] == "capacity" for f in rejections)
        assert [f for f in rejections if f.get("fallback")] == []
        assert admission.budget.snapshot()["by_kind"] == {}

    @pytest.mark.asyncio
    async def test_an_old_stub_still_falls_back_for_a_target_shaped_refusal(
        self, peer_ok, monkeypatch
    ) -> None:
        """The complement, and the reason this is not a blanket refusal.

        ``compat`` is a property of the TARGET: this daemon has no mapping for
        it, the host is fine, and the stub's own exec is the topology the
        connection asked for. Refusing that too would strand every pre-upgrade
        session behind a daemon whose target map drifted -- a worse outage than
        the one the capacity rule prevents -- so the tag still rides it, and the
        exec it authorises is still charged to the budget.
        """
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=gw._TargetUnknown("no mapping"))
        )
        admission = _admission(max_procs=4)
        hangup = asyncio.Event()
        writer = _FakeWriter()
        task = asyncio.create_task(
            _handle(
                _ScriptedReader(_register_frame(), {"type": "ensure_backend"}, hangup),
                writer,
                _fake_pool(),
                admission,
            )
        )
        for _ in range(50):
            await asyncio.sleep(0.005)
            if len(writer.writes) >= 2:
                break
        frames = writer.frames()
        assert frames[1]["type"] == "rejected" and frames[1]["class"] == "compat"
        assert frames[1]["fallback"] is True
        assert "retry_after_secs" not in frames[1]
        assert admission.budget.snapshot()["by_kind"] == {"fallback": 1}
        hangup.set()
        await asyncio.wait_for(task, timeout=5)
        assert admission.budget.procs_in_use == 0

    @pytest.mark.asyncio
    async def test_a_new_stub_gets_a_classed_capacity_rejection_with_no_fallback(
        self, peer_ok, monkeypatch
    ) -> None:
        monkeypatch.setattr(gw, "_acquire_backend", AsyncMock(side_effect=PoolAtCapacity("full")))
        writer = _FakeWriter()
        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend", "wait_budget_secs": 600}),
            writer,
            _fake_pool(),
            _admission(),
        )
        frames = writer.frames()
        assert frames[1]["type"] == "rejected" and frames[1]["class"] == "capacity"
        assert "fallback" not in frames[1] and frames[1]["retry_after_secs"] > 0

    @pytest.mark.asyncio
    async def test_a_queued_new_stub_gets_keepalives_and_pings_are_answered(
        self, peer_ok, monkeypatch
    ) -> None:
        """The wire while a spawn waits: ``queued`` every tick, ``pong`` for every
        ping, a non-control frame parked for after ``ready``."""
        admission = _admission(capacity=1)
        blocker = await admission.gate.acquire(label="blocker")
        monkeypatch.setattr(adm, "QUEUED_KEEPALIVE_SECS", 0.01)
        backend = _fake_backend()
        original = gw._acquire_backend

        async def acquire(*args: Any, **kwargs: Any) -> Any:
            # A real gate wait with the keepalive callback the handler supplied,
            # then a fake backend in place of a fork.
            permit = await admission.gate.acquire(
                label="x",
                deadline=kwargs["wait_deadline"],
                on_queued=kwargs["on_queued"],
                keepalive_secs=0.01,
            )
            permit.release()
            return backend, True

        assert original is not None
        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        release_blocker = asyncio.Event()
        hangup = asyncio.Event()

        async def unblock() -> None:
            await asyncio.sleep(0.05)
            blocker.release()
            release_blocker.set()

        reader = _ScriptedReader(
            _register_frame(),
            {"type": "ensure_backend", "wait_budget_secs": 600},
            {"type": "ping"},
            {"type": "ping"},
            {"type": "unregister"},
            hangup,
        )
        writer = _FakeWriter()
        asyncio.create_task(unblock())

        async def finish() -> None:
            await release_blocker.wait()
            await asyncio.sleep(0.05)
            hangup.set()

        asyncio.create_task(finish())
        await asyncio.wait_for(_handle(reader, writer, _fake_pool(), admission), timeout=5)
        types = [f["type"] for f in writer.frames()]
        assert types[0] == "registered"
        assert types.count("pong") == 2, types
        assert "queued" in types, types
        assert types.index("queued") < types.index("ready"), "keepalives precede ready"
        queued = next(f for f in writer.frames() if f["type"] == "queued")
        assert queued["position"] == 1 and queued["capacity"] == 1
        # The parked ``unregister`` was processed after ``ready`` and ended the
        # connection cleanly rather than being dropped.
        assert types[-1] == "ready"

    @pytest.mark.parametrize("dimension", ["frames", "bytes"])
    @pytest.mark.asyncio
    async def test_a_flood_parked_during_a_spawn_wait_drops_that_one_conn(
        self, peer_ok, monkeypatch, dimension
    ) -> None:
        """Either aggregate bound ends the connection. ``pending`` is drained
        only after the acquire returns, so an unbounded park during a 600 s queue
        wait is the daemon's whole RSS -- and with it every co-pooled session."""
        size = 100
        if dimension == "frames":
            monkeypatch.setattr(gw, "_MAX_PENDING_FRAMES", 4)
            fits = gw._MAX_PENDING_FRAMES
        else:
            monkeypatch.setattr(gw, "_MAX_PENDING_BYTES", 250)
            fits = gw._MAX_PENDING_BYTES // size
        cancelled = False
        admitted = asyncio.Event()  # never set: the spawn stays queued

        async def acquire(*args: Any, **kwargs: Any) -> Any:
            nonlocal cancelled
            try:
                await admitted.wait()
            except asyncio.CancelledError:
                cancelled = True
                raise
            return _fake_backend(), True

        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        flood = [_padded_frame(size, i) for i in range(fits + 3)]
        reader = _ScriptedReader(
            _register_frame(),
            {"type": "ensure_backend", "wait_budget_secs": 600},
            {"type": "ping"},
            {"type": "ping"},
            *flood,
            asyncio.Event(),  # a hangup the test never sets
        )
        writer = _FakeWriter()
        await asyncio.wait_for(
            _handle(reader, writer, _fake_pool(), _admission(capacity=1)), timeout=5
        )
        # Dropped with no ``ready`` and no ``rejected``; the pings were answered
        # and never counted toward the bound.
        assert [f["type"] for f in writer.frames()] == ["registered", "pong", "pong"]
        assert cancelled, "the queued acquire was cancelled, not orphaned"
        # What fits, plus the one frame that overflowed -- parked before the bound
        # is read, so it is never silently dropped. The rest of the flood and the
        # hangup are still unread, so it was the bound that ended the connection
        # and not EOF.
        assert reader.remaining == len(flood) - (fits + 1) + 1

    @pytest.mark.asyncio
    async def test_a_burst_one_frame_under_the_bound_is_parked_whole(self, monkeypatch) -> None:
        """The under-bound companion: one frame short of the count bound is a
        LEGITIMATE burst, so every frame is still parked in arrival order for the
        main loop and the acquire's own result is what comes back."""
        monkeypatch.setattr(gw, "_MAX_PENDING_FRAMES", 8)
        burst = [_padded_frame(100, i) for i in range(gw._MAX_PENDING_FRAMES - 1)]
        pending: deque[bytes] = deque()
        reader = _ScriptedReader({"type": "ping"}, *burst, asyncio.Event())
        writer = _FakeWriter()
        admitted = asyncio.Event()

        async def acquire() -> str:
            await admitted.wait()
            return "acquired"

        async def admit_once_the_burst_is_parked() -> None:
            while reader.remaining > 1:  # only the hangup event left
                await asyncio.sleep(0)
            admitted.set()

        asyncio.create_task(admit_once_the_burst_is_parked())
        result = await asyncio.wait_for(
            gw._await_answering_pings(reader, writer, pending, acquire(), stub_uuid=_STUB),
            timeout=5,
        )
        assert result == "acquired"
        assert list(pending) == burst, "every parked frame survives, in arrival order"
        assert [f["type"] for f in writer.frames()] == ["pong"]

    @pytest.mark.asyncio
    async def test_a_fallback_is_charged_until_the_stub_hangs_up(
        self, peer_ok, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=gw._TargetUnknown("no target mapping"))
        )
        admission = _admission(max_procs=4)
        hangup = asyncio.Event()
        reader = _ScriptedReader(
            _register_frame(), {"type": "ensure_backend", "wait_budget_secs": 600}, hangup
        )
        writer = _FakeWriter()
        task = asyncio.create_task(_handle(reader, writer, _fake_pool(), admission))
        for _ in range(50):
            await asyncio.sleep(0.005)
            if len(writer.writes) >= 2:
                break
        frames = writer.frames()
        assert frames[1]["type"] == "rejected" and frames[1]["class"] == "compat"
        assert frames[1]["fallback"] is True
        assert admission.budget.snapshot()["by_kind"] == {"fallback": 1}, "the exec is charged"
        assert not task.done(), "the charge holds while the socket is open"
        hangup.set()
        await asyncio.wait_for(task, timeout=5)
        assert admission.budget.procs_in_use == 0

    @pytest.mark.asyncio
    async def test_a_fallback_the_budget_cannot_take_becomes_capacity(
        self, peer_ok, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            gw, "_acquire_backend", AsyncMock(side_effect=gw._TargetUnknown("no target mapping"))
        )
        admission = _admission(max_procs=1)
        admission.budget.reserve(label="occupant")
        writer = _FakeWriter()
        await _handle(
            _ScriptedReader(_register_frame(), {"type": "ensure_backend", "wait_budget_secs": 600}),
            writer,
            _fake_pool(),
            admission,
        )
        frames = writer.frames()
        assert frames[1]["class"] == "capacity" and "fallback" not in frames[1]

    @pytest.mark.asyncio
    async def test_stats_carry_the_admission_snapshot(self, peer_ok) -> None:
        writer = _FakeWriter()
        await _handle(
            _ScriptedReader({"type": "stats"}), writer, _fake_pool(), _admission(capacity=3)
        )
        (frame,) = writer.frames()
        assert frame["admission"]["spawn_gate"]["capacity"] == 3
        assert "host_budget" in frame["admission"]


class TestAcquireBackendOrder:
    """``_acquire_backend`` with a real pool: permit, then budget, then slot."""

    @pytest.mark.asyncio
    async def test_nothing_is_charged_while_a_spawn_waits_in_the_gate(self, monkeypatch) -> None:
        """A queued spawn holds NO host charge, and none is left after it fails.

        The gate is the only step that waits, so it goes first: a charge taken
        before it prices a process that does not exist for as long as the wait
        lasts (up to ``spawn_queue_wait_secs``, 600 s by default). Ten queued
        stubs then reach the ceiling with nothing running and the eleventh is
        refused ``capacity`` -- which deliberately authorises no fallback -- on an
        idle host. Measured with a real ``BackendPool`` and a real budget whose
        ceiling is exactly the number of waiters, so a charge held across the wait
        cannot pass this: the third acquire would raise ``HostBudgetExhausted``
        instead of queueing.
        """
        from kiro_crew.mcp_gateway.pool import BackendPool

        pool = BackendPool(max_backends=8)
        admission = _admission(capacity=1, max_procs=2)
        blocker = await admission.gate.acquire(label="blocker")
        waiters = [
            asyncio.create_task(
                gw._acquire_backend(
                    pool,
                    PoolKey.from_register(_register_frame(server_name=f"queued-{i}")),
                    _resolver,
                    admission=admission,
                    wait_deadline=time.monotonic() + 30.0,
                )
            )
            for i in range(2)
        ]
        for _ in range(100):
            await asyncio.sleep(0.005)
            if admission.gate.queued == 2:
                break
        assert admission.gate.queued == 2 and admission.gate.in_flight == 1
        snapshot = admission.budget.snapshot()
        assert (snapshot["procs"], snapshot["charges"], snapshot["by_kind"]) == (0, 0, {})
        # A third arrival meets the QUEUE, never a full host: the budget still
        # has both its units, so what it waits for is a spawn slot.
        with pytest.raises(adm.SpawnGateTimeout):
            await gw._acquire_backend(
                pool,
                PoolKey.from_register(_register_frame(server_name="third")),
                _resolver,
                admission=admission,
                wait_deadline=time.monotonic() + 0.02,
            )
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        assert admission.budget.procs_in_use == 0, "and a cancelled waiter leaves none behind"
        assert pool.resident_pending == 0
        blocker.release()

    @pytest.mark.asyncio
    async def test_the_charge_is_released_on_a_gate_failure(self, monkeypatch) -> None:
        from kiro_crew.mcp_gateway.pool import BackendPool

        pool = BackendPool(max_backends=4)
        admission = _admission(capacity=1, max_procs=4)
        blocker = await admission.gate.acquire(label="blocker")
        key = PoolKey.from_register(_register_frame())
        with pytest.raises(adm.SpawnGateTimeout):
            await gw._acquire_backend(
                pool, key, _resolver, admission=admission, wait_deadline=time.monotonic() + 0.02
            )
        assert admission.budget.procs_in_use == 0, "the charge is released with the gate failure"
        assert pool.resident_pending == 0
        assert admission.gate.queued == 0
        blocker.release()

    @pytest.mark.asyncio
    async def test_resident_capacity_is_refused_before_any_fork(self, monkeypatch) -> None:
        from kiro_crew.mcp_gateway.pool import BackendPool

        pool = BackendPool(max_backends=1)
        occupant = _fake_backend()
        await pool.add(PoolKey.from_register(_register_frame(server_name="other")), occupant)
        await occupant.attach_stub("s-occupant")  # attached: not evictable
        spawned = AsyncMock()
        monkeypatch.setattr(gw, "spawn_backend", spawned)
        admission = _admission(capacity=2, max_procs=4)
        with pytest.raises(PoolAtCapacity):
            await gw._acquire_backend(
                pool, PoolKey.from_register(_register_frame()), _resolver, admission=admission
            )
        spawned.assert_not_awaited()
        assert admission.gate.in_flight == 0 and admission.budget.procs_in_use == 0
        assert admission.gate.snapshot()["outcomes"]["neutral"] == 1

    @pytest.mark.asyncio
    async def test_prewarm_settles_neutral_at_once_and_a_stub_spawn_watches_initialize(
        self, monkeypatch
    ) -> None:
        from kiro_crew.mcp_gateway.pool import BackendPool

        pool = BackendPool(max_backends=4)
        admission = _admission(capacity=2, max_procs=4)
        backend = _fake_backend()
        exited = asyncio.Event()
        backend.process.wait = exited.wait

        async def fake_spawn(**kwargs: Any) -> Backend:
            assert kwargs["initialize_timeout_secs"] == admission.initialize_timeout_secs
            return backend

        monkeypatch.setattr(gw, "spawn_backend", fake_spawn)
        monkeypatch.setattr(gw, "_declared_env_to_forward", lambda k: {})
        monkeypatch.setattr(gw, "resolve_secret_uris", lambda env, home: (env, []))
        key = PoolKey.from_register(_register_frame())
        got, was_spawned = await gw._acquire_backend(
            pool, key, _resolver, admission=admission, prewarm=True
        )
        assert got is backend and was_spawned
        await _settle()
        assert admission.gate.in_flight == 0, "prewarm releases its permit at once"
        assert admission.gate.snapshot()["outcomes"]["neutral"] == 1
        assert admission.budget.procs_in_use == 1, "but the process is charged until reaped"
        exited.set()
        await _settle()
        assert admission.budget.procs_in_use == 0

        # A stub spawn on another key holds its permit until initialize resolves.
        second = _fake_backend()
        second.process.wait = asyncio.Event().wait

        async def fake_spawn2(**kwargs: Any) -> Backend:
            return second

        monkeypatch.setattr(gw, "spawn_backend", fake_spawn2)
        await gw._acquire_backend(
            pool,
            PoolKey.from_register(_register_frame(server_name="two")),
            _resolver,
            admission=admission,
        )
        await _settle()
        assert admission.gate.in_flight == 1, "held through the initialize window"
        second._init_state = "ready"
        second._init_done_event.set()
        await _settle()
        assert admission.gate.in_flight == 0
        assert admission.gate.snapshot()["outcomes"]["success"] == 1
        await admission.close()


class TestSetSpawnCapacityFrame:
    """The gate's capacity seam over the wire: every value earns an ANSWER."""

    @pytest.mark.parametrize("capacity", [float("inf"), float("-inf"), float("nan")])
    def test_a_non_finite_capacity_is_refused_not_raised(self, capacity: float) -> None:
        """``int(inf)`` raises, and a raise here costs the REPLY, not the gate.

        An exception escaping ``_apply_set_spawn_capacity`` propagates out of the
        control short-circuit, which drops the connection with NO reply of either
        kind -- so the controller keeps the value PENDING and retries it against a
        daemon that answers nothing, instead of being told no once. And ``inf`` is
        not an exotic input: it is what ``json.loads`` returns for a well-formed
        ``1e400``, asserted below so the premise is measured, not claimed.
        """
        assert json.loads('{"capacity": 1e400}')["capacity"] == float("inf")
        admission = _admission(capacity=2)
        reply = gw._apply_set_spawn_capacity(
            {"type": "set-spawn-capacity", "capacity": capacity}, admission
        )
        assert reply["type"] == "spawn-capacity-rejected"
        assert admission.gate.capacity == 2, "and the live capacity did not move"

    def test_a_usable_capacity_still_moves_the_gate(self) -> None:
        admission = _admission(capacity=2)
        reply = gw._apply_set_spawn_capacity({"capacity": 3}, admission)
        assert (reply["type"], reply["capacity"]) == ("spawn-capacity", 3)


@pytest.mark.xdist_group("mcp_gateway")
class TestTheRefusalArrivesBeforeTheStubGivesUp:
    """Real endpoint, real ``_handle_connection``, real ``stub`` pre-flight.

    The two sides' budgets are the shipped shape -- one number, used by both --
    and the defect this pins is pure timing, so neither side can be a double: a
    scripted reader has no clock skew, and the stub's own timer starts before the
    frame it is negotiating with has been written.
    """

    @pytest.mark.asyncio
    async def test_a_queue_aware_stub_receives_the_capacity_refusal_not_a_timeout(
        self, peer_ok, monkeypatch, short_sock_dir
    ) -> None:
        # One budget for both sides, exactly as the shipped defaults are one
        # number on each side (600 s); scaled down so the test costs its wait.
        budget_secs = 1.2
        admission = _admission(capacity=1, spawn_queue_wait_secs=budget_secs)
        blocker = await admission.gate.acquire(label="blocker")

        async def acquire(*args: Any, **kwargs: Any) -> Any:
            # A real wait on the deadline the handler computed, against a gate
            # that is full: the only thing under test is which side ends first.
            permit = await admission.gate.acquire(
                label="x", deadline=kwargs["wait_deadline"], on_queued=kwargs["on_queued"]
            )
            permit.release()
            raise AssertionError("unreachable: the gate is blocked")

        monkeypatch.setattr(gw, "_acquire_backend", acquire)
        sock = Path(short_sock_dir) / "gw-budget.sock"
        pool = _fake_pool()

        async def handler(reader: Any, writer: Any) -> None:
            await gw._handle_connection(
                reader, writer, pool, _resolver, sock, None, admission=admission
            )

        transport.prepare_dir(sock)
        server = await transport.serve(sock, handler, limit=1 << 16)
        try:
            reader, writer = await transport.connect(sock, limit=1 << 16)
            writer.write(json.dumps(_register_frame()).encode("utf-8") + b"\n")
            await writer.drain()
            registered = json.loads((await asyncio.wait_for(reader.readuntil(b"\n"), 10)).decode())
            assert registered["type"] == "registered"
            assert "spawn_queue" in registered["capabilities"]
            outcome, frame = await asyncio.wait_for(
                stub_mod._ensure_backend_admitted(
                    reader,
                    writer,
                    queue_aware=True,
                    total_budget_secs=budget_secs,
                    # Long enough that only the BUDGETS decide this, never the
                    # silence timer the keepalives renew.
                    silence_secs=30.0,
                ),
                timeout=budget_secs + 10,
            )
            assert outcome != stub_mod._ADMIT_TIMEOUT, (
                "the stub gave up first, so it runs fallback_exec -- the exec a "
                "capacity refusal exists to withhold, charged to nothing"
            )
            assert outcome == stub_mod._ADMIT_REJECTED and frame is not None
            assert frame["class"] == gw.REJECT_CLASS_CAPACITY
            assert "fallback" not in frame and frame["retry_after_secs"] > 0
            writer.close()
        finally:
            server.close()
            blocker.release()
            await admission.close()


@_POSIX_ONLY
@pytest.mark.xdist_group("mcp_gateway")
class TestPrewarmYieldsToALiveStub:
    """A real ``run_gatewayd`` prewarm pass, because the yield lives at its call site.

    ``_acquire_backend`` cannot express this on its own: what the pass owes a
    live session is a per-key decision and a bounded wait, and both are arguments
    the pass passes IN. Driven the way ``test_gatewayd_more_coverage.py`` drives
    prewarming -- seeded hot keys, a real daemon, ``_acquire_backend`` recording
    what it was handed.
    """

    @pytest.mark.asyncio
    async def test_a_stub_queueing_mid_pass_stands_the_rest_of_it_down(
        self, short_sock_dir, monkeypatch, caplog
    ) -> None:
        """The stated priority is "below every stub", which a once-per-pass check
        cannot deliver: a pass takes as long as its spawns, and the gate admits in
        strict arrival order, so a prewarm that enqueues after the stub arrived is
        served BEFORE it. The check is therefore per key, and the wait it does
        take is bounded -- an unbounded one parks under ``_prewarm_lock``, which
        the credential-rotation re-warm has to take.
        """
        socket_path = Path(short_sock_dir) / "gw-prewarm.sock"
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        (Path(short_sock_dir) / "hot-keys.json").write_text(
            json.dumps(
                {
                    "keys": [
                        {"register": _register_frame(server_name=s), "hits": h, "last_seen": now}
                        for s, h in (("hot-one", 9), ("hot-two", 4))
                    ],
                    "totals": {"hits": 3, "misses": 1},
                }
            ),
            encoding="utf-8",
        )
        warmed: list[str] = []
        deadlines: list[Optional[float]] = []
        held: list[adm.Permit] = []
        queued: list[asyncio.Task[adm.Permit]] = []

        async def fake_acquire(pool: Any, pool_key: PoolKey, resolver: Any, **kwargs: Any) -> Any:
            warmed.append(pool_key.server_name)
            deadlines.append(kwargs.get("wait_deadline"))
            admission: adm.Admission = kwargs["admission"]
            if len(warmed) == 1:
                # A live stub arrives WHILE the first key is being warmed: it
                # takes the gate's only slot and one more queues behind it.
                held.append(await admission.gate.acquire(label="live-stub"))
                queued.append(asyncio.create_task(admission.gate.acquire(label="live-stub-2")))
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if admission.gate.queued == 1:
                        break
                assert admission.gate.queued == 1
            return _fake_backend(), True

        monkeypatch.setattr(gw, "_acquire_backend", fake_acquire)
        monkeypatch.setattr(gw, "_audit_prewarm_spawn", lambda label: None)
        caplog.set_level("INFO", logger="kiro_crew.mcp_gateway.prewarm")
        stop_event = asyncio.Event()
        daemon = asyncio.create_task(
            gw.run_gatewayd(
                socket_path,
                max_backends=8,
                idle_timeout_secs=300,
                stop_event=stop_event,
                target_resolver=_resolver,
                prewarm_count=2,
                spawn_concurrency=1,
            )
        )
        try:
            totals: list[str] = []
            for _ in range(600):
                await asyncio.sleep(0.01)
                totals = [
                    m.group(1)
                    for m in (
                        re.search(r"prewarm: warmed (\d+)/2 backend", r.getMessage())
                        for r in caplog.records
                    )
                    if m
                ]
                if totals:
                    break
            assert totals == ["1"], f"the pass warmed {totals} of 2 keys, warmed={warmed}"
            assert warmed == ["hot-one"], "the second key stood down for the queued stub"
            assert deadlines and deadlines[0] is not None, "a prewarm wait must be bounded"
            # `(now + cap) - now'` in floats overshoots the cap by ulps when both
            # readings fall in one coarse-clock tick; the bound is to float slack.
            assert deadlines[0] - time.monotonic() <= gw._PREWARM_SPAWN_WAIT_SECS + 1e-6
        finally:
            stop_event.set()
            await asyncio.wait_for(daemon, timeout=15)
            for permit in held:
                permit.release()
            for task in queued:
                task.cancel()
            await asyncio.gather(*queued, return_exceptions=True)
