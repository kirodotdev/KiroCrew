# SPDX-License-Identifier: Apache-2.0
"""``GET /api/agents`` says which crewmates are bound to their own mate file.

The roster's Built from label reads ``owns_mate_file``. Matching names are not
enough: a crewmate whose id equals a user-level agent name stays on that shared
template with no ``mates/agents/<id>.json``, so the row must say False and the
label names the template, as the editor does.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def _make_app() -> web.Application:
    from kiro_crew.dashboard.handlers import api_kirocrew_agents

    app = web.Application()
    app.router.add_get("/api/agents", api_kirocrew_agents)
    return app


@pytest.mark.asyncio
async def test_owns_mate_file_needs_the_file_not_just_matching_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    (mates / "ben.json").write_text(json.dumps({"name": "ben"}), encoding="utf-8")
    monkeypatch.setattr("kiro_crew.config.paths.mate_agents_dir", lambda: mates)

    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        json.dumps(
            {
                "agents": {
                    # Same names, no mate file: left on the shared template.
                    "reviewer": {"kiro_agent": "reviewer", "source": "kirocrew"},
                    # Same names, mate file present.
                    "ben": {"kiro_agent": "ben", "source": "kirocrew"},
                    # Bound to a different template.
                    "other": {"kiro_agent": "kirocrew", "source": "kirocrew"},
                },
                "default_agent": "other",
            }
        ),
        encoding="utf-8",
    )
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=cfg_file):
        async with TestClient(TestServer(_make_app())) as client:
            resp = await client.get("/api/agents")
            assert resp.status == 200
            rows = {a["name"]: a for a in (await resp.json())["agents"]}

    assert rows["reviewer"]["owns_mate_file"] is False
    assert rows["ben"]["owns_mate_file"] is True
    assert rows["other"]["owns_mate_file"] is False


def test_binding_is_mate_and_the_roster_share_one_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kiro_crew import mate_capabilities

    mates = tmp_path / "mates" / "agents"
    mates.mkdir(parents=True)
    monkeypatch.setattr("kiro_crew.config.paths.mate_agents_dir", lambda: mates)
    assert mate_capabilities.owns_mate_file("reviewer", "reviewer") is False
    (mates / "reviewer.json").write_text("{}", encoding="utf-8")
    assert mate_capabilities.owns_mate_file("reviewer", "reviewer") is True
    assert mate_capabilities.owns_mate_file("reviewer", "kirocrew") is False
    assert mate_capabilities.owns_mate_file("reviewer", "") is False
