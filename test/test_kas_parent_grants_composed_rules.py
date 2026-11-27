# SPDX-License-Identifier: Apache-2.0
"""A run's composed permission half that KAS cannot read whole keeps the run's spec.

A run on another agent than its founding chat's takes the founder's grants and
keeps its own denies in one composed ``permissions`` block. Two blocks that are
each readable can compose past what KAS reads; the projection would then drop the
whole block, the run's own denies with it. Such a run keeps its own spec and no
grant, as a run whose founders all run its own spec already does.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _BASE,
    _child,
    _engine_rules,
    _isolated,
    _own,
    _write,
    agents,
    preview,
    sessions,
    stored,
)

from kiro_crew.acp import kas_parent_grants, kas_parent_limits
from kiro_crew.acp.kas_permissions import CREW_ANSWERED_BUILTINS_KEY
from kiro_crew.acp.session_handle import AcpAgentUnavailable

_PROTECTED = "touch protected.txt"


def _denies(capability: str, count: int, first: str = "") -> list[dict[str, Any]]:
    rules = [{"capability": capability, "effect": "deny", "match": [first]}] if first else []
    rules += [
        {"capability": capability, "effect": "deny", "match": [f"{capability}-{n}"]}
        for n in range(count - len(rules))
    ]
    return rules


#: Its own file refuses one ``touch`` among 101 shell denies: readable alone.
_RUN = {
    **_BASE,
    "name": "many-denies",
    "permissions": {"rules": _denies("shell", 101, _PROTECTED)},
}
#: Grants the shell and refuses 100 fetches: readable alone.
_FOUNDER = {
    **_BASE,
    "name": "many-fetch-denies",
    "allowedTools": ["execute_bash"],
    "permissions": {"rules": _denies("web_fetch", 100)},
}


@pytest.mark.asyncio
async def test_a_composed_half_kas_cannot_read_keeps_the_runs_own_denies(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _RUN)
    _write(agents, _FOUNDER)
    try:
        batch = await _child(sessions, "many-fetch-denies", "many-denies")
    except AcpAgentUnavailable:
        batch = None  # refused outright: nothing of the run reaches the engine
    if batch is not None:
        assert ("shell", _PROTECTED) in _engine_rules(batch, "many-denies", "deny")
        assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "many-denies")

    # Not the composed block: the run's own spec, with no grant handed down.
    kas_parent_limits.note_parent("subagent:c1", "dashboard:p")
    spec, found = kas_parent_grants.founding_grants_spec(_RUN, "many-denies", "subagent:c1", [])
    assert spec is _RUN
    assert found.applies and found.founders == ()
