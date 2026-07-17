"""Tests for fire_tool_hooks helper and global hook store accessor."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    ScriptHookStore,
    fire_tool_hooks,
    get_global_hook_store,
    set_global_hook_store,
)


@pytest.fixture(autouse=True)
def _reset_global_store():
    """Reset global hook store between tests."""
    set_global_hook_store(None)  # type: ignore[arg-type]
    yield
    set_global_hook_store(None)  # type: ignore[arg-type]


@pytest.fixture
def hook_store(tmp_path: Path) -> ScriptHookStore:
    return ScriptHookStore(tmp_path)


class TestGlobalHookStore:
    """Test get/set global hook store accessor."""

    def test_default_is_none(self):
        assert get_global_hook_store() is None

    def test_set_and_get(self, hook_store: ScriptHookStore):
        set_global_hook_store(hook_store)
        assert get_global_hook_store() is hook_store

    def test_overwrite(self, tmp_path: Path):
        store1 = ScriptHookStore(tmp_path / "a")
        store2 = ScriptHookStore(tmp_path / "b")
        set_global_hook_store(store1)
        set_global_hook_store(store2)
        assert get_global_hook_store() is store2


class TestFireToolHooks:
    """Test fire_tool_hooks helper."""

    @pytest.mark.asyncio
    async def test_none_store_is_noop(self):
        # Should not raise
        await fire_tool_hooks(None, "Running: echo hello")

    @pytest.mark.asyncio
    async def test_strips_running_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "Running: echo hello")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="echo hello",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_no_prefix(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "@builder-mcp/ReadFile")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="@builder-mcp/ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_parses_tool_input_json(self, hook_store: ScriptHookStore):
        ti = json.dumps({"path": "/tmp/test.txt"})
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", ti)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input={"path": "/tmp/test.txt"},
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_invalid_json_passes_none(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", "not-json")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_empty_title(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "")
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_fire_exception_swallowed(self, hook_store: ScriptHookStore):
        with patch.object(
            hook_store, "fire", new_callable=AsyncMock, side_effect=RuntimeError("boom"),
        ):
            # Should not raise
            await fire_tool_hooks(hook_store, "ReadFile")

    @pytest.mark.asyncio
    async def test_none_tool_input_skipped(self, hook_store: ScriptHookStore):
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(hook_store, "ReadFile", None)
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id=None,
                parent_session_key=None,
                agent_role=None,
            )

    @pytest.mark.asyncio
    async def test_passes_subagent_metadata(self, hook_store: ScriptHookStore):
        """When called with subagent_id, parent_session_key, agent_role, those propagate to fire()."""
        with patch.object(hook_store, "fire", new_callable=AsyncMock) as mock_fire:
            await fire_tool_hooks(
                hook_store,
                "ReadFile",
                None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            mock_fire.assert_called_once_with(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                tool_input=None,
                subagent_id="abc12345",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )


class TestScriptHookStoreFire:
    """Test ScriptHookStore.fire() emits subagent_id, parent_session_key, agent_role into hook_event.

    These tests register a real hook in the store, patch run_script_hook to capture
    the hook_event payload, and assert that the conditional emission branches in fire()
    add (or omit) the new fields correctly.
    """

    @pytest.fixture
    def fire_store(self, tmp_path: Path) -> ScriptHookStore:
        store = ScriptHookStore(tmp_path)
        store.create({
            "name": "test-hook",
            "event": HOOK_EVENT_PRE_TOOL_USE,
            "matcher": "",
            "command": "echo test",
        })
        return store

    @pytest.mark.asyncio
    async def test_fire_emits_subagent_id_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_parent_session_key_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                parent_session_key="dashboard:slot-1",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert "subagent_id" not in hook_event
            assert "agent_role" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_agent_role_when_set(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["agent_role"] == "utility"
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event

    @pytest.mark.asyncio
    async def test_fire_emits_all_three_together(self, fire_store: ScriptHookStore):
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(
                HOOK_EVENT_PRE_TOOL_USE,
                tool_name="ReadFile",
                subagent_id="sub-abc",
                parent_session_key="dashboard:slot-1",
                agent_role="utility",
            )
            (_, _, hook_event), _ = mock_run.call_args
            assert hook_event["subagent_id"] == "sub-abc"
            assert hook_event["parent_session_key"] == "dashboard:slot-1"
            assert hook_event["agent_role"] == "utility"

    @pytest.mark.asyncio
    async def test_fire_omits_all_three_when_none(self, fire_store: ScriptHookStore):
        """Backward compatibility: when all three are None (default), payload is byte-identical to pre-CR behavior."""
        with patch("kiro_crew.hooks.run_script_hook", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = type("R", (), {"hook_name": "test-hook", "exit_code": 0, "stdout": "", "stderr": "", "duration_ms": 1})()
            await fire_store.fire(HOOK_EVENT_PRE_TOOL_USE, tool_name="ReadFile")
            (_, _, hook_event), _ = mock_run.call_args
            assert "subagent_id" not in hook_event
            assert "parent_session_key" not in hook_event
            assert "agent_role" not in hook_event
