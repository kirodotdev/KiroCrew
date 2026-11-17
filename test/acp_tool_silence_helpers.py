"""The tick harness the per-call silence timer tests drive a turn through.

A dedicated ``*_helpers.py`` imported by bare name, per the repo convention: no
``test_*`` module imports another. ``test_acp_tool_silence_timer`` and
``test_acp_tool_silence_visibility`` both run their turns here.

The handle's clock is a fake that each queue read advances by one exact tick, so
every instant a test computes is exact. The runtime is a stand-in for a kiro-cli
shared runtime; what it answers about the roster route (``_Turn.routes``) and its
count of frames that reached no session (``_Turn.unrouted``) are the test's to set.
"""

from __future__ import annotations

import asyncio
import itertools
import types
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import session_handle, session_handle_dispatch
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings
from kiro_crew.acp.types import ACP_BACKEND_KIRO, METHOD_SUBAGENT_LIST_UPDATE, JsonRpcMessage

# Binary fractions, so the clock's sums are exact.
_TICK = 1 / 64
_START = 1024.0
#: The built-in and network bound, and the sub-agent bound, in seconds.
_BOUND = 16 * _TICK
#: The stdio bound (Kiro Crew's own servers are stdio).
_STDIO_BOUND = 32 * _TICK
#: The turn's own budget: far past every cancel below, so a turn the timer
#: never acts on ends on its deadline instead.
_TURN = 512 * _TICK

_Script = Callable[[int, AcpSessionHandle], list[JsonRpcMessage]]


class _Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> float:
        return self.now


def install_tick_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """Put the fake clock under every session-handle owner, read on every pass."""
    fake = _Clock()
    monkeypatch.setattr(session_handle, "time", types.SimpleNamespace(monotonic=fake))
    # Read the timers on every pass, so the instants below are exact.
    monkeypatch.setattr(session_handle_dispatch, "SILENCE_CHECK_SECS", 0.0)
    return fake


class _TickQueue:
    """The session queue. Each read is one tick: it returns the frames scripted for
    that tick, one per read, else it times out (the watchdog's empty-queue arm)."""

    def __init__(self, clock: _Clock, handle_ref: list, script: _Script) -> None:
        self._clock = clock
        self._handle_ref = handle_ref
        self._script = script
        self._items: list[JsonRpcMessage] = []
        self.tick = 0

    async def get(self) -> JsonRpcMessage:
        # What the last read woke runs at that read's instant, and so do the done
        # callbacks of a task it finished (a KAS hook's end closes its hold there):
        # a real read waits far longer than the loop takes to run them.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if self._items:
            return self._items.pop(0)
        self.tick += 1
        self._clock.now += _TICK
        frames = self._script(self.tick, self._handle_ref[0])
        if frames:
            self._items.extend(frames[1:])
            return frames[0]
        raise asyncio.TimeoutError

    def get_nowait(self) -> JsonRpcMessage:
        """The turn start's drain: only what is already queued, no tick."""
        if not self._items:
            raise asyncio.QueueEmpty
        return self._items.pop(0)

    def put_nowait(self, item: JsonRpcMessage) -> None:
        self._items.append(item)

    def qsize(self) -> int:
        return len(self._items)


def _settings() -> WatchdogSettings:
    # The oracle arm is kept out of the way (it would only act past the hard
    # cap), so every cancel below is the per-call timer's.
    return WatchdogSettings(
        check_after_secs=_TURN,
        stale_window_secs=_TURN,
        tool_stall_suspect_secs=_TURN,
        tool_stall_hard_cap_secs=_TURN,
        builtin_tool_silence_secs=_BOUND,
        mcp_network_silence_secs=_BOUND,
        mcp_stdio_silence_secs=_STDIO_BOUND,
        subagent_silence_secs=_BOUND,
    )


def _update(update: dict, session: str = "sA") -> JsonRpcMessage:
    return JsonRpcMessage(method="session/update", params={"sessionId": session, "update": update})


def _tool_call(call_id: str, *, kind: str | None, kiro: dict, raw: dict | None = None) -> dict:
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "title": call_id,
        "status": "pending",
        "rawInput": raw or {},
        "_meta": {"kiro": kiro},
    }
    if kind is not None:
        frame["kind"] = kind
    return frame


#: The lost call: kiro-cli's built-in write, a file edit.
_EDIT = _tool_call("tc-edit", kind="edit", kiro={"toolName": "write"}, raw={"path": "a.txt"})
#: Kiro Crew's own ``wait`` on ``kirocrew-core``, a stdio server.
_WAIT = _tool_call(
    "tc-wait",
    kind="other",
    kiro={"toolName": "wait", "mcpServerName": "kirocrew-core"},
    raw={"seconds": 600},
)
#: kiro-cli's native sub-agent tool.
_SUBAGENT = _tool_call("tc-sub", kind=None, kiro={"toolName": "subagent"}, raw={"task": "x"})


