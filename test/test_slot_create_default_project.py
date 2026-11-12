"""A new chat opens in ``dashboard.default_project`` while the resolver is stalled.

``_birth_default_project`` resolves the configured directory itself on a worker
thread, so it asks the canonical gate about that path, which answers inline off the
event loop. A stall cooldown over a healthy directory therefore does not read as a
sensitive match, and a directory on the fence is still skipped.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.security import paths

_WORKSPACE_DEFAULT = "workspace-default"


def _cfg(project: Path) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.dashboard.default_project = str(project)
    return cfg


@pytest.fixture
def _workspace_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_handlers, "default_project_dir", lambda _ws: _WORKSPACE_DEFAULT)


@pytest.mark.asyncio
async def test_a_stall_cooldown_keeps_the_configured_default_project(
    tmp_path: Path, _workspace_default: None
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    paths._mark_stalled(paths._stall_prefix(str(project)), 2.0)

    born = await chat_handlers._birth_default_project(_cfg(project), "default")

    assert born == os.path.realpath(project)


@pytest.mark.asyncio
async def test_a_sensitive_default_project_still_falls_back(_workspace_default: None) -> None:
    # The crew home is a per-test tmp dir here; ``diag`` is on its fence.
    fenced = Path(os.environ["KIROCREW_HOME"]) / "diag"
    fenced.mkdir(parents=True, exist_ok=True)

    born = await chat_handlers._birth_default_project(_cfg(fenced), "default")

    assert born == _WORKSPACE_DEFAULT
