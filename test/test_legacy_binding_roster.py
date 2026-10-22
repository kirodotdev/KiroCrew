# SPDX-License-Identifier: Apache-2.0
"""A legacy built-in binding with no member id is not listed as a crewmate.

An older install's ``assistant`` binding is ``source: builtin`` with no
``member_id``. ``GET /api/agents`` (Customize > Crewmates and the crewmate
pickers) and ``GET /api/members`` leave it out; the reserved ``default`` member
and a user crewmate that has no id yet stay listed.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.mate_worker import is_legacy_binding

CONFIG = {
    "agents": {
        "default": {"kiro_agent": "kirocrew", "source": "builtin"},
        "helper": {"kiro_agent": "kirocrew", "source": "builtin", "member_id": ""},
        "ada": {"kiro_agent": "ada", "source": "kirocrew", "member_id": "ada"},
        "shared-v1": {"kiro_agent": "kirocrew", "source": "kirocrew"},
    },
    "default_agent": "default",
}


def test_only_a_builtin_binding_without_an_id_is_legacy():
    assert is_legacy_binding("assistant", SimpleNamespace(source="builtin", member_id=""))
    assert not is_legacy_binding("default", SimpleNamespace(source="builtin", member_id=""))
    assert not is_legacy_binding("mate", SimpleNamespace(source="builtin", member_id="mate"))
    assert not is_legacy_binding("shared-v1", SimpleNamespace(source="kirocrew", member_id=""))


@pytest.mark.asyncio
async def test_the_agents_roster_leaves_the_legacy_binding_out(tmp_path: Path) -> None:
    from kiro_crew.dashboard.handlers import api_kirocrew_agents

    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=path):
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/agents")
            names = {a["name"] for a in (await resp.json())["agents"]}
    assert "helper" not in names
    assert {"default", "ada", "shared-v1"} <= names
