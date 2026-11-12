"""A meeting agent's ``widget_type`` that is not a string falls back to the default.

``PUT /config`` tests the value against the widget table, a dict. A list or an
object is not hashable, so testing it there raised out of the handler as a 500 and
nothing was saved. Every other unknown value already falls back to the default.
"""

from __future__ import annotations

import pytest
from meetings_helpers import (  # noqa: F401
    app_fixture,
    client_for,
    enabled_fixture,
    fake_sessions_fixture,
    reset_module_state_fixture,
    root_fixture,
)
from meetings_routes_helpers import BASE

from kiro_crew.apps.builtins.meetings.backend import constants as k


@pytest.mark.asyncio
@pytest.mark.parametrize("widget", [["markdown"], {"kind": "html"}, 7, None, "nope"])
async def test_a_widget_type_that_is_not_a_known_name_saves_as_the_default(app, widget):
    async with client_for(app) as client:
        resp = await client.put(
            f"{BASE}/config",
            json={"config": {"meeting_agents": [{"id": "note-taker", "widget_type": widget}]}},
        )
        assert resp.status == 200, await resp.text()
        body = await (await client.get(f"{BASE}/config")).json()

    agents = {a["id"]: a for a in body["config"]["meeting_agents"]}
    assert agents["note-taker"]["widget_type"] == k.DEFAULT_WIDGET_TYPE


@pytest.mark.asyncio
@pytest.mark.parametrize("widget", sorted(k.WIDGET_EXT_MAP))
async def test_a_known_widget_type_is_kept(app, widget):
    async with client_for(app) as client:
        resp = await client.put(
            f"{BASE}/config",
            json={"config": {"meeting_agents": [{"id": "note-taker", "widget_type": widget}]}},
        )
        assert resp.status == 200, await resp.text()
        body = await (await client.get(f"{BASE}/config")).json()

    agents = {a["id"]: a for a in body["config"]["meeting_agents"]}
    assert agents["note-taker"]["widget_type"] == widget
