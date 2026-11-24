"""The ``kas_subagents`` preview off or never set: a person's own agent on KAS projects as before.

The engine offers an agent two kinds of sub-agent tool: ``invoke_sub_agent``, and
one ``subagent_<id>`` per agent its registry holds. An agent file whose ``tools``
is ``"*"`` or ``@builtin`` mounts them without naming them. Off, the preview adds
no rule to any entry, whatever its file says, so such an agent starts engine
sub-agents as it did before the preview existed; on, a person's own agent keeps
the same batch, since only the default agent's chat mounts the helper.

What holds whatever the preview says is the first commit's fix: an engine-run
sub-agent is held to its parent's limits. Kiro Crew's own agents are refused by
name to every entry (``kas_subagent_targets.deny_own_agents``) and the parent's
gate-only limits are stamped on the sub-agent's requests
(``session_handle_permissions``); neither reads the preview.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

import pytest

import kiro_crew.agent as agent_mod
import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES
from kiro_crew.config.loader import KiroCrewConfig

_DASHBOARD = "dashboard:t1"
_TARGET = {"name": "mine-target", "prompt": "p", "tools": ["fs_read"]}

#: Parent agent files, by what their ``tools`` (or the rest of the file) says about
#: a sub-agent tool.
_PARENTS: dict[str, dict] = {
    "star-list": {"tools": ["*"]},
    "star-string": {"tools": "*"},
    "builtin-tag": {"tools": ["@builtin", "@kirocrew-core"]},
    "names-use_subagent": {"tools": ["fs_read", "use_subagent", "@kirocrew-core"]},
    "names-invoke_sub_agent": {"tools": ["fs_read", "invoke_sub_agent"]},
    "names-a-per-agent-tool": {"tools": ["fs_read", "subagent_mine-target"]},
    "names-the-per-agent-tag": {"tools": ["fs_read", "subagent/mine-target"]},
    "short-list": {"tools": ["fs_read", "fs_write", "execute_bash", "@kirocrew-core"]},
    "no-tools-field": {},
    "lists-a-target": {
        "tools": ["fs_read", "use_subagent"],
        "toolsSettings": {"subagent": {"availableAgents": ["mine-target"]}},
    },
    "trusts-a-target": {
        "tools": ["*"],
        "toolsSettings": {
            "subagent": {"availableAgents": ["mine-target"], "trustedAgents": ["mine-target"]}
        },
    },
    # An agent installed from outside carries its own permissions block.
    "own-permissions-block": {
        "tools": ["*"],
        "permissions": {
            "rules": [{"capability": "subagent", "match": ["mine-target"], "effect": "allow"}]
        },
    },
    "excludes-only-spawn_run": {"tools": ["*"], "excludedTools": ["@kirocrew-core/spawn_run"]},
}

#: The one rule that would take every engine sub-agent off an entry.
_EVERY_SUBAGENT = {"capability": "subagent", "effect": "deny"}


@pytest.fixture
def agents_dir(monkeypatch, tmp_path) -> Path:
    user = tmp_path / "home" / "agents"
    user.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: user)
    monkeypatch.setattr(agent_mod, "ensure_agent_materialized", lambda _agent: None)
    return user


@pytest.fixture
def stored(monkeypatch, tmp_path):
    """Write the preview into a real ``config.json`` the projection loads (``None``: no key)."""

    def _store(value: bool | None) -> None:
        home = tmp_path / "crew-home"
        home.mkdir(exist_ok=True)
        previews = {} if value is None else {engine.KAS_SUBAGENTS_PREVIEW: value}
        body = {"dashboard": {"feature_previews": previews}}
        (home / "config.json").write_text(json.dumps(body), encoding="utf-8")
        monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)

    return _store


def _write(directory: Path, spec: dict) -> None:
    (directory / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


async def _batch(agent: str = "mine", key: str = _DASHBOARD, **kw) -> list[dict]:
    extras = await KasHarness().session_extras(agent, work_dir=None, session_key=key, **kw)
    return extras.custom_agents


async def _unswitched(monkeypatch, agent: str = "mine", key: str = _DASHBOARD, **kw):
    """The batch with the preview not acting on it: what the projection built before it."""
    with monkeypatch.context() as m:
        m.setattr(engine, "engine_subagents_on", lambda *a, **k: False)
        return await _batch(agent, key, **kw)


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


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, True], ids=["unset", "off", "on"])
@pytest.mark.parametrize("kind", sorted(_PARENTS))
async def test_a_users_agent_projects_as_before_the_preview(
    agents_dir, stored, monkeypatch, value, kind
):
    _write(agents_dir, {"name": "mine", "prompt": "p", **_PARENTS[kind]})
    _write(agents_dir, _TARGET)
    stored(value)
    batch = await _batch()
    assert batch == await _unswitched(monkeypatch)
    for entry in batch:
        assert _EVERY_SUBAGENT not in _rules(entry), entry["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["unset", "off"])
@pytest.mark.parametrize(
    "key",
    ["dashboard:t1", "dashboard:member-ada", "cron:job-1", "subagent:abc", "slack:C1:T1", ""],
)
async def test_off_adds_no_blanket_rule_in_any_kind_of_kas_session(agents_dir, stored, value, key):
    _write(agents_dir, {"name": "mine", "prompt": "p", "tools": ["*"]})
    stored(value)
    for kw in ({}, {"member_dispatch": True}, {"crew_panel": True}, {"remote_workspace": True}):
        [entry] = await _batch(key=key, **kw)
        assert _EVERY_SUBAGENT not in _rules(entry), (key, kw)
        assert not _refused(entry, "any-other-agent"), (key, kw)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["unset", "off"])
async def test_off_still_refuses_kiro_crews_own_agents_by_name(agents_dir, stored, value):
    """The first commit's fix: a star agent's engine child is never one of Kiro Crew's own."""
    _write(agents_dir, {"name": "mine", "prompt": "p", "tools": ["*"]})
    stored(value)
    [entry] = await _batch()
    assert sorted(n for n in OWNED_KIRO_AGENT_NAMES if not _refused(entry, n)) == []
    # A person's own target stays reachable, as before the preview.
    assert not _refused(entry, "mine-target")


