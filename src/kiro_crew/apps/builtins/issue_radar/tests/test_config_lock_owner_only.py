"""Issue Radar's config lock is owner-only, like everything else in the data home.

The core pins the rule for its own stores in ``test/test_owner_only_files.py``: under
the usual ``022`` umask a file created without an explicit mode lands at ``0644``.
This is the same check for the lock beside the app's ``config.json``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.apps.builtins.issue_radar.backend import store_repos
from kiro_crew.config.paths import config_dir

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _readable_by_others(root: Path) -> list[str]:
    """Every file or directory under *root* with a group/other bit, links excluded."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath, name)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                continue
            if stat.S_IMODE(st.st_mode) & 0o077:
                found.append(f"{oct(stat.S_IMODE(st.st_mode))} {path.relative_to(root)}")
    return found


def test_the_config_lock_is_owner_only() -> None:
    home = config_dir()
    with store_repos._config_lock():
        pass

    assert _mode(store_repos.data_dir() / "config.json.lock") == 0o600
    assert _readable_by_others(home) == []
