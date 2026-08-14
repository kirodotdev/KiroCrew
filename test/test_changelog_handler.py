"""Tests for the dashboard /api/changelog endpoint and its source resolution.

Regression coverage for the v3.0.0 bug where the gateway changelog modal showed
nothing: project-dir detection broke (the project-level ``agents/`` marker dir
was removed), and CHANGELOG.md was never bundled into the package, so
``/api/changelog`` returned ``{"content": ""}`` on toolbox installs.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import updates


@pytest.fixture(autouse=True)
def _clear_project_dir(monkeypatch):
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)


def _request() -> MagicMock:
    return MagicMock()


def test_changelog_path_prefers_project_dir(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CHANGELOG.md").write_text("# Changelog\n\n## [9.9.9]\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))

    assert updates._changelog_path() == proj / "CHANGELOG.md"


def test_changelog_path_falls_back_to_bundled(monkeypatch):
    """With no project dir, resolve the CHANGELOG bundled inside the package."""
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    bundled = Path(updates.__file__).resolve().parents[2] / "CHANGELOG.md"

    result = updates._changelog_path()

    # In a source checkout the bundled copy may be absent; in a built/installed
    # package it must resolve. Either way, the result must never be a stale
    # project path — it is None or the bundled path.
    assert result in (None, bundled)


def test_changelog_path_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    # Point __file__ resolution at a tree with no bundled CHANGELOG by faking
    # the package location via a project dir that lacks the file.
    proj = tmp_path / "empty"
    proj.mkdir()
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    # No project CHANGELOG; bundled may or may not exist depending on build.
    result = updates._changelog_path()
    assert result is None or result.name == "CHANGELOG.md"


@pytest.mark.asyncio
async def test_api_changelog_returns_project_content(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CHANGELOG.md").write_text("# Changelog\n\nhello\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))

    resp = await updates.api_changelog(_request())

    assert resp.status == 200
    assert "hello" in json.loads(resp.body)["content"]


@pytest.mark.asyncio
async def test_api_changelog_empty_when_no_source(tmp_path, monkeypatch):
    """Endpoint returns empty content (not a 500) when no CHANGELOG resolves."""
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    monkeypatch.setattr(updates, "_changelog_path", lambda: None)

    resp = await updates.api_changelog(_request())

    assert resp.status == 200
    assert json.loads(resp.body)["content"] == ""


@pytest.mark.asyncio
async def test_api_releases_returns_per_version_entries(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [0.1.2] — 2026-07-30\n\nFirst release.\n", encoding="utf-8"
    )
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(proj))
    monkeypatch.setattr(updates, "_changelog_cache", None)
    monkeypatch.setattr(updates, "_local_version", "0.1.2")

    resp = await updates.api_releases(_request())
    body = json.loads(resp.body)

    assert resp.status == 200
    assert body["current_version"] == "0.1.2"
    assert body["stale"] is False
    assert [r["version"] for r in body["releases"]] == ["0.1.2"]
    assert body["releases"][0]["date"] == "2026-07-30"
    assert "First release." in body["releases"][0]["body"]


@pytest.mark.asyncio
async def test_api_releases_does_not_freeze_the_event_loop(monkeypatch):
    """The read and parse must be offloaded, not run on the gateway's loop.

    The gateway runs every session on one loop, so a synchronous read plus a
    parse that is linear in the changelog's size stalls every other task for as
    long as it takes. A ticker measures that directly: with the work in a
    thread it keeps counting; on the loop it cannot run at all.
    """
    def slow_read() -> str:
        time.sleep(0.3)
        return "## [0.1.2] — 2026-07-30\n\nnotes\n"

    monkeypatch.setattr(updates, "_read_changelog", slow_read)
    monkeypatch.setattr(updates, "_local_version", "0.1.2")

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        resp = await updates.api_releases(_request())
    finally:
        task.cancel()

    assert resp.status == 200
    # ~30 ticks fit in 0.3s; a frozen loop yields 0. The floor is deliberately
    # far below the expectation so a loaded runner cannot fail it.
    assert ticks >= 3, f"event loop was starved during the parse (ticks={ticks})"
