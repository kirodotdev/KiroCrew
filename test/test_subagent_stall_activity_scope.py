"""Which stream events count as a subagent's OWN activity (``#4841``).

``SubagentInfo.last_activity`` is what idle-stall detection measures
(``_maybe_flag_stall``: ``idle = now - info.last_activity``), and ``_run_inner``
refreshes it from the subagent's event stream via ``_touch_activity``.

Under ``agent.session_sharing`` (default true) co-tenant subagents are separate
sessions on ONE ``AcpRuntime``, and that runtime fans every frame carrying no
``sessionId`` out to EVERY registered session queue (``_reader_loop``: "No
sessionId -> genuinely global notification; broadcast to all"). The roster
notification ``_kiro.dev/subagent/list_update`` is such a frame, so it reaches
co-tenants that did not produce it. Refreshing the idle clock on it made a
wedged subagent look busy because an unrelated tenant's roster changed --
clearing its "stalled" badge and restarting its idle count.

The discriminator is PROVENANCE, not event kind. ``EVENT_SUBAGENT_LIST`` has two
producers: the ownerless roster broadcast above, and the KAS sub-agent lifecycle
path, which is reached through a ROUTED ``session/update`` frame and therefore is
the session's own progress. A kind-based exclusion would suppress the second one
and falsely badge a working agent, so the runtime marks the fanned-out frame
(``JsonRpcMessage.fanout_no_owner``), the dispatch loop carries that onto the
event (``AcpEvent.runtime_global``), and only that flag suppresses the refresh.

Tests, in two groups:

*Provenance marking* -- an ownerless frame fanned out to several sessions is
marked; the same frame delivered to a LONE session is not (it is the sole owner);
a routed frame is never marked.

*What the stream loop counts* -- a ``runtime_global`` event refreshes neither the
clock nor the badge; a session-scoped one refreshes both. The second is the
negative control, and it is also the regression test for the KAS lifecycle path,
which yields the same event kind with the flag clear.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp._dispatch import classify_notification
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import METHOD_SUBAGENT_LIST_UPDATE
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    LLMEvent,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# Ancient wall-clock stamp: any refresh replaces it with ~time.time(), so
# "unchanged" cannot be satisfied by an accidental re-write.
_ANCIENT = 1.0

_ROSTER_FRAME = {"method": METHOD_SUBAGENT_LIST_UPDATE, "params": {"subagents": []}}


# ── provenance marking at the runtime boundary ───────────────────────


def _runtime() -> tuple[AcpRuntime, asyncio.StreamReader]:
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader


async def _deliver(rt: AcpRuntime, reader: asyncio.StreamReader, frame: dict, sids: list[str]):
    """Feed *frame* through the real reader loop and return one message per sid."""
    queues = {sid: asyncio.Queue() for sid in sids}
    rt._session_queues.update(queues)
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        reader.feed_data((json.dumps(frame) + "\n").encode())
        return [await asyncio.wait_for(queues[sid].get(), timeout=1.0) for sid in sids]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_ownerless_frame_fanned_to_co_tenants_is_marked():
    """One roster frame reaches every co-tenant (parent slot + two subagents on
    one runtime) and is marked as naming no owner -- the contamination path."""
    rt, reader = _runtime()
    sids = ["parent", "sub-a", "sub-b"]
    msgs = await _deliver(rt, reader, _ROSTER_FRAME, sids)
    assert len(msgs) == len(sids)  # fanned out to ALL of them
    for msg in msgs:
        assert msg.method == METHOD_SUBAGENT_LIST_UPDATE
        # The dispatch loop turns it into the roster event on each co-tenant.
        assert classify_notification(msg) == "subagent_list"
        assert msg.fanout_no_owner is True


@pytest.mark.asyncio
async def test_ownerless_frame_to_a_lone_session_is_not_marked():
    """A single registered session IS the sole owner, so the same frame stays
    unmarked and keeps counting as that session's own activity."""
    rt, reader = _runtime()
    (msg,) = await _deliver(rt, reader, _ROSTER_FRAME, ["only"])
    assert msg.fanout_no_owner is False


@pytest.mark.asyncio
async def test_routed_frame_is_never_marked():
    """A frame carrying a sessionId is delivered to its one owner, so it is its
    activity -- this is the provenance the KAS lifecycle path relies on."""
    rt, reader = _runtime()
    frame = {"method": "session/update", "params": {"sessionId": "sub-a", "update": {}}}
    (msg,) = await _deliver(rt, reader, frame, ["sub-a"])
    assert msg.fanout_no_owner is False


