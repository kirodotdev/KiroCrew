"""A workflow ``ctx.agent()`` step that names no agent runs as the chat that started it.

The run freezes its caller's execution carrier at admission. A chat on a custom
agent binds a ``template`` carrier naming that agent, so a step with no
``agent=`` opens its session on that agent, on the warm pool and on the
cold-start path alike, and its usage row is attributed to it. A default chat (the
``kirocrew`` floor), a carrier bound to a member and an app's own agent pass no
agent, so their steps keep the configured pool agent.

No provider is constructed and no process is spawned: ``stream_and_collect`` is
an echo and ``SessionManager`` is a recording double.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.apps.manager as apps_manager
import kiro_crew.dashboard.handlers.usage as usage_mod
import kiro_crew.member_essential_context as member_essential_context
import kiro_crew.workflows.agent_exec as agent_exec
import kiro_crew.workflows.agent_pool as agent_pool
from kiro_crew.apps.manager_lookup import AgentOwner
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, bind_session_execution
from kiro_crew.mate_worker import MATE_WORKER_AGENT
from kiro_crew.testing.wait import await_until
from kiro_crew.workflows.service import WorkflowService

pytestmark = pytest.mark.asyncio

CALLER = "dashboard:chat-inherit"
SCRIPT = (
    'META = {"name": "inherit"}\n' "async def workflow(ctx):\n" "    return await ctx.agent('hi')\n"
)


class _FakeProvider:
    def __init__(self, key: str) -> None:
        self.key = key

    async def new_conversation(self) -> None:
        return None

    def is_process_alive(self) -> bool:
        return True

    def set_delete_delay(self, seconds: float) -> None:
        return None


class _RecordingSessions:
    """SessionManager double recording the agent every step session opens on."""

    admission_closed = False

    def __init__(self) -> None:
        self.agents: list[object] = []

    async def get_or_create(self, key, *, agent=None, **_kw):
        self.agents.append(agent)
        return _FakeProvider(key), True, False

    def release(self, key, *, cleanup=False):
        return None

    async def destroy(self, key):
        return None

    async def reset(self, key, **_kw):
        return None

    async def end_children_for(self, key):
        return None


@pytest.fixture(autouse=True)
def _echo_model(_floor_monkeypatch):
    async def _echo(provider, message, **_kw):
        return f"reply[{provider.key}]"

    _floor_monkeypatch.setattr(agent_exec, "stream_and_collect", _echo)
    _floor_monkeypatch.setattr(agent_pool, "stream_and_collect", _echo)


@pytest.fixture(autouse=True)
def _user_level_custom_agent():
    """``custom-agent`` is a user-level spec, as a custom agent made in the dashboard is."""
    agents = kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "custom-agent.json").write_text(json.dumps({"name": "custom-agent"}))


@pytest.fixture
def usage_agents(monkeypatch):
    """The agent each step's usage row names, captured without a disk write."""
    agents: list[str] = []
    monkeypatch.setattr(
        usage_mod, "_write_token_record", lambda record, now: agents.append(record["agent"])
    )
    return agents


def _template(template: str) -> ExecutionContext:
    return ExecutionContext(
        None, MemoryStoreRef("default"), "template", template, "persistent", "", template
    )


def _member_on(template: str, kind: str = "member") -> ExecutionContext:
    """A carrier bound to member ``alpha``: its own chat, or its crewmate worker."""
    return ExecutionContext("alpha", MemoryStoreRef("memory-alpha", "alpha"), kind, template)


async def _run_one_step(carrier: ExecutionContext, *, pooled: bool) -> _RecordingSessions:
    bind_session_execution(CALLER, carrier)
    sessions = _RecordingSessions()
    svc = WorkflowService(sessions=sessions, persist=False, pool_agents=pooled)
    started = await svc.start(SCRIPT, session_key=CALLER)
    assert "run_id" in started, started
    handle = svc.registry.get(started["run_id"])
    assert handle is not None and handle.task is not None
    await await_until(handle.task, what="the workflow run")
    snap = svc.status(started["run_id"])
    assert snap and snap["status"] == "finished", snap
    return sessions


