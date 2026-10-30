"""M6.2 — production agent_fn that runs ctx.agent() steps through a real model.

Asserts build_agent_fn's wiring WITHOUT a real model/kiro-cli:
  * each call acquires a session and streams the prompt via stream_and_collect
  * default (no session=) → a fresh isolated per-call session, released after
  * session=<key> → reuse that named session, release its lease but keep history
  * agent/model/cwd from opts (or defaults) flow into get_or_create
  * end-to-end through the runner: a workflow's ctx.agent() reaches the model

``stream_and_collect`` is patched (it's the tested core primitive elsewhere); this
suite is about agent_exec's session lifecycle + opts plumbing.
See GATES (M6) and docs/system-specs/modules/workflows.md.
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import kiro_crew.workflows.agent_exec as agent_exec
from kiro_crew.config.loader import KiroCrewConfig
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
        # Every other kwarg, index-aligned with ``created``.
        self.extra_kwargs: list[dict] = []

    async def get_or_create(self, key, *, agent=None, model=None, cwd=None, extra_env=None, **kw):
        self.created.append(
            (key, {"agent": agent, "model": model, "cwd": cwd, "extra_env": extra_env})
        )
        self.extra_kwargs.append(kw)
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
    assert out.startswith("reply[wf:wf_x:")  # ran on an ephemeral ``wf:`` key
    assert len(sessions.created) == 1
    # ephemeral session was released (torn down) after the call
    assert sessions.released == [sessions.created[0][0]]


async def test_subagents_get_full_tooled_agent_not_lite() -> None:
    """A workflow's ctx.agent() steps are the WORKERS — they must have full tool
    access (MCPs, file/web tools), so they use the default full agent, NOT the
    tool-less agent-lite used for authoring. Default agent=None ⇒ the backend
    resolves the full default agent (with its complete toolset)."""
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_x")
    await fn("do real work", {})
    _key, kw = sessions.created[0]
    assert kw["agent"] is None  # NOT "agent-lite" — full default agent + tools

    # An explicit per-step agent override still flows through (e.g. a specialist).
    sessions2 = FakeSessions()
    fn2 = build_agent_fn(sessions2, run_id="wf_y")
    await fn2("specialized", {"agent": "agent-sde"})
    assert sessions2.created[0][1]["agent"] == "agent-sde"


async def test_named_session_is_reused_and_lease_released() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_x")

    out = await fn("step1", {"session": "chain-A"})
    assert out == "reply[chain-A]:step1"
    assert sessions.created[0][0] == "chain-A"
    # Returning the lease does not destroy the named provider or its history.
    assert sessions.released == ["chain-A"]


async def test_two_default_calls_get_distinct_sessions() -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_y")
    await fn("a", {})
    await fn("b", {})
    keys = [c[0] for c in sessions.created]
    assert keys[0] != keys[1]  # isolated per call (parallel-safe)
    assert all(k.startswith("wf:wf_y:") for k in keys)


async def test_opts_and_defaults_flow_into_get_or_create(tmp_path, monkeypatch) -> None:
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_z", default_agent="researcher", default_model="m1")

    await fn("p", {})  # uses defaults
    assert sessions.created[-1][1] == {
        "agent": "researcher",
        "model": "m1",
        "cwd": None,
        "extra_env": None,
    }

    # A per-call cwd answers the same allowlist ``spawn_run`` answers (a step is a
    # spawn), so the override is exercised under a root the operator allows. What
    # flows through is the resolved realpath, as on the spawn path.
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    monkeypatch.setattr(
        KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                agent=SimpleNamespace(subagent_cwd_allowed_roots=[os.path.realpath(str(tmp_path))])
            )
        ),
    )

    await fn("p", {"agent": "coder", "model": "m2", "cwd": str(work_dir)})  # opts override
    assert sessions.created[-1][1] == {
        "agent": "coder",
        "model": "m2",
        "cwd": os.path.realpath(str(work_dir)),
        "extra_env": None,
    }


async def test_extra_env_run_level_pin_flows_into_get_or_create() -> None:
    """A run-level extra_env pin reaches every spawned session,
    just like default_agent/default_model/cwd (WorkflowContext.agent has no
    per-call env= override, so this is a run-level pin)."""
    env = {"CORRELATION_ID": "abc123", "MC_ENDPOINT": "https://example.test"}
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_env", extra_env=env)

    await fn("ephemeral step", {})
    assert sessions.created[-1][1]["extra_env"] == env

    # also on a named (stateful) session
    await fn("chain step", {"session": "chain-A"})
    assert sessions.created[-1][1]["extra_env"] == env

    # default (no pin) stays None — no accidental env leakage
    plain = FakeSessions()
    await build_agent_fn(plain, run_id="wf_plain")("p", {})
    assert plain.created[-1][1]["extra_env"] is None


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


def _effort_script(effort: str) -> str:
    return (
        'META = {"name": "effort"}\n'
        "async def workflow(ctx):\n"
        f"    return await ctx.agent('step', effort={effort!r})\n"
    )


async def test_step_effort_reaches_get_or_create_as_override() -> None:
    """ctx.agent(effort=) is the step's effort: the session is created with it
    as reasoning_effort_override, the same override spawn_run passes."""
    from kiro_crew.workflows.runner import WorkflowRunner

    sessions = FakeSessions()
    runner = WorkflowRunner(agent_fn=build_agent_fn(sessions, run_id="wf_eff"), audit=_no_audit)
    res = await runner.run(_effort_script("high"), run_id="wf_eff", now="2026-10-29T00:00:00Z")
    assert res.ok, res.error
    assert sessions.extra_kwargs == [{"reasoning_effort_override": "high"}]


@pytest.mark.parametrize("opts", [{}, {"effort": None}, {"effort": ""}])
async def test_unset_effort_passes_no_override(opts) -> None:
    sessions = FakeSessions()
    await build_agent_fn(sessions, run_id="wf_noeff")("p", opts)
    assert sessions.extra_kwargs == [{}]


async def test_invalid_effort_fails_the_step_before_allocating() -> None:
    """A level outside the shared vocabulary fails the step with a named error
    and allocates no session, instead of running at a default."""
    from kiro_crew.workflows.runner import WorkflowRunner

    sessions = FakeSessions()
    runner = WorkflowRunner(agent_fn=build_agent_fn(sessions, run_id="wf_bad"), audit=_no_audit)
    script = (
        'META = {"name": "effort"}\n'
        "async def workflow(ctx):\n"
        "    return {'out': await ctx.agent('step', effort='turbo')}\n"
    )
    res = await runner.run(script, run_id="wf_bad", now="2026-10-29T00:00:00Z")
    assert res.ok, res.error
    assert res.result == {"out": None}
    assert sessions.created == []
    with pytest.raises(agent_exec.WorkflowEffortInvalid, match="turbo"):
        await build_agent_fn(sessions, run_id="wf_bad2")("p", {"effort": "turbo"})
    assert sessions.created == []


def _no_audit(*_a, **_k) -> None:
    return None


async def test_agent_step_persists_usage_row_with_surface() -> None:
    """Each workflow agent step appends one usage row tagged
    surface='workflow', carrying the step's agent/model and context occupancy."""
    sessions = FakeSessions()
    fn = build_agent_fn(sessions, run_id="wf_u", default_agent="researcher", default_model="m1")

    persist = AsyncMock()
    with (
        patch("kiro_crew.dashboard.handlers.usage.persist_token_record_async", persist),
        patch(
            "kiro_crew.dashboard.handlers.usage.read_context_tokens",
            MagicMock(return_value=(42, 200000)),
            create=True,
        ),
    ):
        await fn("do work", {})

    persist.assert_awaited_once()
    kwargs = persist.await_args.kwargs
    assert kwargs["surface"] == "workflow"
    assert kwargs["agent"] == "researcher"
    assert kwargs["context_used"] == 42
    assert kwargs["context_window"] == 200000


@pytest.mark.parametrize("opts", [{}, {"session": "chain-A"}])
async def test_identity_is_published_before_each_agent_turn(monkeypatch, opts):
    from kiro_crew.messaging import identity

    sessions = FakeSessions()
    events = []

    async def publish(owner, key):
        assert owner is sessions
        assert sessions.created[-1][0] == key
        events.append(("publish", key))

    async def stream(provider, message, **kwargs):
        assert events[-1] == ("publish", provider.key)
        events.append(("stream", provider.key))
        return "done"

    monkeypatch.setattr(identity, "publish_turn_identity", publish)
    monkeypatch.setattr(agent_exec, "stream_and_collect", stream)
    agent = build_agent_fn(sessions, run_id="wf_identity")
    assert await agent("first", opts) == "done"
    assert await agent("second", opts) == "done"
    assert [event for event, _ in events] == ["publish", "stream", "publish", "stream"]
    assert sessions.released == [key for key, _ in sessions.created]
