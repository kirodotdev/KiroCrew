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
@pytest.mark.parametrize("agent", ["kirocrew", "kirocrew-worker", "kirocrew-research"])
@pytest.mark.parametrize("chat", sorted(_CHATS))
async def test_every_chat_kind_mounts_and_the_parents_deny_reaches_the_child(
    agents_dir, setting, chat, agent
):
    _write(agents_dir, _spec())
    _write(agents_dir, _spec(agent))
    setting(True)
    key, kw = _CHATS[chat]
    batch = await _batch(agent, key, **kw)
    assert _HELPER in batch, chat
    assert _PARENT_DENY in _rules(batch[agent]), chat
    assert _PARENT_DENY in _rules(batch[_HELPER]), chat
    # The child holds no Kiro Crew tool, and nothing the parent does not.
    assert not any(isinstance(t, str) and t.startswith("@") for t in batch[_HELPER]["tools"])


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
async def test_only_the_default_agents_own_dashboard_chats_drop_both_spawn_tools(
    agents_dir, setting, monkeypatch, chat
):
    _write(agents_dir, _spec())
    setting(True)
    key, kw = _CHATS[chat]
    excluded = (await _batch("kirocrew", key, **kw))["kirocrew"].get("excludedTools") or []
    # The person's own dashboard key, whatever the session start mounts beside it.
    dropped = key == "dashboard:t1"
    assert all((t in excluded) is dropped for t in _SPAWN), chat


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatch", [False, True], ids=["plain", "member-dispatch"])
@pytest.mark.parametrize(("name", "dropped"), [("default", True), ("ada", False)])
async def test_the_member_named_default_drops_both_spawn_tools_on_the_default_agent(
    agents_dir, setting, monkeypatch, name, dropped, dispatch
):
    _write(agents_dir, _spec())
    _write(agents_dir, _spec("kirocrew-research"))
    setting(True)
    monkeypatch.setattr(
        members_mod, "member_identity_for_session", lambda k: {"slug": name, "name": name}
    )
    key = f"dashboard:member-{name}"
    batch = await _batch("kirocrew", key, member_dispatch=dispatch)
    excluded = batch["kirocrew"].get("excludedTools") or []
    assert all((t in excluded) is dropped for t in _SPAWN)
    # The contract reads the same decision from the agent and the key.
    mode = engine.contract_engine_subagent_mode(PROVIDER_LABEL_KAS, "kirocrew", key)
    assert mode == ("replace" if dropped else "beside")
    # On another agent, even the member named default keeps them.
    other = await _batch("kirocrew-research", key)
    mine = other["kirocrew-research"].get("excludedTools") or []
    assert not any(t in mine for t in _SPAWN)


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
        assert mode == ("" if own else "beside"), key
