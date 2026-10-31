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
import io
import json
import os
import stat
import sys
import time
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
        "keys": {
            "loaded": ["OPENAI_API_KEY"],
            "file_left": False,
            "launch_env": [],
            "parent_launch_env": [],
            "dumpable": "linux-non-dumpable",
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
    issue = _issue(_marker(child_env=proof))
    assert "forwarded" in issue
    # Counts, never the names: they come straight off the vault mapping.
    assert "OPENAI_API_KEY" not in issue


def test_a_proof_of_other_names_refuses_and_names_the_strands_mapping() -> None:
    issue = _issue(_marker(), names=("ANTHROPIC_API_KEY",))
    assert "agent.strands_env" in issue
    assert "ANTHROPIC_API_KEY" not in issue and "OPENAI_API_KEY" not in issue


def test_the_child_probes_own_error_text_is_not_echoed() -> None:
    proof = {
        "names": ["OPENAI_API_KEY"],
        "parent_missing": [],
        "child_visible": [],
        "error": "RuntimeError: OPENAI_API_KEY",
    }
    issue = _issue(_marker(child_env=proof))
    assert "could not run its child-environment check" in issue
    assert "OPENAI_API_KEY" not in issue


@pytest.mark.parametrize(
    ("dumpable", "shown"),
    [
        ("prctl-failed:1", "(prctl-failed)"),
        ("unavailable:OPENAI_API_KEY", "(unavailable)"),
        ("OPENAI_API_KEY", "(an unrecognised status)"),
    ],
)
def test_a_dumpable_worker_is_reported_by_status_word_only(monkeypatch, dumpable, shown) -> None:
    monkeypatch.setattr(gate.platform_compat, "IS_LINUX", True)
    keys = {
        "loaded": ["OPENAI_API_KEY"],
        "file_left": False,
        "launch_env": [],
        "parent_launch_env": [],
        "dumpable": dumpable,
    }
    issue = _issue(_marker(keys=keys))
    assert shown in issue and "OPENAI_API_KEY" not in issue and ":1" not in issue


def test_an_unseen_name_in_the_child_env_proof_is_counted_not_named() -> None:
    proof = {
        "names": ["OPENAI_API_KEY"],
        "parent_missing": ["OPENAI_API_KEY"],
        "child_visible": [],
        "error": None,
    }
    issue = _issue(_marker(child_env=proof))
    assert "never saw 1 name(s)" in issue and "OPENAI_API_KEY" not in issue


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


def test_a_strands_provider_pin_is_kept_even_when_another_catalog_lists_it(monkeypatch) -> None:
    """OpenCode also advertises ``openai/gpt-4.1``; that is no reason to drop it here."""
    monkeypatch.setattr(strands_mod.model_scope, "foreign_namespaces", lambda *_a: ("opencode",))
    assert strands_mod.strands_model_args("openai/gpt-4.1") == ["--model", "openai/gpt-4.1"]
    # An unknown provider prefix is still judged by the catalogs.
    assert strands_mod.strands_model_args("nope/gpt-4.1") == []


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


def test_vault_keys_go_to_a_one_time_file_never_the_launch_env(monkeypatch, tmp_path) -> None:
    """No provider key in the launch environment: ``/proc/<pid>/environ`` would show it
    to every shell the agent runs. The keys go to an owner-only file in the session's
    private scratch window, and only its path is in the environment."""
    from kiro_crew.acp.harness import strands_keys

    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk"})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=tmp_path, _shared_scratch=None)
    env: dict[str, str] = {"OPENAI_API_KEY": "inherited-from-the-host"}

    async def to_thread(fn, *args):
        return fn(*args)

    asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    assert "OPENAI_API_KEY" not in env
    assert "sk" not in env.values()
    withheld = env["KIROCREW_STRANDS_WITHHELD"].split(":")
    assert "OPENAI_API_KEY" in withheld
    assert set(strands_mod.STRANDS_CREDENTIAL_NAMES) <= set(withheld)
    path = Path(env[strands_keys.KEYS_FILE_ENV])
    assert path.parent.parent == tmp_path
    assert json.loads(path.read_text(encoding="utf-8")) == {"OPENAI_API_KEY": "sk"}
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_vault_keys_with_no_private_window_refuse_the_session(monkeypatch) -> None:
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk"})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=None, _shared_scratch=None)

    async def to_thread(fn, *args):
        return fn(*args)

    from kiro_crew.acp.client import AcpToolGateUnroutable

    env: dict[str, str] = {}
    with pytest.raises(AcpToolGateUnroutable, match="nowhere private"):
        asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    assert "OPENAI_API_KEY" not in env


def test_a_key_file_an_earlier_spawn_left_is_swept_before_the_next(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp.harness import strands_keys

    stale = strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "old"})
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk"})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=tmp_path, _shared_scratch=None)

    async def to_thread(fn, *args):
        return fn(*args)

    env: dict[str, str] = {}
    asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    assert not Path(stale).exists()
    assert Path(env[strands_keys.KEYS_FILE_ENV]).exists()


