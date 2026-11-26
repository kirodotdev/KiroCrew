"""With the ``kas_subagents`` preview on, every KAS chat kind mounts the engine's sub-agent tool.

A person's dashboard chat, a crew member's chat, a member dispatch, a Crew
panel, a cron run and a messaging channel's chat all mount it on the chat's own
agent entry when that agent is one Kiro Crew wrote (a person's own agent file is
read as kiro-cli reads it), and the shipped helper carries that entry's denies, so the parent's
deny reaches the child in each. A conductor's chat never mounts it, a Bot's own
thread on its conductor included: a conductor hands its work to sessions. Only a
person's own dashboard chat on the shipped
default agent, and the member named ``default``'s own chat on it, drop
``spawn_run`` and ``spawn_sub_agents``. Off or never set, every chat kind projects
the batch of before the preview, and no session start writes an agent file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew import crew_mode
from kiro_crew import members as members_mod
from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.mate_worker import MATE_CONDUCTOR_AGENT, MATE_CONDUCTOR_EXCLUDED_TOOLS

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = "kirocrew-helper"
_SPAWN = ("@kirocrew-core/spawn_run", "@kirocrew-core/spawn_sub_agents")
_PARENT_DENY = {"capability": "filesystem", "match": ["/secret/**"], "effect": "deny"}

#: Every chat kind the preview reaches: (session key, session_extras kwargs).
_CHATS = {
    "dashboard": ("dashboard:t1", {}),
    "member": ("dashboard:member-ada", {}),
    "member-dispatch": ("dashboard:t1", {"member_dispatch": True}),
    "crew-panel": ("dashboard:t1", {"crew_panel": True}),
    "cron": ("cron:job-1", {}),
    "channel": ("slack:C1:T1", {}),
}


def _spec(name: str = "kirocrew") -> dict:
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    return {
        "name": name,
        "description": "an agent",
        "prompt": "You are an agent.",
        "tools": shipped["tools"],
        "allowedTools": shipped["allowedTools"],
        "permissions": {"rules": [_PARENT_DENY]},
    }


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    monkeypatch.setattr(agent_mod, "require_fresh_derived_spec", lambda *a, **k: None)
    monkeypatch.setattr(members_mod, "member_identity_for_session", lambda k: None)
    return user


@pytest.fixture
def bot_worker(monkeypatch, tmp_path):
    """Every worker session names the Bot ``pixel``, whose own file adds nothing."""
    from types import SimpleNamespace

    from kiro_crew import mate_worker

    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id, config=None: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel"),
    )
    _write(mates, {"name": "pixel", "prompt": ""})


def _as_bot_worker(request, agent: str) -> None:
    """The Bot worker chat runs on a Bot's execution record: set one up for it."""
    if agent == "kirocrew-mate-worker":
        request.getfixturevalue("bot_worker")


@pytest.fixture
def setting(monkeypatch):
    def _set(on: bool | None) -> None:
        cfg = KiroCrewConfig()
        cfg.dashboard.feature_previews = {} if on is None else {engine.KAS_SUBAGENTS_PREVIEW: on}
        monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))

    return _set


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch(agent: str, key: str, work_dir: str | None = None, **kw) -> dict[str, dict]:
    extras = await KasHarness().session_extras(agent, work_dir=work_dir, session_key=key, **kw)
    return {a["id"]: a for a in extras.custom_agents}


def _rules(entry: dict) -> list[dict]:
    return (entry.get("permissions") or {}).get("rules") or []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent", ["kirocrew", "kirocrew-worker", "kirocrew-research", "kirocrew-mate-worker"]
)
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_every_chat_kind_mounts_and_the_child_is_the_chats_own_agent(
    agents_dir, setting, request, chat, agent
):
    _as_bot_worker(request, agent)
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(agent))
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch(agent, key, **kw)
    assert _HELPER in batch, chat
    parent, child = batch[agent], batch[_HELPER]
    assert _PARENT_DENY in _rules(parent), chat
    assert _PARENT_DENY in _rules(child), chat
    # The agent an unnamed spawn_run runs: its own built-ins, less the sub-agent
    # tool, and no Kiro Crew tool.
    builtins = [t for t in parent["tools"] if not t.startswith("@") and t != "invoke_sub_agent"]
    assert child["tools"] == builtins, chat
    assert child["prompt"].startswith(parent["prompt"]), chat


