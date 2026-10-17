"""Starts on the shared ``_bg`` runtime never take a user session-start permit.

Every ``run_bg_oneliner`` (auto-titles, nav labels, folder icons, summaries, STT
endpointing) opens a fresh ``session/new`` on the one shared ``_bg`` runtime,
which answers them one at a time. Start priority alone does not keep those off
a person's path: titles fire on the first send and reach the gate before the
person's own start has finished spawning, so they take the permits first, and a
title holds its permit for as long as it waits inside the ``_bg`` process. They
therefore start under a gate of their own.

Drives the REAL ``AcpRuntime.create_session`` against a fake ``session/new``
(as ``test_session_start_gate`` does); nothing spawns kiro-cli.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_start_priority import bounded, settle_tasks, until
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.acp.runtime as runtime_mod
import kiro_crew.acp.runtime_start as runtime_start
import kiro_crew.config.live as live_mod
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import METHOD_SESSION_NEW
from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.start_priority import StartPriority


@pytest.fixture(autouse=True)
def _fresh_gates(monkeypatch):
    import kiro_crew.acp.session_handle as sh

    monkeypatch.setattr(sh, "_MCP_DRAIN_NO_REPORT_CEILING", 0.05, raising=False)
    runtime_mod._session_start_gates.clear()
    runtime_start._bg_runtime_session_start_gates.clear()
    # A one-permit user gate: the tightest case, where one permit held by a
    # ``_bg`` start stalls every person's start on the gateway.
    monkeypatch.setattr(live_mod, "snapshot", lambda: None)
    monkeypatch.setattr(runtime_mod, "_resolve_session_start_concurrency", lambda: 1)
    yield
    runtime_mod._session_start_gates.clear()
    runtime_start._bg_runtime_session_start_gates.clear()


def _make_runtime() -> AcpRuntime:
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdout = asyncio.StreamReader()
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    rt._expect_mcp_reports = False
    rt._session_start_timeout = 5.0
    rt._start_collect_timeout = 5.0
    return rt


def _blocking_session_new(rt: AcpRuntime, monkeypatch, release: dict[str, asyncio.Event]):
    """``session/new`` that answers only when the test sets its cwd's event."""
    sent: list[str] = []

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            cwd = str(params.get("cwd") or "")
            sent.append(cwd)
            await release[cwd].wait()
            return {"sessionId": f"sid-{cwd.strip('/')}"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)
    return sent


def _bg_start(rt: AcpRuntime, cwd: str, priority: StartPriority = StartPriority.BACKGROUND):
    return asyncio.create_task(
        rt.create_session(cwd=cwd, mcp_servers=[], bg_runtime_start=True, start_priority=priority)
    )


@pytest.mark.asyncio
async def test_bg_runtime_starts_on_the_wire_and_queued_leave_a_person_start_unblocked(
    monkeypatch,
):
    """Two ``_bg`` starts (one on the wire, one queued) leave the user gate
    untouched: a person's FOREGROUND start sends its ``session/new`` at once and
    finishes while both ``_bg`` starts are still outstanding. On the shared gate
    the person waited for the title holding the only permit, priority or not."""
    rt = _make_runtime()
    release = {"/bg1": asyncio.Event(), "/bg2": asyncio.Event(), "/user": asyncio.Event()}
    sent = _blocking_session_new(rt, monkeypatch, release)

    bg = [_bg_start(rt, "/bg1"), _bg_start(rt, "/bg2")]
    started = list(bg)
    try:
        await until(lambda: sent == ["/bg1"], "first _bg session/new sent")
        bg_gate = await runtime_start.bg_runtime_session_start_gate()
        user_gate = await runtime_mod.session_start_gate()
        assert bg_gate is not user_gate
        await until(lambda: bg_gate.queued == 1, "second _bg start queued")
        assert sent == ["/bg1"], "the _bg gate is one permit: the second _bg start queues"
        assert (bg_gate.active, bg_gate.queued) == (1, 1)
        assert (user_gate.active, user_gate.queued) == (0, 0)

        user = asyncio.create_task(
            rt.create_session(cwd="/user", mcp_servers=[], start_priority=StartPriority.FOREGROUND)
        )
        started.append(user)
        await until(lambda: "/user" in sent, "the person's session/new sent")
        assert sent == ["/bg1", "/user"], "a person's session/new must not wait behind _bg starts"
        assert (user_gate.active, user_gate.queued) == (1, 0)
        release["/user"].set()
        handle = await bounded(user, "the person's start")
        assert handle.session_id == "sid-user"
        assert not any(t.done() for t in bg), "the _bg starts are still outstanding"
        assert (user_gate.active, user_gate.releases) == (0, 1)

        release["/bg1"].set()
        release["/bg2"].set()
        await bounded(asyncio.gather(*bg), "the _bg starts")
        assert (bg_gate.active, bg_gate.queued, bg_gate.releases) == (0, 0, 2)
        assert user_gate.releases == 1, "no _bg start ever held a user permit"
    finally:
        await settle_tasks(started, "leftover starts")


