"""Mochi ``QueuePoller``'s control surface: poll serialization, plan backoff, spawn wait.

The poller runs once a second over ``mochi-queue.json``. These pin the parts that
decide whether work runs once, twice or never:

* ``poll`` is serialized, so a call landing mid-poll queues exactly one follow-up, an
  error never kills the loop, and a stopped poller does nothing.
* A missing queue triggers a plan. The plan lock, its lazy expiry (counted as a
  failure that consumes the poll), the exponential retry backoff, the first-launch
  grace, and the two resets (``clear_plan_spawn_lock``, ``on_user_chat_success``)
  decide when the next plan may fire.
* The serial spawn wait ends early only for the tracked subagent id, or when the
  ``tick``-driven timeout fires. ``stop`` drops the timeout.
* The prompt builders' fallbacks and optional fields.

The clock is injected, ``qf.read_queue`` is replaced, and the queue path sits under
``tmp_path`` so any lock sidecar the poller opens stays there.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi import queue_poller as qp
from kiro_crew.apps.builtins.mochi.queue_poller import (
    PLAN_BACKOFF_BASE_MS,
    PLAN_SPAWN_TIMEOUT_MS,
    SPAWN_TIMEOUT_MS,
    QueuePoller,
    build_agent_prompt,
    build_missed_notify_prompt,
    build_watch_check_prompt,
)
from kiro_crew.testing.wait import await_until


class _Clock:
    def __init__(self, now: int = 1_000_000) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


class _Callbacks:
    def __init__(self) -> None:
        self.plans = 0
        self.during_plan: Any = None

    async def trigger_plan(self) -> None:
        self.plans += 1
        if self.during_plan is not None:
            await self.during_plan()


@pytest.fixture()
def queue() -> list[dict]:
    """Queue documents the next reads return, one per read; empty means missing."""
    return []


@pytest.fixture()
def reads(monkeypatch: pytest.MonkeyPatch, queue: list[dict]) -> list[str]:
    """Every queue read. With nothing scripted the queue is missing, so a poll may plan."""
    seen: list[str] = []

    def _read(path: str) -> dict | None:
        seen.append(path)
        return queue.pop(0) if queue else None

    monkeypatch.setattr(qf, "read_queue", _read)
    return seen


@pytest.fixture()
def make_poller(tmp_path: Path) -> Callable[..., QueuePoller]:
    """Pollers whose queue path (and so its ``.lock`` sidecar) sits under ``tmp_path``."""
    path = str(tmp_path / "mochi-queue.json")

    def _make(
        clock: _Clock, callbacks: _Callbacks | None = None, *, start: bool = True
    ) -> QueuePoller:
        p = QueuePoller(path, callbacks or _Callbacks(), clock=clock)
        if start:
            p.start()
        return p

    return _make


class TestPollGate:
    @pytest.mark.asyncio
    async def test_a_stopped_poller_reads_nothing(self, make_poller, reads) -> None:
        p = make_poller(_Clock(), start=False)  # never started
        await p.poll()
        assert reads == []
        assert p.execute_next() is None

    @pytest.mark.asyncio
    async def test_execute_next_polls_once_started(self, make_poller, reads) -> None:
        cb = _Callbacks()
        p = make_poller(_Clock(), cb)
        pending = p.execute_next()
        assert pending is not None
        await pending
        assert (len(reads), cb.plans) == (1, 1)

    @pytest.mark.asyncio
    async def test_stop_turns_execute_next_off_again(self, make_poller, reads) -> None:
        p = make_poller(_Clock())
        p.stop()
        assert p.execute_next() is None
        assert reads == []

    @pytest.mark.asyncio
    async def test_calls_landing_mid_poll_queue_exactly_one_follow_up(
        self, make_poller, reads
    ) -> None:
        cb = _Callbacks()
        p = make_poller(_Clock(), cb)

        async def _reenter() -> None:
            # Three calls while the first poll is still running collapse into one.
            await p.poll()
            await p.poll()
            await p.poll()

        cb.during_plan = _reenter
        await p.poll()
        assert len(reads) == 2

    @pytest.mark.asyncio
    async def test_a_poll_error_is_logged_and_the_next_poll_still_runs(
        self, make_poller, monkeypatch, caplog
    ) -> None:
        calls: list[int] = []

        def _boom(path: str) -> None:
            calls.append(1)
            raise OSError("disk gone")

        monkeypatch.setattr(qf, "read_queue", _boom)
        caplog.set_level(logging.ERROR, logger=qp.logger.name)
        p = make_poller(_Clock())
        await p.poll()
        await p.poll()
        assert len(calls) == 2  # the first error did not leave the poller stuck
        assert [r.getMessage() for r in caplog.records] == ["[QueuePoller] poll error"] * 2


class TestPlanTrigger:
    @pytest.mark.asyncio
    async def test_a_missing_queue_triggers_one_plan_while_the_lock_holds(
        self, make_poller, reads
    ) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        await p.poll()
        clock.now += PLAN_SPAWN_TIMEOUT_MS - 1
        await p.poll()
        assert cb.plans == 1

    @pytest.mark.asyncio
    async def test_an_expired_plan_lock_backs_off_exponentially(
        self, make_poller, reads, queue
    ) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        await p.poll()  # no queue yet: plan 1
        clock.now += PLAN_SPAWN_TIMEOUT_MS
        queue.append({"tasks": []})  # a queue, but no fresh plan landed
        await p.poll()  # lock expiry: counted as a failure, consumes the poll
        clock.now += PLAN_BACKOFF_BASE_MS - 1
        await p.poll()
        assert cb.plans == 1  # still inside the 1-minute backoff
        clock.now += 1
        await p.poll()
        assert cb.plans == 2
        clock.now += PLAN_SPAWN_TIMEOUT_MS
        queue.append({"tasks": []})
        await p.poll()  # second failure: the backoff doubles
        clock.now += 2 * PLAN_BACKOFF_BASE_MS - 1
        await p.poll()
        assert cb.plans == 2
        clock.now += 1
        await p.poll()
        assert cb.plans == 3

    @pytest.mark.asyncio
    async def test_plans_stop_after_the_failure_cap(self, make_poller, reads, queue) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        for _ in range(qp.MAX_PLAN_FAILS):
            await p.poll()
            clock.now += PLAN_SPAWN_TIMEOUT_MS
            queue.append({"tasks": []})
            await p.poll()
            clock.now += qp._PLAN_BACKOFF_CAP_MS
        assert cb.plans == qp.MAX_PLAN_FAILS
        await p.poll()
        assert cb.plans == qp.MAX_PLAN_FAILS

    @pytest.mark.asyncio
    async def test_a_fresh_plan_releases_the_lock_without_a_failure(
        self, make_poller, reads, queue
    ) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        await p.poll()
        # Agents write second-precision timestamps, so up to 1s before the spawn counts.
        stamp = datetime.fromtimestamp((clock.now - 999) / 1000, tz=timezone.utc)
        queue.append({"tasks": [], "planned_at": stamp.isoformat().replace("+00:00", "Z")})
        await p.poll()  # releases the lock
        clock.now += PLAN_SPAWN_TIMEOUT_MS  # long after: no expiry is counted
        await p.poll()  # queue missing again: a plan may fire at once, no backoff
        assert cb.plans == 2

    @pytest.mark.asyncio
    async def test_a_user_chat_success_lifts_the_backoff(self, make_poller, reads, queue) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        await p.poll()
        clock.now += PLAN_SPAWN_TIMEOUT_MS
        queue.append({"tasks": []})
        await p.poll()  # failure: the retry is a minute away
        p.on_user_chat_success()
        await p.poll()
        assert cb.plans == 2

    @pytest.mark.asyncio
    async def test_clearing_the_plan_lock_allows_an_immediate_plan(
        self, make_poller, reads
    ) -> None:
        cb = _Callbacks()
        p = make_poller(_Clock(), cb)
        await p.poll()
        p.clear_plan_spawn_lock()  # e.g. the plan agent's chat_done
        await p.poll()
        assert cb.plans == 2

    @pytest.mark.asyncio
    async def test_first_launch_grace_holds_plans_until_it_expires(
        self, make_poller, reads
    ) -> None:
        clock = _Clock()
        cb = _Callbacks()
        p = make_poller(clock, cb)
        p.enable_first_launch_grace(5_000)
        await p.poll()
        p.tick(clock.now + 4_999)
        await p.poll()
        assert cb.plans == 0
        p.tick(clock.now + 5_000)
        await p.poll()
        assert cb.plans == 1

    @pytest.mark.asyncio
    async def test_ending_the_grace_early_allows_a_plan(self, make_poller, reads) -> None:
        cb = _Callbacks()
        p = make_poller(_Clock(), cb)
        p.enable_first_launch_grace()
        p.end_first_launch_grace()
        await p.poll()
        assert cb.plans == 1


class TestSpawnWait:
    @staticmethod
    async def _waiting(p: QueuePoller, spawn_id: str | None) -> asyncio.Task[bool]:
        task = asyncio.ensure_future(p._await_spawn())
        await asyncio.sleep(0)  # let the wait arm its future and deadline
        p._current_spawn_id = spawn_id
        return task

    @pytest.mark.asyncio
    async def test_only_the_tracked_agent_ends_the_wait(self, make_poller) -> None:
        p = make_poller(_Clock())
        task = await self._waiting(p, "sub-1")
        p.notify_agent_done("sub-other")
        await asyncio.sleep(0)
        assert not task.done()
        p.notify_agent_done("sub-1")
        assert await await_until(task, what="the spawn wait") is False  # ended early, not timed out

    @pytest.mark.asyncio
    async def test_a_done_with_no_id_ends_the_wait(self, make_poller) -> None:
        p = make_poller(_Clock())
        task = await self._waiting(p, "sub-1")
        p.notify_agent_done()
        assert await await_until(task, what="the spawn wait") is False

    @pytest.mark.asyncio
    async def test_tick_fires_the_timeout_at_the_deadline(self, make_poller) -> None:
        clock = _Clock()
        p = make_poller(clock)
        task = await self._waiting(p, "sub-1")
        p.tick(clock.now + SPAWN_TIMEOUT_MS - 1)
        await asyncio.sleep(0)
        assert not task.done()
        p.tick(clock.now + SPAWN_TIMEOUT_MS)
        assert await await_until(task, what="the spawn wait") is True

    @pytest.mark.asyncio
    async def test_stop_drops_the_timeout_but_a_done_still_ends_the_wait(self, make_poller) -> None:
        clock = _Clock()
        p = make_poller(clock)
        task = await self._waiting(p, None)
        p.stop()
        p.tick(clock.now + 10 * SPAWN_TIMEOUT_MS)
        await asyncio.sleep(0)
        assert not task.done()
        p.notify_agent_done("anything")  # nothing tracked, so any id ends it
        assert await await_until(task, what="the spawn wait") is False

    def test_a_done_with_no_wait_pending_is_a_no_op(self, make_poller) -> None:
        p = make_poller(_Clock())
        p.notify_agent_done("sub-1")
        p.tick(10**12)


class TestPrompts:
    def test_an_agent_prompt_is_the_planners_own_or_a_fallback(self) -> None:
        assert build_agent_prompt({"id": "t1", "action": {"prompt": "do it"}}) == "do it"
        assert build_agent_prompt({"id": "t1", "action": {}}) == "Execute freestyle task: t1"
        assert build_agent_prompt({"id": "t2"}) == "Execute freestyle task: t2"

    def test_a_watch_prompt_lists_only_the_fields_each_item_has(self) -> None:
        prompt = build_watch_check_prompt(
            [
                {
                    "id": "w1",
                    "label": "Build",
                    "kind": "ci",
                    "target": "repo/x",
                    "triggerCondition": "red",
                    "lastResult": "green",
                    "priority": "high",
                    "checkCount": 3,
                },
                {"id": "w2", "label": "Inbox", "kind": "mail", "priority": "normal"},
            ]
        )
        assert (
            "- Build (kind: ci, id: w1)\n  target: repo/x\n  triggerCondition: red\n"
            "  lastResult: green\n  priority: high\n  checkCount: 3\n"
            "- Inbox (kind: mail, id: w2)\n"
        ) in prompt
        assert "priority: normal" not in prompt
        assert "FORBIDDEN tools" in prompt

    def test_a_missed_notify_prompt_falls_back_to_status_changed(self) -> None:
        prompt = build_missed_notify_prompt(
            [
                {"id": "w1", "label": "Build", "lastResult": "went red"},
                {"id": "w2", "label": "Mail"},
            ]
        )
        assert "- Build (id: w1): went red\n- Mail (id: w2): status changed\n" in prompt
        assert "Do NOT load any skills" in prompt
        assert "FORBIDDEN tools" in prompt
