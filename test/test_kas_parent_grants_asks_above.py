"""An ``ask`` of a session in between holds for a run of its founding chat's own agent.

With the KAS sub-agents preview on, a run whose founding chats all run the run's own
agent keeps its own permission half. The ``ask`` rules of the sessions above it still
reach it: a chat on one agent starts an agent that asks, which starts the first agent
again, and that run asks as a run of any other agent does. A run nothing above asks
more of than its own file does is left byte for byte as it is.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _ASKS,
    _BASE,
    _BROAD,
    _SHELL_ASK,
    _UNREADABLE_RULES,
    _allows,
    _answer,
    _batch,
    _isolated,
    _own,
    agents,
    preview,
    sessions,
    stored,
)
from test_kas_parent_limits import _rules, _write

import kiro_crew.config.paths as paths_mod
import kiro_crew.subagent as subagent_mod
from kiro_crew import mate_worker
from kiro_crew.acp import kas_agents, kas_parent_grants, kas_parent_limits
from kiro_crew.acp.harness.kas import KasHarness
from kiro_crew.acp.kas_permissions import _MAX_RULES, CREW_ANSWERED_BUILTINS_KEY
from kiro_crew.acp.session_handle import AcpAgentUnavailable
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY

_LEAF = "subagent:leaf"
_FETCH_ASK = {"capability": "web_fetch", "effect": "ask"}
#: Runs the shell unasked beside an ask of its own on fetching.
_CAREFUL = {**_BROAD, "name": "careful", "permissions": {"rules": [_FETCH_ASK]}}
#: Runs the shell unasked and refuses one command.
_REFUSES = {
    **_BROAD,
    "name": "refuses",
    "toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}},
}
#: Asks before the shell and refuses one command.
_ASKS_AND_REFUSES = {
    **_ASKS,
    "name": "asks-and-refuses",
    "toolsSettings": {"shell": {"deniedCommands": ["touch secret.*"]}},
}


async def _chain(known, root: str, middle: str, leaf: str) -> list[dict]:
    """The batch of a run of *leaf* that a run of *middle* started under a chat on *root*."""
    known["dashboard:p"], known["subagent:mid"] = root, middle
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent(_LEAF, "subagent:mid")
    return await _batch(leaf, _LEAF)


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["broad", "plain"], ids=["the-founders-agent", "another-agent"])
async def test_an_ask_between_reaches_a_run_whichever_agent_it_names(
    agents, sessions, preview, leaf  # noqa: F811
):
    """The impostor cell: the agent that asks starts its founder's agent, whose own
    file grants the shell, and the write still asks."""
    batch = await _chain(sessions, "broad", "asks", leaf)

    assert _SHELL_ASK in _rules(batch, leaf)
    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, leaf)
    assert _answer(batch, leaf).action != TOOL_AUTO_APPROVE
    assert kas_parent_limits.run_is_held(_LEAF, leaf) is True


@pytest.mark.asyncio
async def test_the_run_keeps_its_own_half_beside_the_ask_above(
    agents, sessions, preview  # noqa: F811
):
    """Only the ask is added: what the run's own file grants outside it still runs."""
    _write(agents, {**_ASKS, "name": "asks-to-fetch", "permissions": {"rules": [_FETCH_ASK]}})

    batch = await _chain(sessions, "wide", "asks", "wide")
    assert _allows(batch, "wide") == {"web_fetch"}
    assert _answer(batch, "wide").action != TOOL_AUTO_APPROVE

    batch = await _chain(sessions, "wide", "asks-to-fetch", "wide")
    assert _FETCH_ASK in _rules(batch, "wide")
    assert _answer(batch, "wide").action == TOOL_AUTO_APPROVE


