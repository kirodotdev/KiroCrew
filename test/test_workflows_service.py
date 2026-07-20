"""M6.3/M6.4 — WorkflowService façade (author / start / status / result / cancel).

This is the gateway-side object the chat workflow_* MCP tools and the Workflows
tab both talk to. Asserts:
  * author: NL intent → validated script (retries on invalid model output, fails clean)
  * start: validates + launches a background run, returns run_id, result captured
  * on_done fires with the originating session_key (M6.4 routes result→chat)
  * status/result/list/cancel reach the shared registry

All against fakes — no real model/kiro-cli. ``stream_and_collect`` is patched.
See GATES (M6) and docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import asyncio

import pytest

import kiro_crew.llm_helpers as llm_helpers
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

GOOD_SCRIPT = (
    'META = {"name": "demo", "description": "d"}\n'
    "async def workflow(ctx):\n"
    "    ctx.log('hi')\n"
    "    return {'ok': True}\n"
)


class FakeProvider:
    def __init__(self, scripted: list[str]) -> None:
        self._scripted = scripted
        self._i = 0


class FakeSessions:
    def __init__(self, scripted: list[str]) -> None:
        self._scripted = scripted
        self.released: list[str] = []
        self.acquired: list[tuple[str, dict]] = []  # (key, kwargs) per get_or_create
        self.cleaned: list[str] = []  # keys released with cleanup=True

    async def get_or_create(self, key, **kw):
        self.acquired.append((key, kw))
        return FakeProvider(self._scripted), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)
        if cleanup:
            self.cleaned.append(key)


def _patch_stream(monkeypatch, replies: list[str]) -> dict:
    """Patch stream_and_collect to return successive canned replies."""
    state = {"i": 0}

    async def fake_stream(provider, message, **kw):
        r = replies[min(state["i"], len(replies) - 1)]
        state["i"] += 1
        return r

    monkeypatch.setattr(llm_helpers, "stream_and_collect", fake_stream)
    # service.py binds stream_and_collect at module top (top-level-imports rule),
    # so patch the name in the service module's namespace too.
    import kiro_crew.workflows.service as svc_mod

    monkeypatch.setattr(svc_mod, "stream_and_collect", fake_stream)
    return state


async def _wait_terminal(svc: WorkflowService, run_id: str, timeout: float = 3.0):
    t = 0.0
    while t < timeout:
        snap = svc.status(run_id)
        if snap and snap["status"] != "running":
            return snap
        await asyncio.sleep(0.02)
        t += 0.02
    raise AssertionError("run did not finish")


# --------------------------------------------------------------------------- #
# author
# --------------------------------------------------------------------------- #


async def test_author_returns_valid_script(monkeypatch) -> None:
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("do a tiny thing")
    assert out["ok"] is True


async def test_author_uses_isolated_torn_down_lite_session(monkeypatch) -> None:
    """Separation of concerns: authoring runs in a FRESH, ISOLATED, ephemeral
    session (never the shared _bg), on the tool-less kirocrew-lite agent, and tears
    it down after — so a workflow's authoring never pollutes (or is polluted by)
    chat/consolidation/other runs, while staying cheap (no MCP toolset load)."""
    _patch_stream(monkeypatch, [GOOD_SCRIPT])
    sessions = FakeSessions([])
    svc = WorkflowService(sessions=sessions)
    await svc.author("do a tiny thing")
    assert len(sessions.acquired) == 1
    key, kw = sessions.acquired[0]
    # NOT the shared background session
    assert key != "_bg" and key.startswith("wf-author:")
    # tool-less lite agent
    assert kw.get("agent") == "kirocrew-lite"
    # torn down (cleanup=True) — nothing persists between runs
    assert key in sessions.cleaned


async def test_author_retries_then_succeeds(monkeypatch) -> None:
    # first reply invalid (import), second valid → author must retry and succeed
    _patch_stream(monkeypatch, ["import os\n" + GOOD_SCRIPT, GOOD_SCRIPT])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is True


async def test_author_all_invalid_fails_clean(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["import os\nasync def workflow(ctx):\n    return 1\n"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is False
    assert out["errors"]


async def test_author_strips_code_fence(monkeypatch) -> None:
    fenced = "```python\n" + GOOD_SCRIPT + "```"
    _patch_stream(monkeypatch, [fenced])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.author("x")
    assert out["ok"] is True
    assert "```" not in out["source"]


# --------------------------------------------------------------------------- #
# start / status / result / on_done / cancel
# --------------------------------------------------------------------------- #


async def test_start_launches_run_and_injects_on_done(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])  # the workflow's ctx.agent uses this
    done: list[dict] = []
    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_done=lambda rid, snap: done.append({"rid": rid, **snap}),
    )
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="slot:main")
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished"
    assert snap["result"] == {"ok": True}
    # M6.4: on_done carried the originating session so the result routes to chat
    await asyncio.sleep(0.02)
    assert done and done[0]["session_key"] == "slot:main"


async def test_start_rejects_invalid_script() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start("import os\n")
    assert "error" in out and "run_id" not in out


async def test_result_and_list(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start(GOOD_SCRIPT, name="demo")
    rid = out["run_id"]
    await _wait_terminal(svc, rid)
    full = svc.result(rid)
    assert full["run_id"] == rid and "events" in full
    runs = svc.list_runs()
    assert any(r["run_id"] == rid for r in runs)


async def test_run_ids_are_deterministic_monotonic() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    a = svc._new_run_id()
    b = svc._new_run_id()
    assert a == "wf_000001" and b == "wf_000002"


async def test_concurrency_cap_flows_to_runner() -> None:
    """The service's concurrency cap must reach the runner (and thus bound
    parallel/pipeline fan-out) — without it, a fan-out workflow runs every agent
    at once and can overload the box."""
    svc = WorkflowService(sessions=FakeSessions([]), concurrency=5)
    runner = svc._runner("wf_x")
    assert runner._concurrency == 5


# --------------------------------------------------------------------------- #
# M6.7 — start_from_intent: author INSIDE the run (no synchronous-author block)
# --------------------------------------------------------------------------- #


async def test_start_from_intent_returns_run_id_then_authors_and_runs(monkeypatch) -> None:
    """workflow_run(intent=…) path: returns a run_id immediately, then the run
    authors its own script (visible Authoring phase) and executes to completion."""
    _patch_stream(monkeypatch, [GOOD_SCRIPT])  # authoring reply
    events: list[dict] = []
    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_event=lambda rid, ev: events.append(ev),
    )
    out = await svc.start_from_intent("do a tiny thing", session_key="slot:main")
    # run_id is returned right away — authoring has NOT blocked this call.
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "finished"
    assert snap["result"] == {"ok": True}
    # The stream shows an Authoring phase before the workflow body.
    titles = [e["data"]["title"] for e in events if e["type"] == "phase_started"]
    assert "Authoring" in titles
    # The authored source is persisted on the handle (for rerun/restart).
    h = svc.registry.get(out["run_id"])
    assert h is not None and "async def workflow" in h.source


async def test_start_from_intent_authoring_failure_is_failed_run(monkeypatch) -> None:
    """If the model never yields a valid script, the run ends 'failed' (not a
    crash, not a hang) with the authoring errors recorded."""
    _patch_stream(monkeypatch, ["import os\nasync def workflow(ctx):\n    return 1\n"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start_from_intent("nonsense")
    assert "run_id" in out
    snap = await _wait_terminal(svc, out["run_id"])
    assert snap["status"] == "failed"


async def test_start_from_intent_requires_intent() -> None:
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start_from_intent("   ")
    assert "error" in out and "run_id" not in out


# --------------------------------------------------------------------------- #
# View source + edit-and-rerun (FIX-11): snapshot exposes source; rerun_subtree
# can run an edited script (validated, fresh — no stale prefix replay).
# --------------------------------------------------------------------------- #


async def test_full_snapshot_exposes_source(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    out = await svc.start(GOOD_SCRIPT, name="demo")
    rid = out["run_id"]
    await _wait_terminal(svc, rid)
    full = svc.result(rid)  # full snapshot (include_events=True)
    assert "async def workflow" in full["source"]
    # compact list view stays light — no source there
    assert "source" not in svc.list_runs()[0]


async def test_rerun_with_edited_source_runs_fresh(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    rid = (await svc.start(GOOD_SCRIPT, name="demo"))["run_id"]
    await _wait_terminal(svc, rid)
    edited = (
        'META = {"name": "demo2", "description": "edited"}\n'
        "async def workflow(ctx):\n"
        "    ctx.log('edited run')\n"
        "    return {'edited': True}\n"
    )
    out = await svc.rerun_subtree(rid, 0, source=edited)
    assert out.get("edited") is True and out["replayed_before"] == 0
    new_rid = out["run_id"]
    snap = await _wait_terminal(svc, new_rid)
    assert snap["status"] == "finished" and snap["result"] == {"edited": True}


async def test_rerun_with_invalid_edited_source_rejected(monkeypatch) -> None:
    _patch_stream(monkeypatch, ["stub"])
    svc = WorkflowService(sessions=FakeSessions([]))
    rid = (await svc.start(GOOD_SCRIPT, name="demo"))["run_id"]
    await _wait_terminal(svc, rid)
    out = await svc.rerun_subtree(rid, 0, source="import os\n")
    assert "errors" in out and "run_id" not in out


# --------------------------------------------------------------------------- #
# Integration: a finished chat-linked run must (1) inject its result into the
# originating slot AND (2) auto-run an agent turn so the launching agent actually
# interprets the result. Drives the REAL WorkflowService -> runner -> on_done ->
# inject_workflow_result(on_injected=...) wiring; only _run_chat is stubbed (no
# model). Regression for "workflow result never reaches the agent to interpret".
# --------------------------------------------------------------------------- #


class _IntgSlot:
    """Slot double exposing the real enqueue-or-run contract used by the auto-turn."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.messages: list[dict] = []
        self.linked_session_key = ""
        self.title = ""
        self.running = False
        self.turns: list[str] = []  # prompts that started an agent turn

    def append(self, role, content, cls="", ts="", *, broadcast=True, meta=None):
        self.messages.append({"role": role, "content": content})

    def enqueue_or_run_prompt(self, prompt, run_chat_coro, state) -> bool:
        # Mirror the real state.py primitive: busy -> queue (False), else run (True).
        if self.running:
            return False
        self.append("user", prompt, "msg msg-u")
        self.turns.append(prompt)
        return True