def test_a_left_key_file_is_swept_even_when_the_mapping_is_now_empty(monkeypatch, tmp_path) -> None:
    """The operator cleared agent.strands_env before the respawn: the old file still goes."""
    from kiro_crew.acp.harness import strands_keys

    own = tmp_path / "own"
    shared = tmp_path / "shared"
    own.mkdir()
    shared.mkdir()
    stale_own = strands_keys.write_keys_file(str(own), {"OPENAI_API_KEY": "old"})
    stale_shared = strands_keys.write_keys_file(str(shared), {"OPENAI_API_KEY": "old"})
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=own, _shared_scratch=shared)

    async def to_thread(fn, *args):
        return fn(*args)

    env: dict[str, str] = {}
    asyncio.run(launch.prepare_spawn_env(env, to_thread=to_thread))
    assert not Path(stale_own).exists() and not Path(stale_shared).exists()
    assert strands_keys.KEYS_FILE_ENV not in env


def test_the_shared_window_is_swept_when_the_session_has_no_private_one(
    monkeypatch, tmp_path
) -> None:
    from kiro_crew.acp.harness import strands_keys

    stale = strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "old"})
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=None, _shared_scratch=tmp_path)

    async def to_thread(fn, *args):
        return fn(*args)

    asyncio.run(launch.prepare_spawn_env({}, to_thread=to_thread))
    assert not Path(stale).exists()


@pytest.mark.parametrize("window", ["_scratch_dir", "_shared_scratch"])
def test_an_old_key_file_that_cannot_be_removed_refuses_the_spawn(
    monkeypatch, tmp_path, window
) -> None:
    """Fail closed: a sweep that cannot prove the window clear starts no child."""
    from kiro_crew.acp.client import AcpToolGateUnroutable
    from kiro_crew.acp.harness import strands_keys

    stale = strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "old"})
    # The removal fails (as on a read-only key directory) and leaves the file.
    monkeypatch.setattr(strands_mod, "remove_keys_dir", lambda directory: None)
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {})
    launch = strands_mod.StrandsLaunch()
    session = {"_scratch_dir": None, "_shared_scratch": None, window: tmp_path}
    launch._session = SimpleNamespace(**session)

    async def to_thread(fn, *args):
        return fn(*args)

    with pytest.raises(AcpToolGateUnroutable, match="could not remove") as refused:
        asyncio.run(launch.prepare_spawn_env({}, to_thread=to_thread))
    assert Path(stale).exists()
    # The text is Kiro Crew's own: nothing the sweep read is echoed.
    assert "keys.json" not in str(refused.value)


def test_a_window_that_cannot_be_listed_refuses_the_spawn(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp.client import AcpToolGateUnroutable

    real_listdir = os.listdir

    def listdir(path):
        if str(path) == str(tmp_path):
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(strands_mod.os, "listdir", listdir)
    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=tmp_path, _shared_scratch=None)

    async def to_thread(fn, *args):
        return fn(*args)

    with pytest.raises(AcpToolGateUnroutable, match="cannot be checked for old provider key"):
        asyncio.run(launch.prepare_spawn_env({}, to_thread=to_thread))


