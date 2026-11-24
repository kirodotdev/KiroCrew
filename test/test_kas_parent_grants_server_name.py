"""A founding agent's grant by MCP server name reaches the run's own server of that name.

With the KAS sub-agents preview on, as on kiro-cli: the starting agent approved the
name, so the sub-agent's own server of that name answers unasked, set up the same way
or not, and whether or not the starting agent holds a server of that name. What names
no server (``*``, a server's ``autoApprove`` verbs, an ``mcp`` allow with no
``match``) still needs the same server on both sides, and every ask, deny and hold
decides as it did.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _BASE,
    _batch,
    _child,
    _isolated,
    agents,
    preview,
    sessions,
    stored,
)
from test_kas_parent_limits import _rules, _write

import kiro_crew.config.paths as paths_mod
from kiro_crew import mate_worker
from kiro_crew.acp import kas_agents, kas_parent_grants, kas_parent_limits

_THEIRS = {"command": "srv", "args": ["a"]}
_OURS = {"command": "other", "args": ["a"]}
#: How the two files hold ``casrv``: the founder's entry and the run's.
_SETUPS = {
    "another-command": (_THEIRS, _OURS),
    "founder-holds-none": (None, _OURS),
}
_ASK = {"capability": "mcp", "effect": "ask", "match": ["casrv/look"]}
_DENY = {"capability": "mcp", "effect": "deny", "match": ["casrv/drop"]}


@pytest.fixture(autouse=True)
def _no_crew_servers(_floor_monkeypatch):
    _floor_monkeypatch.setattr(kas_agents, "_project_mcp_servers", lambda *a, **k: {})


def _spec(name: str, server: Any = None, **keys: Any) -> dict[str, Any]:
    held = {"mcpServers": {"casrv": server}} if server is not None else {}
    return {**_BASE, "name": name, **held, **keys}


def _mcp(batch: list[dict], agent: str) -> set[str]:
    """What the engine runs unasked on an MCP server for *agent*."""
    return {
        match
        for rule in _rules(batch, agent)
        if rule.get("effect") == "allow" and rule.get("capability") == "mcp"
        for match in rule.get("match") or ["*"]
    }


# ── a grant that names the server ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", sorted(_SETUPS))
@pytest.mark.parametrize(
    "grant, reaches",
    [
        ({"allowedTools": ["@casrv/look"]}, "casrv/look"),
        (
            {
                "permissions": {
                    "rules": [{"capability": "mcp", "effect": "allow", "match": ["casrv/*"]}]
                }
            },
            "casrv/*",
        ),
    ],
    ids=["one-tool-listed", "an-allow-rule"],
)
async def test_each_way_of_naming_the_server_reaches_the_runs_own(
    agents, sessions, preview, setup, grant, reaches  # noqa: F811
):
    theirs, ours = _SETUPS[setup]
    _write(agents, _spec("names", theirs, **grant))
    _write(agents, _spec("holds", ours))

    assert _mcp(await _child(sessions, "names", "holds"), "holds") == {reaches}


def test_the_rule_reads_the_runs_own_servers_only():
    reaches = kas_parent_grants.server_grant_reaches

    assert reaches("casrv", {"mcpServers": {"casrv": _OURS}})
    assert not reaches("casrv", {"mcpServers": {"other": _OURS}})
    assert not reaches("casrv", {})
    assert not reaches("casrv", {"mcpServers": {"casrv": "not an entry"}})


@pytest.mark.asyncio
async def test_two_founders_that_both_grant_the_name_hand_it_down(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"]))
    _write(agents, _spec("names-too", allowedTools=["@casrv"]))
    _write(agents, _spec("holds", _OURS))
    kas_parent_limits.note_parent("subagent:c1", "dashboard:q")
    sessions["dashboard:q"] = "names-too"

    assert _mcp(await _child(sessions, "names", "holds"), "holds") == {"casrv/*"}


# ── what did not change ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grant, alike",
    [
        ({"allowedTools": ["*"]}, set()),
        ({"permissions": {"rules": [{"capability": "mcp", "effect": "allow"}]}}, set()),
        ({"mcpServers": {"casrv": {**_THEIRS, "autoApprove": ["look"]}}}, {"casrv/look"}),
    ],
    ids=["a-star", "an-allow-rule-with-no-match", "auto-approve-verbs"],
)
@pytest.mark.parametrize("ours", [_THEIRS, _OURS], ids=["alike", "another-command"])
async def test_a_grant_that_names_no_server_reaches_only_a_server_held_alike(
    agents, sessions, preview, grant, alike, ours  # noqa: F811
):
    """A server's ``autoApprove`` verbs travel only to the same server. ``*`` and an
    allow rule with no ``match`` hand down no MCP grant, alike or not."""
    _write(agents, _spec("wide", _THEIRS, **grant))
    _write(agents, _spec("holds", ours))

    reached = _mcp(await _child(sessions, "wide", "holds"), "holds")
    assert reached == (alike if ours == _THEIRS else set())


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", sorted(_SETUPS))
async def test_a_founders_deny_on_that_server_is_on_the_runs_entry(
    agents, sessions, preview, setup  # noqa: F811
):
    theirs, ours = _SETUPS[setup]
    _write(agents, _spec("names", theirs, allowedTools=["@casrv"], permissions={"rules": [_DENY]}))
    _write(agents, _spec("holds", ours))

    assert _DENY in _rules(await _child(sessions, "names", "holds"), "holds")


@pytest.mark.asyncio
@pytest.mark.parametrize("setup", sorted(_SETUPS))
async def test_the_runs_own_ask_and_deny_on_that_server_stay(
    agents, sessions, preview, setup  # noqa: F811
):
    theirs, ours = _SETUPS[setup]
    _write(agents, _spec("names", theirs, allowedTools=["@casrv"]))
    _write(agents, _spec("holds", ours, permissions={"rules": [_ASK, _DENY]}))

    rules = _rules(await _child(sessions, "names", "holds"), "holds")
    assert _ASK in rules and _DENY in rules


@pytest.mark.asyncio
async def test_an_ask_on_that_server_of_an_agent_above_reaches_the_run(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"]))
    _write(agents, _spec("middle", permissions={"rules": [_ASK]}))
    _write(agents, _spec("holds", _OURS))
    sessions["dashboard:p"], sessions["subagent:mid"] = "names", "middle"
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent("subagent:leaf", "subagent:mid")

    assert _ASK in _rules(await _batch("holds", "subagent:leaf"), "holds")


@pytest.mark.asyncio
async def test_a_second_founder_that_does_not_grant_the_name_withholds_it(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"]))
    _write(agents, _spec("silent", _THEIRS))
    _write(agents, _spec("holds", _OURS))
    kas_parent_limits.note_parent("subagent:c1", "dashboard:q")
    sessions["dashboard:q"] = "silent"

    assert _mcp(await _child(sessions, "names", "holds"), "holds") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "theirs, ours, kept",
    [(True, False, set()), (False, True, set()), (True, True, {"casrv/*"})],
    ids=["founder-only", "worker-only", "both"],
)
async def test_a_bots_worker_keeps_a_server_grant_both_it_and_its_founder_hold(
    agents, sessions, preview, monkeypatch, tmp_path, theirs, ours, kept  # noqa: F811
):
    worker = mate_worker.MATE_WORKER_AGENT
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr(paths_mod, "mate_agents_dir", lambda: mates)
    monkeypatch.setattr(mate_worker, "mate_file_for_member", lambda member_id, config=None: "pixel")
    monkeypatch.setattr(
        "kiro_crew.execution_context.read_session_execution",
        lambda key: SimpleNamespace(member_id="pixel"),
    )
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.kas_model_catalog.vault_identity", lambda *_a: None, raising=False
    )
    _write(mates, {"name": "pixel", "prompt": ""})
    _write(agents, _spec(worker, _OURS, allowedTools=["@casrv"] if ours else []))
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"] if theirs else []))

    assert _mcp(await _child(sessions, "names", worker), worker) == kept


@pytest.mark.asyncio
async def test_a_founders_hook_the_run_lacks_withholds_the_server_grant(
    agents, sessions, preview  # noqa: F811
):
    guard = {"preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}]}
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"], hooks=guard))
    _write(agents, _spec("holds", _OURS))

    assert _mcp(await _child(sessions, "names", "holds"), "holds") == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False], ids=["never-set", "off"])
@pytest.mark.parametrize("own, kept", [([], set()), (["@casrv"], {"casrv/*"})], ids=["no", "own"])
async def test_preview_off_a_run_keeps_its_own_server_grants_and_gains_none(
    agents, sessions, stored, value, own, kept  # noqa: F811
):
    stored(value)
    _write(agents, _spec("names", _THEIRS, allowedTools=["@casrv"]))
    _write(agents, _spec("holds", _OURS, allowedTools=own))

    assert _mcp(await _child(sessions, "names", "holds"), "holds") == kept
