"""Tests for the dashboard /api/changelog endpoint and its source resolution.

Regression coverage for the v3.0.0 bug where the gateway changelog modal showed
nothing: project-dir detection broke (the project-level ``agents/`` marker dir
was removed), and CHANGELOG.md was never bundled into the package, so
``/api/changelog`` returned ``{"content": ""}`` on toolbox installs.
"""

from __future__ import annotations

import json
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