def test_a_key_directory_written_after_the_listing_is_left_to_its_spawn(
    monkeypatch, tmp_path
) -> None:
    """Another spawn of the tree writing its own live file mid-sweep is not a refusal."""
    from kiro_crew.acp.harness import strands_keys

    old = strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "old"})
    real_remove = strands_mod.remove_keys_dir
    live: list[str] = []

    def remove(directory):
        real_remove(directory)
        live.append(strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "live"}))

    monkeypatch.setattr(strands_mod, "remove_keys_dir", remove)
    assert strands_mod._sweep_keys_dirs((str(tmp_path), "")) == ""
    assert not Path(old).exists() and Path(live[0]).exists()


def test_a_prefixed_directory_with_no_key_file_is_not_a_refusal(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp.harness import strands_keys

    planted = tmp_path / f"{strands_keys.KEYS_DIR_PREFIX}empty"
    planted.mkdir()
    monkeypatch.setattr(strands_mod, "remove_keys_dir", lambda directory: None)
    assert strands_mod._sweep_keys_dirs((str(tmp_path), "")) == ""


def test_a_key_file_that_cannot_be_inspected_refuses_the_spawn(monkeypatch, tmp_path) -> None:
    from kiro_crew.acp.harness import strands_keys

    stale = strands_keys.write_keys_file(str(tmp_path), {"OPENAI_API_KEY": "old"})
    monkeypatch.setattr(strands_mod, "remove_keys_dir", lambda directory: None)
    real_lstat = os.lstat

    def lstat(target, *args, **kwargs):
        if str(target) == stale:
            raise PermissionError("cannot inspect")
        return real_lstat(target, *args, **kwargs)

    monkeypatch.setattr(strands_mod.os, "lstat", lstat)
    assert strands_mod._sweep_keys_dirs((str(tmp_path), "")) == strands_mod._SWEEP_KEY_LEFT


def test_a_planted_link_with_the_key_prefix_is_neither_followed_nor_a_refusal(
    tmp_path,
) -> None:
    from kiro_crew.acp.harness import strands_keys

    if sys.platform == "win32":
        pytest.skip("symlinks")
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "keep.txt").write_text("x", encoding="utf-8")
    window = tmp_path / "window"
    window.mkdir()
    (window / f"{strands_keys.KEYS_DIR_PREFIX}planted").symlink_to(target)
    assert strands_mod._sweep_keys_dirs((str(window), "")) == ""
    assert (target / "keep.txt").exists()


@pytest.mark.parametrize(
    ("keys", "fragment"),
    [
        (None, "no key-route proof"),
        (
            {
                "loaded": [],
                "file_left": False,
                "launch_env": [],
                "parent_launch_env": [],
                "dumpable": "linux-non-dumpable",
            },
            "a different set of provider settings",
        ),
        (
            {
                "loaded": ["OPENAI_API_KEY"],
                "file_left": True,
                "launch_env": [],
                "parent_launch_env": [],
                "dumpable": "linux-non-dumpable",
            },
            "left the one-time",
        ),
        (
            {
                "loaded": ["OPENAI_API_KEY"],
                "file_left": False,
                "launch_env": ["OPENAI_API_KEY"],
                "parent_launch_env": [],
                "dumpable": "linux-non-dumpable",
            },
            "worker's launch",
        ),
        (
            {
                "loaded": ["OPENAI_API_KEY"],
                "file_left": False,
                "launch_env": [],
                "parent_launch_env": ["OPENAI_API_KEY"],
                "dumpable": "linux-non-dumpable",
            },
            "Strands CLI's launch",
        ),
    ],
)
def test_a_marker_that_does_not_prove_the_key_route_refuses(keys, fragment) -> None:
    issue = _issue(_marker(keys=keys))
    assert fragment in issue
    # Counts, never names: the names come from the vault mapping.
    assert "OPENAI_API_KEY" not in issue


