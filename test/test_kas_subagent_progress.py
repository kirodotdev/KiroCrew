"""KAS native sub-agent progress (Group B).

KAS delivers sub-agent lifecycle as ``session/update`` discriminants carrying
``_meta.kiro.agentSubtaskId`` (individual) or ``_meta.kiro.pipeline`` (pipeline).
These tests pin that the KAS-gated interception in
``AcpSessionHandle._handle_update`` routes them to EVENT_SUBAGENT_LIST /
EVENT_SUBAGENT_ACTIVITY, and that the kiro path is untouched.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    JsonRpcMessage,
)


def _handle(backend: str) -> AcpSessionHandle:
    """A handle over a fake runtime pinned to the given backend."""
    rt = MagicMock()
    rt.acp_backend = backend
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    return AcpSessionHandle("sB", asyncio.Queue(), rt)


def _update(handle: AcpSessionHandle, update: dict) -> list:
    return handle._handle_update(
        JsonRpcMessage(method="session/update", params={"update": update})
    )


# ── Individual agent-subtask spawn → EVENT_SUBAGENT_LIST ─────────────────────


def test_individual_agent_subtask_emits_subagent_list() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    assert events[0].subagents is not None
    assert len(events[0].subagents) == 1
    entry = events[0].subagents[0]
    assert entry["sessionId"] == "sa-1"
    assert entry["sessionName"] == "Sub-agent: researcher"
    assert entry["agentName"] == "researcher"
    assert entry["initialQuery"] == "Sub-agent: researcher"
    assert entry["status"]["type"] == "in_progress"


# ── Completed frame → entry completed ────────────────────────────────────────


def test_completed_agent_subtask_updates_roster() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # Spawn
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: coder",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-2"}},
    })
    # Complete
    events = _update(handle, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc1",
        "title": "Sub-agent: coder",
        "status": "completed",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-2"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    entry = events[0].subagents[0]
    assert entry["sessionId"] == "sa-2"
    assert entry["status"]["type"] == "completed"


# ── Pipeline frame → one entry per stage ─────────────────────────────────────


def test_pipeline_frame_creates_entries_per_stage() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-pipe",
        "title": "Pipeline",
        "_meta": {"kiro": {"pipeline": {
            "groupId": "g1",
            "stages": [
                {"name": "research", "role": "researcher", "status": "in_progress",
                 "dependsOn": [], "agentSubtaskId": "ps-1"},
                {"name": "code", "role": "coder", "status": "pending",
                 "dependsOn": ["ps-1"], "agentSubtaskId": "ps-2"},
            ],
        }}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_LIST
    assert len(events[0].subagents) == 2
    ids = {e["sessionId"] for e in events[0].subagents}
    assert ids == {"ps-1", "ps-2"}
    statuses = {e["sessionId"]: e["status"]["type"] for e in events[0].subagents}
    assert statuses["ps-1"] == "in_progress"
    assert statuses["ps-2"] == "pending"


# ── Child nested tool_call → activity prefix + cache populated + tool event ──


def test_child_nested_tool_call_emits_activity() -> None:
    """A child nested tool_call emits ONLY activity (not a top-level tool event)
    yet still populates the security caches via a side-effect parser call."""
    from kiro_crew.acp.types import EVENT_TOOL_CALL

    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-tc-1",
        "title": "read_file src/main.py",
        "status": "in_progress",
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    # Activity only — the tool must NOT surface as a top-level tool call.
    activity_events = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
    tool_events = [e for e in events if e.kind == EVENT_TOOL_CALL]
    assert len(activity_events) == 1
    assert activity_events[0].sub_session_id == "sa-1"
    assert activity_events[0].tool_call_id == "child-tc-1"
    assert activity_events[0].title == "read_file src/main.py"
    assert tool_events == []
    # The shell cache is deliberately NOT populated: this update carried no
    # `kind`, so the classification is UNRESOLVED. Caching the miss-default
    # False here would let the later permission frame read it as a RESOLVED
    # non-shell (shell_classified=True) and skip the low-fidelity downgrade
    # without any classification having happened.
    # (Cache keys are origin-scoped: "<frame sessionId>|<toolCallId>".)
    assert "sB|child-tc-1" not in handle._tool_call_is_shell
    assert "child-tc-1" not in handle._tool_call_is_shell
    # With a usable `kind`, the side-effect parse DOES populate the cache.
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "child-tc-2",
        "title": "run tests",
        "kind": "execute",
        "status": "in_progress",
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    assert handle._tool_call_is_shell.get("sB|child-tc-2") is True


# ── Child agent_message_chunk → EVENT_SUBAGENT_ACTIVITY w/ redacted text ─────


def test_child_agent_message_chunk_emits_activity() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Working on the fix..."},
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_ACTIVITY
    assert events[0].sub_session_id == "sa-1"
    assert events[0].text == "Working on the fix..."


# ── Kiro-backend parity: KAS-shaped frame NOT intercepted on kiro ────────────


def test_kas_subagent_frame_not_intercepted_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    # On kiro, a tool_call with _meta.kiro.agentSubtaskId falls through to the
    # shared parser (no EVENT_SUBAGENT_LIST, just a normal tool_call event).
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
    })
    # Should NOT produce EVENT_SUBAGENT_LIST — kiro path processes it as a
    # normal tool call.
    subagent_events = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
    assert subagent_events == []


def test_kas_subagent_chunk_not_intercepted_on_kiro_backend() -> None:
    handle = _handle(ACP_BACKEND_KIRO)
    events = _update(handle, {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "hello"},
        "_meta": {"kiro": {"agentSubtaskId": "sa-1"}},
    })
    # On kiro, this is just a regular text chunk, NOT subagent activity.
    assert len(events) == 1
    assert events[0].kind == EVENT_TEXT_CHUNK
    assert events[0].text == "hello"


# ── Failed status in roster ──────────────────────────────────────────────────


def test_failed_agent_subtask_updates_roster() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: builder",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-3"}},
    })
    events = _update(handle, {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "tc1",
        "title": "Sub-agent: builder",
        "status": "failed",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-3"}},
    })
    assert events[0].subagents[0]["status"]["type"] == "failed"


# ── No agentSubtaskId → normal tool_call pass-through ────────────────────────


def test_normal_tool_call_not_intercepted_on_kas() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-normal",
        "title": "read_file",
        "status": "in_progress",
        "_meta": {"kiro": {}},
    })
    # Falls through to parse_session_update → normal EVENT_TOOL_CALL
    from kiro_crew.acp.types import EVENT_TOOL_CALL
    assert any(e.kind == EVENT_TOOL_CALL for e in events)


# ── Regression: roster reset per turn (BLOCKER) ──────────────────────────────


def _status_of(events: list, sid: str) -> str | None:
    for ev in events:
        for entry in (ev.subagents or []):
            if entry.get("sessionId") == sid:
                return entry["status"]["type"]
    return None


def test_roster_cleared_between_turns_excludes_prior_completed() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    # Turn 1: sub-agent A spawns then completes.
    _update(handle, {"sessionUpdate": "tool_call", "toolCallId": "tcA", "title": "Sub-agent: A",
                     "status": "in_progress", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "A"}}})
    _update(handle, {"sessionUpdate": "tool_call_update", "toolCallId": "tcA", "title": "Sub-agent: A",
                     "status": "completed", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "A"}}})
    assert "A" in handle._kas_subagent_roster
    # Turn boundary: prompt()'s per-turn reset block clears the roster (parity
    # with kiro-cli's authoritative full list each turn).
    handle._kas_subagent_roster.clear()
    # Turn 2: a new sub-agent B — its LIST must NOT resurrect the completed A.
    events = _update(handle, {"sessionUpdate": "tool_call", "toolCallId": "tcB", "title": "Sub-agent: B",
                              "status": "in_progress", "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "B"}}})
    ids = {e.get("sessionId") for ev in events for e in (ev.subagents or [])}
    assert ids == {"B"}


# ── Regression: child thinking chunk not surfaced (BLOCKER) ──────────────────


def test_child_thinking_chunk_not_surfaced() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "agent_message_chunk",
                              "_meta": {"kiro": {"agentSubtaskId": "c1"}},
                              "content": {"type": "thinking", "text": "private reasoning"}})
    assert events == []


def test_child_text_chunk_is_surfaced() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    events = _update(handle, {"sessionUpdate": "agent_message_chunk",
                              "_meta": {"kiro": {"agentSubtaskId": "c1"}},
                              "content": {"type": "text", "text": "hello from child"}})
    assert len(events) == 1
    assert events[0].kind == EVENT_SUBAGENT_ACTIVITY
    assert events[0].sub_session_id == "c1"
    assert events[0].text == "hello from child"


# ── Pipeline stage status transition reflected in the list ───────────────────


def test_pipeline_stage_status_transition() -> None:
    handle = _handle(ACP_BACKEND_KAS)
    stage_running = {"name": "s1", "role": "r1", "status": "in_progress", "agentSubtaskId": "st1"}
    stage_done = {"name": "s1", "role": "r1", "status": "completed", "agentSubtaskId": "st1"}
    _update(handle, {
        "sessionUpdate": "tool_call", "toolCallId": "tcP", "title": "Orchestrate Sub-agent",
        "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [stage_running]}}},
    })
    events = _update(handle, {
        "sessionUpdate": "tool_call_update", "toolCallId": "tcP",
        "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [stage_done]}}},
    })
    assert _status_of(events, "st1") == "completed"


# ── Per-spawn SEL audit on the KAS roster path (GPT 5.6 F1) ──────────────────
# A KAS-auto-approved use_subagent spawn is answered by the backend and raises
# no permission request, so the CLI/dashboard invocation audit never runs. This
# routed PARENT roster path is the one per-spawn lifecycle signal that provably
# belongs to THIS session, so AcpSessionHandle records each new spawn here, once.

def _capture_sel():
    """A fake sel() whose log_api_access appends kwargs to the returned list."""
    import types as _t

    events: list = []
    return events, (lambda: _t.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)))


def test_each_kas_spawn_is_audited_once() -> None:
    from unittest.mock import patch

    handle = _handle(ACP_BACKEND_KAS)
    events, fake_sel = _capture_sel()
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        # Spawn sa-1, then a same-id status update (re-emits the roster).
        _update(handle, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc1",
            "title": "Sub-agent: researcher",
            "status": "in_progress",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
        })
        _update(handle, {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "tc1",
            "title": "Sub-agent: researcher",
            "status": "completed",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
        })
        # A genuinely new spawn, sa-2.
        _update(handle, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc2",
            "title": "Sub-agent: coder",
            "status": "in_progress",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-2"}},
        })

    spawn = [e for e in events if e["operation"] == "spawn_invocation_observed"]
    # sa-1 audited once despite two frames; sa-2 audited once → 2 total.
    assert len(spawn) == 2
    audited = " ".join(e["resources"] for e in spawn)
    assert "sa-1" in audited and "sa-2" in audited
    assert all(e["source"] == "kas_subagent_roster" for e in spawn)
    # The roster frame does NOT reveal how the spawn was approved: a ceiling can
    # send a KAS spawn through a manual permission prompt. The record must not
    # assert an approval route it does not know, or a manual approval is filed as
    # auto-approved (audit-integrity bug).
    assert all("auto-approve" not in e["resources"].lower() for e in spawn)


def test_pipeline_stages_each_audited_once() -> None:
    from unittest.mock import patch

    handle = _handle(ACP_BACKEND_KAS)
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-pipe",
        "title": "Pipeline",
        "_meta": {"kiro": {"pipeline": {
            "groupId": "g1",
            "stages": [
                {"name": "research", "role": "researcher", "status": "in_progress",
                 "agentSubtaskId": "ps-1"},
                {"name": "code", "role": "coder", "status": "in_progress",
                 "agentSubtaskId": "ps-2"},
            ],
        }}},
    }
    events, fake_sel = _capture_sel()
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        _update(handle, frame)
        _update(handle, frame)  # identical re-send: no new audits

    spawn = [e for e in events if e["operation"] == "spawn_invocation_observed"]
    assert len(spawn) == 2
    assert {"ps-1", "ps-2"} <= set(" ".join(e["resources"] for e in spawn).split())


def test_a_failed_audit_is_retried_on_the_next_roster_frame() -> None:
    # A sink error must not permanently mark the spawn audited: the sid is added
    # to the dedup set only after log_api_access succeeds, so the next roster
    # frame retries it. (A failed audit that marked the sid done would drop the
    # record forever.)
    import types as _t
    from unittest.mock import patch

    events: list = []
    calls = {"n": 0}

    def _log(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("sink down")
        events.append(kw)

    fake_sel = lambda: _t.SimpleNamespace(log_api_access=_log)  # noqa: E731

    handle = _handle(ACP_BACKEND_KAS)
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc1",
        "title": "Sub-agent: researcher",
        "status": "in_progress",
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
    }
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        _update(handle, frame)  # first attempt raises (swallowed), sid NOT marked
        _update(handle, frame)  # retry: succeeds this time

    spawn = [e for e in events if e["operation"] == "spawn_invocation_observed"]
    assert len(spawn) == 1 and "sa-1" in spawn[0]["resources"]


def test_pipeline_audit_records_the_stage_role_not_its_name() -> None:
    # A pipeline stage carries a distinct name and role; the audit must record
    # the ROLE, not the stage name (audit-accuracy).
    from unittest.mock import patch

    handle = _handle(ACP_BACKEND_KAS)
    events, fake_sel = _capture_sel()
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        _update(handle, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-pipe",
            "title": "Pipeline",
            "_meta": {"kiro": {"pipeline": {
                "groupId": "g1",
                "stages": [
                    {"name": "stage-alpha", "role": "researcher",
                     "status": "in_progress", "agentSubtaskId": "ps-1"},
                ],
            }}},
        })
    spawn = [e for e in events if e["operation"] == "spawn_invocation_observed"]
    assert len(spawn) == 1
    res = spawn[0]["resources"]
    assert "researcher" in res, f"stage role must be audited, got: {res}"
    assert "role stage-alpha" not in res, "stage NAME must not be recorded as the role"


def test_kas_spawn_audit_runs_off_the_event_loop() -> None:
    # sel() does blocking filesystem work on first use (SEL init); doing it
    # inline on the ACP event loop stalls the loop. When a loop is running the
    # audit must be offloaded (asyncio.to_thread), i.e. run on a DIFFERENT thread
    # than the loop.
    import asyncio
    import threading
    import types as _t
    from unittest.mock import patch

    seen: dict = {}

    def _log(**kw):
        seen["thread"] = threading.get_ident()

    async def _run():
        loop_thread = threading.get_ident()
        handle = _handle(ACP_BACKEND_KAS)
        with patch("kiro_crew.acp.session_handle.sel",
                   lambda: _t.SimpleNamespace(log_api_access=_log)):
            _update(handle, {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc1",
                "title": "Sub-agent: researcher",
                "status": "in_progress",
                "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
            })
            # Drain the offloaded audit task(s) before asserting.
            if handle._audit_tasks:
                await asyncio.gather(*list(handle._audit_tasks))
        return seen.get("thread"), loop_thread

    audit_thread, loop_thread = asyncio.run(_run())
    assert audit_thread is not None, "audit never ran"
    assert audit_thread != loop_thread, "audit ran ON the event-loop thread (must offload)"


def test_pending_pipeline_stage_is_not_audited_until_it_starts() -> None:
    # A pipeline announces stages before they run. A `pending` stage has not
    # spawned yet (it may never — it can be cancelled), so auditing it as
    # observed-spawned is a false record. Audit only once it leaves `pending`.
    from unittest.mock import patch

    handle = _handle(ACP_BACKEND_KAS)

    def _pipe(ps2_status):
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-pipe",
            "title": "Pipeline",
            "_meta": {"kiro": {"pipeline": {"groupId": "g1", "stages": [
                {"name": "research", "role": "researcher", "status": "in_progress",
                 "agentSubtaskId": "ps-1"},
                {"name": "code", "role": "coder", "status": ps2_status,
                 "agentSubtaskId": "ps-2"},
            ]}}},
        }

    events, fake_sel = _capture_sel()
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        _update(handle, _pipe("pending"))   # ps-2 pending → only ps-1 audited
        first = [e for e in events if e["operation"] == "spawn_invocation_observed"]
        assert len(first) == 1 and "ps-1" in first[0]["resources"]
        assert "ps-2" not in " ".join(e["resources"] for e in first)
        _update(handle, _pipe("in_progress"))  # ps-2 now started → audited
        after = [e for e in events if e["operation"] == "spawn_invocation_observed"]
    assert len(after) == 2
    assert "ps-2" in " ".join(e["resources"] for e in after)


def test_audit_sink_failure_does_not_break_roster_emission() -> None:
    from unittest.mock import patch

    def _broken():
        raise RuntimeError("no sink")

    handle = _handle(ACP_BACKEND_KAS)
    with patch("kiro_crew.acp.session_handle.sel", _broken):
        events = _update(handle, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc1",
            "title": "Sub-agent: researcher",
            "status": "in_progress",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
        })
    # The roster event the UI depends on is still emitted despite the sink error.
    assert len(events) == 1 and events[0].kind == EVENT_SUBAGENT_LIST


def test_kiro_backend_spawn_is_not_audited() -> None:
    # On the kiro (non-KAS) backend the KAS interception does not run, so no
    # spawn audit is emitted — the audit is specific to the KAS roster path.
    from unittest.mock import patch

    handle = _handle(ACP_BACKEND_KIRO)
    events, fake_sel = _capture_sel()
    with patch("kiro_crew.acp.session_handle.sel", fake_sel):
        _update(handle, {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc1",
            "title": "Sub-agent: researcher",
            "status": "in_progress",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-1"}},
        })
    assert [e for e in events if e["operation"] == "spawn_invocation_observed"] == []


def test_kas_spawn_audit_records_each_spawn_once_under_concurrent_frames() -> None:
    # The audit is deduped by sessionId, but the whole roster is rescanned on
    # EVERY frame. If the sid is only marked audited after log_api_access
    # returns, a second frame arriving while the first emit is still inside that
    # (slow, offloaded) sink call re-selects the same sid, and two worker threads
    # both clear the dedup check -> the same spawn is recorded twice, breaking
    # the "audited once per spawn" invariant. Reserve the sid on the event loop
    # so the second frame cannot re-select it.
    import asyncio
    import threading
    import types as _t
    from unittest.mock import patch

    in_sink = threading.Event()
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def _log(**kw):
        with lock:
            calls.append(str(kw.get("resources") or ""))
        # Park inside the sink so the reservation can be observed mid-flight.
        in_sink.set()
        release.wait(5)

    def _frame(tc: str):
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": tc,
            "title": "Sub-agent: researcher",
            "status": "in_progress",
            "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sa-race"}},
        }

    async def _run():
        handle = _handle(ACP_BACKEND_KAS)
        with patch(
            "kiro_crew.acp.session_handle.sel",
            lambda: _t.SimpleNamespace(log_api_access=_log),
        ):
            _update(handle, _frame("tc1"))
            # Park the offloaded emit inside the sink, then observe the dedup set
            # WHILE it is in flight. Deterministic: no dependence on which worker
            # thread wins. Marking only after the sink returns leaves the sid
            # unreserved here, so the next frame re-selects it and double-records.
            await asyncio.to_thread(in_sink.wait, 5)
            reserved_mid_flight = "sa-race" in handle._audited_kas_spawn_sids
            # Second roster frame for the SAME spawn, still in flight.
            _update(handle, _frame("tc2"))
            release.set()
            if handle._audit_tasks:
                await asyncio.gather(*list(handle._audit_tasks))
        return reserved_mid_flight, calls

    reserved_mid_flight, got = asyncio.run(_run())
    assert reserved_mid_flight, (
        "sid was not reserved on the event loop before the sink returned, so a "
        "concurrent frame can re-select it and record the spawn twice"
    )
    spawn_rows = [r for r in got if "sa-race" in r]
    assert len(spawn_rows) == 1, (
        f"spawn recorded {len(spawn_rows)}x, must be exactly once: {spawn_rows}"
    )
