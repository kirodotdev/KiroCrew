"""Contract tests for Google Antigravity's standalone ACP harness."""

from __future__ import annotations

import json
import os
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
from kiro_crew.agent_sdk import tool_gate as tool_gate_mod
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_ANTIGRAVITY,
    ACP_BACKEND_FORBIDDEN_MODES,
    ACP_BACKENDS_EFFORT_AT_LAUNCH,
    ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION,
    ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
    ACP_BACKENDS_HONOR_ZERO_TOOL_BAN,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_MCP_RELAUNCH_RELOAD,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
    ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    ACP_BACKENDS_RELOAD_DISPOSED_SESSION,
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


def test_antigravity_tunes_and_mounts_on_the_live_session_not_by_relaunch() -> None:
    """Model, effort and MCP reach a live 1.4.4 session; nothing is a launch argument."""
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert ACP_BACKEND_ANTIGRAVITY in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKEND_ANTIGRAVITY not in ACP_BACKENDS_MODEL_AT_LAUNCH
    assert ACP_BACKEND_ANTIGRAVITY not in ACP_BACKENDS_EFFORT_AT_LAUNCH
    assert ACP_BACKEND_ANTIGRAVITY not in ACP_BACKENDS_MCP_RELAUNCH_RELOAD
    # No capture of a disposal error or of a second-process load exists for 1.4.4.
    assert ACP_BACKEND_ANTIGRAVITY not in ACP_BACKENDS_RELOAD_DISPOSED_SESSION


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
    # The backend card renders this sentence in every install state, so it names the pin.
    assert (
        f"verifies Google Antigravity ACP {ANTIGRAVITY_VERIFIED_VERSION} only"
        in declaration.sign_in_remedy
    )


def test_two_file_antigravity_install_resolves_only_as_a_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = tmp_path / antigravity_mod.ANTIGRAVITY_ACP_BIN
    helper = tmp_path / antigravity_mod.ANTIGRAVITY_HARNESS_BIN
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


def test_antigravity_relative_override_resolves_against_the_gateway_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child starts in the chat's workspace, so a relative path must not reach the spawn."""
    server = tmp_path / "agy_acp_server.par"
    helper = tmp_path / "localharness_external"
    for member in (server, helper):
        member.write_bytes(b"x")
    # The POSIX branch, whatever host runs the test.
    monkeypatch.setattr(antigravity_mod.platform_compat, "IS_WINDOWS", False)
    monkeypatch.setattr(antigravity_mod.platform_compat, "is_executable_file", os.path.isfile)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ANTIGRAVITY_ACP_BIN_ENV, "." + os.sep + server.name)
    monkeypatch.setenv(ANTIGRAVITY_HARNESS_PATH_ENV, "." + os.sep + helper.name)
    _cold_resolution_cache(monkeypatch)
    forget_antigravity_resolution()

    resolution = resolve_antigravity_runtime()

    for found, expected in ((resolution.server, server), (resolution.helper, helper)):
        assert found is not None and os.path.isabs(found)
        assert os.path.samefile(found, expected)


def test_antigravity_install_probe_names_the_missing_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(acp_driver, "antigravity_components_resolve", lambda: (True, False))
    state = backend_install._probe_antigravity()
    assert state.installed == backend_install.MISSING
    assert state.missing_components == ("localharness_external",)
    assert "1.4.4" in (state.install_command or "")
    assert "verifies Google Antigravity ACP 1.4.4 only" in (state.install_command or "")
    assert "refuses other versions" in (state.install_command or "")


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


def test_antigravity_credential_mask_spares_only_its_own_token_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = os.path.expanduser("~")
    own = {
        os.path.join(home, *leaf.split("/"))
        for leaf in tool_gate_mod.ADAPTER_OWN_CREDENTIAL_LEAVES[ACP_BACKEND_ANTIGRAVITY]
    }
    masked = set(tool_gate_mod.adapter_hidden_credential_dirs(ACP_BACKEND_ANTIGRAVITY))
    assert own and own.isdisjoint(masked)
    for floor_leaf in (".aws", ".ssh"):
        assert os.path.join(home, floor_leaf) in masked
    # Declared leaves are the only reason these two files are spared: without the
    # declaration the same mask hides them.
    with monkeypatch.context() as ctx:
        ctx.setitem(tool_gate_mod.ADAPTER_OWN_CREDENTIAL_LEAVES, ACP_BACKEND_ANTIGRAVITY, ())
        undeclared = set(tool_gate_mod.adapter_hidden_credential_dirs(ACP_BACKEND_ANTIGRAVITY))
    assert own <= undeclared


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
    with pytest.raises(AcpToolGateUnroutable, match="not the version") as refused:
        _refuse_unverified_antigravity_version(_client("1.4.5"))
    message = str(refused.value)
    assert ANTIGRAVITY_VERIFIED_VERSION in message
    assert "https://dl.google.com/agy-extensions/releases/" in message
    assert "agentclientprotocol.com/registry" not in message
    assert "Kiro Crew verifies Google Antigravity ACP 1.4.4 only" in message
    assert "refuses other versions" in message