def test_on_linux_a_dumpable_worker_refuses(monkeypatch) -> None:
    monkeypatch.setattr(gate.platform_compat, "IS_LINUX", True)
    keys = {
        "loaded": ["OPENAI_API_KEY"],
        "file_left": False,
        "launch_env": [],
        "parent_launch_env": [],
        "dumpable": "prctl-failed:1",
    }
    assert "non-dumpable" in _issue(_marker(keys=keys))


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


def test_the_read_back_sends_its_frames_through_the_bounded_blocking_writer() -> None:
    """Two fixed frames, written once through ``transport_framing``, under its cap."""
    import io

    from kiro_crew.acp.transport_framing import write_frames_blocking

    sink = io.BytesIO()
    write_frames_blocking(sink, strands_mod._readback_requests("/work"))
    lines = sink.getvalue().decode("utf-8").splitlines()
    assert [json.loads(line)["method"] for line in lines] == ["initialize", "session/new"]
    with pytest.raises(ValueError, match="blocking-write cap"):
        write_frames_blocking(io.BytesIO(), [{"pad": "x" * 5000}])


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks")
def test_the_stale_key_sweep_never_follows_a_planted_link(monkeypatch, tmp_path) -> None:
    """A prefixed entry in the scratch window may be the agent's link to elsewhere: the
    sweep removes only real one-time directories and leaves the link's target alone."""
    from kiro_crew.acp.harness import strands_keys

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keys.json").write_text("not ours", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    (scratch / f"{strands_keys.KEYS_DIR_PREFIX}planted").symlink_to(outside)

    monkeypatch.setattr(strands_mod, "_strands_vault_env", lambda: {"OPENAI_API_KEY": "sk"})
    launch = strands_mod.StrandsLaunch()
    launch._session = SimpleNamespace(_scratch_dir=scratch, _shared_scratch=None)

    async def to_thread(fn, *args):
        return fn(*args)

    asyncio.run(launch.prepare_spawn_env({}, to_thread=to_thread))
    assert (outside / "keys.json").read_text(encoding="utf-8") == "not ours"


def test_the_node_floor_is_measured_as_the_cli_child_will_see_it(monkeypatch) -> None:
    """The session's PATH, the directory the CLI starts in, and the sandbox wrap.

    A version-manager shim picks Node by directory and by PATH, so the check runs
    where and with what the child runs, each ``node --version`` confined.
    """
    from kiro_crew.acp.harness import pi_node

    seen: dict = {}

    def choose(bin_, floor, search_path, *, cwd="", wrap=None, label=""):
        seen.update(bin=bin_, floor=floor, path=search_path, cwd=cwd, wrap=wrap, label=label)
        return pi_node.PiNodeChoice()

    monkeypatch.setattr(strands_mod.pi_node, "choose_pi_node", choose)
    monkeypatch.setenv("PATH", "/gateway/bin")
    session = SimpleNamespace(
        _agent_env_for_spawn=lambda: {"PATH": os.pathsep.join(["/session/shims", "/usr/bin"])},
        _extra_env={},
        _spawn_work_dir="/work/project",
    )
    wrap = object()

    strands_mod._strands_node_choice(session, "/opt/strands/bin/strands", wrap)

    entries = seen["path"].split(os.pathsep)
    assert "/session/shims" in entries
    assert "/gateway/bin" not in entries
    assert seen["cwd"] == "/work/project"
    assert seen["wrap"] is wrap
    assert seen["floor"] == strands_mod.STRANDS_NODE_FLOOR
    assert seen["label"] == "The Strands CLI"


def test_the_node_probe_is_wrapped_in_the_sandbox_the_preflight_decided() -> None:
    """The probe wrap needs the credential mask, so the choice comes after the preflight
    and before the read-back that launches the CLI on the chosen argv."""
    import inspect

    source = inspect.getsource(strands_mod.StrandsLaunch.resolve_spawn)
    preflight = source.index("launch_mod._sandbox_preflight")
    choice = source.index("_strands_node_choice")
    readback = source.index("self._prove_gate(")
    assert preflight < choice < readback
    assert "pi_mod._pi_probe_wrap(ctx.sandbox_mode, hidden, expose)" in source[choice:readback]


