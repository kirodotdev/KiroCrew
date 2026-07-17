"""M6.2 — production agent_fn that runs ctx.agent() steps through a real model.

Asserts build_agent_fn's wiring WITHOUT a real model/kiro-cli:
  * each call acquires a session and streams the prompt via stream_and_collect
  * default (no session=) → a fresh isolated per-call session, released after
  * session=<key> → reuse that named session, NOT released (stateful chain)
  * agent/model/cwd from opts (or defaults) flow into get_or_create
  * end-to-end through the runner: a workflow's ctx.agent() reaches the model

``stream_and_collect`` is patched (it's the tested core primitive elsewhere); this
suite is about agent_exec's session lifecycle + opts plumbing.
See GATES (M6) and docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import pytest

import kiro_crew.workflows.agent_exec as agent_exec
from kiro_crew.workflows.agent_exec import build_agent_fn

pytestmark = pytest.mark.asyncio


class FakeProvider:
    def __init__(self, key: str) -> None:
        self.key = key


class FakeSessions:
    """Minimal SessionManager double recording acquire/release + ctor kwargs."""

    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.released: list[str] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, **kw):
        self.created.append((key, {"agent": agent, "model": model, "cwd": cwd}))
        return FakeProvider(key), True, False

    def release(self, key, *, cleanup=False):
        self.released.append(key)


@pytest.fixture(autouse=True)
def _patch_stream(monkeypatch):
    """stream_and_collect → echo the prompt + the session key it ran on."""

    async def fake_stream(provider, message, **kw):
        return f"reply[{provider.key}]:{message}"

    monkeypatch.setattr(agent_exec, "stream_and_collect", fake_stream)


async def test_default_uses_fresh_ephemeral_session_and_releases() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_x")

    out = await fn("hello", {})
    assert out.startswith("reply[wf:wf_x:")  # ran on a wf-scoped ephemeral key
    assert len(sessions.created) == 1
    # ephemeral session was released (torn down) after the call
    assert sessions.released == [sessions.created[0][0]]


async def test_subagents_get_full_tooled_agent_not_lite() -> None:
    """A workflow's ctx.agent() steps are the WORKERS — they must have full tool
    access (MCPs, file/web tools), so they use the default full agent, NOT the
    tool-less meshclaw-lite used for authoring. Default agent=None ⇒ the backend
    resolves the full default agent (with its complete toolset)."""
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_x")
    await fn("do real work", {})
    _key, kw = sessions.created[0]
    assert kw["agent"] is None  # NOT "meshclaw-lite" — full default agent + tools

    # An explicit per-step agent override still flows through (e.g. a specialist).
    sessions2 = FakeSessions()
    fn2 = build_agent_fn(sessions2, run_id="wf_y")
    await fn2("specialized", {"agent": "meshclaw-sde"})
    assert sessions2.created[0][1]["agent"] == "meshclaw-sde"


async def test_named_session_is_reused_and_not_released() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_x")

    out = await fn("step1", {"session": "chain-A"})
    assert out == "reply[chain-A]:step1"
    assert sessions.created[0][0] == "chain-A"
    # a named (stateful) session persists across steps — NOT released
    assert sessions.released == []


async def test_two_default_calls_get_distinct_sessions() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_y")
    await fn("a", {})
    await fn("b", {})
    keys = [c[0] for c in sessions.created]
    assert keys[0] != keys[1]  # isolated per call (parallel-safe)
    assert all(k.startswith("wf:wf_y:") for k in keys)


async def test_opts_and_defaults_flow_into_get_or_create() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_z", default_agent="researcher", default_model="m1")

    await fn("p", {})  # uses defaults
    assert sessions.created[-1][1] == {"agent": "researcher", "model": "m1", "cwd": None}

    await fn("p", {"agent": "coder", "model": "m2", "cwd": "/tmp/x"})  # opts override
    assert sessions.created[-1][1] == {"agent": "coder", "model": "m2", "cwd": "/tmp/x"}


async def test_end_to_end_through_runner() -> None:
    """A workflow's ctx.agent() reaches the model via the built agent_fn."""
    from kiro_crew.workflows.runner import WorkflowRunner

    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_e2e")
    runner = WorkflowRunner(agent_fn=fn, audit=lambda *a, **k: None)

    script = (
        'META = {"name": "research"}\n'
        "async def workflow(ctx):\n"
        "    return await ctx.agent('origin of pizza')\n"
    )
    res = await runner.run(script, run_id="wf_e2e", now="2026-06-18T00:00:00Z")
    assert res.ok, res.error
    assert "origin of pizza" in str(res.result)
    assert sessions.created  # the model was actually engaged
