"""Coverage for the Mochi poller callbacks in hooks.py: each callback routes an
action to the runtime service that owns it, and the spawn seam is the only
path to an agent."""

from __future__ import annotations

import asyncio
import datetime as _dt
from collections import deque
from typing import Any

import pytest

from kiro_crew.apps.builtins.mochi import hooks


class _Poller:
    """The poller service: only start and stop, as the real QueuePoller exposes to the idle sink."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def start(self) -> None:
        self._events.append("start")

    def stop(self) -> None:
        self._events.append("stop")


class _NotifyGate:
    """The notification gate: only push."""

    def __init__(self, pushed: list[tuple[dict, int]]) -> None:
        self._pushed = pushed

    def push(self, action: dict, now: int) -> None:
        self._pushed.append((action, now))


class _StateManager:
    """The pet state manager: only set_mood."""

    def __init__(self, moods: list[tuple[str, int]]) -> None:
        self._moods = moods

    def set_mood(self, mood: str, now: int) -> None:
        self._moods.append((mood, now))


class _Watchlist:
    """The watchlist service: only the spawn-lock release and the idle pause and resume."""

    def __init__(self, owner: "_FakeRuntime") -> None:
        self._owner = owner

    def clear_spawn_lock(self) -> None:
        self._owner.spawn_lock_cleared = True

    def on_idle(self, now: int) -> None:
        self._owner.idle_events.append("idle")

    def on_resume(self) -> None:
        self._owner.idle_events.append("resume")


class _FakeRuntime:
    """Records every call the callbacks make into the runtime and its four services."""

    def __init__(self) -> None:
        self.published: list[tuple[str, Any]] = []
        self.pushed: list[tuple[dict, int]] = []
        self.moods: list[tuple[str, int]] = []
        self.marks: list[list[Any]] = []
        self.logged: list[tuple[str, str]] = []
        self.spawn_lock_cleared = False
        self.poller_events: list[str] = []
        self.idle_events: list[str] = []
        self.poller = _Poller(self.poller_events)
        self.notify_gate = _NotifyGate(self.pushed)
        self.state_manager = _StateManager(self.moods)
        self.watchlist = _Watchlist(self)

    def publish(self, kind: str, payload: Any) -> None:
        self.published.append((kind, payload))

    def plan_prompt(self) -> str:
        return "PLAN"

    def replan_prompt(self) -> str:
        return "REPLAN"

    def due_watch_items(self) -> list[dict]:
        return [{"id": 1}]

    def unnotified_watch_items(self) -> list[dict]:
        return [{"id": 2}]

    def mark_items_notified(self, ids: list[Any]) -> None:
        self.marks.append(ids)

    def _log_activity(self, kind: str, content: str) -> None:
        self.logged.append((kind, content))


@pytest.fixture
def runtime_and_spawns():
    spawns: list[tuple[str, str]] = []

    async def fake_spawn(prompt: str, agent: str) -> str:
        spawns.append((prompt, agent))
        return "agent reply"

    hooks.set_spawn_fn(fake_spawn)
    try:
        yield _FakeRuntime(), spawns
    finally:
        hooks.set_spawn_fn(None)


@pytest.mark.asyncio
async def test_move_and_notify_are_routed_to_their_services(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    cb = hooks._PollerCallbacks(rt)
    await cb.on_move({"to": "desk"})
    await cb.on_notify({"summary": "hi"})
    assert rt.published == [("mochi:move", {"to": "desk"})]
    action, now = rt.pushed[0]
    assert action == {"summary": "hi"} and isinstance(now, int)


@pytest.mark.asyncio
async def test_mood_is_read_from_mood_or_value_and_empty_is_ignored(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    cb = hooks._PollerCallbacks(rt)
    await cb.on_mood({"mood": "calm"})
    await cb.on_mood({"value": "curious"})
    await cb.on_mood({})
    assert [mood for mood, _ in rt.moods] == ["calm", "curious"]


@pytest.mark.asyncio
async def test_plan_and_replan_spawn_their_prompts(runtime_and_spawns):
    rt, spawns = runtime_and_spawns
    cb = hooks._PollerCallbacks(rt)
    await cb.trigger_plan()
    await cb.trigger_replan()
    assert spawns == [("PLAN", "mochi-bg"), ("REPLAN", "mochi-bg")]


@pytest.mark.asyncio
async def test_spawn_agent_returns_the_spawn_reply(runtime_and_spawns):
    rt, spawns = runtime_and_spawns
    assert await hooks._PollerCallbacks(rt).spawn_agent("task") == "agent reply"
    assert spawns == [("task", "mochi-bg")]


def test_watch_item_queries_and_marking_delegate_to_runtime(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    cb = hooks._PollerCallbacks(rt)
    assert cb.get_due_watch_items() == [{"id": 1}]
    assert cb.get_unnotified_watch_items() == [{"id": 2}]
    cb.mark_items_notified([1, 2])
    assert rt.marks == [[1, 2]]


def test_spawn_start_is_a_no_op_and_spawn_end_clears_the_lock(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    cb = hooks._PollerCallbacks(rt)
    cb.on_agent_spawn_start()
    assert rt.spawn_lock_cleared is False
    cb.on_agent_spawn_end()
    assert rt.spawn_lock_cleared is True


def test_budget_exhaustion_notifies_and_logs_the_resume_time(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    resume_ms = 3_600_000
    resume = _dt.datetime.fromtimestamp(resume_ms / 1000).strftime("%H:%M")
    hooks._PollerCallbacks(rt).on_budget_exhausted(resume_ms)
    action, _now = rt.pushed[0]
    assert action["summary"].endswith(f"next check after {resume}.")
    assert action["mood"] == "sleepy" and action["source"] == "system"
    assert rt.logged == [("budget", f"hourly spawn budget exhausted, resumes {resume}")]


def test_idle_pause_and_resume_drive_poller_and_watchlist(runtime_and_spawns):
    rt, _ = runtime_and_spawns
    idle = hooks._IdleCallbacks(rt)
    idle.on_pause()
    idle.on_resume()
    assert rt.poller_events == ["stop", "start"]
    assert rt.idle_events == ["idle", "resume"]
    assert rt.published[-2:] == [
        ("mochi:idle", {"paused": True}),
        ("mochi:idle", {"paused": False}),
    ]


@pytest.mark.asyncio
async def test_watchlist_callbacks_spawn_publish_and_redact(runtime_and_spawns):
    rt, spawns = runtime_and_spawns
    cb = hooks._WatchlistCallbacks(rt)
    await cb.spawn_agent("watch-prompt")
    cb.force_replan()
    planted = "AKIA" + "IOSFODNN7EXAMPLE"  # split literal, as test_mochi_hooks.py does
    cb.on_notify_direct(f"status {planted} line")
    assert spawns == [("watch-prompt", "mochi-bg")]
    assert ("mochi:force-replan", {}) in rt.published
    kind, payload = rt.published[-1]
    assert kind == "mochi:notify" and payload["degraded"] is True
    assert planted not in payload["summary"] and payload["summary"].startswith("status ")


def _bare_runtime(**attrs: Any) -> hooks.MochiRuntime:
    """A MochiRuntime carrying only the attributes the method under test reads."""
    rt = object.__new__(hooks.MochiRuntime)
    rt.__dict__.update(attrs)
    return rt


@pytest.mark.asyncio
async def test_drain_handoff_tasks_cancels_and_awaits_every_pending_task():
    async def park() -> None:
        await asyncio.Event().wait()

    rt = _bare_runtime(_poll_tasks=set())
    tasks = [asyncio.ensure_future(park()) for _ in range(2)]
    rt._poll_tasks = set(tasks)
    # Bounded: the parked tasks only finish if the drain cancels them, so a drain that
    # forgets to would otherwise hang this await instead of failing it.
    await asyncio.wait_for(rt._drain_handoff_tasks(), timeout=30)
    # Checked while the test's loop is still running: closing the loop cancels whatever is
    # left, which would hide a drain that never cancelled or never waited.
    assert rt._poll_tasks == set()
    assert len(tasks) == 2 and all(task.cancelled() for task in tasks)


def test_chat_push_during_a_turn_is_deferred_and_the_buffer_is_capped():
    rt = _bare_runtime(_deferred_chat_pushes=[], _chat_turn_busy=lambda now: True)
    depth = hooks.MochiRuntime._CHAT_PUSH_SCAN_DEPTH

    for i in range(depth + 1):
        rt._push_to_chat({"summary": f"push {i}"})

    contents = [action["summary"] for action in rt._deferred_chat_pushes]
    assert len(contents) == depth
    assert contents[0] == "push 1" and contents[-1] == f"push {depth}"


def test_chat_push_without_text_is_dropped_before_the_turn_gate():
    rt = _bare_runtime(_deferred_chat_pushes=[], _chat_turn_busy=lambda now: True)

    rt._push_to_chat({"summary": ""})

    assert rt._deferred_chat_pushes == []


def test_empty_push_is_not_broadcast_to_the_chat(tmp_path):
    sent: list[str] = []
    rt = _bare_runtime(
        data_dir=tmp_path,
        _recent_chat_pushes=deque(),
        publish=lambda kind, data: sent.append(kind),
    )

    rt._deliver_chat_push({"summary": ""})

    assert sent == []


def test_unreadable_quiet_period_falls_back_to_five_minutes(monkeypatch, tmp_path):
    monkeypatch.setattr(hooks, "load_settings", lambda _data_dir: {"quietPeriodMins": "soon"})
    now = [1_000_000]
    monkeypatch.setattr(hooks, "_now_ms", lambda: now[0])
    sent: list[tuple[str, dict]] = []
    rt = _bare_runtime(
        data_dir=tmp_path,
        _recent_chat_pushes=deque(),
        publish=lambda kind, data: sent.append((kind, data)),
    )

    rt._deliver_chat_push({"summary": "hello there"})
    now[0] += 5 * 60_000  # exactly five minutes on: still inside the window, so a repeat is dropped
    rt._deliver_chat_push({"summary": "hello there"})
    assert [kind for kind, _ in sent] == ["mochi:chat-push"]
    assert sent[0][1]["content"] == "hello there"

    now[0] += 1  # one millisecond past five minutes: the window has closed
    rt._deliver_chat_push({"summary": "hello there"})
    assert [kind for kind, _ in sent] == ["mochi:chat-push", "mochi:chat-push"]