class _ReadbackProbe:
    """A stand-in for the read-back's ``Popen``: records what happened to it."""

    instances: list = []

    def __init__(self, *args, **kwargs) -> None:
        self.stdin = io.BytesIO()
        self.killed = False
        self.waited = False
        self.returncode = None
        _ReadbackProbe.instances.append(self)

    def poll(self):
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        return self.returncode


def _readback_session(work_dir: str) -> SimpleNamespace:
    return SimpleNamespace(_spawn_work_dir=work_dir)


def test_an_oversize_read_back_is_refused_before_any_probe_starts(tmp_path, monkeypatch) -> None:
    _ReadbackProbe.instances = []
    monkeypatch.setattr(strands_mod.subprocess_mod, "Popen", _ReadbackProbe)
    work_dir = str(tmp_path / ("w" * 200) / ("x" * 200) / ("y" * 200)) * 7

    issue, _remedy = strands_mod._verify_strands_gate(
        _readback_session(work_dir),
        ["strands"],
        {},
        "/sealed/agent.py",
        str(tmp_path / "marker.json"),
        "nonce",
        child_scrub_names=(),
    )

    assert "do not fit the probe's pipe" in issue
    assert _ReadbackProbe.instances == []


def test_a_failure_after_the_probe_started_still_kills_and_reaps_it(tmp_path, monkeypatch) -> None:
    _ReadbackProbe.instances = []
    monkeypatch.setattr(strands_mod.subprocess_mod, "Popen", _ReadbackProbe)
    calls = {"n": 0}
    real_write = strands_mod.write_frames_blocking

    def write(stdin, requests):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(stdin, requests)  # the dry run
        raise BrokenPipeError("the probe went away")

    monkeypatch.setattr(strands_mod, "write_frames_blocking", write)

    issue, _remedy = strands_mod._verify_strands_gate(
        _readback_session(str(tmp_path)),
        ["strands"],
        {},
        "/sealed/agent.py",
        str(tmp_path / "marker.json"),
        "nonce",
        child_scrub_names=(),
    )

    assert "could not be read back" in issue
    (probe,) = _ReadbackProbe.instances
    assert probe.killed and probe.waited and probe.stdin.closed


def test_the_reported_model_is_the_one_the_launch_passed() -> None:
    assert strands_mod.strands_launched_model(["--model", "openai/gpt-4.1"]) == "openai/gpt-4.1"
    assert strands_mod.strands_launched_model([]) == ""


def test_resolve_spawn_records_the_launched_model_not_the_pin() -> None:
    """A pin another harness owns is dropped from the argv, so it must not be reported."""
    import inspect

    source = inspect.getsource(strands_mod.StrandsLaunch.resolve_spawn)
    after = source[source.index("strands_launch_model_args") :]
    assert "session._model = strands_launched_model(model_args) or DEFAULT_MODEL" in after


def test_aws_profile_is_not_a_mapping_the_sandboxed_child_could_use() -> None:
    """The sandbox masks ~/.aws, so a profile name points the child at nothing."""
    assert "AWS_PROFILE" not in strands_mod.STRANDS_ENV_NAMES
    assert "AWS_BEARER_TOKEN_BEDROCK" in strands_mod.STRANDS_ENV_NAMES


# ── Blocking helpers: project dir, vault env, launch config sweep, marker, key files ──


def test_project_dir_is_created_owner_only_under_the_gate_artifact_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(tmp_path))
    path = strands_mod.strands_project_dir()
    assert path == str(tmp_path / "strands")
    assert os.path.isdir(path)
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o700


def test_project_dir_that_is_not_a_directory_refuses_the_harness(monkeypatch, tmp_path):
    from kiro_crew.acp.transport_errors import AcpToolGateUnroutable

    (tmp_path / "strands").write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(pi_mod, "_pi_gate_artifact_dir", lambda: str(tmp_path))
    with pytest.raises(AcpToolGateUnroutable, match="could not be created or secured"):
        strands_mod.strands_project_dir()


