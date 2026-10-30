"""MiMo Code (``mimocode``) ACP backend: registration, launch env, tripwire, tool names,
argument aliases, shared-surface cases."""

from __future__ import annotations

import ast
import asyncio
import copy
import inspect
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import acp_launch_capture as capture_mod
import pytest

import kiro_crew
from kiro_crew import acp_tool_gate, security
from kiro_crew import sel as sel_module
from kiro_crew.acp import _dispatch
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient, AcpToolGateUnroutable
from kiro_crew.acp.harness import mimocode as mimocode_mod
from kiro_crew.acp.harness import process_adapter_for
from kiro_crew.acp.harness.base import ProcessAdapter, SpawnContext
from kiro_crew.acp.harness_tool_names import (
    HARNESS_PARAM_ALIASES,
    HARNESS_UNASKED_TOOL_IDS,
    MIMOCODE_TOOL_IDS_BY_KIRO_TOOL,
    canonical_tool_params,
    harness_tool_match_names,
    qualified_harness_tool_id,
)
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.agent_sdk import host_auth
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_MIMOCODE,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_COMPACT,
    ACP_BACKENDS_INLINE_COMPACTION,
    BASELINE_SELECTABLE_BACKENDS,
    Routing,
    launch_for,
    routing_for,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.tool_gate import permission_setting_for
from kiro_crew.apps.builtins.aws_control.crew.packaging.pipeline import (
    sensitive as aws_control_sensitive,
)
from kiro_crew.config import sections as sections_mod
from kiro_crew.config.sections import agent_env_refusal, coerce_agent_env
from kiro_crew.hook_runtime.tool_identity import ToolCall
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig
from kiro_crew.knowledge.llm_pool import _get_acp_backend
from kiro_crew.messaging import commands as messaging_commands
from kiro_crew.platform.tool_paths import edit_target_candidates
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.mirrors import mimocode as mimocode_mirror_mod
from kiro_crew.providers.mirrors.base import AgentConfigMirror
from kiro_crew.providers.mirrors.mimocode import MimoCodeMirror
from kiro_crew.sandbox import scrub_agent_subprocess_env

# ── acp mimocode backend
# MiMo Code's own launch, mirror, tripwire (0.1.15). Pinned: ``acp.harness.mimocode``,
# ``providers.mirrors.mimocode`` import nothing of opencode's; config variable; 0.1 line;
# env overrides the read-back cannot see; credential relocation the floor cannot fence;
# unasked-shell tripwire for the loopback skip-all switch; ``always`` it would mistake.

# ── Vocabulary, launch and independence


def test_the_backend_is_selectable_and_routed_like_opencode() -> None:
    assert ACP_BACKEND_MIMOCODE in BASELINE_SELECTABLE_BACKENDS
    assert routing_for(ACP_BACKEND_MIMOCODE) is Routing.VERIFIED_SEEDED_SETTINGS
    assert permission_setting_for(ACP_BACKEND_MIMOCODE) == ("permission", "ask")


def test_the_launch_row_names_the_binary_and_pins_the_tested_line() -> None:
    launch = launch_for(ACP_BACKEND_MIMOCODE)
    assert (launch.binary, launch.acp_args, launch.bin_env_var) == ("mimo", ("acp",), "MIMO_BIN")
    assert launch.install_command.endswith("@mimo-ai/cli@0.1")
    assert mimocode_mod._MIMOCODE_TESTED_VERSION == (0, 1)


def test_first_run_setup_completes_without_kiro_cli() -> None:
    """No kiro-cli, so first-run setup offers it alone."""
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_INDEPENDENT_SETUP

    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_INDEPENDENT_SETUP


def test_the_process_adapter_and_the_mirror_are_mimos_own() -> None:
    adapter = process_adapter_for(ACP_BACKEND_MIMOCODE)
    assert type(adapter) is mimocode_mod.MimoCodeLaunch
    assert type(adapter).__mro__[1] is ProcessAdapter
    assert MimoCodeMirror.__mro__[1] is AgentConfigMirror


def test_the_backend_s_session_rows_and_handshake_dialect(tmp_path) -> None:
    """Owned sessions, ``session/load`` with modes, member panel, captured integer
    dialect, per-tool denies in the inline config block."""
    from kiro_crew.acp import client_start
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_HARNESS_OWNED_SESSIONS,
        ACP_BACKENDS_LOAD_WITHOUT_MODES,
        ACP_BACKENDS_MEMBER_PANEL,
    )
    from kiro_crew.providers.mirrors.registry import PROJECTIONS, PerToolDeny, ProjectionKind

    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert ACP_BACKEND_MIMOCODE not in ACP_BACKENDS_LOAD_WITHOUT_MODES
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MEMBER_PANEL
    fixture = Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "session-live.jsonl"
    frames = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()[1:]]
    captured = next(
        f["result"]["protocolVersion"]
        for f in frames
        if isinstance(f.get("result"), dict) and "protocolVersion" in f["result"]
    )
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    sent = client_start._initialize_params(client)["protocolVersion"]
    assert sent == client_start.PROTOCOL_VERSION_MIMOCODE == captured == 1
    projection = PROJECTIONS[ACP_BACKEND_MIMOCODE]
    assert projection.kind is ProjectionKind.MIRROR
    assert projection.per_tool_deny is PerToolDeny.SETTINGS_FILE


#: Client ``_mimocode_*`` fields: per-spawn read-back records.
_MIMOCODE_CLIENT_FIELDS = (
    "_mimocode_denies_in_force",
    "_mimocode_denies_unenforced",
    "_mimocode_native_mounts",
    "_mimocode_disabled_servers",
    "_mimocode_native_mount_launches",
    "_mimocode_mcp_tool_lists",
    "_mimocode_memory_root",
    "_mimocode_config_mcp_servers",
)


def test_the_client_carries_exactly_the_mimocode_fields_reset_is_held_to(tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    assert {name for name in vars(client) if name.startswith("_mimocode_")} == set(
        _MIMOCODE_CLIENT_FIELDS
    )


@pytest.mark.parametrize("field", _MIMOCODE_CLIENT_FIELDS)
def test_reset_state_returns_each_mimocode_field_to_its_fresh_value(tmp_path, field: str) -> None:
    """A new process inherits no read-back from the last launch."""
    fresh = getattr(AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE), field)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    stale: object = ("stale",)
    if isinstance(fresh, str):
        stale = "stale-root"
    elif isinstance(fresh, dict):
        # An in-flight tool-list read is a task; reset cancels it.
        stale = {"stale": MagicMock()}
    elif isinstance(fresh, (frozenset, type(None))):
        stale = frozenset({"stale"})
    setattr(client, field, stale)
    assert getattr(client, field) != fresh
    client._reset_state()
    assert getattr(client, field) == fresh


@pytest.mark.parametrize("module", [mimocode_mod, mimocode_mirror_mod])
def test_the_mimocode_modules_import_nothing_of_opencodes(module) -> None:
    """Neither imports opencode's launch or mirror, so changes there cannot move it."""
    tree = ast.parse(inspect.getsource(module))
    imported = [
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    assert not [name for name in imported if "opencode" in name]
    names = [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    ]
    assert not [name for name in names if "opencode" in name.lower()]


_OPENCODE_MODULES = ("kiro_crew.acp.harness.opencode", "kiro_crew.providers.mirrors.opencode")
#: The facade re-exports all harness helpers (opencode's too): judge it by where the
#: names the launch takes are bound.
_CLIENT_FACADE = "kiro_crew.acp.client"


def _source_of(module: str) -> Path | None:
    rel = Path(*module.split("."))
    root = Path(kiro_crew.__file__).parent.parent
    for candidate in (root / rel.with_suffix(".py"), root / rel / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _kiro_crew_imports(module: str) -> dict[str, set[str]]:
    """*module*'s ``kiro_crew`` imports and names taken; function-level included,
    relative resolved, ``from pkg import submodule`` counted."""
    path = _source_of(module)
    assert path is not None, module
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.setdefault(alias.name, set())
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*parts, *([base] if base else [])])
            found.setdefault(base, set()).update(alias.name for alias in node.names)
            for alias in node.names:
                if _source_of(f"{base}.{alias.name}") is not None:
                    found.setdefault(f"{base}.{alias.name}", set())
    return {name: taken for name, taken in found.items() if name.split(".")[0] == "kiro_crew"}


def _reaches_opencode(modules) -> list[str]:
    return sorted(m for m in modules if any(m.startswith(o) for o in _OPENCODE_MODULES))


def _module_level_origin(module: str, name: str) -> str:
    """Where *module*'s top level last binds *name*: itself, an import source, or ``""``."""
    tree = ast.parse(_source_of(module).read_text(encoding="utf-8"))
    origin = ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                origin = module
        elif isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                origin = module
        elif isinstance(node, ast.ImportFrom):
            if name in {alias.asname or alias.name for alias in node.names}:
                origin = node.module or ""
    return origin