@pytest.mark.parametrize("value", [None, False], ids=["unset", "off"])
def test_off_still_stamps_the_parents_limits_on_an_engine_child(stored, tmp_path, value):
    """The other half of that fix: a restricted star parent's engine child gets its limits."""
    from test_kas_native_child_limits import _SHELL_ALLOWLIST, _judge, _requests, _shell

    from kiro_crew.hooks import TOOL_DENY

    stored(value)
    assert engine.kas_subagents_preview_on(KiroCrewConfig.load()) is False
    work = Path(os.path.realpath(tmp_path)) / "proj"
    (work / "out").mkdir(parents=True)
    parent, child = _requests(_shell("ls /"), _SHELL_ALLOWLIST, ["*"], work)
    for event in (parent, child):
        verdict = _judge(event)
        assert (verdict.action, verdict.tier) == (TOOL_DENY, "spec-shell-allowlist")


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False, True], ids=["unset", "off", "on"])
async def test_a_file_that_excludes_the_sub_agent_tool_starts_none_either_way(
    agents_dir, stored, value
):
    """The excludedTools hold does not read the preview."""
    _write(
        agents_dir,
        {"name": "mine", "prompt": "p", "tools": ["*"], "excludedTools": ["use_subagent"]},
    )
    stored(value)
    [entry] = await _batch()
    assert _EVERY_SUBAGENT in _rules(entry)


def test_the_parent_limit_fix_never_reads_the_preview():
    """Nothing that holds a child to its parent asks whether the preview is on."""
    root = Path(engine.__file__).parents[1]
    for rel in (
        "acp/kas_subagent_targets.py",
        "acp/session_handle_permissions.py",
        "acp/kas_child_finished.py",
        "acp/kas_engine_subagent_target.py",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "kas_engine_subagents" not in text and "kas_subagents" not in text, rel