def test_env_mapping_is_read_from_config(monkeypatch):
    from kiro_crew.config import loader

    fake = SimpleNamespace(agent=SimpleNamespace(strands_env={"OPENAI_API_KEY": "secret://k"}))
    monkeypatch.setattr(loader.KiroCrewConfig, "load", classmethod(lambda cls: fake))
    assert strands_mod._strands_env_mapping() == {"OPENAI_API_KEY": "secret://k"}


def test_vault_env_names_are_validated_and_sorted_without_opening_the_vault(monkeypatch):
    monkeypatch.setattr(strands_mod, "_strands_env_mapping", lambda: {})
    assert strands_mod._strands_vault_env_names() == ()
    mapping = {"OPENAI_API_KEY": "secret://b", "ANTHROPIC_API_KEY": "secret://a"}
    monkeypatch.setattr(strands_mod, "_strands_env_mapping", lambda: mapping)
    monkeypatch.setattr(strands_mod, "resolve_secret_uris", pytest.fail)
    assert strands_mod._strands_vault_env_names() == ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def test_vault_env_names_refuse_a_literal_value(monkeypatch):
    monkeypatch.setattr(strands_mod, "_strands_env_mapping", lambda: {"OPENAI_API_KEY": "sk-x"})
    with pytest.raises(ValueError, match="literal value") as caught:
        strands_mod._strands_vault_env_names()
    assert "sk-x" not in str(caught.value)


def test_vault_env_refuses_a_name_the_agent_scrub_would_remove(monkeypatch):
    monkeypatch.setattr(strands_mod, "agent_env_scrub_prefixes", lambda: ("OPENAI_",))
    monkeypatch.setattr(
        strands_mod, "_strands_env_mapping", lambda: {"OPENAI_API_KEY": "secret://k"}
    )
    with pytest.raises(ValueError, match="scrub removes"):
        strands_mod._strands_vault_env()


def test_vault_env_resolves_the_mapping_through_the_vault(monkeypatch):
    seen: dict = {}

    def _resolve(mapping, home, *, subject):
        seen.update(mapping=dict(mapping), subject=subject)
        return {"OPENAI_API_KEY": "sk-resolved"}, {"OPENAI_API_KEY"}

    monkeypatch.setattr(strands_mod, "_strands_env_mapping", lambda: {})
    assert strands_mod._strands_vault_env() == {}
    monkeypatch.setattr(
        strands_mod, "_strands_env_mapping", lambda: {"OPENAI_API_KEY": "secret://k"}
    )
    monkeypatch.setattr(strands_mod, "resolve_secret_uris", _resolve)
    assert strands_mod._strands_vault_env() == {"OPENAI_API_KEY": "sk-resolved"}
    assert seen == {"mapping": {"OPENAI_API_KEY": "secret://k"}, "subject": "agent.strands_env"}


def test_state_dir_is_under_the_cli_home_never_the_work_dir(tmp_path):
    assert strands_mod._state_dir(tmp_path) == str(
        tmp_path / ".strands" / "cli" / "kirocrew-sessions"
    )


def test_stale_launch_configs_of_dead_or_old_spawns_are_swept(monkeypatch, tmp_path):
    prefix = strands_mod._MCP_CONFIG_PREFIX
    own = os.getpid()
    names = {
        "other.json": True,  # not ours: kept
        f"{prefix}notapid-x.json": True,  # unparsable pid: kept
        f"{prefix}{own}-fresh.json": True,  # this process, recent: kept
        f"{prefix}{own}-old.json": False,  # this process, past the window: removed
        f"{prefix}999999-dead.json": False,  # a pid that is not running: removed
        f"{prefix}424242-live.json": True,  # a live other process: kept
    }
    for name in names:
        (tmp_path / name).write_text("{}", encoding="utf-8")
    old = time.time() - strands_mod._STALE_CONFIG_SECS - 60
    os.utime(tmp_path / f"{prefix}{own}-old.json", (old, old))
    monkeypatch.setattr(strands_mod.platform_compat, "pid_exists", lambda pid: pid == 424242)
    strands_mod._sweep_stale_configs(str(tmp_path))
    assert {name: (tmp_path / name).exists() for name in names} == names


