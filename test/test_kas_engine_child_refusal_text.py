"""The engine's refusal names an own agent the default agent's mount left out.

The engine quotes the ``match`` of the first deny that matches a refused call. An
agent the mount found and left out was refused by the mount's deny on every name,
which reads ``deny subagent matching "*"``. A deny that names it, placed before
that one, makes the text name the agent. Which calls are refused does not change.
"""

from __future__ import annotations

from typing import Any

import pytest
from kas_engine_child_helpers import (
    _HELPER,
    _agents_dir,
    _batch,
    _default_spec,
    _own,
    _setting,
    _write,
)

from kiro_crew.acp import kas_native_batch
from kiro_crew.acp.kas_engine_subagent_target import is_mount_deny

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_DENY_BY_DEFAULT = {"toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}}}
_OWN_HOOK = {"hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]}}


def _subagent_denies(batch: list[dict[str, Any]], name: str = "kirocrew") -> list[dict[str, Any]]:
    [entry] = [a for a in batch if a["id"] == name]
    rules = (entry.get("permissions") or {}).get("rules") or []
    return [r for r in rules if r.get("capability") == "subagent" and r.get("effect") == "deny"]


@pytest.mark.asyncio
async def test_the_agents_left_out_are_denied_by_name_before_the_mounts_deny(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("fine"))
    _write(agents_dir, _own("limited", **_DENY_BY_DEFAULT))
    _write(agents_dir, _own("hooked", **_OWN_HOOK))
    setting("on")

    denies = _subagent_denies(await _batch())

    [named] = [r for r in denies if r.get("match") == ["hooked", "limited"]]
    [mount] = [r for r in denies if is_mount_deny(r)]
    assert denies.index(named) < denies.index(mount) and denies[-1] is mount
    # Still refused by both, and the registered one still spared.
    assert mount["exclude"] == [_HELPER, "fine"] and "exclude" not in named


@pytest.mark.asyncio
async def test_with_no_agent_left_out_no_rule_is_added(agents_dir, setting):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("fine"))
    setting("on")

    denies = _subagent_denies(await _batch())

    assert all(is_mount_deny(r) or "fine" not in (r.get("match") or []) for r in denies)
    assert len(denies) == 2  # Kiro Crew's own agents by name, then the mount's.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, kw",
    [("off", {}), ("on", {"key": "cron:job"}), ("on", {"member_dispatch": True})],
    ids=["off", "cron", "member-dispatch"],
)
async def test_a_session_without_the_mount_names_no_one(agents_dir, setting, value, kw):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("limited", **_DENY_BY_DEFAULT))
    setting(value)

    batch = await _batch(**kw)

    assert _HELPER not in {a["id"] for a in batch}
    assert not any("limited" in (r.get("match") or []) for r in _subagent_denies(batch))


def test_naming_changes_nothing_without_names_or_without_the_mount():
    parent = {"id": "kirocrew", "permissions": {"rules": []}}
    batch = [parent, {"id": _HELPER}]
    assert kas_native_batch.name_left_out(batch, "kirocrew", ()) is batch
    unmounted = [parent, {"id": "someone"}]
    assert kas_native_batch.name_left_out(unmounted, "kirocrew", {"limited"}) is unmounted