@pytest.mark.asyncio
async def test_antigravity_pin_is_enforced_where_the_mode_is_armed() -> None:
    client = _client("1.4.5")
    client._acp_config_options = [_mode_option()]
    client.set_config_option = AsyncMock(return_value={})

    with pytest.raises(AcpToolGateUnroutable, match="not the version"):
        await client._apply_session_permission_routing()

    client.set_config_option.assert_not_awaited()


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


@pytest.mark.parametrize("command", ["ls", "./deploy.sh", "/usr/bin/id", "printf ok"])
def test_antigravity_shell_call_is_a_shell_tool_whatever_its_command_text(command: str) -> None:
    """An execute frame's title is the command line; it must never name the tool."""
    from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event
    from kiro_crew.acp._dispatch_parts.tool_call_identity import harness_tool_name
    from kiro_crew.acp.harness_tool_names import harness_tool_match_names

    update = {"title": command, "kind": "execute"}
    cache = {"call-shell": harness_tool_name(update, backend=ACP_BACKEND_ANTIGRAVITY)}
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "sessionId": "antigravity-session-1",
                "toolCall": {"toolCallId": "call-shell", **update},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        ),
        harness_tool_name_cache=cache,
        harness_backend=ACP_BACKEND_ANTIGRAVITY,
    )

    assert event is not None
    assert event.harness_tool_id == "antigravity#run_command"
    assert "execute_bash" in (harness_tool_match_names(event.harness_tool_id) or ())


def _antigravity_permission_event(
    raw_input: dict,
    *,
    kind: str,
    shell: bool,
    mcp_server: str = "",
    with_tool_input: bool = False,
    inline: bool = False,
    backend: str = ACP_BACKEND_ANTIGRAVITY,
):
    """*with_tool_input* also fills the rendered-input caches as the tool_call parser
    does; *inline* puts the arguments on the permission frame and caches no params."""
    from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event

    tool_call = {"toolCallId": "call-1", "title": "x y", "kind": kind}
    if inline:
        tool_call["input"] = raw_input
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "sessionId": "antigravity-session-1",
                "toolCall": tool_call,
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                ],
            },
        ),
        tool_input_cache={"call-1": json.dumps(raw_input, indent=2)} if with_tool_input else None,
        tool_input_redacted_cache={"call-1": False} if with_tool_input else None,
        shell_cache={"call-1": shell},
        raw_params_cache=None if inline else {"call-1": raw_input},
        mcp_server_name_cache={"call-1": mcp_server} if mcp_server else None,
        harness_backend=backend,
    )
    assert event is not None
    return event


def test_antigravity_shell_command_reaches_the_gate_under_its_own_spelling() -> None:
    """The unverifiable-shell tier refuses a shell call that has no ``command``."""
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "printf ok", "command_line": "printf ok"}
    event = _antigravity_permission_event(raw, kind="execute", shell=True)

    assert ToolCall.from_event(event).command == "printf ok"
    assert raw == {"CommandLine": "printf ok", "command_line": "printf ok"}


def test_antigravity_shell_spellings_that_disagree_stay_unverifiable() -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "printf ok", "command_line": "rm -rf /tmp/x"}
    event = _antigravity_permission_event(raw, kind="execute", shell=True)

    assert ToolCall.from_event(event).command is None


_ANTIGRAVITY_INPUT_PATHS = [
    pytest.param({"with_tool_input": True}, id="tool-input-cache"),
    pytest.param({"inline": True}, id="inline-input"),
]


@pytest.mark.parametrize("source", _ANTIGRAVITY_INPUT_PATHS)
@pytest.mark.parametrize(
    "raw",
    [
        {"command": "ls", "CommandLine": "touch review-output"},
        {"CommandLine": "printf ok", "command_line": "rm -rf /tmp/x"},
    ],
)
def test_antigravity_conflicting_shell_spellings_stay_unverifiable_with_rendered_input(
    raw: dict, source: dict
) -> None:
    """The rendered input is read when the params name no command, so it must not name one."""
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    original = dict(raw)
    event = _antigravity_permission_event(raw, kind="execute", shell=True, **source)

    assert event.shell_command is None
    assert ToolCall.from_event(event).command is None
    assert raw == original


@pytest.mark.parametrize("source", _ANTIGRAVITY_INPUT_PATHS)
def test_antigravity_agreeing_shell_spellings_reach_the_gate_with_rendered_input(
    source: dict,
) -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "printf ok", "command_line": "printf ok"}
    event = _antigravity_permission_event(raw, kind="execute", shell=True, **source)

    assert event.shell_command == "printf ok"
    assert ToolCall.from_event(event).command == "printf ok"


