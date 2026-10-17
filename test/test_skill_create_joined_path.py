"""Pins the JOINED-path refusal on ``POST /api/skills``.

The name bound measures the name alone, but ``create_skill`` hands the filesystem
``<root>/<name>/SKILL.md``, and whether that whole string fits is a property of the
host: a Windows install without long-path support refuses it past ``MAX_PATH``
while one with ``LongPathsEnabled`` writes it. So the handler does not guess a
length; it maps the create's own too-long ``OSError`` onto the coded 400 the name
bound returns, audits that refusal, and leaves every other ``OSError`` as it was.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import kiro_crew.dashboard.handlers.prompts as prompts_mod
import kiro_crew.skills as skills_mod
from kiro_crew.skills import SkillsLoader


def _winerror(code: int) -> OSError:
    """What a Windows create raises: CPython files most path errors under ``ENOENT``,
    so only ``winerror`` tells them apart."""
    exc = OSError(errno.ENOENT, f"winerror {code}")
    exc.winerror = code  # type: ignore[attr-defined]
    return exc


@pytest.fixture(autouse=True)
def _owner(_floor_monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py.

    Patches through the isolation floor's own MonkeyPatch (D11), not the shared
    ``monkeypatch``: an autouse fixture on the shared instance is lifted by any
    test that calls ``monkeypatch.undo()``.
    """
    _floor_monkeypatch.setattr(
        prompts_mod, "is_owner_dashboard_request", lambda _request: True, raising=False
    )


@pytest.fixture
def sel(monkeypatch) -> MagicMock:
    m = MagicMock()
    monkeypatch.setattr(prompts_mod, "_sel", lambda: m)
    return m


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


class _RaisingSkills:
    """A loader whose create raises *exc*, standing in for the filesystem's refusal."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls: list[tuple[str, str]] = []

    def create_skill(self, name: str, content: str) -> bool:
        self.calls.append((name, content))
        raise self.exc


def _raising(monkeypatch, exc: BaseException) -> None:
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: _RaisingSkills(exc))


async def _create(name: str) -> object:
    return await prompts_mod.api_skills_create(_FakeRequest({"name": name, "content": "body"}))


def _refused(resp) -> bool:
    return resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"


class TestSkillCreateJoinedPath:
    @pytest.mark.asyncio
    async def test_windows_filename_exceeds_range_is_a_coded_400(self, monkeypatch, sel):
        _raising(monkeypatch, _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_windows_buffer_overflow_is_a_coded_400(self, monkeypatch, sel):
        _raising(monkeypatch, _winerror(prompts_mod._WINERROR_BUFFER_OVERFLOW))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_posix_too_long_create_is_a_coded_400(self, monkeypatch, sel):
        _raising(monkeypatch, OSError(errno.ENAMETOOLONG, "File name too long"))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_ambiguous_windows_error_is_left_to_mean_what_it_says(self, monkeypatch, sel):
        # ERROR_PATH_NOT_FOUND also names a missing parent, so it is not re-read as
        # the length; it keeps the handling every other OSError has.
        exc = _winerror(3)
        _raising(monkeypatch, exc)
        with pytest.raises(OSError) as info:
            await _create("my-skill")
        assert info.value is exc

    @pytest.mark.asyncio
    async def test_unrelated_oserror_still_surfaces(self, monkeypatch, sel):
        # Only the LENGTH refusal is a request-shape error; a full disk is not.
        exc = OSError(errno.ENOSPC, "No space left on device")
        _raising(monkeypatch, exc)
        with pytest.raises(OSError) as info:
            await _create("my-skill")
        assert info.value is exc

    @pytest.mark.asyncio
    async def test_refusal_is_audited_as_rejected(self, monkeypatch, sel):
        # The write reached the filesystem, so the refusal leaves the same SEL
        # record a rejected create does, with the code that names why.
        _raising(monkeypatch, OSError(errno.ENAMETOOLONG, "File name too long"))
        assert _refused(await _create("my-skill"))
        sel.log_tool_invocation.assert_called_once()
        kwargs = sel.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "api_skills_create"
        assert kwargs["outcome"] == "rejected"
        assert kwargs["metadata"] == {"name": "my-skill", "code": "name_too_long"}

    @pytest.mark.asyncio
    async def test_real_loader_by_name_refused_leaves_nothing_behind(
        self, tmp_path, monkeypatch, sel
    ):
        # The real loader on the branch a Windows host takes (no dir_fd), with the
        # directory create refusing the path: nothing is written and the answer is
        # the coded 400 rather than the OSError escaping as a 500.
        root = tmp_path / "skills"
        root.mkdir()
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: loader)
        monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
        real_mkdir = os.mkdir

        def _mkdir(path, mode=0o777, *, dir_fd=None):
            if Path(os.fspath(path)).name == "my-skill":
                raise _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", _mkdir)
        assert _refused(await _create("my-skill"))
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_a_create_the_host_accepts_still_returns_200(self, tmp_path, monkeypatch, sel):
        # No pre-flight guess and no spurious refusal: when the filesystem accepts
        # the joined path (no OSError at all), the create still returns 200, so a
        # host that takes a long path loses nothing to the new classification.
        # Deterministic on every host -- it drives a create that simply succeeds
        # rather than probing whether the host tolerates a path past MAX_PATH; the
        # refusal arm is covered by the tests above.
        root = tmp_path / "skills"
        root.mkdir()
        name = "d" * 40
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: loader)
        resp = await _create(name)
        assert resp.status == 200
        assert (root / name / "SKILL.md").read_text(encoding="utf-8") == "body"
