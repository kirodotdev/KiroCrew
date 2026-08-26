"""Focused tests for the official Codex ACP backend seam."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.acp import client as client_mod
from kiro_crew.acp.client import (
    CODEX_AUTH_REQUIRED_MESSAGE,
    AcpAuthRequired,
    AcpClient,
    AcpError,
    AcpModelUnavailable,
    CodexAcpClient,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_ADVERTISED_MODEL_GATING,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_AGENT_SPEC,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_BACKENDS_KIRO_PREREQUISITE,
    ACP_BACKENDS_PROMPT_COMMANDS,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    CODEX_CLIENT_CAPABILITIES,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODEL,
)
from kiro_crew.config.loader import KiroCrewConfig, _normalize_acp_backend
from kiro_crew.dashboard import kiro_readiness
from kiro_crew.providers import acp as provider_mod
from kiro_crew.providers.codex import CodexAcpProvider


def test_codex_capabilities_are_explicit_and_fail_closed() -> None:
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_SELECTABLE
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_INTERNAL_SANDBOX
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_ADVERTISED_MODEL_GATING
    assert ACP_BACKEND_CODEX in ACP_BACKENDS_PROMPT_COMMANDS
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_PREREQUISITE
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_AGENT_SPEC
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_SESSION_SHARING
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_STEER
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_ACP_RUNTIME
    assert ACP_BACKEND_CODEX not in ACP_BACKENDS_KIRO_IDENTITY_STORE


def test_codex_is_selected_only_at_provider_registry_seam(tmp_path) -> None:
    from kiro_crew.acp.client import CodexAcpClient
    from kiro_crew.platform.defaults import DefaultProviderRegistry
    from kiro_crew.providers.codex import CodexAcpProvider

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX

    provider = DefaultProviderRegistry().create_factory(cfg)(
        session_key="test:codex-registry",
        cwd=str(tmp_path),
    )

    assert isinstance(provider, CodexAcpProvider)
    assert isinstance(provider.client, CodexAcpClient)


def test_shared_acp_client_construction_has_no_codex_dispatch() -> None:
    import inspect

    for method_name in ("_spawn", "_new_session_following_substitution", "_initialize_session"):
        source = inspect.getsource(getattr(AcpClient, method_name))
        assert "_is_codex" not in source
        assert "CODEX_" not in source

    startup_source = inspect.getsource(AcpClient._apply_startup_model)
    assert "self._is_kiro and self._model_is_unusable" in startup_source
    assert "ACP_BACKENDS_ADVERTISED_MODEL_GATING" not in startup_source


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="exercises a POSIX executable bit; Windows resolution is covered by "
    "test_resolve_codex_acp_windows_npm_shim_to_node_argv below",
)
def test_resolve_codex_acp_from_augmented_path(tmp_path, monkeypatch) -> None:
    adapter = tmp_path / "codex-acp"
    adapter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    adapter.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(client_mod, "_mise_which", lambda _tool: None)

    assert client_mod._resolve_codex_acp_bin() == str(adapter)


def test_resolve_codex_acp_windows_npm_shim_to_node_argv(tmp_path, monkeypatch) -> None:
    prefix = tmp_path / "npm"
    shim = prefix / "codex-acp.cmd"
    node = prefix / "node.exe"
    package = prefix / "node_modules" / "@agentclientprotocol" / "codex-acp"
    entry = package / "dist" / "cli.js"
    entry.parent.mkdir(parents=True)
    shim.write_text("@echo off\n", encoding="utf-8")
    node.write_text("node", encoding="utf-8")
    entry.write_text("console.log('codex-acp')\n", encoding="utf-8")
    (package / "package.json").write_text(
        '{"bin":{"codex-acp":"dist/cli.js"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(client_mod.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(client_mod, "_resolve_codex_acp_bin", lambda: str(shim))

    assert client_mod._resolve_codex_acp_argv() == [str(node), str(entry.resolve())]


@pytest.mark.asyncio
async def test_missing_codex_adapter_has_backend_specific_diagnostic(tmp_path) -> None:
    client = CodexAcpClient(work_dir=tmp_path)
    with patch.object(
        client_mod, "_resolve_codex_acp_argv_for_spawn", AsyncMock(return_value=None)
    ):
        with pytest.raises(AcpError, match="@agentclientprotocol/codex-acp"):
            await client._spawn()


@pytest.mark.asyncio
async def test_codex_initialize_uses_verified_protocol_capabilities(tmp_path) -> None:
    client = CodexAcpClient(work_dir=tmp_path)
    client._session_id = "session"
    sent: dict = {}

    async def send_request(method: str, params: dict) -> int:
        if method == "initialize":
            sent.update(params)
        return 1

    async def wait_for_response(_request_id: int, timeout: float = 0) -> dict:
        return {"protocolVersion": 1, "agentCapabilities": {}}

    client._send_request = send_request  # type: ignore[assignment]
    client._wait_for_response = wait_for_response  # type: ignore[assignment]
    client._drain_notifications = AsyncMock()  # type: ignore[assignment]

    await client._initialize_session()

    assert sent["protocolVersion"] == 1
    assert sent["clientCapabilities"] == CODEX_CLIENT_CAPABILITIES


@pytest.mark.asyncio
async def test_codex_startup_model_gating_stays_in_dedicated_initializer(tmp_path) -> None:
    client = CodexAcpClient(work_dir=tmp_path, model="gpt-5.6-sol")
    client._session_id = "session"
    client._available_models = [{"modelId": "gpt-5.6-terra"}]
    requests: list[tuple[str, dict]] = []

    async def send_request(method: str, params: dict) -> int:
        requests.append((method, params))
        return len(requests)

    client._send_request = send_request  # type: ignore[assignment]
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"protocolVersion": 1, "agentCapabilities": {}}
    )
    client._drain_notifications = AsyncMock()  # type: ignore[assignment]

    await client._initialize_session()

    assert all(method != METHOD_SET_MODEL for method, _params in requests)
    assert client._model == client_mod.DEFAULT_MODEL


def test_codex_is_a_selectable_config_backend() -> None:
    assert _normalize_acp_backend(ACP_BACKEND_CODEX) == ACP_BACKEND_CODEX


def test_codex_managed_mcp_descriptors_do_not_require_kiro_agent_spec(tmp_path) -> None:
    client = CodexAcpClient(
        work_dir=tmp_path,
        channel_id="slack:channel-1",
    )
    with patch.object(client_mod, "ensure_agent_materialized") as materialize:
        servers = client._codex_session_mcp_servers()

    names = {entry["name"] for entry in servers}
    assert {"kirocrew-core", "kirocrew-cron"} <= names
    assert "kirocrew-dashboard" not in names
    materialize.assert_not_called()
    for entry in servers:
        assert isinstance(entry["command"], str)
        assert isinstance(entry["args"], list)
        assert isinstance(entry["env"], list)
        assert "--channel-id" not in entry["args"]


def test_config_loader_imports_before_acp_package() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import kiro_crew.config.loader"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_codex_session_new_receives_managed_mcp_descriptors(tmp_path) -> None:
    client = CodexAcpClient(work_dir=tmp_path)
    descriptor = {"name": "kirocrew-core", "command": "python", "args": [], "env": []}
    client._codex_session_mcp_servers = MagicMock(return_value=[descriptor])  # type: ignore[method-assign]
    sent: dict = {}

    async def send_request(method: str, params: dict) -> int:
        assert method == METHOD_SESSION_NEW
        sent.update(params)
        return 1

    client._send_request = send_request  # type: ignore[assignment]
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"sessionId": "codex-session"}
    )

    response = await client._new_session_following_substitution()

    assert response["sessionId"] == "codex-session"
    assert sent == {"cwd": str(tmp_path), "mcpServers": [descriptor]}


@pytest.mark.asyncio
async def test_codex_session_load_uses_id_cwd_and_mcp_without_kiro_meta(tmp_path) -> None:
    client = CodexAcpClient(work_dir=tmp_path)
    client.set_resume_session_id("resume-me")
    descriptor = {"name": "kirocrew-core", "command": "python", "args": [], "env": []}
    client._codex_session_mcp_servers = MagicMock(return_value=[descriptor])  # type: ignore[method-assign]
    requests: list[tuple[str, dict]] = []

    async def send_request(method: str, params: dict) -> int:
        requests.append((method, params))
        return len(requests)

    async def wait_for_response(request_id: int, timeout: float = 0) -> dict:
        method = requests[request_id - 1][0]
        if method == "initialize":
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}}
        if method == METHOD_SESSION_LOAD:
            return {
                "sessionId": "resume-me",
                "modes": {"availableModes": []},
                "models": {"availableModels": [], "currentModelId": "served"},
            }
        raise AssertionError(method)

    client._send_request = send_request  # type: ignore[assignment]
    client._wait_for_response = wait_for_response  # type: ignore[assignment]
    client._drain_notifications = AsyncMock()  # type: ignore[assignment]

    await client._initialize_session()

    load_params = next(params for method, params in requests if method == METHOD_SESSION_LOAD)
    assert load_params == {
        "sessionId": "resume-me",
        "cwd": str(tmp_path),
        "mcpServers": [descriptor],
    }
    assert client.resumed is True


@pytest.mark.asyncio
async def test_codex_reject_once_uses_advertised_permission_option() -> None:
    client = CodexAcpClient()
    client._permission_options[7] = {"reject": "reject_once"}
    client._send_response = AsyncMock()  # type: ignore[method-assign]

    await client.reject_tool(7)

    client._send_response.assert_awaited_once_with(  # type: ignore[attr-defined]
        7,
        {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
    )


def test_codex_captures_advertised_models_usage_config_and_effort() -> None:
    client = CodexAcpClient()
    client._sync_effort_levels = MagicMock()  # type: ignore[method-assign]
    response = {
        "models": {
            "currentModelId": "served-model",
            "availableModels": [{"modelId": "served-model", "name": "Served", "description": ""}],
        },
        "configOptions": [
            {
                "id": "reasoning_effort",
                "options": [{"value": "medium"}, {"value": "high"}],
            }
        ],
        "modes": {"availableModes": [{"id": "read-only"}]},
    }

    client._capture_available_models(response)
    client._store_session_config(response)
    client._track_usage_update(
        client_mod.JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "usage_update",
                    "used": 12,
                    "size": 100,
                }
            },
        )
    )

    assert client.available_models()[0]["modelId"] == "served-model"
    assert client.effort_config_id == "reasoning_effort"
    assert client.get_valid_effort_levels() == ["medium", "high"]
    assert client.last_prompt_stats.context_used_tokens == 12
    assert client.last_prompt_stats.context_window_tokens == 100


def test_codex_completed_mcp_result_shape_emits_tool_result() -> None:
    client = CodexAcpClient()
    message = client_mod.JsonRpcMessage(
        method="session/update",
        params={
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "managed-mcp-call",
                "status": "completed",
                "rawOutput": {
                    "error": None,
                    "result": {
                        "content": [{"type": "text", "text": "managed MCP result"}],
                        "isError": False,
                    },
                },
            }
        },
    )

    event = client._extract_tool_call_update(message)

    assert event is not None
    assert event.kind == client_mod.EVENT_TOOL_RESULT
    assert event.tool_call_id == "managed-mcp-call"
    assert event.tool_output == "managed MCP result"
    assert event.tool_final is True


def test_codex_auth_error_has_codex_remediation() -> None:
    error = {"code": -32000, "message": "Authentication required", "data": "unauthorized"}
    with pytest.raises(AcpAuthRequired, match="codex login"):
        client_mod._raise_acp_error(
            error,
            auth_required_message=CODEX_AUTH_REQUIRED_MESSAGE,
        )


@pytest.mark.asyncio
async def test_codex_model_switch_uses_only_advertised_ids() -> None:
    client = CodexAcpClient()
    client._session_id = "session"
    client._available_models = [{"modelId": "served", "name": "Served"}]
    client._send_request = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(return_value={})  # type: ignore[method-assign]

    await client.set_model("served")
    client._send_request.assert_awaited_once_with(  # type: ignore[attr-defined]
        METHOD_SET_MODEL,
        {"sessionId": "session", "modelId": "served"},
    )
    with pytest.raises(AcpModelUnavailable, match="codex login status"):
        await client.set_model("not-advertised")


def test_codex_provider_never_writes_kiro_overlays(tmp_path) -> None:
    provider = CodexAcpProvider(
        work_dir=tmp_path,
        model="auto",
        effort_per_model={"auto": "high"},
        tool_search=True,
    )
    with (
        patch.object(provider_mod, "_write_cli_overlay") as effort_overlay,
        patch.object(provider_mod, "_write_tool_search_overlay") as tool_overlay,
    ):
        provider._apply_effort_overlay()
        provider._apply_tool_search_overlay()
    effort_overlay.assert_not_called()
    tool_overlay.assert_not_called()


@pytest.mark.asyncio
async def test_pending_codex_switch_keeps_running_kiro_readiness_gate() -> None:
    from types import SimpleNamespace

    request = MagicMock()
    request.path = "/api/models"
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO),
        )
    }
    pending = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_CODEX))

    with (
        patch.object(KiroCrewConfig, "load", return_value=pending),
        patch.object(kiro_readiness, "kiro_verified_ready", AsyncMock(return_value=False)),
    ):
        response = await kiro_readiness.reject_if_kiro_unverified(request)

    assert response is not None
    assert response.status == 503


@pytest.mark.asyncio
async def test_restarted_codex_bypasses_kiro_prerequisite_checks() -> None:
    from types import SimpleNamespace

    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(acp_backend=ACP_BACKEND_CODEX),
        )
    }
    stale = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO))

    with (
        patch.object(KiroCrewConfig, "load", return_value=stale),
        patch.object(kiro_readiness, "kiro_verified_ready", AsyncMock()) as readiness,
    ):
        response = await kiro_readiness.reject_if_kiro_unverified(request)

    assert response is None
    readiness.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_running_backend_fails_closed_to_kiro_readiness() -> None:
    from types import SimpleNamespace

    request = MagicMock()
    request.path = "/api/models"
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(acp_backend="unknown-adapter"),
            kiro_prerequisite_service=object(),
        )
    }

    with patch.object(
        kiro_readiness,
        "kiro_verified_ready",
        AsyncMock(return_value=False),
    ) as readiness:
        response = await kiro_readiness.reject_if_kiro_unverified(request)

    assert response is not None
    assert response.status == 503
    readiness.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_background_work_uses_the_dedicated_provider_path() -> None:
    from kiro_crew import session as session_mod

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    provider = MagicMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    factory = MagicMock(return_value=provider)
    manager = session_mod.SessionManager(cfg, provider_factory=factory)

    class _UnexpectedRuntime:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("Codex background work must not construct AcpRuntime")

    with patch("kiro_crew.acp.runtime.AcpRuntime", _UnexpectedRuntime):
        handle = await manager.get_bg_session()

    factory.assert_called_once_with(
        session_mod.BACKGROUND_KEY,
        agent=session_mod.BACKGROUND_AGENT,
    )
    provider.start.assert_awaited_once()
    assert handle._sess.provider is provider


@pytest.mark.asyncio
async def test_refresh_defaults_preserves_the_running_backend_until_restart() -> None:
    from kiro_crew import session as session_mod

    running = KiroCrewConfig()
    running.agent.acp_backend = ACP_BACKEND_KIRO
    manager = session_mod.SessionManager(running)
    pending = KiroCrewConfig()
    pending.agent.acp_backend = ACP_BACKEND_CODEX
    rebuilt_factory = MagicMock()

    with (
        patch.object(session_mod.KiroCrewConfig, "load", return_value=pending),
        patch.object(session_mod, "build_provider_factory", return_value=rebuilt_factory) as build,
        patch.object(manager, "start_pool", AsyncMock()),
    ):
        await manager.refresh_defaults()

    assert manager.acp_backend == ACP_BACKEND_KIRO
    assert manager._cfg.agent.acp_backend == ACP_BACKEND_KIRO
    build.assert_called_once_with(pending)


@pytest.mark.asyncio
async def test_session_reload_preserves_the_running_backend_until_gateway_restart() -> None:
    from kiro_crew import session as session_mod

    running = KiroCrewConfig()
    running.agent.acp_backend = ACP_BACKEND_KIRO
    manager = session_mod.SessionManager(running)
    pending = KiroCrewConfig()
    pending.agent.acp_backend = ACP_BACKEND_CODEX
    rebuilt_factory = MagicMock()

    with (
        patch.object(session_mod.KiroCrewConfig, "load", return_value=pending),
        patch.object(session_mod, "build_provider_factory", return_value=rebuilt_factory) as build,
        patch.object(manager, "start_pool", AsyncMock()),
    ):
        await manager.reload_provider_factory()

    assert manager.acp_backend == ACP_BACKEND_KIRO
    assert manager._cfg.agent.acp_backend == ACP_BACKEND_KIRO
    build.assert_called_once_with(pending)


def test_codex_is_advertised_in_generated_config_schema() -> None:
    from kiro_crew.config.schema import SCHEMA_REGISTRY

    entry = next(item for item in SCHEMA_REGISTRY if item.path == "agent.acp_backend")
    assert entry.enum_values == ["", ACP_BACKEND_CODEX, "kas"]
    assert "@agentclientprotocol/codex-acp" in entry.help


@pytest.mark.asyncio
async def test_codex_models_use_the_provider_abc_contract() -> None:
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import agents
    from kiro_crew.providers.base import LLMProvider

    provider = MagicMock(spec=LLMProvider)
    provider.available_models.return_value = [
        {
            "modelId": "registered-model",
            "name": "Registered model",
            "description": "Advertised through the provider seam",
        }
    ]
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(
                acp_backend=ACP_BACKEND_CODEX,
                active_providers=lambda: [provider],
            )
        )
    }

    response = await agents.api_models(request)

    assert json.loads(response.text or "") == [
        {"model_name": "auto", "display_name": "Auto", "description": ""},
        {
            "model_name": "registered-model",
            "display_name": "Registered model",
            "description": "Advertised through the provider seam",
        },
    ]


@pytest.mark.asyncio
async def test_pending_kiro_switch_keeps_running_codex_models() -> None:
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import agents

    older = MagicMock(is_codex_backend=True)
    older.available_models.return_value = [{"modelId": "older", "name": "Older"}]
    newest = MagicMock(is_codex_backend=True)
    newest.available_models.return_value = [
        {
            "modelId": "adapter-model",
            "name": "Adapter model",
            "description": "Advertised live",
        }
    ]
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(
            sessions=SimpleNamespace(
                acp_backend=ACP_BACKEND_CODEX,
                active_providers=lambda: [older, newest],
            )
        )
    }
    pending = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO))

    with (
        patch.object(agents.KiroCrewConfig, "load", return_value=pending) as load,
        patch.object(agents, "reject_if_kiro_unverified", AsyncMock()) as readiness,
        patch.object(agents, "create_subprocess_limited", AsyncMock()) as spawn,
    ):
        response = await agents.api_models(request)

    assert json.loads(response.text or "") == [
        {"model_name": "auto", "display_name": "Auto", "description": ""},
        {
            "model_name": "adapter-model",
            "display_name": "Adapter model",
            "description": "Advertised live",
        },
    ]
    load.assert_not_called()
    readiness.assert_not_awaited()
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_kiro_switch_keeps_running_codex_usage() -> None:
    import json
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import sessions

    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(sessions=SimpleNamespace(acp_backend=ACP_BACKEND_CODEX))
    }
    pending = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO))
    with (
        patch.object(KiroCrewConfig, "load", return_value=pending) as load,
        patch.object(sessions, "reject_if_kiro_unverified", AsyncMock()) as readiness,
        patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch,
    ):
        response = await sessions.api_sessions_usage(request)

    assert json.loads(response.text or "") == {"usage": {"available": False, "backend": "codex"}}
    load.assert_not_called()
    readiness.assert_not_awaited()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_codex_switch_keeps_running_kiro_endpoint_guards() -> None:
    from types import SimpleNamespace

    from kiro_crew.dashboard.handlers import agents, sessions

    request = MagicMock()
    request.app = {"state": SimpleNamespace(sessions=SimpleNamespace(acp_backend=ACP_BACKEND_KIRO))}
    pending = SimpleNamespace(agent=SimpleNamespace(acp_backend=ACP_BACKEND_CODEX))
    blocked = MagicMock(status=503)
    readiness = AsyncMock(return_value=blocked)

    with (
        patch.object(KiroCrewConfig, "load", return_value=pending) as load,
        patch.object(agents, "reject_if_kiro_unverified", readiness),
        patch.object(sessions, "reject_if_kiro_unverified", readiness),
        patch.object(agents, "create_subprocess_limited", AsyncMock()) as spawn,
        patch.object(sessions, "_fetch_usage_bg", AsyncMock()) as fetch,
    ):
        models_response = await agents.api_models(request)
        usage_response = await sessions.api_sessions_usage(request)

    assert models_response is blocked
    assert usage_response is blocked
    load.assert_not_called()
    assert readiness.await_count == 2
    spawn.assert_not_awaited()
    fetch.assert_not_awaited()


def test_codex_doctor_reports_adapter_and_skips_kiro_dependencies(
    tmp_path, capsys, monkeypatch
) -> None:
    import urllib.error

    from kiro_crew import cli_doctor

    def _codex_config() -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agent.acp_backend = ACP_BACKEND_CODEX
        return cfg

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: _codex_config()))
    mock_run = MagicMock(returncode=0, stdout="tool 1.0.0", stderr="")
    with (
        patch.object(
            cli_doctor.shutil,
            "which",
            side_effect=lambda binary, **_kwargs: f"/usr/local/bin/{binary}",
        ),
        patch.object(cli_doctor, "_resolve_codex_acp_bin", return_value="/opt/codex-acp"),
        patch.object(cli_doctor, "KIRO_AGENTS_DIR", tmp_path),
        patch.object(cli_doctor.subprocess, "run", return_value=mock_run),
        patch.object(
            cli_doctor.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("no gateway"),
        ),
        patch.object(cli_doctor, "is_local_only", return_value=True),
        patch.object(cli_doctor, "config_dir", return_value=tmp_path),
        patch.object(cli_doctor, "_load_llama_class", return_value=object),
        patch.object(cli_doctor, "model_file_present", return_value=False),
        patch.object(cli_doctor.sandbox, "detect_backend", return_value="namespace"),
        patch.object(KiroCrewConfig, "load_credentials", return_value={}),
        patch.object(cli_doctor, "_agent_spec_model_problems") as model_pins,
        patch.object(cli_doctor, "doctor_dead_paths") as dead_paths,
        patch.object(cli_doctor, "_doctor_cli_installer_residue") as installer_residue,
        patch.object(cli_doctor, "_doctor_mcp_tools") as mcp_tools,
    ):
        cli_doctor._doctor()

    out = capsys.readouterr().out
    assert "codex-acp:   ✅ /opt/codex-acp" in out
    assert "codex auth:  checked by the adapter at session start" in out
    assert "Kiro agent spec not required by selected backend" in out
    assert "Kiro agent specs not used" in out
    assert "Codex adapter session default / advertised models" in out
    assert "kiro-cli:    ⏭  installed but unused by backend" in out
    assert "kiro-cli:    ⏭  not used by selected backend" in out
    assert "kiro-cli login" not in out
    model_pins.assert_not_called()
    dead_paths.assert_not_called()
    installer_residue.assert_not_called()
    mcp_tools.assert_not_called()
    assert not any(call.args[0][0] == cli_doctor.KIRO_CLI_BIN for call in mock_run.call_args_list)


def test_set_codex_effort_falls_back_to_nearest_lower_advertised_level() -> None:
    """Codex only advertises a subset of EFFORT_LEVELS; a requested level the
    backend never advertised degrades to the nearest lower one it does, rather
    than failing the whole apply."""
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
    provider._client.set_config_option = AsyncMock()

    import asyncio

    asyncio.get_event_loop().run_until_complete(provider._set_codex_effort("medium"))

    provider._client.set_config_option.assert_awaited_once_with("reasoning_effort", "low")


def test_set_codex_effort_raises_when_no_lower_level_is_advertised() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client.get_valid_effort_levels = MagicMock(return_value=["high"])
    provider._client.set_config_option = AsyncMock()

    import asyncio

    with pytest.raises(ValueError, match="did not advertise effort 'low'"):
        asyncio.get_event_loop().run_until_complete(provider._set_codex_effort("low"))
    provider._client.set_config_option.assert_not_awaited()


def test_set_codex_effort_skips_silently_when_backend_advertises_no_effort_option() -> None:
    """A Codex model with no reasoning_effort config option (get_valid_effort_levels
    returns []) is a no-op, not an error -- not every Codex model supports effort."""
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client.get_valid_effort_levels = MagicMock(return_value=[])
    provider._client.set_config_option = AsyncMock()

    import asyncio

    asyncio.get_event_loop().run_until_complete(provider._set_codex_effort("high"))

    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_initial_effort_is_a_no_op_when_nothing_resolves() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._resolve_effort = MagicMock(return_value=None)
    provider._set_codex_effort = AsyncMock()

    await provider._apply_initial_effort()

    provider._set_codex_effort.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_initial_effort_applies_the_resolved_level() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._resolve_effort = MagicMock(return_value="high")
    provider._set_codex_effort = AsyncMock()

    await provider._apply_initial_effort()

    provider._set_codex_effort.assert_awaited_once_with("high")


@pytest.mark.asyncio
async def test_apply_initial_effort_logs_and_swallows_a_failed_apply() -> None:
    """Startup must not crash the session over an effort the backend rejects --
    the model still starts, just without the requested effort level applied."""
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._resolve_effort = MagicMock(return_value="high")
    provider._set_codex_effort = AsyncMock(
        side_effect=ValueError("Codex did not advertise effort 'high'")
    )

    await provider._apply_initial_effort()  # must not raise


@pytest.mark.asyncio
async def test_change_effort_returns_false_when_current_model_has_no_effort_option() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client.get_valid_effort_levels = MagicMock(return_value=[])

    assert await provider.change_effort("high") is False


@pytest.mark.asyncio
async def test_change_effort_rejects_an_invalid_level_before_touching_the_backend() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
    provider._client.set_config_option = AsyncMock()

    with pytest.raises(ValueError, match="invalid effort level"):
        await provider.change_effort("not-a-real-level")
    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_effort_applies_and_records_the_new_level_per_model() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client._model = "gpt-5-codex"
    provider._client.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
    provider._client.set_config_option = AsyncMock()

    assert await provider.change_effort("high") is True

    assert provider._effort_per_model["gpt-5-codex"] == "high"
    provider._client.set_config_option.assert_awaited_once_with("reasoning_effort", "high")


@pytest.mark.asyncio
async def test_change_effort_restores_the_prior_value_when_the_backend_call_fails() -> None:
    """A failed apply must not leave the in-memory per-model map pointing at a
    level the backend never actually accepted."""
    provider = CodexAcpProvider(work_dir=".", model="auto", effort_per_model={"gpt-5-codex": "low"})
    provider._client._model = "gpt-5-codex"
    provider._client.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
    provider._client.set_config_option = AsyncMock(side_effect=AcpError("wire rejected"))

    with pytest.raises(AcpError, match="wire rejected"):
        await provider.change_effort("high")

    assert provider._effort_per_model["gpt-5-codex"] == "low"


@pytest.mark.asyncio
async def test_change_effort_failure_with_no_prior_value_clears_the_entry() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    provider._client._model = "gpt-5-codex"
    provider._client.get_valid_effort_levels = MagicMock(return_value=["low", "high"])
    provider._client.set_config_option = AsyncMock(side_effect=AcpError("wire rejected"))

    with pytest.raises(AcpError):
        await provider.change_effort("high")

    assert "gpt-5-codex" not in provider._effort_per_model


@pytest.mark.asyncio
async def test_clear_effort_removes_the_current_models_entry_and_returns_false() -> None:
    """Codex has no separate "clear" wire call; clearing is purely a local
    bookkeeping op, unlike Kiro's overlay-backed clear_effort."""
    provider = CodexAcpProvider(work_dir=".", model="auto", effort_per_model={"auto": "high"})
    provider._client._model = "auto"

    assert await provider.clear_effort() is False
    assert "auto" not in provider._effort_per_model


