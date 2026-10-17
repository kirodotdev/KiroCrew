from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.agent_discovery import clear_project_agent_cache, project_agent_names
from kiro_crew.config.loader import KIRO_CLI_BUILTIN_AGENTS, KiroCrewConfig
from kiro_crew.config.sections import KiroCrewAgentConfig
from kiro_crew.subagent import (
    AGENT_NOT_FOUND_CODE,
    SubagentManager,
    _inherited_member_agent,
)

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _sessions(selection: tuple[str, str]) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock()
    sessions.release = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=selection)
    sessions._sessions = {}
    sessions._pool_cwd = ""
    return sessions


def _project_agent(root: Path, name: str) -> Path:
    agents = root / ".kiro" / "agents"
    agents.mkdir(parents=True)
    (agents / f"{name}.json").write_text(json.dumps({"name": name}))
    return root


def _config_with_member(kiro_agent: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {"alice": KiroCrewAgentConfig(kiro_agent=kiro_agent)}
    return cfg


@pytest.mark.parametrize(
    ("kind", "template", "expected"),
    [
        ("member", "gone", "gone"),
        ("template", "parent-agent", ""),
        ("member", "", ""),
    ],
)
def test_inheritedMemberAgent_selectionShapes_namesOnlyAMembersTemplate(
    kind: str, template: str, expected: str
) -> None:
    assert _inherited_member_agent(kind, template) == expected


def test_inheritedMemberAgent_coldProjectCache_namesNothing(tmp_path) -> None:
    clear_project_agent_cache()
    assert _inherited_member_agent("member", "repobot", str(tmp_path)) == ""
    _project_agent(tmp_path, "repobot")
    project_agent_names(str(tmp_path), operation="test_inherited_admission", source="unknown")
    assert _inherited_member_agent("member", "repobot", str(tmp_path)) == "repobot"


class TestInheritedMemberAgentAdmission:
    def test_spawn_inheritedMemberSpecMissing_refusedBeforeReceipt(self) -> None:
        sessions = _sessions(("member", "alice"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("gone")),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.done is True
        assert info.queued is False
        assert info.error_code == AGENT_NOT_FOUND_CODE
        assert "'gone' not found" in info.error
        assert info.id not in manager._tasks
        sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawnAsync_inheritedMemberSpecMissing_refusedBeforeReceipt(self) -> None:
        sessions = _sessions(("member", "alice"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("gone")),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = await manager.spawn_async("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.done is True
        assert info.error_code == AGENT_NOT_FOUND_CODE
        assert info.id not in manager._tasks
        sessions.get_or_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawnAsync_inheritedMemberSpecMissing_scansOffLoop(self) -> None:
        sessions = _sessions(("member", "alice"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        scan_threads: list[threading.Thread] = []

        def _scan() -> list:
            scan_threads.append(threading.current_thread())
            return []

        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("gone")),
            patch("kiro_crew.subagent.list_agents", side_effect=_scan),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = await manager.spawn_async("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.error_code == AGENT_NOT_FOUND_CODE
        assert scan_threads
        assert threading.main_thread() not in scan_threads

    @pytest.mark.asyncio
    async def test_spawnAsync_cacheWarmsAfterColdOffLoopCheck_gateDoesNotScanOnLoop(
        self, tmp_path
    ) -> None:
        clear_project_agent_cache()
        project = _project_agent(tmp_path / "repo", "other")
        sessions = _sessions(("member", "alice"))
        sessions._pool_cwd = str(project)
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        loop_thread = threading.current_thread()
        scan_threads: list[threading.Thread] = []

        def _scan() -> list:
            scan_threads.append(threading.current_thread())
            return []

        cold_check = manager._check_agent_off_loop

        async def _cold_then_warm(*args, **kwargs):
            check = await cold_check(*args, **kwargs)
            await asyncio.to_thread(
                project_agent_names, str(project), operation="test", source="unknown"
            )
            return check

        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("gone")),
            patch("kiro_crew.subagent.list_agents", side_effect=_scan),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch.object(manager, "_check_agent_off_loop", side_effect=_cold_then_warm),
        ):
            info = await manager.spawn_async("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert loop_thread not in scan_threads

    @pytest.mark.asyncio
    async def test_checkAgentOffLoop_noAgentMemberContext_checksInheritedTemplate(self) -> None:
        manager = SubagentManager(sessions=_sessions(("template", "")), ctx_builder=None)
        with patch("kiro_crew.subagent.list_agents", return_value=[]):
            check = await manager._check_agent_off_loop(
                "",
                "",
                execution_context={"selection_kind": "member", "template_id": "gone"},
            )
        assert check is not None
        assert check[0] == "gone"
        assert check[3] == ""
        assert check[-1] == AGENT_NOT_FOUND_CODE

    def test_spawn_inheritedProjectAgent_coldCacheNotRefused(self, tmp_path) -> None:
        clear_project_agent_cache()
        project = _project_agent(tmp_path / "repo", "repobot")
        sessions = _sessions(("member", "alice"))
        sessions._pool_cwd = str(project)
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("repobot")),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.error_code != AGENT_NOT_FOUND_CODE
        assert "repobot" not in (info.error or "")

    @pytest.mark.parametrize("builtin", sorted(KIRO_CLI_BUILTIN_AGENTS))
    def test_spawn_inheritedKiroCliBuiltinTemplate_notRefused(self, builtin: str) -> None:
        sessions = _sessions(("member", "alice"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member(builtin)),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.error_code != AGENT_NOT_FOUND_CODE
        assert builtin not in (info.error or "")

    @pytest.mark.asyncio
    async def test_checkAgentOffLoop_inheritedProjectAgent_foundOnceCacheIsWarm(
        self, tmp_path
    ) -> None:
        project = _project_agent(tmp_path / "repo", "repobot")
        resolved = os.path.realpath(project)
        config = _config_with_member("repobot")
        config.agent.subagent_cwd_allowed_roots = [str(tmp_path)]
        context = {"selection_kind": "member", "template_id": "repobot"}
        manager = SubagentManager(sessions=_sessions(("member", "alice")), ctx_builder=None)
        clear_project_agent_cache()
        with (
            patch.object(KiroCrewConfig, "load", return_value=config),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
        ):
            cold = await manager._check_agent_off_loop("", str(project), execution_context=context)
            project_agent_names(resolved, operation="test_inherited_admission", source="unknown")
            warm = await manager._check_agent_off_loop("", str(project), execution_context=context)
        assert cold is None
        assert warm is not None
        assert warm[:2] == ("repobot", resolved)
        assert warm[4:] == ("repobot", "", "")

    def test_spawn_memberContinuationWithNoAgent_notRefusedAtAdmission(self) -> None:
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        record = ExecutionContext(
            member_id=None,
            store=MemoryStoreRef(store_id="default", member_id=None),
            selection_kind="member",
            template_id="gone",
            selection_name="alice",
        )
        sessions = _sessions(("member", "alice"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch.object(KiroCrewConfig, "load", return_value=_config_with_member("gone")),
            patch("kiro_crew.subagent_persistence.read_run_execution", return_value=record),
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn(
                "task",
                parent_session_key="dashboard:chat-1",
                conversation_key="subagent:abc123",
            )
        assert info is not None
        assert info.error_code != AGENT_NOT_FOUND_CODE
        assert "'gone' not found" not in (info.error or "")

    def test_spawn_inheritedParentTemplate_notRevalidated(self) -> None:
        sessions = _sessions(("template", "parent-agent"))
        manager = SubagentManager(sessions=sessions, ctx_builder=None)
        with (
            patch("kiro_crew.subagent.list_agents", return_value=[]),
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
        ):
            info = manager.spawn("task", parent_session_key="dashboard:chat-1")
        assert info is not None
        assert info.error_code != AGENT_NOT_FOUND_CODE
