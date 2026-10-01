"""A mode name that is a generated skill view maps back to the agent it was built from.

kiro-cli answers ``session/set_mode`` with ``Mode '<name>' not found`` for a view
the running process never loaded. These tests pin both halves of the fix:
a stored view name maps back to the agent it was built from before anything is
sent, and the error for a view kiro-cli has not loaded names that agent and a
repair that keeps the operator's config. Recovering a view changed under a
warm runtime is the set_mode bracket's job, not this file's.
"""

from __future__ import annotations

import gc
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError, _format_runtime_rpc_error

VIEW = "kirocrew-skill-view-" + "a" * 24
OTHER_VIEW = "kirocrew-skill-view-" + "b" * 24


@pytest.fixture(autouse=True)
def _fresh_view_memory(monkeypatch, tmp_path):
    # No test reads this host's own agents directory for a sidecar.
    monkeypatch.setattr(projection, "_VIEW_SOURCES", {})
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: tmp_path / "no-agents")


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    (agents / "custom.json").write_text('{"name":"custom","description":"v1"}', encoding="utf-8")
    return agents, project


# ── A stored view name maps back to its source agent ──


def test_a_view_from_an_earlier_process_maps_back_through_its_sidecar(native_tree):
    agents, project = native_tree
    prepared = projection.prepare_native_skill_projection(project)
    stored = prepared.agent("custom")
    # A new process: nothing in memory, and the view file itself is gone.
    projection._VIEW_SOURCES.clear()
    (agents / f"{stored}.json").unlink()
    assert projection.source_agent_name(stored) == "custom"


def test_a_view_resolves_after_the_boot_drain_removed_alias_and_sidecar(native_tree):
    agents, project = native_tree
    prepared = projection.prepare_native_skill_projection(project)
    stored = prepared.agent("custom")
    # A gateway restart: nothing in memory, and the drain took both files.
    projection._VIEW_SOURCES.clear()
    (agents / f"{stored}.json").unlink()
    (agents / projection._PROJECTION_METADATA_DIR_NAME / f"{stored}.json").unlink()
    assert projection.source_agent_name(stored) == "custom"


def test_the_view_ledger_keeps_only_its_newest_admissible_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(projection, "_VIEW_SOURCES_MAX", 2)
    names = ["kirocrew-skill-view-" + f"{n:024x}" for n in range(3)]
    projection._record_view_ledger(tmp_path, {"a": names[0], "b": names[1]})
    projection._record_view_ledger(tmp_path, {"c": names[2], "x" * 10_000: VIEW})
    assert projection._read_view_ledger(tmp_path) == {names[1]: "b", names[2]: "c"}
    (tmp_path / projection._VIEW_LEDGER_NAME).write_text(
        '{"../escape": "a", "%s": "%s"}' % (names[0], OTHER_VIEW), encoding="utf-8"
    )
    assert projection._read_view_ledger(tmp_path) == {}


def test_the_view_memory_retains_no_oversized_agent_name():
    projection._remember_view_sources(
        {"x" * 10_000: VIEW, "a\nforged": VIEW, OTHER_VIEW: VIEW, "custom": OTHER_VIEW}
    )
    assert projection._VIEW_SOURCES == {OTHER_VIEW: "custom"}


def test_a_view_nothing_records_is_refused_not_guessed(native_tree):
    with pytest.raises(projection.RetiredSkillView, match="Pick the agent"):
        projection.source_agent_name(VIEW)


def test_an_agent_name_passes_through_without_a_read(monkeypatch):
    monkeypatch.setattr(projection, "kiro_agents_dir", MagicMock(side_effect=AssertionError))
    assert projection.source_agent_name("custom") == "custom"


def test_set_mode_never_sends_a_stale_view_name(native_tree):
    agents, project = native_tree
    first = projection.prepare_native_skill_projection(project)
    stale = first.agent("custom")
    del first
    gc.collect()
    (agents / "custom.json").write_text('{"name":"custom","description":"v2"}', encoding="utf-8")
    current = projection.prepare_native_skill_projection(project)
    assert current.agent("custom") != stale
    sent = current.request("session/set_mode", {"sessionId": "s", "modeId": stale})
    assert sent["modeId"] == current.agent("custom")
    frame = current.frame({"result": {"modes": {"currentModeId": stale}}})
    assert frame["result"]["modes"]["currentModeId"] == "custom"


def test_the_projection_refuses_a_view_it_cannot_attribute():
    prepared = projection.NativeSkillProjection({"custom": OTHER_VIEW})
    assert prepared.agent(OTHER_VIEW) == OTHER_VIEW
    with pytest.raises(projection.RetiredSkillView):
        prepared.request("session/set_mode", {"modeId": VIEW})


@pytest.mark.asyncio
async def test_create_and_load_map_a_stored_view_before_any_guard():
    projection._remember_view_sources({"custom": VIEW})
    rt = AcpRuntime(work_dir="/tmp")
    assert await rt._source_agent(VIEW) == "custom"
    assert await rt._source_agent("kirocrew") == "kirocrew"
    assert await rt._source_agent(None) is None
    with pytest.raises(AcpRuntimeError, match="Pick the agent"):
        await rt._source_agent(OTHER_VIEW)


@pytest.mark.asyncio
async def test_a_handle_set_mode_sends_the_source_agent():
    from kiro_crew.acp.session_handle import AcpSessionHandle

    projection._remember_view_sources({"custom": VIEW})
    runtime = MagicMock()
    runtime.send_request = AsyncMock()
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._runtime = runtime
    handle._session_id = "s"
    await handle.set_mode(VIEW)
    params = runtime.send_request.await_args.args[1]
    assert params["modeId"] == "custom"


# ── The error names the source agent and keeps the operator's config ──


def _mode_not_found(name: str) -> dict:
    return {"code": -32603, "message": "Internal error", "data": f"Mode '{name}' not found"}


def test_a_missing_view_error_names_its_source_agent():
    projection._remember_view_sources({"custom": VIEW})
    text = _format_runtime_rpc_error(_mode_not_found(VIEW))
    assert "agent 'custom'" in text and VIEW in text
    assert "setup" not in text


def test_a_missing_view_nothing_remembers_still_reads_as_a_view(tmp_path):
    text = _format_runtime_rpc_error(_mode_not_found(OTHER_VIEW))
    assert "skill view" in text and "this agent" in text
    assert "setup" not in text


def test_a_missing_real_spec_suggests_setup_without_clean(tmp_path):
    with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
        text = _format_runtime_rpc_error(_mode_not_found("kirocrew"))
    assert "kirocrew setup --agent-only`" in text
    assert "--clean" not in text


def test_the_direct_client_launches_the_source_agent(tmp_path):
    from kiro_crew.acp.client import AcpClient, AcpError

    projection._remember_view_sources({"custom": VIEW})
    client = AcpClient(work_dir=tmp_path / "wd", agent=VIEW, sandbox_mode="off")
    client._prepare_spawn_workspace()
    assert client._agent == "custom"
    client = AcpClient(work_dir=tmp_path / "wd", agent=OTHER_VIEW, sandbox_mode="off")
    with pytest.raises(AcpError, match="Pick the agent"):
        client._prepare_spawn_workspace()
