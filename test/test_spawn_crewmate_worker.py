"""``spawn_run(crew=<crewmate>)`` of a crewmate that owns its file runs on its worker.

Such a crewmate's chat runs on ``kirocrew-mate-conductor``, whose spec lives in
the crewmate folder: it is no agent a sub-agent can load by name, and the
conductor hands work off rather than doing it. A spawn that selects the crewmate
with no ``agent`` named therefore runs on the crewmate's worker
(``mate_worker.spawn_worker_execution``), member, store and mode kept, the
record shape a hand-off and a scheduled turn give the worker. A crewmate bound to
any other agent, and a spawn that names its ``agent``, are unchanged.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from member_memory_helpers import write_member_home

from kiro_crew import mate_worker
from kiro_crew.config import loader
from kiro_crew.config import paths as paths_mod
from kiro_crew.config.loader import KiroCrewConfig, config_dir
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_WORKER_AGENT
from kiro_crew.subagent import AGENT_NOT_FOUND_CODE, SubagentManager
from kiro_crew.subagent_manager.admission.gate import _GateMixin

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: The parent: a default-crew chat on the shared store.
PARENT = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")


def _home(*, owns_file: bool) -> KiroCrewConfig:
    write_member_home(config_dir(), "alpha")
    path = config_dir() / "config.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["agents"]["alpha"]["kiro_agent"] = "alpha" if owns_file else "kiro_default"
    path.write_text(json.dumps(doc), encoding="utf-8")
    if owns_file:
        mates = paths_mod.mate_agents_dir()
        mates.mkdir(parents=True, exist_ok=True)
        (mates / "alpha.json").write_text(
            json.dumps({"name": "alpha", "prompt": "", "tools": []}), encoding="utf-8"
        )
    loader._invalidate_config_cache()
    loader.refresh_materialized_agents(heal_default=False)
    return KiroCrewConfig.load()


def _resolve(**kwargs) -> ExecutionContext:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    return _GateMixin.resolve_spawn_execution(
        manager._admission, parent_session_key="dashboard:chat-1", _record=PARENT, **kwargs
    )


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock(side_effect=RuntimeError("stop before the provider"))
    sessions.release = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", "kirocrew"))
    sessions.get_approval_policy = MagicMock(return_value="")
    sessions._sessions = {}
    sessions._pool_cwd = ""
    return sessions


@pytest.mark.parametrize("selector", ["crew", "target_member"])
def test_a_crewmate_that_owns_its_file_is_spawned_on_its_worker(selector):
    _home(owns_file=True)
    # The crewmate's own selection names its conductor, the agent its chat runs on.
    assert loader.member_template_id(KiroCrewConfig.load().agents["alpha"]) == MATE_CONDUCTOR_AGENT

    execution = _resolve(**{selector: "alpha"})

    assert mate_worker.is_own_worker_execution(execution)
    assert execution.template_id == MATE_WORKER_AGENT
    assert execution.member_id == "alpha"
    assert execution.store == MemoryStoreRef("member-alpha", "alpha")


def test_the_dashboard_spawn_routes_admitted_crewmate_runs_on_its_worker():
    """``/api/spawn`` derives the member's execution itself and hands it over as
    ``_execution_context``; that record names the conductor and maps the same way."""
    config = _home(owns_file=True)
    from kiro_crew.execution_context import resolve_member_execution

    admitted = resolve_member_execution(config, "alpha", validate_memory_files=False)
    assert admitted.template_id == MATE_CONDUCTOR_AGENT
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    execution = _GateMixin.resolve_spawn_execution(
        manager._admission,
        parent_session_key="dashboard:chat-1",
        crew="alpha",
        _execution_context=admitted.to_record(),
    )

    assert execution == admitted.with_template(MATE_WORKER_AGENT, MATE_WORKER_AGENT)


def test_a_crewmate_bound_to_a_shared_agent_keeps_its_member_selection():
    _home(owns_file=False)

    execution = _resolve(crew="alpha")

    assert execution.selection_kind == "member"
    assert execution.template_id == "kiro_default"
    assert not mate_worker.is_own_worker_execution(execution)


def test_a_crew_spawn_naming_its_agent_keeps_that_agent():
    _home(owns_file=True)

    execution = _resolve(crew="alpha", agent="kirocrew-worker")

    assert (execution.selection_kind, execution.template_id) == ("member", "kirocrew-worker")
    assert execution.member_id == "alpha"


@pytest.mark.parametrize(
    "record",
    [
        # A member selection on any agent but the conductor.
        ExecutionContext("alpha", MemoryStoreRef("member-alpha", "alpha"), "member", "alpha"),
        # A conductor selection with no member identity names no crewmate.
        ExecutionContext(None, MemoryStoreRef("default"), "member", MATE_CONDUCTOR_AGENT),
        # A template run is never rewritten.
        ExecutionContext(
            "alpha", MemoryStoreRef("member-alpha", "alpha"), "template", MATE_CONDUCTOR_AGENT
        ),
        None,
    ],
)
def test_only_a_crewmates_conductor_selection_maps_to_its_worker(record):
    assert mate_worker.spawn_worker_execution(record) is None


def test_a_continuation_of_a_worker_run_stays_on_the_worker():
    _home(owns_file=True)
    worker = _resolve(crew="alpha")
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    continued = _GateMixin.resolve_spawn_execution(
        manager._admission, conversation_key="subagent:abc123", _record=worker
    )

    assert continued == worker


def test_spawn_of_a_crewmate_that_owns_its_file_is_not_refused_for_its_conductor():
    """The reported failure: admission looked the conductor up by name and refused it."""
    _home(owns_file=True)
    manager = SubagentManager(sessions=_sessions(), ctx_builder=None)
    with (
        patch("kiro_crew.subagent.list_agents", return_value=[]),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = manager.spawn("task", parent_session_key="dashboard:chat-1", crew="alpha")
    assert info is not None
    assert info.error_code != AGENT_NOT_FOUND_CODE
    assert MATE_CONDUCTOR_AGENT not in (info.error or "")
    assert mate_worker.is_own_worker_execution(info.execution_context)


async def _run(manager: SubagentManager) -> "object":
    with (
        patch("kiro_crew.subagent.list_agents", return_value=[]),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        info = await manager.spawn_async(
            "task", parent_session_key="dashboard:chat-1", crew="alpha", approval_mode="auto"
        )
        assert info is not None
        task = manager._tasks.get(info.id)
        if task is not None:
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
    return info


@pytest.mark.asyncio
async def test_the_worker_run_takes_its_crewmates_backend_route(monkeypatch):
    from kiro_crew.members import is_crewmate_worker_session

    _home(owns_file=True)
    monkeypatch.setattr(mate_worker, "worker_kit_backend_refusal", lambda backend: None)
    sessions = _sessions()
    manager = SubagentManager(sessions=sessions, ctx_builder=None)

    info = await _run(manager)

    assert info.error_code != AGENT_NOT_FOUND_CODE
    assert MATE_CONDUCTOR_AGENT not in (info.error or "")
    assert sessions.get_or_create.await_count >= 1
    assert sessions.get_or_create.await_args.kwargs.get("agent") == MATE_WORKER_AGENT
    assert is_crewmate_worker_session(f"subagent:{info.id}")


@pytest.mark.asyncio
async def test_a_worker_run_records_its_activity_under_the_crewmate(monkeypatch):
    """The worker record names the worker template; the crewmate's activity log
    is keyed by the crewmate, so the run is recorded under it."""
    from kiro_crew import members

    _home(owns_file=True)
    monkeypatch.setattr(mate_worker, "worker_kit_backend_refusal", lambda backend: None)
    recorded: list[str] = []

    def _record(member, *args, **kwargs):
        recorded.append(member)
        raise RuntimeError("stop after the activity record")

    monkeypatch.setattr(members, "record_activity", _record)
    sessions = _sessions()
    client = MagicMock()
    client.session_id = "sid-1"
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    manager = SubagentManager(sessions=sessions, ctx_builder=None)

    await _run(manager)

    assert recorded == ["alpha"]


@pytest.mark.asyncio
async def test_a_worker_run_on_a_backend_without_its_kit_is_refused_by_name(monkeypatch):
    _home(owns_file=True)
    monkeypatch.setattr(
        mate_worker, "worker_kit_backend_refusal", lambda backend: "set agent.member_acp_backend"
    )
    sessions = _sessions()
    manager = SubagentManager(sessions=sessions, ctx_builder=None)

    info = await _run(manager)

    assert "capability_harness_unsupported: set agent.member_acp_backend" in (info.error or "")
    sessions.get_or_create.assert_not_called()


def _pin_harness(stored: str) -> None:
    """Store crewmate ``alpha``'s own harness pin (``agents.alpha.acp_backend``)."""
    path = config_dir() / "config.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["agents"]["alpha"]["acp_backend"] = stored
    path.write_text(json.dumps(doc), encoding="utf-8")
    loader._invalidate_config_cache()


