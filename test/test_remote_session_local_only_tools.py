"""A remote session never gets a tool that acts on the gateway machine.

Its engine runs here, so built-in file/shell tools, set_project, file_send, the
browser panel, pods and computer use are unmounted and refused in band.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import replace

import pytest

from kiro_crew import hooks, mcp_core
from kiro_crew import session_environment as se
from kiro_crew.acp._dispatch_parts.permission_requests import build_permission_event
from kiro_crew.acp.kas_agents import to_client_custom_agent
from kiro_crew.acp.kas_permissions import _mcp_pattern
from kiro_crew.acp.runtime import (
    _WORKSPACE_TOOLS_BY_BUILTIN,
    AcpRuntime,
    AcpRuntimeError,
    _without_local_only_grants,
)
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.coder.session_launch import SessionEnvironmentChoices
from kiro_crew.computer_use import tools as computer_tools
from kiro_crew.config import paths
from kiro_crew.hook_runtime.tool_identity import ToolCall
from kiro_crew.mcp_tool_titles import _table
from kiro_crew.mcp_tools import artifacts
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.governance import may_skip_gate, parse_policy


@pytest.fixture(autouse=True)
def _developer_flag_on(_floor_monkeypatch):
    # Session Environments are reachable only with the developer flag.
    _floor_monkeypatch.setenv("KIROCREW_DEV_MODE", "1")


WORKSPACE = {"name": "workspace", "command": "bridge"}


def _runtime() -> AcpRuntime:
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = {"server": WORKSPACE, "workdir": "/workspace/app"}
    return runtime


def _servers(*names: str) -> list[dict]:
    return [{"name": n, "command": n} for n in names]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "data_home", lambda: tmp_path)
    return tmp_path


def _bind(home, key: str, profile: str | None = "dev") -> None:
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").set(key, profile)


def test_a_whole_core_grant_becomes_every_core_tool_except_the_local_only_ones():
    grants = _without_local_only_grants(["@kirocrew-core", "@kirocrew-cron"])
    core = {g.split("/", 1)[1] for g in grants if g.startswith("@kirocrew-core/")}
    assert core == set(_table()["kirocrew-core"]) - se.LOCAL_ONLY_CORE_TOOLS
    assert core and not core & se.LOCAL_ONLY_CORE_TOOLS
    # A cron job runs on the gateway machine, so the cron server is local-only too.
    assert not any(g.startswith("@kirocrew-cron") for g in grants)


def test_explicit_and_wildcard_grants_cannot_readmit_a_local_only_tool():
    grants = _without_local_only_grants(
        [
            "@kirocrew-core/file_send",
            "@kirocrew-core/memory_recall",
            "@kirocrew-core/b*",
            "@kirocrew-core/pod_*",
            "@kirocrew-computer",
            "@kirocrew-computer/computer_click",
        ]
    )
    assert "@kirocrew-core/memory_recall" in grants
    assert not any(g.endswith(("/file_send", "/browser", "/pod_up", "/pod_down")) for g in grants)
    assert "@kirocrew-core/pod_ls" in grants  # read-only: kept
    assert not any("*" in g for g in grants)
    assert not any(g.startswith("@kirocrew-computer") for g in grants)


def test_agent_tools_drop_builtins_builtin_group_and_local_only_tools():
    runtime = _runtime()
    servers = _servers("kirocrew-core", "kirocrew-computer", "kirocrew-cron")
    wire = runtime._remote_session_servers(servers)
    assert [s["name"] for s in wire] == ["kirocrew-core", "workspace"]
    [star] = runtime._remote_workspace_agents([{"name": "a", "tools": ["*"]}], wire)
    [listed] = runtime._remote_workspace_agents(
        [
            {
                "name": "b",
                "tools": ["read", "shell", "@builtin", "@kirocrew-core", "@kirocrew-computer"],
            }
        ],
        wire,
    )
    for agent in (star, listed):
        tools = agent["tools"]
        assert "@workspace" in tools
        assert not {"read", "shell", "@builtin", "@kirocrew-core"} & set(tools)
        assert "@kirocrew-core/set_project" not in tools
        assert "@kirocrew-core/spawn_run" in tools
        assert not any(t.startswith("@kirocrew-computer") for t in tools)


def test_an_agent_block_local_only_server_is_withheld_and_never_awaited():
    # A managed server the session-level hoist left in the active agent's own block
    # would mount on this machine, and the readiness barrier would wait for it.
    from kiro_crew.acp.mcp_session_report import required_managed_servers

    runtime = _runtime()
    wire = runtime._remote_session_servers(_servers("kirocrew-core", "kirocrew-cron"))
    block = {"kirocrew-cron": {"command": "x"}, "kirocrew-computer": {"command": "y"}}
    [agent] = runtime._remote_workspace_agents(
        [{"id": "a", "name": "a", "tools": ["*"], "mcpServers": dict(block)}], wire
    )
    assert agent["mcpServers"] == {}
    params = {"mcpServers": wire, "_meta": {"kiro": {"customAgents": [agent]}}}
    required = required_managed_servers(params, "a")
    assert "kirocrew-cron" not in required and "kirocrew-computer" not in required
    assert "kirocrew-core" in required


def test_a_local_session_is_untouched():
    runtime = object.__new__(AcpRuntime)
    runtime._remote_workspace = None
    servers = _servers("kirocrew-core", "kirocrew-computer")
    agents = [{"name": "a", "tools": ["*"]}]
    assert runtime._remote_session_servers(servers) is servers
    assert runtime._remote_workspace_agents(agents, servers) is agents


def test_refusal_follows_the_choice_record(home):
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None
    _bind(home, "dashboard:a")
    refusal = se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send")
    assert refusal and "remote workspace" in refusal
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "memory_recall") is None
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-computer", "computer_click")
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-cron", "cron_add")
    # A subagent that inherited the environment is remote too.
    SessionEnvironmentChoices(home / "coder" / "session-choices.json").inherit(
        "subagent:x", "dashboard:a"
    )
    assert se.refuse_local_only_tool("subagent:x", "kirocrew-core", "browser")
    _bind(home, "dashboard:a", None)
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "file_send") is None


def test_a_known_placement_answers_for_its_own_session_only(home, monkeypatch):
    _bind(home, "dashboard:b")

    def _no_store_read():
        raise AssertionError("the choices store was read")

    real = se._choices
    monkeypatch.setattr(se, "_choices", _no_store_read)
    with se.known_placement("dashboard:a", True):
        assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "set_project")
    with se.known_placement("dashboard:a", False):
        assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "set_project") is None
    # Another session's question still goes to the store.
    monkeypatch.setattr(se, "_choices", real)
    with se.known_placement("dashboard:a", False):
        assert se.refuse_local_only_tool("dashboard:b", "kirocrew-core", "set_project")


@pytest.mark.asyncio
async def test_directive_replay_reads_placement_off_the_gateway_loop(home, monkeypatch):
    # GPT 6.1 on 359b3eaa06: the gateway replays set_project on its event loop, and
    # the remote-workspace guard read the choices store there, blocking every task.
    import threading
    from unittest.mock import AsyncMock, MagicMock

    from aiohttp import web

    from kiro_crew.dashboard import directive_queue
    from kiro_crew.dashboard.handlers.sessions import api_session_directive

    _bind(home, "dashboard:a")
    loop_thread = threading.get_ident()
    readers: list[int] = []
    real = se._choices

    def _spy():
        readers.append(threading.get_ident())
        return real()

    monkeypatch.setattr(se, "_choices", _spy)
    req = MagicMock(spec=web.Request)
    req.headers = {"X-Session-Key": "dashboard:a"}
    req.can_read_body = True
    req.json = AsyncMock(return_value={"tool": "set_project", "raw_args": {"path": str(home)}})
    req.app = {"state": MagicMock()}
    req.get = {"internal_auth": True, "peer_verified": True}.get
    resp = await api_session_directive(req)
    # Remote: the guard refuses, so nothing is derived or parked...
    assert resp.status == 400
    assert directive_queue.depth("dashboard:a") == 0
    # ...and the store was read, but never on the loop's thread.
    assert readers and loop_thread not in readers


def test_an_unreadable_choice_record_refuses(home):
    _bind(home, "dashboard:a")
    path = home / "coder" / "session-choices.json"
    path.write_text("{broken", encoding="utf-8")
    assert se.refuse_local_only_tool("dashboard:a", "kirocrew-core", "set_project")
    # A session that was never bound keeps its local tools.
    assert se.refuse_local_only_tool("dashboard:b", "kirocrew-core", "set_project") is None
    assert se.refuse_local_only_tool("", "kirocrew-core", "set_project") is None


def test_core_dispatch_refuses_before_the_handler_runs(home, monkeypatch):
    _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:a")
    ran: list[str] = []
    monkeypatch.setattr(mcp_core, "dispatch", lambda name, args: ran.append(name) or "ran")
    monkeypatch.setattr(mcp_core, "_deny_channel_agent_dispatch", lambda name, args: None)
    for tool in sorted(se.LOCAL_ONLY_CORE_TOOLS):
        assert "remote workspace" in mcp_core._call_tool_inner(tool, {})
    # A task spec is a path on this machine; the runner gets local tools.
    assert "remote workspace" in mcp_core._call_tool_inner("task_run", {"spec": "/srv/t.md"})
    assert ran == []
    assert mcp_core._call_tool_inner("memory_recall", {}) == "ran"


@pytest.mark.parametrize("bound", [True, False])
def test_cron_refuses_in_a_remote_session_before_any_routing(home, monkeypatch, bound):
    # A cron job's command, script or agent session runs on the gateway machine.
    from kiro_crew import mcp_cron

    if bound:
        _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_cron, "_resolve_session_key", lambda: "dashboard:a")
    posted: list[dict] = []
    monkeypatch.setattr(mcp_core, "_post", lambda path, body: posted.append(body) or {"ok": True})
    out = mcp_cron._call_tool(
        "cron_add", {"name": "nightly", "command": "make test", "every": 3600}
    )
    assert ("remote workspace" in out) is bound
    if bound:
        assert posted == []


@pytest.mark.parametrize("bound", [True, False])
def test_deploy_refuses_a_gateway_directory_only_in_a_remote_session(home, monkeypatch, bound):
    if bound:
        _bind(home, "dashboard:a")
    monkeypatch.setattr(mcp_core, "_resolve_session_key", lambda: "dashboard:a")
    posted: list[dict] = []
    monkeypatch.setattr(mcp_core, "_post", lambda path, body: posted.append(body) or {"ok": True})
    out = artifacts.deploy_artifact("deploy_artifact", {"site_id": "s", "local_dir": "/srv/site"})
    assert ("remote workspace" in out) is bound
    assert bool(posted) is not bound
    # An artifact lives on the gateway on purpose, so its deploy is never refused.
    posted.clear()
    artifacts.deploy_artifact("deploy_artifact", {"site_id": "s", "artifact_slug": "a"})
    assert len(posted) <= 1 and not any("local_dir" in b for b in posted)


def test_computer_use_dispatch_refuses_a_remote_session(home, monkeypatch):
    _bind(home, "dashboard:a")
    name = sorted(computer_tools.MCP_COMPUTER_SCHEMAS)[0]
    out = computer_tools._dispatch(
        name, {}, session_key="dashboard:a", agent="default", app="", approval_recorded=False
    )
    payload = json.loads(out) if out.lstrip().startswith("{") else {"text": out}
    assert "remote workspace" in json.dumps(payload)


@pytest.mark.parametrize(
    "pattern", ["artifact_*_comments", "artifact_get_comment?", "brow?er", "artifact_[gd]*"]
)
def test_core_wildcards_keep_the_complete_requested_pattern(pattern):
    grant = f"@kirocrew-core/{pattern}"
    expected = {
        f"@kirocrew-core/{tool}"
        for tool in set(_table()["kirocrew-core"]) - se.LOCAL_ONLY_CORE_TOOLS
        if _mcp_pattern(grant) is not None and fnmatch.fnmatchcase(tool, pattern)
    }
    assert set(_without_local_only_grants([grant])) == expected
    assert "@kirocrew-core/artifact_delete" not in expected


@pytest.mark.parametrize(
    "excluded,expected",
    [
        ("execute_bash", ["run_command"]),
        ("shell", ["run_command"]),
        ("@builtin/execute_bash", ["run_command"]),
        ("fs_read", ["read_file", "list_directory"]),
        ("read", ["read_file", "list_directory"]),
        ("@builtin/fs_write", ["write_file", "edit_file"]),
        ("write", ["write_file", "edit_file"]),
        ("str_replace", ["edit_file"]),
        ("read_file", ["read_file"]),
        ("list_directory", ["list_directory"]),
        ("file_search", ["glob"]),
        ("run_command", ["run_command"]),
        ("fs_append", ["write_file", "edit_file"]),
        ("delete_file", ["write_file", "edit_file"]),
        ("glob", ["glob"]),
        ("grep", ["grep"]),
        ("@builtin", [""]),
    ],
)
def test_remote_builtin_exclusions_cover_workspace_replacements(excluded, expected):
    spec = {"tools": "*", "excludedTools": [excluded, "@other/tool"]}
    [agent] = _runtime()._remote_workspace_agents([spec], _servers("workspace"))
    translated = [f"@workspace/{tool}" if tool else "@workspace" for tool in expected]
    assert agent["excludedTools"] == [excluded, "@other/tool", *translated]
    assert spec["excludedTools"] == [excluded, "@other/tool"]


@pytest.mark.parametrize(
    "settings",
    [
        {"execute_bash": {"deniedCommands": ["git push.*"]}},
        {"fs_write": {"deniedPaths": ["/etc"]}},
        {"fs_read": {"deniedPaths": ["~/.ssh"]}},
    ],
)
def test_an_agents_own_shell_or_file_denies_refuse_a_remote_session(settings):
    agent = to_client_custom_agent("a", {"tools": "*", "toolsSettings": settings}, "p")
    assert agent["permissions"]["rules"]
    with pytest.raises(AcpRuntimeError, match="cannot enforce"):
        _runtime()._remote_workspace_agents([agent], _servers("workspace"))


@pytest.mark.parametrize(
    "rules",
    [
        [{"capability": "shell", "effect": "ask"}],
        [{"capability": "web_fetch", "match": ["evil.test"], "effect": "deny"}],
        [{"capability": "mcp", "match": ["srv/*"], "effect": "allow"}],
    ],
)
def test_rules_the_workspace_keeps_meaning_do_not_refuse(rules):
    agent = {"tools": "*", "permissions": {"rules": rules}}
    [out] = _runtime()._remote_workspace_agents([agent], _servers("workspace"))
    assert out["permissions"] == {"rules": rules}


@pytest.mark.parametrize("ref", ["@workspace", "@workspace/run_command", "@workspace/r*"])
def test_workspace_auto_approval_cannot_skip_the_shell_floor(ref):
    assert not may_skip_gate(ref, None)
    agent = to_client_custom_agent("a", {"tools": ["@workspace"], "allowedTools": [ref]}, "p")
    assert "permissions" not in agent


def _workspace_call(tool, session_key="chat-remote", kind="read", **params):
    """A workspace call judged the way production judges it: the KAS permission
    frame through ``build_permission_event``, then ``ToolCall.from_event``, then the
    gate. The runtime names the workspace server only when it attached one, which a
    session confirmed remote has and a local session never does."""
    se.remember_remote_workspace("chat-remote", "dev", "/workspace/app")
    try:
        attached = se.session_attached_remotely(session_key)
        event = _permission_event(tool, kind, params, remote=attached)
        call = ToolCall.from_event(event)
        return hooks.HookManager().on_tool_call(
            event.title, session_key=session_key, **call.kwargs()
        )
    finally:
        se.forget_remote_workspace("chat-remote")


def _permission_event(tool, kind="read", params=None, *, remote=True, server="workspace"):
    """The event a KAS ``session/request_permission`` for ``@server/tool`` builds."""
    tool_call_id = "tc-1"
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "title": f"@{server}/{tool}",
                    "kind": kind,
                },
                "options": [{"optionId": "a", "kind": "allow_once"}],
            },
        ),
        raw_params_cache={tool_call_id: dict(params or {})},
        shell_cache={tool_call_id: False},
        mcp_server_name_cache={tool_call_id: server},
        tool_name_cache={tool_call_id: tool},
        remote_workspace_server=se.WORKSPACE_SERVER_NAME if remote else "",
        remote_workspace_root="/workspace/app" if remote else "",
    )
    assert event is not None
    return event


def test_a_local_server_named_workspace_is_not_rekinded_into_a_shell(home):
    def ls(key):
        return _workspace_call("run_command", key, "other", command="ls").action

    # ``ls`` passes the read-only shell tier; a local MCP server gets no such pass.
    assert ls("chat-remote") == hooks.TOOL_AUTO_APPROVE
    assert ls("chat-local") != hooks.TOOL_AUTO_APPROVE
    _bind(home, "chat-bound")  # recorded but not attached in this process: still remote
    assert ls("chat-bound") == hooks.TOOL_AUTO_APPROVE
    (home / "coder" / "session-choices.json").write_text("{", encoding="utf-8")
    assert ls("chat-bound") != hooks.TOOL_AUTO_APPROVE  # unreadable never widens


def _workspace_policy(monkeypatch, **policy):
    ceiling = parse_policy({"version": 1, "boot": {"fail_closed": True}, **policy})
    context = replace(hooks.current_context(), governance=ceiling)
    monkeypatch.setattr(hooks, "current_context", lambda: context)
    monkeypatch.setattr(gp, "resolve_active_scope", lambda *args, **kw: None)


@pytest.mark.parametrize("tool", ["run_command", "write_file", "edit_file", "read_file"])
def test_workspace_scopes_reach_the_gateway_policy(monkeypatch, tool):
    # A guest path, matched as the guest's POSIX path on every gateway OS
    # (platform/guest_paths.py), so this runs on the Windows shards too.
    path = "/workspace/app/private/target"
    _workspace_policy(
        monkeypatch,
        commands={"mode": "deny", "deny": ["rm -rf *"]},
        filesystem={"write": {"mode": "deny", "deny": [path]}},
    )
    allowed = _workspace_call("run_command", command="printf hello")
    assert allowed.action != hooks.TOOL_DENY
    result = _workspace_call(tool, command="rm -rf build", path=path)
    assert (result.action == hooks.TOOL_DENY) == (tool != "read_file")
    if tool != "read_file":
        assert result.tier == "governance"


@pytest.mark.parametrize(
    "builtin, tool",
    [("execute_bash", "run_command"), ("fs_write", "write_file"), ("fs_read", "read_file")],
)
def test_a_deny_on_the_replaced_builtin_binds_the_workspace_tool(
    monkeypatch, tmp_path, builtin, tool
):
    _workspace_policy(monkeypatch, tools={"mode": "deny", "deny": [builtin]})
    result = _workspace_call(tool, command="printf hello", path=str(tmp_path / "f"))
    assert result.action == hooks.TOOL_DENY and result.tier == "governance"
    assert _workspace_call("grep", pattern="x", path=".").action != hooks.TOOL_DENY

    inverse = {t: (b,) for b, tools in _WORKSPACE_TOOLS_BY_BUILTIN.items() for t in tools}
    assert se.WORKSPACE_DENY_ALIASES == inverse


def test_workspace_shell_meets_the_gateway_deny_floor():
    assert _workspace_call("run_command", command="printf hello").action != hooks.TOOL_DENY
    assert _workspace_call("run_command", command=None).tier == "unverifiable-shell"
    result = _workspace_call("run_command", command="rm -rf /")
    assert result.action == hooks.TOOL_DENY
    assert result.tier and result.tier != "governance"


#: Core tools reviewed as safe for a remote session: they act on gateway state on
#: purpose (artifacts, memory, crons, chats) or take content, never a gateway path.
REMOTE_SAFE_CORE_TOOLS = frozenset("""
artifact_delete artifact_delete_comment artifact_folder_create artifact_folder_delete
artifact_folder_list artifact_folder_move artifact_folder_rename artifact_get
artifact_get_comments artifact_list artifact_mark_review artifact_move artifact_post_comment
artifact_reply_comment artifact_revert artifact_save artifact_update artifact_versions
ask_question autonudge_stop chat_tag delete_message deploy_artifact
design_tweak_update_thread get_chat_session issue_radar_crew_read issue_radar_crew_record
issue_radar_record_investigation kiro_cli_logs knowledge_add_document knowledge_dedup
knowledge_list_sources learn_add learn_list learn_remove list_sessions
local_knowledge_search memory_recall monitor_inspect monitor_start monitor_stop
monitor_update monitor_watch nothing_to_do ops_mission_control_api pod_ls pod_status
read_slack_profile register_hook reset_conversation resource_status route_crew
search_chat_history select_crew send_message send_notification session_ledger_read
session_ledger_record skill_discover skill_fetch skill_search spawn_continue spawn_list
spawn_release spawn_run spawn_status spawn_steer spawn_sub_agents suggest_followup
update_message wait workflow_author workflow_cancel workflow_library_list workflow_list
workflow_result workflow_status request_server
""".split())


def test_every_core_tool_is_classified_for_remote_sessions():
    # A new core tool must be judged local-only or remote-safe, never default in.
    core = set(_table()["kirocrew-core"])
    assert not REMOTE_SAFE_CORE_TOOLS & se.LOCAL_ONLY_CORE_TOOLS
    assert core - se.LOCAL_ONLY_CORE_TOOLS - REMOTE_SAFE_CORE_TOOLS == set()
    assert {"task_run", "workflow_run", "workflow_rerun_subtree"} <= se.LOCAL_ONLY_CORE_TOOLS


# ── The builder is where a workspace tool gets its built-in identity ──────────


@pytest.mark.parametrize(
    "tool, kind, builtin",
    [
        ("read_file", "read", "fs_read"),
        ("list_directory", "read", "fs_read"),
        ("glob", "read", "glob"),
        ("grep", "read", "grep"),
        ("write_file", "edit", "fs_write"),
        ("edit_file", "edit", "fs_write"),
        ("run_command", "execute", "execute_bash"),
    ],
)
def test_each_workspace_tool_is_read_as_the_builtin_it_replaces(tool, kind, builtin):
    event = _permission_event(tool, "other", {"command": "make test", "path": "f"})
    assert (event.tool_kind, event.harness_builtin_tool) == (kind, builtin)
    # The server stays named, so no local-only tier reads it as this machine's tool.
    assert event.mcp_server_name == "workspace" and event.tool_name == tool
    if tool == "run_command":
        # Titled with its command, as KAS titles its own shell, so the commands
        # policy reads the command text.
        assert event.is_shell and event.title == "make test"
        assert event.shell_command == "make test"
    else:
        assert not event.is_shell and event.title == f"@workspace/{tool}"


@pytest.mark.parametrize("remote, server", [(False, "workspace"), (True, "other")])
def test_only_an_attached_workspace_server_is_mapped(remote, server):
    event = _permission_event(
        "run_command", "other", {"command": "ls"}, remote=remote, server=server
    )
    assert (event.tool_kind, event.is_shell, event.harness_builtin_tool) == ("other", False, "")
    assert event.title == f"@{server}/run_command"


def test_an_untrusted_identity_is_not_mapped():
    tool_call_id = "tc-1"
    event, _ = build_permission_event(
        JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": tool_call_id, "title": "@workspace/run_command"},
                "options": [{"optionId": "a", "kind": "allow_once"}],
            },
        ),
        raw_params_cache={tool_call_id: {"command": "ls"}},
        remote_workspace_server=se.WORKSPACE_SERVER_NAME,
        remote_workspace_root="/workspace/app",
    )
    assert event is not None and not event.is_shell and not event.harness_builtin_tool


def test_the_session_handle_names_the_workspace_only_when_its_runtime_attached_it():
    from types import SimpleNamespace

    from kiro_crew.acp import session_handle_permissions as shp

    def handle(remote_workspace):
        return SimpleNamespace(
            _tool_call_inputs={},
            _tool_call_input_redacted={},
            _tool_call_is_shell={"s|tc-1": False},
            _tool_call_raw_params={"s|tc-1": {"command": "ls"}},
            _tool_call_diff_path={},
            _tool_kind_cache_for_wire=lambda: None,
            _tool_call_mcp_server={"s|tc-1": "workspace"},
            _tool_call_tool_name={"s|tc-1": "run_command"},
            _tool_call_harness_tool_name={},
            _identity_unreadable_cache_for_wire=lambda: None,
            _session_id="s",
            _runtime=SimpleNamespace(acp_backend="kas", _remote_workspace=remote_workspace),
            _permission_options={},
            _permission_gate_events={},
        )

    msg = JsonRpcMessage(
        id=1,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-1", "title": "@workspace/run_command"},
            "options": [{"optionId": "a", "kind": "allow_once"}],
        },
    )
    attached = {"server": {"name": "workspace"}, "workdir": "/workspace/app"}
    remote = shp._build_permission_event(handle(attached), msg)
    unread = shp._build_permission_event(handle({"server": {"name": "workspace"}}), msg)
    local = shp._build_permission_event(handle(None), msg)
    assert remote.is_shell and remote.title == "ls"
    assert remote.raw_tool_params == {"command": "ls"}
    # An unreadable workdir leaves the tool unmapped, never anchored elsewhere.
    assert not unread.is_shell and unread.title == "@workspace/run_command"
    assert not local.is_shell and local.title == "@workspace/run_command"


def test_the_permission_event_builder_is_the_only_source_of_a_gated_identity():
    """Every gate caller judges a permission event (audit: permission_floor,
    tool_policy, the messaging/Slack/Telegram turn gates, subagents, the task and
    auto-improvement runners), so the builder is the one place a remote workspace
    call can enter the gate. A second constructor would be a path around the mapping."""
    from pathlib import Path

    import kiro_crew

    root = Path(kiro_crew.__file__).parent
    makers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if "/tests/" not in path.as_posix()
        and "kind=EVENT_PERMISSION_REQUEST" in path.read_text(encoding="utf-8")
    )
    assert makers == ["acp/_dispatch_parts/permission_requests.py"]


# ── A relative workspace path is judged where the guest resolves it ───────────


@pytest.mark.parametrize(
    "tool, key, stated, judged",
    [
        ("read_file", "path", "private/record.txt", "/workspace/app/private/record.txt"),
        ("write_file", "path", "./private/../private/x", "/workspace/app/private/x"),
        ("glob", "path", ".", "/workspace/app"),
        ("run_command", "cwd", "sub", "/workspace/app/sub"),
        ("read_file", "path", "/etc/hosts", "/etc/hosts"),
    ],
)
def test_a_workspace_path_is_anchored_at_the_guest_workdir(tool, key, stated, judged):
    event = _permission_event(tool, "other", {key: stated, "command": "ls"})
    assert event.raw_tool_params[key] == judged


def test_a_relative_path_meets_an_absolute_filesystem_deny(monkeypatch):
    """GPT 6.1 on 49519e2b85: with the guest root ``/workspace/app`` and a deny on
    ``/workspace/app/private/**``, ``private/record.txt`` must be refused, not judged
    under the gateway's working directory."""
    _workspace_policy(
        monkeypatch,
        filesystem={
            "read": {"mode": "deny", "deny": ["/workspace/app/private/**"]},
            "write": {"mode": "deny", "deny": ["/workspace/app/private/**"]},
        },
    )
    for tool in ("read_file", "write_file", "edit_file"):
        result = _workspace_call(tool, kind="other", path="private/record.txt")
        assert result.action == hooks.TOOL_DENY and result.tier == "governance", tool
    assert _workspace_call("read_file", kind="other", path="src/a.py").action != hooks.TOOL_DENY


def _deferred(tool, server="workspace", **arguments):
    """A Tool Search deferred call's input: the backend's ``tool_call`` envelope."""
    return {"tool_id": f"{server}::{tool}", "arguments": arguments}


def test_a_deferred_call_is_judged_by_the_arguments_the_guest_receives(monkeypatch):
    """GPT 6.1 on 507390cf02: a deferred ``read_file`` wraps its arguments in
    ``{"tool_id", "arguments"}``; the nested relative path must still meet the deny."""
    _workspace_policy(
        monkeypatch,
        filesystem={"read": {"mode": "deny", "deny": ["/workspace/app/restricted/**"]}},
    )
    se.remember_remote_workspace("chat-remote", "dev", "/workspace/app")
    try:
        event = _permission_event(
            "read_file", "other", _deferred("read_file", path="restricted/report.txt")
        )
        assert event.raw_tool_params == {"path": "/workspace/app/restricted/report.txt"}
        assert not event.mcp_identity_unreadable
        call = ToolCall.from_event(event)
        result = hooks.HookManager().on_tool_call(
            event.title, session_key="chat-remote", **call.kwargs()
        )
        assert result.action == hooks.TOOL_DENY and result.tier == "governance"
    finally:
        se.forget_remote_workspace("chat-remote")


def test_a_deferred_shell_keeps_its_command():
    event = _permission_event("run_command", "other", _deferred("run_command", command="ls -la"))
    assert event.is_shell and event.raw_tool_params["command"] == "ls -la"
    assert event.title == "ls -la"


@pytest.mark.parametrize(
    "envelope",
    [
        _deferred("write_file", path="x"),  # names another workspace tool
        _deferred("read_file", server="other", path="x"),  # names another server
        {**_deferred("read_file", path="x"), "extra": 1},  # carries something else
        {"tool_id": "workspace::read_file", "arguments": "path=x"},  # not an object
    ],
)
def test_an_envelope_for_another_call_is_refused(envelope):
    event = _permission_event("read_file", "other", envelope)
    assert event.mcp_identity_unreadable
    call = ToolCall.from_event(event)
    result = hooks.HookManager().on_tool_call(
        event.title, session_key="chat-remote", **call.kwargs()
    )
    assert result.action == hooks.TOOL_DENY
