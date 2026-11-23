"""usable_workspace_dir_from_entry hands back the path it judged, not a spelling.

The folder is judged through ``realpath`` but was returned unresolved, so a link
swapped in after the judgment decided where the run started. An existing folder
now comes back as its realpath; a folder that is not there yet is created
without following a link planted at the name, then judged again before use.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import security
from kiro_crew.config.loader_locations import (
    usable_workspace_dir_from_entry,
    workspace_dir_from_entry,
)
from kiro_crew.config.sections import WorkspaceConfig
from kiro_crew.testing.links import make_dir_link


def _entry(path: Path) -> WorkspaceConfig:
    return WorkspaceConfig(dir=str(path))


def _mark_sensitive(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    """Treat *target* and everything under it as a sensitive path."""
    root = os.path.realpath(target)
    monkeypatch.setattr(
        security,
        "is_sensitive_canonical_path",
        lambda p: p == root or p.startswith(root + os.sep),
    )


def test_existing_folder_through_a_link_is_returned_as_its_realpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mark_sensitive(monkeypatch, tmp_path / "nowhere")
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    make_dir_link(alias, real)

    got = usable_workspace_dir_from_entry(_entry(alias), "w")

    assert got == Path(os.path.realpath(real))
    assert got != alias


def test_link_planted_at_a_missing_folder_after_the_judgment_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    alias = tmp_path / "alias"  # not created yet: a scheduled run makes it on use
    root = os.path.realpath(secret)
    planted: list[bool] = []

    def judge(p: str) -> bool:
        if not planted:  # the first judgment sees a missing folder, then the swap lands
            planted.append(True)
            make_dir_link(alias, secret)
        return p == root or p.startswith(root + os.sep)

    monkeypatch.setattr(security, "is_sensitive_canonical_path", judge)

    got = usable_workspace_dir_from_entry(_entry(alias), "w", must_exist=False)

    assert got == workspace_dir_from_entry(None)
    assert not os.path.realpath(got).startswith(root)