#: Every conductor: a Bot's own conductor and the shipped conductor templates.
_CONDUCTORS = sorted(crew_mode.CONDUCTOR_TEMPLATES | {MATE_CONDUCTOR_AGENT})


async def _batch_of_before(monkeypatch, agent: str, key: str, **kw) -> dict[str, dict]:
    with monkeypatch.context() as m:
        m.setattr(engine, "engine_subagents_on", lambda *a, **k: False)
        return await _batch(agent, key, **kw)


@pytest.mark.asyncio
async def test_a_bots_fresh_conductor_chat_gets_no_engine_sub_agent_mount(
    agents_dir, setting, monkeypatch
):
    # A Bot's own thread runs on its conductor, spec'd as the default template with
    # Kiro Crew's spawn tools left out; preview on, runtime sharing off.
    _write(agents_dir, _spec())
    conductor = {
        **_spec(MATE_CONDUCTOR_AGENT),
        "excludedTools": list(MATE_CONDUCTOR_EXCLUDED_TOOLS),
    }
    _write(agents_dir, conductor)
    setting(True)
    key = "dashboard:member-ada"
    batch = await _batch(MATE_CONDUCTOR_AGENT, key, member_dispatch=True)
    assert _HELPER not in batch
    assert "invoke_sub_agent" not in batch[MATE_CONDUCTOR_AGENT]["tools"]
    before = await _batch_of_before(monkeypatch, MATE_CONDUCTOR_AGENT, key, member_dispatch=True)
    assert batch == before
    assert engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, MATE_CONDUCTOR_AGENT, key) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", _CONDUCTORS)
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_no_conductor_chat_mounts_the_engine_tool(
    agents_dir, setting, monkeypatch, chat, agent
):
    # Every built-in the helper holds, so only the conductor's identity keeps it off.
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(agent))
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch(agent, key, **kw)
    assert _HELPER not in batch, chat
    assert batch == await _batch_of_before(monkeypatch, agent, key, **kw), chat
    assert engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, agent, key) == "", chat


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_the_configured_conductor_mode_agent_mounts_no_engine_tool(
    agents_dir, setting, monkeypatch, chat
):
    from types import SimpleNamespace

    # A conductor mode chat runs on the agent ``agent.crew_mode_agent`` names.
    configured = "kirocrew-research"
    live = SimpleNamespace(agent=SimpleNamespace(crew_mode_agent=configured))
    monkeypatch.setattr(crew_mode, "_live_cfg", lambda: live)
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(configured))
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch(configured, key, **kw)
    assert _HELPER not in batch, chat
    assert batch == await _batch_of_before(monkeypatch, configured, key, **kw), chat
    assert engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, configured, key) == "", chat
    # The default agent's chat of the same kind still mounts it.
    assert _HELPER in await _batch("kirocrew", key, **kw), chat


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
@pytest.mark.parametrize(
    "agent", ["kirocrew", "kirocrew-research", "kirocrew-worker", "kirocrew-mate-worker"]
)
async def test_every_mounted_chat_drops_both_spawn_tools(agents_dir, setting, request, chat, agent):
    _as_bot_worker(request, agent)
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(agent))
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch(agent, key, **kw)
    assert _HELPER in batch, chat
    excluded = batch[agent].get("excludedTools") or []
    assert all(t in excluded for t in _SPAWN), chat
    assert engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, agent, key) == "replace"


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", [False, True], ids=["plain", "member-dispatch"])
@pytest.mark.parametrize("name", ["default", "ada"])
async def test_every_members_chat_drops_both_spawn_tools(
    agents_dir, setting, monkeypatch, name, dispatch
):
    _write(agents_dir, _spec())
    _write(agents_dir, _spec("kirocrew-research"))
    setting(True)
    monkeypatch.setattr(
        members_mod, "member_identity_for_session", lambda k: {"slug": name, "name": name}
    )
    key = f"dashboard:member-{name}"
    for agent in ("kirocrew", "kirocrew-research"):
        batch = await _batch(agent, key, member_dispatch=dispatch)
        excluded = batch[agent].get("excludedTools") or []
        assert all(t in excluded for t in _SPAWN), agent
        mode = engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, agent, key)
        assert mode == "replace", agent


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["unset", "off"])
@pytest.mark.parametrize("agent", ["kirocrew", "kirocrew-research", "mine"])
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_off_every_chat_kind_projects_the_batch_of_before(
    agents_dir, setting, monkeypatch, chat, agent, value
):
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(agent))
    setting(value)
    key, kw = _CHATS[chat]
    got = json.dumps(await _batch(agent, key, **kw), sort_keys=True)
    with monkeypatch.context() as m:
        m.setattr(engine, "engine_subagents_on", lambda *a, **k: False)
        before = json.dumps(await _batch(agent, key, **kw), sort_keys=True)
    assert got == before


