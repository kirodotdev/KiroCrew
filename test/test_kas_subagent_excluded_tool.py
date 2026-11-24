"""An agent file that excludes the sub-agent tool starts no KAS sub-agent.

``excludedTools`` naming the sub-agent tool (kiro-cli's ``use_subagent`` or
``subagent``, KAS's ``invoke_sub_agent``, or a tag or pattern that covers it)
takes ``invoke_sub_agent`` off the agent. The engine also gives the agent one
``subagent_<id>`` tool per agent its registry holds, which carries neither that
id nor the ``@builtin`` tag, so the exclusion leaves those mounted and the agent
starts a child through one of them. The projection therefore gives such an entry
the rule the engine takes every sub-agent tool off the tool list for and refuses
a call by. An agent file that does not exclude the tool is unchanged.

The ``kas_subagents`` preview is on throughout. There, a person's own
``tools: ["*"]`` file whose exclusion is one of the two lists run on kiro-cli is
read as kiro-cli read it instead (``test_kas_excluded_tool_cli.py``), so those two
rows are left out of the matrix below.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_subagent_targets as targets
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.config.loader import KiroCrewConfig

_NAMES = ("mine-target", "kirocrew-worker", "kirocrew-helper", "any-other-agent")
_SPAWNS = ["@kirocrew-core/spawn_run", "@kirocrew-core/spawn_sub_agents"]

#: ``excludedTools`` lists that say "no sub-agent tool".
_EXCLUDES_THE_TOOL: dict[str, list[str]] = {
    "all-four-names": ["use_subagent", "invoke_sub_agent", *_SPAWNS],
    "use_subagent": ["use_subagent"],
    "invoke_sub_agent": ["invoke_sub_agent"],
    "kiro-cli-short-spelling": ["subagent"],
    "builtin-tag": ["@builtin"],
    "subagent-tag": ["@subagent"],
    "pattern": ["invoke_*"],
    "everything": ["*"],
}

#: The lists a ``tools: ["*"]`` file is read as kiro-cli reads it for, preview on.
_READ_AS_KIRO_CLI = frozenset({"all-four-names", "kiro-cli-short-spelling"})

#: Every other pairing of a list and a ``tools`` value.
_HELD = [
    (kind, tools)
    for kind in sorted(_EXCLUDES_THE_TOOL)
    for tools in (["*"], "*", ["@builtin", "@kirocrew-core"])
    if not (tools == ["*"] and kind in _READ_AS_KIRO_CLI)
]

#: ``excludedTools`` lists that do not.
_KEEPS_THE_TOOL: dict[str, list[str] | None] = {
    "nothing-excluded": None,
    "only-kiro-crews-spawn-tools": _SPAWNS,
    "another-built-in": ["fs_write"],
    "one-per-agent-tool": ["subagent_mine-target"],
    "a-server": ["@some-server"],
}


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


@pytest.fixture(autouse=True)
def _engine_value(_floor_monkeypatch):
    """Turn the ``kas_subagents`` preview on: this hold is not the switch's."""
    cfg = KiroCrewConfig()
    cfg.dashboard.feature_previews = {"kas_subagents": True}
    _floor_monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))


def _write(directory: Path, name: str, **spec) -> None:
    body = {"name": name, "prompt": "p", **{k: v for k, v in spec.items() if v is not None}}
    (directory / f"{name}.json").write_text(json.dumps(body), encoding="utf-8")


async def _batch(agent: str = "mine", key: str = "dashboard:t1") -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=key)
    return extras.custom_agents


async def _unheld(monkeypatch, agent: str = "mine") -> list[dict]:
    """The batch with this hold switched off: what the projection built without it."""
    with monkeypatch.context() as m:
        m.setattr(targets, "deny_excluded_subagent_tool", lambda batch: batch, raising=False)
        return await _batch(agent)


def _rules(entry: dict) -> list[dict]:
    return (entry.get("permissions") or {}).get("rules") or []


def _refused(entry: dict, name: str) -> bool:
    """Whether *entry* denies a sub-agent call naming *name*, as KAS reads a rule."""
    return any(
        r.get("capability") == "subagent"
        and r.get("effect") == "deny"
        and any(fnmatch.fnmatchcase(name, p) for p in r.get("match") or ["*"])
        and not any(fnmatch.fnmatchcase(name, p) for p in r.get("exclude") or [])
        for r in _rules(entry)
    )


def _takes_the_tools_off(entry: dict) -> bool:
    """Whether KAS drops every ``subagent`` tool from *entry*'s tool list: a
    ``deny`` with no ``exclude`` whose ``match`` is absent, empty or ``["*"]``."""
    return any(
        r.get("capability") == "subagent"
        and r.get("effect") == "deny"
        and r.get("match") in (None, [], ["*"])
        and not r.get("exclude")
        for r in _rules(entry)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,tools", _HELD, ids=str)
async def test_a_file_that_excludes_the_tool_starts_no_engine_sub_agent(
    agents_dir, monkeypatch, kind, tools
):
    _write(agents_dir, "mine", tools=tools, excludedTools=_EXCLUDES_THE_TOOL[kind])
    _write(agents_dir, "mine-target", tools=["fs_read"])

    [entry] = await _batch()
    [was] = await _unheld(monkeypatch)

    assert _takes_the_tools_off(entry)
    assert [n for n in _NAMES if not _refused(entry, n)] == []
    # Only a rule was added, after the ones the entry had.
    assert {k: v for k, v in entry.items() if k != "permissions"} == {
        k: v for k, v in was.items() if k != "permissions"
    }
    assert _rules(entry) == [*_rules(was), {"capability": "subagent", "effect": "deny"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", sorted(_KEEPS_THE_TOOL))
async def test_a_file_that_keeps_the_tool_is_unchanged(agents_dir, monkeypatch, kind):
    _write(agents_dir, "mine", tools=["*"], excludedTools=_KEEPS_THE_TOOL[kind])
    _write(agents_dir, "mine-target", tools=["fs_read"])

    batch = await _batch()

    assert batch == await _unheld(monkeypatch)
    assert not _takes_the_tools_off(batch[0])
    assert not _refused(batch[0], "mine-target")


@pytest.mark.asyncio
async def test_a_registered_target_that_excludes_the_tool_is_held_too(agents_dir):
    _write(
        agents_dir,
        "mine",
        tools=["fs_read", "use_subagent"],
        toolsSettings={"subagent": {"availableAgents": ["mine-target"]}},
    )
    _write(agents_dir, "mine-target", tools=["*"], excludedTools=["use_subagent"])

    batch = {a["id"]: a for a in await _batch()}

    assert _takes_the_tools_off(batch["mine-target"])
    # The parent still reaches the target it lists.
    assert not _takes_the_tools_off(batch["mine"])
    assert not _refused(batch["mine"], "mine-target")


@pytest.mark.asyncio
async def test_the_hold_is_recorded_in_the_security_event_log(agents_dir, monkeypatch):
    import types

    events: list[dict] = []
    monkeypatch.setattr(
        targets,
        "sel",
        lambda: types.SimpleNamespace(log_api_access=lambda **kw: events.append(kw)),
    )
    _write(agents_dir, "mine", tools=["*"], excludedTools=["use_subagent"])

    await _batch()

    [event] = [e for e in events if e["operation"] == "kas_subagent_tool_excluded"]
    assert event["source"] == "kas_agent_projection"
    assert event["resources"] == (
        "every engine sub-agent denied (its excludedTools withhold the sub-agent tool) "
        "for agent mine"
    )
