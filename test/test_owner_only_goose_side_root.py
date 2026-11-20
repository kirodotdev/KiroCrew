"""The side goose home is owner-only all the way down, parent folder included."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.harness.goose import build_side_goose_root

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_the_side_root_and_its_config_folder_are_owner_only(tmp_path: Path) -> None:
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    root = build_side_goose_root(tmp_path / "absent", scratch)
    assert root.name == "goose-side-root"
    assert _mode(root) == 0o700
    assert _mode(root / "config") == 0o700