def test_antigravity_file_targets_reach_the_sensitive_path_check() -> None:
    from kiro_crew.platform.tool_paths import target_paths

    token = "/home/u/.gemini/antigravity-acp/acp_token.json"
    for raw in (
        {"AbsolutePath": token},
        {"absolute_path": token},
        {"TargetFile": token, "CodeContent": "x"},
        {"target_file": token},
        {"SearchPath": token, "Query": "a"},
        {"DirectoryPath": token},
    ):
        event = _antigravity_permission_event(raw, kind="read", shell=False)
        assert token in target_paths(event.raw_tool_params), raw


def test_antigravity_gate_params_keep_what_the_call_already_states() -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_identity import antigravity_gate_params

    assert antigravity_gate_params(None, shell=True) is None
    # An existing ``command`` is a third spelling: when it disagrees the call is unverifiable.
    assert "command" not in antigravity_gate_params(
        {"command": "ls", "CommandLine": "echo other", "command_line": "echo other"}, shell=True
    )
    assert (
        antigravity_gate_params({"command": "ls", "CommandLine": "ls"}, shell=True)["command"]
        == "ls"
    )
    assert "command" not in antigravity_gate_params({"CommandLine": ["ls"]}, shell=True)
    # A call that is not a shell call never gains a ``command``.
    assert "command" not in antigravity_gate_params(
        {"command_line": "x", "path": "/p"}, shell=False
    )
    # A lone path stays a string, so a rule reading a string ``path`` still sees it.
    assert antigravity_gate_params({"AbsolutePath": "/b"}, shell=False)["path"] == "/b"
    assert antigravity_gate_params({"path": "/a", "search_path": "/a"}, shell=False)["path"] == "/a"
    assert antigravity_gate_params({"path": "/a", "AbsolutePath": "/b"}, shell=False)["path"] == [
        "/a",
        "/b",
    ]
    assert antigravity_gate_params({"path": ["/a"], "target_file": "/b"}, shell=False)["path"] == [
        "/a",
        "/b",
    ]


def test_antigravity_mcp_call_arguments_are_left_as_the_tool_sent_them() -> None:
    raw = {"path": "/srv/x", "command_line": "x", "search_path": "/tmp", "SourcePath": "/s"}
    event = _antigravity_permission_event(raw, kind="other", shell=False, mcp_server="probe")

    assert event.raw_tool_params == raw


def test_antigravity_non_shell_call_keeps_its_path_and_gains_no_command() -> None:
    raw = {"path": "/srv/secret", "command_line": "x", "pattern": "k"}
    event = _antigravity_permission_event(raw, kind="search", shell=False)

    assert event.raw_tool_params == raw


# --- Argument names the two key lists do not cover fail closed ------------------------

#: Path-like names the runtime defines that ``_ANTIGRAVITY_PATH_KEYS`` does not list.
_AG_UNLISTED_PATH_NAMES = (
    "file_path",
    "filePath",
    "RelativePath",
    "relative_path",
    "SourcePath",
    "ArtifactDirectoryPath",
    "KeywordPath",
    "WorkspacePath",
    "WorkspacePaths",
    "working_dir",
    "absolute_file_path",
)
_AG_CALL_SHAPES = [
    pytest.param("read", False, id="file-tool"),
    pytest.param("execute", True, id="shell-tool"),
]


def _ag_own_token() -> str:
    return os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-acp", "acp_token.json")


def _ag_path_refusal(raw: dict, *, kind: str = "read", shell: bool = False, **kw) -> str | None:
    """The sensitive-path verdict on the call, read as ``_tier_param_paths`` reads it."""
    from kiro_crew.platform.tool_paths import target_paths
    from kiro_crew.security.paths import sensitive_path_refusal

    event = _antigravity_permission_event(raw, kind=kind, shell=shell, **kw)
    found = target_paths(event.raw_tool_params)
    if found.truncated:
        return "truncated"
    return next((r for r in map(sensitive_path_refusal, found) if r), None)


@pytest.mark.parametrize(("kind", "shell"), _AG_CALL_SHAPES)
@pytest.mark.parametrize("name", _AG_UNLISTED_PATH_NAMES)
def test_antigravity_unlisted_path_name_reaches_the_sensitive_path_check(
    name: str, kind: str, shell: bool
) -> None:
    raw = {name: _ag_own_token(), "CommandLine": "ls"}
    original = dict(raw)

    assert _ag_path_refusal(raw, kind=kind, shell=shell) is not None
    assert raw == original