async def _builder_kwargs(monkeypatch, carrier: ExecutionContext, *, pooled: bool) -> dict:
    """The keyword arguments the service hands the step builder for *carrier*'s run."""
    from kiro_crew.workflows import service as service_mod

    captured: dict[str, object] = {}

    def _spy(sessions, **kw):
        captured.update(kw)

        async def _never_called(prompt, opts):  # pragma: no cover - not run here
            raise AssertionError("the runner was not started by this test")

        return (_never_called, None) if pooled else _never_called

    monkeypatch.setattr(service_mod, "build_pooled_agent_fn" if pooled else "build_agent_fn", _spy)
    svc = WorkflowService(sessions=_RecordingSessions(), persist=False, pool_agents=pooled)
    scope = SimpleNamespace(
        execution_context=carrier, memory_mode="persistent", validate=None, origin=CALLER
    )
    default_agent = await svc._inherited_agent(scope)
    svc._runner("run-wiring", memory_scope=scope, session_key=CALLER, default_agent=default_agent)
    return captured


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_a_step_with_no_agent_runs_the_caller_s_template(pooled, usage_agents):
    sessions = await _run_one_step(_template("custom-agent"), pooled=pooled)

    assert sessions.agents == ["custom-agent"]
    assert usage_agents == ["custom-agent"]


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_a_default_chat_keeps_the_pool_agent(pooled, usage_agents):
    sessions = await _run_one_step(_template("kirocrew"), pooled=pooled)

    assert sessions.agents == [None]


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_the_service_hands_the_builders_the_caller_s_template(monkeypatch, pooled):
    kwargs = await _builder_kwargs(monkeypatch, _template("custom-agent"), pooled=pooled)

    assert kwargs.get("default_agent") == "custom-agent", kwargs


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
@pytest.mark.parametrize(
    "carrier",
    [_member_on("member-template"), _member_on(MATE_WORKER_AGENT, kind="template")],
    ids=["member-chat", "crewmate-worker"],
)
async def test_a_member_bound_caller_hands_the_builders_no_agent(monkeypatch, carrier, pooled):
    """A member's steps need the crew routing these builders do not carry."""
    kwargs = await _builder_kwargs(monkeypatch, carrier, pooled=pooled)

    assert kwargs.get("default_agent") is None, kwargs


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_an_app_s_own_agent_hands_the_builders_no_agent(monkeypatch, pooled):
    """Its chat runs the app's registered spec through a launch copy no step carries."""
    monkeypatch.setattr(
        apps_manager,
        "owning_app_for_agent",
        lambda agent: AgentOwner("notes", "Notes", True) if agent == "notes-agent" else None,
    )

    app_kwargs = await _builder_kwargs(monkeypatch, _template("notes-agent"), pooled=pooled)
    custom_kwargs = await _builder_kwargs(monkeypatch, _template("custom-agent"), pooled=pooled)

    assert app_kwargs.get("default_agent") is None, app_kwargs
    assert custom_kwargs.get("default_agent") == "custom-agent", custom_kwargs


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_a_project_only_template_hands_the_builders_no_agent(monkeypatch, pooled):
    """A name only a checkout declares may resolve to nothing in the run's own directory."""
    kwargs = await _builder_kwargs(monkeypatch, _template("project-only-agent"), pooled=pooled)

    assert kwargs.get("default_agent") is None, kwargs


@pytest.mark.parametrize("pooled", [True, False], ids=["pooled", "cold-start"])
async def test_a_linked_user_level_template_hands_the_builders_its_name(monkeypatch, pooled):
    """A spec linked into the agents directory is user-level too, as a chat session reads it.

    The path resolver keeps the writer's no-link fence, so the name is found by the
    linked reader a session's own spec read falls back to; that reader is faked here.
    """
    seen: list[str] = []

    def linked(template, *, operation, source):
        seen.append(template)
        return (kiro_agents_dir() / f"{template}.json", {"name": template})

    monkeypatch.setattr(member_essential_context, "linked_global_template_spec", linked)

    kwargs = await _builder_kwargs(monkeypatch, _template("linked-agent"), pooled=pooled)

    assert kwargs.get("default_agent") == "linked-agent", kwargs
    assert seen == ["linked-agent"]