@pytest.mark.asyncio
async def test_a_foreground_bg_runtime_start_is_served_ahead_of_queued_titles(monkeypatch):
    """The ``_bg`` gate keeps start priority: a FOREGROUND one-liner (STT
    endpointing, a person's turn canary) queued behind BACKGROUND titles goes out
    next once the running start answers."""
    rt = _make_runtime()
    names = ("/running", "/title1", "/title2", "/stt")
    release = {n: asyncio.Event() for n in names}
    sent = _blocking_session_new(rt, monkeypatch, release)

    tasks = [_bg_start(rt, "/running")]
    try:
        await until(lambda: sent == ["/running"], "running _bg start sent")
        bg_gate = await runtime_start.bg_runtime_session_start_gate()
        tasks += [_bg_start(rt, "/title1"), _bg_start(rt, "/title2")]
        await until(lambda: bg_gate.queued == 2, "two titles queued")
        tasks.append(_bg_start(rt, "/stt", StartPriority.FOREGROUND))
        await until(lambda: bg_gate.queued == 3, "STT start queued")

        release["/running"].set()
        await until(lambda: len(sent) == 2, "next _bg start sent")
        assert sent == ["/running", "/stt"]

        for n in names:
            release[n].set()
        await bounded(asyncio.gather(*tasks), "every _bg start")
        assert sent == ["/running", "/stt", "/title1", "/title2"]
        assert bg_gate.releases == 4
    finally:
        await settle_tasks(tasks, "leftover starts")


@pytest.mark.asyncio
async def test_a_permit_reports_the_counts_of_the_gate_it_came_from():
    """The collector log line reads ``permit.gate_counts()``, so a ``_bg`` start
    logs the ``_bg`` gate's depth, not the user gate's."""
    user_gate = await runtime_mod.session_start_gate()
    bg_gate = await runtime_start.bg_runtime_session_start_gate()
    user_permit = await bounded(user_gate.acquire(), "user permit")
    bg_permit = await bounded(bg_gate.acquire(), "_bg permit")
    queued = asyncio.create_task(bg_gate.acquire())
    try:
        await until(lambda: bg_gate.queued == 1, "second _bg acquire queued")
        assert bg_permit.gate_counts() == (1, 1)
        assert user_permit.gate_counts() == (1, 0)
    finally:
        bg_permit.release()
        (await bounded(queued, "queued _bg permit")).release()
        user_permit.release()


@pytest.mark.asyncio
async def test_other_starts_keep_the_user_gate(monkeypatch):
    """Without ``bg_runtime_start`` the start uses the user gate as before."""
    rt = _make_runtime()
    release = {"/u1": asyncio.Event(), "/u2": asyncio.Event()}
    sent = _blocking_session_new(rt, monkeypatch, release)
    tasks = [asyncio.create_task(rt.create_session(cwd=c, mcp_servers=[])) for c in ("/u1", "/u2")]
    try:
        await until(lambda: sent == ["/u1"], "first user start sent")
        user_gate = await runtime_mod.session_start_gate()
        await until(lambda: user_gate.queued == 1, "second user start queued")
        release["/u1"].set()
        release["/u2"].set()
        await bounded(asyncio.gather(*tasks), "both user starts")
        assert user_gate.releases == 2
        assert not runtime_start._bg_runtime_session_start_gates, "no _bg gate was created"
    finally:
        await settle_tasks(tasks, "leftover starts")


@pytest.mark.asyncio
@pytest.mark.parametrize("priority", [StartPriority.BACKGROUND, StartPriority.FOREGROUND])
async def test_get_bg_session_starts_on_the_bg_runtime_gate(priority):
    """``SessionManager.get_bg_session`` (the path every ``run_bg_oneliner``
    takes) asks the shared runtime for a ``_bg`` start, at the caller's priority."""
    calls: list[dict] = []

    class _FakeRuntime:
        def __init__(self, **kwargs) -> None:
            self.pid = _UNALLOCATABLE_PID

        async def spawn(self, **kwargs) -> None:
            return None

        def is_alive(self) -> bool:
            return True

        def has_active_sessions(self) -> bool:
            return True

        def has_active_or_initializing_sessions(self) -> bool:
            return True

        async def create_session(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(session_id="sid-bg")

        async def kill(self, *, expected: bool = False, reason: str = "") -> None:
            return None

    cfg = KiroCrewConfig()
    cfg.session.pool_size = 0
    mgr = SessionManager(cfg)
    with patch.object(runtime_mod, "AcpRuntime", _FakeRuntime):
        handle = await mgr.get_bg_session(start_priority=priority)
    assert handle.session_id == "sid-bg"
    assert len(calls) == 1
    assert calls[0].get("bg_runtime_start") is True
    assert calls[0].get("start_priority") is priority
