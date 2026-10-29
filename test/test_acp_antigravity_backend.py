"""Contract tests for Google Antigravity's standalone ACP harness."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.client_antigravity import _refuse_unverified_antigravity_version
from kiro_crew.acp.harness import antigravity as antigravity_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.antigravity import (
    ANTIGRAVITY_ACP_BIN_ENV,
    ANTIGRAVITY_HARNESS_PATH_ENV,
    ANTIGRAVITY_PROTOCOL_VERSION,
    ANTIGRAVITY_VERIFIED_VERSION,
    AntigravityLaunch,
    AntigravityResolution,
    forget_antigravity_resolution,
    resolve_antigravity_runtime,
)
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.transport_errors import AcpToolGateUnroutable
from kiro_crew.acp.types import PROVIDER_LABEL_BY_BACKEND, JsonRpcMessage
from kiro_crew.agent_sdk import backend_install, host_auth
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_ANTIGRAVITY,
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    BASELINE_SELECTABLE_BACKENDS,
    effort_config_option_id,
    model_registry_namespace,
)
from kiro_crew.agent_sdk.backends_models import (
    ACP_BACKENDS_ADVERTISED_MODEL_SELECTION,
    ACP_BACKENDS_CONFIG_MODEL_SELECT_IS_CANONICAL,
)
from kiro_crew.agent_sdk.backends_routing import (
    ACP_BACKEND_PERMISSION_CONFIG,
    ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST,
    ACP_BACKEND_ROUTING,
    Routing,
)
from kiro_crew.agent_sdk.drivers import acp as acp_driver
from kiro_crew.providers.mirrors.base import Concern
from kiro_crew.providers.mirrors.registry import mirror_for


def _cold_resolution_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start one test from a cold resolution cache without leaking its globals."""
    monkeypatch.setattr(antigravity_mod, "_cache", None)
    monkeypatch.setattr(antigravity_mod, "_cache_generation", antigravity_mod._cache_generation + 1)


def _client(version: str = ANTIGRAVITY_VERIFIED_VERSION) -> AcpClient:
    client = AcpClient(acp_backend=ACP_BACKEND_ANTIGRAVITY)
    client._agent_version = version
    client._agent_version_read = True
    return client


def _mode_option(value: str = "default") -> dict:
    return {
        "id": "mode",
        "type": "select",
        "currentValue": value,
        "options": [
            {"value": "default"},
            {"value": "auto_edit"},
            {"value": "yolo"},
        ],
    }


def test_antigravity_is_selectable_with_a_complete_host_contract() -> None:
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_ANTIGRAVITY in BASELINE_SELECTABLE_BACKENDS
    assert PROVIDER_LABEL_BY_BACKEND[ACP_BACKEND_ANTIGRAVITY] == "antigravity"
    assert ACP_BACKEND_ROUTING[ACP_BACKEND_ANTIGRAVITY] is Routing.SESSION_CONFIG
    assert ACP_BACKEND_PERMISSION_CONFIG[ACP_BACKEND_ANTIGRAVITY] == ("mode", "default")
    assert ACP_BACKEND_FORBIDDEN_MODES[ACP_BACKEND_ANTIGRAVITY] == frozenset({"auto_edit", "yolo"})
    assert ACP_BACKEND_PROMPT_COMMAND_ALLOWLIST[ACP_BACKEND_ANTIGRAVITY] == frozenset(
        {"plan", "usage"}
    )
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_CONFIG_MODEL_SELECT_IS_CANONICAL
    assert effort_config_option_id(ACP_BACKEND_ANTIGRAVITY) == "thinking_level"
    assert model_registry_namespace(ACP_BACKEND_ANTIGRAVITY) == "antigravity"
    assert ACP_BACKEND_ANTIGRAVITY not in ACP_BACKENDS_HONOR_ZERO_TOOL_BAN


def test_antigravity_mirror_and_auth_are_declared() -> None:
    mirror = mirror_for(ACP_BACKEND_ANTIGRAVITY)
    assert mirror is not None
    assert mirror.backend == ACP_BACKEND_ANTIGRAVITY
    assert set(mirror.rulings()) == set(Concern)
    declaration = next(
        declaration
        for declaration in host_auth.AGENT_AUTH_DECLARATIONS
        if declaration.backend == ACP_BACKEND_ANTIGRAVITY
    )
    assert declaration.home_override_env_vars == ("GEMINI_HOME",)
    assert ".gemini/antigravity-acp/acp_token.json" in declaration.credential_leaves
    assert declaration.adapter_own_leaves == declaration.credential_leaves
    assert ".gemini/antigravity-acp/settings.json" not in declaration.credential_leaves
    assert "oauth-personal" in declaration.sign_in_remedy


