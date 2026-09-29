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

BUDGET = prompts_mod.MAX_PROMPT_NAME_BYTES


def _winerror(code: int) -> OSError:
    """What a Windows create raises: CPython files most path errors under ``ENOENT``,
    so only ``winerror`` tells them apart."""
    exc = OSError(errno.ENOENT, f"winerror {code}")
    exc.winerror = code  # type: ignore[attr-defined]
    return exc


@pytest.fixture(autouse=True)
def _owner(monkeypatch):
    """Run as the dashboard owner; the owner gate is covered in test_skill_write_guard.py."""
    monkeypatch.setattr(
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


def _host_holds(path: Path) -> bool:
    """Probe whether THIS host accepts a directory at *path*; long paths are a host
    capability (a stock Windows shell refuses one past 260 characters), not a platform."""
    try:
        path.mkdir(parents=True)
    except OSError:
        return False
    path.rmdir()
    return True


async def _create(name: str) -> object:
    return await prompts_mod.api_skills_create(_FakeRequest({"name": name, "content": "body"}))


def _refused(resp) -> bool:
    return resp.status == 400 and json.loads(resp.body)["code"] == "name_too_long"


class TestSkillCreateJoinedPath:
    @pytest.mark.asyncio
    async def test_windows_filename_exceeds_range_is_a_coded_400(self, monkeypatch):
        _raising(monkeypatch, _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_windows_buffer_overflow_is_a_coded_400(self, monkeypatch):
        _raising(monkeypatch, _winerror(prompts_mod._WINERROR_BUFFER_OVERFLOW))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_posix_too_long_create_is_a_coded_400(self, monkeypatch):
        _raising(monkeypatch, OSError(errno.ENAMETOOLONG, "File name too long"))
        assert _refused(await _create("my-skill"))

    @pytest.mark.asyncio
    async def test_ambiguous_windows_error_is_left_to_mean_what_it_says(self, monkeypatch):
        # ERROR_PATH_NOT_FOUND also names a missing parent, so it is not re-read as
        # the length; it keeps the handling every other OSError has.
        exc = _winerror(3)
        _raising(monkeypatch, exc)
        with pytest.raises(OSError) as info:
            await _create("my-skill")
        assert info.value is exc

    @pytest.mark.asyncio
    async def test_unrelated_oserror_still_surfaces(self, monkeypatch):
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
    async def test_windows_by_name_create_refused_leaves_nothing_behind(
        self, tmp_path, monkeypatch
    ):
        # The real loader on the branch a Windows host takes (no dir_fd), with the
        # directory create refusing the path: nothing is written and the answer is
        # the coded 400 rather than the OSError escaping as a 500.
        root = tmp_path / "skills"
        root.mkdir()
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: loader)
        monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
        real_mkdir = Path.mkdir

        def _refusing_mkdir(self: Path, *args, **kwargs):
            if self.name == "my-skill":
                raise _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", _refusing_mkdir)
        assert _refused(await _create("my-skill"))
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_name_of_exactly_the_budget_still_creates_where_the_host_holds_it(
        self, tmp_path, monkeypatch
    ):
        # No pre-flight guess: a host that takes the joined path keeps writing it.
        # Whether it does is probed, not assumed -- the thesis of this module is
        # that the host may refuse, and such a host is covered by the tests above.
        root = tmp_path / "skills"
        root.mkdir()
        name = "d" * BUDGET
        if not _host_holds(root / name):
            pytest.skip("this host refuses the joined path; the refusal arm is covered above")
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: loader)
        resp = await _create(name)
        assert resp.status == 200
        assert (root / name / "SKILL.md").read_text(encoding="utf-8") == "body"


def _loader_at(monkeypatch, root: Path) -> SkillsLoader:
    loader = SkillsLoader(skills_path=root, install_builtins=False)
    monkeypatch.setattr(prompts_mod, "_get_skills", lambda _s: loader)
    return loader


