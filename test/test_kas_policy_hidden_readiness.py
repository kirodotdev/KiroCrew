"""Session start does not wait for a managed server the agent's tool policy empties.

An app agent whose manifest declares only its own tools gets every managed tool
excluded through ``managedToolPolicy``. ``kirocrew-cron`` then connects and lists
nothing for that agent's session, so no catalog and no tool tag ever reach the
readiness barrier. Waiting for them would spend the full budget and fail every
start with ``kirocrew-cron: tools not advertised``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import mcp_start_wait, runtime_session_load
from kiro_crew.acp.mcp_session_report import McpSessionReport
from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_handle_runtime_contract import AcpRuntimeError
from kiro_crew.acp.types import ACP_BACKEND_KAS, METHOD_KAS_MCP_STATUS, JsonRpcMessage
from kiro_crew.mcp_discovery_managed import managed_tool_names_for_policy
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, finished

_SID = "sess-policy-hidden"
_AGENT = "app-agent"
_CRON = "kirocrew-cron"
_CLIENT = {"kiro": {"resource": {"source": {"origin": "client"}}}}
# Resumed shape, so a failure only unregisters (no backend round trip). The agent
# grants the whole server, as Crew's default servers or an author's edit do.
_PARAMS = {
    "sessionId": _SID,
    "mcpServers": [{"name": _CRON}],
    "_meta": {"kiro": {"customAgents": [{"id": _AGENT, "tools": ["read", f"@{_CRON}"]}]}},
}


def _cron_tools() -> list[str]:
    tools = managed_tool_names_for_policy(_CRON)
    assert tools, "kirocrew-cron declares its tools in-process"
    return tools


@pytest.fixture(autouse=True)
def sealed(_floor_monkeypatch):
    """A packaged install, which the sandbox seals; a test checkout is not one."""
    _floor_monkeypatch.setattr(
        "kiro_crew.mcp_listing_gateway._package_tree_is_sealed", lambda: True
    )


@pytest.fixture
def agents_dir(tmp_path, monkeypatch):
    path = tmp_path / "agents"
    path.mkdir()
    monkeypatch.setattr("kiro_crew.config.paths.kiro_agents_dir", lambda: path)
    return path


def _write_spec(agents_dir, policy: dict | None) -> None:
    spec: dict = {"name": _AGENT, "tools": ["read", f"@{_CRON}"]}
    if policy is not None:
        spec["managedToolPolicy"] = policy
    (agents_dir / f"{_AGENT}.json").write_text(json.dumps(spec))


class TestPolicyHiddenServers:
    def test_every_tool_excluded_hides_the_server(self, agents_dir):
        _write_spec(agents_dir, {"exclude": _cron_tools()})
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == {_CRON}

    def test_one_tool_left_keeps_the_wait(self, agents_dir):
        _write_spec(agents_dir, {"exclude": _cron_tools()[1:]})
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == frozenset()

    def test_a_server_withheld_whole_is_hidden(self, agents_dir):
        _write_spec(agents_dir, {"excludeServers": [_CRON]})
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == {_CRON}

    def test_no_policy_hides_nothing(self, agents_dir):
        _write_spec(agents_dir, None)
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == frozenset()

    def test_an_unsealed_install_names_no_tool_and_hides_nothing(self, agents_dir, monkeypatch):
        _write_spec(agents_dir, {"exclude": _cron_tools()})
        monkeypatch.setattr("kiro_crew.mcp_listing_gateway._package_tree_is_sealed", lambda: False)

        imported: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.mcp_discovery_managed.managed_tool_names_for_policy",
            lambda name: imported.append(name) or [],
        )
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == frozenset()
        assert imported == []

    def test_an_unreadable_spec_hides_nothing(self, agents_dir):
        (agents_dir / f"{_AGENT}.json").write_text("{not json")
        assert mcp_start_wait.policy_hidden_servers(_AGENT, (_CRON,)) == frozenset()


def _empty_cron_status() -> JsonRpcMessage:
    # What the host reports for a server that lists no tool: connected, empty catalog.
    server = {"name": _CRON, "status": "connected", "tools": [], "_meta": _CLIENT}
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": [server]}
    )


def _handle() -> AcpSessionHandle:
    handle = AcpSessionHandle.__new__(AcpSessionHandle)
    handle._session_id = _SID
    handle._mcp_report = McpSessionReport()
    handle._mcp_report.begin_session([])
    handle._queue = asyncio.Queue()
    frame = _empty_cron_status()
    handle._mcp_report.fold_frame(frame)
    handle._queue.put_nowait(frame)
    handle._apply_init_notification = lambda *_a, **_k: None  # type: ignore[method-assign]
    # The bare handle has no runtime; the late model-select read is not under test.
    handle.settle_kas_model_select = AsyncMock()  # type: ignore[method-assign]
    return handle


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_session_load, "_declared_governed", lambda: False)
    monkeypatch.setattr(runtime_session_load, "MOUNTED_MCP_WAIT_SECS", 0)
    return AcpRuntime(work_dir=tmp_path / "ws", sandbox_mode="off", acp_backend=ACP_BACKEND_KAS)


@pytest.mark.asyncio
async def test_a_server_the_policy_empties_does_not_hold_the_start(runtime, agents_dir):
    _write_spec(agents_dir, {"exclude": _cron_tools()})
    await finished(
        runtime._wait_managed_mcp(_handle(), _PARAMS, _AGENT, FALLBACK_TIMEOUT_SECS, 0),
        "start barrier",
    )


@pytest.mark.asyncio
async def test_a_server_with_a_tool_left_still_has_to_show_it(runtime, agents_dir):
    _write_spec(agents_dir, {"exclude": _cron_tools()[1:]})
    with pytest.raises(AcpRuntimeError, match="kirocrew-cron: tools not advertised"):
        await runtime._wait_managed_mcp(_handle(), _PARAMS, _AGENT, 0.2, 0)


# The PPTX Maker app's Spec agent, as Crew registers and launches it on KAS: its
# manifest declares only the app's own tools, so registration excludes every
# managed tool, `skill_search` aside.
_APP = "pptx-maker"
_APP_AGENT = "pptx-maker-spec"
_CORE = "kirocrew-core"


def _register_app_agents() -> None:
    """Install the shipped app's record and register its agents, as enabling it does."""
    from kiro_crew.apps import bridges, manager
    from kiro_crew.apps.execution import shipped_builtin_app_root
    from kiro_crew.apps.manifest import AppManifest

    root = shipped_builtin_app_root(_APP)
    assert root is not None
    raw = json.loads((root / "app.json").read_text())
    manager.app_dir(_APP).mkdir(parents=True, exist_ok=True)
    manager._write_installed(
        _APP,
        manager.InstalledApp(
            name=_APP,
            version=raw["version"],
            displayName=raw["displayName"],
            enabled=True,
            source="builtin",
            origin="builtin",
        ),
    )
    bridges._register_agents(_APP, AppManifest.from_dict(raw), root, [])


