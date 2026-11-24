"""The shipped KAS helper holds its parent's MCP servers and is judged as its parent is.

``kirocrew-helper`` runs as the default agent's sub-agent in the engine. It holds
the servers its parent holds, declared exactly as the parent's projected entry
declares them (so the engine hands it the parent's running process), with the
parent's exclusions, and never one of Kiro Crew's own servers. Its permission
requests reach Kiro Crew's gate stamped as its parent's are, so each call gets
the verdict the parent's own call gets. A native child that runs another agent's
spec keeps the limits and no grant.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_agents_mcp_servers
from kiro_crew.acp import kas_engine_subagent_target as target
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_subagent_targets import mounts_subagent_tool
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import ACP_BACKEND_KAS, AcpEvent, JsonRpcMessage
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.platform.bootstrap import build_default_context
from kiro_crew.platform.governance import parse_policy

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = target.KAS_HELPER_AGENT
_KEY = "dashboard:t1"
_HOME = Path.home()
#: Spelled in two pieces so this file's own text is not one destructive command line.
_DESTRUCTIVE = "rm " + "-rf /"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {"kas_subagents": True}
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))
    yield
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


def _spec(**changes: Any) -> dict[str, Any]:
    """The shipped default agent with a user's servers, as Kiro Crew writes them into its file."""
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    spec = {
        "name": "kirocrew",
        "description": "the default agent",
        "prompt": "You are the default agent.",
        "tools": [*shipped["tools"], "@plain", "@with-env", "@one-tool/keep_me", "@pooled"],
        "allowedTools": [*shipped["allowedTools"], "@plain/granted"],
        "mcpServers": {
            "plain": {
                "command": sys.executable,
                "args": ["/srv/plain.py"],
                "disabledTools": ["hidden", "glob_*"],
            },
            "with-env": {
                "command": sys.executable,
                "args": ["/srv/env.py"],
                "env": {"P6_SETTING": "value-kept-on-this-machine"},
            },
            "one-tool": {"command": sys.executable, "args": ["/srv/one.py"], "timeout": 5000},
            "not-mounted": {"command": sys.executable, "args": ["/srv/unlisted.py"]},
            "pooled": {"command": sys.executable, "args": ["/srv/pooled.py"]},
        },
    }
    spec.update(changes)
    return spec


class _Batch(dict):
    """The registered batch by id, with the projection's own record of the helper mount."""

    helper_parent = ""