def test_two_file_antigravity_install_resolves_only_as_a_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / "agy_acp_server.par"
    helper = tmp_path / "localharness_external"
    server.write_bytes(b"server")
    helper.write_bytes(b"helper")
    server.chmod(0o700)
    helper.chmod(0o700)
    monkeypatch.setenv(ANTIGRAVITY_ACP_BIN_ENV, str(server))
    monkeypatch.delenv(ANTIGRAVITY_HARNESS_PATH_ENV, raising=False)
    _cold_resolution_cache(monkeypatch)
    forget_antigravity_resolution()

    resolution = resolve_antigravity_runtime()

    assert resolution.server == str(server)
    assert resolution.helper == str(helper)


def test_antigravity_install_probe_names_the_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_driver, "antigravity_components_resolve", lambda: (True, False))
    state = backend_install._probe_antigravity()
    assert state.installed == backend_install.MISSING
    assert state.missing_components == ("localharness_external",)
    assert "1.4.4" in (state.install_command or "")


def test_antigravity_probe_does_not_replace_the_spawn_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = AntigravityResolution(server=None, helper=None, search_path=str(tmp_path))
    installed = AntigravityResolution(
        server=str(tmp_path / "agy_acp_server.par"),
        helper=str(tmp_path / "localharness_external"),
        search_path=str(tmp_path),
    )
    monkeypatch.setattr(antigravity_mod, "_cache", cached)
    monkeypatch.setattr(antigravity_mod, "_resolve_antigravity_runtime", lambda: installed)

    assert acp_driver.antigravity_components_resolve() == (True, True)
    assert antigravity_mod.antigravity_cached_resolution() == cached
    assert backend_install._probe_antigravity().restart_required is True


def test_antigravity_recheck_fences_a_resolution_already_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()
    installed = AntigravityResolution(
        server=str(tmp_path / "agy_acp_server.par"),
        helper=str(tmp_path / "localharness_external"),
        search_path=str(tmp_path),
    )

    def slow_resolve() -> AntigravityResolution:
        started.set()
        assert release.wait(2)
        return installed

    _cold_resolution_cache(monkeypatch)
    monkeypatch.setattr(antigravity_mod, "_resolve_antigravity_runtime", slow_resolve)
    worker = threading.Thread(target=resolve_antigravity_runtime)
    worker.start()
    assert started.wait(2)
    forget_antigravity_resolution()
    release.set()
    worker.join(2)

    assert not worker.is_alive()
    assert antigravity_mod.antigravity_cached_resolution() is None


@pytest.mark.asyncio
async def test_antigravity_spawn_sets_helper_and_credential_mask(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolution = AntigravityResolution(
        server=str(tmp_path / "agy_acp_server.par"),
        helper=str(tmp_path / "localharness_external"),
        search_path=str(tmp_path),
    )
    monkeypatch.setattr(
        "kiro_crew.acp.harness.antigravity.resolve_antigravity_runtime",
        lambda **_kwargs: resolution,
    )
    preflight = AsyncMock(return_value=("/hidden/credential-home",))
    monkeypatch.setattr(launch_mod, "_run_preflight_bounded", preflight)
    session = SimpleNamespace(_prepare_session_mcp=AsyncMock())
    ctx = SpawnContext(
        agent="kirocrew",
        work_dir=tmp_path,
        model=None,
        environ={},
        home=tmp_path,
        sandbox_mode="standard",
        session=session,
    )

    adapter = AntigravityLaunch()
    plan = await adapter.resolve_spawn(ctx)
    env: dict[str, str] = {}
    adapter.apply_spawn_env(env)

    session._prepare_session_mcp.assert_awaited_once()
    preflight.assert_awaited_once_with(
        launch_mod._sandbox_preflight, ACP_BACKEND_ANTIGRAVITY, "standard"
    )
    assert plan.argv == [resolution.server]
    assert plan.spawn_label == "agy_acp_server"
    assert plan.extra_hidden_dirs == ("/hidden/credential-home",)
    assert env[ANTIGRAVITY_HARNESS_PATH_ENV] == resolution.helper
    assert process_adapter_for(ACP_BACKEND_ANTIGRAVITY).backend == ACP_BACKEND_ANTIGRAVITY


@pytest.mark.asyncio
async def test_antigravity_requires_mode_default_before_the_first_prompt() -> None:
    client = _client()
    client._acp_config_options = [_mode_option()]
    client.set_config_option = AsyncMock(return_value={})

    await client._apply_session_permission_routing()

    client.set_config_option.assert_awaited_once_with("mode", "default")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto_edit", "yolo"])
async def test_antigravity_stops_on_a_forbidden_mode(mode: str) -> None:
    client = _client()
    client._session_id = "antigravity-session-1"
    client._session_key = "dashboard:test"
    client._kill_process = AsyncMock()
    msg = JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": client._session_id,
            "update": {"sessionUpdate": "current_mode_update", "currentModeId": mode},
        },
    )

    with pytest.raises(AcpToolGateUnroutable):
        await client._tripwire_session_config(msg)
    client._kill_process.assert_awaited_once_with(force=True)