@pytest.mark.parametrize("module", [mimocode_mod.__name__, mimocode_mirror_mod.__name__])
def test_no_module_the_mimocode_modules_import_reaches_opencode_s(module: str) -> None:
    """Their imports import nothing of opencode's either."""
    direct = _kiro_crew_imports(module)
    assert direct, "the import walk found nothing, so it cannot vouch for anything"
    assert not _reaches_opencode(direct)
    for dependency in sorted(set(direct) - {_CLIENT_FACADE}):
        assert not _reaches_opencode(_kiro_crew_imports(dependency)), dependency
    taken = direct.get(_CLIENT_FACADE, set())
    if module == mimocode_mod.__name__:
        assert taken == {"_resolve_spawn_env", "augmented_path"}
    for name in sorted(taken):
        origin = _module_level_origin(_CLIENT_FACADE, name)
        assert origin, f"{name} is not bound at the facade's top level"
        assert not _reaches_opencode([origin]), f"{name} comes from {origin}"


# ── Environment


#: Names read by launch, seed, read-back and the binary resolver. Child envs copy all of
#: ``os.environ``, so other host ``MIMOCODE_*`` go too.
_HOST_MIMO_ENV = (
    "MIMO_BIN",
    "MIMOCODE_HOME",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_CONFIG",
    "MIMOCODE_CONFIG_DIR",
    "MIMOCODE_AUTH_CONTENT",
)


def _isolate_mimocode_env(monkeypatch) -> None:
    """Remove the host's MiMo Code variables."""
    names = {
        *_HOST_MIMO_ENV,
        *mimocode_mod._MIMOCODE_STRIPPED_ENV,
        *(name for name, _value in mimocode_mod._MIMOCODE_PINNED_ENV),
        *(name for name, _why in mimocode_mod._MIMOCODE_REFUSED_ENV),
        *(name for name in os.environ if name.upper().startswith("MIMOCODE_")),
    }
    for name in sorted(names):
        monkeypatch.delenv(name, raising=False)


def test_the_spawn_env_strips_the_unseen_overrides_and_pins_the_import_switch() -> None:
    launch = mimocode_mod.MimoCodeLaunch()
    launch._config_content = '{"permission":"ask"}'
    env = {
        "MIMOCODE_PERMISSION": '{"*":"allow"}',
        "MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS": "1",
        "MIMOCODE_AUTO_APPROVE_DELETE": "1",
        "OPENCODE_CONFIG_CONTENT": "{}",
        "KEEP": "me",
    }
    launch.apply_spawn_env(env)
    assert env == {
        "OPENCODE_CONFIG_CONTENT": "{}",
        "KEEP": "me",
        "MIMOCODE_DISABLE_CLAUDE_IMPORT": "1",
        "MIMOCODE_EXPERIMENTAL_CRON": "false",
        "MIMOCODE_DISABLE_CRON": "1",
        "MIMOCODE_CODEX_MODE": "false",
        "MIMOCODE_CONFIG_CONTENT": '{"permission":"ask"}',
    }


def test_the_codex_toolset_is_pinned_off_over_an_ambient_value() -> None:
    """GPT toolset runs tools in ``exec``; nested asks name no ``tool_call`` frame, so a
    nested shell call reaches the gate with no command."""
    env = {"MIMOCODE_CODEX_MODE": "true"}
    mimocode_mod._apply_mimocode_env(env)
    assert env["MIMOCODE_CODEX_MODE"] == "false"


@pytest.mark.parametrize("data_home", ["absolute", None, "relative"])
def test_the_memory_root_follows_the_harness_data_home(tmp_path, data_home) -> None:
    home = str(tmp_path / "home")
    env = {"HOME": home}
    if data_home == "absolute":
        env["XDG_DATA_HOME"] = str(tmp_path / "xdg")
        expected = str(tmp_path / "xdg" / "mimocode" / "memory")
    else:
        if data_home == "relative":
            env["XDG_DATA_HOME"] = "relative"
        expected = str(tmp_path / "home" / ".local" / "share" / "mimocode" / "memory")
    assert mimocode_mod._mimocode_memory_root(env) == expected


@pytest.mark.parametrize(
    "name",
    [
        "MIMOCODE_EXPERIMENTAL",
        "MIMOCODE_EXPERIMENTAL_ORCHESTRATOR",
        "MIMOCODE_EXPERIMENTAL_WORKFLOW_TOOL",
        "MIMOCODE_ENABLE_EXEC_TOOL",
    ],
)
def test_a_switch_that_adds_an_unasking_tool_is_stripped(name: str) -> None:
    """Each adds ``workflow``, ``session`` or ``exec``; none asks."""
    env = {name: "1"}
    mimocode_mod._apply_mimocode_env(env)
    assert name not in env


@pytest.mark.parametrize(
    ("name", "value"),
    [("MIMOCODE_EXPERIMENTAL_CRON", "true"), ("MIMOCODE_DISABLE_CRON", "0")],
)
def test_the_cron_switches_are_pinned_off_over_an_ambient_value(name: str, value: str) -> None:
    """0.1.15 ``cron`` is on by default and schedules prompts unasked."""
    env = {name: value}
    mimocode_mod._apply_mimocode_env(env)
    assert env["MIMOCODE_EXPERIMENTAL_CRON"] == "false"
    assert env["MIMOCODE_DISABLE_CRON"] == "1"


def test_the_subagent_row_names_the_fork_s_actor_not_its_task_tracker() -> None:
    from kiro_crew.acp.harness_tool_names import MIMOCODE_TOOL_IDS_BY_KIRO_TOOL

    assert MIMOCODE_TOOL_IDS_BY_KIRO_TOOL["use_subagent"] == ("actor",)
    assert harness_tool_match_names("mimocode#actor")[0] == "use_subagent"
    assert "use_subagent" not in harness_tool_match_names("mimocode#task")


def test_a_foreground_actor_and_its_subagent_s_shell_both_ask_crew() -> None:
    """Replays ``acp_frames/mimocode/actor-subagent-live.jsonl``: ``actor`` is
    ``use_subagent``; its subagent asks on the same session, command recovered."""
    from kiro_crew.acp import _dispatch

    fixture = (
        Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "actor-subagent-live.jsonl"
    )
    frames = [json.loads(line) for line in fixture.read_text(encoding="utf-8").splitlines()[1:]]
    names: dict = {}
    raw: dict = {}
    shell: dict = {}
    events = []
    for frame in frames:
        if frame["method"] != "session/request_permission":
            _dispatch.parse_session_update(
                frame["params"]["update"],
                harness_tool_name_cache=names,
                raw_params_cache=raw,
                shell_cache=shell,
            )
            continue
        msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        event, _ = _dispatch.build_permission_event(
            msg,
            harness_tool_name_cache=names,
            raw_params_cache=raw,
            shell_cache=shell,
            harness_backend=ACP_BACKEND_MIMOCODE,
        )
        events.append(event)
    actor, child = events
    assert (actor.title, actor.tool_kind, actor.harness_tool_id) == (
        "actor",
        "other",
        "mimocode#actor",
    )
    assert harness_tool_match_names(actor.harness_tool_id)[0] == "use_subagent"
    assert child.harness_tool_id == "mimocode#bash" and child.is_shell
    assert child.shell_command == "echo actor-child-ok > ~/mimo-proj/actor-fg.txt"


def test_the_seed_merges_the_ambient_mimocode_variable_not_opencodes(monkeypatch) -> None:
    _isolate_mimocode_env(monkeypatch)
    monkeypatch.setenv("MIMOCODE_CONFIG_CONTENT", json.dumps({"model": "probe/m"}))
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", json.dumps({"model": "other/m"}))
    seed = json.loads(mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE, ("srv_tool",)))
    assert seed == {"model": "probe/m", "permission": {"*": "ask", "srv_tool": "deny"}}


# ── Read-back and version gate


def _answer(stdout: str, returncode: int = 0):
    return type("_Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": ""})()


def _readback(monkeypatch, tmp_path, stdout: str, **env: str):
    """Run the read-back as the launch does; return (verdict, child env, client)."""
    seen: dict = {}

    def _run(argv, **kwargs):
        seen["env"] = dict(kwargs["env"])
        return _answer(stdout)

    monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", _run)
    _isolate_mimocode_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    seed = mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE)
    verdict = mimocode_mod._verify_mimocode_routing(
        client, ACP_BACKEND_MIMOCODE, ["/opt/bin/mimo", "debug", "config"], seed
    )
    return verdict, seen.get("env"), client


def test_an_ask_read_back_passes_with_the_child_env_cleaned(monkeypatch, tmp_path) -> None:
    verdict, env, _client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}, "agent": {}}',
        MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS="1",
        MIMOCODE_AUTO_APPROVE_DELETE="1",
        MIMOCODE_PERMISSION='{"*": "allow"}',
    )
    assert verdict == ("", "")
    assert json.loads(env["MIMOCODE_CONFIG_CONTENT"]) == {"permission": "ask"}
    for name, value in mimocode_mod._MIMOCODE_PINNED_ENV:
        assert env[name] == value
    for stripped in mimocode_mod._MIMOCODE_STRIPPED_ENV:
        assert stripped not in env


def test_a_permissive_read_back_refuses(monkeypatch, tmp_path) -> None:
    """A CONFIG problem: remedy names the source outranking the seed, not the reinstall
    advice for a read-back that could not run."""
    (issue, remedy), _env, _client = _readback(
        monkeypatch, tmp_path, '{"permission": {"*": "allow"}}'
    )
    assert "allow" in issue
    assert "'permission' reads 'allow' rather than the required 'ask'" in issue
    assert remedy == acp_tool_gate.remediation_for(ACP_BACKEND_MIMOCODE)
    assert "higher-precedence config source" in remedy
    assert "debug config" not in remedy
    assert mimocode_mod.MIMO_INSTALL_COMMAND not in remedy