def test_antigravity_listed_path_names_are_still_refused() -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_identity import _ANTIGRAVITY_PATH_KEYS

    for name in _ANTIGRAVITY_PATH_KEYS:
        assert _ag_path_refusal({name: _ag_own_token()}) is not None, name


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param({"WorkspacePaths": ["/srv/ok", "TOKEN"]}, id="list"),
        pytest.param({"Paths": [["TOKEN"]]}, id="nested-list"),
        pytest.param({"Options": {"SourcePath": "TOKEN"}}, id="dict-under-any-name"),
        pytest.param({"SourceFolder": {"Inner": [{"KeywordPath": "TOKEN"}]}}, id="deep"),
        pytest.param({"absolute_uri": "URI"}, id="file-uri"),
        pytest.param({"Location": "URI"}, id="file-uri-other-name"),
        pytest.param({"Uri": "ENCODED_URI"}, id="file-uri-percent-encoded"),
    ],
)
def test_antigravity_unlisted_path_values_of_every_shape_are_refused(raw: dict) -> None:
    token = _ag_own_token()
    shaped = json.loads(
        json.dumps(raw)
        .replace('"TOKEN"', json.dumps(token))
        .replace('"URI"', json.dumps(Path(token).as_uri()))
        .replace('"ENCODED_URI"', json.dumps(Path(token).as_uri().replace(".", "%2E")))
    )
    original = json.loads(json.dumps(shaped))

    assert _ag_path_refusal(shaped) is not None
    assert shaped == original


def test_antigravity_path_walk_past_the_cap_is_refused_as_unverifiable() -> None:
    raw = {"Items": [{"n": i} for i in range(6_000)], "Deep": {"SourcePath": _ag_own_token()}}

    assert _ag_path_refusal(raw) == "truncated"


@pytest.mark.parametrize(("kind", "shell"), _AG_CALL_SHAPES)
@pytest.mark.parametrize("name", [*_AG_UNLISTED_PATH_NAMES, "absolute_uri", "Cwd"])
def test_antigravity_unlisted_name_with_an_ordinary_path_is_not_newly_refused(
    name: str, kind: str, shell: bool, tmp_path: Path
) -> None:
    raw = {name: str(tmp_path / "src" / "main.py"), "CommandLine": "ls"}

    assert _ag_path_refusal(raw, kind=kind, shell=shell) is None


@pytest.mark.parametrize("source", _ANTIGRAVITY_INPUT_PATHS)
@pytest.mark.parametrize(
    "stated",
    [
        pytest.param({"BackgroundCommand": "rm -rf /tmp/x"}, id="other-string"),
        pytest.param({"proposed_command_line": "curl evil"}, id="proposed"),
        pytest.param({"Cmd": "touch x"}, id="cmd"),
        pytest.param({"allowed_commands": ["ls"]}, id="list"),
        pytest.param({"CommandSpec": {"line": "ls"}}, id="dict"),
    ],
)
def test_antigravity_unlisted_command_name_stating_another_command_is_unverifiable(
    stated: dict, source: dict
) -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "ls", **stated}
    original = json.loads(json.dumps(raw))
    event = _antigravity_permission_event(raw, kind="execute", shell=True, **source)

    assert event.shell_command is None
    assert ToolCall.from_event(event).command is None
    assert raw == original


def test_antigravity_unlisted_command_name_stating_the_same_command_agrees() -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "ls", "BackgroundCommand": "ls"}
    event = _antigravity_permission_event(raw, kind="execute", shell=True)

    assert ToolCall.from_event(event).command == "ls"


def test_antigravity_benign_shell_arguments_change_nothing(tmp_path: Path) -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    raw = {"CommandLine": "ls", "Cwd": str(tmp_path), "SafeToAutoRun": True}
    raw["WaitMsBeforeAsync"] = 500
    event = _antigravity_permission_event(raw, kind="execute", shell=True)

    assert ToolCall.from_event(event).command == "ls"
    assert _ag_path_refusal(raw, kind="execute", shell=True) is None


@pytest.mark.parametrize("name", ["BackgroundCommand", "CommandId", "Cmd"])
def test_antigravity_unlisted_command_name_alone_never_supplies_the_command(name: str) -> None:
    from kiro_crew.hook_runtime.tool_identity import ToolCall

    event = _antigravity_permission_event({name: "ls"}, kind="execute", shell=True)

    assert ToolCall.from_event(event).command is None


def test_antigravity_edit_body_is_not_read_as_a_path(tmp_path: Path) -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_identity import antigravity_gate_params

    target = str(tmp_path / "main.py")
    raw = {
        "TargetFile": target,
        "Instruction": "rename x",
        "ReplacementChunks": [{"TargetContent": "x = 1", "ReplacementContent": "y = 1"}],
    }

    assert antigravity_gate_params(raw, shell=False)["path"] == target


