"""Dashboard readers of a path they resolved themselves keep working through a resolver stall.

Each site here resolves the path itself (``os.path.realpath`` or ``Path.resolve``) on a
worker and asks the canonical gate about the result, which answers inline off the event
loop. A stall cooldown over a healthy path's prefix therefore does not read as a
sensitive match, and a path on the fence is still refused. Every site is run the way the
dashboard runs it: on a worker thread, or through the HTTP route that offloads it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from test_project_tree import (  # noqa: F401
    _make_app,
    mock_sel,
    passthrough_sandbox,
    plain_project,
)

from kiro_crew.dashboard import handlers
from kiro_crew.platform.defaults import DefaultPromptSourceProvider
from kiro_crew.security import paths


def _stall(path: Path) -> None:
    paths._mark_stalled(paths._stall_prefix(str(path)), 2.0)


def _fenced(*parts: str) -> Path:
    # The crew home is a per-test tmp dir here; ``diag`` is on its fence.
    d = Path(os.environ["KIROCREW_HOME"], "diag", *parts)
    d.mkdir(parents=True, exist_ok=True)
    return d


class TestProjectTree:
    @staticmethod
    async def _get(project: Path, **query: str) -> tuple[int, dict]:
        async with TestClient(TestServer(_make_app(str(project)))) as client:
            resp = await client.get("/api/project/tree", params={"path": str(project), **query})
            return resp.status, await resp.json()

    @pytest.mark.asyncio
    async def test_a_stall_cooldown_lists_a_healthy_project_and_folder(
        self, plain_project, mock_sel  # noqa: F811
    ):
        (plain_project / "sub").mkdir(parents=True)
        (plain_project / "sub" / "a.txt").write_text("x")
        _stall(plain_project)

        status, whole = await self._get(plain_project)
        assert status == 200, whole
        status, folder = await self._get(plain_project, dir="sub")
        assert status == 200, folder
        assert folder["paths"] == ["sub/a.txt"]

    @pytest.mark.asyncio
    async def test_a_fenced_folder_is_still_refused(self, mock_sel):  # noqa: F811
        # Real paths, so the folder is refused by the fence and not as a link.
        fenced = Path(os.path.realpath(_fenced()))
        status, data = await self._get(fenced.parent.parent, dir=f"{fenced.parent.name}/diag")
        assert status == 403
        assert data["code"] == "dir_refused"
        status, data = await self._get(fenced)
        assert status == 403
        assert data["code"] == "access_denied"


class TestPrompts:
    @staticmethod
    async def _entry(root: Path) -> dict | None:
        (root / "hello.md").write_text("# Hello\n")
        return await asyncio.to_thread(
            handlers._prompt_dir_entry, root / "hello.md", root.resolve(), "local"
        )

    @staticmethod
    async def _edition_sops(monkeypatch, home: Path, root: Path) -> list[str]:
        (root / "a.sop.md").write_text("# A\n")
        monkeypatch.setattr(DefaultPromptSourceProvider, "prompt_source_roots", lambda self: [root])
        # The scan also lists ~/.kiro/prompts; keep the real home's prompts out of it.
        monkeypatch.setattr(Path, "home", lambda: home)
        return [p["name"] for p in await asyncio.to_thread(handlers._build_prompt_base)]

    @pytest.mark.asyncio
    async def test_a_stall_cooldown_keeps_a_user_prompt(self, tmp_path):
        root = tmp_path / "prompts"
        root.mkdir()
        _stall(root)
        entry = await self._entry(root)
        assert entry is not None and entry["name"] == "hello"

    @pytest.mark.asyncio
    async def test_a_fenced_user_prompt_is_still_dropped(self):
        assert await self._entry(_fenced("prompts")) is None

    @pytest.mark.asyncio
    async def test_a_stall_cooldown_keeps_an_edition_sop(self, tmp_path, monkeypatch):
        root = tmp_path / "Pkg-1.0"
        root.mkdir()
        _stall(root)
        assert "a" in await self._edition_sops(monkeypatch, tmp_path, root)

    @pytest.mark.asyncio
    async def test_a_fenced_edition_sop_is_still_dropped(self, tmp_path, monkeypatch):
        assert "a" not in await self._edition_sops(monkeypatch, tmp_path, _fenced("Pkg-1.0"))
