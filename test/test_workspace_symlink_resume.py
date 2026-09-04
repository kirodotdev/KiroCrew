"""Regression test: cold session resume on hosts with a symlinked workspace.

On hosts where ``$HOME``/workspace are symlinks (e.g.
``/home/<u> -> /local/home/<u>``, ``/home/<u>/workplace -> /workplace/<u>``),
the symlink-form path and its realpath name the same directory via different
strings. The per-session work_dir is passed as the spawn cwd and persisted as
``cwd`` in ``session_map.json``, while the kiro-cli transcript is written under
the resolved path. If the per-session work_dir keeps the symlink form, the
persisted cwd points at a directory that does not agree with the resolved form
and cold resume silently falls back to a fresh session.

``workspace_root`` is realpath-normalized at the source so the SAME resolved
path flows into the spawn cwd and the stored ``session_map`` cwd — making write
and resume agree on every platform.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.config.loader import workspace_root


def _make_aliased_workspace(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real workspace dir plus a directory alias that points at it.

    Returns ``(alias_form, real_form)`` where ``alias_form`` traverses a
    symlink or junction parent — the cloud-desktop topology in miniature.
    """
    real_parent = tmp_path / "local" / "ws"
    real_parent.mkdir(parents=True)
    link_parent = tmp_path / "home" / "ws"
    link_parent.parent.mkdir(parents=True)
    platform_compat.symlink_or_junction(real_parent, link_parent)
    return link_parent, real_parent


class TestWorkspaceRootRealpath:
    def test_root_is_realpathed_through_directory_alias(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        alias_form, real_form = _make_aliased_workspace(tmp_path)
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(alias_form))

        root = workspace_root()

        # The returned root must be the resolved form, not the symlink form.
        assert root == real_form.resolve()
        assert str(root) == os.path.realpath(str(alias_form))

    def test_no_symlink_is_noop(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """When the workspace path has no symlink component, normalization is a
        no-op and the root is returned unchanged (apart from existing)."""
        plain = tmp_path / "plain-ws"
        monkeypatch.setenv("KIROCREW_WORKSPACE", str(plain))

        root = workspace_root()

        assert root == plain.resolve()
        assert root.is_dir()
