"""Which of the kinds the default agent's mount refuses can be held for a sub-agent.

A person's agent with a granted tool beside ``deniedCommands`` is registered when
every one of those entries reached its entry as an engine deny rule: the engine
refuses what they name for a sub-agent, and the sub-agent's own grant answers
nothing, so no limit is left for the gate alone. Every other kind is refused as
before.
"""

from __future__ import annotations

from typing import Any

import pytest
from kas_engine_child_helpers import _agents_dir, _batch, _default_spec, _own, _setting, _write

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)


def _shell_denies(entry: dict[str, Any]) -> list[str]:
    rules = (entry.get("permissions") or {}).get("rules") or []
    return [
        glob
        for r in rules
        if r.get("capability") == "shell" and r.get("effect") == "deny"
        for glob in r.get("match") or []
    ]


async def _registered(agents_dir, setting, **keys: Any) -> dict[str, Any] | None:
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("mine", **keys))
    setting("on")
    return next((a for a in await _batch() if a["id"] == "mine"), None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "denied, globs",
    [
        (["touch .*"], {"touch *"}),
        (["(touch|mkdir) .*"], {"touch *", "mkdir *"}),
        (["rm .*", "shred .*"], {"rm *", "shred *"}),
    ],
    ids=["one command", "a group of two", "two entries"],
)
async def test_denied_commands_the_engine_holds_register_beside_a_grant(
    agents_dir, setting, denied, globs
):
    entry = await _registered(
        agents_dir,
        setting,
        allowedTools=["execute_bash"],
        toolsSettings={"shell": {"deniedCommands": denied}},
    )

    assert entry is not None
    assert globs <= set(_shell_denies(entry))
    # Its own grant travels as no engine allow: the engine never runs its shell unasked.
    rules = entry["permissions"]["rules"]
    assert not any(r.get("capability") == "shell" and r.get("effect") == "allow" for r in rules)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "keys",
    [
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {"shell": {"deniedCommands": ["rm [a-z]+\\.txt"]}},
        },
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {"shell": {"deniedCommands": ["touch .*", "rm [a-z]+\\.txt"]}},
        },
        {
            "tools": ["fs_write"],
            "allowedTools": ["fs_write"],
            "toolsSettings": {"fs_write": {"deniedPaths": ["secret/**"]}},
        },
        {
            "tools": ["fs_write", "execute_bash"],
            "allowedTools": ["fs_write", "execute_bash"],
            "toolsSettings": {
                "shell": {"deniedCommands": ["touch .*"]},
                "fs_write": {"deniedPaths": ["secret/**"]},
            },
        },
        {"toolsSettings": {"shell": {"denyByDefault": True, "allowedCommands": ["ls"]}}},
        {
            "allowedTools": ["execute_bash"],
            "toolsSettings": {
                "shell": {
                    "denyByDefault": True,
                    "allowedCommands": ["ls"],
                    "deniedCommands": ["touch .*"],
                }
            },
        },
        {
            "tools": ["fs_write"],
            "toolsSettings": {"fs_write": {"fallbackAction": "deny", "allowedPaths": ["out"]}},
        },
        {"hooks": {"preToolUse": [{"matcher": "*", "command": "exit 2"}]}},
        {
            "tools": ["execute_bash", "web_fetch"],
            "allowedTools": ["execute_bash"],
            "toolsSettings": {
                "shell": {"deniedCommands": ["touch .*"]},
                "web_fetch": {"blocked": ["https://internal\\.example\\.com/admin.*"]},
            },
        },
    ],
    ids=[
        "a deny with no glob spelling",
        "one deny the engine holds and one it does not",
        "write deniedPaths",
        "denied commands and write deniedPaths",
        "denyByDefault",
        "denyByDefault with denied commands",
        "a write allowlist",
        "its own preToolUse hook",
        "denied commands the engine holds beside a blocked url only the gate refuses",
    ],
)
async def test_a_limit_only_the_gate_could_apply_still_keeps_the_agent_out(
    agents_dir, setting, keys
):
    assert await _registered(agents_dir, setting, **keys) is None


@pytest.mark.asyncio
async def test_a_custom_parents_listed_agent_is_judged_as_before(agents_dir, setting):
    """Only the default agent's mount reads it this way."""
    _write(
        agents_dir,
        {
            "name": "delegator",
            "prompt": "Delegate.",
            "tools": ["fs_read", "use_subagent"],
            "toolsSettings": {"subagent": {"availableAgents": ["mine"]}},
        },
    )
    _write(
        agents_dir,
        _own(
            "mine",
            allowedTools=["execute_bash"],
            toolsSettings={"shell": {"deniedCommands": ["touch .*"]}},
        ),
    )
    setting("on")

    assert "mine" not in {a["id"] for a in await _batch("delegator")}
