"""Hang-resilience telemetry — the kirocrew.* series added after the silent
child-permission hang incidents (issue #3785, PRs #3786/#3889).

Each test drives the REAL production emit site (never a reimplementation)
with the recorder mocked, so a renamed metric, changed attr enum, or removed
emit fails here.

Self-contained: CI runs test shards in separate processes where sibling test
modules are not importable, so the small runtime harness is inlined here
rather than imported from ``test.test_acp_runtime``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp.runtime import AcpRuntime, JsonRpcMessage
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.metrics import events as metric_events


def _make_runtime():
    """An initialized AcpRuntime wired to a fake subprocess (no real process)."""
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 4242
    rt._process = proc
    rt._pid = 4242
    rt._initialized = True
    return rt, reader, proc


def _register(rt: AcpRuntime, *session_ids: str) -> dict[str, asyncio.Queue]:
    queues = {sid: asyncio.Queue() for sid in session_ids}
    rt._session_queues.update(queues)
    return queues


def _perm_msg(req_id: int, session_id: str = "child-a") -> JsonRpcMessage:
    return JsonRpcMessage.from_dict(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "session/request_permission",
            "params": {
                "sessionId": session_id,
                "toolCall": {"toolCallId": f"tc-{req_id}", "title": "Running: x"},
                "options": [{"optionId": "reject_once", "name": "Reject", "kind": "reject_once"}],
            },
        }
    )


@pytest.fixture()
def recorded(monkeypatch):
    """Capture every emit_counter call routed through metrics.events."""
    calls: list[tuple[str, dict]] = []

    def _fake(name: str, attrs: dict) -> None:
        calls.append((name, dict(attrs)))

    # Patch at the SOURCE module; the emitting modules import the function by
    # name, so patch their bound references too.
    for mod in (
        "kiro_crew.metrics.events",
        "kiro_crew.acp.runtime",
        "kiro_crew.acp.session_handle",
        "kiro_crew.subagent",
        "kiro_crew.session",
        "kiro_crew.dashboard.chat_runner",
    ):
        monkeypatch.setattr(f"{mod}.emit_counter", _fake, raising=False)
    return calls


def test_emit_counter_never_raises():
    """Telemetry must never break the instrumented path."""
    with patch(
        "kiro_crew.metrics.provider.get_recorder",
        side_effect=RuntimeError("recorder down"),
    ):
        metric_events.emit_counter("kirocrew.test", {"a": 1})  # no raise


@pytest.mark.asyncio
async def test_runtime_denial_emits_child_permission_denied(recorded):
    rt, _, _ = _make_runtime()
    await rt._answer_unroutable_permission(_perm_msg(42), "child-a")
    await asyncio.sleep(0)
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {"surface": "runtime", "reason": "unregistered_session_auto_reject"} in hits


def test_dropped_frame_emits_method_class(recorded):
    rt, _, _ = _make_runtime()
    rt._note_dropped_frame("s-x", "session/request_permission")
    rt._note_dropped_frame("s-x", "session/update")
    rt._note_dropped_frame("s-x", "weird/method")
    classes = [a["method_class"] for n, a in recorded if n == metric_events.DROPPED_FRAMES]
    assert classes == ["permission", "update", "other"]


def test_handle_reject_emits_child_permission_denied(recorded):
    rt, _, _ = _make_runtime()
    q = _register(rt, "sA")
    handle = AcpSessionHandle("sA", q["sA"], rt)
    handle._audit_handle_reject(
        7,
        "Running: x",
        "child_low_fidelity_unaware_consumer",
        sub_session_id="child-a",
    )
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {
        "surface": "session_handle",
        "reason": "child_low_fidelity_unaware_consumer",
    } in hits
    # PARENT-origin rejections (empty sub_session_id, e.g. an abandoned
    # parent-turn request answered by the pre-turn drain) must NOT emit —
    # they would corrupt the child-denial series.
    handle._audit_handle_reject(8, "Running: y", "stranded_request_pre_turn_drain")
    assert len([a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]) == 1


@pytest.mark.asyncio
async def test_routed_child_permission_emits_routed(recorded):
    """A child permission request delivered to the owner's queue (the
    mode-parity pipeline) counts as ROUTED — the impact numerator: each one
    would have been a silent 2h hang before #3786."""
    rt, reader, _ = _make_runtime()
    queues = _register(rt, "parent-session")
    task = asyncio.ensure_future(rt._reader_loop())
    try:
        reader.feed_data(
            (
                json.dumps(
                    {
                        "method": "_kiro.dev/subagent/list_update",
                        "params": {"subagents": [{"sessionId": "child-a"}]},
                    }
                )
                + "\n"
            ).encode()
        )
        rt.mark_turn_active("parent-session", True)  # owner has in-flight prompt
        reader.feed_data(
            (
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 88,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": "child-a",
                            "toolCall": {"toolCallId": "tc-88", "title": "Running: x"},
                            "options": [
                                {
                                    "optionId": "reject_once",
                                    "name": "Reject",
                                    "kind": "reject_once",
                                }
                            ],
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        # Let the reader route the frame.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if not queues["parent-session"].empty():
                break
        routed = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_ROUTED]
        assert routed == [{"surface": "runtime"}]
        assert not queues["parent-session"].empty()  # actually delivered
    finally:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


@pytest.mark.asyncio
async def test_subagent_child_reject_emits_denied(recorded):
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
    from kiro_crew.subagent import SubagentManager

    client = MagicMock()

    async def _noop(_rid):
        return None

    client.reject_tool = _noop
    ev = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        request_id=9,
        title="t",
        sub_session_id="child-a",
    )
    with patch("kiro_crew.subagent.sel"):
        await SubagentManager._reject_and_log(client, 9, "k", ev, error="child_escalation_limit")
    hits = [a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]
    assert {"surface": "subagent", "reason": "child_escalation_limit"} in hits
    # Parent-origin rejections do NOT emit (child series only).
    ev2 = LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=10, title="t")
    with patch("kiro_crew.subagent.sel"):
        await SubagentManager._reject_and_log(client, 10, "k", ev2, error="hook_deny")
    assert len([a for n, a in recorded if n == metric_events.CHILD_PERMISSION_DENIED]) == 1


