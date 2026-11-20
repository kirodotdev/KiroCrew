# SPDX-License-Identifier: Apache-2.0
"""A link at ``members`` is opened by descriptor and the opened object is vetted.

``open_member_folder`` opens what the link at ``members`` names ONCE, relative to the held
data home, and vets the opened object by its identity (``fstat``) against the data home
and every folder above it. No name is used after that. A chain of more than 40 links is
refused (the kernel stops at 40), and a link whose text is not text is refused.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from conftest import requires_symlinks
from kiro_crew import members, pinned_fs
from kiro_crew.member_folder import open_member_folder

SLUG = "ada"

needs_pinned_walk = pytest.mark.skipif(
    not pinned_fs.supports_pinned_walk(), reason="descriptor open needs a pinned walk"
)


def _patch_open(monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    """Replace ``os.open``; the spy must stay a function that takes ``dir_fd=`` or the
    platform check (``os.open in os.supports_dir_fd``) turns the pinned walk off."""
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {spy})
    monkeypatch.setattr(os, "open", spy)


def _members() -> Path:
    return members.members_root()


def _open() -> Exception | None:
    try:
        with open_member_folder(members.member_slug(SLUG)):
            pass
    except members.MemberSlugError as exc:
        return exc
    return None


def _outside(tmp_path: Path) -> Path:
    outside = tmp_path / "outside"
    outside.mkdir()
    return outside


@requires_symlinks
@needs_pinned_walk
def test_no_name_is_used_after_the_screen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = _outside(tmp_path)
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, named)
    base = named.parent.resolve()
    by_name: list[str] = []
    me = threading.get_ident()
    real_open = os.open
    real_resolve = Path.resolve

    def spy_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # Only this thread: a background writer (an audit log) also calls the patched open.
        if kwargs.get("dir_fd") is None and threading.get_ident() == me:
            by_name.append(os.fspath(path))
        return real_open(path, flags, *args, **kwargs)

    def spy_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
        if self == named:
            raise AssertionError("the link at members was resolved by name")
        return real_resolve(self, *args, **kwargs)

    _patch_open(monkeypatch, spy_open)
    monkeypatch.setattr(Path, "resolve", spy_resolve)
    result = _open()
    assert result is None, repr(result.__cause__)
    assert set(by_name) <= {str(base)}, by_name
    assert (outside / members.member_slug(SLUG)).is_dir()


@requires_symlinks
@needs_pinned_walk
@pytest.mark.parametrize("levels_up", [0, 1, 2])
def test_the_opened_object_is_vetted_by_identity_against_the_data_home_and_above(
    levels_up: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The link is re-pointed at the moment it is opened: what is opened is what is vetted."""
    outside = _outside(tmp_path)
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, named)
    forbidden = named.parent.resolve()
    for _ in range(levels_up):
        forbidden = forbidden.parent
    real_open = os.open
    fired: list[bool] = []

    def repoint_when_opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        follows = not flags & os.O_NOFOLLOW
        if path == named.name and kwargs.get("dir_fd") is not None and follows and not fired:
            fired.append(True)
            named.unlink()
            os.symlink(forbidden, named)
        return real_open(path, flags, *args, **kwargs)

    _patch_open(monkeypatch, repoint_when_opened)
    result = _open()
    assert fired, "the link at members was never opened by descriptor"
    assert isinstance(result, members.MemberSlugError), result
    assert not (forbidden / members.member_slug(SLUG)).exists()


@requires_symlinks
@needs_pinned_walk
def test_a_chain_of_more_than_forty_links_is_refused(tmp_path: Path) -> None:
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    (named.parent / "real").mkdir()
    count = 45
    for index in range(1, count):
        os.symlink(f"hop{index + 1}", named.parent / f"hop{index}")
    os.symlink("real", named.parent / f"hop{count}")
    os.symlink("hop1", named)
    assert isinstance(_open(), members.MemberSlugError)
    assert not (named.parent / "real" / members.member_slug(SLUG)).exists()


@requires_symlinks
@needs_pinned_walk
@pytest.mark.skipif(sys.platform == "win32", reason="names that are not text are a POSIX case")
def test_a_link_whose_target_is_not_text_is_refused_by_reading_the_link_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    target = os.fsencode(tmp_path) + b"/folder-\xff"
    try:
        os.mkdir(target)
    except OSError:
        pytest.skip("this file system refuses names that are not text")
    os.symlink(target, os.fsencode(named))
    reads: list[Any] = []
    real_readlink = os.readlink

    def spy_readlink(path: Any, *args: Any, **kwargs: Any) -> Any:
        reads.append(kwargs.get("dir_fd"))
        return real_readlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "readlink", spy_readlink)
    assert isinstance(_open(), members.MemberSlugError)
    assert reads and all(fd is not None for fd in reads), reads


def _follow_open(path: Any, flags: int, kwargs: dict[str, Any], name: str) -> bool:
    return path == name and kwargs.get("dir_fd") is not None and not flags & os.O_NOFOLLOW


@requires_symlinks
@needs_pinned_walk
def test_a_link_pointed_elsewhere_after_it_is_opened_still_writes_to_the_opened_folder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened = _outside(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(opened, named)
    real_open = os.open
    fired: list[bool] = []

    def repoint_once_opened(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        fd = real_open(path, flags, *args, **kwargs)
        if _follow_open(path, flags, kwargs, named.name) and not fired:
            fired.append(True)
            named.unlink()
            os.symlink(elsewhere, named)
        return fd

    _patch_open(monkeypatch, repoint_once_opened)
    assert _open() is None
    assert fired
    assert (opened / members.member_slug(SLUG)).is_dir()
    assert list(elsewhere.iterdir()) == []


@requires_symlinks
@needs_pinned_walk
def test_a_link_removed_before_it_is_opened_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    opened = _outside(tmp_path)
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(opened, named)
    real_open = os.open
    fired: list[bool] = []

    def remove_before_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if _follow_open(path, flags, kwargs, named.name) and not fired:
            fired.append(True)
            named.unlink()
        return real_open(path, flags, *args, **kwargs)

    _patch_open(monkeypatch, remove_before_open)
    assert isinstance(_open(), members.MemberSlugError)
    assert fired
    assert list(opened.iterdir()) == []


@requires_symlinks
@needs_pinned_walk
@pytest.mark.skipif(sys.platform == "win32", reason="search-only folders are a POSIX notion")
def test_a_folder_above_the_data_home_that_can_be_searched_but_not_read_is_no_obstacle(
    tmp_path: Path,
) -> None:
    """The identity walk needs search permission on the folders above, never read."""
    outside = _outside(tmp_path)
    named = _members()
    named.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(outside, named)
    above = named.parent.resolve().parent
    mode = above.stat().st_mode & 0o7777
    above.chmod(0o111)
    try:
        if os.access(above, os.R_OK):
            pytest.skip("this user can read a folder without the read bit (root)")
        result = _open()
    finally:
        above.chmod(mode)
    assert result is None, repr(result.__cause__)
    assert (outside / members.member_slug(SLUG)).is_dir()
