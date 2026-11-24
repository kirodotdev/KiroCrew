"""Kiro Crew's own agents are never a KAS agent's native sub-agents.

KAS's ``invoke_sub_agent`` runs any name its session registry holds: the
``customAgents`` batch and every on-disk profile the engine keeps, which includes
the specs Kiro Crew generates. So the projection gives each entry that mounts the
tool a ``subagent`` deny naming Kiro Crew's own agents, with or without an
``availableAgents`` list, and a user's own agents stay as reachable as before.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_subagent_targets import ENGINE_WIDE_AGENTS
from kiro_crew.agent_files import OWNED_KIRO_AGENT_FILES

#: Every agent Kiro Crew writes a spec for, by name.
OWNED_KIRO_AGENT_NAMES = frozenset(f.removesuffix(".json") for f in OWNED_KIRO_AGENT_FILES)

_HELPER = {
    "name": "helper",
    "description": "The user's own plain helper",
    "prompt": "You are the helper.",
    "tools": ["fs_read"],
}


def _boss(**extra) -> dict:
    return {
        "name": "boss",
        "prompt": "Hand every task to a sub-agent.",
        "tools": ["fs_read", "use_subagent"],
        **extra,
    }


def _listing(*available: str) -> dict:
    return {"toolsSettings": {"subagent": {"availableAgents": list(available)}}}


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch() -> tuple[dict[str, dict], str]:
    extras = await KasHarness().session_extras("boss", work_dir=None)
    return {a["id"]: a for a in extras.custom_agents}, extras.withheld_credentials_notice


def _subagent_rules(entry: dict) -> list[dict]:
    rules = (entry.get("permissions") or {}).get("rules") or []
    return [r for r in rules if r.get("capability") == "subagent"]


def _refused(entry: dict, name: str) -> bool:
    """Whether *entry*'s ``subagent`` rules deny *name*, read as KAS reads a rule:
    a deny applies to a name one ``match`` pattern covers and no ``exclude`` does."""
    return any(
        rule.get("effect") == "deny"
        and any(fnmatch.fnmatchcase(name, p) for p in rule.get("match") or ["*"])
        and not any(fnmatch.fnmatchcase(name, p) for p in rule.get("exclude") or [])
        for rule in _subagent_rules(entry)
    )


def test_the_own_agent_names_are_the_specs_kiro_crew_writes():
    from kiro_crew import agent_files

    assert agent_files.OWNED_KIRO_AGENT_NAMES == OWNED_KIRO_AGENT_NAMES
    assert {"kirocrew", "kirocrew-lite", "kirocrew-worker", "kirocrew-heartbeat"} <= (
        OWNED_KIRO_AGENT_NAMES
    )


@pytest.mark.asyncio
async def test_without_a_list_no_own_agent_can_be_run_as_a_sub_agent(agents_dir):
    _write(agents_dir, _boss())
    _write(agents_dir, _HELPER)

    batch, notice = await _batch()

    assert list(batch) == ["boss"]
    assert [
        name for name in sorted(OWNED_KIRO_AGENT_NAMES) if not _refused(batch["boss"], name)
    ] == []
    # Kiro Crew's own agents are refused without a word: the notice names only the
    # engine's wide agents this narrowed parent is refused.
    assert "cannot hand work to general-task-execution" in notice
    assert not any(name in notice for name in OWNED_KIRO_AGENT_NAMES)


@pytest.mark.asyncio
async def test_without_a_list_the_users_own_agent_is_still_reachable(agents_dir):
    _write(agents_dir, _boss())
    _write(agents_dir, _HELPER)

    batch, _notice = await _batch()

    assert not _refused(batch["boss"], "helper")
    # Rules naming agents, never a deny over every name: the engine's wide agents
    # this narrowed parent is refused, then Kiro Crew's own agents.
    assert [r.get("match") for r in _subagent_rules(batch["boss"])] == [
        sorted(ENGINE_WIDE_AGENTS),
        sorted(OWNED_KIRO_AGENT_NAMES),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "own",
    ["kirocrew-lite", "kirocrew-worker", "kirocrew-mate-worker"],
    ids=["service", "derived", "mate-service"],
)
async def test_a_list_naming_an_own_agent_neither_registers_nor_runs_it(agents_dir, own):
    _write(agents_dir, _boss(**_listing(own, "helper")))
    _write(agents_dir, _HELPER)
    _write(agents_dir, {**_HELPER, "name": own})

    batch, notice = await _batch()

    assert list(batch) == ["boss", "helper"]
    assert f"{own} (it is one of Kiro Crew's own agents)" in notice
    # The list takes the name out of its own deny; this one still refuses the call.
    assert _refused(batch["boss"], own)
    assert not _refused(batch["boss"], "helper")


@pytest.mark.asyncio
async def test_a_registered_target_that_mounts_the_tool_is_held_to_the_same_rule(agents_dir):
    _write(agents_dir, _boss(**_listing("helper")))
    _write(agents_dir, {**_HELPER, "tools": ["fs_read", "use_subagent"]})

    batch, _notice = await _batch()

    assert _refused(batch["helper"], "kirocrew-worker")
    assert not _refused(batch["helper"], "boss")


@pytest.mark.asyncio
async def test_an_agent_that_does_not_mount_the_tool_gets_only_the_own_agents_rule(
    agents_dir, monkeypatch
):
    from kiro_crew.acp import kas_subagent_targets

    _write(agents_dir, _boss(tools=["fs_read"]))
    with monkeypatch.context() as m:
        m.setattr(kas_subagent_targets, "deny_own_agents", lambda batch: batch)
        before, _ = await _batch()

    batch, _notice = await _batch()

    entry = batch["boss"]
    assert _subagent_rules(entry) == [
        {"capability": "subagent", "match": sorted(OWNED_KIRO_AGENT_NAMES), "effect": "deny"}
    ]
    # Only the rule is added: tools, excludedTools and servers are what they were.
    for key in ("tools", "excludedTools", "mcpServers"):
        assert entry.get(key) == before["boss"].get(key), key


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tools",
    [["fs_read", "subagent_kirocrew-worker"], ["fs_read", "subagent/*"]],
    ids=["per-agent-tool", "per-agent-pattern"],
)
async def test_an_agent_that_selects_only_per_agent_tools_cannot_run_an_own_agent(
    agents_dir, tools
):
    # The engine's subagent_<id> tool runs through the same name check as
    # invoke_sub_agent, so the owned-name deny reaches it too.
    _write(agents_dir, _boss(tools=tools))

    batch, _notice = await _batch()

    assert [n for n in sorted(OWNED_KIRO_AGENT_NAMES) if not _refused(batch["boss"], n)] == []


@pytest.mark.asyncio
async def test_a_refused_target_is_denied_to_a_parent_that_selects_only_its_per_agent_tool(
    agents_dir,
):
    # A target refused for a limit only the gate applies stays runnable from disk by
    # its per-agent tool; the parent's refusal deny must hold without invoke_sub_agent.
    _write(agents_dir, _boss(tools=["fs_read", "subagent_guarded"], **_listing("guarded")))
    _write(
        agents_dir,
        {**_HELPER, "name": "guarded", "hooks": {"preToolUse": [{"matcher": "*", "command": "x"}]}},
    )

    batch, notice = await _batch()

    assert list(batch) == ["boss"]
    assert _refused(batch["boss"], "guarded")
    # Nothing is registered or announced for a parent without invoke_sub_agent.
    assert "guarded" not in notice


@pytest.mark.asyncio
async def test_a_parent_without_the_tool_releases_each_target_it_projected(agents_dir, monkeypatch):
    # Its targets are only classified for the refusal deny, never registered, so what
    # their projection wrote (launch snapshots, Connection bridges) is released.
    from kiro_crew.acp import kas_env_launch

    released: list[str] = []
    real = kas_env_launch.discard_batch

    def _record(custom_agents, written=()):
        released.extend(str(a.get("id")) for a in custom_agents if isinstance(a, dict))
        return real(custom_agents, written)

    monkeypatch.setattr(kas_env_launch, "discard_batch", _record)
    _write(agents_dir, _boss(tools=["fs_read", "subagent_helper"], **_listing("helper")))
    _write(agents_dir, _HELPER)

    batch, _notice = await _batch()

    assert list(batch) == ["boss"]
    assert "helper" in released


@pytest.mark.asyncio
async def test_each_own_agent_deny_is_recorded_in_the_security_event_log(agents_dir, monkeypatch):
    import types

    from kiro_crew.acp import kas_subagent_targets

    _write(agents_dir, _boss(**_listing("helper")))
    _write(agents_dir, {**_HELPER, "tools": ["fs_read", "use_subagent"]})
    events: list[dict] = []
    monkeypatch.setattr(
        kas_subagent_targets,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )

    await _batch()

    recorded = [e for e in events if e["operation"] == "kas_subagent_own_agents_denied"]
    assert {e["source"] for e in recorded} == {"kas_agent_projection"}
    count = len(OWNED_KIRO_AGENT_NAMES)
    assert sorted(e["resources"] for e in recorded) == [
        f"sub-agent tool denied {count} of Kiro Crew's own agents for agent boss",
        f"sub-agent tool denied {count} of Kiro Crew's own agents for agent helper",
    ]


@pytest.mark.asyncio
async def test_an_agent_that_does_not_mount_the_tool_records_one_deny(agents_dir, monkeypatch):
    import types

    from kiro_crew.acp import kas_subagent_targets

    _write(agents_dir, _boss(tools=["fs_read"]))
    events: list[dict] = []
    monkeypatch.setattr(
        kas_subagent_targets,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )

    await _batch()

    recorded = [e for e in events if e["operation"] == "kas_subagent_own_agents_denied"]
    assert [e["resources"] for e in recorded] == [
        f"sub-agent tool denied {len(OWNED_KIRO_AGENT_NAMES)} of Kiro Crew's own agents"
        " for agent boss"
    ]
