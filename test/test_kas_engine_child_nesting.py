"""A sub-agent that another sub-agent started: its root's grants, under every rule above it.

The engine reports how deep under the session's own agent a sub-agent runs, and
applies the rules of every sub-agent above a call to it (a deny refuses, an ask
raises the request). A request alone does not say which sub-agent started it, and
these tests replay no frame of the start (``test_kas_engine_child_chain.py`` does).
So a registered
sub-agent below the first level gets the session agent's grants less whatever any
registered agent's own ``ask`` holds back, and no grant on an MCP tool. An engine
that names no one leaves it asked. The requests are the ones a real engine sent
(``kas_engine_child_helpers``): ``own-star`` started ``own-model`` and the helper.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from kas_engine_child_helpers import (
    _HELPER,
    _NESTED_HELPER,
    _NESTED_OWN,
    _RECORDED,
    _agents_dir,
    _batch,
    _default_spec,
    _granted,
    _handle,
    _mounted_with_own_agents,
    _own,
    _own_request,
    _replay,
    _setting,
    _sid,
    _verdict,
    _write,
)

from kiro_crew.acp import kas_engine_child_grants as grants
from kiro_crew.hooks import TOOL_AUTO_APPROVE

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_NESTED = pytest.mark.parametrize("label", [_NESTED_OWN, _NESTED_HELPER], ids=["own", "helper"])


@pytest.mark.asyncio
@_NESTED
async def test_a_sub_agent_below_the_first_level_is_judged_as_the_sessions_agent(
    agents_dir, setting, tmp_path, label
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(batch, tmp_path, sid=_sid(label))

    own = _own_request(handle, label)
    nested = _replay(handle, label)

    assert _RECORDED[label]["request"]["_meta"]["kiro"]["requester"]["subagent"]["depth"] == 2
    assert nested.native_child_call
    assert nested.spec_granted_builtins == {
        ref for ref in own.spec_granted_builtins if not ref.startswith("@")
    }
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(nested) == _verdict(own)


@pytest.mark.asyncio
@_NESTED
async def test_an_ask_of_any_registered_agent_holds_the_grant_back_below_the_first_level(
    agents_dir, setting, tmp_path, label
):
    """``own-star`` asks about the shell in its own file. The engine does not say
    who started the sub-agent, so any registered agent's ask counts as above it."""
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("own-model"))
    ask = {"capability": "shell", "effect": "ask"}
    _write(agents_dir, _own("own-star", tools=["*"], permissions={"rules": [ask]}))
    setting("on")
    batch = await _batch()
    handle = _handle(batch, tmp_path, sid=_sid(label))

    nested = _replay(handle, label)

    assert "execute_bash" not in nested.spec_granted_builtins
    assert "fs_read" in nested.spec_granted_builtins
    assert _verdict(nested)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_the_same_ask_does_not_reach_an_agent_the_session_agent_started(
    agents_dir, setting, tmp_path
):
    """At the first level nothing sits between: another agent's ask is not its own."""
    from kas_engine_child_helpers import _DIRECT_OWN

    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("own-model"))
    ask = {"capability": "shell", "effect": "ask"}
    _write(agents_dir, _own("own-star", tools=["*"], permissions={"rules": [ask]}))
    setting("on")
    handle = _handle(await _batch(), tmp_path, sid=_sid(_DIRECT_OWN))

    assert "execute_bash" in _replay(handle, _DIRECT_OWN).spec_granted_builtins


def _entries(**rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A mounted batch by hand: ``kirocrew``, the helper and one entry per keyword."""
    mount = {
        "capability": "subagent",
        "effect": "deny",
        "match": ["*"],
        "exclude": [_HELPER, *rules],
    }
    same = {"command": "python", "args": ["/srv/same.py"]}
    return [
        {"id": "kirocrew", "mcpServers": {"same": same}, "permissions": {"rules": [mount]}},
        *(
            {"id": name, "mcpServers": {"same": same}, "permissions": {"rules": own}}
            for name, own in rules.items()
        ),
        {"id": _HELPER},
    ]


@pytest.mark.parametrize(
    "requester, named, between",
    [
        (("leaf", 1), "leaf", []),
        (("leaf", 2), "leaf", ["mid", _HELPER]),
        (("leaf", 5), "leaf", ["mid", _HELPER]),
        ((_HELPER, 2), _HELPER, ["leaf", "mid"]),
        ((_HELPER, 1), None, None),
        (("leaf", 0), None, None),
        (("leaf", -1), None, None),
        (("leaf", True), None, None),
        (("leaf", 2.0), None, None),
        (("leaf", "2"), None, None),
        (("stranger", 2), None, None),
        (("kirocrew", 2), None, None),
        (None, None, None),
    ],
)
def test_who_is_named_and_what_may_sit_above_it(requester, named, between):
    answer = grants.named_own_agent(requester, _entries(leaf=[], mid=[]), "kirocrew")
    if named is None:
        assert answer is None
        return
    child, parent, above = answer
    assert (child["id"], parent["id"]) == (named, "kirocrew")
    assert sorted(a["id"] for a in above) == sorted(between)


def test_no_grant_on_an_mcp_tool_is_kept_below_the_first_level(tmp_path):
    """Both agents declare ``same`` as the session's agent does; which servers the
    one in between shares is not known, so the grant on its tool is not kept."""
    from types import SimpleNamespace

    def stamped() -> SimpleNamespace:
        return SimpleNamespace(
            spec_granted_builtins=frozenset({"fs_read", "@same/write"}),
            spec_hook_held=frozenset({"@same/held"}),
            spec_granted_commands=("ls *",),
            spec_granted_paths=("out/**",),
            spec_granted_read_paths=("docs/**",),
            spec_denied_grant=None,
        )

    batch = _entries(leaf=[], mid=[])
    child, parent, above = grants.named_own_agent(("leaf", 2), batch, "kirocrew")
    direct, nested = stamped(), stamped()

    grants.narrow(direct, child, parent)
    grants.narrow(nested, child, parent, above)

    assert direct.spec_granted_builtins == {"fs_read", "@same/write"}
    assert direct.spec_hook_held == {"@same/held"}
    assert nested.spec_granted_builtins == {"fs_read"} and nested.spec_hook_held == frozenset()
    assert nested.spec_granted_commands == ("ls *",) and nested.spec_granted_paths == ("out/**",)


@pytest.mark.asyncio
async def test_an_engine_that_names_no_one_leaves_the_nested_sub_agent_asked(
    agents_dir, setting, tmp_path
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    request = copy.deepcopy(_RECORDED[_NESTED_OWN]["request"])
    del request["_meta"]["kiro"]["requester"]

    event = _replay(_handle(batch, tmp_path, sid=_sid(_NESTED_OWN)), _NESTED_OWN, request)

    assert event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE
