"""Pins the name-too-long refusal on ``GET/PUT/DELETE /api/skills/{name}``.

The URL name reaches the loader unbounded, and the loader's first ``exists()`` /
``is_dir()`` on ``<root>/<name>`` raises when the filesystem refuses the path for
its length. That escaped the handler as an uncoded 500. The handler now maps the
filesystem's own too-long refusal onto the coded 400 ``POST /api/skills`` already
answers, and leaves every other ``OSError`` as it was.
"""

from __future__ import annotations

import errno
import json
import sys
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


@pytest.fixture
def sel(monkeypatch) -> MagicMock:
    m = MagicMock()
    monkeypatch.setattr(prompts_mod, "_sel", lambda: m)
    return m


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


class _RaisingSkills:
    """A loader whose every verb raises *exc*, standing in for the filesystem."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    def load_skill(self, name: str) -> str | None:
        raise self.exc

    def update_skill(self, name: str, content: str) -> bool:
        raise self.exc

    def delete_skill(self, name: str) -> bool:
        raise self.exc


def _use(monkeypatch, skills) -> None:
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _state: skills)


async def _call(method: str, name: str):
    body = {"content": "body"} if method == "PUT" else None
    return await prompts_mod.api_skill_detail(_FakeRequest(method, name, body))


def _is_name_too_long(resp) -> bool:
    return resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"


VERBS = ("GET", "PUT", "DELETE")


class TestMappedRefusal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", VERBS)
    async def test_enametoolong_is_a_coded_400(self, monkeypatch, sel, method):
        _use(monkeypatch, _RaisingSkills(OSError(errno.ENAMETOOLONG, "File name too long")))
        assert _is_name_too_long(await _call(method, "a" * 300))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", VERBS)
    async def test_windows_path_too_long_is_a_coded_400(self, monkeypatch, sel, method):
        # CPython files a Windows too-long path under ENOENT; only winerror says so.
        exc = OSError(errno.ENOENT, "winerror 206")
        exc.winerror = 206  # type: ignore[attr-defined]
        _use(monkeypatch, _RaisingSkills(exc))
        assert _is_name_too_long(await _call(method, "a" * 300))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ("PUT", "DELETE"))
    async def test_a_refused_mutation_is_audited(self, monkeypatch, sel, method):
        _use(monkeypatch, _RaisingSkills(OSError(errno.ENAMETOOLONG, "File name too long")))
        await _call(method, "a" * 300)
        kwargs = sel.log_tool_invocation.call_args.kwargs
        assert kwargs["outcome"] == "rejected"
        assert kwargs["metadata"]["reason"] == "name_too_long"
        assert kwargs["tool_name"] == (
            "api_skill_update" if method == "PUT" else "api_skill_delete"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", VERBS)
    async def test_any_other_oserror_still_raises(self, monkeypatch, sel, method):
        _use(monkeypatch, _RaisingSkills(PermissionError(errno.EACCES, "denied")))
        with pytest.raises(PermissionError):
            await _call(method, "my-skill")


@pytest.mark.skipif(
    sys.platform == "win32", reason="the 300-byte component is the POSIX ENAMETOOLONG arm"
)
class TestRealLoader:
    """The reported case, end to end over a real ``SkillsLoader``."""

    @pytest.fixture
    def loader(self, tmp_path, monkeypatch) -> SkillsLoader:
        # The root must exist, as it does on a real install: with no root the
        # kernel stops at the missing parent (ENOENT) and never judges the
        # over-long component, so the probe answers "absent" instead of raising.
        (tmp_path / "skills").mkdir()
        skills = SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)
        _use(monkeypatch, skills)
        return skills

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", VERBS)
    async def test_a_300_character_name_is_a_coded_400(self, loader, sel, method):
        assert _is_name_too_long(await _call(method, "a" * 300))

    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", VERBS)
    async def test_a_missing_ordinary_name_is_still_404(self, loader, sel, method):
        assert (await _call(method, "no-such-skill")).status == 404