def test_the_composed_spec_is_the_runs_own_with_the_asks_above_appended(
    agents, sessions, preview  # noqa: F811
):
    sessions["dashboard:p"], sessions["subagent:mid"] = "careful", "asks"
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent(_LEAF, "subagent:mid")
    _write(agents, _CAREFUL)
    own = kas_parent_limits.parent_spec("careful", _LEAF, "")

    spec, found = kas_parent_grants.founding_grants_spec(own, "careful", _LEAF, [])

    assert spec == {**own, "permissions": {"rules": [_FETCH_ASK, _SHELL_ASK]}}
    # Its entry is the projection of that spec: no founder limits it.
    assert (found.applies, found.kept, found.swapped) == (True, True, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "root, middle",
    [("broad", "narrow"), ("careful", "narrow"), ("careful", "careful"), ("careful", "")],
    ids=["no-ask-above", "the-founders-own-ask", "the-same-ask-between", "a-direct-child"],
)
async def test_a_run_nothing_above_asks_more_of_is_left_as_it_is(
    agents, sessions, stored, monkeypatch, root, middle  # noqa: F811
):
    _write(agents, _CAREFUL)
    sessions["dashboard:p"] = root
    if middle:
        sessions["subagent:mid"] = middle
        kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent(_LEAF, "subagent:mid" if middle else "dashboard:p")
    before = await _batch(root, _LEAF)

    halves: list[str] = []
    real = kas_parent_grants._half
    monkeypatch.setattr(
        kas_parent_grants, "_half", lambda *a, **k: halves.append("x") or real(*a, **k)
    )
    stored(True)
    own = kas_parent_limits.parent_spec(root, _LEAF, "")
    spec, found = kas_parent_grants.founding_grants_spec(own, root, _LEAF, [])

    assert spec is own and found is kas_parent_grants._UNCHANGED
    assert await _batch(root, _LEAF) == before
    assert halves == []
    assert kas_parent_grants.runs_on_founding_grants(_LEAF, root) is False


@pytest.mark.asyncio
async def test_the_denies_of_the_chain_decide_as_they_do_with_the_preview_off(
    agents, sessions, stored  # noqa: F811
):
    """The founder's deny (the run's own, the same file) and the middle agent's: the
    same row and the same words, although the ask took the grant the deny stood beside."""
    for spec in (_REFUSES, _ASKS_AND_REFUSES):
        _write(agents, spec)
    verdicts = {}
    for value in (None, True):
        stored(value)
        batch = await _chain(sessions, "refuses", "asks-and-refuses", "refuses")
        verdicts[value] = {
            command: (verdict.action, verdict.tier, verdict.reason)
            for command in ("rm x", "echo a && rm x", "touch secret")
            for verdict in [_answer(batch, "refuses", command)]
        }

    assert verdicts[True] == verdicts[None]
    assert {action for action, _tier, _reason in verdicts[True].values()} == {TOOL_DENY}


@pytest.mark.asyncio
async def test_a_run_whose_rules_cannot_hold_the_asks_above_keeps_no_grant(
    agents, sessions, preview  # noqa: F811
):
    """One rule more than the engine reads would drop the whole block, the asks with
    it: such a run is not started."""
    full = [
        {"capability": "web_fetch", "effect": "deny", "match": [f"https://x{i}.example/*"]}
        for i in range(_MAX_RULES)
    ]
    _write(agents, {**_BROAD, "name": "full", "permissions": {"rules": full}})

    with pytest.raises(AcpAgentUnavailable, match="more than"):
        await _chain(sessions, "full", "asks", "full")
    assert kas_parent_limits.run_is_held(_LEAF, "full") is True


@pytest.mark.asyncio
async def test_a_run_whose_own_rules_cannot_be_read_keeps_no_grant_under_an_ask(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, {**_BROAD, "name": "unread", "permissions": _UNREADABLE_RULES})

    batch = await _chain(sessions, "unread", "asks", "unread")

    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "unread")
    assert _answer(batch, "unread").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["broad", "plain"], ids=["the-founders-agent", "another-agent"])
async def test_a_session_between_whose_agent_no_file_defines_leaves_the_run_no_grant(
    agents, sessions, preview, leaf  # noqa: F811
):
    """What that session asks for cannot be read, so nothing runs unasked under it."""
    sessions["dashboard:p"], sessions["subagent:mid"] = "broad", "gone"
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent(_LEAF, "subagent:mid")
    extras = await KasHarness().session_extras(leaf, work_dir=None, session_key=_LEAF)
    batch = list(extras.custom_agents)

    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, leaf)
    assert _answer(batch, leaf).action != TOOL_AUTO_APPROVE
    assert "no agent file defines 'gone'" in extras.withheld_credentials_notice