def test_an_agent_override_refuses(monkeypatch, tmp_path) -> None:
    """0.1.15: a project agent override survives the inline seed."""
    (issue, remedy), _env, _client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}, "agent": {"build": {"permission": {"bash": "allow"}}}}',
    )
    assert "'build'" in issue
    assert "higher-precedence config source" in remedy


def test_a_relocated_credential_home_refuses_before_any_child(monkeypatch, tmp_path) -> None:
    (issue, remedy), env, _client = _readback(
        monkeypatch, tmp_path, '{"permission": {"*": "ask"}}', MIMOCODE_HOME=str(tmp_path)
    )
    assert env is None, "the read-back child ran with a refused variable in its environment"
    assert issue.startswith("MIMOCODE_HOME is set")
    assert remedy.startswith("Unset MIMOCODE_HOME in the gateway's environment")


def test_the_read_back_records_mimos_own_session_fields(monkeypatch, tmp_path) -> None:
    """Rewritten MCP names and native mounts go to mimocode's fields."""
    verdict, _env, client = _readback(
        monkeypatch,
        tmp_path,
        '{"permission": {"*": "ask"}, "mcp": {"docs.server": {}, "plain": {}}}',
    )
    assert verdict == ("", "")
    assert client._mimocode_config_mcp_servers == ("docs.server",)
    assert client._mimocode_native_mounts == frozenset({"docs.server", "plain"})
    assert client._opencode_config_mcp_servers == ()
    assert client._opencode_native_mounts == frozenset()
    assert client._harness_mcp_server_names() == ("docs.server",)
    # The recorded name splits the harness's rewritten title back.
    qualified = qualified_harness_tool_id(
        ACP_BACKEND_MIMOCODE, "docs_server_search", client._harness_mcp_server_names()
    )
    assert qualified.split("#", 1)[1].split("|")[0] == "@docs.server/search"


@pytest.mark.parametrize(
    ("stdout", "refused", "named"),
    [
        ("0.1.15", False, ""),
        ("0.2.0", True, "0.2"),
        ("1.0.3", True, "1.0"),
        ("mimo", True, "readable version"),
    ],
)
def test_the_version_gate_holds_the_0_1_line(monkeypatch, tmp_path, stdout, refused, named) -> None:
    _isolate_mimocode_env(monkeypatch)
    monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", lambda argv, **kw: _answer(stdout))

    async def _wrap(argv, **kwargs):
        return list(argv), ""

    monkeypatch.setattr(mimocode_mod, "wrap_argv_async", _wrap)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    issue, remedy = asyncio.run(
        mimocode_mod._mimocode_version_refusal(
            client, "/opt/bin/mimo", sandbox_mode="standard", hidden=(), expose=()
        )
    )
    assert bool(issue) is refused
    if refused:
        assert named in issue and "0.1" in issue
        assert "@mimo-ai/cli@0.1" in remedy and "MIMO_BIN" in remedy


# ── The unasked-shell tripwire


def _tripwire_client() -> AcpClient:
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_MIMOCODE
    client._unasked_shell_tripwire = True
    client._pi_gate_asked_ids = set()
    client._pi_gate_denied_ids = set()
    client._pi_gate_request_tool = {}
    client._tool_call_is_shell = {}
    client._session_id = "s1"
    client._session_key = "k1"
    client._kill_process = AsyncMock(return_value=None)
    return client


def _audit(monkeypatch) -> list[dict]:
    """Security event log rows the tripwire writes."""
    written: list[dict] = []
    monkeypatch.setattr(
        sel_module,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **fields: written.append(fields)),
    )
    return written


_UNASKED = "without asking Kiro Crew's gate"
_DENIED = "after Kiro Crew's gate DENIED it"
_UNANSWERED = "before Kiro Crew answered its permission request"


def _assert_tripped(excinfo, written: list, tool_call_id: str, *, call, what, outcome) -> None:
    """Refusal and audit row carry this harness's remedy, not pi's."""
    message = str(excinfo.value)
    assert message.startswith(
        f"MiMo Code ran {call} {tool_call_id} {what}, so its asking posture is no longer "
        "in force for this session; the harness was stopped."
    ), message
    assert "Start a new chat." in message
    assert "its own loopback permission route" in message
    assert "no longer reach Kiro Crew's gate" in message
    assert "PI_ACP_PI_COMMAND" not in message and "--patch overlay" not in message
    assert written == [
        {
            "session_key": "k1",
            "source": "acp",
            "tool_name": tool_call_id,
            "tool_kind": "other",
            "outcome": outcome,
            "metadata": {"reason": "unasked_shell_tripwire", "backend": ACP_BACKEND_MIMOCODE},
        }
    ]


def _completed(tool_call_id: str, kind: str | None) -> JsonRpcMessage:
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": tool_call_id,
        "status": "completed",
    }
    if kind is not None:
        update["kind"] = kind
    return JsonRpcMessage(method="session/update", params={"sessionId": "s1", "update": update})


def _asked(tool_call_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=7,
        method="session/request_permission",
        params={"sessionId": "s1", "toolCall": {"toolCallId": tool_call_id, "rawInput": {}}},
    )


def test_an_unasked_shell_completion_stops_the_harness(monkeypatch) -> None:
    """Captured bypass: after skip-all a shell call ran with no permission frame."""
    written = _audit(monkeypatch)
    client = _tripwire_client()
    with pytest.raises(AcpToolGateUnroutable, match="shell call call-1") as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-1", "execute")))
    assert client._kill_process.await_count == 1
    _assert_tripped(
        excinfo, written, "call-1", call="shell call", what=_UNASKED, outcome="ran_without_gate"
    )


def test_the_kind_falls_back_to_the_cached_shell_classification(monkeypatch) -> None:
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._tool_call_is_shell["call-2"] = True
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-2", None)))
    _assert_tripped(
        excinfo, written, "call-2", call="shell call", what=_UNASKED, outcome="ran_without_gate"
    )


@pytest.mark.parametrize(
    "name", sorted(HARNESS_UNASKED_TOOL_IDS[ACP_BACKEND_MIMOCODE]), ids=lambda n: n
)
def test_an_unasked_completion_of_a_never_asking_tool_is_normal(monkeypatch, name) -> None:
    """``memory``, ``history``, ``task`` and the rest of the row never ask here."""
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._tool_call_harness_tool_name = {"call-3": name}
    asyncio.run(client._tripwire_pi_gate(_completed("call-3", "other")))
    assert client._kill_process.await_count == 0
    assert written == []


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("crew-probe_crew_probe_echo", "other"),
        ("read", "read"),
        ("grep", "search"),
        ("webfetch", "fetch"),
        ("websearch", "other"),
        ("skill", "other"),
        ("view_image", "read"),
        ("", None),
    ],
    ids=["mcp", "read", "grep", "webfetch", "websearch", "skill", "view_image", "unnamed"],
)
def test_an_unasked_completion_of_any_other_tool_stops_the_harness(monkeypatch, name, kind) -> None:
    """Seeded, all other tools (MCP too) ask, so after ``skip-all`` any such call trips,
    not only shell; an unnamed tool is not presumed to skip the ask."""
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._tool_call_harness_tool_name = {"call-3": name} if name else {}
    with pytest.raises(AcpToolGateUnroutable, match="tool call call-3 without asking") as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-3", kind)))
    assert client._kill_process.await_count == 1
    _assert_tripped(
        excinfo, written, "call-3", call="tool call", what=_UNASKED, outcome="ran_without_gate"
    )


def test_the_never_asking_row_names_the_unasked_native_tools_backends() -> None:
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_UNASKED_NATIVE_TOOLS

    assert set(HARNESS_UNASKED_TOOL_IDS) == set(ACP_BACKENDS_UNASKED_NATIVE_TOOLS)
    # Asking tools are outside the row; ``actor`` alone asks only for some actions.
    asking = {tool for ids in MIMOCODE_TOOL_IDS_BY_KIRO_TOOL.values() for tool in ids}
    assert asking & HARNESS_UNASKED_TOOL_IDS[ACP_BACKEND_MIMOCODE] == {"actor"}


def test_an_asked_shell_call_completes_and_the_ask_is_consumed(monkeypatch) -> None:
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._note_pi_gate_asked(_asked("call-4"))
    # Crew's answer: approve_tool pops it before writing.
    client._pi_gate_request_tool.pop("7")
    asyncio.run(client._tripwire_pi_gate(_completed("call-4", "execute")))
    assert client._kill_process.await_count == 0
    assert written == []
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-4", "execute")))
    _assert_tripped(
        excinfo, written, "call-4", call="shell call", what=_UNASKED, outcome="ran_without_gate"
    )


def test_a_denied_call_that_completes_trips_whatever_its_kind(monkeypatch) -> None:
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._pi_gate_asked_ids.add("call-5")
    client._pi_gate_denied_ids.add("call-5")
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-5", "other")))
    _assert_tripped(
        excinfo, written, "call-5", call="tool call", what=_DENIED, outcome="ran_despite_deny"
    )


def test_an_unarmed_session_never_trips(monkeypatch) -> None:
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._unasked_shell_tripwire = False
    asyncio.run(client._tripwire_pi_gate(_completed("call-6", "execute")))
    assert client._kill_process.await_count == 0
    assert written == []


