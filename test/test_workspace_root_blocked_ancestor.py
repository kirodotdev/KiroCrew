"""Creating the workspace root names the path that blocks it.

``mkdir(parents=True)`` fails with a bare ``FileExistsError`` or
``NotADirectoryError`` from inside pathlib when a component of the root is a
dangling link (``~/workplace`` pointing at a volume not mounted yet) or a file.
The gateway cannot start without the root, so the error must name the component
and the fix instead.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.config import loader


@requires_symlinks
def test_a_dangling_link_ancestor_is_named(tmp_path: Path) -> None:
    link = tmp_path / "workplace"
    link.symlink_to(tmp_path / "not-mounted")
    root = link / "kirocrew-workspace"

    with pytest.raises(NotADirectoryError) as caught:
        loader._resolve_workspace_root(root)

    message = str(caught.value)
    assert caught.value.filename == str(link)
    assert "does not exist" in message
    assert "KIROCREW_WORKSPACE" in message
    # Nothing was created through the link.
    assert not os.path.lexists(tmp_path / "not-mounted")


def test_a_file_ancestor_is_named(tmp_path: Path) -> None:
    blocker = tmp_path / "workplace"
    blocker.write_text("")
    root = blocker / "kirocrew-workspace"

    with pytest.raises(NotADirectoryError) as caught:
        loader._resolve_workspace_root(root)

    assert caught.value.filename == str(blocker)
    assert "is not a directory" in str(caught.value)


def test_a_file_at_the_root_itself_is_named(tmp_path: Path) -> None:
    root = tmp_path / "kirocrew-workspace"
    root.write_text("")

    with pytest.raises(NotADirectoryError) as caught:
        loader._resolve_workspace_root(root)

    assert caught.value.filename == str(root)


@requires_symlinks
def test_a_link_to_a_directory_still_works(tmp_path: Path) -> None:
    real = tmp_path / "volume"
    real.mkdir()
    (tmp_path / "workplace").symlink_to(real)

    resolved = loader._resolve_workspace_root(tmp_path / "workplace" / "kirocrew-workspace")

    assert resolved == Path(os.path.realpath(real / "kirocrew-workspace"))
    assert resolved.is_dir()


def test_the_error_is_still_an_os_error(tmp_path: Path) -> None:
    """Callers that catch ``OSError`` around the workspace root keep catching it."""
    blocker = tmp_path / "workplace"
    blocker.write_text("")

    with pytest.raises(OSError):
        loader._resolve_workspace_root(blocker / "kirocrew-workspace")