async def _batch(agents_dir: Path, spec: dict[str, Any], **kw: Any) -> _Batch:
    (agents_dir / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    extras = await KasHarness().session_extras("kirocrew", work_dir=None, session_key=_KEY, **kw)
    batch = _Batch({a["id"]: a for a in extras.custom_agents})
    batch.helper_parent = extras.shipped_helper_parent
    return batch


def _wire(batch: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each entry as ``session/new`` carries it."""
    params = build_session_new_params("/w", mcp_servers=[], kas_custom_agents=list(batch.values()))
    return {a["id"]: a for a in params["_meta"]["kiro"]["customAgents"]}


# ── what the helper holds ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_helper_lists_its_parents_servers_as_the_parent_lists_them(agents_dir):
    batch = await _batch(agents_dir, _spec())
    parent, helper = batch["kirocrew"], batch[_HELPER]
    refs = [t for t in helper["tools"] if t.startswith("@")]
    assert sorted(refs) == ["@one-tool/keep_me", "@plain", "@pooled", "@with-env"]
    assert set(refs) <= set(parent["tools"]), "never a tool reference the parent does not hold"
    assert not mounts_subagent_tool(helper)


@pytest.mark.asyncio
async def test_the_helpers_declarations_are_the_parents_byte_for_byte_on_the_wire(agents_dir):
    """The engine keys a running server by its name and declaration: an equal
    declaration is handed the parent's process, a different one starts another."""
    wire = _wire(await _batch(agents_dir, _spec()))
    parent, helper = wire["kirocrew"]["mcpServers"], wire[_HELPER]["mcpServers"]
    assert sorted(helper) == ["one-tool", "plain", "pooled", "with-env"]
    for name, declaration in helper.items():
        assert json.dumps(declaration, sort_keys=True) == json.dumps(parent[name], sort_keys=True)
    # The launcher of a server with env, a plain command, and one with its own timeout.
    assert "kiro_crew.kas_env_shim" in helper["with-env"]["args"]
    assert helper["plain"]["args"] == ["/srv/plain.py"]
    assert helper["one-tool"]["timeout"] == 5000


@pytest.mark.asyncio
async def test_a_pooled_server_reaches_the_helper_through_the_sessions_own_stub(
    agents_dir, monkeypatch
):
    """A broker stub travels in the session-level array, once for the whole batch:
    neither entry declares the server, the helper names it as its parent does, so
    it is the parent's stub and its pooled backend, with no second launch."""
    monkeypatch.setattr(
        kas_harness.session_servers_mod,
        "injection_server_names",
        lambda *a, **k: frozenset({"pooled", "someone-elses"}),
    )
    wire = _wire(await _batch(agents_dir, _spec()))
    parent, helper = wire["kirocrew"], wire[_HELPER]
    assert "pooled" not in parent["mcpServers"] and "pooled" not in helper["mcpServers"]
    assert "@pooled" in parent["tools"] and "@pooled" in helper["tools"]
    assert "@pooled" not in helper["excludedTools"]
    # The servers neither is stubbed for are still the parent's declarations.
    for name in ("plain", "with-env", "one-tool"):
        assert helper["mcpServers"][name] == parent["mcpServers"][name]
    # A stub of a server the parent's spec does not declare stays out.
    assert "@someone-elses" in helper["excludedTools"]
    assert "@someone-elses" not in helper["tools"]


@pytest.mark.asyncio
async def test_what_the_parent_does_not_hold_the_helper_does_not_hold(agents_dir):
    batch = await _batch(agents_dir, _spec())
    parent, helper = batch["kirocrew"], batch[_HELPER]
    # A server the parent's tools do not mount, and a tool of a server it switched off.
    assert "@not-mounted" in helper["excludedTools"]
    assert "not-mounted" not in helper.get("mcpServers", {})
    for withdrawn in ("@plain/hidden", "@plain/glob_*"):
        assert withdrawn in parent["excludedTools"] and withdrawn in helper["excludedTools"]
    # A server the parent switches off whole.
    muted = _spec()
    muted["mcpServers"]["plain"]["disabled"] = True
    helper = (await _batch(agents_dir, muted))[_HELPER]
    assert "@plain" not in helper["tools"] and "plain" not in helper.get("mcpServers", {})


@pytest.mark.asyncio
async def test_a_parents_hook_on_a_granted_server_tool_keeps_the_helper_at_ask(agents_dir):
    """A PreToolUse hook runs on a permission request, so the projection turns the
    parent's grant of what it covers into an ask. The helper carries that ask."""
    hooked = _spec(hooks={"preToolUse": [{"matcher": "@plain/granted", "command": "true"}]})
    batch = await _batch(agents_dir, hooked)

    def asks(entry: dict[str, Any]) -> list[dict[str, Any]]:
        rules = (entry.get("permissions") or {}).get("rules") or []
        return [
            r
            for r in rules
            if r.get("capability") == "mcp"
            and r.get("effect") == "ask"
            and any(str(m).startswith("plain/") for m in r.get("match") or [])
        ]

    assert asks(batch["kirocrew"]), "the parent's own call comes back as a request"
    for rule in asks(batch["kirocrew"]):
        assert rule in batch[_HELPER]["permissions"]["rules"]


@pytest.mark.asyncio
async def test_a_parent_with_no_server_leaves_the_helper_with_built_ins(agents_dir):
    spec = _spec(mcpServers={})
    spec["tools"] = [t for t in spec["tools"] if not t.startswith("@") or "kirocrew" in t]
    helper = (await _batch(agents_dir, spec))[_HELPER]
    assert not [t for t in helper["tools"] if t.startswith("@")]
    assert not helper.get("mcpServers")


@pytest.mark.asyncio
async def test_kiro_crews_own_servers_stay_out_by_rule(agents_dir, monkeypatch):
    """Excluded by what they are, not by a list here: a managed server added later is out too."""
    grown = frozenset({*kas_agents_mcp_servers.MANAGED_MCP_SERVER_NAMES, "kirocrew-added-later"})
    # Through the facade, which every holder of the name mirrors.
    from kiro_crew.acp import kas_agents

    monkeypatch.setattr(kas_agents, "MANAGED_MCP_SERVER_NAMES", grown)
    assert "kirocrew-added-later" in target.own_servers()
    spec = _spec()
    spec["tools"] = [*spec["tools"], "@kirocrew-added-later"]
    spec["mcpServers"]["kirocrew-added-later"] = {"command": sys.executable, "args": ["/x.py"]}
    helper = (await _batch(agents_dir, spec))[_HELPER]
    for name in grown:
        assert f"@{name}" in helper["excludedTools"], name
        assert not [t for t in helper["tools"] if t == f"@{name}" or t.startswith(f"@{name}/")]
        assert name not in helper.get("mcpServers", {})


@pytest.mark.parametrize("name", ["subagent", "builtin", "mcp", "a*", "a/b"])
def test_a_server_named_like_an_engine_tag_or_pattern_is_not_listed(name):
    parent = {
        "id": "kirocrew",
        "tools": "*",
        "mcpServers": {name: {"command": "x"}, "fine": {"command": "y"}},
    }
    assert list(target.shared_servers(parent, ())) == ["fine"]


# ── launch snapshots: a batch releases a name as often as it holds it ─────────


def _holds() -> dict[str, int]:
    """The holds on this test's own snapshot folder, by file name."""
    here = str(kas_env_launch.snapshot_dir())
    return {
        Path(k).name: v
        for k, v in kas_env_launch._SHARED_HOLDS.items()
        if v and str(Path(k).parent) == here
    }


@pytest.mark.asyncio
async def test_a_batch_whose_helper_repeats_a_launcher_releases_what_it_holds(agents_dir):
    """Two chats of the same agent share one launch snapshot. Closing one must
    leave it for the other: the helper's repeat holds the name it lists."""
    first = list((await _batch(agents_dir, _spec())).values())
    second = list((await _batch(agents_dir, _spec())).values())
    names = kas_env_launch.batch_snapshot_names(first)
    [name] = set(names)
    assert len(names) == 2, "the parent's entry and the helper's repeat"
    assert _holds() == {name: 4}, "one hold per entry that names it, in each batch"
    snapshot = kas_env_launch.snapshot_dir() / name
    kas_env_launch.discard_batch(first, names)
    assert _holds() == {name: 2} and snapshot.exists(), "the other chat still launches from it"
    kas_env_launch.discard_batch(second, kas_env_launch.batch_snapshot_names(second))
    assert _holds() == {} and not snapshot.exists()


@pytest.mark.asyncio
async def test_two_agents_of_one_batch_never_name_the_same_snapshot(agents_dir):
    """A name carries its agent, so each projection holds its own and both balance."""
    from kas_agents_helpers import _spec as agent_spec

    from kiro_crew.acp.kas_agents import to_client_custom_agent

    server = {"command": sys.executable, "args": ["/srv/env.py"], "env": {"K": "v"}}
    spec = agent_spec(mcpServers={"s": server})
    one = to_client_custom_agent("agent-one", spec, "p", session_key=_KEY)
    two = to_client_custom_agent("agent-two", spec, "p", session_key=_KEY)
    names = kas_env_launch.batch_snapshot_names([one, two])
    assert len(set(names)) == 2 and sorted(_holds().values()) == [1, 1]
    kas_env_launch.discard_batch([one, two], names)
    assert _holds() == {}


# ── the gate: a helper's call gets its parent's verdict ───────────────────────


def _handle(batch: dict[str, dict[str, Any]], cwd: Path) -> AcpSessionHandle:
    rt = MagicMock()
    rt.acp_backend = ACP_BACKEND_KAS
    rt.pid = None
    rt._remote_workspace = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    handle = AcpSessionHandle("own-sid", asyncio.Queue(), rt)
    handle._bound_cwd = str(cwd)
    KasHarness().record_session_projection(
        handle, list(batch.values()), "kirocrew", getattr(batch, "helper_parent", "")
    )
    return handle


@dataclasses.dataclass(frozen=True)
class _Call:
    label: str
    tool_id: str
    title: str
    kind: str
    raw: dict[str, Any]
    server: str = ""


def _calls(cwd: Path) -> list[_Call]:
    return [
        _Call(
            "read a workspace file", "read_file", "Read File", "read", {"path": str(cwd / "a.txt")}
        ),
        _Call("search", "grep_search", "Grep Search", "search", {"query": "x", "path": str(cwd)}),
        _Call("find files", "file_search", "File Search", "search", {"query": "*.py"}),
        _Call("list a folder", "list_directory", "List Directory", "read", {"path": str(cwd)}),
        _Call(
            "read a credential file",
            "read_file",
            "Read File",
            "read",
            {"path": str(_HOME / ".aws" / "credentials")},
        ),
        _Call(
            "a plain command", "run_command", "Run Command", "execute", {"command": "make build"}
        ),
        _Call(
            "a destructive command",
            "run_command",
            "Run Command",
            "execute",
            {"command": _DESTRUCTIVE},
        ),
        _Call(
            "a command that reads a key",
            "run_command",
            "Run Command",
            "execute",
            {"command": "cat " + str(_HOME / ".ssh" / "id_rsa")},
        ),
        _Call(
            "write a workspace file",
            "fs_write",
            "Write",
            "edit",
            {"path": str(cwd / "out.txt"), "command": "create"},
        ),
        _Call(
            "write Kiro Crew's policy file",
            "fs_write",
            "Write",
            "edit",
            {"path": str(_HOME / ".kiro" / "crew" / "security_policy.json"), "command": "create"},
        ),
        _Call(
            "fetch a page", "web_fetch", "Web Fetch", "fetch", {"url": "https://example.invalid/"}
        ),
        _Call(
            "a user MCP tool", "mcp_plain_lookup", "@plain/lookup", "other", {"key": "k"}, "plain"
        ),
        _Call(
            "a granted user MCP tool", "mcp_plain_granted", "@plain/granted", "other", {}, "plain"
        ),
    ]


def _request(handle: AcpSessionHandle, call: _Call, *, child: bool) -> AcpEvent:
    """*call* as the engine sends it: the ``tool_call`` frame, then the permission request."""
    meta: dict[str, Any] = {"toolOrigin": "default"}
    if call.server:
        meta["serverName"] = call.server
    if child:
        meta["agentSubtaskId"] = "subtask-1"
    handle._handle_update(
        JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": call.title,
                    "kind": call.kind,
                    "status": "pending",
                    "rawInput": call.raw,
                    "_meta": {"kiro": meta},
                }
            },
        )
    )
    event = handle._build_permission_event(
        JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "sessionId": "own-sid",
                "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": call.title},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": {"kiro": {"toolId": call.tool_id}},
            },
        )
    )
    assert event is not None and event.native_child_call is child
    return event


