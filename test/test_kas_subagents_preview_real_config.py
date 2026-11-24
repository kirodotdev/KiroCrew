"""The ``kas_subagents`` preview read from a real ``config.json``, as a person stores it.

Unset and off, the shipped default agent mounts no engine sub-agent tool and no
helper, and a person's ``tools: ["*"]`` agent gets no rule that refuses every
engine sub-agent. A stored ``agent.subagents`` key moves nothing: one stored
setting decides. On, the default agent mounts the helper.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp.harness import kas as kas_harness
from kiro_crew.acp.harness.kas import KasHarness

_DEFAULTS = Path(kas_harness.__file__).parents[2] / "config" / "defaults.json"
_HELPER = "kirocrew-helper"
_EVERY_SUBAGENT = {"capability": "subagent", "effect": "deny"}


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    agents = tmp_path / "home" / "agents"
    agents.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    crew = tmp_path / "crew-home"
    crew.mkdir()
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: crew)
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    default = {
        "name": "kirocrew",
        "prompt": "p",
        "tools": shipped["tools"],
        "allowedTools": shipped["allowedTools"],
    }
    (agents / "kirocrew.json").write_text(json.dumps(default), encoding="utf-8")
    (agents / "mine.json").write_text(
        json.dumps({"name": "mine", "prompt": "p", "tools": ["*"]}), encoding="utf-8"
    )
    return crew


def _store(crew: Path, body: dict) -> None:
    (crew / "config.json").write_text(json.dumps(body), encoding="utf-8")


async def _batch(agent: str) -> dict[str, dict]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key="dashboard:t1")
    return {a["id"]: a for a in extras.custom_agents}


def _mounts_engine_tool(entry: dict) -> bool:
    tools = entry.get("tools")
    return isinstance(tools, list) and "invoke_sub_agent" in tools


def _rules(entry: dict) -> list:
    return (entry.get("permissions") or {}).get("rules") or []


_OFF_SHAPES = {
    "unset": {},
    "off": {"dashboard": {"feature_previews": {"kas_subagents": False}}},
    "retired-key-crew": {"agent": {"subagents": "crew"}},
    "retired-key-engine": {"agent": {"subagents": "engine"}},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_OFF_SHAPES))
async def test_unset_or_off_mounts_nothing_on_the_default_agent(home, shape):
    _store(home, _OFF_SHAPES[shape])
    batch = await _batch("kirocrew")
    assert _HELPER not in batch
    assert not _mounts_engine_tool(batch["kirocrew"])
    assert "@kirocrew-core/spawn_sub_agents" not in (batch["kirocrew"].get("excludedTools") or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_OFF_SHAPES))
async def test_unset_or_off_adds_no_blanket_rule_to_a_star_agent(home, shape):
    _store(home, _OFF_SHAPES[shape])
    batch = await _batch("mine")
    assert _EVERY_SUBAGENT not in _rules(batch["mine"])


@pytest.mark.asyncio
async def test_on_mounts_the_helper_on_the_default_agent(home):
    _store(home, {"dashboard": {"feature_previews": {"kas_subagents": True}}})
    batch = await _batch("kirocrew")
    assert _HELPER in batch
    assert _mounts_engine_tool(batch["kirocrew"])
    assert "@kirocrew-core/spawn_sub_agents" in batch["kirocrew"]["excludedTools"]


@pytest.mark.asyncio
async def test_on_leaves_a_star_agent_without_the_blanket_rule(home):
    _store(home, {"dashboard": {"feature_previews": {"kas_subagents": True}}})
    batch = await _batch("mine")
    assert _HELPER not in batch
    assert _EVERY_SUBAGENT not in _rules(batch["mine"])