def test_dashboard_ceiling_emits_timeout_cause(recorded):
    """The 2h-ceiling path cancels _run_chat before EVENT_COMPLETE, so the
    in-turn emit never fires — finish_turn_task (the confirmed-deadline
    callback) must emit the cause metric from the slot's stashed snapshot.
    (Its lazy `from metrics.events import emit_counter` picks up the
    `recorded` fixture's patch at call time.)"""
    from kiro_crew.dashboard import turn_dispatch

    class _DoneTask:
        def cancelled(self):
            return False

        def exception(self):
            return TimeoutError("turn exceeded the 7200s ceiling")

    state = MagicMock()
    state._background_tasks = set()
    slot = MagicMock()
    slot.key = "chat-1"
    slot._last_turn_awaiting_permission = True
    slot._last_turn_children_announced = True

    # finish_turn_task imports emit_counter lazily from metrics.events; the
    # `recorded` fixture already patched that module's symbol.
    turn_dispatch.finish_turn_task(state, slot, _DoneTask(), 7200.0)

    hits = [a for n, a in recorded if n == metric_events.TURN_TIMEOUT_CAUSE]
    assert hits == [
        {
            "path": "dashboard_ceiling",
            "awaiting_permission": True,
            "children_announced": True,
        }
    ]


def test_terminal_children_do_not_count_as_announced():
    """Completed (terminal) tracker entries must not count toward hang
    attribution: only children still unfinished at the cut may set
    children_announced. Exercises the real close_all safety net to pin the
    hazard: it force-marks every entry done, so the snapshot MUST be taken
    before it runs (terminal entries also linger for reconnect replay)."""
    from kiro_crew.dashboard import chat_runner

    state = MagicMock()
    slot = MagicMock()
    slot.key = "chat-1"

    # Case 1: all children completed long before the cut -> not announced.
    tracker = {"child-done": {"done": True, "done_at": 1.0, "id": "native:a"}}
    unfinished = any(not i.get("done") for i in tracker.values())
    assert unfinished is False

    # Case 2: a child is still live at the cut -> announced, and the
    # close_all safety net must not retroactively erase that (it marks the
    # entry done, which is exactly why the snapshot precedes it).
    tracker["child-live"] = {"done": False, "started": 0.0}
    unfinished = any(not i.get("done") for i in tracker.values())
    assert unfinished is True
    chat_runner._native_subagent_close_all(state, slot, tracker, None)
    assert all(i.get("done") for i in tracker.values())
    assert unfinished is True  # snapshot unaffected by close_all
