"""Pins the coded 404 on ``GET/PUT/DELETE /api/skills/{name}``.

A name the loader refuses (a rooted path, ``.``, ``..``) or one nobody installed
answers 404 with ``code: skill_not_found``, so the dashboard can switch on the
code instead of the English ``error`` text.
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


@pytest.fixture(autouse=True)
def _loader(tmp_path, _floor_monkeypatch) -> SkillsLoader:
    (tmp_path / "skills").mkdir()
    skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
    _floor_monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: skills)
    return skills


class _FakeRequest:
    """The slice of ``web.Request`` the detail handler reads for these verbs."""

    def __init__(self, method: str, name: str, body: dict | None = None) -> None:
        self.method = method
        self.match_info = {"name": name}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        self.headers: dict = {}
        self.cookies: dict = {}
        self.query: dict = {}
        self._body = body or {}

    async def json(self) -> dict:
        return self._body


async def _call(method: str, name: str):
    body = {"content": "body"} if method == "PUT" else None
    return await prompts_mod.api_skill_detail(_FakeRequest(method, name, body))


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ("GET", "PUT", "DELETE"))
@pytest.mark.parametrize("name", ("/etc/x", ".", "..", "no-such-skill"))
async def test_unknown_or_refused_name_is_a_coded_404(method, name):
    resp = await _call(method, name)
    assert resp.status == 404
    assert json.loads(resp.body) == {"error": "not found", "code": "skill_not_found"}
