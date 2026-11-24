"""With the ``kas_subagents`` preview off or never set, a person's own agents change nothing.

``native_batch`` gained two steps with the own-agents mount: the person's own agents
(``own_agents``), projected only for an accepted engine mount, and
``mount_deny_last``, run on every batch. Off and never set, neither may move a batch:
``own_agents`` is never called, and the batch is the one the switch alone returns,
``deny_excluded_subagent_tool(deny_own_agents(entries + targets))``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from kas_engine_child_helpers import _agents_dir, _setting

from kiro_crew.acp import kas_subagent_targets
from kiro_crew.acp.kas_engine_subagent_target import KAS_HELPER_AGENT, mount_deny_last
from kiro_crew.acp.kas_native_batch import native_batch
from kiro_crew.acp.kas_subagent_targets import deny_own_agents

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_KEY = "dashboard:t1"

#: One session entry per shape: the default agent, an own agent on every tool, and
#: an own agent that lists the agents it may hand work to.
_ENTRIES: dict[str, list[dict[str, Any]]] = {
    "default-agent": [{"id": "kirocrew", "name": "kirocrew", "tools": ["*"]}],
    "own-agent-all-tools": [{"id": "mine", "name": "mine", "tools": ["*"]}],
    "own-agent-available-agents": [
        {
            "id": "lister",
            "name": "lister",
            "tools": ["*"],
            "toolsSettings": {"subagent": {"availableAgents": ["helper-a"]}},
        }
    ],
}
_TARGETS: dict[str, list[dict[str, Any]]] = {
    "default-agent": [],
    "own-agent-all-tools": [],
    "own-agent-available-agents": [{"id": "helper-a", "name": "helper-a", "tools": ["read"]}],
}


@pytest.mark.parametrize("value", ["off", "unset"])
@pytest.mark.parametrize("shape", sorted(_ENTRIES))
def test_off_and_never_set_project_no_own_agent_and_keep_the_switch_batch(
    agents_dir: Path, setting, value: str, shape: str
) -> None:
    setting(value)
    calls: list[int] = []

    def own_agents() -> tuple[list[dict[str, Any]], str]:
        calls.append(1)
        return [], ""

    entries = copy.deepcopy(_ENTRIES[shape])
    targets = copy.deepcopy(_TARGETS[shape])
    agent = entries[0]["id"]
    expected = kas_subagent_targets.deny_excluded_subagent_tool(
        deny_own_agents([*copy.deepcopy(entries), *copy.deepcopy(targets)])
    )

    batch = native_batch(
        entries,
        targets,
        agent=agent,
        session_key=_KEY,
        agents_dir=agents_dir,
        own_agents=own_agents,
    )

    assert calls == []
    assert batch == expected
    assert all(e.get("id") != KAS_HELPER_AGENT for e in batch)


def test_mount_deny_last_returns_a_batch_without_the_helper_as_it_is() -> None:
    batch = deny_own_agents([copy.deepcopy(e) for v in _ENTRIES.values() for e in v])
    assert mount_deny_last(batch, "kirocrew") is batch
