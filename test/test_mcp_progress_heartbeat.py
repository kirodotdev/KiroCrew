"""Kiro Crew's own long tools heartbeat while they wait.

A parked (deferred) call whose ``tools/call`` carried a ``_meta.progressToken`` gets
an MCP ``notifications/progress`` about every 30 s, naming its token and its current
phase, so a client that times calls by their silence does not read a call that is
only waiting as one that was lost. Each step also runs under the harness call id the
call was approved under, which the tool's ``/api/session-keepalive`` heartbeat names
(``mcp_caller.heartbeat_fields``), so the gateway restarts that call's own timer.

No test here waits on a fixed sleep: the loop's own outputs are the signals.
"""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest
from test_mcp_shared_deferred_tools import _LoopHarness, _Parked, _tools_call_named

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_caller import current_tool_call_id, heartbeat_fields, set_current_tool_call_id
from kiro_crew.mcp_shared import DeferredTool
from kiro_crew.mcp_shared_calls import _DeferredEntry, progress_heartbeat, request_progress_token


class _Phased(_Parked):
    """A parked call with a phase that records the call id each step runs under."""

    phase = "waiting_for_answer"

    def __init__(self, text: str, *, release: threading.Event) -> None:
        super().__init__(text, release=release)
        self.step_call_ids: list[str] = []

    def step(self) -> str | None:
        self.step_call_ids.append(current_tool_call_id())
        return super().step()


def _call_with_token(req_id: Any, tool_name: str, token: Any) -> dict:
    msg = _tools_call_named(req_id, tool_name)
    msg["params"]["_meta"] = {"progressToken": token}
    return msg


def _progress(sent: list[tuple[str, dict]]) -> list[dict]:
    return [p for m, p in sent if m == "notifications/progress"]


class TestProgressHeartbeat:
    def _notifications(self, monkeypatch) -> list[tuple[str, dict]]:
        sent: list[tuple[str, dict]] = []
        monkeypatch.setattr(mcp_shared, "notify", lambda m, p: sent.append((m, p)))
        # Every pass is due, so the beats are counted without waiting 30 s.
        monkeypatch.setattr(mcp_shared, "PROGRESS_HEARTBEAT_SECS", 0.0)
        return sent

    def test_a_parked_call_that_asked_for_progress_gets_a_growing_heartbeat(self, monkeypatch):
        sent = self._notifications(monkeypatch)
        release = threading.Event()
        parked = _Phased("answered", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "ask" else "ok")
        try:
            harness.send(_call_with_token(1, "ask", "tc-ask"))
            assert harness.wait_for(lambda: len(_progress(sent)) >= 3), "no heartbeat was sent"
            beats = _progress(sent)
            assert {b["progressToken"] for b in beats} == {"tc-ask"}
            assert {b["message"] for b in beats} == {"waiting_for_answer"}
            counts = [b["progress"] for b in beats]
            assert counts == sorted(counts) and len(set(counts)) == len(counts)
            # Each step ran under the call's own harness id (the token here,
            # with no gateway caller block).
            assert parked.step_call_ids and set(parked.step_call_ids) == {"tc-ask"}
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_a_call_with_no_progress_token_gets_no_heartbeat(self, monkeypatch):
        sent = self._notifications(monkeypatch)
        release = threading.Event()
        parked = _Phased("answered", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "ask" else "ok")
        try:
            harness.send(_tools_call_named(1, "ask"))
            assert harness.wait_for(lambda: parked.steps >= 3)
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _progress(sent) == []
        finally:
            release.set()
            harness.close()


class _Plain(DeferredTool):
    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        return None


def test_the_beat_is_paced_and_skips_a_stuck_or_cancelled_call():
    every = mcp_shared.PROGRESS_HEARTBEAT_SECS
    assert every == 30.0
    entry = _DeferredEntry(1, "wait", None, "", _Plain(), progress_token=7)
    first = entry.next_progress
    assert progress_heartbeat(entry, first - 1.0) is None
    assert progress_heartbeat(entry, first) == {
        "progressToken": 7,
        "progress": 1,
        "message": "running",
    }
    assert progress_heartbeat(entry, first + 1.0) is None
    # A step holding its thread past the stuck bound is not vouched for.
    entry.stepping = MagicMock()
    entry.step_started = first
    stuck = first + every + mcp_shared.DEFERRED_STEP_STUCK_SECS
    assert progress_heartbeat(entry, stuck) is None
    entry.stepping = None
    entry.cancelled = True
    assert progress_heartbeat(entry, stuck + every) is None
    entry.cancelled = False
    assert progress_heartbeat(entry, stuck + 2 * every)["progress"] == 2