def test_antigravity_refuses_an_unverified_runtime_version() -> None:
    _refuse_unverified_antigravity_version(_client())
    with pytest.raises(AcpToolGateUnroutable, match="not the version"):
        _refuse_unverified_antigravity_version(_client("1.4.5"))


def test_antigravity_protocol_and_model_options_are_exact() -> None:
    from kiro_crew.acp.client import PROTOCOL_VERSION_ANTIGRAVITY

    assert ANTIGRAVITY_PROTOCOL_VERSION == 1
    assert PROTOCOL_VERSION_ANTIGRAVITY == 1
    assert ANTIGRAVITY_VERIFIED_VERSION == "1.4.4"
    assert effort_config_option_id(ACP_BACKEND_ANTIGRAVITY) == "thinking_level"


def test_antigravity_uses_the_bare_model_select_as_its_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kiro_crew.acp.client_model_selection import model_registry
    from kiro_crew.acp.session_handle import session_models_envelope

    fixture = Path(__file__).parent / "fixtures" / "acp_frames" / "antigravity"
    response = json.loads(
        (fixture / "handshake-live.jsonl").read_text(encoding="utf-8").splitlines()[2]
    )["result"]
    assert response["models"]["availableModels"][0]["modelId"] == "gemini-3.8-flash-high"

    envelope = session_models_envelope(response, ACP_BACKEND_ANTIGRAVITY)
    assert [row["modelId"] for row in envelope["availableModels"]] == ["gemini-3.8-flash"]
    assert envelope["currentModelId"] == "gemini-3.8-flash"

    monkeypatch.setattr(model_registry, "refresh_advertised_models", lambda *args, **kwargs: False)
    client = _client()
    client._capture_available_models(response)
    assert [row["modelId"] for row in client._available_models] == ["gemini-3.8-flash"]
    assert client._resolved_model_id == "gemini-3.8-flash"


def test_antigravity_native_tool_kinds_and_mcp_identity_are_mapped() -> None:
    from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event
    from kiro_crew.acp._dispatch_parts.tool_call_identity import harness_tool_name
    from kiro_crew.acp.harness_tool_names import (
        harness_tool_match_names,
        qualified_harness_tool_id,
    )

    shell = qualified_harness_tool_id(ACP_BACKEND_ANTIGRAVITY, "", tool_kind="execute")
    mcp = qualified_harness_tool_id(
        ACP_BACKEND_ANTIGRAVITY,
        "marker",
        {"probe"},
        tool_kind="other",
        mcp_server="probe",
    )

    assert harness_tool_match_names(shell) is not None
    assert "execute_bash" in harness_tool_match_names(shell)
    file_search = harness_tool_match_names("antigravity#find_file")
    assert file_search is not None
    assert {"fs_read", "glob"} <= set(file_search)
    assert mcp == "antigravity#@probe/marker"
    assert harness_tool_name({"title": "Run grep_search?"}) == ""
    assert (
        harness_tool_name({"title": "Run grep_search?"}, backend=ACP_BACKEND_ANTIGRAVITY)
        == "grep_search"
    )

    from kiro_crew.acp._dispatch_parts.session_update_parser import parse_session_update

    names: dict[str, str] = {}
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-search-parsed",
            "title": "Run grep_search?",
            "kind": "search",
        },
        harness_tool_name_cache=names,
        backend=ACP_BACKEND_ANTIGRAVITY,
    )
    assert names["call-search-parsed"] == "grep_search"

    names.clear()
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call-search-parsed",
            "title": "Run grep_search?",
            "kind": "search",
        },
        harness_tool_name_cache=names,
        backend="opencode",
    )
    assert names["call-search-parsed"] == ""
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "sessionId": "antigravity-session-1",
                "toolCall": {
                    "toolCallId": "call-search",
                    "title": "Run grep_search?",
                    "kind": "search",
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        ),
        harness_tool_name_cache={"call-search": "grep_search"},
        harness_backend=ACP_BACKEND_ANTIGRAVITY,
    )
    assert event is not None
    assert event.harness_tool_id == "antigravity#grep_search"
