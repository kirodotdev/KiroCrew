# SPDX-License-Identifier: Apache-2.0
"""An allow rule with ``"match": []`` grants its whole capability, read as the projection reads it.

The projection reads an empty ``match`` as no match (``kas_permissions``), so such
an allow grants the whole capability. The founding-grants limiting reads the rule
the same way: a run keeps it only when every holder above it holds the whole
capability, and a ``deny`` or ``ask`` with an empty ``match`` is left as it is.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_kas_parent_grants import (  # noqa: F401 -- the fixtures register by import
    _BASE,
    _allows,
    _batch,
    _child,
    _isolated,
    _own,
    _write,
    agents,
    preview,
    sessions,
    stored,
)

from kiro_crew.acp import kas_parent_grants

_WHOLE_FETCH = {"capability": "web_fetch", "effect": "allow", "match": []}
#: Fetches anything unasked, written with an empty ``match``.
_FOUNDER = {**_BASE, "name": "fetches", "permissions": {"rules": [_WHOLE_FETCH]}}


@pytest.mark.asyncio
async def test_a_founders_empty_match_allow_reaches_its_run(
    agents, sessions, preview  # noqa: F811
):
    _write(agents, _FOUNDER)
    # The founding chat's own session fetches unasked.
    assert "web_fetch" in _allows(await _batch("fetches", "dashboard:p"), "fetches")

    batch = await _child(sessions, "fetches", "plain")
    assert "web_fetch" in _allows(batch, "plain")


def _entry(*rules: dict[str, Any]) -> dict[str, Any]:
    return {"id": "run", "permissions": {"rules": list(rules)}}


def test_an_empty_match_allow_is_held_as_the_whole_capability():
    held = kas_parent_grants._held(_entry(_WHOLE_FETCH), False)
    assert ("web_fetch", "") in held.allows


def test_an_empty_match_allow_needs_every_holder_to_hold_the_whole_capability():
    child = _entry({"capability": "mcp", "effect": "allow", "match": []})
    one_tool = kas_parent_grants._held(
        _entry({"capability": "mcp", "effect": "allow", "match": ["srv/a"]}), False
    )
    whole = kas_parent_grants._held(_entry({"capability": "mcp", "effect": "allow"}), False)

    # A founder that holds one tool hands down no whole-server grant.
    assert kas_parent_grants._limited(child, [one_tool])["permissions"]["rules"] == []
    assert kas_parent_grants._limited(child, [whole, one_tool])["permissions"]["rules"] == []
    # Every holder holds the whole capability: the rule stays as written.
    kept = kas_parent_grants._limited(child, [whole])["permissions"]["rules"]
    assert kept == [{"capability": "mcp", "effect": "allow", "match": []}]


@pytest.mark.parametrize("effect", ["deny", "ask"])
def test_a_deny_or_ask_with_an_empty_match_is_left_as_it_is(effect):
    rule = {"capability": "web_fetch", "effect": effect, "match": []}
    nothing = kas_parent_grants._held(_entry(), False)
    assert kas_parent_grants._limited(_entry(rule), [nothing])["permissions"]["rules"] == [rule]
