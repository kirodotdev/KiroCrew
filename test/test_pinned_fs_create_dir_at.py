"""``pinned_fs.create_and_open_dir_at``: a child directory made relative to a held fd."""

from __future__ import annotations

import os

import pytest

from kiro_crew import pinned_fs

pytestmark = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="needs descriptor-relative opens"
)


def _open_dir(path) -> int:
    return os.open(str(path), pinned_fs.dir_flags())


def test_it_creates_the_child_and_reopens_one_that_exists(tmp_path):
    parent = _open_dir(tmp_path)
    try:
        first = pinned_fs.create_and_open_dir_at(parent, "page", what="page")
        os.close(first)
        assert (tmp_path / "page").is_dir()
        again = pinned_fs.create_and_open_dir_at(parent, "page", what="page")
        os.close(again)
    finally:
        os.close(parent)


def test_a_parent_renamed_away_and_replaced_by_a_link_still_gets_the_child(tmp_path):
    """The create follows the held descriptor, whatever the name now points at."""
    held = tmp_path / "held"
    held.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    parent = _open_dir(held)
    try:
        held.rename(tmp_path / "moved")
        held.symlink_to(target, target_is_directory=True)
        fd = pinned_fs.create_and_open_dir_at(parent, "page", what="page")
        os.close(fd)
    finally:
        os.close(parent)
    assert list(target.iterdir()) == []
    assert (tmp_path / "moved" / "page").is_dir()


def test_a_link_at_the_child_name_is_refused(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "page").symlink_to(target, target_is_directory=True)
    parent = _open_dir(tmp_path)
    try:
        with pytest.raises(pinned_fs.PinnedPathRefusal):
            pinned_fs.create_and_open_dir_at(parent, "page", what="page")
    finally:
        os.close(parent)


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_a_name_that_is_not_one_component_is_refused(tmp_path, name):
    parent = _open_dir(tmp_path)
    try:
        with pytest.raises(pinned_fs.PinnedPathRefusal):
            pinned_fs.create_and_open_dir_at(parent, name, what="page")
    finally:
        os.close(parent)
