"""A sub-agent's own task profile decides whether it may spawn.

The spawn gate asks ``capabilities.spawn`` for the SPAWNING run. When that run
is a sub-agent started as a named agent, a profile bound to that agent's name
(``{"type": "task", "id": "<agent>"}``) is the profile its tool calls are judged
by, so the spawn gate reads the same one. Without it the gate reads the
``subagent`` surface profile for every nested spawn, whatever the agent.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from governance_chokepoints_helpers import _isolate  # noqa: F401
from governance_chokepoints_helpers import _install
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew import subagent
from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef, execution_for_store
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.subagent import SubagentInfo, SubagentManager, _vet_spawn_governance

pytestmark = pytest.mark.usefixtures("healthy_host_memory", "close_subagent_managers")

_PARENT_KEY = "subagent:parent01"


def _profile(name: str, bind_type: str, bind_id: str, spawn: dict) -> None:
    assert gp._PROFILES_DIR is not None
    (gp._PROFILES_DIR / f"{name}.json").write_text(
        json.dumps(
            {
                "name": name,
                "bind": {"type": bind_type, "id": bind_id},
                "capabilities": {"spawn": spawn},
            }
        ),
        encoding="utf-8",
    )
    gp.reset_store()


def _surface(enabled: bool) -> None:
    _profile("subagent", "surface", "subagent", {"enabled": enabled})


def _agent(agent: str, spawn: dict) -> None:
    _profile(f"task-{agent}", "task", agent, spawn)


def _manager(parent_agent: str) -> SubagentManager:
    """A manager holding one live sub-agent that runs as *parent_agent*."""
    manager = SubagentManager(sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn())
    manager._agents["parent01"] = SubagentInfo(
        execution_context=execution_for_store(""),
        id="parent01",
        task="parent work",
        agent=parent_agent,
        parent_session_key="dashboard:root",
    )
    return manager


async def _spawn_error(manager: SubagentManager) -> str:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = manager.spawn("child work", parent_session_key=_PARENT_KEY)
        assert info is not None
        task = manager._tasks.get(info.id)
        if task is not None:
            await task
    return info.error


class TestTheGateReadsTheSpawningAgentsProfile:
    def test_an_allowing_agent_profile_permits_the_spawn(self):
        _install(None)
        _surface(False)
        _agent("researcher", {"enabled": True})
        assert _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="researcher") is None

    def test_a_denying_agent_profile_refuses_the_spawn(self):
        _install(None)
        _surface(True)
        _agent("researcher", {"enabled": False})
        assert _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="researcher") is not None

    def test_the_agent_profiles_agents_scope_is_applied(self):
        _install(None)
        _surface(False)
        _agent(
            "researcher",
            {"enabled": True, "scopes": {"agents": {"mode": "allow", "allow": ["helper"]}}},
        )
        assert _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="researcher") is None
        assert _vet_spawn_governance(_PARENT_KEY, "deployer", parent_agent="researcher") is not None

    @pytest.mark.parametrize("surface_allows", [True, False])
    def test_an_agent_with_no_profile_is_judged_by_the_surface(self, surface_allows):
        _install(None)
        _surface(surface_allows)
        with_agent = _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="writer")
        without = _vet_spawn_governance(_PARENT_KEY, "helper")
        assert (with_agent is None) is surface_allows
        assert with_agent == without

    def test_the_policy_ceiling_still_binds_an_allowing_agent_profile(self):
        _install(
            {
                "version": 1,
                "boot": {"fail_closed": True},
                "capabilities": {"spawn": {"enabled": False}},
            }
        )
        _agent("researcher", {"enabled": True})
        assert _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="researcher") is not None

    def test_a_profile_for_the_target_agent_is_not_the_spawners(self):
        """The profile read is the spawning agent's, never the spawned agent's."""
        _install(None)
        _surface(False)
        _agent("helper", {"enabled": True})
        assert _vet_spawn_governance(_PARENT_KEY, "helper", parent_agent="researcher") is not None


class TestANestedSpawnIsAdmittedOnTheParentRunsAgent:
    @pytest.mark.asyncio
    async def test_an_allowing_agent_profile_admits_it(self):
        _install(None)
        _surface(False)
        _agent("researcher", {"enabled": True})
        assert await _spawn_error(_manager("researcher")) == ""

    @pytest.mark.asyncio
    async def test_a_denying_agent_profile_refuses_it(self):
        _install(None)
        _surface(True)
        _agent("researcher", {"enabled": False})
        assert "refused by governance" in await _spawn_error(_manager("researcher"))

    @pytest.mark.asyncio
    async def test_no_agent_profile_leaves_the_surface_verdict(self):
        _install(None)
        _surface(False)
        assert "refused by governance" in await _spawn_error(_manager("researcher"))

    @pytest.mark.asyncio
    async def test_a_parent_run_with_no_agent_leaves_the_surface_verdict(self):
        _install(None)
        _surface(False)
        _agent("researcher", {"enabled": True})
        assert "refused by governance" in await _spawn_error(_manager(""))

    @pytest.mark.asyncio
    async def test_two_live_parent_runs_must_both_be_permitted(self):
        """A continuation runs on the first run's key: the stricter profile decides."""
        _install(None)
        _surface(True)
        _agent("researcher", {"enabled": True})
        _agent("auditor", {"enabled": False})
        manager = _manager("researcher")
        manager._agents["cont01"] = SubagentInfo(
            execution_context=execution_for_store(""),
            id="cont01",
            task="continued work",
            agent="auditor",
            parent_session_key="dashboard:root",
            conversation_key=_PARENT_KEY,
        )
        assert "refused by governance" in await _spawn_error(manager)