def test_a_run_whose_record_is_gone_reads_as_a_session_on_no_agent():
    """The real reader: a run with no live record and no folder names no agent and
    raises nothing. A delivered run's folder is pruned while the gateway runs on."""
    assert subagent_mod._parent_template_for_spawn("subagent:pruned-run") == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["broad", "plain"], ids=["the-founders-agent", "another-agent"])
async def test_a_session_between_with_no_record_of_its_agent_leaves_the_run_no_grant(
    agents, sessions, preview, leaf  # noqa: F811
):
    """The parent note still names the run between, and its record is gone: which
    agent it ran, and so what that agent asks for, is not known."""
    sessions["dashboard:p"] = "broad"
    assert "subagent:mid" not in sessions
    kas_parent_limits.note_parent("subagent:mid", "dashboard:p")
    kas_parent_limits.note_parent(_LEAF, "subagent:mid")
    extras = await KasHarness().session_extras(leaf, work_dir=None, session_key=_LEAF)
    batch = list(extras.custom_agents)

    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, leaf)
    assert _answer(batch, leaf).action != TOOL_AUTO_APPROVE
    assert "has no record of the agent it ran" in extras.withheld_credentials_notice
    assert kas_parent_limits.run_is_held(_LEAF, leaf) is True


@pytest.mark.asyncio
async def test_a_founding_chat_with_no_record_of_its_agent_leaves_the_run_no_grant(
    agents, sessions, preview  # noqa: F811
):
    """The top of the chain: a chat whose record names no agent hands down nothing."""
    assert "dashboard:p" not in sessions
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    batch = await _batch("claims", "subagent:c1")

    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "claims")
    assert _answer(batch, "claims").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
@pytest.mark.parametrize("leaf", ["refuses", "refuses-too"], ids=["the-founders-agent", "another"])
async def test_a_run_left_no_grant_still_refuses_by_its_own_denies(
    agents, sessions, stored, leaf  # noqa: F811
):
    """The grant its deny stood beside is gone; the deny refuses by the same row and
    the same words as with the preview never set."""
    for spec in (_REFUSES, {**_REFUSES, "name": "refuses-too"}):
        _write(agents, spec)
    verdicts = {}
    for value in (None, True):
        stored(value)
        batch = await _chain(sessions, "refuses", "gone", leaf)
        verdicts[value] = {
            command: (verdict.action, verdict.tier, verdict.reason)
            for command in ("rm x", "echo a && rm x")
            for verdict in [_answer(batch, leaf, command)]
        }

    assert verdicts[True] == verdicts[None]
    assert {action for action, _tier, _reason in verdicts[True].values()} == {TOOL_DENY}
    assert _answer(batch, leaf).action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_an_ask_between_on_a_server_granted_by_name_reaches_the_founders_agent(
    agents, sessions, preview, monkeypatch  # noqa: F811
):
    monkeypatch.setattr(kas_agents, "_project_mcp_servers", lambda *a, **k: {})
    ask = {"capability": "mcp", "effect": "ask", "match": ["casrv/look"]}
    server = {"casrv": {"command": "srv", "args": ["a"]}}
    _write(agents, {**_BASE, "name": "names", "allowedTools": ["@casrv"], "mcpServers": server})
    _write(agents, {**_BASE, "name": "asks-a-server", "permissions": {"rules": [ask]}})

    batch = await _chain(sessions, "names", "asks-a-server", "names")

    assert ask in _rules(batch, "names")


@pytest.mark.asyncio
@pytest.mark.parametrize("founder", ["its-own-agent", "wide"])
async def test_a_bots_worker_gains_the_ask_between(
    agents, sessions, preview, monkeypatch, tmp_path, founder  # noqa: F811
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
    _write(mates, {"name": "pixel", "prompt": ""})
    _write(agents, {**_BASE, "name": worker, "allowedTools": ["web_fetch"], "mcpServers": {}})
    _write(agents, {**_ASKS, "name": "asks-to-fetch", "permissions": {"rules": [_FETCH_ASK]}})
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.kas_model_catalog.vault_identity", lambda *_a: None, raising=False
    )

    root = worker if founder == "its-own-agent" else founder
    batch = await _chain(sessions, root, "asks-to-fetch", worker)

    assert _FETCH_ASK in _rules(batch, worker)