def test_only_a_string_or_integer_token_is_a_progress_token():
    assert request_progress_token({"_meta": {"progressToken": "tc-1"}}) == "tc-1"
    assert request_progress_token({"_meta": {"progressToken": 4}}) == 4
    assert request_progress_token({"_meta": {"progressToken": True}}) is None
    assert request_progress_token({"_meta": {"progressToken": {"x": 1}}}) is None
    assert request_progress_token({"_meta": "x"}) is None
    assert request_progress_token({}) is None


def test_a_heartbeat_names_the_call_it_runs_under_and_its_phase():
    set_current_tool_call_id("tc-9")
    try:
        assert heartbeat_fields("poll") == {"tool_call_id": "tc-9", "phase": "poll"}
        assert heartbeat_fields("x" * 200)["phase"] == "x" * 64
    finally:
        set_current_tool_call_id("")
    assert heartbeat_fields("poll") == {"tool_call_id": "", "phase": "poll"}


def test_the_long_tools_report_their_phase():
    from kiro_crew.mcp_tools.browser import _AskWait
    from kiro_crew.mcp_tools.control import _AskStep, _WaitStep
    from kiro_crew.mcp_tools.spawn_parts.wave import _SubAgentsStep

    assert _WaitStep(seconds=60, reason_safe="", identified=False).progress_phase() == "waiting"
    assert _AskStep("sk", "ask-1", confirmed=True).progress_phase() == "waiting_for_answer"
    assert (
        _AskWait(ask_id="a", body_key="k", session_header="s").progress_phase()
        == "waiting_for_answer"
    )
    wave = _SubAgentsStep(
        sa_ids=["a1"],
        sa_errors=[],
        parent_session="dashboard:x",
        max_wait=60.0,
        inline_result=lambda _id, _text: "",
    )
    assert wave.progress_phase() == "poll"


def _keepalives(post: MagicMock) -> list[dict]:
    return [c.args[1] for c in post.call_args_list if c.args[0] == "/api/session-keepalive"]


def test_a_wave_beats_while_the_parent_waits_for_its_slot(monkeypatch, manual_clock):
    """The hold (a sub-agent parent waiting for its slot back after its children
    ended) beats like the poll does, naming no member: the call is working, and a
    hold that sent nothing would read as a lost call once its bound ran out."""
    import kiro_crew.mcp_core as mcp_core
    from kiro_crew.mcp_tools.spawn_parts import wave

    manual_clock.install(monkeypatch, mcp_core)
    post = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    step = wave._SubAgentsStep(
        sa_ids=["a1"],
        sa_errors=[],
        parent_session="subagent:p1",
        max_wait=3600.0,
        inline_result=lambda _id, _text: "",
    )
    step.wait_settled.add("a1")
    step.phase = "hold"
    probes: list[str] = []

    def probe() -> tuple[bool, None]:
        probes.append(step.phase)
        manual_clock.advance(wave.RESUME_HOLD_SECS)
        return False, None

    monkeypatch.setattr(step.hold, "probe", probe)
    set_current_tool_call_id("tc-wave")
    try:
        for _ in range(12):
            assert step.step() is None
    finally:
        set_current_tool_call_id("")

    beats = _keepalives(post)
    assert len(probes) == 12
    # Steps start every 8 s from 0 to 88 s and a beat is due 30 s after the last
    # one: beats at 32 and 64 s.
    assert beats == [{"tool_call_id": "tc-wave", "phase": "hold", "subagent_ids": []}] * 2


def test_spawn_run_beats_while_it_submits_its_batch(monkeypatch, manual_clock):
    """A spawn_run batch whose submissions are slow keeps naming its call."""
    import kiro_crew.mcp_core as mcp_core
    from kiro_crew.mcp_tools.spawn import _SpawnRunStep

    manual_clock.install(monkeypatch, mcp_core)
    post = MagicMock(return_value={"ok": True})
    monkeypatch.setattr(mcp_core, "_post", post)
    submitted: list[int] = []

    def submit_one(index: int) -> None:
        submitted.append(index)
        manual_clock.advance(20.0)

    step = _SpawnRunStep(
        task_count=4,
        agent_ids=[],
        transport_errors=[],
        submit_one=submit_one,
        receipt=lambda: "receipt",
        reconcile_lost=lambda _reason: True,
    )
    set_current_tool_call_id("tc-run")
    try:
        results = [step.step() for _ in range(4)]
    finally:
        set_current_tool_call_id("")

    assert results == [None, None, None, "receipt"]
    assert submitted == [0, 1, 2, 3]
    # Steps start at 0, 20, 40 and 60 s, and a beat is due 30 s after the last
    # one: a beat before the third step only (the fourth is 20 s after it).
    assert _keepalives(post) == [{"tool_call_id": "tc-run", "phase": "submit"}]