def test_launch_config_write_failure_leaves_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(strands_mod, "_config_directory", lambda: str(tmp_path))
    with pytest.raises(TypeError):
        strands_mod.write_strands_mcp_config({"mcpServers": {"x": object()}})
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(strands_mod._MCP_CONFIG_PREFIX)]


def test_marker_reads_json_and_refuses_everything_else(monkeypatch, tmp_path):
    marker = tmp_path / "marker.json"
    assert strands_mod._read_marker(str(marker)) is None  # absent
    marker.write_text('{"loaded": true}', encoding="utf-8")
    assert strands_mod._read_marker(str(marker)) == {"loaded": True}
    marker.write_text("{not json", encoding="utf-8")
    assert strands_mod._read_marker(str(marker)) is None
    marker.write_bytes(b" " * (strands_mod._MARKER_MAX_BYTES + 1))
    assert strands_mod._read_marker(str(marker)) is None
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(strands_mod.os, "read", lambda fd, n: b"")
    assert strands_mod._read_marker(str(marker)) is None  # short read

    def _boom(fd, n):
        raise OSError("io")

    monkeypatch.setattr(strands_mod.os, "read", _boom)
    assert strands_mod._read_marker(str(marker)) is None


def test_marker_that_is_a_directory_is_refused(tmp_path):
    if sys.platform == "win32":
        pytest.skip("a directory cannot be opened as a file on Windows")
    assert strands_mod._read_marker(str(tmp_path)) is None


def test_key_sweep_treats_a_gone_window_as_clear(tmp_path):
    assert strands_mod._sweep_keys_dirs(("", str(tmp_path / "gone"))) == ""


def test_key_file_left_refuses_when_it_cannot_inspect(monkeypatch, tmp_path):
    candidate = tmp_path / "kc-strands-keys-x"
    candidate.mkdir()
    real_lstat = os.lstat

    def _denied_outer(path, *a, **k):
        raise PermissionError(path)

    monkeypatch.setattr(strands_mod.os, "lstat", _denied_outer)
    assert strands_mod._key_file_left(str(candidate)) is True

    def _denied_inner(path, *a, **k):
        if str(path).endswith(strands_mod.KEYS_FILE_NAME):
            raise PermissionError(path)
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(strands_mod.os, "lstat", _denied_inner)
    assert strands_mod._key_file_left(str(candidate)) is True


def test_keys_file_write_failure_removes_its_directory(monkeypatch, tmp_path):
    from kiro_crew.acp.harness import strands_keys

    with pytest.raises(TypeError):
        strands_keys.write_keys_file(str(tmp_path), {"K": object()})  # type: ignore[dict-item]
    assert list(tmp_path.iterdir()) == []


def test_keys_dir_removal_skips_foreign_and_missing_entries(tmp_path):
    from kiro_crew.acp.harness import strands_keys

    strands_keys.discard_keys_file("")  # nothing named: nothing removed
    foreign = tmp_path / "not-ours"
    foreign.mkdir()
    strands_keys.remove_keys_dir(str(foreign))
    assert foreign.is_dir()
    strands_keys.remove_keys_dir(str(tmp_path / (strands_keys.KEYS_DIR_PREFIX + "missing")))
    path = strands_keys.write_keys_file(str(tmp_path), {"K": "v"})
    strands_keys.discard_keys_file(path)
    assert not os.path.exists(os.path.dirname(path))


def test_sign_in_remedy_names_the_bedrock_vault_mapping_not_the_aws_chain() -> None:
    """The sandbox masks ``~/.aws`` and the launch strips ``AWS_*``: the remedy says so."""
    from kiro_crew.agent_sdk import host_auth

    for text in (
        host_auth.sign_in_remedy(STRANDS),
        host_auth.signed_out_message(STRANDS),
        host_auth.bedrock_auth_remedy(STRANDS),
    ):
        assert "AWS_BEARER_TOKEN_BEDROCK" in text
        assert "AWS_REGION" in text
        assert "credential chain" not in text
