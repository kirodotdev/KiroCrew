"""``mochi/queue_poller.py``: the ``_do_poll`` steps the control and backoff suites leave open.

Due tasks, watch spawns with their storm breaker and budget, plan triggering, the merged
write-back and spawn timeouts. The queue is a real file under ``tmp_path`` and the clock is
injected. A spawned agent finishes (or times out) from a callback scheduled on the loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from kiro_crew.apps.builtins.mochi import queue_file as qf
from kiro_crew.apps.builtins.mochi import queue_poller as qp
from kiro_crew.apps.builtins.mochi.activity_budget import ActivityBudget
from kiro_crew.apps.builtins.mochi.queue_poller import MAX_FAIL_COUNT, SPAWN_TIMEOUT_MS, QueuePoller
from kiro_crew.apps.spawn_sdk import SpawnError
from kiro_crew.testing.wait import finished

pytestmark = pytest.mark.asyncio

NOW = 1_800_000_000_000  # epoch milliseconds, on a whole second
FAR = "2999-01-01T00:00:00.000Z"  # a planned_until that keeps route_poll on "execute"
COOLDOWN_MS = 30_000  # the watch lock a spawn that landed leaves behind


class _Rig:
    """A started poller over a real queue file. It is also the poller's clock and callbacks."""

    def __init__(self, path: Path, **poller_kwargs: Any) -> None:
        self.path, self.now = path, NOW
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (kind, merged action) per task run
        self.plans: list[str] = []
        self.prompts: list[str] = []  # every spawn_agent prompt
        self.events: list[str] = []  # the spawn start/end hooks
        self.fail: set[str] = set()  # ids of tasks whose callback raises
        self.during: Any = None  # called inside each task callback
        self.trigger_error: Exception | None = None
        self.spawn_error: Exception | None = None
        self.timeout = False  # True: the spawned agent never reports back
        # The watch members are absent as far as the poller can tell until a test sets them.
        self.get_due_watch_items = self.get_unnotified_watch_items = None
        self.on_budget_exhausted = None
        self.on_move = lambda action: self._run_task("move", action)
        self.on_notify = lambda action: self._run_task("notify", action)
        self.on_mood = lambda action: self._run_task("mood", action)
        self.trigger_plan = lambda: self._trigger("plan")
        self.trigger_replan = lambda: self._trigger("replan")
        self.on_agent_spawn_start = lambda: self.events.append("start")
        self.on_agent_spawn_end = lambda: self.events.append("end")
        self.poller = QueuePoller(str(path), self, clock=lambda: self.now, **poller_kwargs)
        self.poller.start()

    async def poll(self) -> None:
        # A poll waits on the spawn future until the stub's callback resolves it, so a missed
        # handshake must fail by name here instead of hanging the worker.
        await finished(self.poller.poll(), "the poll")

    async def _run_task(self, kind: str, action: dict[str, Any]) -> None:
        if self.during is not None:
            self.during()
        if action["id"] in self.fail:
            raise RuntimeError(f"{kind} failed")
        self.calls.append((kind, action))

    async def _trigger(self, kind: str) -> None:
        self.plans.append(kind)
        if self.trigger_error is not None:
            raise self.trigger_error

    async def spawn_agent(self, task_prompt: str) -> str:
        self.prompts.append(task_prompt)
        if self.spawn_error is not None:
            raise self.spawn_error
        # The poller waits as soon as this returns; the agent reports back (or the spawn
        # deadline passes) on the loop's next turn.
        loop = asyncio.get_running_loop()
        if self.timeout:
            loop.call_soon(self.poller.tick, self.now + SPAWN_TIMEOUT_MS)
        else:
            loop.call_soon(self.poller.notify_agent_done, "agent-1")
        return "agent-1"

    def write(self, *tasks: dict[str, Any], **top: Any) -> None:
        queue = {"planned_at": qf._iso(NOW - 3_600_000), "planned_until": FAR, "tasks": list(tasks)}
        self.path.write_text(json.dumps(queue | top), encoding="utf-8")

    def read(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def task(self, task_id: str) -> dict[str, Any]:
        return next(t for t in self.read()["tasks"] if t["id"] == task_id)

    def ran(self) -> list[tuple[str, str]]:
        return [(kind, action["id"]) for kind, action in self.calls]


def _task(task_id: str, kind: str, **extra: Any) -> dict[str, Any]:
    due = qf._iso(NOW - 1_000)
    return {"id": task_id, "type": kind, "execute_after": due, "done": False, **extra}


def _items(count: int) -> list[dict[str, Any]]:
    return [{"id": f"w{i}", "label": f"item-{i}", "kind": "web"} for i in range(count)]


@pytest.fixture()
def rig(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> _Rig:
    caplog.set_level(logging.WARNING, logger=qp.logger.name)
    return _Rig(tmp_path / qf.QUEUE_FILE)


async def test_due_tasks_run_with_merged_actions_and_are_stamped_done(rig: _Rig, caplog) -> None:
    note = _task("n1", "notify", summary="task", tone="soft")  # task < action < payload
    note.update(action={"summary": "act", "sticky": True}, payload={"summary": "pay"})
    rig.write(
        note,
        _task("m1", "mood"),
        _task("bad", "mood"),
        _task("mv1", "move"),
        _task("mv2", "move"),
        _task("later", "notify", execute_after=qf._iso(NOW + 5_000)),
    )
    rig.fail = {"bad"}
    await rig.poll()
    assert rig.ran() == [("notify", "n1"), ("mood", "m1"), ("move", "mv1")]
    merged = rig.calls[0][1]
    assert (merged["summary"], merged["sticky"], merged["tone"]) == ("pay", True, "soft")
    assert "failed to execute deterministic task bad" in caplog.text  # and the poll went on
    assert [t["id"] for t in rig.read()["tasks"] if not t["done"]] == ["bad", "mv2", "later"]
    assert {rig.task(i)["executed_at"] for i in ("n1", "m1", "mv1")} == {qf._iso(NOW)}
    await rig.poll()  # one move per poll: the second one goes next time
    assert rig.ran()[-1] == ("move", "mv2")


async def test_planned_move_and_mood_tasks_past_the_stale_limit_are_skipped(rig: _Rig) -> None:
    old = qf._iso(NOW - qp.STALE_THRESHOLD_MS - 1_000)
    rig.write(
        _task("stale-move", "move", execute_after=old),
        _task("stale-mood", "mood", execute_after=old),
        _task("urgent-move", "move", execute_after=old, urgent=True),
        _task("old-notify", "notify", execute_after=old),
        _task("edge-mood", "mood", execute_after=qf._iso(NOW - qp.STALE_THRESHOLD_MS)),
    )
    await rig.poll()
    assert rig.ran() == [("move", "urgent-move"), ("notify", "old-notify"), ("mood", "edge-mood")]
    for task_id in ("stale-move", "stale-mood"):  # marked done without running
        stale = rig.task(task_id)
        assert (stale["done"], stale["executed_at"]) == (True, qf._iso(NOW))


@pytest.mark.parametrize(
    ("top", "kind"),
    [({"planned_until": qf._iso(NOW - 1_000)}, "plan"), ({"needs_replan": True}, "replan")],
)
async def test_an_expired_or_flagged_plan_saves_done_tasks_and_plans(rig, top, kind) -> None:
    rig.write(_task("n1", "notify"), **top)
    rig.trigger_error = RuntimeError("gateway down")  # a failed trigger is swallowed
    await rig.poll()
    await rig.poll()  # the failed attempt still holds the plan lock
    assert (rig.ran(), rig.plans) == ([("notify", "n1")], [kind])
    assert rig.task("n1")["done"] is True  # saved before the plan agent starts


async def test_due_watch_items_spawn_one_capped_batch_then_cool_down(rig: _Rig) -> None:
    rig.write()
    rig.get_due_watch_items = lambda: _items(qp.MAX_WATCH_BATCH + 2)
    await rig.poll()
    (prompt,) = rig.prompts
    assert f"item-{qp.MAX_WATCH_BATCH - 1} (kind: web" in prompt
    assert f"item-{qp.MAX_WATCH_BATCH}" not in prompt
    rig.now += COOLDOWN_MS - 1  # a spawn that landed leaves a short lock, not the full one
    await rig.poll()
    assert len(rig.prompts) == 1
    rig.now += 1
    await rig.poll()
    assert (len(rig.prompts), rig.events) == (2, ["start", "end", "start", "end"])


async def test_storm_breaker_caps_watch_spawns_until_the_window_rolls(rig: _Rig, caplog) -> None:
    rig.write()
    rig.get_due_watch_items = lambda: _items(1)
    for _ in range(qp.MAX_WATCH_SPAWNS_PER_WINDOW):
        await rig.poll()
        rig.now += COOLDOWN_MS
    await rig.poll()  # the cooldown has lapsed, but the window is spent
    assert "storm breaker: 5 spawns in 10min window" in caplog.text
    rig.now += SPAWN_TIMEOUT_MS - 1  # throttling took the full lock
    await rig.poll()
    assert len(rig.prompts) == qp.MAX_WATCH_SPAWNS_PER_WINDOW
    rig.now = NOW + qp.WATCH_STORM_WINDOW_MS + 1  # a new window counts from zero
    await rig.poll()
    assert len(rig.prompts) == qp.MAX_WATCH_SPAWNS_PER_WINDOW + 1


async def test_a_budget_caps_the_batch_and_hourly_spawns_and_reports_once(tmp_path: Path) -> None:
    budget = ActivityBudget("t", max_spawns_per_hour=1, watch_min_interval_ms=0, max_watch_batch=2)
    rig = _Rig(tmp_path / qf.QUEUE_FILE, budget_provider=lambda: budget)
    rig.write()
    exhausted: list[int] = []
    rig.get_due_watch_items = lambda: _items(4)
    rig.on_budget_exhausted = exhausted.append
    await rig.poll()
    (prompt,) = rig.prompts
    assert "item-1 (kind: web" in prompt and "item-2" not in prompt  # batch capped at 2
    rig.now += COOLDOWN_MS
    await rig.poll()  # the one spawn this hour is used up
    rig.now += 1_000
    await rig.poll()  # the throttle holds the full lock, so it reports once per window
    assert (len(rig.prompts), exhausted) == (1, [NOW + 3_600_000])


async def test_a_failed_watch_spawn_keeps_the_full_lock(rig: _Rig, caplog) -> None:
    rig.write()
    rig.get_due_watch_items = lambda: _items(1)
    rig.spawn_error = RuntimeError("gateway down")
    await rig.poll()
    assert rig.events == ["start", "end"]  # the spawn window closes even on failure
    assert "watch check spawnAgent failed" in caplog.text
    assert "[QueuePoller] watch check failed" in caplog.text
    rig.now += SPAWN_TIMEOUT_MS - 1  # a spawn that landed would be free again after 30 s
    await rig.poll()
    assert len(rig.prompts) == 1
    rig.now += 1
    await rig.poll()
    assert len(rig.prompts) == 2


async def test_missed_notifications_are_recovered_after_the_cooldown(rig: _Rig, caplog) -> None:
    rig.write()
    due = _items(1)
    rig.get_due_watch_items = lambda: due
    rig.get_unnotified_watch_items = lambda: [{"id": "w9", "label": "Mail", "lastResult": "3 new"}]
    await rig.poll()
    assert len(rig.prompts) == 1  # the watch spawn's cooldown also holds recovery back
    assert "Check the status of these watched items" in rig.prompts[0]
    due.clear()
    rig.now += COOLDOWN_MS
    await rig.poll()
    assert "- Mail (id: w9): 3 new" in rig.prompts[1]
    rig.spawn_error = RuntimeError("gateway down")
    rig.now += COOLDOWN_MS
    await rig.poll()
    assert "missed-notification recovery failed" in caplog.text


async def test_freestyle_spawns_run_in_turn_and_skip_bad_and_other_tasks(rig, caplog) -> None:
    rig.write(
        _task("odd", "mystery"),
        _task("junk", "freestyle", action="not an object"),
        _task("fs1", "freestyle", action={"prompt": "do one"}),
        _task("fs2", "freestyle"),
    )
    await rig.poll()
    assert rig.prompts == ["do one", "Execute freestyle task: fs2"]
    assert rig.events == ["start", "end", "start", "end"]
    assert "failed to spawn agent task junk" in caplog.text


@pytest.mark.parametrize("error", [RuntimeError("boom"), SpawnError("refused")])
async def test_a_failed_spawn_closes_its_window_and_the_rest_run(rig, caplog, error) -> None:
    rig.write(_task("fs1", "freestyle"), _task("fs2", "freestyle"))
    rig.spawn_error = error
    await rig.poll()
    assert rig.prompts == ["Execute freestyle task: fs1", "Execute freestyle task: fs2"]
    assert (rig.events, caplog.text.count("spawnAgent failed for")) == (["start", "end"] * 2, 2)


async def test_timed_out_spawns_count_failures_and_the_limit_queues_a_notify(rig: _Rig) -> None:
    rig.timeout = True
    rig.write(_task("fs1", "freestyle"), _task("fs2", "freestyle", fail_count=MAX_FAIL_COUNT - 1))
    await rig.poll()
    counts = [(rig.task(i)["failed"], rig.task(i)["fail_count"]) for i in ("fs1", "fs2")]
    assert counts == [(True, 1), (True, MAX_FAIL_COUNT)]
    assert len(rig.read()["tasks"]) == 3  # only the task at the limit got a notify
    notice = rig.task(f"fail-notify-fs2-{NOW}")
    summary = f"Task fs2 failed {MAX_FAIL_COUNT} times. Please check."
    assert notice["action"] == {"summary": summary, "sticky": True}
    kind = (notice["type"], notice["category"], notice["source"], notice["done"])
    assert (kind, notice["execute_after"]) == (("notify", "error", "system", False), qf._iso(NOW))


async def test_a_disk_error_recording_a_timeout_is_logged_not_raised(rig, caplog) -> None:
    rig.timeout = True
    rig.write(_task("fs1", "freestyle"))
    with mock.patch.object(qf, "write_queue_atomic", side_effect=OSError("disk full")):
        await rig.poll()
    assert "spawn timeout handler error for fs1" in caplog.text
    assert "failed to write queue" in caplog.text
    assert "failed" not in rig.task("fs1")


async def test_a_task_appended_during_the_poll_survives_the_done_stamps(rig: _Rig) -> None:
    rig.write(_task("n1", "notify"))

    def _append_like_the_mcp_server() -> None:
        queue = rig.read()
        queue["tasks"].append(_task("late", "freestyle", execute_after=qf._iso(NOW + 1_000)))
        rig.path.write_text(json.dumps(queue), encoding="utf-8")

    rig.during = _append_like_the_mcp_server
    await rig.poll()
    assert [t["id"] for t in rig.read()["tasks"]] == ["n1", "late"]
    assert (rig.task("n1")["done"], rig.task("late")["done"]) == (True, False)


async def test_a_queue_reset_during_the_poll_is_not_resurrected(rig: _Rig) -> None:
    rig.write(_task("n1", "notify"))
    rig.during = rig.path.unlink
    await rig.poll()
    assert not rig.path.exists()
