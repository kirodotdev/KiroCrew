"""The Strands harness: its gate seal and read-back, launch MCP file, vault env, model.

Strands CLI 0.2.3 runs a Strands Python project over ACP (``strands --acp-server
--agent <project>``) and asks only for a Strands interrupt, so Crew runs it on its own
sealed gate module, which raises one per tool call in Crew's gate envelope. Each fact
the launch builds on is pinned here, against the live corpus under
``test/fixtures/acp_frames/strands/`` where the corpus carries the frame. No network,
no model and no Strands install: the gate module is package data read as bytes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event, gate_envelope
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness import strands as strands_mod
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk import tool_gate as gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_STRANDS,
    ACP_BACKENDS_MODEL_AT_LAUNCH,
    STRANDS_VERIFIED_VERSION,
    Readback,
    Routing,
    gate_readback_for,
    launch_for,
    routing_for,
)

STRANDS = ACP_BACKEND_STRANDS
_CORPUS = Path(__file__).parent / "fixtures" / "acp_frames" / "strands"
NONCE = "nonce-for-this-session"


def _frames(name: str) -> list[dict]:
    lines = (_CORPUS / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _permission_frames(name: str) -> list[dict]:
    return [f for f in _frames(name) if f.get("method") == "session/request_permission"]


# ── Routing ──


def test_strands_is_gate_extension_routed_with_a_load_marker() -> None:
    assert routing_for(STRANDS) is Routing.VERIFIED_GATE_EXTENSION
    assert gate_readback_for(STRANDS) is Readback.LOAD_MARKER
    verdict, _why = gate.routing_verdict(STRANDS)
    assert verdict is gate.Verdict.ROUTED
    assert gate.is_enforced(STRANDS)


def test_the_launch_is_the_project_mode_acp_server_never_the_builtin_profile() -> None:
    launch = launch_for(STRANDS)
    assert launch.binary == "strands"
    assert launch.acp_args == ("--acp-server",)
    assert launch.install_command == f"npm i -g @strands-agents/cli@{STRANDS_VERIFIED_VERSION}"


# ── The gate module ──


def test_the_pinned_digest_is_the_shipped_gate_module() -> None:
    payload = Path(strands_mod.strands_gate_module_path()).read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(payload).hexdigest() == strands_mod.STRANDS_GATE_SHA256


def test_a_gate_module_with_other_bytes_is_never_sealed(tmp_path, monkeypatch) -> None:
    forged = tmp_path / "gate.py"
    forged.write_text("agent = None\n", encoding="utf-8")
    monkeypatch.setattr(strands_mod, "strands_gate_module_path", lambda: str(forged))
    monkeypatch.setattr(strands_mod, "strands_project_dir", lambda: str(tmp_path))
    with pytest.raises(pi_mod.PiGateExtensionTampered):
        strands_mod._seal_strands_gate()
    assert not (tmp_path / "agent.py").exists()


def test_the_sealed_copy_is_the_project_entry_and_read_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(strands_mod, "strands_project_dir", lambda: str(tmp_path))
    sealed = strands_mod._seal_strands_gate()
    assert Path(sealed) == tmp_path / "agent.py"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(sealed).st_mode) == 0o400


def test_the_gate_module_names_the_envelope_and_probe_crew_reads() -> None:
    source = Path(strands_mod.strands_gate_module_path()).read_text(encoding="utf-8")
    # The two spellings the client keys on, so a rename on one side fails here.
    assert f'GATE_NAME = "{strands_mod.STRANDS_GATE_NAME}"' in source
    assert 'ENVELOPE_MARKER = "kiro-crew-gate"' in source
    for name in (
        strands_mod._ENV_GATE_SESSION,
        strands_mod._ENV_GATE_MARKER,
        strands_mod._ENV_PROJECT,
        strands_mod._ENV_STATE_DIR,
        strands_mod._ENV_WITHHELD,
    ):
        assert f'os.environ.pop("{name}"' in source


# ── The load marker ──


def _marker(**overrides: object) -> dict:
    marker = {
        "plugin": strands_mod.STRANDS_GATE_NAME,
        "nonce": NONCE,
        "module": "/opt/run/strands/agent.py",
        "child_env": {
            "names": ["OPENAI_API_KEY"],
            "parent_missing": [],
            "child_visible": [],
            "error": None,
        },
    }
    marker.update(overrides)
    return marker


def _issue(marker: object, names: tuple[str, ...] = ("OPENAI_API_KEY",)) -> str:
    return gate.gate_marker_issue(
        STRANDS, marker, "/opt/run/strands/agent.py", NONCE, child_scrub_names=names
    )


def test_this_sessions_marker_from_crews_module_passes() -> None:
    assert _issue(_marker()) == ""


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"plugin": "something-else"}, "does not name"),
        ({"nonce": "an-earlier-session"}, "another session's nonce"),
        ({"module": "/home/me/agent.py"}, "not Kiro Crew's gate"),
        ({"child_env": None}, "no child-environment proof"),
    ],
)
def test_a_marker_that_does_not_prove_the_gate_refuses(override: dict, fragment: str) -> None:
    assert fragment in _issue(_marker(**override))


def test_a_key_visible_to_a_child_refuses_the_session() -> None:
    proof = {
        "names": ["OPENAI_API_KEY"],
        "parent_missing": [],
        "child_visible": ["OPENAI_API_KEY"],
        "error": None,
    }
    assert "forwarded" in _issue(_marker(child_env=proof))


def test_a_proof_of_other_names_refuses_and_names_the_strands_mapping() -> None:
    issue = _issue(_marker(), names=("ANTHROPIC_API_KEY",))
    assert "agent.strands_env" in issue


def test_a_missing_marker_refuses() -> None:
    assert _issue(None)


# ── The permission envelope, off the live corpus ──


@pytest.mark.parametrize(
    "name", ["permission-deny-allow-live.jsonl", "user-project-tools-live.jsonl"]
)
def test_every_permission_request_in_the_corpus_is_a_gate_envelope(name: str) -> None:
    frames = _permission_frames(name)
    assert frames
    for frame in frames:
        tool_call = frame["params"]["toolCall"]
        envelope = gate_envelope(tool_call, "<nonce>")
        assert envelope is not None, tool_call
        assert envelope["toolCallId"].startswith("tooluse_strands")
        assert envelope["title"] == tool_call["title"]
        # Without this session's nonce the same frame is read as raw, never trusted.
        assert gate_envelope(tool_call, NONCE) is None


def test_the_envelope_carries_the_shell_command_the_gate_judges() -> None:
    frame = _permission_frames("permission-deny-allow-live.jsonl")[0]
    event, recorded = build_permission_event(
        JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"]),
        gate_envelope_nonce="<nonce>",
        harness_backend=STRANDS,
    )
    assert event is not None
    assert event.title == "shell"
    assert event.tool_kind == "execute"
    assert "echo" in event.tool_input
    assert recorded == {"once": "allow", "always": "allow", "reject": "deny"}


def test_a_denied_call_fails_and_an_allowed_one_completes_in_the_corpus() -> None:
    updates = [
        f["params"]["update"]
        for f in _frames("permission-deny-allow-live.jsonl")
        if f.get("method") == "session/update"
        and f["params"]["update"].get("sessionUpdate") == "tool_call_update"
    ]
    assert [u["status"] for u in updates] == ["failed", "completed"]
    assert "did not approve" in json.dumps(updates[0]["content"])


def test_a_projects_own_and_vended_tools_ask_too() -> None:
    titles = [
        f["params"]["toolCall"]["title"]
        for f in _permission_frames("user-project-tools-live.jsonl")
    ]
    assert titles == ["letter_counter", "file_editor", "file_editor"]


# ── The launch MCP file ──


def test_stdio_and_url_elements_both_go_to_the_launch_file() -> None:
    document = strands_mod.strands_mcp_config(
        [
            {
                "name": "probe",
                "command": "/usr/bin/probe",
                "args": ["--x", 1],
                "env": [{"name": "TOKEN", "value": "t"}],
            },
            {
                "type": "http",
                "name": "web",
                "url": "https://h/mcp",
                "headers": [{"name": "A", "value": "b"}],
            },
            {"type": "sse", "name": "events", "url": "https://h/sse", "headers": []},
            {"name": "", "command": "x"},
            {"name": "no-command"},
            "not-an-element",
        ]
    )
    assert document == {
        "mcpServers": {
            "probe": {
                "command": "/usr/bin/probe",
                "args": ["--x", "1"],
                "env": {"TOKEN": "t"},
                "continueOnError": True,
            },
            "web": {"url": "https://h/mcp", "transport": "streamable-http", "headers": {"A": "b"}},
            "events": {"url": "https://h/sse", "transport": "sse", "headers": {}},
        }
    }


def test_no_element_means_no_file() -> None:
    assert strands_mod.strands_mcp_config([]) is None


def test_the_launch_file_is_owner_only_and_removed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(strands_mod, "_config_directory", lambda: str(tmp_path))
    path = strands_mod.write_strands_mcp_config({"mcpServers": {"a": {"command": "x"}}})
    assert Path(path).parent == tmp_path
    assert Path(path).name.startswith(f"strands-mcp-{os.getpid()}-")
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    strands_mod.discard_strands_mcp_config(path)
    assert not Path(path).exists()
    strands_mod.discard_strands_mcp_config(path)


# ── agent.strands_env ──


def test_every_provider_name_the_cli_reads_is_accepted() -> None:
    mapping = {name: f"secret://{name.lower()}" for name in strands_mod.STRANDS_ENV_NAMES}
    strands_mod._validate_strands_env_mapping(mapping)


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("OPENAI_API_KEY", "sk-live", "literal value"),
        ("NODE_OPTIONS", "secret://x", "not one of the provider settings"),
        ("STRANDS_CLI_MCP_DISCOVERY", "secret://x", "not a provider setting"),
        ("KIROCREW_SESSION_KEY", "secret://x", "not a provider setting"),
        ("AWS_SECRET_ACCESS_KEY", "secret://x", "not one of the provider settings"),
        ("bad name", "secret://x", "not a provider setting"),
    ],
)
def test_a_mapping_this_harness_would_not_honour_is_refused(key, value, fragment) -> None:
    with pytest.raises(ValueError) as caught:
        strands_mod._validate_strands_env_mapping({key: value})
    assert fragment in str(caught.value)
    assert value not in str(caught.value) or value.startswith("secret://")


def test_no_mapped_name_is_one_the_agent_scrub_strips() -> None:
    from kiro_crew.sandbox import agent_env_scrub_prefixes

    prefixes = agent_env_scrub_prefixes()
    for name in strands_mod.STRANDS_ENV_NAMES:
        assert not any(name.startswith(prefix) for prefix in prefixes), name


# ── Model ──


@pytest.mark.parametrize("model", ["", None, "auto"])
def test_auto_launches_the_projects_own_default(model) -> None:
    assert strands_mod.strands_model_args(model) == []


def test_a_provider_model_id_is_a_launch_argument() -> None:
    assert strands_mod.strands_model_args("anthropic/claude-sonnet-4-5") == [
        "--model",
        "anthropic/claude-sonnet-4-5",
    ]


def test_a_pin_another_harness_owns_is_left_out(monkeypatch) -> None:
    monkeypatch.setattr(strands_mod.model_scope, "foreign_namespaces", lambda *_a: ("acp",))
    assert strands_mod.strands_model_args("claude-sonnet-4.5") == []


def test_the_model_is_a_launch_argument_and_a_switch_is_refused() -> None:
    from kiro_crew.acp import client_model_selection as selection

    assert STRANDS in ACP_BACKENDS_MODEL_AT_LAUNCH
    client = SimpleNamespace(backend=STRANDS, _session_id="s", _model="bedrock/x")
    asyncio.run(selection.set_model(client, "bedrock/x"))
    with pytest.raises(AcpError, match="new chat"):
        asyncio.run(selection.set_model(client, "anthropic/y"))


# ── The install ──


def _npm_strands(root: Path, version: str) -> str:
    package = root / "lib" / "node_modules" / "@strands-agents" / "cli"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": "@strands-agents/cli", "version": version}), encoding="utf-8"
    )
    script = package / "bin" / "strands.js"
    script.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    return str(script)


def test_the_installed_release_is_read_from_its_manifest(tmp_path) -> None:
    script = _npm_strands(tmp_path, "0.2.3")
    assert strands_mod.strands_installed_version(script) == "0.2.3"
    assert strands_mod._strands_version_issue(script) == ""


def test_another_release_is_refused_with_the_verified_install(tmp_path) -> None:
    script = _npm_strands(tmp_path, "0.9.0")
    issue = strands_mod._strands_version_issue(script)
    assert "0.9.0" in issue and f"@strands-agents/cli@{STRANDS_VERIFIED_VERSION}" in issue


def test_an_unreadable_release_is_not_a_wrong_one(tmp_path) -> None:
    lone = tmp_path / "strands"
    lone.write_text("", encoding="utf-8")
    assert strands_mod._strands_version_issue(str(lone)) == ""


def _venv(project: Path, *, with_harness: bool) -> None:
    python = Path(strands_mod.strands_python(str(project)))
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    if with_harness:
        if sys.platform == "win32":
            site = project / ".venv" / "Lib" / "site-packages"
        else:
            site = project / ".venv" / "lib" / "python3.12" / "site-packages"
        (site / "strands_harness").mkdir(parents=True)


def test_no_python_environment_names_the_command_that_creates_it(tmp_path) -> None:
    issue = strands_mod.strands_python_env_issue(str(tmp_path))
    assert "venv" in issue and "strands-harness==" in issue


def test_an_environment_without_the_harness_names_the_install(tmp_path) -> None:
    _venv(tmp_path, with_harness=False)
    assert "pip install" in strands_mod.strands_python_env_issue(str(tmp_path))


def test_a_ready_environment_has_no_issue(tmp_path) -> None:
    _venv(tmp_path, with_harness=True)
    assert strands_mod.strands_python_env_issue(str(tmp_path)) == ""


# ── The child environment ──


def test_the_child_gets_discovery_off_the_nonce_and_its_state_dir() -> None:
    launch = strands_mod.StrandsLaunch()
    launch._nonce = NONCE
    launch._state_dir = "/state"
    env: dict[str, str] = {}
    launch.apply_spawn_env(env)
    assert env["STRANDS_CLI_MCP_DISCOVERY"] == "off"
    assert env["STRANDS_CLI_SKILL_DISCOVERY"] == "off"
    assert env["KIROCREW_STRANDS_GATE_SESSION"] == NONCE
    assert env["KIROCREW_STRANDS_STATE_DIR"] == "/state"
    assert "KIROCREW_STRANDS_GATE_MARKER" not in env
    assert "KIROCREW_STRANDS_PROJECT" not in env


def test_vault_keys_are_injected_and_every_credential_name_is_withheld(monkeypatch) -> None:
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk"})
    launch = strands_mod.StrandsLaunch()
    env: dict[str, str] = {}

    async def to_thread(fn, *args):
        return fn(*args)

    asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    assert env["OPENAI_API_KEY"] == "sk"
    withheld = env["KIROCREW_STRANDS_WITHHELD"].split(":")
    assert "OPENAI_API_KEY" in withheld
    assert set(strands_mod.STRANDS_CREDENTIAL_NAMES) <= set(withheld)


def test_a_bad_mapping_refuses_the_session(monkeypatch) -> None:
    def refuse() -> dict[str, str]:
        raise ValueError("agent.strands_env entry 'X' is not a provider setting")

    monkeypatch.setattr(strands_mod, "_strands_vault_env", refuse)

    async def to_thread(fn, *args):
        return fn(*args)

    from kiro_crew.acp.client import AcpToolGateUnroutable

    with pytest.raises(AcpToolGateUnroutable, match="not a provider setting"):
        asyncio.run(strands_mod.StrandsLaunch().prepare_spawn_env({}, to_thread=to_thread))


# ── The operator's project ──


def test_no_project_runs_the_bundled_one(monkeypatch) -> None:
    from kiro_crew.config import loader

    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(lambda cls: SimpleNamespace(agent=SimpleNamespace(strands_project=""))),
    )
    assert strands_mod._strands_user_project() == ""


def test_a_project_folder_resolves_to_its_agent_py(tmp_path, monkeypatch) -> None:
    from kiro_crew.config import loader

    (tmp_path / "agent.py").write_text("agent = None\n", encoding="utf-8")
    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(agent=SimpleNamespace(strands_project=str(tmp_path)))
        ),
    )
    assert strands_mod._strands_user_project() == str((tmp_path / "agent.py").resolve())


def test_a_path_that_is_no_project_refuses(tmp_path, monkeypatch) -> None:
    from kiro_crew.config import loader

    monkeypatch.setattr(
        loader.KiroCrewConfig,
        "load",
        classmethod(
            lambda cls: SimpleNamespace(
                agent=SimpleNamespace(strands_project=str(tmp_path / "nope"))
            )
        ),
    )
    with pytest.raises(ValueError, match="not a Strands Python project"):
        strands_mod._strands_user_project()


def test_bedrock_auth_failure_names_the_strands_env_setting():
    """Strands runs sandboxed with ~/.aws masked, so the remedy names the vault mapping."""
    from kiro_crew.acp.transport_errors import _format_acp_error

    frame = {"code": -32603, "message": "Internal error", "data": "AccessDeniedException: denied"}
    strands_text = _format_acp_error(frame, None, backend=ACP_BACKEND_STRANDS)
    assert "agent.strands_env" in strands_text
    assert "AWS_BEARER_TOKEN_BEDROCK" in strands_text
    assert "aws sso login" not in strands_text
    # Every other harness keeps the host-credential advice.
    assert "agent.strands_env" not in _format_acp_error(frame, None, backend="")


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        # The setting defaults: provider bedrock, model auto, so no --model at all.
        ("bedrock", "auto", ""),
        ("", "", ""),
        ("openai", "auto", ""),
        ("anthropic", "some-model", "anthropic/some-model"),
        ("Bedrock", " some.bedrock-id ", "bedrock/some.bedrock-id"),
        ("", "some.bedrock-id", "bedrock/some.bedrock-id"),
        # Written once when the model already carries this provider's prefix ...
        ("openai", "openai/some-model", "openai/some-model"),
        # ... and LiteLLM's own route names keep their slash under its prefix.
        ("litellm", "openai/some-model", "litellm/openai/some-model"),
        ("ollama", "some-model:3b", "ollama/some-model:3b"),
    ],
)
def test_the_configured_provider_and_model_build_the_launch_model(provider, model, expected):
    assert strands_mod.strands_configured_model(provider, model) == expected


def test_the_setting_defaults_pass_no_model_and_name_no_model_id() -> None:
    from kiro_crew.config.sections_agent import AgentConfig

    agent = AgentConfig()
    assert (agent.strands_provider, agent.strands_model) == ("bedrock", "auto")
    assert strands_mod.strands_configured_model(agent.strands_provider, agent.strands_model) == ""
    assert (
        strands_mod.strands_launch_model_args(
            None,
            lambda: strands_mod.strands_configured_model(
                agent.strands_provider, agent.strands_model
            ),
        )
        == []
    )


def test_the_provider_list_is_the_one_the_setting_offers() -> None:
    from dataclasses import fields

    from kiro_crew.config.sections_agent import AgentConfig

    field = next(f for f in fields(AgentConfig) if f.name == "strands_provider")
    assert set(field.metadata["enum"]) == strands_mod.STRANDS_PROVIDERS


def test_a_provider_the_cli_does_not_know_refuses_by_setting() -> None:
    with pytest.raises(ValueError, match="agent.strands_provider"):
        strands_mod.strands_configured_model("azure", "some-model")


# ── Resume and cancel, from the recorded wire ──


def test_session_load_answers_no_modes_and_replays_the_history() -> None:
    frames = _frames("session-load-live.jsonl")
    load = next(f for f in frames if f.get("id") == 2)
    # LOAD_WITHOUT_MODES: the load result is an empty object, so no mode or model
    # select is read back from it.
    assert load["result"] == {}
    replayed = [
        f["params"]["update"]["sessionUpdate"]
        for f in frames
        if f.get("method") == "session/update" and frames.index(f) < frames.index(load)
    ]
    assert "user_message_chunk" in replayed and "tool_call" in replayed
    assert frames[-1]["result"]["stopReason"] == "end_turn"


def test_a_cancel_mid_turn_ends_the_prompt_cancelled() -> None:
    frames = _frames("cancel-midturn-live.jsonl")
    assert frames[-1]["result"] == {"stopReason": "cancelled"}


def test_the_launch_file_mounts_an_mcp_tool_that_still_asks() -> None:
    """The session/new array was empty; the server came from --mcp-config, and its
    tool raised the gate's permission request under the mcp__ title."""
    frames = _frames("mcp-stdio-mount-live.jsonl")
    asks = _permission_frames("mcp-stdio-mount-live.jsonl")
    assert [a["params"]["toolCall"]["title"] for a in asks] == ["mcp__probe__codeword"] * 2
    completed = [
        f
        for f in frames
        if f.get("method") == "session/update"
        and f["params"]["update"].get("status") == "completed"
    ]
    assert any("tangerine-42" in json.dumps(f) for f in completed)


