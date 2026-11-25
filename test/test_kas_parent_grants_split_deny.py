# SPDX-License-Identifier: Apache-2.0
"""A run whose own shell deny the gate cannot read takes no founder grant.

``touch|mkdir`` reaches the engine only as the globs ``touch`` and ``mkdir``, which
are narrower than the entry's own regex, and Kiro Crew's gate cannot read a
top-level alternation at all. The run's projection keeps the shell grant back at
the engine's ask for such an entry, so its founding chat's shell grant does not
answer the run's ``touch marker``: it asks, as its own file makes it ask. A deny
the gate reads keeps the grant, which the gate answers only after that deny.
"""

from __future__ import annotations

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _BASE,
    _answer,
    _child,
    _isolated,
    _own,
    _write,
    agents,
    preview,
    sessions,
    stored,
)

from kiro_crew.acp.kas_permissions import CREW_ANSWERED_BUILTINS_KEY, CREW_SPEC_DENIES_KEY
from kiro_crew.hooks import TOOL_AUTO_APPROVE

_SPLIT = {
    **_BASE,
    "name": "splits",
    "toolsSettings": {"shell": {"deniedCommands": ["touch|mkdir"]}},
}
_READ = {
    **_BASE,
    "name": "reads",
    "toolsSettings": {"shell": {"deniedCommands": ["rm .*"]}},
}


@pytest.mark.asyncio
async def test_a_run_with_a_deny_the_gate_cannot_read_takes_no_founder_grant(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _SPLIT)

    batch = await _child(sessions, "broad", "splits")

    assert CREW_ANSWERED_BUILTINS_KEY not in _own(batch, "splits")
    assert _answer(batch, "splits", "touch marker").action != TOOL_AUTO_APPROVE


@pytest.mark.asyncio
async def test_a_run_whose_denies_the_gate_reads_keeps_its_founders_grant(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _READ)

    batch = await _child(sessions, "broad", "reads")

    # The grant travels in the deny record, which the gate checks first.
    assert _own(batch, "reads")[CREW_SPEC_DENIES_KEY]["builtins"] == ["execute_bash"]
    assert _answer(batch, "reads", "touch marker").action == TOOL_AUTO_APPROVE
    assert _answer(batch, "reads", "rm x").action != TOOL_AUTO_APPROVE