def test_a_call_completing_before_crew_answered_stops_the_harness(monkeypatch) -> None:
    """Loopback ``POST /permission/:id/reply`` answered; the call beat Crew's answer."""
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._note_pi_gate_asked(_asked("call-8"))
    with pytest.raises(AcpToolGateUnroutable, match="before Kiro Crew answered") as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed("call-8", "execute")))
    assert client._pi_gate_request_tool == {}
    _assert_tripped(
        excinfo, written, "call-8", call="shell call", what=_UNANSWERED, outcome="ran_before_answer"
    )


def test_crews_own_answer_leaves_nothing_outstanding(monkeypatch, tmp_path) -> None:
    written = _audit(monkeypatch)
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._unasked_shell_tripwire = True
    client._kill_process = AsyncMock(return_value=None)
    client._note_pi_gate_asked(_asked("call-9"))
    client._permission_options[7] = {"once": "once", "reject": "reject"}
    client._permission_gate_events = {7: object()}
    monkeypatch.setattr("kiro_crew.permission_floor.refusal_for", lambda event: None)
    outstanding_at_write: list = []

    async def _send(request_id, result, **kwargs):
        outstanding_at_write.append(dict(client._pi_gate_request_tool))

    monkeypatch.setattr(client, "_send_response", _send)
    assert asyncio.run(client.approve_tool(7)) is True
    assert outstanding_at_write == [{}], "the answer went out while still outstanding"
    asyncio.run(client._tripwire_pi_gate(_completed("call-9", "execute")))
    assert client._kill_process.await_count == 0
    assert written == []


def test_a_failed_call_consumes_its_outstanding_ask(monkeypatch) -> None:
    written = _audit(monkeypatch)
    client = _tripwire_client()
    client._note_pi_gate_asked(_asked("call-10"))
    failed = _completed("call-10", "execute")
    failed.params["update"]["status"] = "failed"
    asyncio.run(client._tripwire_pi_gate(failed))
    assert client._pi_gate_request_tool == {}
    assert client._kill_process.await_count == 0
    assert written == []


def _write_client(tmp_path, name: str = "edit"):
    client = _tripwire_client()
    client._mimocode_memory_root = str(tmp_path / "data" / "mimocode" / "memory")
    client._tool_call_harness_tool_name = {"call-w": name}
    client._tool_call_params = {}
    client._spawn_work_dir = str(tmp_path / "proj")
    return client


def _completed_with(raw_input: dict, kind: str = "edit") -> JsonRpcMessage:
    message = _completed("call-w", kind)
    message.params["update"]["rawInput"] = raw_input
    return message


def _assert_write_tripped(excinfo, written: list) -> None:
    _assert_tripped(
        excinfo, written, "call-w", call="tool call", what=_UNASKED, outcome="ran_without_gate"
    )


def test_an_unasked_write_outside_the_memory_tree_stops_the_harness(monkeypatch, tmp_path) -> None:
    """Edits outside the memory tree ask; unasked, the gate never saw it (skip-all or
    a stored approval)."""
    written = _audit(monkeypatch)
    client = _write_client(tmp_path)
    target = str(tmp_path / "proj" / "notes.txt")
    with pytest.raises(AcpToolGateUnroutable, match="tool call call-w without asking") as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed_with({"file_path": target})))
    assert client._kill_process.await_count == 1
    _assert_write_tripped(excinfo, written)


@pytest.mark.parametrize(
    ("name", "key"),
    [("edit", "file_path"), ("write", "file_path"), ("notebook_edit", "notebook_path")],
)
def test_an_unasked_write_inside_the_memory_tree_is_normal(
    monkeypatch, tmp_path, name, key
) -> None:
    written = _audit(monkeypatch)
    client = _write_client(tmp_path, name)
    target = str(tmp_path / "data" / "mimocode" / "memory" / "projects" / "p" / "note.md")
    asyncio.run(client._tripwire_pi_gate(_completed_with({key: target})))
    assert client._kill_process.await_count == 0
    assert written == []


@pytest.mark.skipif(
    sys.platform == "win32", reason="creating a symlink needs a privilege Windows CI lacks"
)
def test_a_memory_path_that_resolves_outside_the_tree_trips(monkeypatch, tmp_path) -> None:
    written = _audit(monkeypatch)
    memory = tmp_path / "data" / "mimocode" / "memory"
    memory.mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (memory / "link").symlink_to(tmp_path / "outside")
    client = _write_client(tmp_path)
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(
            client._tripwire_pi_gate(_completed_with({"file_path": str(memory / "link" / "x.md")}))
        )
    _assert_write_tripped(excinfo, written)


def test_a_patch_that_moves_a_file_into_the_memory_tree_trips(monkeypatch, tmp_path) -> None:
    """``apply_patch`` skips asking for a move into the memory tree; both ends judged."""
    written = _audit(monkeypatch)
    client = _write_client(tmp_path, "apply_patch")
    source = tmp_path / "proj" / "keep.txt"
    dest = tmp_path / "data" / "mimocode" / "memory" / "keep.txt"
    patch = f"*** Begin Patch\n*** Update File: {source}\n*** Move to: {dest}\n@@\n-a\n+a\n*** End Patch"
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed_with({"patch_text": patch})))
    _assert_write_tripped(excinfo, written)


def test_an_unasked_write_naming_no_target_trips(monkeypatch, tmp_path) -> None:
    written = _audit(monkeypatch)
    client = _write_client(tmp_path)
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed_with({})))
    _assert_write_tripped(excinfo, written)


def test_an_unasked_write_on_a_session_with_no_memory_tree_trips(monkeypatch, tmp_path) -> None:
    """With no recorded tree no write may skip its ask."""
    written = _audit(monkeypatch)
    client = _write_client(tmp_path)
    client._mimocode_memory_root = ""
    target = str(tmp_path / "data" / "mimocode" / "memory" / "note.md")
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        asyncio.run(client._tripwire_pi_gate(_completed_with({"file_path": target})))
    _assert_write_tripped(excinfo, written)


def test_an_unasked_tool_outside_the_write_row_is_not_judged_by_its_path(
    monkeypatch, tmp_path
) -> None:
    written = _audit(monkeypatch)
    client = _write_client(tmp_path, "memory")
    asyncio.run(
        client._tripwire_pi_gate(_completed_with({"file_path": "/etc/passwd"}, kind="other"))
    )
    assert client._kill_process.await_count == 0
    assert written == []


# ── The launch, end to end at its own seams


class _MimoCodeLaunchRig:
    """``MimoCodeLaunch.resolve_spawn`` against fakes at the adapter's seams."""

    BIN = "/opt/bin/mimo"
    HIDDEN = ("/opt/creds/.aws",)
    SANDBOX = ["/opt/run/kirocrew_sandbox_launcher"]
    #: Launcher artifact per wrap; the launch must reclaim each.
    LAUNCHERS = {
        "wrap_version": "/opt/run/launcher-version.sh",
        "wrap_readback": "/opt/run/launcher-readback.sh",
    }

    def __init__(
        self,
        monkeypatch,
        tmp_path,
        *,
        routing=("", ""),
        probe_stdout="0.1.15",
        readback_stdout: str | None = None,
    ):
        """*readback_stdout*: real read-back on that document, not *routing*."""
        _isolate_mimocode_env(monkeypatch)
        #: ``(name, thread id, args)`` per seam, in reach order.
        self.events: list[tuple] = []
        self.loop_thread: int | None = None
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
        self.adapter = mimocode_mod.MimoCodeLaunch()
        self._tmp_path = tmp_path
        real_verify = mimocode_mod._verify_mimocode_routing
        reclaim_of = {
            path: name.replace("wrap_", "reclaim_") for name, path in self.LAUNCHERS.items()
        }
        _record = self.record

        def _reclaim(path):
            _record(reclaim_of.get(path, "reclaim_unknown"), path)

        monkeypatch.setattr(launch_mod, "_unlink_readback_launcher", _reclaim)

        async def _launch(backend):
            _record("resolve", backend)
            return self.BIN, [self.BIN, "acp"], "mimo acp", "mimo"

        async def _prepare_session_mcp():
            _record("session_mcp")

        async def _preflight(preflight, backend, mode):
            _record("preflight", preflight, backend, mode)
            return self.HIDDEN

        async def _wrap(argv, **kwargs):
            name = "wrap_version" if "--version" in argv else "wrap_readback"
            _record(name, list(argv), kwargs)
            return [*self.SANDBOX, *argv], self.LAUNCHERS[name]

        def _verify(session, backend, argv, config_content):
            _record("verify", list(argv), json.loads(config_content))
            if readback_stdout is None:
                return routing
            return real_verify(session, backend, argv, config_content)

        def _run(argv, **kwargs):
            if "--version" in argv:
                _record("version_probe", argv, kwargs)
                return _answer(probe_stdout)
            _record("readback_child", argv, kwargs)
            return _answer(readback_stdout or "")

        real_child_env = mimocode_mod._mimocode_child_env

        def _child_env(session):
            _record("child_env")
            return real_child_env(session)

        monkeypatch.setattr(mimocode_mod, "_mimocode_child_env", _child_env)
        monkeypatch.setattr(launch_mod, "resolve_self_served_launch", _launch)
        monkeypatch.setattr(self.client, "_prepare_session_mcp", _prepare_session_mcp)
        monkeypatch.setattr(launch_mod, "_run_preflight_bounded", _preflight)
        monkeypatch.setattr(mimocode_mod, "wrap_argv_async", _wrap)
        monkeypatch.setattr(mimocode_mod, "_verify_mimocode_routing", _verify)
        monkeypatch.setattr(mimocode_mod.subprocess_mod, "run", _run)

    def record(self, name: str, *args) -> None:
        self.events.append((name, threading.get_ident(), args))

    def names(self) -> list[str]:
        return [event[0] for event in self.events]

    def event(self, name: str) -> tuple:
        """The one event of *name*; fails if reached twice or never."""
        matches = [event for event in self.events if event[0] == name]
        assert len(matches) == 1, f"{name!r} was reached {len(matches)} times: {self.names()}"
        return matches[0]

    def reclaimed(self) -> list[str]:
        """Reclaimed launcher artifacts, in order."""
        return [event[2][0] for event in self.events if event[0].startswith("reclaim_")]

    def run(self):
        async def _go():
            self.loop_thread = threading.get_ident()
            return await self.adapter.resolve_spawn(
                SpawnContext(
                    agent="kirocrew",
                    work_dir=self._tmp_path,
                    model=None,
                    environ={},
                    home=self._tmp_path,
                    sandbox_mode="standard",
                    session=self.client,
                )
            )

        return asyncio.run(asyncio.wait_for(_go(), timeout=30))