def _snapshot(*roots: Path) -> dict[str, bytes]:
    return {
        str(path): path.read_bytes()
        for root in roots
        if root.exists()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_a_session_start_with_the_preview_on_writes_no_agent_file(
    agents_dir, setting, tmp_path, chat
):
    project = tmp_path / "project"
    (project / ".kiro" / "agents").mkdir(parents=True)
    _write(agents_dir, _spec())
    _write(agents_dir, _spec("mine"))
    _write(project / ".kiro" / "agents", _spec("mine"))
    setting(True)
    before = _snapshot(agents_dir, project / ".kiro")
    key, kw = _CHATS[chat]
    assert _HELPER in await _batch("kirocrew", key, work_dir=str(project), **kw)
    # A person's own agent is read as kiro-cli reads it: no helper, and no write either.
    assert _HELPER not in await _batch("mine", key, work_dir=str(project), **kw)
    assert _snapshot(agents_dir, project / ".kiro") == before


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_a_persons_own_agents_run_as_children_only_where_the_spawn_tools_go(
    agents_dir, setting, chat
):
    own = {"name": "fine", "prompt": "p", "tools": ["fs_read"]}
    _write(agents_dir, _spec())
    _write(agents_dir, own)
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch("kirocrew", key, **kw)
    excluded = batch["kirocrew"].get("excludedTools") or []
    replaced = all(t in excluded for t in _SPAWN)
    assert ("fine" in batch) is replaced, chat


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_a_registered_own_agent_starts_no_sub_agent(agents_dir, setting, chat):
    """An own agent with every tool and no sub-agent deny of its own is still a leaf."""
    from kiro_crew.acp.kas_subagent_targets import NO_ENGINE_SUBAGENTS_RULE

    _write(agents_dir, {**_spec(), "tools": "*"})
    _write(agents_dir, {"name": "wide", "prompt": "p", "tools": ["*"]})
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch("kirocrew", key, **kw)
    assert NO_ENGINE_SUBAGENTS_RULE in _rules(batch["wide"]), chat


#: A chat agent built without shell or file writes, and an own agent that lists both.
_NARROW_PARENT_TOOLS = ["fs_read", "grep", "@kirocrew-core"]
_SHELL_AGENT = {"name": "shelly", "prompt": "p", "tools": ["fs_read", "execute_bash", "fs_write"]}
_SHELL_IDS = {"execute_bash", "shell", "fs_write", "write", "control_bash_process"}


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_an_own_agent_keeps_a_built_in_its_chat_lacks_only_where_a_person_answers(
    agents_dir, setting, chat
):
    """A cron run's ``auto`` or a trusted channel answers a child's ask with no one
    there, so outside the default agent's own dashboard chats a person's own agent
    holds only the built-ins the chat's agent mounts."""
    _write(agents_dir, {**_spec(), "tools": _NARROW_PARENT_TOOLS})
    _write(agents_dir, _SHELL_AGENT)
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch("kirocrew", key, **kw)
    if chat not in _KEEPS_UNSHARED:
        child = batch["shelly"]
        assert not set(child["tools"]) & _SHELL_IDS, chat
        assert "fs_read" in child["tools"], chat
    elif "shelly" in batch:
        assert set(batch["shelly"]["tools"]) & _SHELL_IDS, chat


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["kirocrew-worker", "kirocrew-mate-worker"])
async def test_a_created_sessions_own_agent_holds_only_its_parents_built_ins(
    agents_dir, setting, request, agent
):
    _as_bot_worker(request, agent)
    _write(agents_dir, _spec())
    _write(agents_dir, {**_spec(agent), "tools": _NARROW_PARENT_TOOLS})
    _write(agents_dir, _SHELL_AGENT)
    setting(True)
    child = (await _batch(agent, "dashboard:t1"))["shelly"]
    assert not set(child["tools"]) & _SHELL_IDS
    assert "fs_read" in child["tools"]


