"""The attributes pin must refuse a directory junction, not only a symlink.

A Windows junction is not a symlink (``S_ISLNK`` is False, ``S_ISDIR`` is True),
so a symlink-only test would write the pin THROUGH it. Linux has no junctions,
so the junction probe in ``platform_compat_links`` is faked for one path.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat_links
from kiro_crew.apps.builtins.auto_improvement.spine import git_safety
from kiro_crew.apps.builtins.auto_improvement.tests.symlink_capability import requires_symlinks


def _clone(tmp_path: Path) -> Path:
    root = tmp_path / "clone"
    (root / ".git").mkdir(parents=True)
    return root


def test_pin_refuses_info_junction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _clone(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    info = root / ".git" / "info"
    info.mkdir()  # a plain dir standing in for a junction to `outside`
    monkeypatch.setattr(
        platform_compat_links, "_ISJUNCTION", lambda p: os.fspath(p) == os.fspath(info)
    )
    with pytest.raises(git_safety.GitSafetyError):
        git_safety.pin_attributes(root)
    assert not (info / "attributes").exists()
    assert not (outside / "attributes").exists()


def test_pin_still_writes_through_plain_dirs(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    assert git_safety.pin_attributes(root) is True
    assert (root / ".git" / "info" / "attributes").read_text() == git_safety._ATTRIBUTES_PIN


@requires_symlinks
def test_pin_still_refuses_symlinked_info(tmp_path: Path) -> None:
    root = _clone(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".git" / "info").symlink_to(outside, target_is_directory=True)
    with pytest.raises(git_safety.GitSafetyError):
        git_safety.pin_attributes(root)
    assert not (outside / "attributes").exists()