def test_the_launch_reads_back_the_mimocode_seed_and_arms_the_tripwire(
    monkeypatch, tmp_path
) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    assert rig.client._unasked_shell_tripwire is False
    plan = rig.run()
    assert plan.argv == [rig.BIN, "acp"]
    _name, _thread, (argv, seed) = rig.event("verify")
    assert argv == [*rig.SANDBOX, rig.BIN, "debug", "config"]
    assert seed["permission"] == "ask"
    assert rig.client._unasked_shell_tripwire is True
    # Each launcher is reclaimed after its child exits.
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]


def test_both_children_run_bounded_in_the_session_work_dir_without_a_shell(
    monkeypatch, tmp_path
) -> None:
    """Each child gets an argv list, a timeout and the session work dir (config root)."""
    seed_doc = '{"permission": {"*": "ask"}}'
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, readback_stdout=seed_doc)
    rig.run()
    for name, timeout in (
        ("version_probe", mimocode_mod._MIMOCODE_VERSION_TIMEOUT_S),
        ("readback_child", mimocode_mod._MIMOCODE_READBACK_TIMEOUT_S),
    ):
        _name, _thread, (argv, kwargs) = rig.event(name)
        assert isinstance(argv, list) and argv[0] == rig.SANDBOX[0], name
        assert kwargs["timeout"] == timeout and timeout > 0, name
        assert kwargs["cwd"] == rig.client._spawn_work_dir, name
        assert Path(kwargs["cwd"]).resolve() == tmp_path.resolve(), name
        assert kwargs.get("shell", False) is False, name
    _name, _thread, (_argv, readback_kwargs) = rig.event("readback_child")
    _name, _thread, (_argv, seed) = rig.event("verify")
    assert json.loads(readback_kwargs["env"]["MIMOCODE_CONFIG_CONTENT"]) == seed
    _name, _thread, (_argv, version_kwargs) = rig.event("version_probe")
    assert "MIMOCODE_CONFIG_CONTENT" not in version_kwargs["env"], "the version child got a seed"
    assert rig.client._unasked_shell_tripwire is True


def test_the_read_back_and_the_version_probe_run_wrapped_and_off_the_loop(
    monkeypatch, tmp_path
) -> None:
    """Both run the harness binary: behind the sandbox and credential mask, on a worker
    thread, since inline either stalls every dashboard tab for a session start."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    expose = acp_tool_gate.adapter_expose_files(ACP_BACKEND_MIMOCODE, rig.HIDDEN)
    for name, argv in (
        ("wrap_version", [rig.BIN, "--version"]),
        ("wrap_readback", [rig.BIN, "debug", "config"]),
    ):
        _name, _thread, (wrapped, kwargs) = rig.event(name)
        assert wrapped == argv
        assert kwargs["extra_hidden_dirs"] == rig.HIDDEN, f"{name} lost the session's mask"
        assert kwargs["extra_expose_files"] == expose
        assert kwargs["strip_python_env"] is True
    _name, probe_thread, (probe_argv, _kwargs) = rig.event("version_probe")
    assert probe_argv == [*rig.SANDBOX, rig.BIN, "--version"]
    assert probe_thread != rig.loop_thread, "the version child ran on the event loop"
    _name, verify_thread, _args = rig.event("verify")
    assert verify_thread != rig.loop_thread, "the routing read-back ran on the event loop"
    # Child env reads agent.env and the agent socket from disk, so off the loop.
    env_threads = [event[1] for event in rig.events if event[0] == "child_env"]
    assert env_threads, "no child environment was built"
    assert rig.loop_thread not in env_threads, "a child environment was built on the loop"
    for name in ("reclaim_version", "reclaim_readback"):
        _name, reclaim_thread, _args = rig.event(name)
        assert reclaim_thread != rig.loop_thread, f"{name} unlinked on the event loop"


def test_the_floor_and_the_version_gate_precede_the_read_back(monkeypatch, tmp_path) -> None:
    """Mask before any child; untested binary never reaches the read-back; each launcher
    reclaimed after its child, before the next wrap."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    names = rig.names()
    assert (
        names.index("session_mcp")
        < names.index("preflight")
        < names.index("wrap_version")
        < names.index("version_probe")
        < names.index("reclaim_version")
        < names.index("wrap_readback")
        < names.index("verify")
        < names.index("reclaim_readback")
    )
    _name, _thread, (preflight, backend, mode) = rig.event("preflight")
    assert preflight is launch_mod._sandbox_preflight
    assert (backend, mode) == (ACP_BACKEND_MIMOCODE, "standard")


def _assert_raised_without_context(excinfo) -> None:
    """Raised ``from None``: the gate's exception is not chained."""
    assert excinfo.value.__suppress_context__ is True
    assert excinfo.value.__cause__ is None


def test_a_relocated_home_is_refused_before_any_child_runs(monkeypatch, tmp_path) -> None:
    """``MIMOCODE_HOME`` moves the credential past the mask; not even ``--version`` runs."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    monkeypatch.setenv("MIMOCODE_HOME", str(tmp_path / "elsewhere"))
    with pytest.raises(AcpToolGateUnroutable, match="MIMOCODE_HOME is set") as excinfo:
        rig.run()
    assert "Unset MIMOCODE_HOME in the gateway's environment" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert not {"wrap_version", "version_probe", "wrap_readback", "verify"} & set(rig.names())
    assert rig.reclaimed() == [], "no child ran, so nothing was there to reclaim"
    assert rig.client._unasked_shell_tripwire is False


def test_the_launch_records_the_memory_root_it_armed_for(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path)
    rig.run()
    assert rig.client._mimocode_memory_root == str(tmp_path / "xdg" / "mimocode" / "memory")


def test_a_refused_routing_leaves_the_tripwire_unarmed(monkeypatch, tmp_path) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, routing=("permission is allow", "set ask"))
    with pytest.raises(AcpToolGateUnroutable) as excinfo:
        rig.run()
    message = str(excinfo.value)
    assert "would not reach Kiro Crew's security gate (permission is allow)" in message
    assert message.endswith(" set ask")
    _assert_raised_without_context(excinfo)
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


def test_an_untested_line_is_refused_before_the_read_back(monkeypatch, tmp_path) -> None:
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, probe_stdout="0.2.0")
    with pytest.raises(AcpToolGateUnroutable, match="launch refused") as excinfo:
        rig.run()
    assert "reports 0.2" in str(excinfo.value) and "@mimo-ai/cli@0.1" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert not {"wrap_readback", "verify"} & set(rig.names())
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"]]
    assert rig.client._unasked_shell_tripwire is False


# ── A deny rule the read-back finds out of force


#: A seeded deny kept in force, and the same deny outranked by an agent rule for that
#: tool id (appended after the top-level rules, so it wins).
_DENY_HELD = '{"permission": {"*": "ask", "docs_search": "deny"}}'
_DENY_LOST = (
    '{"permission": {"*": "ask", "docs_search": "deny"}, '
    '"agent": {"build": {"permission": {"docs_search": "ask"}}}%s}'
)


def _reprojecting_rig(monkeypatch, tmp_path, readback: str, unhonoured: frozenset[str]):
    """Projects the ``docs_search`` deny; fake re-projection records *unhonoured*."""
    rig = _MimoCodeLaunchRig(monkeypatch, tmp_path, readback_stdout=readback)
    rig.client._session_harness_deny_rules = ("docs_search",)

    def _resolve_session_mcp_servers():
        rig.record("reproject", rig.client._mimocode_denies_in_force)
        rig.client._session_mcp_unhonoured = unhonoured
        return [{"name": "kept"}]

    monkeypatch.setattr(rig.client, "_resolve_session_mcp_servers", _resolve_session_mcp_servers)
    return rig


def test_a_deny_the_read_back_finds_out_of_force_re_projects_the_session(
    monkeypatch, tmp_path, caplog
) -> None:
    """The lost rule's server is withheld whole; re-projected off the loop, held rules only."""
    rig = _reprojecting_rig(monkeypatch, tmp_path, _DENY_LOST % "", frozenset({"docs"}))
    with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
        rig.run()
    _name, _thread, (_argv, seed) = rig.event("verify")
    assert seed["permission"] == {"*": "ask", "docs_search": "deny"}
    assert rig.client._mimocode_denies_unenforced == frozenset({"docs_search"})
    # Re-projection sees that no seeded deny held.
    _name, reproject_thread, (in_force_seen,) = rig.event("reproject")
    assert in_force_seen == frozenset()
    assert reproject_thread != rig.loop_thread, "the re-projection read the spec on the loop"
    assert rig.names().index("reclaim_readback") < rig.names().index("reproject")
    assert rig.client._session_mcp_cache == [{"name": "kept"}]
    assert rig.client._session_mcp_unhonoured == frozenset({"docs"})
    assert "1 per-tool deny rule(s) are not in force" in caplog.text
    assert "docs_search" in caplog.text
    assert rig.client._unasked_shell_tripwire is True