_REPORTS = {"reports": {"command": "/srv/reports.py"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_ref, child_ref, kept",
    [
        ("@reports/read_report", "@reports", ["@reports/read_report"]),
        ("@reports/read_*", "@reports/delete_report", []),
        ("@reports/read_*", "@reports/read_report", ["@reports/read_report"]),
        ("@reports", "@reports/delete_report", ["@reports/delete_report"]),
    ],
)
async def test_a_child_never_mounts_a_server_tool_its_parent_leaves_out(
    agents_dir, setting, parent_ref, child_ref, kept
):
    _write(
        agents_dir,
        {**_spec(), "tools": [*_spec()["tools"], parent_ref], "mcpServers": dict(_REPORTS)},
    )
    _write(
        agents_dir,
        {
            "name": "reporter",
            "prompt": "p",
            "tools": ["fs_read", child_ref],
            "mcpServers": dict(_REPORTS),
        },
    )
    setting(True)
    child = (await _batch("kirocrew", "cron:job-1"))["reporter"]
    assert [t for t in child["tools"] if t.startswith("@reports")] == kept
    if not kept:
        assert "@reports" in child["excludedTools"]


@pytest.mark.asyncio
async def test_a_parents_tool_pattern_keeps_the_built_ins_it_selects(agents_dir, setting):
    """A parent's ``execute_*`` mounts ``execute_bash``, so the child keeps it."""
    _write(agents_dir, {**_spec(), "tools": ["fs_read", "execute_*", "@kirocrew-core"]})
    _write(agents_dir, _SHELL_AGENT)
    setting(True)
    child = (await _batch("kirocrew", "cron:job-1"))["shelly"]
    assert "execute_bash" in child["tools"]
    assert not {"fs_write", "write"} & set(child["tools"])


@pytest.mark.asyncio
async def test_an_own_agent_with_wildcard_tools_is_left_out_under_a_narrow_parent(
    agents_dir, setting
):
    _write(agents_dir, {**_spec(), "tools": _NARROW_PARENT_TOOLS})
    _write(agents_dir, {"name": "wide", "prompt": "p", "tools": ["*"]})
    setting(True)
    batch = await _batch("kirocrew", "cron:job-1")
    assert "wide" not in batch
    assert _HELPER in batch


@pytest.mark.asyncio
async def test_a_left_out_own_agent_releases_its_launch_holds(agents_dir, setting, monkeypatch):
    from kiro_crew.acp import kas_env_launch

    released: list[list] = []
    monkeypatch.setattr(kas_env_launch, "discard_batch", lambda agents, *a: released.append(agents))
    _write(agents_dir, {**_spec(), "tools": _NARROW_PARENT_TOOLS})
    _write(agents_dir, {"name": "wide", "prompt": "p", "tools": ["*"]})
    setting(True)
    await _batch("kirocrew", "cron:job-1")
    assert any(isinstance(a, dict) and a.get("id") == "wide" for batch in released for a in batch)


#: An own agent with a server the chat's agent does not hold, and one it holds alike.
_SERVER_AGENT = {
    "name": "srvagent",
    "prompt": "p",
    "tools": ["fs_read", "@own-srv", "@both"],
    "mcpServers": {
        "own-srv": {"command": "/srv/own.py"},
        "both": {"command": "/srv/both.py"},
    },
}

#: Chat kinds by whether a person's own agent there keeps a server its chat lacks:
#: only the two chats that registered own agents before (the default agent's own
#: dashboard chat, which a Crew panel opens, and the member ``default``'s).
_KEEPS_UNSHARED = {"dashboard", "crew-panel", "member-dispatch"}


@pytest.mark.asyncio
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_an_own_agent_keeps_a_server_its_chat_lacks_only_where_a_person_answers(
    agents_dir, setting, chat
):
    """A cron run's ``auto`` or a trusted channel answers a child's ask with no one
    there, so outside the default agent's own dashboard chats a person's own agent
    holds only the servers it shares with the chat's agent."""
    _write(
        agents_dir,
        {
            **_spec(),
            "tools": [*_spec()["tools"], "@both"],
            "mcpServers": {"both": {"command": "/srv/both.py"}},
        },
    )
    _write(agents_dir, _SERVER_AGENT)
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch("kirocrew", key, **kw)
    child = batch["srvagent"]
    keeps = chat in _KEEPS_UNSHARED
    assert ("own-srv" in (child.get("mcpServers") or {})) is keeps, chat
    assert ("@own-srv" in (child.get("excludedTools") or [])) is not keeps, chat
    # A server it declares as the chat's agent does stays, everywhere.
    assert "both" in (child.get("mcpServers") or {}), chat
    assert "@both" not in (child.get("excludedTools") or []), chat


@pytest.mark.asyncio
async def test_a_pooled_server_both_declare_alike_stays_with_the_child(
    agents_dir, setting, monkeypatch
):
    """A pooled server reaches the child as the session's own stub, so it is shared."""
    from kiro_crew.acp.kas_settings_mcp import CREW_SETTINGS_MCP_KEY

    monkeypatch.setattr(
        kas_harness.session_servers_mod,
        "injection_server_names",
        lambda *a, **k: frozenset({"pooled"}),
    )
    pooled = {"command": "/srv/pooled.py"}
    _write(
        agents_dir,
        {**_spec(), "tools": [*_spec()["tools"], "@pooled"], "mcpServers": {"pooled": pooled}},
    )
    child = {**_SERVER_AGENT, "tools": [*_SERVER_AGENT["tools"], "@pooled"]}
    _write(agents_dir, {**child, "mcpServers": {**_SERVER_AGENT["mcpServers"], "pooled": pooled}})
    setting(True)
    batch = await _batch("kirocrew", "cron:job-1")
    entry = batch["srvagent"]
    assert "@pooled" not in entry["excludedTools"]
    assert "pooled" in entry[CREW_SETTINGS_MCP_KEY]["declared"]
    assert "@own-srv" in entry["excludedTools"]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["kirocrew-worker", "kirocrew-research", "kirocrew-mate-worker"])
