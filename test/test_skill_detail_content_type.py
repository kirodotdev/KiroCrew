"""Pins the coded 400 for a non-string ``content`` on ``PUT /api/skills/{name}``.

The handler checked only that ``content`` was truthy, so a number, list, object
or ``true`` reached the SKILL.md writer and failed there with a 500. The create
route already answers this with ``invalid_field_type``; the edit route now does
the same, and the file on disk is left as it was.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers.prompts as prompts_mod
from kiro_crew.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _owner(_floor_monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py."""
    _floor_monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )


@pytest.fixture(autouse=True)
def _sel(_floor_monkeypatch) -> MagicMock:
    m = MagicMock()
    _floor_monkeypatch.setattr(prompts_mod, "_sel", lambda: m)
    return m


@pytest.fixture()
def loader(tmp_path, _floor_monkeypatch) -> SkillsLoader:
    (tmp_path / "skills").mkdir()
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    _floor_monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: skills)
    assert skills.create_skill("demo", "original body")
    return skills


class _FakeRequest:
    """The slice of ``web.Request`` the detail handler reads for a PUT."""

    def __init__(self, body: dict) -> None:
        self.method = "PUT"
        self.match_info = {"name": "demo"}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        self.headers: dict = {}
        self.cookies: dict = {}
        self.query: dict = {}
        self._body = body

    async def json(self) -> dict:
        return self._body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [5, True, ["a"], {"a": 1}, 0, False, None, [], {}],
    ids=["int", "bool", "list", "dict", "zero", "false", "null", "empty-list", "empty-dict"],
)
async def test_a_non_string_content_is_a_coded_400(loader, content):
    resp = await prompts_mod.api_skill_detail(_FakeRequest({"content": content}))
    assert resp.status == 400
    assert json.loads(resp.body) == {
        "error": "content must be a string",
        "code": "invalid_field_type",
    }
    assert loader.load_skill("demo") == "original body"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"content": ""}], ids=["absent", "empty-string"])
async def test_a_missing_or_empty_content_is_still_required(loader, body):
    resp = await prompts_mod.api_skill_detail(_FakeRequest(body))
    assert resp.status == 400
    assert json.loads(resp.body) == {"error": "content is required"}
    assert loader.load_skill("demo") == "original body"


@pytest.mark.asyncio
async def test_a_string_content_still_saves(loader):
    resp = await prompts_mod.api_skill_detail(_FakeRequest({"content": "new body"}))
    assert resp.status == 200
    assert loader.load_skill("demo") == "new body"