class _IntgState:
    def __init__(self, slots) -> None:
        self._slots = dict(slots)
        self.conversation_log = None
        self.broadcasts: list = []
        self.slots_pushed = 0

    def get_slot(self, name):
        return self._slots.get(name)

    def get_or_create_slot(self, name, **kw):
        self._slots.setdefault(name, _IntgSlot(name))
        return self._slots[name]

    def broadcast_ws(self, kind, payload):
        self.broadcasts.append((kind, payload))

    def push_slots_update(self):
        self.slots_pushed += 1


async def test_finished_run_injects_result_and_autoruns_agent_turn(monkeypatch) -> None:
    from kiro_crew.dashboard.workflow_inject import inject_workflow_result

    _patch_stream(monkeypatch, ["stub"])
    origin = _IntgSlot("chat-1")
    dstate = _IntgState({"chat-1": origin})

    # Reproduce the gateway's _wf_on_done: inject, and on a fresh originating
    # inject, start an agent turn via the slot's enqueue-or-run primitive.
    def _auto_turn(slot, snap):
        prompt = f"[Workflow `{snap.get('name')}` finished] interpret the result above."
        slot.enqueue_or_run_prompt(prompt, lambda s, sl, m: None, dstate)
        dstate.push_slots_update()

    def _on_done(rid, snap):
        inject_workflow_result(dstate, rid, snap, on_injected=_auto_turn)

    svc = WorkflowService(sessions=FakeSessions([]), on_done=_on_done)
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="dashboard:chat-1")
    await _wait_terminal(svc, out["run_id"])
    await asyncio.sleep(0.05)  # let on_done fire

    # (1) result summary injected as an assistant message into the ORIGINATING slot
    assert any(m["role"] == "assistant" and "demo" in m["content"] for m in origin.messages)
    # (2) an agent turn was auto-started with a user-role prompt to interpret it
    assert len(origin.turns) == 1, origin.turns
    assert any(m["role"] == "user" and "interpret" in m["content"] for m in origin.messages)
    assert dstate.slots_pushed >= 1