def _by_name_leaf_refusal(monkeypatch, leaf: str, exc: OSError) -> None:
    """On the by-name branch, make ``mkdir(parents=True)`` do what it does on a host
    that refuses the leaf: bring the intermediates into being, then fail the leaf."""
    monkeypatch.setattr(skills_mod, "_DIR_FD_SUPPORTED", False)
    real_mkdir = Path.mkdir

    def _mkdir(self: Path, *args, **kwargs):
        if self.name == leaf:
            real_mkdir(self.parent, parents=True, exist_ok=True)
            raise exc
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _mkdir)


class TestSkillCreateRefusalLeavesNoParent:
    """A nested name whose LEAF the host refuses must not strand the parent the
    create made: the exists() guard would then meet that empty parent forever and
    answer every later create of the parent name as a duplicate."""

    @pytest.mark.asyncio
    async def test_by_name_refusal_removes_the_parent_it_made(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        root.mkdir()
        _loader_at(monkeypatch, root)
        _by_name_leaf_refusal(
            monkeypatch, "my-skill", _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
        )
        assert _refused(await _create("pack/my-skill"))
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_parent_name_creates_after_the_refusal(self, tmp_path, monkeypatch):
        # The symptom itself: after a refused ``pack/my-skill``, ``pack`` is not a
        # duplicate of a skill that has no SKILL.md.
        root = tmp_path / "skills"
        root.mkdir()
        _loader_at(monkeypatch, root)
        _by_name_leaf_refusal(
            monkeypatch, "my-skill", _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
        )
        assert _refused(await _create("pack/my-skill"))
        resp = await _create("pack")
        assert resp.status == 200, json.loads(resp.body)
        assert (root / "pack" / "SKILL.md").read_text(encoding="utf-8") == "body"

    @pytest.mark.asyncio
    async def test_pre_existing_parent_survives_the_refusal(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        (root / "pack").mkdir(parents=True)
        _loader_at(monkeypatch, root)
        _by_name_leaf_refusal(
            monkeypatch, "my-skill", _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
        )
        assert _refused(await _create("pack/my-skill"))
        # Empty and removable, and still there: the create never made it.
        assert (root / "pack").is_dir()

    @pytest.mark.asyncio
    async def test_deeper_intermediates_are_all_given_back(self, tmp_path, monkeypatch):
        root = tmp_path / "skills"
        root.mkdir()
        _loader_at(monkeypatch, root)
        _by_name_leaf_refusal(
            monkeypatch, "my-skill", _winerror(prompts_mod._WINERROR_FILENAME_EXCED_RANGE)
        )
        assert _refused(await _create("a/b/c/my-skill"))
        assert list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_unclassified_oserror_still_propagates_and_leaves_no_parent(
        self, tmp_path, monkeypatch
    ):
        # The unwind is the create's, not the classifier's: any failed create gives
        # back what it made, and the error keeps its existing handling.
        root = tmp_path / "skills"
        root.mkdir()
        _loader_at(monkeypatch, root)
        exc = OSError(errno.ENOSPC, "No space left on device")
        _by_name_leaf_refusal(monkeypatch, "my-skill", exc)
        with pytest.raises(OSError) as info:
            await _create("pack/my-skill")
        assert info.value is exc
        assert list(root.iterdir()) == []

    @pytest.mark.skipif(not skills_mod._DIR_FD_SUPPORTED, reason="pinned branch needs dir_fd")
    @pytest.mark.asyncio
    async def test_pinned_refusal_removes_the_parent_it_made(self, tmp_path, monkeypatch):
        # The pinned branch makes the intermediates by name, then the leaf under a
        # descriptor; a leaf the host refuses must give the intermediates back too.
        root = tmp_path / "skills"
        root.mkdir()
        _loader_at(monkeypatch, root)
        real_mkdir = os.mkdir

        def _mkdir(path, mode=0o777, *, dir_fd=None):
            if dir_fd is not None and os.fspath(path) == "my-skill":
                raise OSError(errno.ENAMETOOLONG, "File name too long")
            return real_mkdir(path, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "mkdir", _mkdir)
        assert _refused(await _create("pack/my-skill"))
        assert list(root.iterdir()) == []
        resp = await _create("pack")
        assert resp.status == 200, json.loads(resp.body)