@pytest.fixture
def _kit_backends(monkeypatch):
    """Record the backend the worker run is judged on, and let every one through."""
    from kiro_crew.agent_sdk import backends

    monkeypatch.setattr(backends, "selectable_backends", lambda: frozenset({"", "kas", "claude"}))
    judged: list[str] = []

    def _judge(backend):
        judged.append(backend)
        return None

    monkeypatch.setattr(mate_worker, "worker_kit_backend_refusal", _judge)
    return judged


@pytest.mark.asyncio
async def test_an_unpinned_worker_run_is_judged_on_the_member_route(_kit_backends):
    """The crew spawn path hands the WHOLE config to the worker's backend helper:
    with no pin a default config answers the member route (KAS), never ``""``."""
    _home(owns_file=True)
    manager = SubagentManager(sessions=_sessions(), ctx_builder=None)

    info = await _run(manager)

    assert _kit_backends == ["kas"]
    assert "capability_harness_unsupported" not in (info.error or "")


@pytest.mark.asyncio
async def test_a_pinned_worker_run_is_judged_on_its_crewmates_pin(_kit_backends):
    _home(owns_file=True)
    _pin_harness("claude")
    manager = SubagentManager(sessions=_sessions(), ctx_builder=None)

    await _run(manager)

    assert _kit_backends == ["claude"]


@pytest.mark.asyncio
async def test_a_worker_run_on_an_unstartable_pin_is_refused_before_it_starts(_kit_backends):
    """A pinned harness this gateway cannot start refuses the run; nothing falls
    back to another harness and no session is opened."""
    _home(owns_file=True)
    _pin_harness("codex")
    sessions = _sessions()
    manager = SubagentManager(sessions=sessions, ctx_builder=None)

    info = await _run(manager)

    assert "crewmate_harness_unavailable" in (info.error or "")
    assert _kit_backends == []
    sessions.get_or_create.assert_not_called()


def test_the_worker_backend_refuses_a_config_section():
    """Handing the helper ``config.agent`` instead of the config is a type error,
    not a silent "no pin, no member route" answer."""
    with pytest.raises(TypeError):
        mate_worker.crewmate_worker_backend(KiroCrewConfig().agent)