@pytest.mark.parametrize(
    "uri", ["file://[::1/x", "file:///%41:/x", "file:", "FILE://host/share/x", "file:///\x00"]
)
def test_antigravity_malformed_file_uri_does_not_break_the_gate(uri: str) -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_identity import antigravity_gate_params

    out = antigravity_gate_params({"Uri": uri}, shell=False)

    assert uri in out["path"]


@pytest.mark.parametrize(("kind", "shell"), _AG_CALL_SHAPES)
def test_antigravity_named_path_past_the_walk_budget_is_refused(kind: str, shell: bool) -> None:
    from kiro_crew.platform.tool_paths import _TARGET_PATH_MAX_NODES

    raw = {
        "CommandLine": "ls",
        "Blob": ["x"] * _TARGET_PATH_MAX_NODES,
        "SourcePath": _ag_own_token(),
    }

    assert _ag_path_refusal(raw, kind=kind, shell=shell) is not None


def test_antigravity_unlisted_command_name_gives_a_file_tool_no_command() -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_identity import antigravity_gate_params

    assert "command" not in antigravity_gate_params({"BackgroundCommand": "x"}, shell=False)


@pytest.mark.parametrize("backend", ["", "copilot"])
def test_other_backends_keep_unlisted_arguments_as_sent(backend: str) -> None:
    raw = {"SourcePath": _ag_own_token(), "CommandLine": "ls", "BackgroundCommand": "rm x"}
    event = _antigravity_permission_event(raw, kind="execute", shell=True, backend=backend)

    assert event.raw_tool_params == raw


# --- The unasked-call tripwire (``ACP_BACKEND_MUST_ASK_TOOL_KINDS``) -------------------


def _frame(update: dict) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={"sessionId": "antigravity-session-1", "update": update},
    )


def _call(call_id: str, kind: str, **extra: object) -> JsonRpcMessage:
    return _frame(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": call_id,
            "title": call_id,
            "kind": kind,
            "status": "pending",
            **extra,
        }
    )


def _end(call_id: str, status: str = "completed") -> JsonRpcMessage:
    return _frame({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": status})


def _ask(call_id: str, kind: str, request_id: int = 7) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "antigravity-session-1",
            "toolCall": {"toolCallId": call_id, "kind": kind, "status": "pending"},
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
            ],
        },
    )


_MCP_META = {"_meta": {"mcp": {"server": "probe", "tool": "marker"}, "is_mcp_tool_call": True}}


@pytest.fixture
def unasked(monkeypatch: pytest.MonkeyPatch):
    """An Antigravity client whose kill and SEL writes are recorded, not performed."""
    from kiro_crew import sel as sel_module

    audited: list[dict] = []
    recorder = SimpleNamespace(log_tool_invocation=lambda **kw: audited.append(kw))
    monkeypatch.setattr(sel_module, "sel", lambda: recorder)

    def build(backend: str = ACP_BACKEND_ANTIGRAVITY) -> AcpClient:
        client = _client() if backend == ACP_BACKEND_ANTIGRAVITY else AcpClient(acp_backend=backend)
        client._session_id = "antigravity-session-1"
        client._session_key = "dashboard:test"
        client._kill_process = AsyncMock()
        return client

    return build, audited


async def _feed(client: AcpClient, *frames: JsonRpcMessage) -> None:
    for msg in frames:
        if msg.method == "session/request_permission":
            client._build_permission_event(msg)
        else:
            await client._tripwire_permission_state(msg)


def test_the_must_ask_table_names_antigravity_alone() -> None:
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_MUST_ASK_TOOL_KINDS

    assert ACP_BACKEND_MUST_ASK_TOOL_KINDS == {
        ACP_BACKEND_ANTIGRAVITY: frozenset({"execute", "edit", "delete", "move", "fetch", "mcp"})
    }


@pytest.mark.asyncio
async def test_a_shell_call_that_completes_unasked_ends_the_session(unasked) -> None:
    build, audited = unasked
    client = build()
    with pytest.raises(AcpToolGateUnroutable, match="without a permission request"):
        await _feed(client, _call("call-1", "execute"), _end("call-1"))
    client._kill_process.assert_awaited_once_with(force=True)
    assert len(audited) == 1
    row = audited[0]
    assert row["outcome"] == "denied"
    assert row["tool_kind"] == "execute"
    assert row["tool_name"] == "call-1"
    assert row["metadata"] == {
        "reason": "unasked_call_tripwire",
        "backend": ACP_BACKEND_ANTIGRAVITY,
        "tool_call_id": "call-1",
    }


