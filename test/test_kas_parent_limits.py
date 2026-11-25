"""A KAS sub-agent run is refused what the agents above it are refused.

A run that names another agent than its parent's runs that agent's own spec. Its
session's entry also carries the engine ``deny`` rules of each agent above it, and
its requests carry those agents' gate-only limits, so a command, path or URL the
parent's spec denies is refused in the run too. Only denies travel: no allow of a
parent reaches the run, and the run's own grants and limits stay as they were.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_kas_spec_builtin_grant import _handle, _permission
from test_tool_call_classification_kas import _kas_builtin_flow

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
import kiro_crew.subagent as subagent_mod
from kiro_crew.acp import kas_parent_limits
from kiro_crew.acp._dispatch import attach_kas_custom_agents, scoped_tool_cache_key
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_parent_limits import CREW_PARENT_LIMITS_KEY
from kiro_crew.acp.session_handle import AcpAgentUnavailable
from kiro_crew.acp.types import AcpEvent
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs
from kiro_crew.platform import context as ctx_mod
from kiro_crew.platform import governance_profiles as gp
from kiro_crew.subagent_persistence import create_agent_folder

_REPO = Path(__file__).resolve().parents[1]

#: The cause report's parent: only ``echo`` may run.
_LIMITED = {
    "name": "limited",
    "prompt": "p",
    "tools": ["*"],
    "toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["echo .*"]}},
}
_PLAIN = {"name": "plain", "prompt": "p", "tools": ["*"]}
_WORKER = {"name": "worker", "prompt": "p", "tools": ["*"], "allowedTools": ["web_fetch"]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    _floor_monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    yield
    gp.reset_store()
    ctx_mod.reset_context()


@pytest.fixture
def agents(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    for spec in (_LIMITED, _PLAIN, _WORKER):
        _write(user, spec)
    return user


@pytest.fixture
def sessions(monkeypatch) -> dict[str, Any]:
    """Session key -> the agent that session runs; an exception is raised instead."""
    known: dict[str, Any] = {}
    real = subagent_mod._parent_template_for_spawn

    def template(key: str) -> str:
        if key not in known:
            return real(key)
        if isinstance(known[key], Exception):
            raise known[key]
        return known[key]

    monkeypatch.setattr(subagent_mod, "_parent_template_for_spawn", template)
    return known


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch(agent: str, key: str) -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=key)
    return list(extras.custom_agents)


def _records(batch: list[dict], agent: str, cwd: str = "/work"):
    return kas_parent_limits.projected_parent_limits(batch, agent, cwd)


def _rules(batch: list[dict], agent: str) -> list[dict]:
    entry = next(a for a in batch if a["id"] == agent)
    return (entry.get("permissions") or {}).get("rules") or []


def _shell(command: str) -> AcpEvent:
    return _kas_builtin_flow(
        "run_command", title="Run Command", kind="execute", raw_input={"command": command}
    )


def _stamped(
    event: AcpEvent,
    batch: list[dict],
    agent: str,
    *,
    native_child: bool = False,
    cwd: str = "/work",
):
    """*event* as the run's session handle, bound to *cwd*, stamps a request of the
    run's agent."""
    handle = _handle(ACP_BACKEND_KAS)
    handle.kas_crew_answered = frozenset()
    handle._bound_cwd = cwd
    KasHarness().record_session_projection(handle, batch, agent)
    if native_child:
        handle._native_child_tool_call_ids.add(scoped_tool_cache_key("own-sid", "tc-1"))
    stamped = handle._build_permission_event(_permission("own-sid"))
    assert stamped is not None
    judged = AcpEvent(**{**event.__dict__})
    for name in (n for n in vars(stamped) if n.startswith("spec_")):
        setattr(judged, name, getattr(stamped, name))
    return judged


def _judge(event: AcpEvent):
    return HookManager(HooksConfig()).on_tool_call(event.title, **hook_gate_kwargs(event))


