"""A skill create or edit whose content holds a lone surrogate answers a coded 400.

JSON allows an escaped lone surrogate (``"\\ud800"``), which ``json.loads`` hands
back as a ``str`` the SKILL.md writer cannot encode, so both routes answered 500.
The sibling prompt routes already answer ``content_not_encodable``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import kiro_crew.dashboard.handlers.prompts as prompts_mod
from kiro_crew.skills import SkillsLoader

_CODED = {"error": "content is not valid UTF-8 text", "code": "content_not_encodable"}


@pytest.fixture(autouse=True)
def _owner(_floor_monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py."""
    _floor_monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )
    _floor_monkeypatch.setattr(prompts_mod, "_sel", lambda: MagicMock())


@pytest.fixture()
def skills(tmp_path, monkeypatch):
    (tmp_path / "skills").mkdir()
    loader = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: loader)
    assert loader.create_skill("demo", "original body")
    return loader


def _app() -> web.Application:
    app = web.Application()
    state = MagicMock()
    state.context_builder = None
    app["state"] = state
    app.router.add_post("/api/skills", prompts_mod.api_skills_create)
    app.router.add_route("*", "/api/skills/{name:.+}", prompts_mod.api_skill_detail)
    return app


@pytest.mark.asyncio
async def test_create_with_a_lone_surrogate_is_a_coded_400(skills, tmp_path):
    async with TestClient(TestServer(_app())) as client:
        resp = await client.post("/api/skills", json={"name": "probe-x", "content": "body \ud800"})
        assert resp.status == 400
        assert await resp.json() == _CODED
    assert not (tmp_path / "skills" / "probe-x").exists()


@pytest.mark.asyncio
async def test_edit_with_a_lone_surrogate_is_a_coded_400(skills):
    async with TestClient(TestServer(_app())) as client:
        resp = await client.put("/api/skills/demo", json={"content": "edited \ud800"})
        assert resp.status == 400
        assert await resp.json() == _CODED
        # Valid non-ASCII content still saves.
        ok = await client.put("/api/skills/demo", json={"content": "edited \U0001f600"})
        assert ok.status == 200
    assert "\U0001f600" in (skills.load_skill("demo") or "")