def _templated(run_id: str, template: str, agent: str = "", **kwargs) -> SubagentInfo:
    return SubagentInfo(
        execution_context=ExecutionContext(None, MemoryStoreRef("default"), "template", template),
        id=run_id,
        task="work",
        agent=agent,
        parent_session_key="dashboard:root",
        **kwargs,
    )


def _expect(error: str, permitted: bool) -> None:
    if permitted:
        assert error == ""
    else:
        assert "refused by governance" in error


class TestARunWithNoExplicitAgent:
    """A run with no explicit agent is judged by its lineage, else the surface."""

    @pytest.mark.parametrize(
        "profile_allows,surface_allows", [(False, True), (True, False)], ids=["deny", "allow"]
    )
    @pytest.mark.asyncio
    async def test_a_follow_up_is_judged_as_the_agent_it_continues(
        self, profile_allows, surface_allows
    ):
        _install(None)
        _surface(surface_allows)
        _agent("researcher", {"enabled": profile_allows})
        manager = _manager("researcher")
        manager._agents["parent01"].done = True
        manager._agents["cont01"] = _templated("cont01", "researcher", conversation_key=_PARENT_KEY)

        _expect(await _spawn_error(manager), profile_allows)

    @pytest.mark.parametrize(
        "profile_allows,surface_allows",
        [(True, False), (False, True), (None, True), (None, False)],
        ids=["template-allows", "template-denies", "no-profile-allow", "no-profile-deny"],
    )
    @pytest.mark.asyncio
    async def test_a_plain_child_is_judged_by_the_surface_alone(
        self, profile_allows, surface_allows
    ):
        _install(None)
        _surface(surface_allows)
        if profile_allows is not None:
            _agent("kirocrew", {"enabled": profile_allows})
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn()
        )
        manager._agents["parent01"] = _templated("parent01", "kirocrew")

        _expect(await _spawn_error(manager), surface_allows)

    @pytest.mark.parametrize(
        "profile_allows,surface_allows", [(False, True), (True, False)], ids=["deny", "allow"]
    )
    @pytest.mark.asyncio
    async def test_a_follow_up_whose_original_is_gone_is_judged_by_the_surface(
        self, profile_allows, surface_allows
    ):
        _install(None)
        _surface(surface_allows)
        _agent("researcher", {"enabled": profile_allows})
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn()
        )
        manager._agents["cont01"] = _templated("cont01", "researcher", conversation_key=_PARENT_KEY)

        _expect(await _spawn_error(manager), surface_allows)

    @pytest.mark.asyncio
    async def test_an_unnamed_live_run_beside_a_named_one_keeps_the_surface_verdict(self):
        _install(None)
        _surface(False)
        _agent("auditor", {"enabled": True})
        manager = SubagentManager(
            sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn()
        )
        manager._agents["parent01"] = _templated("parent01", "")
        manager._agents["cont01"] = _templated(
            "cont01", "researcher", agent="auditor", conversation_key=_PARENT_KEY
        )

        _expect(await _spawn_error(manager), False)


class TestTheTurnStartRecheckReadsTheSameProfile:
    """A child that inherits its agent is vetted again when its turn starts."""

    @pytest.mark.asyncio
    async def test_both_checks_name_the_spawning_agent(self, monkeypatch):
        _install(None)
        _surface(False)
        _agent("researcher", {"enabled": True})
        vet = MagicMock(wraps=subagent._vet_spawn_governance)
        monkeypatch.setattr(subagent, "_vet_spawn_governance", vet)
        manager = _manager("researcher")
        inherited = ExecutionContext(None, MemoryStoreRef("default"), "template", "kirocrew")
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn(
                "child work",
                parent_session_key=_PARENT_KEY,
                _execution_context=inherited.to_record(),
                _parent_spawn_policy=("kirocrew", (), ()),
            )
            assert info is not None
            await manager._tasks[info.id]

        assert info.error == ""
        # The admission gate, then the turn-start recheck on the inherited agent.
        assert [call.args[1] for call in vet.call_args_list] == ["", "kirocrew"]
        assert all(call.kwargs.get("parent_agent") == "researcher" for call in vet.call_args_list)

    @pytest.mark.asyncio
    async def test_a_root_chat_spawn_is_vetted_as_before(self, monkeypatch):
        """No sub-agent parent: the call carries no ``parent_agent`` at all."""
        _install(None)
        vet = MagicMock(wraps=subagent._vet_spawn_governance)
        monkeypatch.setattr(subagent, "_vet_spawn_governance", vet)
        manager = _manager("researcher")
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = manager.spawn("child work", parent_session_key="dashboard:root")
            assert info is not None
            await manager._tasks[info.id]

        assert info.error == ""
        assert vet.call_args_list and all(
            "parent_agent" not in c.kwargs for c in vet.call_args_list
        )