# ── the cause report's cells ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("child", ["worker", "plain"], ids=["cell-B", "cell-C"])
async def test_a_named_child_is_refused_the_command_its_parent_is_refused(agents, sessions, child):
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch(child, "subagent:c1")

    verdict = _judge(_stamped(_shell("touch marker"), batch, child))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert "the spec of agent 'limited', under which this sub-agent runs" in verdict.reason
    assert "denyByDefault" in verdict.reason and "this agent's spec" not in verdict.reason
    # What the parent's allowlist matches is not refused, and is not granted either.
    allowed = _judge(_stamped(_shell("echo hi > out.txt"), batch, child))
    assert allowed.tier != "spec-parent-limits" and allowed.action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_without_a_limited_parent_the_same_command_is_not_refused(agents, sessions):
    sessions["dashboard:p"] = "plain"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    assert _judge(_stamped(_shell("touch marker"), batch, "worker")).action != TOOL_DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["limited", "plain", ""], ids=["cell-D", "no-limit", "none"])
async def test_a_child_on_its_parents_agent_or_under_no_limit_is_unchanged(
    agents, sessions, parent
):
    """The parent's own agent, a parent with no limit and no parent at all: the
    run's batch is the one it had."""
    child = parent or "plain"
    before = await _batch(child, "subagent:c1")
    sessions["dashboard:p"] = parent
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _batch(child, "subagent:c1") == before
    assert _records(before, child) == ()


@pytest.mark.asyncio
async def test_a_session_that_is_not_a_sub_agent_run_is_unchanged(agents, sessions):
    sessions["dashboard:p"] = "limited"
    before = await _batch("worker", "dashboard:chat")
    kas_parent_limits.note_parent("dashboard:chat", "dashboard:p")
    assert await _batch("worker", "dashboard:chat") == before


