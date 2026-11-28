"""``install_pruned`` reads a prune, and only a prune."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

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


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def test_a_leftover_interpreter_in_the_pruned_tree_is_not_respawnable(
    monkeypatch, tmp_path
) -> None:
    """Windows keeps a running ``python.exe`` through a partial prune, but a
    relaunch from it cannot import the package that went with the prune."""
    old = tmp_path / "0.13.0"
    pkg = _package(old / "Lib" / "site-packages")
    python = _executable(old / "python.exe")
    venv_python = _executable(old / "bin" / "python")
    current = _executable(tmp_path / "0.13.1" / "python.exe")
    _package(tmp_path / "0.13.1" / "Lib" / "site-packages")
    monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", pkg.resolve())
    monkeypatch.setenv(install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE)

    for command in (python, venv_python):
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(command))
        assert install_liveness.respawned_by_pool() is True  # intact: unchanged
    (pkg / "__init__.py").unlink()  # the update's partial prune
    for command in (python, venv_python):
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(command))
        assert install_liveness.respawned_by_pool() is False
    for command in (current, _executable(tmp_path / "0.13.1" / "Scripts" / "kirocrew.exe")):
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(command))
        assert install_liveness.respawned_by_pool() is True  # another intact install


def test_a_pruned_venv_whose_python_links_outside_it_is_not_respawnable(
    monkeypatch, tmp_path
) -> None:
    """A venv's ``bin/python`` links to a base interpreter elsewhere; the venv,
    not the link's target, is the install the package went with."""
    venv = tmp_path / "venv-0.13.0"
    pkg = _package(venv / "lib" / "python3.12" / "site-packages")
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    base = _executable(tmp_path / "base" / "bin" / "python3")
    link = venv / "bin" / "python"
    link.parent.mkdir()
    try:
        link.symlink_to(base)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", pkg.resolve())
    monkeypatch.setenv(install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE)
    monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(link))
    assert install_liveness.respawned_by_pool() is True
    (pkg / "__init__.py").unlink()
    assert install_liveness.respawned_by_pool() is False