async def _launched_params() -> dict:
    from kiro_crew.acp.harness.kas import KasHarness

    extras = await KasHarness().session_extras(
        _APP_AGENT, work_dir=None, session_key="dashboard:policy-hidden"
    )
    return {
        "sessionId": _SID,
        "mcpServers": [],
        "_meta": {"kiro": {"customAgents": extras.custom_agents}},
    }


def _app_handle() -> AcpSessionHandle:
    """Core lists `skill_search`; cron lists nothing, as the policy makes it."""
    core = {"name": _CORE, "status": "connected", "_meta": _CLIENT}
    core["tools"] = [{"name": "skill_search", "description": "probe", "disabled": False}]
    cron = {"name": _CRON, "status": "connected", "tools": [], "_meta": _CLIENT}
    frame = JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": _SID, "servers": [core, cron]}
    )
    handle = _handle()
    handle._queue = asyncio.Queue()
    handle._mcp_report.fold_frame(frame)
    handle._queue.put_nowait(frame)
    return handle


class TestTheLaunchedAppSpec:
    @pytest.mark.asyncio
    async def test_the_registered_spec_mounts_no_managed_server(self):
        from kiro_crew.acp.mcp_session_report import required_managed_servers

        _register_app_agents()
        params = await _launched_params()
        assert required_managed_servers(params, _APP_AGENT) == ()

    @pytest.mark.asyncio
    async def test_crew_s_default_servers_on_an_unrecognised_app_agent_still_start(
        self, runtime, monkeypatch
    ):
        # When the owner lookup misses the app (it cannot read the app's files),
        # Crew adds and grants its default servers. The registered spec's policy
        # still hides every cron tool, so cron lists nothing.
        _register_app_agents()
        monkeypatch.setattr("kiro_crew.apps.manager.owning_app_for_agent", lambda agent: None)
        params = await _launched_params()
        tools = params["_meta"]["kiro"]["customAgents"][0]["tools"]
        assert f"@{_CRON}" in tools and f"@{_CORE}" in tools
        assert mcp_start_wait.policy_hidden_servers(_APP_AGENT, (_CORE, _CRON)) == {_CRON}
        await finished(
            runtime._wait_managed_mcp(_app_handle(), params, _APP_AGENT, FALLBACK_TIMEOUT_SECS, 0),
            "start barrier",
        )