@pytest.mark.asyncio
async def test_dispatch_carries_provenance_onto_the_roster_event():
    """End-to-end through the REAL dispatch loop: an ownerless roster frame
    fanned out to a co-tenant yields an event marked ``runtime_global``.

    Without this the marking could stop at the runtime boundary and the whole
    exclusion would be inert while every other test still passed.
    """
    rt, reader = _runtime()
    queues = {sid: asyncio.Queue() for sid in ("sub-a", "sub-b")}
    rt._session_queues.update(queues)
    handle = AcpSessionHandle("sub-a", queues["sub-a"], rt)
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    try:
        reader.feed_data((json.dumps(_ROSTER_FRAME) + "\n").encode())
        reader.feed_data(
            (json.dumps({"id": 1, "result": {"stopReason": "end_turn"}}) + "\n").encode()
        )
        events = [ev async for ev in handle._dispatch_events(req_id=1, timeout=3.0)]
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    roster = [ev for ev in events if ev.kind == EVENT_SUBAGENT_LIST]
    assert len(roster) == 1
    assert roster[0].runtime_global is True


# ── what the stream loop counts as this subagent's activity ──────────


def _mock_sessions(stream_factory: object) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _manager(stream_factory: object) -> SubagentManager:
    mgr = SubagentManager(sessions=_mock_sessions(stream_factory), ctx_builder=_mock_ctx_builder())
    # Dedicated-process path: deterministic under MagicMock sessions. The
    # activity clock lives in _run_inner, entered once per turn on BOTH paths.
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    mgr._fire_event = AsyncMock()
    return mgr


async def _observe_after(event: SimpleNamespace) -> tuple[float, bool]:
    """Drive one real subagent turn whose stream yields *event* while the run
    looks long-idle and already badged, and report the ``(last_activity,
    stalled)`` the stream loop left behind.

    The generator re-plants the ancient stamp and the badge *before* handing the
    event over, so the reading is taken after the loop body processed exactly
    that event -- ``_run_inner``'s own start-of-run reset cannot mask it.
    """
    observed: list[tuple[float, bool]] = []
    holder: dict[str, SubagentManager] = {}

    def stream_factory(_msg: str, *_a: object, **_kw: object) -> object:
        async def _gen():
            info: SubagentInfo = next(iter(holder["mgr"]._agents.values()))
            info.last_activity = _ANCIENT
            info.stalled = True
            yield event
            # Resumed after the loop body handled the event above.
            observed.append((info.last_activity, info.stalled))
            yield SimpleNamespace(kind=EVENT_COMPLETE, stop_reason="end_turn", runtime_global=False)

        return _gen()

    mgr = _manager(stream_factory)
    holder["mgr"] = mgr
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("watch my idle clock")
        assert info is not None
        await mgr._tasks[info.id]
    assert len(observed) == 1, "the stream loop never saw the event under test"
    return observed[0]


@pytest.mark.asyncio
async def test_fanned_out_roster_event_is_not_own_activity():
    """A co-tenant's roster broadcast must leave the idle clock and the stalled
    badge alone -- it is not this subagent's progress."""
    last_activity, stalled = await _observe_after(
        SimpleNamespace(kind=EVENT_SUBAGENT_LIST, subagents=[], runtime_global=True)
    )
    assert last_activity == _ANCIENT
    assert stalled is True


@pytest.mark.asyncio
async def test_routed_roster_event_is_own_activity():
    """SAME event kind, routed provenance (the KAS sub-agent lifecycle path):
    this IS the session's own progress, so it must still refresh the clock and
    clear the badge. Without this the fix would falsely badge a working agent."""
    last_activity, stalled = await _observe_after(
        SimpleNamespace(kind=EVENT_SUBAGENT_LIST, subagents=[], runtime_global=False)
    )
    assert last_activity > _ANCIENT
    assert stalled is False


@pytest.mark.asyncio
async def test_session_scoped_chunk_is_own_activity():
    """Negative control on an ordinary event kind, so the exclusion above cannot
    pass vacuously by nothing ever touching the clock."""
    last_activity, stalled = await _observe_after(
        SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="x", runtime_global=False)
    )
    assert last_activity > _ANCIENT
    assert stalled is False


def test_provider_event_translation_carries_the_flag():
    """``AcpProvider`` rebuilds ``LLMEvent`` field by field, so a new field is
    silently dropped unless it is named there. Pin the copy: without it the
    provenance degrades to the default False on that hop and the exclusion goes
    inert on every path that translates events.
    """
    src = LLMEvent(kind=EVENT_SUBAGENT_LIST, subagents=[], runtime_global=True)
    assert AcpProvider._to_llm_event(src).runtime_global is True
