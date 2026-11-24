"""Which native KAS child a permission request belongs to, read off the request itself.

With the person's own agents registered beside ``kirocrew-helper``, the helper is
one of several children the default agent can start. The engine (0.66.119) names
the asking sub-agent and its depth on each request it raises, and the gate gives
the helper its parent's grants only on that word: the helper's exact id, started
by the session's own agent, in a session whose registration mounted the shipped
helper (the host memory ``kas_shipped_helper_parent``). A
person's agent, a helper another sub-agent started, a request that names no one
(every request of 0.66.117) and a malformed one keep the limits and are asked.

The requests are the ones a real engine sent (``kas_engine_child_helpers``).
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from kas_engine_child_helpers import (
    _DIRECT_HELPER,
    _DIRECT_OWN,
    _HELPER,
    _NESTED_HELPER,
    _NESTED_OWN,
    _OWN_STARTING_THE_HELPER,
    _RECORDED,
    _STAMPED,
    _agents_dir,
    _Batch,
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

from kiro_crew.acp import kas_engine_subagent_target as target
from kiro_crew.acp import kas_wire
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE
from kiro_crew.agent_files import OWNED_KIRO_AGENT_NAMES
from kiro_crew.hooks import TOOL_AUTO_APPROVE

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

# ── one session, two children ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_helper_and_a_persons_agent_in_one_session_are_judged_apart(
    agents_dir, setting, tmp_path
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    assert not target.helper_is_the_only_child(batch, "kirocrew")
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER))

    own = _own_request(handle, _DIRECT_HELPER)
    helper = _replay(handle, _DIRECT_HELPER)
    person = _replay(handle, _DIRECT_OWN)

    assert helper.native_child_call and person.native_child_call
    # The helper: every stamped field its parent's own request carries, so the same verdict.
    for name in _STAMPED:
        assert getattr(helper, name) == getattr(own, name), name
    assert _granted(own) and _verdict(own)[0] == TOOL_AUTO_APPROVE
    assert _verdict(helper) == _verdict(own)
    # The person's agent is a child too; what it is given is in
    # ``test_kas_engine_child_grants.py``.


def _named(name: object = _HELPER, depth: object = 1) -> dict[str, Any]:
    return {"agentSubtaskId": "st-1", "agentName": name, "depth": depth}


#: What the engine could put where it names the asker, and anything that is not the
#: helper started by the session's own agent. ``...`` leaves the key out.
_NOT_THE_HELPER: dict[str, Any] = {
    "no requester": ...,
    "requester is not an object": "kirocrew-helper",
    "no sub-agent on the requester": {"sessionId": "s"},
    "sub-agent is not an object": {"subagent": "kirocrew-helper"},
    "no name": {"subagent": {"agentSubtaskId": "st-1", "depth": 1}},
    "empty name": {"subagent": _named("")},
    "another case": {"subagent": _named(_HELPER.upper())},
    "a space before": {"subagent": _named(" kirocrew-helper")},
    "a longer name": {"subagent": _named("kirocrew-helper-2")},
    "a name in a list": {"subagent": _named(["kirocrew-helper"])},
    "one of Kiro Crew's own agents": {"subagent": _named(sorted(OWNED_KIRO_AGENT_NAMES)[0])},
    "no depth": {"subagent": {"agentSubtaskId": "st-1", "agentName": _HELPER}},
    "depth 0": {"subagent": _named(depth=0)},
    "depth as text": {"subagent": _named(depth="1")},
    "depth as a float": {"subagent": _named(depth=1.0)},
    "depth as a boolean": {"subagent": _named(depth=True)},
    "depth as null": {"subagent": _named(depth=None)},
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_NOT_THE_HELPER))
async def test_a_request_that_does_not_name_the_helper_as_a_direct_child_is_asked(
    agents_dir, setting, tmp_path, case
):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    request = copy.deepcopy(_RECORDED[_DIRECT_HELPER]["request"])
    if _NOT_THE_HELPER[case] is ...:
        del request["_meta"]["kiro"]["requester"]
    else:
        request["_meta"]["kiro"]["requester"] = _NOT_THE_HELPER[case]

    event = _replay(_handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER)), _DIRECT_HELPER, request)

    assert event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE


@pytest.mark.parametrize(
    "requester, named",
    [
        ((_HELPER, 1), True),
        ((_HELPER, 2), False),
        ((_HELPER, True), False),
        ((_HELPER, None), False),
        (("own-model", 1), False),
        ((None, 1), False),
        (None, False),
        ((_HELPER,), False),
        ([_HELPER, 1], False),
    ],
)
def test_the_engines_word_is_the_exact_id_at_depth_one(requester, named):
    assert target.engine_names_the_helper(requester) is named


@pytest.mark.parametrize(
    "params",
    [
        None,
        [],
        {},
        {"_meta": None},
        {"_meta": {"kiro": []}},
        {"_meta": {"kiro": {"requester": []}}},
    ],
)
def test_a_request_with_a_hop_missing_names_no_sub_agent(params):
    assert kas_wire.requesting_subagent(params) is None


def test_the_recorded_requests_name_their_sub_agent_and_depth():
    read = {label: kas_wire.requesting_subagent(r["request"]) for label, r in _RECORDED.items()}
    assert read == {
        _DIRECT_HELPER: (_HELPER, 1),
        _DIRECT_OWN: ("own-model", 1),
        _OWN_STARTING_THE_HELPER: ("own-star", 1),
        _NESTED_HELPER: (_HELPER, 2),
        _NESTED_OWN: ("own-model", 2),
    }


# ── no own agent registered: the helper is the only child, as before ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "named", [True, False], ids=["an engine that names the asker", "one that does not"]
)
async def test_with_no_own_agent_the_helper_is_judged_as_its_parent_whatever_the_engine_sends(
    agents_dir, setting, tmp_path, named
):
    _write(agents_dir, _default_spec())
    setting("on")
    batch = await _batch()
    assert [a["id"] for a in batch] == ["kirocrew", _HELPER]
    assert target.helper_is_the_only_child(batch, "kirocrew")
    request = copy.deepcopy(_RECORDED[_DIRECT_HELPER]["request"])
    if not named:
        del request["_meta"]["kiro"]["requester"]
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER))

    own = _own_request(handle, _DIRECT_HELPER)
    helper = _replay(handle, _DIRECT_HELPER, request)

    for name in _STAMPED:
        assert getattr(helper, name) == getattr(own, name), name
    assert _granted(helper) and _verdict(helper) == _verdict(own)


# ── where nothing is mounted ──────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, kw",
    [
        ("off", {}),
        ("unset", {}),
        ("on", {"key": "cron:job"}),
        ("on", {"key": "dashboard:member-autofix"}),
        ("on", {"member_dispatch": True}),
        ("on", {"crew_panel": True}),
    ],
    ids=["off", "never-set", "cron", "a-members-chat", "member-dispatch", "panel"],
)
async def test_a_session_without_the_mount_gives_no_child_a_grant(
    agents_dir, setting, tmp_path, value, kw
):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("own-model"))
    setting(value)
    batch = await _batch(**kw)
    assert _HELPER not in {a["id"] for a in batch}
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER))

    assert not _granted(_replay(handle, _DIRECT_HELPER))


@pytest.mark.asyncio
async def test_a_mounted_batch_whose_registration_recorded_no_mount_gives_the_helper_no_grant(
    agents_dir, setting, tmp_path
):
    """The batch alone is not the record: the same registered entries with no host
    memory of the mount (a re-projection that mounted none) earn the name nothing."""
    batch = await _mounted_with_own_agents(agents_dir, setting)
    assert batch.helper_parent == "kirocrew"
    handle = _handle(list(batch), tmp_path, sid=_sid(_DIRECT_HELPER))

    assert handle.kas_shipped_helper_parent == ""
    helper = _replay(handle, _DIRECT_HELPER)
    assert helper.native_child_call and not _granted(helper)
    assert _verdict(helper)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_another_harness_never_reads_the_requester(agents_dir, setting, tmp_path):
    batch = await _mounted_with_own_agents(agents_dir, setting)
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER), backend=ACP_BACKEND_CLAUDE)
    assert not _granted(_replay(handle, _DIRECT_HELPER))


# ── a person's own agent named like the helper ────────────────────────────────


def _delegator(*also: str) -> dict[str, Any]:
    """A custom parent that lists an agent named like the helper, then *also*, as its sub-agents."""
    return {
        "name": "delegator",
        "prompt": "Delegate.",
        "tools": ["fs_read", "execute_bash", "use_subagent"],
        "allowedTools": ["fs_read", "execute_bash"],
        "toolsSettings": {"subagent": {"availableAgents": [_HELPER, *also]}},
    }


#: The look-alike as the only sub-agent its parent lists, and listed beside another.
_LISTED_WITH = pytest.mark.parametrize(
    "also", [(), ("own-model",)], ids=["the only one listed", "listed beside another"]
)


async def _a_persons_agent_under_the_helpers_id(
    agents_dir: Path, setting, also: tuple[str, ...]
) -> _Batch:
    _write(agents_dir, _delegator(*also))
    for name in (_HELPER, *also):
        _write(agents_dir, _own(name))
    setting("on")
    batch = await _batch("delegator")
    # The person's agent sits under the helper's id, and the parent's own
    # ``availableAgents`` deny has the shape of the mount's.
    assert {a["id"] for a in batch} == {"delegator", _HELPER, *also}
    [theirs] = [a for a in batch if a["id"] == _HELPER]
    assert theirs["description"] == f"the person's {_HELPER}"
    return batch


@pytest.mark.asyncio
@_LISTED_WITH
async def test_a_persons_agent_named_like_the_helper_under_a_custom_parent_is_asked(
    agents_dir, setting, tmp_path, also
):
    """No mount was recorded for a custom parent, so the name earns nothing."""
    batch = await _a_persons_agent_under_the_helpers_id(agents_dir, setting, also)
    assert batch.helper_parent == ""
    handle = _handle(batch, tmp_path, agent="delegator", sid=_sid(_DIRECT_HELPER))

    event = _replay(handle, _DIRECT_HELPER)

    assert event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_file_named_like_the_helper_mounts_nothing_and_its_child_is_asked(
    agents_dir, setting, tmp_path
):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own(_HELPER))
    _write(agents_dir, _own("own-model"))
    setting("on")

    batch = await _batch()

    # The mount is refused whole: no helper, no agent of the person, nothing under that id.
    assert [a["id"] for a in batch] == ["kirocrew"]
    # The engine loads the person's file itself and reports its child under the
    # helper's name at depth 1, as the recorded request reads: the name earns nothing.
    event = _replay(_handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER)), _DIRECT_HELPER)
    assert event.native_child_call and not _granted(event)
    assert _verdict(event)[0] != TOOL_AUTO_APPROVE


# ── the order of the denies ───────────────────────────────────────────────────


def _subagent_denies(entry: dict[str, Any]) -> list[dict[str, Any]]:
    rules = (entry.get("permissions") or {}).get("rules") or []
    return [r for r in rules if r.get("capability") == "subagent" and r.get("effect") == "deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("own_agents", [0, 1], ids=["helper only", "with an own agent"])
async def test_the_denies_that_name_agents_come_before_the_mounts_deny(
    agents_dir, setting, own_agents
):
    """The engine quotes the first deny that matches: a refused agent of Kiro
    Crew's own must read as named, not as every sub-agent being blocked."""
    _write(agents_dir, _default_spec())
    if own_agents:
        _write(agents_dir, _own("own-model"))
    setting("on")

    parent = next(a for a in await _batch() if a["id"] == "kirocrew")

    rules = parent["permissions"]["rules"]
    denies = _subagent_denies(parent)
    [mount] = [r for r in denies if r.get("match") == ["*"]]
    [named] = [r for r in denies if r.get("match") == sorted(OWNED_KIRO_AGENT_NAMES)]
    assert rules.index(named) < rules.index(mount)
    assert rules[-1] is mount
    assert mount["exclude"] == [_HELPER, *(["own-model"] if own_agents else [])]


def test_only_the_mounted_entrys_own_mount_deny_moves():
    mount = {**target.only_helper_rule(), "exclude": [_HELPER, "own-model"]}
    named = {"capability": "subagent", "match": ["kirocrew-worker"], "effect": "deny"}
    other = {"capability": "shell", "effect": "ask"}
    parent = {"id": "kirocrew", "permissions": {"rules": [other, mount, named]}}
    child = {"id": "own-model", "permissions": {"rules": [mount, named]}}
    helper = {"id": _HELPER}

    out = target.mount_deny_last([parent, child, helper], "kirocrew")

    assert out[0]["permissions"]["rules"] == [other, named, mount]
    assert out[1] is child and out[2] is helper
    # No helper in the batch, so no mount: nothing moves.
    unmounted = [parent, child]
    assert target.mount_deny_last(unmounted, "kirocrew") is unmounted