@pytest.mark.asyncio
async def test_stream_command_maps_client_events_through_to_llm_event() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    sentinel = object()

    async def _events(_command: str):
        yield "raw-acp-event"

    provider._client.stream_events = _events
    provider._to_llm_event = MagicMock(return_value=sentinel)

    events = [event async for event in provider.stream_command("hello")]

    assert events == [sentinel]
    provider._to_llm_event.assert_called_once_with("raw-acp-event")


@pytest.mark.asyncio
async def test_cleanup_session_is_a_no_op_codex_owns_its_own_lifecycle() -> None:
    provider = CodexAcpProvider(work_dir=".", model="auto")
    assert await provider.cleanup_session("some-session-id") is None


def test_codex_provider_factory_resolves_background_effort_for_heartbeat_and_lite_agents(
    tmp_path,
) -> None:
    """kirocrew-lite and kirocrew-heartbeat use the background role's effort,
    never the interactive default -- matching Kiro's own background-role split."""
    from kiro_crew.providers.codex import create_codex_provider_factory

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agent.model = "auto"
    cfg.agent.reasoning_effort = "xhigh"
    cfg.agent.role_efforts["background"] = "low"

    factory = create_codex_provider_factory(cfg)

    provider = factory(session_key="test:bg", agent="kirocrew-heartbeat", cwd=str(tmp_path))

    assert isinstance(provider, CodexAcpProvider)
    # The background role's effort ("low") must win over the interactive
    # cfg.agent.reasoning_effort ("xhigh") for a background-role agent.
    assert provider._effort_per_model.get("auto") == "low"


def test_codex_provider_factory_uses_interactive_effort_for_a_normal_agent(tmp_path) -> None:
    from kiro_crew.providers.codex import create_codex_provider_factory

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agent.model = "auto"
    cfg.agent.reasoning_effort = "high"

    factory = create_codex_provider_factory(cfg)

    provider = factory(session_key="test:interactive", agent="kirocrew", cwd=str(tmp_path))

    assert provider._effort_per_model.get("auto") == "high"


def test_codex_provider_factory_respects_explicit_model_and_effort_overrides(tmp_path) -> None:
    from kiro_crew.providers.codex import create_codex_provider_factory

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.agent.model = "auto"
    cfg.agent.reasoning_effort = "low"

    factory = create_codex_provider_factory(cfg)

    provider = factory(
        session_key="test:override",
        agent="kirocrew",
        cwd=str(tmp_path),
        model_override="gpt-5-codex",
        reasoning_effort_override="xhigh",
    )

    assert provider._client._model == "gpt-5-codex"
    assert provider._effort_per_model.get("gpt-5-codex") == "xhigh"


def test_codex_provider_factory_wires_mcp_gateway_paths_only_when_stubs_are_configured(
    tmp_path,
) -> None:
    """Mirrors Kiro's own overlay/socket wiring: the gateway paths are only
    materialized when the config actually has stub servers to route through
    the overlay, so an unconfigured install never touches gateway internals."""
    from kiro_crew.providers.codex import create_codex_provider_factory

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.mcp_gateway.stub_servers = []

    factory = create_codex_provider_factory(cfg)
    provider = factory(session_key="test:no-stubs", cwd=str(tmp_path))

    assert provider._client._mcp_gateway_overlay is None
    assert provider._client._mcp_gateway_socket is None


def test_codex_provider_factory_wires_the_overlay_and_socket_when_stubs_are_configured(
    tmp_path,
) -> None:
    from kiro_crew.mcp_gateway.rewriter import default_overlay_dir, default_socket_path
    from kiro_crew.providers.codex import create_codex_provider_factory

    cfg = KiroCrewConfig()
    cfg.agent.acp_backend = ACP_BACKEND_CODEX
    cfg.mcp_gateway.stub_servers = ["some-mcp"]

    factory = create_codex_provider_factory(cfg)
    provider = factory(session_key="test:with-stubs", cwd=str(tmp_path))

    assert provider._client._mcp_gateway_overlay == str(default_overlay_dir())
    assert provider._client._mcp_gateway_socket == str(default_socket_path())
    assert provider._client._mcp_gateway_settings_mcp_json is not None
