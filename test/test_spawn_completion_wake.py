"""A caller waiting for a sub-agent run is woken by the run's own ending.

``spawn_sub_agents`` and a blocking ``kirocrew spawn run`` wait for runs to end.
The gateway holds their status read (``GET /api/spawn/{id}?wait_secs=N``) until the
run's record flips ``done``, so the caller returns when the run ends and not at its
next poll. The interval stays as the fallback for a read the gateway could not hold.

The wave tests run the real tool against children that end at set instants of a
manual clock; a read that asks to wait advances that clock exactly as far as the
gateway would have held it. Nothing here sleeps or asserts a wall-clock duration.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import threading
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs

import pytest
from mcp_shared_helpers import _LoopHarness, _tools_call

from kiro_crew import mcp_core
from kiro_crew.dashboard.handlers.messaging import api_spawn_status
from kiro_crew.mcp_shared import DEFERRED_STEP_WORKERS, drive_deferred
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.mcp_tools.spawn import _SubAgentsStep, spawn_sub_agents
from kiro_crew.subagent import SubagentInfo, wait_done
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until, await_until

NEVER = float("inf")


class _Children:
    """The gateway's side of a wave: each child ends at an instant of the clock.

    A status read that asks to wait is held until its child ends or the wait
    passes, whichever is first (``holds=False`` answers every read at once, as a
    gateway does for a run it has no live record to wait on).
    """

    def __init__(self, clock: Any, ends: dict[str, float], *, holds: bool = True) -> None:
        self.clock = clock
        self.zero = clock.monotonic()
        self.ends = ends
        self.holds = holds
        self.failed: dict[str, str] = {}
        self.queued: set[str] = set()
        self._ids = iter(ends)
        #: ``(member, asked at, seconds it asked the gateway to wait)`` per status read.
        self.reads: list[tuple[str, float, float]] = []
        self.posts: list[str] = []

    def now(self) -> float:
        return self.clock.monotonic() - self.zero

    def post(self, path: str, body: dict | None = None, **_kw: object) -> dict:
        self.posts.append(path)
        return {"id": next(self._ids)} if path == "/api/spawn" else {}

    def get(self, path: str, **_kw: object) -> dict:
        base, _, query = path.partition("?")
        aid = base.rsplit("/", 1)[-1]
        wait = float(parse_qs(query).get("wait_secs", ["0"])[0])
        self.reads.append((aid, self.now(), wait))
        if aid in self.queued:
            return {"id": aid, "done": False, "queued": True, "reason": "concurrency_limit"}
        if self.holds and wait > 0 and self.now() < self.ends[aid]:
            self.clock.advance(min(wait, self.ends[aid] - self.now()))
        if self.now() < self.ends[aid]:
            return {"id": aid, "done": False}
        if aid in self.failed:
            return {"id": aid, "done": True, "error": self.failed[aid], "result": "partial"}
        return {"id": aid, "done": True, "result": f"result of {aid}"}


@pytest.fixture
def wave(monkeypatch, manual_clock):
    """Start a wave of the named children; returns ``(children, deferred step)``."""
    manual_clock.install(monkeypatch, mcp_core)
    monkeypatch.setattr(mcp_core, "sel", MagicMock())
    monkeypatch.setenv("KIROCREW_SPAWN_SUB_AGENTS_MAX_WAIT", "60")

    def start(ends: dict[str, float], *, parent: str = "dashboard:owner", holds: bool = True):
        children = _Children(manual_clock, ends, holds=holds)
        monkeypatch.setattr(mcp_core, "_post", children.post)
        monkeypatch.setattr(mcp_core, "_get", children.get)
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: parent)
        step = spawn_sub_agents("spawn_sub_agents", {"agents": [{"prompt": aid} for aid in ends]})
        assert isinstance(step, _SubAgentsStep)
        for _member in ends:
            assert step.step() is None
        assert step.phase == "poll"
        return children, step

    return start


def _statuses(reply: str) -> list[str]:
    return [json.loads(block).get("status", "") for block in reply.split("\n\n")]


class TestTheWaveReturnsWhenItsLastChildEnds:
    def test_the_reply_is_built_the_moment_the_last_child_ends(self, wave):
        """Two children end off the interval's grid; the wave returns at the later
        ending, not at the next multiple of the interval after it."""
        children, step = wave({"a": 3.25, "b": 5.25})
        reply = drive_deferred(step, clock=mcp_core.time)
        assert _statuses(reply) == ["completed", "completed"]
        assert children.now() == 5.25
        assert [json.loads(b)["text"] for b in reply.split("\n\n")] == [
            "result of a",
            "result of b",
        ]

    def test_a_child_that_ended_before_the_wait_began_is_not_waited_for(self, wave):
        children, step = wave({"a": 0.0, "b": 0.0})
        reply = drive_deferred(step, clock=mcp_core.time)
        assert _statuses(reply) == ["completed", "completed"]
        assert children.now() == 0.0

    def test_an_earlier_member_ending_last_still_ends_the_wave_at_once(self, wave):
        children, step = wave({"slow": 7.5, "fast": 1.0})
        drive_deferred(step, clock=mcp_core.time)
        assert children.now() == 7.5

    def test_a_failed_child_ends_the_wave_like_a_finished_one(self, wave):
        children, step = wave({"a": 1.5, "b": 4.75})
        children.failed["b"] = "turn_limit:5"
        reply = drive_deferred(step, clock=mcp_core.time)
        blocks = [json.loads(b) for b in reply.split("\n\n")]
        assert [b["status"] for b in blocks] == ["completed", "error"]
        assert blocks[1]["error"] == "turn_limit:5" and blocks[1]["text"] == "partial"
        assert children.now() == 4.75

    def test_a_chat_parent_gets_its_reply_from_the_step_that_saw_the_last_ending(self, wave):
        """No step is spent between the last ending and the reply: each one in
        between would cost the answer a dispatch-loop tick."""
        _children, step = wave({"a": 0.5})
        assert _statuses(step.step() or "") == ["completed"]

    def test_a_nested_parent_probes_its_slot_in_the_step_that_saw_the_last_ending(self, wave):
        children, step = wave({"a": 0.5}, parent="subagent:p1")
        granted = iter([{"known": True, "granted": False}, {"known": True, "granted": True}])
        plain_get = children.get
        probes: list[float] = []

        def get(path: str, **kw: object) -> dict:
            if "/resume" in path:
                probes.append(children.now())
                return next(granted)
            return plain_get(path, **kw)

        mcp_core._get = get  # the fixture's monkeypatch restores the binding
        assert step.step() is None, "the slot is not granted yet: nothing is returned"
        assert probes == [0.5] and step.phase == "hold"
        assert _statuses(step.step() or "") == ["completed"]


class TestTheIntervalIsTheFallback:
    def test_a_read_the_gateway_does_not_hold_is_repeated_on_the_interval(self, wave):
        """A lost wake-up costs at most one interval, and never a spin."""
        children, step = wave({"a": 5.25}, holds=False)
        drive_deferred(step, clock=mcp_core.time)
        waited = [at for member, at, _wait in children.reads if member == "a"]
        assert waited == [0.0, 2.0, 4.0, 6.0, 6.0]  # four passes, then the result read
        assert children.now() == 6.0

    def test_a_member_with_no_run_yet_is_read_on_the_interval_then_waited_for(self, wave):
        children, step = wave({"q": 7.25})
        children.queued.add("q")
        assert step.step() is None and step.due_at() - children.zero == 2.0
        children.clock.advance(2.0)
        assert step.step() is None and step.due_at() - children.zero == 4.0
        children.queued.clear()  # the pump started it
        children.clock.advance(2.0)
        drive_deferred(step, clock=mcp_core.time)
        assert children.now() == 7.25

    def test_a_child_that_never_ends_is_left_running_at_the_deadline(self, wave):
        children, step = wave({"a": NEVER, "b": 1.0})
        reply = drive_deferred(step, clock=mcp_core.time)
        blocks = [json.loads(b) for b in reply.split("\n\n")]
        assert [b["status"] for b in blocks] == ["completed", "still_running"]
        assert blocks[1]["states"] == {"a": "running"} and blocks[1]["waited_secs"] == 60
        assert children.now() == 60.0
        # One held read of the unfinished member per interval, none in between.
        asked = [at for member, at, _wait in children.reads if member == "a"]
        assert asked[:-1] == [2.0 * n for n in range(30)]

    def test_no_read_is_held_past_the_interval_or_the_deadline(self, wave):
        children, step = wave({"a": NEVER})
        drive_deferred(step, clock=mcp_core.time)
        waits = [wait for _member, _at, wait in children.reads]
        assert max(waits) == step.POLL_INTERVAL
        assert all(at + wait <= 60.0 for _member, at, wait in children.reads)

    def test_a_held_read_keeps_a_plain_reads_margin_for_its_answer(self, wave, monkeypatch):
        children, step = wave({"a": 1.0})
        timeouts: list[object] = []
        plain_get = children.get

        def get(path: str, **kw: object) -> dict:
            timeouts.append(kw.get("timeout"))
            return plain_get(path, **kw)

        monkeypatch.setattr(mcp_core, "_get", get)
        drive_deferred(step, clock=mcp_core.time)
        assert timeouts == [10 + step.POLL_INTERVAL, None]  # the wait, then the result


class TestHeldReadsLeaveStepThreadsFree:
    def test_a_wave_reads_without_holding_when_every_held_read_is_taken(self, wave):
        children, step = wave({"a": 5.25})
        taken = DEFERRED_STEP_WORKERS // 2
        for _slot in range(taken):
            assert spawn_tools._HELD_READS.acquire(blocking=False)
        try:
            assert not spawn_tools._HELD_READS.acquire(blocking=False), "half the pool, no more"
            assert step.step() is None
            assert children.reads == [("a", 0.0, 0.0)] and step.due_at() - children.zero == 2.0
        finally:
            for _slot in range(taken):
                spawn_tools._HELD_READS.release()
        children.clock.advance(2.0)
        drive_deferred(step, clock=mcp_core.time)
        assert children.now() == 5.25

    def test_a_pass_returns_its_held_read_even_when_the_read_raises(self, wave, monkeypatch):
        _children, step = wave({"a": 5.25})

        def broken(path: str, **_kw: object) -> dict:
            raise RuntimeError("gateway went away")

        monkeypatch.setattr(mcp_core, "_get", broken)
        with pytest.raises(RuntimeError):
            step.step()
        for _slot in range(DEFERRED_STEP_WORKERS // 2):
            assert spawn_tools._HELD_READS.acquire(blocking=False)
        for _slot in range(DEFERRED_STEP_WORKERS // 2):
            spawn_tools._HELD_READS.release()


class TestACancelDuringAHeldRead:
    def test_the_call_gets_no_answer_and_marks_nothing_collected(self, monkeypatch):
        """The parent's turn is stopped while the wave waits on a child: the held
        read's answer is dropped when it comes, no reply is written, and the child
        keeps its completion event."""
        reading = threading.Event()
        child_ended = threading.Event()
        posts: list[str] = []

        def post(path: str, body: dict | None = None, **_kw: object) -> dict:
            posts.append(path)
            return {"id": "a1"} if path == "/api/spawn" else {}

        def get(path: str, **_kw: object) -> dict:
            reading.set()
            assert child_ended.wait(timeout=FALLBACK_TIMEOUT_SECS)
            return {"id": "a1", "done": True, "result": "late"}

        monkeypatch.setattr(mcp_core, "_post", post)
        monkeypatch.setattr(mcp_core, "_get", get)
        monkeypatch.setattr(mcp_core, "sel", MagicMock())
        monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:owner")
        harness = _LoopHarness(
            monkeypatch,
            lambda name, _args: (
                spawn_sub_agents(name, {"agents": [{"prompt": "task"}]})
                if name == "spawn_sub_agents"
                else "ok"
            ),
        )
        try:
            harness.send(_tools_call(1, "spawn_sub_agents"))
            assert harness.wait_for(reading.is_set)
            harness.send(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
            )
            # Frames are handled in order: this answer proves the cancel was read
            # while the held read was still out.
            harness.send({"jsonrpc": "2.0", "id": 9, "method": "ping"})
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            child_ended.set()
            cancelled = harness.sel_mock.log_tool_invocation
            assert harness.wait_for(
                lambda: any(
                    c.kwargs.get("outcome") == "cancelled" for c in cancelled.call_args_list
                )
            )
            harness.send(_tools_call(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert not any(r[0] == 1 for r in harness.responses)
            assert "/api/spawn/mark-collected" not in posts
        finally:
            child_ended.set()
            harness.close()


def _status_request(info: SubagentInfo, **query: str) -> MagicMock:
    state = MagicMock()
    state.subagents.get = MagicMock(return_value=info)
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"agent_id": info.id}
    request.query = query
    return request


async def _read(request: MagicMock) -> dict:
    return json.loads((await api_spawn_status(request)).body)


class TestTheStatusReadHoldsUntilTheRunEnds:
    @pytest.mark.asyncio
    async def test_a_held_read_is_answered_when_the_run_ends(self):
        info = SubagentInfo(id="a1", task="t")
        read = asyncio.ensure_future(_read(_status_request(info, wait_secs="8")))
        await async_wait_until(lambda: info._done_wakers)  # the read is parked on the run
        assert not read.done()
        info.result = "the answer"
        info.done = True
        body = await await_until(read, what="the held status read")
        assert body["done"] is True and body["result"] == "the answer"
        assert info._done_wakers == []

    @pytest.mark.asyncio
    async def test_a_run_that_does_not_end_is_answered_at_the_bound(self):
        info = SubagentInfo(id="a1", task="t")
        body = await await_until(
            asyncio.ensure_future(_read(_status_request(info, wait_secs="0.01"))),
            what="the status read at its bound",
        )
        assert body["done"] is False
        assert info._done_wakers == []

    @pytest.mark.asyncio
    async def test_a_plain_read_and_a_read_of_a_finished_run_are_answered_at_once(self):
        running = SubagentInfo(id="a1", task="t")
        ended = SubagentInfo(id="a2", task="t", done=True)
        assert (await _read(_status_request(running)))["done"] is False
        assert (await _read(_status_request(ended, wait_secs="8")))["done"] is True
        assert running._done_wakers == [] and ended._done_wakers == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bad", ["nan", "inf", "-1", "soon", "1e400"])
    async def test_a_wait_that_is_not_a_finite_number_of_seconds_is_refused(self, bad):
        info = SubagentInfo(id="a1", task="t")
        response = await api_spawn_status(_status_request(info, wait_secs=bad))
        assert response.status == 400
        assert json.loads(response.body)["code"] == "invalid_wait_secs"
        assert info._done_wakers == []

    @pytest.mark.asyncio
    async def test_a_cancelled_read_leaves_nothing_on_the_run(self):
        info = SubagentInfo(id="a1", task="t")
        read = asyncio.ensure_future(_read(_status_request(info, wait_secs="8")))
        await async_wait_until(lambda: info._done_wakers)
        read.cancel()
        with pytest.raises(asyncio.CancelledError):
            await read
        assert info._done_wakers == []
        info.done = True  # a later ending has nobody to wake, and must not raise


class TestEveryEndingWakesTheWait:
    @pytest.mark.asyncio
    async def test_an_ending_made_on_another_thread_wakes_the_wait(self):
        info = SubagentInfo(id="a1", task="t")
        waiting = asyncio.ensure_future(wait_done(info, FALLBACK_TIMEOUT_SECS))
        await async_wait_until(lambda: info._done_wakers)
        await asyncio.to_thread(setattr, info, "done", True)
        assert await await_until(waiting, what="the wait on the run") is True

    @pytest.mark.asyncio
    async def test_several_waits_on_one_run_are_all_woken(self):
        info = SubagentInfo(id="a1", task="t")
        waits = [asyncio.ensure_future(wait_done(info, FALLBACK_TIMEOUT_SECS)) for _ in range(3)]
        await async_wait_until(lambda: len(info._done_wakers) == 3)
        info.done = True
        assert await await_until(asyncio.gather(*waits), what="the three waits") == [True] * 3

    def test_done_stays_a_plain_field_of_the_record(self):
        """The wake-up rides the field every ending already writes; the record's
        constructor, copy and comparison read it as the bool it was."""
        info = SubagentInfo(id="a1", task="t")
        assert info.done is False and vars(info)["done"] is False
        assert SubagentInfo(id="a1", task="t", done=True).done is True
        assert dataclasses.replace(info, done=True).done is True
        by_name = {f.name: f for f in dataclasses.fields(SubagentInfo)}
        assert by_name["done"].default is False
        assert "done=False" in repr(info)
        info.done = True
        assert info.done is True and vars(info)["done"] is True


class TestTheBlockingCliWaitsOnTheSameRead:
    def _run(
        self, monkeypatch, answers: list[dict], *, held: bool
    ) -> tuple[list[str], list[float]]:
        """Run ``kirocrew spawn run`` against canned status answers; returns the
        status URLs it read and the sleeps it took between them."""
        import kiro_crew.cli_commands as cli

        now = [100.0]
        slept: list[float] = []
        urls: list[str] = []

        def sleep(secs: float) -> None:
            slept.append(secs)
            now[0] += secs

        monkeypatch.setattr(cli, "_time", SimpleNamespace(monotonic=lambda: now[0], sleep=sleep))
        monkeypatch.setattr(cli, "read_local_secret", lambda _port, **_kw: "s")
        queue = iter([{"id": "ag1", "task": "t"}, *answers])

        def send(req: Any, _port: int, timeout: float, operation: str) -> MagicMock:
            if operation == "cli_spawn_poll":
                urls.append(req.full_url)
                assert timeout == 5 + cli._SPAWN_WAIT_SECS
                if held:
                    now[0] += cli._SPAWN_WAIT_SECS  # the gateway held the read
            response = MagicMock()
            response.read.return_value = json.dumps(next(queue)).encode()
            response.__enter__ = MagicMock(return_value=response)
            response.__exit__ = MagicMock(return_value=False)
            return response

        monkeypatch.setattr(cli, "_gateway_send", send)
        cli._spawn_run(
            argparse.Namespace(task="t", fire_and_forget=False, port=5476), "http://127.0.0.1:5476"
        )
        return urls, slept

    def test_each_read_asks_the_gateway_to_wait_and_none_sleeps_after_a_held_one(
        self, monkeypatch, capsys
    ):
        urls, slept = self._run(
            monkeypatch, [{"done": False}, {"done": True, "result": "finished"}], held=True
        )
        assert urls == ["http://127.0.0.1:5476/api/spawn/ag1?wait_secs=2"] * 2
        assert slept == [0.0]
        assert "finished" in capsys.readouterr().out

    def test_a_read_answered_without_a_hold_is_repeated_on_the_interval(self, monkeypatch, capsys):
        _urls, slept = self._run(
            monkeypatch,
            [{"done": False, "queued": True}, {"done": False}, {"done": True, "result": "ok"}],
            held=False,
        )
        assert slept == [2.0, 2.0]
        assert "ok" in capsys.readouterr().out