# ── the chain and the start paths ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_grandchild_is_held_to_its_grandparents_denies(agents, sessions):
    sessions["dashboard:gp"] = "limited"
    create_agent_folder("p1", task="t", agent="plain", parent_session="dashboard:gp")
    create_agent_folder("c2", task="t", agent="worker", parent_session="subagent:p1")
    batch = await _batch("worker", "subagent:c2")
    assert [r.agent for r in _records(batch, "worker")] == ["limited"]
    verdict = _judge(_stamped(_shell("touch marker"), batch, "worker"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")


@pytest.mark.asyncio
@pytest.mark.parametrize("known_by", ["record", "note", "both"])
async def test_every_way_a_run_names_its_parent_holds_it(agents, sessions, known_by):
    """The run's record (a first start, a queue drain, a retry, a restart) and the
    note a run leaves as it starts (a continuation on an older run's key)."""
    sessions["dashboard:p"] = "limited"
    if known_by in ("record", "both"):
        create_agent_folder("c1", task="t", agent="worker", parent_session="dashboard:p")
    if known_by in ("note", "both"):
        kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert [r.agent for r in _records(await _batch("worker", "subagent:c1"), "worker")] == [
        "limited"
    ]


@pytest.mark.asyncio
async def test_a_run_continued_by_another_session_is_held_to_both_parents(agents, sessions):
    _write(agents, {**_PLAIN, "name": "nowrite", "toolsSettings": _WRITE_ALLOWLIST})
    sessions.update({"dashboard:first": "limited", "dashboard:second": "nowrite"})
    create_agent_folder("c1", task="t", agent="worker", parent_session="dashboard:first")
    kas_parent_limits.note_parent("subagent:c1", "dashboard:second")
    held = {r.agent for r in _records(await _batch("worker", "subagent:c1"), "worker")}
    assert held == {"limited", "nowrite"}


@pytest.mark.asyncio
async def test_a_reopened_run_is_still_held_and_reads_its_parents_spec_again(agents, sessions):
    """Each session start of the run reads the parent's file: the running session
    keeps what it read, the next one (a continuation, a restart) reads it fresh."""
    sessions["dashboard:p"] = "limited"
    create_agent_folder("c1", task="t", agent="worker", parent_session="dashboard:p")
    first = await _batch("worker", "subagent:c1")
    kas_parent_limits._NOTED.clear()  # what a restarted gateway holds
    assert _records(await _batch("worker", "subagent:c1"), "worker") == _records(first, "worker")

    changed = {"shell": {"denyByDefault": True, "allowedCommands": ["echo .*", "touch .*"]}}
    _write(agents, {**_LIMITED, "toolsSettings": changed})
    assert _judge(_stamped(_shell("touch marker"), first, "worker")).action == TOOL_DENY
    reopened = await _batch("worker", "subagent:c1")
    assert _judge(_stamped(_shell("touch marker"), reopened, "worker")).action != TOOL_DENY


@pytest.mark.asyncio
async def test_a_native_sub_agent_of_a_held_run_is_held_too(agents, sessions):
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    event = _stamped(_shell("touch marker"), batch, "worker", native_child=True)
    assert _judge(event).tier == "spec-parent-limits"


# ── denies only ──────────────────────────────────────────────────────────────

_WRITE_ALLOWLIST = {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out/**"]}}
_GRANTING = {
    "name": "granting",
    "prompt": "p",
    "tools": ["*"],
    "allowedTools": ["execute_bash", "fs_write", "web_fetch"],
    "permissions": {"rules": [{"capability": "shell", "effect": "allow", "match": ["ls *"]}]},
    "toolsSettings": {
        "shell": {
            "allowedCommands": ["make build", "make clean.*"],
            "deniedCommands": ["make clean", "rm .*"],
        },
        "fs_write": {"allowedPaths": ["src/**"], "deniedPaths": ["secret/**"]},
    },
}


@pytest.mark.asyncio
async def test_no_allow_of_a_parent_reaches_the_child(agents, sessions):
    _write(agents, _GRANTING)
    sessions["dashboard:p"] = "granting"
    own = await _batch("plain", "subagent:c1")
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("plain", "subagent:c1")

    added = [r for r in _rules(batch, "plain") if r not in _rules(own, "plain")]
    assert added and all(r["effect"] == "deny" for r in added)
    assert any(r["capability"] == "shell" and "rm *" in r["match"] for r in added)
    [record] = _records(batch, "plain")
    assert record.denies is not None and record.denies.denied_commands
    assert record.denies.builtins == frozenset() and record.denies.commands == ()
    assert record.denies.web_fetch_trusted == ()

    granted = _stamped(_shell("make build"), batch, "plain")
    assert granted.spec_granted_builtins == frozenset() and granted.spec_granted_commands == ()
    assert granted.spec_granted_paths == () and granted.spec_denied_grant is None
    assert _judge(granted).action != TOOL_AUTO_APPROVE
    denied = _judge(_stamped(_shell("make clean now"), batch, "plain"))
    assert (denied.action, denied.tier) == (TOOL_DENY, "spec-parent-limits")


@pytest.mark.asyncio
async def test_the_childs_own_grants_and_limits_stay(agents, sessions):
    own_spec = {**_GRANTING, "name": "own"}
    _write(agents, own_spec)
    before = await _batch("own", "subagent:c1")
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("own", "subagent:c1")

    stripped = [{k: v for k, v in a.items() if k != CREW_PARENT_LIMITS_KEY} for a in batch]
    assert stripped == before
    assert _judge(_stamped(_shell("make build"), before, "own")).action == TOOL_AUTO_APPROVE
    event = _stamped(_shell("make build"), batch, "own")
    assert event.spec_denied_grant is not None and event.spec_denied_grant.commands
    # Granted by the child's own spec, refused by the parent's allowlist: the deny wins.
    assert _judge(event).tier == "spec-parent-limits"


@pytest.mark.asyncio
async def test_the_record_never_reaches_the_engine(agents, sessions):
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    assert any(CREW_PARENT_LIMITS_KEY in a for a in batch)
    params: dict[str, Any] = {}
    attach_kas_custom_agents(params, batch)
    assert CREW_PARENT_LIMITS_KEY not in json.dumps(params)


@pytest.mark.asyncio
async def test_a_parents_write_allowlist_is_anchored_where_the_parent_runs(
    agents, sessions, monkeypatch, tmp_path
):
    root = Path(os.path.realpath(tmp_path)) / "proj"
    (root / "out").mkdir(parents=True)
    _write(agents, {**_PLAIN, "name": "nowrite", "toolsSettings": _WRITE_ALLOWLIST})
    sessions["dashboard:p"] = "nowrite"
    monkeypatch.setattr(subagent_mod, "parent_project_dirs", lambda _key: (str(root),))
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("plain", "subagent:c1")

    def write(path: str) -> AcpEvent:
        raw = {"path": path, "command": "create"}
        return _kas_builtin_flow("fs_write", title="Write", kind="edit", raw_input=raw)

    outside = _judge(_stamped(write(str(root / "elsewhere.txt")), batch, "plain"))
    assert (outside.action, outside.tier) == (TOOL_DENY, "spec-parent-limits")
    assert "fallbackAction" in outside.reason and "'nowrite'" in outside.reason
    inside = _judge(_stamped(write(str(root / "out" / "a.txt")), batch, "plain"))
    assert inside.tier != "spec-parent-limits"


@pytest.mark.asyncio
async def test_a_blocked_url_of_a_parent_brings_the_childs_fetch_to_the_gate(agents, sessions):
    """The child's own spec runs ``web_fetch`` unasked, so without the host ``ask`` a
    fetch on the parent's blocked URL would never reach the gate that refuses it."""
    entry = r"https://example\.com/private/.*"
    blocked = {"web_fetch": {"blocked": [entry]}}
    _write(agents, {**_PLAIN, "name": "noweb", "toolsSettings": blocked})
    ask = {"capability": "web_fetch", "effect": "ask", "match": ["example.com"]}

    own = await _batch("worker", "subagent:c1")
    assert ask not in _rules(own, "worker")
    assert {"capability": "web_fetch", "effect": "allow"} in _rules(own, "worker")

    sessions["dashboard:p"] = "noweb"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    assert ask in _rules(batch, "worker")
    [record] = _records(batch, "worker")
    assert record.denies.web_fetch_blocked == (entry,)

    fetch = _kas_builtin_flow(
        "web_fetch",
        title="Fetch",
        kind="fetch",
        raw_input={"url": "https://example.com/private/x"},
    )
    verdict = _judge(_stamped(fetch, batch, "worker"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")


@pytest.mark.asyncio
async def test_a_parents_deny_is_read_at_the_gate_whatever_the_parent_grants(agents, sessions):
    """A parent that denies a command and grants no shell has no deny record of its
    own (its calls ask). The child grants the shell itself, so the gate reads the
    parent's deny for it."""
    deny_only = {"shell": {"deniedCommands": ["make clean"]}}
    _write(agents, {**_PLAIN, "name": "denyonly", "toolsSettings": deny_only})
    _write(agents, {**_PLAIN, "name": "shell", "allowedTools": ["execute_bash"]})
    own = await _batch("shell", "subagent:c1")
    assert _judge(_stamped(_shell("make clean now"), own, "shell")).action == TOOL_AUTO_APPROVE

    sessions["dashboard:p"] = "denyonly"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("shell", "subagent:c1")
    [record] = _records(batch, "shell")
    assert record.denies.denied_commands == ("make clean",)
    verdict = _judge(_stamped(_shell("make clean now"), batch, "shell"))
    assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-parent-limits")
    assert _judge(_stamped(_shell("make build"), batch, "shell")).action == TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_deny_the_child_already_has_is_not_added_twice(agents, sessions):
    rule = {"capability": "shell", "effect": "deny", "match": ["rm *"]}
    for name in ("first", "second"):
        _write(agents, {**_PLAIN, "name": name, "permissions": {"rules": [rule]}})
    sessions["dashboard:p"] = "first"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    rules = _rules(await _batch("second", "subagent:c1"), "second")
    assert sum(r == rule for r in rules) == 1


@pytest.mark.asyncio
async def test_an_app_agents_limits_come_from_the_file_its_session_is_bound_to(
    agents, sessions, monkeypatch
):
    from kiro_crew.apps import agent_launch

    bound = {**_LIMITED, "name": "app--agent"}
    monkeypatch.setattr(
        agent_launch,
        "read_bound_app_spec",
        lambda key, agent: bound if (key, agent) == ("dashboard:p", "app--agent") else None,
    )
    sessions["dashboard:p"] = "app--agent"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    assert [r.agent for r in _records(batch, "worker")] == ["app--agent"]


@pytest.mark.asyncio
async def test_a_held_run_that_names_no_agent_is_refused(agents, sessions):
    """A session with no agent registers no entry, so nothing could carry the denies."""
    extras = await KasHarness().session_extras("", work_dir=None, session_key="subagent:c1")
    assert not extras.custom_agents
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    with pytest.raises(AcpAgentUnavailable, match="names no agent"):
        await KasHarness().session_extras("", work_dir=None, session_key="subagent:c1")


@pytest.mark.asyncio
async def test_a_spec_found_from_any_of_the_parents_directories_applies(
    agents, sessions, monkeypatch, tmp_path
):
    """A file left in the Bot folder answers a name only where the folder the
    session runs in does not declare it, so the directory decides which spec the
    parent runs. Every directory the parent may run in is read."""
    mates = tmp_path / "home" / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    _write(mates, {**_LIMITED, "name": "leftover"})
    declares, plain = tmp_path / "declares", tmp_path / "plain"
    project = paths_mod.project_agents_dir(declares)
    project.mkdir(parents=True)
    plain.mkdir()
    _write(project, {**_PLAIN, "name": "leftover"})
    monkeypatch.setattr(
        subagent_mod, "parent_project_dirs", lambda _key: (str(declares), str(plain))
    )
    sessions["dashboard:p"] = "leftover"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("worker", "subagent:c1")
    assert [r.agent for r in _records(batch, "worker")] == ["leftover"]


# ── a parent that cannot be read ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_parent_whose_agent_file_cannot_be_read_refuses_the_child(agents, sessions):
    (agents / "limited.json").write_text("{not json", encoding="utf-8")
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    with pytest.raises(
        AcpAgentUnavailable, match="agent 'limited', under which this sub-agent runs"
    ):
        await _batch("worker", "subagent:c1")


@pytest.mark.asyncio
async def test_a_parent_whose_record_cannot_be_read_refuses_the_child(agents, sessions):
    sessions["dashboard:p"] = subagent_mod.ParentRecordUnreadable("torn")
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    with pytest.raises(AcpAgentUnavailable, match="could not be read"):
        await _batch("worker", "subagent:c1")


@pytest.mark.asyncio
async def test_a_parent_with_no_agent_file_starts_the_child_as_before(agents, sessions):
    before = await _batch("worker", "subagent:c1")
    sessions["dashboard:p"] = "built-in-with-no-file"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _batch("worker", "subagent:c1") == before


# ── other harnesses and shipped agents ───────────────────────────────────────


def test_another_harness_stamps_no_parent_limits():
    handle = _handle(ACP_BACKEND_CLAUDE)
    handle.kas_parent_limits = ("anything",)
    event = handle._build_permission_event(_permission("own-sid"))
    assert event is not None and event.spec_parent_limits == ()


def _shipped_specs() -> list[dict]:
    """Every agent file the repository ships, and the default agent as its installer
    writes it (built, then governed)."""
    from kiro_crew.agent_materialization import auto_approve
    from kiro_crew.agent_parts.spec_build import build_agent_config

    builtins = _REPO / "src" / "kiro_crew" / "apps" / "builtins"
    specs = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(builtins.glob("*/agents/*.json"))
    ]
    default = build_agent_config()
    auto_approve.govern_spec(default, agent_filename="kirocrew.json", source="test")
    return [*specs, {**default, "name": "kirocrew"}]


def test_no_shipped_agent_hands_a_limit_to_a_child(agents, sessions):
    """A child of any shipped parent keeps the entry and the gate record it had."""
    specs = _shipped_specs()
    assert len(specs) > 10
    child = [{"id": "plain", "prompt": "p", "tools": "*"}]
    for spec in specs:
        _write(agents, spec)
        sessions["dashboard:p"] = spec["name"]
        kas_parent_limits._NOTED.clear()
        kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
        assert kas_parent_limits.hold_to_parents(child, "plain", "subagent:c1") is child, spec[
            "name"
        ]


def test_no_spec_kiro_crew_installs_hands_a_limit_to_a_child(tmp_path, monkeypatch, sessions):
    """Every spec a real rebuild writes (the default agent, the conductors, the
    workers, the service agents): a child of any of them keeps the batch it had.

    A Bot's two service agents are left out: its conductor starts no sub-agent, and
    its worker's spec is merged per Bot from what a person added to that Bot.
    """
    from test_agent_install_composition_fail_closed import _Rig

    from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES, is_mate_service_agent

    rig = _Rig(tmp_path, monkeypatch)
    agent_mod.rebuild_agent_config()
    mate = paths_mod.mate_agents_dir()
    installed = sorted(
        {p.stem for d in (rig.agents, mate) if d.is_dir() for p in d.glob("*.json")}
        & OWNED_KIRO_AGENT_NAMES
    )
    installed = [name for name in installed if not is_mate_service_agent(name)]
    assert len(installed) >= 12, installed
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: rig.agents)
    child = [{"id": "plain", "prompt": "p", "tools": "*"}]
    for name in installed:
        sessions["dashboard:p"] = name
        kas_parent_limits._NOTED.clear()
        kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
        assert kas_parent_limits.hold_to_parents(child, "plain", "subagent:c1") is child, name


# ── the warm pool ────────────────────────────────────────────────────────────


async def _pool_decision(key: str, agent: str, backend: str, pool_cwd: str = "") -> list[str]:
    """The warm-pool decision the first start of a kept run of *agent* on *key*
    takes, pool on, its sessions warmed in *pool_cwd* when given. Any other
    sub-agent key is stateless and never reaches the pool."""
    from unittest.mock import AsyncMock, MagicMock

    from test_aws_project_profiles import _cfg, _recording_factory

    from kiro_crew.session import SessionManager

    cfg = _cfg({}, pool_size=2)
    cfg.agent.acp_backend = backend
    manager = SessionManager(cfg, provider_factory=_recording_factory([]))
    manager._drain_and_claim = AsyncMock(return_value=None)
    manager._schedule_replenish = MagicMock()
    if pool_cwd:
        manager._pool_cwd = pool_cwd
    decisions: list[str] = []
    manager._record_pool_decision = lambda decision, _key: decisions.append(decision)
    manager.mark_continuable(key)
    try:
        await manager.get_or_create(key, agent=agent)
    finally:
        await manager.close_all()
    return decisions


@pytest.mark.asyncio
async def test_a_held_run_never_takes_a_pooled_session(agents, sessions):
    """A pooled session's batch was built before the run existed, so it carries no
    parent's denies; a kept run's first start cold-starts instead."""
    sessions["dashboard:p"] = "limited"
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _pool_decision("subagent:c1", "plain", ACP_BACKEND_KAS) == ["bypass_parent_denies"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("parent", "backend"),
    [("plain", ACP_BACKEND_KAS), ("limited", ACP_BACKEND_CLAUDE)],
    ids=["no-limit", "another-harness"],
)
async def test_a_run_that_is_not_held_keeps_its_pool_decision(agents, sessions, parent, backend):
    sessions["dashboard:p"] = parent
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    assert await _pool_decision("subagent:c1", "plain", backend) == ["miss_empty"]