def test_a_deny_the_read_back_finds_in_force_projects_nothing_again(monkeypatch, tmp_path) -> None:
    rig = _reprojecting_rig(monkeypatch, tmp_path, _DENY_HELD, frozenset({"docs"}))
    rig.run()
    assert rig.client._mimocode_denies_in_force == frozenset({"docs_search"})
    assert "reproject" not in rig.names()
    assert rig.client._session_mcp_cache is None
    assert rig.client._unasked_shell_tripwire is True


def test_a_re_projection_that_withholds_a_natively_mounted_server_refuses(
    monkeypatch, tmp_path
) -> None:
    """Re-projection can withhold a server the read-back never judged; a native mount
    keeps its tools, so refusal follows the re-projection."""
    readback = _DENY_LOST % ', "mcp": {"other": {}}'
    rig = _reprojecting_rig(monkeypatch, tmp_path, readback, frozenset({"docs", "other"}))
    with pytest.raises(AcpToolGateUnroutable, match="MiMo Code mounts that server") as excinfo:
        rig.run()
    assert "'other'" in str(excinfo.value) and "'docs'" not in str(excinfo.value)
    assert "stop mounting the server in" in str(excinfo.value)
    assert ".claude.json" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    rig.event("reproject")
    assert rig.client._mimocode_native_mounts == frozenset({"other"})
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


def test_a_native_mount_of_a_server_held_at_the_read_back_refuses_there(
    monkeypatch, tmp_path
) -> None:
    """A native mount whose tool the lost rule names: read-back refuses, no re-projection."""
    readback = _DENY_LOST % ', "mcp": {"docs": {}}'
    rig = _reprojecting_rig(monkeypatch, tmp_path, readback, frozenset({"docs"}))
    with pytest.raises(AcpToolGateUnroutable, match="MiMo Code mounts that server") as excinfo:
        rig.run()
    assert "'docs'" in str(excinfo.value)
    _assert_raised_without_context(excinfo)
    assert "reproject" not in rig.names()
    assert rig.reclaimed() == [rig.LAUNCHERS["wrap_version"], rig.LAUNCHERS["wrap_readback"]]
    assert rig.client._unasked_shell_tripwire is False


# ── Answers, names and the mirror


def test_an_always_answer_is_sent_as_once_on_a_watched_session(monkeypatch, tmp_path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    client._unasked_shell_tripwire = True
    client._permission_options[9] = {"once": "once", "always": "always", "reject": "reject"}
    client._permission_gate_events = {9: object()}
    monkeypatch.setattr("kiro_crew.permission_floor.refusal_for", lambda event: None)
    sent: list = []

    async def _send(request_id, result, **kwargs):
        sent.append(result["outcome"]["optionId"])

    monkeypatch.setattr(client, "_send_response", _send)
    assert asyncio.run(client.approve_tool(9, always=True)) is True
    assert sent == ["once"]


@pytest.mark.parametrize(
    ("title", "servers", "expected"),
    [
        # Captured title: the known server's split, then every "_" split.
        (
            "crew-probe_crew_probe_echo",
            ("crew-probe",),
            "mimocode#@crew-probe/crew_probe_echo|@crew-probe_crew/probe_echo"
            "|@crew-probe_crew_probe/echo",
        ),
        # A rewritten name (``.`` -> ``_``) returns as Crew's name if the read-back or
        # the array named it.
        (
            "docs_server_search",
            ("docs.server",),
            "mimocode#@docs.server/search|@docs/server_search|@docs_server/search",
        ),
        # Unknown: split only at "_"; the dotted name is lost.
        ("docs_server_search", (), "mimocode#@docs/server_search|@docs_server/search"),
    ],
)
def test_a_fused_mcp_title_is_split_back_to_the_server(title, servers, expected) -> None:
    assert qualified_harness_tool_id(ACP_BACKEND_MIMOCODE, title, servers) == expected


def test_a_rewritten_server_name_meets_a_matcher_written_with_it() -> None:
    qualified = qualified_harness_tool_id(
        ACP_BACKEND_MIMOCODE, "docs_server_search", ("docs.server",)
    )
    assert harness_tool_match_names(qualified) == (
        "@docs.server/search",
        "mcp__docs.server__search",
        "@docs/server_search",
        "mcp__docs__server_search",
        "@docs_server/search",
        "mcp__docs_server__search",
        "docs_server_search",
    )


#: MiMo Code 0.1.15 ids, per ``Tool.define(<id>, ...)`` in ``tool/*.ts``: bash, read,
#: view-image, write, edit, apply_patch, multiedit, notebook-edit, grep, glob, webfetch,
#: websearch/index; actor.ts uses ``const id = "actor"``.
_MIMOCODE_TOOL_ROWS = {
    "execute_bash": ("bash",),
    "fs_read": ("read", "view_image"),
    "fs_write": ("write", "edit", "apply_patch", "multiedit", "notebook_edit"),
    "grep": ("grep",),
    "glob": ("glob",),
    "web_fetch": ("webfetch",),
    "web_search": ("websearch",),
    "use_subagent": ("actor",),
}


def test_the_tool_table_is_the_harness_s_own_ids() -> None:
    from kiro_crew.acp.harness_tool_names import HARNESS_TOOL_TABLES, MIMOCODE_TOOL_IDS_BY_KIRO_TOOL

    assert MIMOCODE_TOOL_IDS_BY_KIRO_TOOL == _MIMOCODE_TOOL_ROWS
    assert HARNESS_TOOL_TABLES[ACP_BACKEND_MIMOCODE] is MIMOCODE_TOOL_IDS_BY_KIRO_TOOL


@pytest.mark.parametrize(
    ("kiro", "tool_id"),
    [(kiro, tool_id) for kiro, ids in sorted(_MIMOCODE_TOOL_ROWS.items()) for tool_id in ids],
)
def test_each_harness_tool_id_is_met_first_by_its_kiro_name(kiro: str, tool_id: str) -> None:
    names = harness_tool_match_names(f"{ACP_BACKEND_MIMOCODE}#{tool_id}")
    # ``grep``/``glob`` match on both sides, so appear once.
    leading = tuple(dict.fromkeys((kiro, tool_id)))
    assert names[: len(leading)] == leading


def test_a_native_mount_of_a_withheld_server_refuses(tmp_path) -> None:
    mimocode = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    mimocode._mimocode_native_mounts = frozenset({"docs"})
    mimocode._session_mcp_unhonoured = frozenset({"docs"})
    with pytest.raises(AcpToolGateUnroutable, match="MiMo Code mounts that server"):
        mimocode_mod.refuse_native_mount_of_unhonoured(mimocode)


def test_the_deny_settlement_writes_mimos_own_field(monkeypatch, tmp_path) -> None:
    _isolate_mimocode_env(monkeypatch)
    mimocode = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_MIMOCODE)
    mimocode._session_harness_deny_rules = ("srv_a", "srv_b")
    mimocode._mimocode_denies_unenforced = frozenset({"srv_b"})
    seed = mimocode_mod._mimocode_routing_config(ACP_BACKEND_MIMOCODE, ("srv_a", "srv_b"))
    assert mimocode_mod.settle_mimocode_denies(mimocode, seed) == frozenset({"srv_b"})
    assert mimocode._mimocode_denies_in_force == frozenset({"srv_a"})
    assert mimocode._opencode_denies_in_force is None


def test_the_projection_names_mimocode_in_its_warnings(monkeypatch, caplog) -> None:
    """A withheld-server warning names MiMo Code's config."""
    monkeypatch.setattr(
        mimocode_mirror_mod,
        "session_mcp_projection",
        lambda agent, **kw: _projection_with_disabled_tool(),
    )
    with caplog.at_level("WARNING"):
        mimocode_mirror_mod.mimocode_projection("kirocrew", denies_in_force=frozenset())
    assert "mimocode session MCP" in caplog.text
    assert "MiMo Code config" in caplog.text
    assert "opencode" not in caplog.text


def _projection_with_disabled_tool():
    from types import SimpleNamespace

    return SimpleNamespace(
        servers=[{"name": "docs", "command": "x", "args": [], "env": []}],
        disabled_tools=frozenset({("docs", "search")}),
        disabled_servers=frozenset(),
        derived_spec_snapshot=None,
        zero_tools=False,
        agent_spec=None,
        allowlist=SimpleNamespace(grants=lambda name: True),
    )