async def test_a_created_sessions_own_agent_holds_only_shared_servers(
    agents_dir, setting, request, agent
):
    """A dispatched worker and a Bot's worker chat run unattended too."""
    _as_bot_worker(request, agent)
    _write(agents_dir, _spec())
    _write(
        agents_dir,
        {
            **_spec(agent),
            "tools": [*_spec(agent)["tools"], "@both"],
            "mcpServers": {"both": {"command": "/srv/both.py"}},
        },
    )
    _write(agents_dir, _SERVER_AGENT)
    setting(True)
    batch = await _batch(agent, "dashboard:t1")
    child = batch["srvagent"]
    assert "own-srv" not in (child.get("mcpServers") or {})
    assert "@own-srv" in child["excludedTools"]
    assert "both" in child["mcpServers"]


@pytest.mark.parametrize(("agent", "own"), [("kirocrew", False), ("mine", True)])
def test_the_contract_names_the_engine_tool_only_where_the_mount_can_run(
    agents_dir, setting, agent, own
):
    from kiro_crew.acp.provider_labels import PROVIDER_LABEL_KAS

    _write(agents_dir, _spec())
    _write(agents_dir, {"name": "mine", "prompt": "p", "tools": ["*"]})
    setting(True)
    for key in ("cron:job-1", "slack:C1:T1"):
        mode = engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, agent, key)
        assert mode == ("" if own else "replace"), key
