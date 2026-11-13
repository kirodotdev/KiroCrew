"""The Meetings store is one of the data-home stores the core keeps owner-only.

``kiro_crew.owner_only_files`` names ``apps/meetings/data`` in its startup-sweep
list, spelled out because the app imports the core and not the other way round.
These cases pin the app's half: the store the sweep lists is the one the app
writes, and a transcript the app writes inside the data home is created ``0600``
(and keeps the plain umask mode outside it). The core's own sweep cases are in
``test/test_owner_only_files.py``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import owner_only_files as oof
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.constants import APP_NAME
from kiro_crew.config.paths import config_dir


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


def _mode(path: Path | str) -> int:
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


def test_the_startup_sweep_lists_the_store_the_app_writes() -> None:
    assert f"apps/{APP_NAME}/data" in oof.STARTUP_SWEEP_STORES


@pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")
def test_a_meeting_transcript_is_owner_only_in_the_home_and_plain_outside(tmp_path: Path) -> None:
    home = config_dir()
    inside = home / "apps" / APP_NAME / "data"
    store.append_transcript("m1", "inside", "speech", inside)
    store.append_transcript("m1", "outside", "speech", tmp_path / "shared")

    assert _mode(store.transcript_path("m1", inside)) == 0o600
    assert _readable_by_others(home / "apps") == []
    assert _mode(store.transcript_path("m1", tmp_path / "shared")) == 0o644