@pytest.mark.asyncio
async def test_a_non_string_terminal_id_does_not_consume_a_held_call(unasked) -> None:
    build, audited = unasked
    client = build()
    malformed = _frame(
        {"sessionUpdate": "tool_call_update", "toolCallId": 123, "status": "completed"}
    )
    await _feed(client, _call("call-1", "execute"), malformed)
    client._kill_process.assert_not_awaited()
    assert audited == []
    assert list(client._unasked_watch) == ["call-1"]


@pytest.mark.asyncio
async def test_a_shell_call_that_asked_first_is_left_alone(unasked) -> None:
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", "execute"), _ask("call-1", "execute"), _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_an_ask_after_in_progress_but_before_completion_is_enough(unasked) -> None:
    build, _ = unasked
    client = build()
    in_progress = _frame(
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "in_progress"}
    )
    await _feed(
        client, _call("call-1", "edit"), in_progress, _ask("call-1", "edit"), _end("call-1")
    )
    client._kill_process.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "search", "think", "other", ""])
async def test_a_read_only_or_unknown_kind_may_complete_unasked(unasked, kind: str) -> None:
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", kind), _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_an_mcp_call_that_completes_unasked_ends_the_session(unasked) -> None:
    build, _ = unasked
    client = build()
    with pytest.raises(AcpToolGateUnroutable):
        await _feed(client, _call("call-2", "other", **_MCP_META), _end("call-2"))
    client._kill_process.assert_awaited_once_with(force=True)


@pytest.mark.asyncio
async def test_a_failed_call_without_a_request_did_not_run_and_is_left_alone(unasked) -> None:
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", "execute"), _end("call-1", "failed"))
    client._kill_process.assert_not_awaited()
    assert audited == []
    # The failed call's record is consumed: a later completion of the id is a new call.
    with pytest.raises(AcpToolGateUnroutable):
        await _feed(client, _call("call-1", "execute"), _end("call-1"))


@pytest.mark.asyncio
async def test_a_reused_id_must_ask_again(unasked) -> None:
    build, _ = unasked
    client = build()
    await _feed(client, _call("call-0", "execute"), _ask("call-0", "execute"), _end("call-0"))
    with pytest.raises(AcpToolGateUnroutable):
        await _feed(client, _call("call-0", "execute"), _end("call-0"))


def _dead_process() -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdin=None, stdout=None, stderr=None)


@pytest.mark.asyncio
async def test_an_ask_on_a_dead_process_does_not_vouch_for_its_replacements_call(
    unasked,
) -> None:
    """A ``toolCallId`` is unique only within one backend session, and a respawn opens
    another, so the replacement process may reuse an id the dead one asked about."""
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", "edit"), _ask("call-1", "edit"))
    client._process = _dead_process()
    client._reset_state()
    client._session_id = "antigravity-session-1"
    with pytest.raises(AcpToolGateUnroutable, match="without a permission request"):
        await _feed(client, _call("call-1", "edit", status="completed"))
    client._kill_process.assert_awaited_once_with(force=True)
    assert [row["metadata"]["reason"] for row in audited] == ["unasked_call_tripwire"]


