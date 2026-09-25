"""Pins the name-LENGTH bound on ``POST /api/skills``.

Without it the whole submitted name reaches ``create_skill``, where an over-long
component, an over-long joined path and an over-deep nesting each raise something
no caller maps, so the client gets a 500 instead of the coded 400 the sibling
``api_prompt_create`` returns. One arm per raise, plus the names still written.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import kiro_crew.dashboard.handlers.prompts as prompts_mod

BUDGET = prompts_mod.MAX_PROMPT_NAME_BYTES


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py."""
    monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )


class _FakeRequest:
    """The slice of ``web.Request`` the create handler actually reads."""

    def __init__(self, body: dict) -> None:
        self.method = "POST"
        self.match_info: dict = {}
        self.app = {"state": SimpleNamespace(context_builder=None)}
        self.headers: dict = {}
        self._body = body

    async def json(self) -> dict:
        return self._body


class _RecordingSkills:
    """Records ``create_skill`` so a test can assert the write was (not) reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def create_skill(self, name: str, content: str) -> bool:
        self.calls.append((name, content))
        return True


@pytest.fixture
def recorder(monkeypatch) -> _RecordingSkills:
    rec = _RecordingSkills()
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: rec)
    return rec


async def _create(name: str) -> object:
    return await prompts_mod.api_skills_create(_FakeRequest({"name": name, "content": "body"}))


def _refused(resp, recorder) -> bool:
    return (
        resp.status == 400
        and json.loads(resp.body)["code"] == "name_too_long"
        and recorder.calls == []
    )


class TestSkillCreateNameLength:
    @pytest.mark.asyncio
    async def test_component_over_the_filesystem_cap_is_refused(self, recorder):
        # One flat component: the arm where skill_dir.exists() raises ENAMETOOLONG.
        assert _refused(await _create("a" * (BUDGET + 1)), recorder)

    @pytest.mark.asyncio
    async def test_long_joined_path_with_short_components_is_refused(self, recorder):
        # Every component short, the JOINED path too long: a per-component bound misses it.
        assert _refused(await _create("/".join(["b" * 20] * 30)), recorder)

    @pytest.mark.asyncio
    async def test_deeply_nested_name_is_refused_before_the_recursive_mkdir(self, recorder):
        # One byte per segment: the arm where mkdir(parents=True) recurses per level.
        assert _refused(await _create("/".join(["c"] * 1100)), recorder)

    @pytest.mark.asyncio
    async def test_name_of_exactly_the_budget_still_creates(self, recorder):
        # Pins the accept side of the boundary, so relaxing `>` to `>=` reds here.
        name = "d" * BUDGET
        resp = await _create(name)
        assert resp.status == 200
        assert recorder.calls == [(name, "body")]

    @pytest.mark.asyncio
    async def test_nested_name_within_budget_still_creates(self, recorder):
        # Nesting is a documented skill-name shape, so the bound must not retire it.
        resp = await _create("pack/my-skill")
        assert resp.status == 200
        assert recorder.calls == [("pack/my-skill", "body")]

    @pytest.mark.asyncio
    async def test_ordinary_name_still_creates(self, recorder):
        resp = await _create("my-skill")
        assert resp.status == 200
        assert recorder.calls == [("my-skill", "body")]