def _verdict(event: AcpEvent) -> tuple[str, str, str]:
    result = HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))
    return result.action, result.tier, str(result.reason)


_STAMPED = [f.name for f in dataclasses.fields(AcpEvent) if f.name.startswith("spec_")]

_CEILINGS: dict[str, dict[str, Any] | None] = {
    "no ceiling": None,
    "the ceiling denies the server": {"mcp": {"mode": "deny", "deny": ["@plain"]}},
    "the ceiling allows one server": {"mcp": {"mode": "allow", "allow": ["@kirocrew-core"]}},
    "the ceiling denies the shell": {"tools": {"mode": "deny", "deny": ["execute_bash"]}},
}


def _install(body: dict[str, Any] | None) -> None:
    base = build_default_context(KiroCrewConfig.load())
    ceiling = None if body is None else parse_policy({"version": 1, "boot": {}, **body})
    ctx_mod.set_context(dataclasses.replace(base, governance=ceiling))


@pytest.mark.asyncio
@pytest.mark.parametrize("ceiling", list(_CEILINGS))
async def test_the_gate_gives_the_helpers_call_its_parents_verdict(agents_dir, tmp_path, ceiling):
    """Every row, as the parent's own request and as the helper's: the same stamped
    fields, so the same action, tier and text."""
    batch = await _batch(agents_dir, _spec())
    assert target.helper_is_the_only_child(list(batch.values()), "kirocrew")
    _install(_CEILINGS[ceiling])
    seen: dict[str, tuple[str, str, str]] = {}
    for call in _calls(tmp_path):
        own = _request(_handle(batch, tmp_path), call, child=False)
        helper = _request(_handle(batch, tmp_path), call, child=True)
        for name in _STAMPED:
            assert getattr(helper, name) == getattr(own, name), (call.label, name)
        assert (helper.mcp_server_name, helper.tool_name) == (own.mcp_server_name, own.tool_name)
        assert helper.mcp_identity_trusted is own.mcp_identity_trusted
        assert _verdict(helper) == _verdict(own), call.label
        seen[call.label] = _verdict(own)
    # The table is not vacuous: it holds a grant, a refusal of each kind and an ask.
    if ceiling == "no ceiling":
        assert seen["read a workspace file"][:2] == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
        assert seen["find files"][:2] == (TOOL_AUTO_APPROVE, "spec-builtin-grant")
        assert seen["a destructive command"][0] == TOOL_DENY
        assert seen["read a credential file"][0] == TOOL_DENY
        assert seen["a user MCP tool"][0] not in (TOOL_AUTO_APPROVE, TOOL_DENY)
    if ceiling == "the ceiling denies the server":
        assert seen["a user MCP tool"][:2] == (TOOL_DENY, "governance")