def _co_tenant_roster() -> JsonRpcMessage:
    """A co-tenant subagent's roster: no sessionId, fanned out and marked."""
    return JsonRpcMessage(
        method=METHOD_SUBAGENT_LIST_UPDATE,
        params={"subagents": []},
        fanout_no_owner=True,
    )


def _child_text(text: str = "working") -> JsonRpcMessage:
    """A native sub-agent's streamed text, routed to its parent's queue."""
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}},
        session="child-1",
    )


class _Turn:
    """One turn on a kiro-cli shared runtime, its frames scripted per tick.

    *routes* is what the runtime answers when asked whether a frame a native
    sub-agent sends under its own ``sessionId`` reaches this session
    (``AcpRuntime.routes_roster_children_to``), and with it the runtime counts the
    frames that reached no session (``AcpRuntime.unrouted_child_frames``), from 0.
    A script may change either mid-turn (``self.routes``, ``self.unrouted``). Left
    unset, the stand-in runtime answers neither question, which proves nothing.

    *keepalive_routed* is whether the registry ``/api/session-keepalive`` reads holds
    this session, as it holds a gateway session's (``kiro_crew.keepalive_route``);
    False is a handle no Kiro Crew heartbeat can reach, such as ``kirocrew chat``'s,
    and None leaves the handle's own default, which nothing has marked.
    """

    def __init__(
        self,
        clock: _Clock,
        script: _Script,
        *,
        backend: str = ACP_BACKEND_KIRO,
        routes: bool | None = None,
        keepalive_routed: bool | None = True,
    ) -> None:
        rt = MagicMock()
        rt._last_activity = clock.now
        rt.pid = None
        rt.acp_backend = backend
        rt.is_alive = MagicMock(return_value=True)
        self.routes = routes
        self.unrouted = 0
        if routes is not None:
            rt.routes_roster_children_to = MagicMock(side_effect=lambda _sid: self.routes)
            rt.unrouted_child_frames = MagicMock(side_effect=lambda: self.unrouted)
        self.cancelled_at: float | None = None

        async def _note(method, *args, **kwargs):
            if method == "session/cancel" and self.cancelled_at is None:
                self.cancelled_at = clock.now - _START

        rt.send_notification = AsyncMock(side_effect=_note)
        rt.send_error = AsyncMock()
        # The answers to the harness's requests (an approval, an elicitation).
        rt.send_response = AsyncMock()
        # Each prompt() turn's ``session/prompt``: request ids 1, 2, ...
        request_ids = itertools.count(1)
        rt.send_request = AsyncMock(side_effect=lambda *_a, **_k: next(request_ids))
        ref: list = [None]
        self.queue = _TickQueue(clock, ref, script)
        handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=_settings())
        ref[0] = handle
        handle._queue = self.queue  # type: ignore[assignment]
        handle._turn_done.clear()
        if keepalive_routed is not None:
            handle.keepalive_routed = keepalive_routed
        self.handle = handle
        #: The consumer: called with each event, awaited when it returns an
        #: awaitable (one that answers an ask through the handle does).
        self.on_event: Callable[[object], object] = lambda ev: None

    async def _consume(self, ev: object) -> None:
        result = self.on_event(ev)
        if asyncio.iscoroutine(result):
            await result

    async def run(self) -> list:
        """The dispatch loop alone, after the silence timer's share of the turn start."""
        self.handle._reset_tool_silence()
        events = []
        async for ev in self.handle._dispatch_events(1, _TURN):
            events.append(ev)
            await self._consume(ev)
        return events

    async def prompt(self) -> list:
        """One turn through ``prompt()``, the path every consumer takes: the turn
        start, and the park ``prompt()`` banks while the consumer holds an event
        (``on_event`` moving the clock is that hold). Turn *n* is request id *n*."""
        self.handle._turn_done.set()
        events = []
        stream = self.handle.prompt("go", timeout=_TURN)  # flake-ok: tick-clock deadline
        async for ev in stream:
            events.append(ev)
            await self._consume(ev)
        return events


def _at(tick: int) -> float:
    return tick * _TICK


def _permission(call_id: str = "tc-edit") -> JsonRpcMessage:
    """The harness's ``session/request_permission`` for *call_id*."""
    return JsonRpcMessage(
        id=900,
        method="session/request_permission",
        params={
            "sessionId": "sA",
            "toolCall": {"toolCallId": call_id, "title": "Editing a.txt", "kind": "edit"},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
            ],
        },
    )
