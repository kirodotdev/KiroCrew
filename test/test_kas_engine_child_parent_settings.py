"""A default agent whose ``toolsSettings`` are shell settings still mounts engine sub-agents,
and every sub-agent is held to those settings as the agent itself is.

The engine carries the deny rules, which each sub-agent's entry copies; the gate
stamps the allowlist, its read-only flag and the denies it refuses itself on every
native sub-agent's request. Any other tool's settings keep the mount off: no test
shows a sub-agent held to them. The requests are the ones a real engine sent
(``kas_engine_child_helpers``): a sub-agent running ``touch <file>``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from kas_engine_child_helpers import (
    _DEFAULTS,
    _DIRECT_HELPER,
    _DIRECT_OWN,
    _HELPER,
    _RECORDED,
    _agents_dir,
    _batch,
    _default_spec,
    _handle,
    _own,
    _own_request,
    _replay,
    _setting,
    _sid,
    _verdict,
    _write,
)

from kiro_crew.acp import kas_engine_subagents as engine
from kiro_crew.hooks import TOOL_AUTO_APPROVE

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

#: The fields a spec's shell limits reach the gate in.
_LIMITS = (
    "spec_shell_allowlist",
    "spec_shell_readonly",
    "spec_unjudged_denies",
    "spec_write_allowlist",
)
_CHILDREN = pytest.mark.parametrize(
    "label", [_DIRECT_HELPER, _DIRECT_OWN], ids=["the helper", "a person's agent"]
)


async def _mounted(agents_dir, setting, shell: dict[str, Any], *, grant_shell: bool = False):
    shipped = json.loads(_DEFAULTS.read_text(encoding="utf-8"))
    allowed = [*shipped["allowedTools"], *(["execute_bash"] if grant_shell else [])]
    _write(agents_dir, _default_spec(allowedTools=allowed, toolsSettings={"shell": shell}))
    _write(agents_dir, _own("own-model"))
    setting("on")
    batch = await _batch()
    assert batch.helper_parent == "kirocrew"
    assert {"own-model", _HELPER} <= {a["id"] for a in batch}
    return batch


@pytest.mark.asyncio
@_CHILDREN
@pytest.mark.parametrize(
    "shell",
    [
        {"denyByDefault": True, "allowedCommands": ["ls .*"]},
        {"denyByDefault": True, "allowedCommands": ["ls .*"], "autoAllowReadonly": True},
        {"deniedCommands": ["touch [a-z-]+\\.txt"]},
    ],
    ids=["an allowlist", "an allowlist with read-only commands", "a deny the engine cannot hold"],
)
@pytest.mark.parametrize("named", [True, False], ids=["asker named", "no asker named"])
async def test_a_command_the_parents_shell_settings_refuse_is_refused_for_a_sub_agent(
    agents_dir, setting, tmp_path, label, shell, named
):
    """With the engine's word and without it (0.66.117): a limit needs no name."""
    batch = await _mounted(agents_dir, setting, shell)
    handle = _handle(batch, tmp_path, sid=_sid(label))
    request = copy.deepcopy(_RECORDED[label]["request"])
    if not named:
        del request["_meta"]["kiro"]["requester"]

    own = _own_request(handle, label)
    child = _replay(handle, label, request)

    for name in _LIMITS:
        assert getattr(child, name) == getattr(own, name), name
    verdict = _verdict(own)
    assert verdict[1].startswith("spec-") and verdict[0] != TOOL_AUTO_APPROVE
    assert _verdict(child) == verdict


@pytest.mark.asyncio
async def test_a_deny_the_engine_holds_is_on_every_sub_agents_entry(agents_dir, setting):
    batch = await _mounted(agents_dir, setting, {"deniedCommands": ["touch .*"]})
    by_id = {a["id"]: a for a in batch}
    [deny] = [
        r
        for r in by_id["kirocrew"]["permissions"]["rules"]
        if r.get("capability") == "shell" and r.get("effect") == "deny"
    ]
    assert deny["match"] == ["touch *"]
    for name in (_HELPER, "own-model"):
        assert deny in by_id[name]["permissions"]["rules"], name


@pytest.mark.asyncio
async def test_a_command_the_parents_allowed_commands_grant_runs_for_the_helper_as_for_it(
    agents_dir, setting, tmp_path
):
    batch = await _mounted(agents_dir, setting, {"allowedCommands": ["touch .*"]})
    handle = _handle(batch, tmp_path, sid=_sid(_DIRECT_HELPER))

    own = _own_request(handle, _DIRECT_HELPER)
    helper = _replay(handle, _DIRECT_HELPER)

    assert own.spec_granted_commands and helper.spec_granted_commands == own.spec_granted_commands
    assert _verdict(own)[0] == TOOL_AUTO_APPROVE and _verdict(helper) == _verdict(own)


@pytest.mark.parametrize(
    "raw, held",
    [
        (None, True),
        ({}, True),
        ({"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}}, True),
        ({"execute_bash": {"deniedCommands": ["rm .*"], "autoAllowReadonly": True}}, True),
        ({"shell": {"denyByDefault": True, "somethingElse": 1}}, False),
        ({"shell": "denyByDefault"}, False),
        ({"shell": {"denyByDefault": True}, "execute_bash": {"denyByDefault": True}}, False),
        ({"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}}, False),
        ({"fs_write": {"deniedPaths": ["secret/**"]}}, False),
        ({"fs_read": {"deniedPaths": ["private/**"]}}, False),
        ({"web_fetch": {"blocked": ["https://x.example/*"]}}, False),
        ({"use_aws": {"deniedServices": ["iam"]}}, False),
        ({"subagent": {"availableAgents": ["mine"]}}, False),
        ({"shell": {"denyByDefault": True}, "fs_read": {"deniedPaths": ["p/**"]}}, False),
        ("shell", False),
    ],
)
def test_only_shell_settings_with_a_mirror_keep_the_mount(raw, held):
    assert engine._only_held_tools_settings(raw) is held
    spec = _default_spec(**({} if raw is None else {"toolsSettings": raw}))
    assert (engine.unmirrored_restriction(spec) == "") is held