def test_the_hooks_ruling_is_withheld() -> None:
    from kiro_crew.agent_sdk.backends import ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    from kiro_crew.providers.mirrors import Concern, Disposition, mirror_for

    assert ACP_BACKEND_MIMOCODE not in ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS
    rulings = mirror_for(ACP_BACKEND_MIMOCODE).rulings()
    assert rulings[Concern.HOOKS].disposition is Disposition.WITHHELD
    assert "ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS" in rulings[Concern.HOOKS].reason
    assert set(rulings) == set(mirror_for(ACP_BACKEND_OPENCODE).rulings())
    for ruling in rulings.values():
        assert "opencode 1.18" not in ruling.reason


# ── A model value the harness would accept blindly


_ADVERTISED = ["bedrock/probe-model", "xiaomi/mimo-v2.6-pro", "xiaomi/mimo-v2.6-pro/high"]


def _model_client(tmp_path, backend: str = ACP_BACKEND_MIMOCODE) -> tuple[AcpClient, list]:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._available_models = [{"modelId": m, "name": m} for m in _ADVERTISED]
    writes: list = []

    async def _set(config_id, value):
        writes.append((config_id, value))

    client.set_config_option = _set  # type: ignore[method-assign]
    return client, writes


def test_the_backend_is_marked_as_taking_any_model_value() -> None:
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKENDS_MODEL_SELECT_UNCHECKED,
        ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION,
    )

    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_MODEL_SELECT_UNCHECKED
    assert ACP_BACKENDS_MODEL_SELECT_UNCHECKED <= ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_OPENCODE not in ACP_BACKENDS_MODEL_SELECT_UNCHECKED


def test_an_inherited_pin_the_select_does_not_carry_is_never_written(tmp_path) -> None:
    """Live: shared spec's ``claude-opus-5.5`` was accepted; every prompt came back empty."""
    client, writes = _model_client(tmp_path)
    assert asyncio.run(client._push_model_config_option("claude-opus-5.5", strict=False)) == ""
    assert writes == []


def test_an_explicit_pick_the_select_does_not_carry_is_refused(tmp_path) -> None:
    from kiro_crew.acp.transport_errors import AcpModelUnavailable

    client, writes = _model_client(tmp_path)
    with pytest.raises(AcpModelUnavailable):
        asyncio.run(client._push_model_config_option("claude-opus-5.5", strict=True))
    assert writes == []


@pytest.mark.parametrize("model", ["bedrock/probe-model", "xiaomi/mimo-v2.6-pro/high"])
def test_an_advertised_value_is_written_as_advertised(tmp_path, model) -> None:
    client, writes = _model_client(tmp_path)
    assert asyncio.run(client._push_model_config_option(model, strict=False)) == model
    assert writes == [("model", model)]


def test_a_cold_cache_keeps_the_ladder(tmp_path) -> None:
    client, writes = _model_client(tmp_path)
    client._available_models = []
    assert asyncio.run(client._push_model_config_option("bedrock/probe-model", strict=False))
    assert writes == [("model", "bedrock/probe-model")]


def test_opencode_still_lets_its_harness_judge(tmp_path) -> None:
    client, writes = _model_client(tmp_path, backend=ACP_BACKEND_OPENCODE)
    asyncio.run(client._push_model_config_option("claude-opus-5.5", strict=False))
    assert writes and writes[0] == ("model", "claude-opus-5.5")


# ── acp harness tool params
# Permission-event argument spellings (``acp.harness_tool_names``); edit gates read one
# set. MiMo's ``notebook_edit`` target is ``notebook_path``, ``apply_patch``'s envelope
# ``patch_text``; only mimocode events gain the canonical name. Refused without it:
# ``fixtures/acp_frames/mimocode/notebook-edit-live.jsonl``.

_FIXTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "mimocode" / "notebook-edit-live.jsonl"
)
_WRITE_ONLY = "~/.kiro/crew/config.json"
_PATCH = "*** Begin Patch\n*** Add File: /tmp/a\n+x\n*** End Patch"


def _frames() -> list[dict]:
    lines = _FIXTURE.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines[1:] if line.strip()]


def _notebook_event(backend: str, target: str | None = None):
    """The captured notebook_edit permission event for *backend*."""
    raw: dict = {}
    names: dict = {}
    events = []
    for frame in copy.deepcopy(_frames()):
        if frame.get("method") != "session/request_permission":
            update = frame["params"]["update"]
            if target and isinstance(update.get("rawInput"), dict):
                if "notebook_path" in update["rawInput"]:
                    update["rawInput"]["notebook_path"] = target
            _dispatch.parse_session_update(
                update, harness_tool_name_cache=names, raw_params_cache=raw
            )
            continue
        msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=frame["params"])
        event, _ = _dispatch.build_permission_event(
            msg, raw_params_cache=raw, harness_tool_name_cache=names, harness_backend=backend
        )
        events.append(event)
    return events[-1]


def _judge(event):
    return HookManager(HooksConfig.from_dict({})).judge(
        ToolCall.from_event(event), session_key="cli_chat"
    )


class TestCanonicalToolParams:
    def test_the_canonical_spelling_is_added_beside_the_harness_s_own(self) -> None:
        params = {"notebook_path": "/w/nb.ipynb", "cell_id": "c1"}
        out = canonical_tool_params(ACP_BACKEND_MIMOCODE, params)
        assert out == {"notebook_path": "/w/nb.ipynb", "cell_id": "c1", "file_path": "/w/nb.ipynb"}
        assert params == {"notebook_path": "/w/nb.ipynb", "cell_id": "c1"}

    @pytest.mark.parametrize("backend", ["", ACP_BACKEND_OPENCODE, "claude", "goose"])
    def test_a_backend_with_no_row_is_handed_back_its_own_params(self, backend: str) -> None:
        params = {"notebook_path": "/w/nb.ipynb", "patch_text": _PATCH}
        assert backend not in HARNESS_PARAM_ALIASES
        assert canonical_tool_params(backend, params) is params

    @pytest.mark.parametrize("params", [None, [], "notebook_path"])
    def test_params_that_are_not_a_dict_come_back_as_they_are(self, params: object) -> None:
        assert canonical_tool_params(ACP_BACKEND_MIMOCODE, params) is params  # type: ignore[arg-type]

    def test_a_different_value_under_the_canonical_key_is_judged_too(self) -> None:
        out = canonical_tool_params(
            ACP_BACKEND_MIMOCODE, {"notebook_path": "/a", "file_path": "/b"}
        )
        assert out is not None and out["file_path"] == ["/b", "/a"]
        assert sorted(edit_target_candidates(out)) == ["/a", "/b"]

    def test_the_same_value_under_both_names_stays_one_value(self) -> None:
        out = canonical_tool_params(
            ACP_BACKEND_MIMOCODE, {"notebook_path": "/a", "file_path": "/a"}
        )
        assert out is not None and out["file_path"] == "/a"

    def test_mimocode_s_patch_envelope_is_judged_and_opencode_s_reader_is_unchanged(self) -> None:
        params = {"patch_text": _PATCH}
        mimocode = canonical_tool_params(ACP_BACKEND_MIMOCODE, params)
        assert list(edit_target_candidates(mimocode)) == ["/tmp/a"]
        assert (
            list(edit_target_candidates(canonical_tool_params(ACP_BACKEND_OPENCODE, params))) == []
        )

    def test_two_different_patch_envelopes_are_unverifiable(self) -> None:
        other = "*** Begin Patch\n*** Add File: /tmp/b\n+x\n*** End Patch"
        out = canonical_tool_params(
            ACP_BACKEND_MIMOCODE, {"patch_text": _PATCH, "patchText": other}
        )
        assert edit_target_candidates(out).truncated is True


class TestTheLiveNotebookEditCapture:
    def test_the_capture_is_a_notebook_edit_asked_as_an_edit(self) -> None:
        event = _notebook_event(ACP_BACKEND_MIMOCODE)
        assert (event.title, event.tool_kind) == ("edit", "edit")
        assert event.raw_tool_params["notebook_path"] == "~/mimo-proj/nb.ipynb"
        assert event.raw_tool_params["file_path"] == "~/mimo-proj/nb.ipynb"
        assert list(edit_target_candidates(event.raw_tool_params, event.diff_path)) == [
            "~/mimo-proj/nb.ipynb"
        ]
        assert _judge(event).action != TOOL_DENY

    def test_without_the_row_the_same_call_names_no_target(self) -> None:
        event = _notebook_event(ACP_BACKEND_OPENCODE)
        assert "file_path" not in event.raw_tool_params
        decision = _judge(event)
        assert decision.action == TOOL_DENY
        assert "names no target path" in decision.reason

    def test_a_protected_notebook_target_is_refused(self) -> None:
        event = _notebook_event(ACP_BACKEND_MIMOCODE, target=_WRITE_ONLY)
        decision = _judge(event)
        assert decision.action == TOOL_DENY
        assert "config.json" in decision.reason


# ── Shared surfaces the backend joins
# mimocode cases of shared harness-enumerating tests, same entry points; shared files
# stay as on main.

#: ``agent.env`` must refuse: config location, inline config, asking-posture switches,
#: credential relocation, inline credential store, a later release's name.
_MIMOCODE_AGENT_ENV_NAMES = (
    "MIMOCODE_CONFIG",
    "MIMOCODE_CONFIG_DIR",
    "MIMOCODE_CONFIG_CONTENT",
    "MIMOCODE_PERMISSION",
    "MIMOCODE_DANGEROUSLY_SKIP_PERMISSIONS",
    "MIMOCODE_AUTO_APPROVE_DELETE",
    "MIMOCODE_HOME",
    "MIMOCODE_AUTH_CONTENT",
    "MIMOCODE_A_FUTURE_RELEASE_NAME",
)

