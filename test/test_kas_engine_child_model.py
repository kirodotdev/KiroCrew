"""A person's own agent, run as an engine sub-agent, runs on the model its own file names.

The engine runs a client-registered sub-agent on the ``model`` its batch entry
carries and falls back to the session's model for an id it does not serve (shown
by runs on engine 0.66.119). The session's own entry carries none: its model is
set by the protocol verb. The ids here are placeholders; no real model is named.
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

from kiro_crew.acp._dispatch import build_session_new_params

agents_dir = pytest.fixture(_agents_dir)
setting = pytest.fixture(_setting)

_ID = "model-x.1"


def _by_id(batch: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {a["id"]: a for a in batch}


@pytest.mark.asyncio
async def test_a_registered_agent_carries_its_own_model_and_no_one_else_does(agents_dir, setting):
    _write(agents_dir, _default_spec(model="model-of-the-chat"))
    _write(agents_dir, _own("pinned", model=_ID))
    _write(agents_dir, _own("plain"))
    setting("on")

    batch = await _batch()
    entries = _by_id(batch)

    assert entries["pinned"]["model"] == _ID
    assert all("model" not in entries[name] for name in ("kirocrew", "plain", _HELPER))
    # As ``session/new`` carries it.
    params = build_session_new_params("/w", mcp_servers=[], kas_custom_agents=list(batch))
    wire = _by_id(params["_meta"]["kiro"]["customAgents"])
    assert wire["pinned"]["model"] == _ID and "model" not in wire["kirocrew"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model",
    ["", "--model", "two words", "a/b", "x" * 129, 7, None, ["model-x"]],
    ids=["empty", "a flag", "a space", "a path", "too long", "a number", "null", "a list"],
)
async def test_a_model_of_another_shape_is_left_out(agents_dir, setting, model):
    _write(agents_dir, _default_spec())
    _write(agents_dir, _own("pinned", model=model))
    setting("on")

    entries = _by_id(await _batch())

    assert "pinned" in entries and "model" not in entries["pinned"]


@pytest.mark.asyncio
async def test_a_custom_parents_listed_agent_carries_no_model(agents_dir, setting):
    """Only the default agent's mount says it; a custom parent's target is as before."""
    _write(
        agents_dir,
        {
            "name": "delegator",
            "prompt": "Delegate.",
            "tools": ["fs_read", "use_subagent"],
            "toolsSettings": {"subagent": {"availableAgents": ["pinned"]}},
        },
    )
    _write(agents_dir, _own("pinned", model=_ID))
    setting("on")

    entries = _by_id(await _batch("delegator"))

    assert "pinned" in entries and "model" not in entries["pinned"]