@pytest.mark.asyncio
async def test_a_child_that_runs_another_agents_spec_keeps_no_grant(agents_dir, tmp_path):
    """Without the helper's mount a native child is some other agent: limits only."""
    batch = await _batch(agents_dir, _spec())
    others = {name: entry for name, entry in batch.items() if name != _HELPER}
    assert not target.helper_is_the_only_child(list(others.values()), "kirocrew")
    # A file search: only the spec's grant answers it (a plain read is also
    # answered by the gate's read-only row, grant or no grant).
    [search] = [c for c in _calls(tmp_path) if c.label == "find files"]
    own = _request(_handle(others, tmp_path), search, child=False)
    child = _request(_handle(others, tmp_path), search, child=True)
    assert own.spec_granted_builtins and not child.spec_granted_builtins
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(child)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_request_that_names_no_tool_call_keeps_no_grant(agents_dir, tmp_path):
    handle = _handle(await _batch(agents_dir, _spec()), tmp_path)
    event = handle._build_permission_event(
        JsonRpcMessage(
            id=9,
            method="session/request_permission",
            params={
                "sessionId": "own-sid",
                "toolCall": {"status": "pending", "title": "Read File"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": {"kiro": {"toolId": "read_file"}},
            },
        )
    )
    assert event is not None and event.native_child_call and not event.spec_granted_builtins


_WRITE_ASKED = {"capability": "fs_write", "effect": "ask"}


@pytest.mark.asyncio
async def test_a_persons_own_agent_named_like_the_helper_gets_no_parent_grant(agents_dir, tmp_path):
    """A person's parent grants writes and lists its own ``kirocrew-helper``, whose
    file asks about writes: the batch looks like a mount, but nothing mounted one."""
    boss = {
        "name": "boss",
        "prompt": "p",
        "tools": ["fs_read", "fs_write", "use_subagent"],
        "allowedTools": ["fs_write"],
        "toolsSettings": {"subagent": {"availableAgents": [_HELPER]}},
    }
    own = {
        "name": _HELPER,
        "prompt": "p",
        "tools": ["fs_write"],
        "permissions": {"rules": [_WRITE_ASKED]},
    }
    (agents_dir / "boss.json").write_text(json.dumps(boss), encoding="utf-8")
    (agents_dir / f"{_HELPER}.json").write_text(json.dumps(own), encoding="utf-8")
    extras = await KasHarness().session_extras("boss", work_dir=None, session_key=_KEY)
    registered = list(extras.custom_agents)
    assert {a["id"] for a in registered} >= {"boss", _HELPER}
    assert extras.shipped_helper_parent == ""
    handle = _handle(_Batch({a["id"]: a for a in registered}), tmp_path)
    handle.kas_projected_agent = "boss"
    KasHarness().record_session_projection(handle, registered, "boss", extras.shipped_helper_parent)
    [write] = [c for c in _calls(tmp_path) if c.label == "write a workspace file"]
    child = _request(handle, write, child=True)
    assert not child.spec_granted_builtins


@pytest.mark.asyncio
async def test_a_registration_that_mounted_none_after_one_that_did_leaves_no_grant(
    agents_dir, tmp_path
):
    batch = await _batch(agents_dir, _spec())
    handle = _handle(batch, tmp_path)
    [search] = [c for c in _calls(tmp_path) if c.label == "find files"]
    assert _request(handle, search, child=True).spec_granted_builtins
    # Re-registered with the same batch, but by a projection that did not mount.
    KasHarness().record_session_projection(handle, list(batch.values()), "kirocrew", "")
    assert handle.kas_shipped_helper_parent == ""
    assert not _request(handle, search, child=True).spec_granted_builtins
    # Provenance for another agent is no proof either.
    KasHarness().record_session_projection(handle, list(batch.values()), "kirocrew", "someone")
    assert not _request(handle, search, child=True).spec_granted_builtins


def test_a_mode_the_mount_does_not_cover_is_not_the_helpers_parent():
    helper = {"id": _HELPER}
    mounted = {"id": "kirocrew", "permissions": {"rules": [target.only_helper_rule()]}}
    other = {"id": "someone", "permissions": {"rules": []}}
    batch = [mounted, other, helper]
    assert target.helper_is_the_only_child(batch, "kirocrew")
    assert not target.helper_is_the_only_child(batch, "someone")
    assert not target.helper_is_the_only_child(batch, "vibe")
    assert not target.helper_is_the_only_child([mounted, other], "kirocrew")
    assert not target.helper_is_the_only_child(None, "kirocrew")


# ── the audit row of a helper's finished call ─────────────────────────────────


def test_a_finished_child_call_leaves_one_audit_row_with_no_argument(monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.agent_sdk.capabilities import capabilities_for
    from kiro_crew.dashboard import chat_runner

    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(
        chat_runner,
        "sel",
        lambda: SimpleNamespace(log_tool_invocation=lambda **kw: rows.append(kw)),
    )

    async def _no_hooks(turn, event, **kwargs):
        return []

    monkeypatch.setattr(chat_runner, "_fire", _no_hooks)
    turn = SimpleNamespace(
        client=SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_KAS)),
        _pending_tool_ids={},
        session_key=_KEY,
        slot=SimpleNamespace(agent="kirocrew"),
    )
    finished = AcpEvent(
        kind="subagent_activity",
        sub_session_id="subtask-1",
        tool_call_id="tc-1",
        title="@plain/lookup",
        tool_final=True,
        tool_status="completed",
        tool_output="the-result-text",
        mcp_server_name="plain",
    )
    asyncio.run(chat_runner._on_child_tool_finished(turn, finished))
    running = dataclasses.replace(finished, tool_final=False, tool_status="in_progress")
    asyncio.run(chat_runner._on_child_tool_finished(turn, running))
    [row] = rows
    assert row["session_key"] == _KEY and row["agent"] == "kirocrew"
    assert (row["tool_name"], row["outcome"], row["source"]) == (
        "@plain/lookup",
        "invoked",
        "dashboard",
    )
    assert row["metadata"] == {"sub_agent": True, "status": "completed"}
    assert "the-result-text" not in json.dumps(row)