_AGENT_ENV = {"HTTPS_PROXY": "http://proxy.example:3128", "NO_PROXY": "localhost,127.0.0.1"}


@pytest.mark.parametrize("name", _MIMOCODE_AGENT_ENV_NAMES)
def test_agent_env_drops_a_mimocode_name(name: str) -> None:
    # In agent.env each would reach every agent child, approved ones too.
    assert agent_env_refusal(name) is not None, name
    assert agent_env_refusal(name.lower()) is not None, name
    assert coerce_agent_env({name: "v", "AWS_PROFILE": "dev"}) == {"AWS_PROFILE": "dev"}


def test_every_variable_the_launch_hands_or_strips_is_refused_in_agent_env() -> None:
    # Routing variable, unseen switches, relocated home: agent.env may set none.
    names = (
        mimocode_mod._ENV_MIMOCODE_CONFIG_CONTENT,
        *mimocode_mod._MIMOCODE_STRIPPED_ENV,
        *(name for name, _value in mimocode_mod._MIMOCODE_PINNED_ENV),
        *(name for name, _why in mimocode_mod._MIMOCODE_REFUSED_ENV),
    )
    for name in names:
        assert name.startswith(sections_mod.AGENT_ENV_DENIED_PREFIXES), name
        assert agent_env_refusal(name) is not None, name


class _StopReadback(Exception):
    """Raised from the env resolver once the read-back env is seen."""


def test_the_read_back_sees_agent_env_like_the_spawn(tmp_path, monkeypatch) -> None:
    """Read-back finds config via env: ``agent.env`` over gateway env, under session overlay."""
    seen: dict = {}

    def _resolve(env, *, kiro_api_key):
        seen.update(env)
        raise _StopReadback

    monkeypatch.setattr(client_mod, "_resolve_spawn_env", _resolve)
    monkeypatch.setattr(client_mod, "agent_env_overlay", lambda: dict(_AGENT_ENV))
    monkeypatch.setenv("HTTPS_PROXY", "http://gateway:1")
    client = AcpClient(
        work_dir=tmp_path,
        acp_backend=ACP_BACKEND_MIMOCODE,
        extra_env={"NO_PROXY": "session-wins"},
    )
    with pytest.raises(_StopReadback):
        mimocode_mod._verify_mimocode_routing(
            client, ACP_BACKEND_MIMOCODE, ["/opt/bin/harness"], "{}"
        )
    assert seen["HTTPS_PROXY"] == _AGENT_ENV["HTTPS_PROXY"]
    assert seen["NO_PROXY"] == "session-wins"


def test_the_spawn_carries_the_agent_env_its_read_back_checked(tmp_path, monkeypatch) -> None:
    """A config save between read-back and launch does not reach the child."""
    monkeypatch.setitem(capture_mod._FIXED_PARENT_ENV, "HTTPS_PROXY", "http://gateway:1")
    readings = iter([dict(_AGENT_ENV), {"HTTPS_PROXY": "http://changed:9"}])

    def read_back(session, *_args, **_kwargs):
        session._agent_env_for_spawn()
        return ("", "")

    answers = capture_mod.capture(
        ACP_BACKEND_MIMOCODE,
        tmp_path,
        extra_patches=(
            patch.object(client_mod, "agent_env_overlay", new=lambda: next(readings)),
            patch.object(client_mod, "scrub_agent_subprocess_env", new=scrub_agent_subprocess_env),
            patch.object(mimocode_mod, "_verify_mimocode_routing", new=read_back),
        ),
    )
    assert answers["env_added"].get("HTTPS_PROXY") == _AGENT_ENV["HTTPS_PROXY"]


#: OpenCode's XDG layout under MiMo's own dir: ``XDG_DATA_HOME`` replaces
#: ``.local/share``, leaving two segments.
_TOKEN_LEAF = ".local/share/mimocode/auth.json"


def test_the_token_leaf_is_fenced_at_home_and_under_its_override(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for var in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "CLAUDE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    security._home_targets_cache.clear()
    try:
        assert _TOKEN_LEAF in security._SENSITIVE_HOME_DIRS
        default = os.path.join(str(home), *_TOKEN_LEAF.split("/"))
        assert security.is_sensitive_path(default) is True
        override = tmp_path / "override-xdg_data_home"
        override.mkdir()
        monkeypatch.setenv("XDG_DATA_HOME", str(override))
        security._home_targets_cache.clear()
        # The gate follows the moved token; the default stays fenced.
        assert security.is_sensitive_path(str(override / "mimocode" / "auth.json")) is True
        assert security.is_sensitive_path(default) is True
    finally:
        security._home_targets_cache.clear()
    pairing = {leaf: under for leaf, _envs, under in host_auth.override_anchored_leaves()}
    assert pairing[_TOKEN_LEAF] == "mimocode/auth.json"


def test_every_harness_owned_credential_leaf_is_fenced_standalone() -> None:
    """Without ``kiro_crew.security`` the aws-control local list is the floor; a
    ``host_auth`` credential absent there is bundled standalone."""
    missing = [
        leaf
        for decl in host_auth.AGENT_AUTH_DECLARATIONS
        for leaf in decl.adapter_own_leaves
        if not aws_control_sensitive._looks_sensitive_standalone(f"/srv/crew/{leaf}")
    ]
    assert not missing, f"harness credential leaves the standalone fence lets through: {missing}"


def test_the_tool_gate_enforces_the_backend() -> None:
    assert acp_tool_gate.is_enforced(ACP_BACKEND_MIMOCODE) is True


def test_the_backend_can_serve_a_manual_compact() -> None:
    """``used`` 48430 -> 34571 over ``/compact`` in
    ``test/fixtures/acp_frames/mimocode/compact-live.jsonl``. Needs ~2.5k-token turns:
    with a ~28.7k fixed prompt, one-word turns are shorter than the summary."""
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_COMPACT


def test_the_compact_finishes_inside_the_prompt_turn() -> None:
    """``/compact`` streams the summary and ends with ``end_turn``, no status frame."""
    assert ACP_BACKEND_MIMOCODE in ACP_BACKENDS_INLINE_COMPACTION
    assert capabilities_for(ACP_BACKEND_MIMOCODE).compacts_inline is True
    provider = AcpProvider(acp_backend=ACP_BACKEND_MIMOCODE)
    assert messaging_commands.compact_unsupported_backend(provider) is None


def test_the_knowledge_pool_degrades_the_backend_to_kiro() -> None:
    # Mirrored, enforced, spawnable like opencode; some native tools never ask, so the
    # zero-tool refusal misses them.
    assert _get_acp_backend({"agent": {"acp_backend": ACP_BACKEND_MIMOCODE}}) == ACP_BACKEND_KIRO


def test_the_orphan_browser_sweep_keeps_a_daemon_a_mimocode_process_may_own(
    tmp_path, monkeypatch
) -> None:
    # npm ``mimo`` execs ``.mimocode``: a plausible owner even with unreadable env, kept.
    import test_pid_lifecycle_browser_daemon as pid_tests

    from kiro_crew import session_pid as sp

    fake = pid_tests.TestBrowserSessionOwnerFakeProc()
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    fake._daemon(proc_root)
    fake._process(
        proc_root,
        901,
        sid=1,
        cmdline=b"/usr/bin/.mimocode\x00chat\x00",
        environ=None,
        cgroup="0::/agent.slice\n",
    )
    fake._linux_fixture(monkeypatch, sp, proc_root)
    assert not sp._is_sweepable_orphan_browser_daemon(
        900, pid_tests._DAEMON_CMDLINE, 900.0, proc_root=proc_root
    )


def _independent_setup(tmp_path, monkeypatch, **kwargs):
    """The shared first-run setup class's backend-less host, for mimocode."""
    import test_kiro_prerequisite_readiness_state as readiness

    rig = readiness.TestUnsandboxedExecRecordsIndependentSetup()
    return rig._service(tmp_path, monkeypatch, selectable=(ACP_BACKEND_MIMOCODE,), **kwargs)


@pytest.mark.asyncio
async def test_first_run_setup_records_when_the_credential_mask_applies(
    tmp_path, monkeypatch
) -> None:
    # No kiro-cli: an installed binary where the mask applies completes setup alone.
    from kiro_crew import sandbox

    service = _independent_setup(tmp_path, monkeypatch, unsandboxed_permitted=False)
    monkeypatch.setattr(sandbox, "detect_backend", lambda: "namespace")
    assert await service.record_independent_backend_setup(ACP_BACKEND_MIMOCODE) is True
    assert service.initial_setup_complete is True
    assert service._setup_marker.exists()


@pytest.mark.asyncio
async def test_first_run_setup_is_refused_when_the_credential_mask_cannot_apply(
    tmp_path, monkeypatch
) -> None:
    # Enforced like codex and opencode: no mask, no setup.
    service = _independent_setup(
        tmp_path, monkeypatch, unsandboxed_permitted=True, mask_applies=False
    )
    assert await service.record_independent_backend_setup(ACP_BACKEND_MIMOCODE) is False
    assert not service._setup_marker.exists()