# ── Launch model, and the commands an operator copies ──


def test_a_chat_model_never_reads_the_configured_fallback() -> None:
    """A provider set with no model is refused only when it would be used."""

    def configured() -> str:
        raise AssertionError("the configured fallback was read for a chat with a model")

    assert strands_mod.strands_launch_model_args("openai/gpt-4.1", configured) == [
        "--model",
        "openai/gpt-4.1",
    ]


def test_with_no_chat_model_the_configured_fallback_launches_or_refuses() -> None:
    assert strands_mod.strands_launch_model_args("auto", lambda: "anthropic/claude") == [
        "--model",
        "anthropic/claude",
    ]
    assert strands_mod.strands_launch_model_args(None, lambda: "") == []

    def refused() -> str:
        return strands_mod.strands_configured_model("azure", "some-model")

    with pytest.raises(ValueError, match="agent.strands_provider"):
        strands_mod.strands_launch_model_args("auto", refused)


@pytest.mark.parametrize("windows", [True, False])
def test_the_python_commands_run_in_the_operators_shell(tmp_path, monkeypatch, windows) -> None:
    """PowerShell reads a bare quoted path as a string, so Windows gets ``&``."""
    monkeypatch.setattr(strands_mod.platform_compat, "IS_WINDOWS", windows)
    setup = strands_mod.strands_python_setup_command(str(tmp_path))
    python = strands_mod.strands_python(str(tmp_path))
    _venv(tmp_path, with_harness=False)
    install = strands_mod.strands_python_env_issue(str(tmp_path))
    if windows:
        assert f'; & "{python}" -m pip install' in setup
        assert f'& "{python}" -m pip install' in install
    else:
        assert f' && "{python}" -m pip install' in setup
        assert "&" not in install.replace("&&", "")