@pytest.mark.asyncio
async def test_an_ask_without_a_process_reset_vouches_for_its_late_completion(
    unasked,
) -> None:
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", "edit"), _ask("call-1", "edit"))
    await _feed(client, _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_a_reset_of_a_client_holding_no_watch_still_trips_the_next_call(unasked) -> None:
    build, _ = unasked
    client = build()
    del client._unasked_watch
    client._process = _dead_process()
    client._reset_state()
    assert not getattr(client, "_unasked_watch", {})
    client._session_id = "antigravity-session-1"
    with pytest.raises(AcpToolGateUnroutable):
        await _feed(client, _call("call-1", "execute"), _end("call-1"))


@pytest.mark.asyncio
async def test_a_process_kept_after_failed_tree_cleanup_keeps_its_asks(unasked) -> None:
    """The early return keeps the process, which is not confirmed dead, and with it
    the session its asks were made in."""
    build, audited = unasked
    client = build()
    await _feed(client, _call("call-1", "edit"), _ask("call-1", "edit"))
    original = SimpleNamespace(returncode=None, stdin=None, stdout=None, stderr=None)
    client._process = original
    client._windows_tree_cleanup_failed = True
    client._reset_state()
    assert client._process is original
    assert client._unasked_watch == {"call-1": {"kind": "edit", "asked": True}}
    await _feed(client, _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_a_denied_call_that_reports_completed_is_not_read_as_unasked(unasked) -> None:
    """How 1.4.4 reports a rejected call is not captured, so a denial is still an ask."""
    build, _ = unasked
    client = build()
    client._send_response = AsyncMock()
    await _feed(client, _call("call-1", "execute"), _ask("call-1", "execute"))
    await client.reject_tool("7")
    client._send_response.assert_awaited_once()
    await _feed(client, _end("call-1"))
    client._kill_process.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["", "copilot"])
async def test_other_backends_are_untouched_by_the_same_frames(unasked, backend: str) -> None:
    build, audited = unasked
    client = build(backend)
    await _feed(client, _call("call-1", "execute"), _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []
    assert not getattr(client, "_unasked_watch", {})


@pytest.mark.asyncio
async def test_a_must_ask_kind_is_not_downgraded_by_a_later_frame(unasked) -> None:
    build, audited = unasked
    client = build()
    relabel = _frame({"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "kind": "read"})
    with pytest.raises(AcpToolGateUnroutable, match="without a permission request"):
        await _feed(client, _call("call-1", "execute"), relabel, _end("call-1"))
    assert audited[0]["tool_kind"] == "execute"


@pytest.mark.asyncio
async def test_calls_that_can_never_trip_are_not_held(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, _ = unasked
    client = build()
    await _feed(client, _call("shell", "execute"))
    for n in range(UNASKED_WATCH_LIMIT + 50):
        await _feed(client, _call(f"call-{n}", "read"))
    assert list(client._unasked_watch) == ["shell"]
    with pytest.raises(AcpToolGateUnroutable, match="without a permission request"):
        await _feed(client, _end("shell"))


def _overflow_row(audited: list[dict]) -> dict:
    assert len(audited) == 1
    assert audited[0]["metadata"]["reason"] == "unasked_watch_overflow"
    return audited[0]


@pytest.mark.asyncio
async def test_one_new_call_past_the_limit_ends_the_session(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, audited = unasked
    client = build()
    for n in range(UNASKED_WATCH_LIMIT):
        await _feed(client, _call(f"call-{n}", "execute"))
    client._kill_process.assert_not_awaited()
    with pytest.raises(AcpToolGateUnroutable, match="more tool calls open"):
        await _feed(client, _call("one-too-many", "execute"))
    client._kill_process.assert_awaited_once_with(force=True)
    row = _overflow_row(audited)
    assert row["tool_name"] == "one-too-many"
    assert row["metadata"]["tool_call_id"] == "one-too-many"
    assert not client._unasked_watch


@pytest.mark.asyncio
async def test_an_update_to_a_held_call_at_the_limit_is_allowed(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, audited = unasked
    client = build()
    for n in range(UNASKED_WATCH_LIMIT):
        await _feed(client, _call(f"call-{n}", "execute"))
    in_progress = _frame(
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-0", "status": "in_progress"}
    )
    await _feed(client, in_progress, _ask("call-0", "execute"), _end("call-0"))
    client._kill_process.assert_not_awaited()
    assert audited == []
    assert len(client._unasked_watch) == UNASKED_WATCH_LIMIT - 1


@pytest.mark.asyncio
async def test_an_asked_call_followed_by_a_full_watch_still_counts_as_asked(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, audited = unasked
    client = build()
    await _feed(client, _call("asked", "execute"), _ask("asked", "execute"))
    for n in range(UNASKED_WATCH_LIMIT - 1):
        await _feed(client, _call(f"call-{n}", "execute"))
    await _feed(client, _end("asked", "completed"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_an_asked_call_is_never_pushed_out_to_be_judged_unasked(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, audited = unasked
    client = build()
    await _feed(client, _call("asked", "execute"), _ask("asked", "execute"))
    with pytest.raises(AcpToolGateUnroutable, match="more tool calls open"):
        for n in range(UNASKED_WATCH_LIMIT):
            await _feed(client, _call(f"call-{n}", "execute"))
    _overflow_row(audited)


@pytest.mark.asyncio
async def test_an_ask_past_the_limit_stops_the_session_at_the_next_frame(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    build, audited = unasked
    client = build()
    for n in range(UNASKED_WATCH_LIMIT):
        await _feed(client, _call(f"call-{n}", "execute"))
    for n in range(5):
        await _feed(client, _ask(f"late-{n}", "execute", request_id=100 + n))
    assert len(client._unasked_watch) == UNASKED_WATCH_LIMIT + 1
    client._kill_process.assert_not_awaited()
    with pytest.raises(AcpToolGateUnroutable, match="more tool calls open"):
        await _feed(client, _end("late-0"))
    client._kill_process.assert_awaited_once_with(force=True)
    _overflow_row(audited)


def _over_long_id() -> str:
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS

    return "L" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)


def _relabel(call_id: str, kind: str) -> JsonRpcMessage:
    return _frame({"sessionUpdate": "tool_call_update", "toolCallId": call_id, "kind": kind})


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["stated", "asked_first", "mcp", "relabelled"])
async def test_a_must_ask_call_with_an_over_long_id_stops_the_session_unrecorded(
    unasked, caplog: pytest.LogCaptureFixture, shape: str
) -> None:
    """The watch never holds a ``toolCallId`` past the shared key bound, nor a cut of
    one, so a must-ask call stated with such an id is refused where it is seen: its
    ``completed`` report could never be judged, and an ask the watch refused to hold
    cannot vouch for it. Neither the log, the error nor the audit row carries the id."""
    long_id = _over_long_id()
    frames = {
        "stated": [_call(long_id, "execute")],
        "asked_first": [_ask(long_id, "edit"), _call(long_id, "edit")],
        "mcp": [_call(long_id, "other", **_MCP_META)],
        "relabelled": [_call(long_id, "read"), _relabel(long_id, "delete")],
    }[shape]
    build, audited = unasked
    client = build()
    with pytest.raises(AcpToolGateUnroutable, match="longer than Kiro Crew tracks") as raised:
        await _feed(client, *frames)
    client._kill_process.assert_awaited_once_with(force=True)
    assert audited == [
        {
            "session_key": "dashboard:test",
            "source": "acp",
            "tool_name": "",
            "tool_kind": "",
            "outcome": "denied",
            "metadata": {
                "reason": "unasked_watch_oversized_id",
                "backend": ACP_BACKEND_ANTIGRAVITY,
                "tool_call_id": "",
            },
        }
    ]
    assert not getattr(client, "_unasked_watch", {})
    assert long_id[:64] not in str(raised.value)
    assert long_id[:64] not in caplog.text


@pytest.mark.asyncio
async def test_an_id_at_the_key_bound_is_held_and_judged(unasked) -> None:
    from kiro_crew.acp._dispatch_parts.tool_call_provenance import TOOL_CALL_CACHE_MAX_KEY_CHARS

    build, audited = unasked
    client = build()
    asked_id = "a" * TOOL_CALL_CACHE_MAX_KEY_CHARS
    silent_id = "s" * TOOL_CALL_CACHE_MAX_KEY_CHARS
    await _feed(
        client, _call(asked_id, "edit"), _ask(asked_id, "edit"), _call(silent_id, "execute")
    )
    assert set(client._unasked_watch) == {asked_id, silent_id}
    await _feed(client, _end(asked_id))
    client._kill_process.assert_not_awaited()
    with pytest.raises(AcpToolGateUnroutable, match="without a permission request"):
        await _feed(client, _end(silent_id))
    assert [row["metadata"]["reason"] for row in audited] == ["unasked_call_tripwire"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["read", "search", "think", ""])
async def test_an_over_long_id_that_could_never_trip_is_neither_held_nor_stopped(
    unasked, kind: str
) -> None:
    long_id = _over_long_id()
    build, audited = unasked
    client = build()
    await _feed(client, _call(long_id, kind), _end(long_id))
    client._kill_process.assert_not_awaited()
    assert audited == []
    assert not getattr(client, "_unasked_watch", {})


@pytest.mark.asyncio
async def test_the_watch_keeps_only_the_harness_own_must_ask_kinds(unasked) -> None:
    """A frame's ``kind`` is backend-authored and unbounded; the watch keeps one of the
    must-ask row's own literals or ``""``, never the frame's bytes."""
    from kiro_crew.agent_sdk.backends import ACP_BACKEND_MUST_ASK_TOOL_KINDS

    build, audited = unasked
    client = build()
    huge = "k" * (1 << 20)
    await _feed(client, _ask("call-1", huge), _call("call-2", "edit"), _relabel("call-2", huge))
    assert client._unasked_watch == {
        "call-1": {"kind": "", "asked": True},
        "call-2": {"kind": "edit", "asked": False},
    }
    await _feed(client, _relabel("call-1", "execute"))
    assert client._unasked_watch["call-1"] == {"kind": "execute", "asked": True}
    row = ACP_BACKEND_MUST_ASK_TOOL_KINDS[ACP_BACKEND_ANTIGRAVITY]
    assert all(entry["kind"] in row for entry in client._unasked_watch.values())
    await _feed(client, _end("call-1"))
    client._kill_process.assert_not_awaited()
    assert audited == []


@pytest.mark.asyncio
async def test_the_overflow_row_never_carries_an_over_long_id(unasked) -> None:
    from kiro_crew.acp.client_unasked_watch import UNASKED_WATCH_LIMIT

    long_id = _over_long_id()
    build, audited = unasked
    client = build()
    for n in range(UNASKED_WATCH_LIMIT):
        await _feed(client, _call(f"call-{n}", "execute"))
    await _feed(client, _ask("late", "execute"))
    assert len(client._unasked_watch) == UNASKED_WATCH_LIMIT + 1
    with pytest.raises(AcpToolGateUnroutable, match="more tool calls open"):
        await _feed(client, _call(long_id, "read"))
    row = _overflow_row(audited)
    assert row["tool_name"] == ""
    assert row["metadata"]["tool_call_id"] == ""
