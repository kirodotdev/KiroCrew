"""``install_pruned`` reads a prune, and only a prune."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from kiro_crew import install_liveness


def _package(root: Path) -> Path:
    pkg = root / "kiro_crew"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    return pkg


def test_the_running_install_is_not_pruned() -> None:
    assert install_liveness.install_pruned() is False


def test_a_removed_version_directory_is_pruned(tmp_path: Path) -> None:
    pkg = _package(tmp_path / "0.8.0.4")
    assert install_liveness.install_pruned(pkg) is False
    shutil.rmtree(tmp_path / "0.8.0.4")
    assert install_liveness.install_pruned(pkg) is True


def test_an_emptied_package_directory_is_pruned(tmp_path: Path) -> None:
    """A prune that leaves the directory node behind is still a prune."""
    pkg = _package(tmp_path)
    (pkg / "__init__.py").unlink()
    assert pkg.is_dir()
    assert install_liveness.install_pruned(pkg) is True


def test_only_the_pool_marker_counts_as_respawnable(monkeypatch) -> None:
    from kiro_crew import install_liveness

    monkeypatch.delenv(install_liveness.POOLED_BACKEND_ENV, raising=False)
    monkeypatch.setenv("KIROCREW_SPAWNED", "1")  # set on agent runtimes too
    assert install_liveness.respawned_by_pool() is False
    monkeypatch.setenv(install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE)
    monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
    assert install_liveness.respawned_by_pool() is True


def test_a_pooled_backend_whose_respawn_command_was_pruned_is_not_respawnable(
    monkeypatch, tmp_path
) -> None:
    """The pool respawns from its frozen target, so a launcher that went with
    the prune would fail to relaunch and leave the session with no tools."""
    from kiro_crew import install_liveness

    launcher = tmp_path / "0.8.0.4" / "bin" / "kirocrew"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\n")
    launcher.chmod(0o755)
    monkeypatch.setenv(install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE)
    monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(launcher))
    assert install_liveness.respawned_by_pool() is True
    launcher.unlink()  # the update's prune
    assert install_liveness.respawned_by_pool() is False
    monkeypatch.delenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV)
    assert install_liveness.respawned_by_pool() is False
