"""The run archive the app writes under the data home is owner-only: files 0600.

The test runs under a ``022`` umask, the usual default, which is the condition the
defect needed: a file created without an explicit mode lands at ``0644``.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir

pytestmark = pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX mode bits")


@pytest.fixture(autouse=True)
def _umask_022() -> Iterator[None]:
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def data_home() -> Path:
    """The test's (isolated) data home, created the way production creates it."""
    return config_dir()


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


def test_the_run_archive_is_owner_only(data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Through the root the supervisor passes (``store.results_dir()``), not a tmp dir."""
    from kiro_crew.apps.builtins.auto_improvement.backend import store
    from kiro_crew.apps.builtins.auto_improvement.spine.archive import Archive
    from kiro_crew.apps.manager import app_data_dir

    # The app's conftest points ``data_dir`` at a bare temp directory; this test is
    # about the production root under the data home, so it is pointed back there.
    data = Path(app_data_dir(store.APP_NAME))
    data.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(store, "data_dir", lambda: data)
    root = store.results_dir()
    assert data_home in root.parents
    archive = Archive(root)
    archive.write_meta({"run": 1})
    archive.save_candidate(cand_id="c1", diff="--- a\n+++ b\n", detail={"k": "v"})
    archive.append_row({"status": "kept"})
    archive.write_drift(1, {"note": "x"})

    assert _readable_by_others(data_home / "apps") == []
    assert _mode(root / "results.tsv") == 0o600
    assert _mode(root / "candidates.jsonl") == 0o600
    assert _mode(root / "candidates" / "c1.diff") == 0o600