# ── Each long tool's keepalive names the call it runs under ──
#
# ``acp-client.md``: a Kiro Crew long tool's keepalive POST names the call
# (``heartbeat_fields``), which stamps that call alone. A beat naming no call
# stamps every call in flight on Kiro Crew's own servers instead, so a long wait's
# beats would keep a lost sibling Kiro Crew call alive: the ticket's shape, a lost
# call held by activity that is not its own.


#: The browser op's own timeout the hand-back carries; no step here waits on it.
_OP_TIMEOUT_S = 5.0


def _keepalive_step(tool: str, monkeypatch) -> tuple[DeferredTool, str]:
    """The parked step of *tool*, due to beat now, and the phase its beat carries."""
    from kiro_crew.mcp_tools import browser, control

    if tool == "wait":
        return control._WaitStep(seconds=600, reason_safe="ci", identified=True), "waiting"
    if tool == "ask_question":
        return control._AskStep("sk", "ask-1", confirmed=True), "waiting_for_answer"
    if tool == "browser-handback":
        monkeypatch.setattr(browser, "_post_handback", lambda *_a, **_k: (200, {}))
        step: Any = browser._HandbackWait(  # flake-ok: timeout_s is the op's, never waited on here
            op="click",
            op_args={},
            body_key="k",
            session_header="sk",
            timeout_s=_OP_TIMEOUT_S,
            interrupted=False,
            interrupted_text="",
        )
        step.next_keepalive = 0.0
        return step, "waiting_for_handback"
    monkeypatch.setattr(browser, "_post_ask_poll", lambda *_a, **_k: (200, {"done": False}))
    step = browser._AskWait(ask_id="a-1", body_key="k", session_header="sk")
    step.next_keepalive = 0.0
    return step, "waiting_for_answer"


@pytest.mark.parametrize("tool", ["wait", "ask_question", "browser-handback", "browser-ask"])
def test_each_long_tools_keepalive_names_the_call_it_runs_under(monkeypatch, tool):
    import kiro_crew.mcp_core as mcp_core

    posts: list[tuple[str, dict]] = []

    def _post(path: str, body: dict, **_kw: Any) -> dict:
        posts.append((path, dict(body)))
        return {"status": "pending"} if path.startswith("/api/agent-ask/") else {}

    monkeypatch.setattr(mcp_core, "_post", _post)
    step, phase = _keepalive_step(tool, monkeypatch)
    # What the dispatch loop installs for each step of the call (``mcp_shared_dispatch``).
    set_current_tool_call_id("tc-long")
    try:
        assert step.step() is None
    finally:
        set_current_tool_call_id("")

    beats = [body for path, body in posts if path == "/api/session-keepalive"]
    assert len(beats) == 1, posts
    assert beats[0]["tool_call_id"] == "tc-long"
    assert beats[0]["phase"] == phase


def test_a_wrapped_long_call_heartbeats_its_own_phase():
    """A wrapped step (a mapped result, an audited one, a hand-back that turned into
    an ask) reports the phase of the step it wraps, not the default ``running``."""
    from kiro_crew.mcp_shared_calls import _AuditedDeferred, _MappedDeferred
    from kiro_crew.mcp_tools import browser

    inner = _Phased("answered", release=threading.Event())
    assert _MappedDeferred(inner, lambda text: text).progress_phase() == "waiting_for_answer"
    assert _AuditedDeferred(inner, lambda _text: None).progress_phase() == "waiting_for_answer"
    handback = browser._HandbackWait(  # flake-ok: timeout_s is the op's, never waited on here
        op="ask",
        op_args={},
        body_key="k",
        session_header="sk",
        timeout_s=_OP_TIMEOUT_S,
        interrupted=False,
        interrupted_text="",
    )
    assert handback.progress_phase() == "waiting_for_handback"
    handback.asked = browser._AskWait(ask_id="a-1", body_key="k", session_header="sk")
    assert handback.progress_phase() == "waiting_for_answer"