async def test_finished_run_busy_slot_queues_turn(monkeypatch) -> None:
    """If the originating slot is mid-turn, the auto-turn queues (does not start),
    so we never stack a concurrent turn — mirrors enqueue_or_run_prompt semantics."""
    from kiro_crew.dashboard.workflow_inject import inject_workflow_result

    _patch_stream(monkeypatch, ["stub"])
    origin = _IntgSlot("chat-1")
    origin.running = True  # busy
    dstate = _IntgState({"chat-1": origin})
    started: list[bool] = []

    def _auto_turn(slot, snap):
        started.append(slot.enqueue_or_run_prompt("interpret", lambda s, sl, m: None, dstate))

    svc = WorkflowService(
        sessions=FakeSessions([]),
        on_done=lambda rid, snap: inject_workflow_result(dstate, rid, snap, on_injected=_auto_turn),
    )
    out = await svc.start(GOOD_SCRIPT, name="demo", session_key="dashboard:chat-1")
    await _wait_terminal(svc, out["run_id"])
    await asyncio.sleep(0.05)
    # Result still injected, but the turn was QUEUED (False), not started.
    assert any(m["role"] == "assistant" for m in origin.messages)
    assert started == [False]
    assert origin.turns == []


# --------------------------------------------------------------------------- #
# Warm-session pool wiring (loading-time win) — reachable in production?
# --------------------------------------------------------------------------- #


async def test_default_service_pools_agents() -> None:
    """The gateway constructs WorkflowService WITHOUT passing pool_agents
    (dashboard/server.py), so the pool must be ON by default. A pooled runner
    has an on_complete teardown (== pool.shutdown) wired; an un-pooled one does
    not. This guards the live wiring so the loading-time win can't silently
    regress to cold-start-per-call."""
    svc = WorkflowService(sessions=FakeSessions([]), concurrency=4)
    assert svc._pool_agents is True  # default engages in production
    runner = svc._runner("wf_probe")
    # on_complete is only set on the pooled path (service._runner wires pool.shutdown).
    assert runner._on_complete is not None


async def test_pool_agents_false_uses_per_call_sessions() -> None:
    """Opt-out restores the original per-call path (no pool teardown wired)."""
    svc = WorkflowService(sessions=FakeSessions([]), pool_agents=False)
    assert svc._pool_agents is False
    runner = svc._runner("wf_probe")
    assert runner._on_complete is None
