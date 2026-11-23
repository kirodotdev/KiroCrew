"""The MCP config cleanup lock is created owner-only (0600) and never re-moded.

``_McpFileLockSync`` touches an empty sidecar lock beside the global MCP file.
Under the usual ``022`` umask a bare ``touch()`` made it ``0644``, so any local
user could see that the MCP file was in use. A lock that already exists keeps its
mode: creation decides the mode, the startup sweep tightens what is already there.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers import mcp as mcp_handlers
from kiro_crew.dashboard.handlers import mcp_config_files

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def lock_path(tmp_path: Path, _floor_monkeypatch: pytest.MonkeyPatch) -> Path:
    settings = tmp_path / "settings"
    # Through the facade: the paths are names its mirrored family shares.
    _floor_monkeypatch.setattr(mcp_handlers, "_GLOBAL_MCP_JSON", settings / "mcp.json")
    _floor_monkeypatch.setattr(mcp_handlers, "_MCP_LOCK_PATH", settings / "mcp.lock")
    return settings / "mcp.lock"


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_new_lock_is_owner_only(lock_path: Path) -> None:
    with mcp_config_files._get_mcp_lock_sync():
        pass
    assert _mode(lock_path) == 0o600


def test_an_existing_lock_keeps_its_mode(lock_path: Path) -> None:
    lock_path.parent.mkdir()
    lock_path.touch()
    lock_path.chmod(0o644)
    with mcp_config_files._get_mcp_lock_sync():
        pass
    assert _mode(lock_path) == 0o644
